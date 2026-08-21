import * as childProcess from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import * as vscode from "vscode";
import {
  ConnectionState,
  ControlPlaneAIStatusV1,
  ControlPlaneCapabilitiesV1,
  ControlPlaneStatusV1,
} from "./api/contracts";
import { ChappieControlPlaneClient, ControlPlaneClientError, ControlPlaneDependencyError, ControlPlaneIncompatibleError } from "./api/client";
import { appendTurn, ChatTurnV1, renderTurns } from "./ui/chat";
import { capabilitySummary, contextPreview, humanState, projectChatAvailability, projectQaFailureMessage, setupActionLabel } from "./ui/presentation";
import { ChappieStatusSidebarV1, StatusRefreshGateV1 } from "./ui/status";
import { ChappieWorkbenchV1 } from "./ui/workbench";
import { EditorContextInputV1, WorkspaceContextCollector } from "./workspace/context";
import { collectGitMetadata } from "./workspace/git_metadata";
import { queryNvidiaTelemetry } from "./workspace/runtime_telemetry";
import { ChappieSetupTerminalV1, isChappieWorkspace, localWorkspaceState, OwnedControlPlaneProcess, readSetupFailureSummary, resolveChappieExecutable, resolveChappieSetupCommand, setupBootstrapRemediation, shouldAutoStartControlPlane } from "./workspace/workspace";

const CONTROL_PLANE_STARTUP_TIMEOUT_MS = 15_000;
const CONTROL_PLANE_STARTUP_POLL_MS = 250;

interface Snapshot {
  status?: ControlPlaneStatusV1;
  ai?: ControlPlaneAIStatusV1;
  capabilities?: ControlPlaneCapabilitiesV1;
}

class ChappieTree implements vscode.TreeDataProvider<vscode.TreeItem> {
  private readonly changed = new vscode.EventEmitter<void>();
  public readonly onDidChangeTreeData = this.changed.event;
  private state: ConnectionState = "DISCONNECTED";
  private snapshot: Snapshot = {};

  public update(state: ConnectionState, snapshot: Snapshot = {}): void {
    this.state = state;
    this.snapshot = snapshot;
    this.changed.fire();
  }

  public getTreeItem(item: vscode.TreeItem): vscode.TreeItem { return item; }

  public getChildren(item?: vscode.TreeItem): vscode.TreeItem[] {
    if (item) { return []; }
    const rows = [
      `● ${humanState(this.state)}`,
      `System: ${humanState(String(this.snapshot.status?.status?.overall_state ?? "UNKNOWN"))}`,
      `AI: ${this.snapshot.ai?.model ?? humanState(this.snapshot.ai?.provider_state)}`,
      `Knowledge: ${humanState(componentState(this.snapshot.status, "KNOWLEDGE"))}`,
    ];
    const capabilities = this.snapshot.capabilities?.capabilities ?? [];
    rows.push(`Capabilities: ${capabilitySummary(capabilities)}`);
    return rows.map((label) => {
      const treeItem = new vscode.TreeItem(label, vscode.TreeItemCollapsibleState.None);
      if (label.startsWith("Capabilities:")) {
        treeItem.command = { command: "chappie.showCapabilities", title: "Chappie: Show Capabilities" };
      }
      if (label === "System: SETUP_REQUIRED") {
        treeItem.command = { command: "chappie.runSetup", title: "Chappie: Set Up" };
      }
      return treeItem;
    });
  }
}

class ChappieChatView implements vscode.WebviewViewProvider {
  private view: vscode.WebviewView | undefined;
  private history: ChatTurnV1[] = [];
  private state: ConnectionState = "DISCONNECTED";
  private model: string | null = null;
  private availability = "Chappie setup is required before the local service can connect.";
  private setupAction = "Set up";
  private preview = "";

  public constructor(private readonly client: () => ChappieControlPlaneClient) {}

  public resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    view.webview.options = { enableScripts: true };
    view.webview.html = chatHtml();
    view.webview.onDidReceiveMessage(async (message: unknown) => {
      if (isSetupMessage(message)) {
        await vscode.commands.executeCommand(
          this.setupAction === "Reconnect" ? "chappie.startLocalControlPlane" : "chappie.runSetup",
        );
        return;
      }
      if (isContextPreviewMessage(message)) {
        this.preview = contextPreviewFor(message);
        this.render();
        return;
      }
      if (!isQuestionMessage(message)) { return; }
      const question = message.question.trim();
      if (this.availability) {
        this.add({ question, error: `${this.availability} Use ${this.setupAction}.` });
        return;
      }
      try {
        this.add({ question, response: await this.client().askProject(question, collectWorkspaceContext(message)) });
      } catch (error) {
        this.add({ question, error: chatError(error) });
      }
    });
    this.render();
  }

  public update(state: ConnectionState, model: string | null, availability: string | null, setupAction = "Set up"): void {
    this.state = state;
    this.model = model;
    this.availability = availability ?? "";
    this.setupAction = setupAction;
    this.render();
  }

  private add(turn: ChatTurnV1): void { this.history = appendTurn(this.history, turn); this.render(); }
  private render(): void {
    this.view?.webview.postMessage({ type: "render", state: this.state, model: this.model, availability: this.availability, setupAction: this.setupAction, preview: this.preview, html: renderTurns(this.history) });
  }
}

function componentState(status: ControlPlaneStatusV1 | undefined, component: string): string {
  const payload = status?.status;
  const components = Array.isArray(payload?.components) ? payload.components : [];
  const found = components.find((item) => typeof item === "object" && item !== null && (item as { component?: string }).component === component) as { state?: string } | undefined;
  return found?.state ?? "UNKNOWN";
}

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  const output = vscode.window.createOutputChannel("Chappie");
  const bar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
  const tree = new ChappieStatusSidebarV1((action) => {
    if (action === "newChat") { void vscode.commands.executeCommand("chappie.newChat"); }
    else if (action === "refresh") { void vscode.commands.executeCommand("chappie.refresh"); }
    else { void vscode.commands.executeCommand("chappie.showDoctor"); }
  });
  const ownedProcess = new OwnedControlPlaneProcess();
  const setupTerminal = new ChappieSetupTerminalV1();
  const setupResultPath = path.join(context.globalStorageUri.fsPath, "setup-result.json");
  let workbench: ChappieWorkbenchV1 | undefined;
  // A refresh may be requested by activation, an explicit command and setup
  // completion at the same time. Only the newest completed snapshot may drive
  // the sidebar and Workbench.
  const statusRefreshGate = new StatusRefreshGateV1();
  const workbenchContext = new Set<"includeFile" | "includeSelection" | "includeGit">();
  const workbenchInputs = new Map<"includeFile" | "includeSelection" | "includeGit", EditorContextInputV1>();
  const configuredPort = (): number => vscode.workspace.getConfiguration("chappie").get<number>("controlPlane.port", 8765);
  const platform = globalThis.process.platform as "win32" | "darwin" | "linux";
  const localState = (): "UNCONFIGURED" | "DEGRADED" | "READY" => {
    const root = workspaceRoot();
    return root ? localWorkspaceState(root, platform, fs.existsSync) : "UNCONFIGURED";
  };
  const chat = new ChappieChatView(() => new ChappieControlPlaneClient(configuredPort()));
  context.subscriptions.push(output, bar, vscode.window.registerWebviewViewProvider("chappie.status", tree), { dispose: () => { ownedProcess.stop(); } });
  bar.command = "chappie.openView";

  const connect = async (): Promise<ConnectionState> => {
    const generation = statusRefreshGate.begin();
    tree.update("CONNECTING"); renderStatus(bar, "CONNECTING");
    const client = new ChappieControlPlaneClient(configuredPort());
    try {
      await client.health();
      const [status, ai, capabilities] = await Promise.all([client.status(), client.aiStatus(), client.capabilities()]);
      if (!statusRefreshGate.isCurrent(generation)) return "CONNECTED";
      tree.update("CONNECTED", { status, ai, capabilities });
      const projectQa = capabilities.capabilities.find((item) => item.capability_id === "project.qa");
      const knowledge = componentState(status, "KNOWLEDGE");
      const availability = projectChatAvailability("CONNECTED", projectQa?.status, ai.provider_state, knowledge, localState());
      const setupAction = setupActionLabel("CONNECTED", projectQa?.status, ai.provider_state, knowledge, localState());
      chat.update(
        "CONNECTED",
        ai.model,
        availability,
        setupAction,
      );
      const system = String(status.status?.overall_state ?? "UNKNOWN");
      workbench?.updateConnection({ state: "CONNECTED", system, model: ai.model, knowledge, availability, setupAction });
      renderStatus(bar, "CONNECTED");
      output.appendLine("Control Plane connected.");
      return "CONNECTED";
    } catch (error) {
      const state: ConnectionState = error instanceof ControlPlaneIncompatibleError ? "INCOMPATIBLE" : "DISCONNECTED";
      if (!statusRefreshGate.isCurrent(generation)) return state;
      tree.update(state);
      const availability = projectChatAvailability(state, undefined, undefined, "UNKNOWN", localState());
      const setupAction = setupActionLabel(state, undefined, undefined, "UNKNOWN", localState());
      chat.update(
        state,
        null,
        availability,
        setupAction,
      );
      workbench?.updateConnection({ state, system: "UNKNOWN", model: null, knowledge: "UNKNOWN", availability, setupAction });
      renderStatus(bar, state);
      const message = error instanceof Error ? error.message : "Control Plane request failed";
      output.appendLine(`${state}: ${message}`);
      if (state === "INCOMPATIBLE") { void vscode.window.showWarningMessage(message, "Chappie: Refresh"); }
      return state;
    }
  };

  const refreshAfterSetup = async (): Promise<void> => {
    output.appendLine("Chappie setup completed; refreshing local status.");
    const connection = await connect();
    if (shouldAutoStartControlPlane(true, true, connection) && !ownedProcess.running) {
      await startOwned();
    }
  };

  const startOwned = async (): Promise<boolean> => {
    const root = workspaceRoot();
    if (!root) {
      output.appendLine("Chappie development environment is not set up.");
      return false;
    }
    const command = resolveChappieExecutable(root, platform, fs.existsSync);
    if (command.command === "chappie") {
      output.appendLine("Chappie development environment is not set up. Run the project setup first.");
      return false;
    }
    const args = [...command.args, "--port", String(configuredPort())];
    output.appendLine(`Auto-starting Control Plane: ${command.command} ${args.join(" ")}`);
    let exited = false;
    let child: childProcess.ChildProcess | undefined;
    try {
      child = ownedProcess.start(
        { ...command, args },
        (file, childArgs, options) => childProcess.spawn(
          file,
          childArgs,
          { cwd: options.cwd, shell: options.shell, stdio: "pipe" },
        ),
      ) as childProcess.ChildProcess | undefined;
    } catch (error) {
      output.appendLine(`Control Plane spawn failed: ${safeDiagnostic(error)}`);
      return false;
    }
    if (!child) {
      output.appendLine("Control Plane auto-start skipped: extension-owned process is already running.");
      return false;
    }
    output.appendLine(`Control Plane child spawned (pid ${child.pid ?? "unknown"}).`);
    child.once("error", (error) => output.appendLine(`Control Plane spawn error: ${safeDiagnostic(error)}`));
    child.once("exit", (code, signal) => {
      exited = true;
      ownedProcess.release(child!);
      output.appendLine(`Control Plane child exited (code ${code ?? "none"}, signal ${signal ?? "none"}).`);
    });
    child.stderr?.on("data", (chunk: Buffer) => {
      output.appendLine(`Control Plane stderr: ${safeDiagnostic(chunk.toString())}`);
    });
    const deadline = Date.now() + CONTROL_PLANE_STARTUP_TIMEOUT_MS;
    while (Date.now() < deadline && !exited) {
      await new Promise((resolve) => setTimeout(resolve, CONTROL_PLANE_STARTUP_POLL_MS));
      try {
        await new ChappieControlPlaneClient(configuredPort()).health();
        output.appendLine("Control Plane readiness handshake succeeded.");
        await connect();
        return true;
      } catch (_error) { /* bounded wait; final result is logged below */ }
    }
    output.appendLine(
      exited
        ? "Control Plane exited before the readiness handshake."
        : `Control Plane readiness timed out after ${CONTROL_PLANE_STARTUP_TIMEOUT_MS}ms.`,
    );
    return false;
  };

  const runSetup = (): void => {
    const root = workspaceRoot();
    const platform = globalThis.process.platform as "win32" | "darwin" | "linux";
    if (!root) {
      void vscode.window.showWarningMessage("Open a recognized Chappie workspace before running setup.");
      return;
    }
    const command = resolveChappieSetupCommand(root, platform, fs.existsSync, setupResultPath);
    if (!command) {
      void vscode.window.showWarningMessage(setupBootstrapRemediation(platform));
      return;
    }
    const existing = vscode.window.terminals.find((terminal) => terminal.name === "Chappie Setup");
    if (existing) {
      setupTerminal.adopt(existing);
      output.appendLine("Revealed existing Chappie Setup terminal.");
      return;
    }
    fs.rmSync(setupResultPath, { force: true });
    setupTerminal.launch(command, (options) => vscode.window.createTerminal(options));
    output.appendLine(`Started canonical interactive setup in ${command.command}.`);
  };

  workbench = new ChappieWorkbenchV1({
    askProject: (question, options) => new ChappieControlPlaneClient(configuredPort()).askProject(question, collectAttachedWorkbenchContext(options, workbenchContext, workbenchInputs)),
    attachContext: async (kind) => {
      let editor = vscode.window.activeTextEditor;
      if (kind === "includeFile") {
        const root = workspaceRoot();
        const picked = root ? await vscode.window.showOpenDialog({ canSelectFiles: true, canSelectFolders: false, canSelectMany: false, defaultUri: vscode.Uri.file(root), title: "Attach workspace file to Chappie" }) : undefined;
        if (!picked?.[0]) throw new Error("No workspace file was selected.");
        const document = await vscode.workspace.openTextDocument(picked[0]);
        const folder = vscode.workspace.getWorkspaceFolder(document.uri);
        if (!folder || document.isUntitled || document.uri.scheme !== "file") throw new Error("Select a file inside the current workspace.");
        workbenchInputs.set(kind, editorInputFor(document, folder, undefined));
      } else {
        if (kind === "includeSelection" && (!editor || editor.selection.isEmpty)) throw new Error("Select text in a workspace file before attaching it.");
        if (kind !== "includeGit" && (!editor || editor.document.isUntitled || editor.document.uri.scheme !== "file" || !vscode.workspace.getWorkspaceFolder(editor.document.uri))) throw new Error("Open a workspace file before attaching context.");
        if (kind === "includeGit") {
          const root = workspaceRoot(); if (!root) throw new Error("Open a recognized workspace before attaching Git context.");
          const folder = vscode.workspace.workspaceFolders?.find((item) => item.uri.fsPath === root); if (!folder) throw new Error("Workspace context is unavailable.");
          workbenchInputs.set(kind, { workspaceRoot: root, workspaceName: folder.name, git: collectGitMetadata(root, gitExec) ?? undefined });
        } else { workbenchInputs.set(kind, editorInputFor(editor!.document, vscode.workspace.getWorkspaceFolder(editor!.document.uri)!, kind === "includeSelection" ? editor!.selection : undefined)); }
      }
      const options = { includeFile: kind === "includeFile", includeSelection: kind === "includeSelection", includeGit: kind === "includeGit" };
      const preview = contextPreview(new WorkspaceContextCollector().collect(workbenchInputs.get(kind)!, options));
      if (preview.includes("Sensitive file blocked")) throw new Error("Sensitive files cannot be attached as context.");
      workbenchContext.add(kind);
      const label = kind === "includeFile" ? `File · ${path.basename(workbenchInputs.get(kind)!.filePath!)}` : kind === "includeSelection" ? `Selection · ${editor!.selection.end.line - editor!.selection.start.line + 1} lines` : "Git · workspace metadata";
      return { kind, label, detail: preview || "Bounded workspace context" };
    },
    removeContext: (kind) => { workbenchContext.delete(kind); workbenchInputs.delete(kind); },
    clearContexts: () => { workbenchContext.clear(); workbenchInputs.clear(); },
    previewContext: (kind) => contextPreview(new WorkspaceContextCollector().collect(workbenchInputs.get(kind)!, { includeFile: kind === "includeFile", includeSelection: kind === "includeSelection", includeGit: kind === "includeGit" })),
    runtimeTelemetry: queryNvidiaTelemetry,
    trace: (message) => output.appendLine(message),
    runAction: async (action) => {
      if (action === "diagnostics") {
        await vscode.commands.executeCommand("chappie.showDoctor");
      } else if (action === "reconnect") {
        await startOwned();
      } else {
        runSetup();
      }
    },
    workspaceName: () => vscode.workspace.workspaceFolders?.find((folder) => folder.uri.fsPath === workspaceRoot())?.name,
    errorMessage: chatError,
  });
  context.subscriptions.push(workbench);

  context.subscriptions.push(vscode.window.onDidCloseTerminal((terminal) => {
    setupTerminal.closed(
      terminal,
      () => {
        fs.rmSync(setupResultPath, { force: true });
        void refreshAfterSetup();
      },
      () => {
        const failure = readSetupFailureSummary(setupResultPath, fs.readFileSync);
        fs.rmSync(setupResultPath, { force: true });
        const message = failure?.message ?? "Chappie setup did not complete. Review the Chappie Setup terminal.";
        output.appendLine(`${message} Keeping the current status.`);
        void vscode.window.showWarningMessage(message);
      },
    );
  }));

  context.subscriptions.push(
    vscode.commands.registerCommand("chappie.refresh", connect),
    vscode.commands.registerCommand("chappie.openWorkbench", () => workbench?.open()),
    vscode.commands.registerCommand("chappie.newChat", () => workbench?.newChat()),
    vscode.commands.registerCommand("chappie.openView", () => vscode.commands.executeCommand("workbench.view.extension.chappie")),
    vscode.commands.registerCommand("chappie.showDoctor", async () => {
      const client = new ChappieControlPlaneClient(configuredPort());
      try {
        const report = await client.diagnostics();
        output.show(true);
        output.appendLine(JSON.stringify(report.diagnostics, null, 2));
      } catch (error) {
        void vscode.window.showErrorMessage(error instanceof Error ? error.message : "Chappie doctor unavailable");
      }
    }),
    vscode.commands.registerCommand("chappie.showCapabilities", async () => {
      const client = new ChappieControlPlaneClient(configuredPort());
      try {
        output.show(true);
        output.appendLine(JSON.stringify((await client.capabilities()).capabilities, null, 2));
      } catch (error) {
        void vscode.window.showErrorMessage(error instanceof Error ? error.message : "Chappie capabilities unavailable");
      }
    }),
    vscode.commands.registerCommand("chappie.runSetup", runSetup),
    vscode.commands.registerCommand("chappie.startLocalControlPlane", async () => {
      await startOwned();
    }),
    vscode.commands.registerCommand("chappie.stopLocalControlPlane", () => {
      const stopped = ownedProcess.stop();
      output.appendLine(stopped ? "Stopped extension-owned Control Plane process." : "No extension-owned Control Plane process to stop.");
      void connect();
    }),
  );
  output.appendLine("Chappie extension activated.");
  const root = workspaceRoot();
  if (!root) {
    const availability = projectChatAvailability("DISCONNECTED", undefined, undefined, "UNKNOWN", "UNCONFIGURED");
    tree.update("DISCONNECTED"); chat.update("DISCONNECTED", null, availability, "Set up"); workbench.updateConnection({ state: "DISCONNECTED", system: "UNKNOWN", model: null, knowledge: "UNKNOWN", availability, setupAction: "Set up" }); renderStatus(bar, "DISCONNECTED");
    output.appendLine("No recognized Chappie workspace is open.");
  } else if (vscode.workspace.getConfiguration("chappie").get<boolean>("autoConnect", true)) {
    output.appendLine(`Recognized Chappie workspace: ${root}`);
    const connection = await connect();
    if (shouldAutoStartControlPlane(true, vscode.workspace.getConfiguration("chappie").get<boolean>("autoStartControlPlane", true), connection) && !ownedProcess.running) {
      await startOwned();
    }
  } else {
    const availability = projectChatAvailability("DISCONNECTED", undefined, undefined, "UNKNOWN", localState());
    const setupAction = setupActionLabel("DISCONNECTED", undefined, undefined, "UNKNOWN", localState());
    tree.update("DISCONNECTED"); chat.update("DISCONNECTED", null, availability, setupAction); workbench.updateConnection({ state: "DISCONNECTED", system: "UNKNOWN", model: null, knowledge: "UNKNOWN", availability, setupAction }); renderStatus(bar, "DISCONNECTED");
  }
}

function workspaceRoot(): string | undefined {
  return vscode.workspace.workspaceFolders?.find((folder) => isChappieWorkspace(
    folder.uri.fsPath,
    fs.existsSync,
    (candidate) => {
      try { return fs.statSync(candidate).isDirectory(); } catch { return false; }
    },
  ))?.uri.fsPath;
}

function renderStatus(item: vscode.StatusBarItem, state: ConnectionState): void {
  item.text = `Chappie: ${state.charAt(0)}${state.slice(1).toLowerCase()}`;
  item.show();
}

function safeDiagnostic(value: unknown): string {
  return String(value).replace(/[\r\n\t]+/g, " ").slice(0, 300);
}

export function deactivate(): void {}

function isQuestionMessage(value: unknown): value is { type: "ask"; question: string; includeFile?: boolean; includeSelection?: boolean; includeGit?: boolean } {
  return typeof value === "object" && value !== null && (value as { type?: unknown }).type === "ask" && typeof (value as { question?: unknown }).question === "string" && (value as { question: string }).question.trim().length > 0 && (value as { question: string }).question.length <= 1000;
}

function isContextPreviewMessage(value: unknown): value is { type: "context-preview"; includeFile?: boolean; includeSelection?: boolean; includeGit?: boolean } {
  return typeof value === "object" && value !== null && (value as { type?: unknown }).type === "context-preview";
}

function isSetupMessage(value: unknown): value is { type: "setup" } {
  return typeof value === "object" && value !== null && (value as { type?: unknown }).type === "setup";
}

function collectWorkspaceContext(message: { includeFile?: boolean; includeSelection?: boolean; includeGit?: boolean }) {
  const editor = vscode.window.activeTextEditor;
  const folder = editor ? vscode.workspace.getWorkspaceFolder(editor.document.uri) : undefined;
  if (!editor || !folder) return undefined;
  const document = editor.document;
  if (document.isUntitled || document.uri.scheme !== "file") throw new Error("UNSUPPORTED_DOCUMENT");
  const start = document.offsetAt(editor.selection.start);
  const end = document.offsetAt(editor.selection.end);
  const git = message.includeGit ? collectGitMetadata(folder.uri.fsPath, (args, cwd) => childProcess.execFileSync("git", args, { cwd, shell: false, timeout: 1500, maxBuffer: 4096, encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] })) : undefined;
  return new WorkspaceContextCollector().collect({ workspaceRoot: folder.uri.fsPath, workspaceName: folder.name, filePath: document.uri.fsPath, languageId: document.languageId, text: document.getText(), selection: start === end ? undefined : { start, end }, cursorLine: editor.selection.active.line, git: git ?? undefined }, { includeFile: Boolean(message.includeFile), includeSelection: Boolean(message.includeSelection), includeGit: Boolean(message.includeGit) });
}

function contextPreviewFor(message: { includeFile?: boolean; includeSelection?: boolean; includeGit?: boolean }): string {
  try {
    return contextPreview(collectWorkspaceContext(message));
  } catch (error) {
    return error instanceof Error && error.message === "SENSITIVE_FILE_BLOCKED"
      ? "Sensitive file blocked. Context was not sent."
      : "Workspace context is unavailable for the active editor.";
  }
}

function gitExec(args: readonly string[], cwd: string): string {
  return childProcess.execFileSync("git", [...args], { cwd, shell: false, timeout: 1500, maxBuffer: 4096, encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] });
}

function editorInputFor(document: vscode.TextDocument, folder: vscode.WorkspaceFolder, selection: vscode.Selection | undefined): EditorContextInputV1 {
  const start = selection ? document.offsetAt(selection.start) : undefined;
  const end = selection ? document.offsetAt(selection.end) : undefined;
  return { workspaceRoot: folder.uri.fsPath, workspaceName: folder.name, filePath: document.uri.fsPath, languageId: document.languageId, text: document.getText(), selection: start === undefined || end === undefined || start === end ? undefined : { start, end }, cursorLine: selection?.active.line ?? 0 };
}

function collectAttachedWorkbenchContext(options: { includeFile: boolean; includeSelection: boolean; includeGit: boolean }, attached: ReadonlySet<"includeFile" | "includeSelection" | "includeGit">, inputs: ReadonlyMap<"includeFile" | "includeSelection" | "includeGit", EditorContextInputV1>) {
  const includeFile = options.includeFile && attached.has("includeFile");
  const includeSelection = options.includeSelection && attached.has("includeSelection");
  const includeGit = options.includeGit && attached.has("includeGit");
  if (!includeFile && !includeSelection && !includeGit) return undefined;
  const primary = (includeSelection ? inputs.get("includeSelection") : undefined) ?? (includeFile ? inputs.get("includeFile") : undefined) ?? inputs.get("includeGit");
  if (!primary) return undefined;
  const git = includeGit ? inputs.get("includeGit")?.git : undefined;
  return new WorkspaceContextCollector().collect({ ...primary, git }, { includeFile, includeSelection, includeGit });
}

function chatError(error: unknown): string {
  if (error instanceof ControlPlaneDependencyError) { return projectQaFailureMessage(error.reasonCode); }
  if (error instanceof ControlPlaneIncompatibleError) { return "Chappie Control Plane version is incompatible."; }
  if (error instanceof ControlPlaneClientError) { return error.message; }
  return "Chappie Project QA is temporarily unavailable.";
}

function chatHtml(): string {
  const nonce = Math.random().toString(36).slice(2);
  return `<!doctype html>
<html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';">
<title>Chappie Chat</title>
<style>
:root { color: var(--vscode-foreground); font-family: var(--vscode-font-family); font-size: 13px; }
* { box-sizing: border-box; } body { background: var(--vscode-sideBar-background); color: var(--vscode-foreground); margin: 0; padding: 12px; }
.visually-hidden { height: 1px; margin: -1px; overflow: hidden; position: absolute; width: 1px; }
.readiness { color: var(--vscode-descriptionForeground); font-size: 12px; margin: 0 0 8px; }
#availability { align-items: center; border-top: 1px solid var(--vscode-panel-border); color: var(--vscode-descriptionForeground); display: none; font-size: 12px; gap: 8px; margin: 0 0 8px; padding: 8px 0; }
#setup { background: transparent; border: 1px solid var(--vscode-button-border, var(--vscode-input-border)); color: var(--vscode-textLink-foreground); cursor: pointer; font: inherit; padding: 2px 6px; }
#history { display: grid; gap: 8px; } .empty { color: var(--vscode-descriptionForeground); padding: 4px 0; }
.suggestions { display: grid; gap: 2px; margin-top: 6px; } .suggestion { background: transparent; border: 0; color: var(--vscode-textLink-foreground); cursor: pointer; padding: 3px 0; text-align: left; }
.turn { border: 1px solid var(--vscode-panel-border); padding: 9px; } .turn p { margin: 0 0 8px; } .turn .label { color: var(--vscode-descriptionForeground); font-size: 11px; font-weight: 700; text-transform: uppercase; }
details { color: var(--vscode-descriptionForeground); font-size: 12px; margin-top: 6px; } ul { margin: 5px 0 0; padding-left: 18px; }
.composer { background: var(--vscode-editor-background); border: 1px solid var(--vscode-input-border); margin-top: 8px; padding: 8px; } .composer:focus-within { border-color: var(--vscode-focusBorder); }
textarea { background: transparent; border: 0; color: var(--vscode-input-foreground); font: inherit; min-height: 54px; outline: 0; resize: vertical; width: 100%; }
.chips { display: flex; flex-wrap: wrap; gap: 4px; margin: 4px 0; }
.chip { background: transparent; border: 0; border-radius: 3px; color: var(--vscode-descriptionForeground); cursor: pointer; padding: 3px 5px; } .chip[aria-pressed="true"] { background: var(--vscode-list-activeSelectionBackground); color: var(--vscode-list-activeSelectionForeground); }
#preview { color: var(--vscode-descriptionForeground); font-family: var(--vscode-editor-font-family); font-size: 11px; margin: 4px 0; white-space: pre-line; } #preview:empty { display: none; }
.actions { align-items: center; display: flex; justify-content: flex-end; gap: 8px; } .hint { color: var(--vscode-descriptionForeground); font-size: 11px; margin-right: auto; } button:focus-visible { outline: 1px solid var(--vscode-focusBorder); outline-offset: 2px; }
#send { background: transparent; border: 0; color: var(--vscode-icon-foreground); cursor: pointer; font-size: 16px; line-height: 1; padding: 4px 6px; } #send:hover { background: var(--vscode-toolbar-hoverBackground); } #send:disabled { cursor: default; opacity: .45; }
</style></head><body>
<p id="readiness" class="readiness">Chat unavailable · Setup required</p><p id="availability" role="status"><span id="availability-text"></span><button id="setup" type="button">Set up</button></p><main id="history" aria-live="polite"></main>
<form id="ask" class="composer"><label for="question" class="visually-hidden">Ask Chappie</label><textarea id="question" maxlength="1000" placeholder="Ask Chappie..."></textarea><div class="chips" role="group" aria-label="Workspace context"><button class="chip" id="file" type="button" aria-pressed="false">File</button><button class="chip" id="selection" type="button" aria-pressed="false">Selection</button><button class="chip" id="git" type="button" aria-pressed="false">Git</button></div><p id="preview"></p><div class="actions"><span class="hint">⌘/Ctrl+Enter to send</span><button id="send" type="submit" aria-label="Send question to Chappie">→</button></div></form>
<script nonce="${nonce}">
const vscode=acquireVsCodeApi(),form=document.getElementById('ask'),question=document.getElementById('question'),send=document.getElementById('send'),history=document.getElementById('history'),availability=document.getElementById('availability'),availabilityText=document.getElementById('availability-text'),readiness=document.getElementById('readiness');
const chips=['file','selection','git'].map(id=>document.getElementById(id));
const options=()=>({includeFile:chips[0].getAttribute('aria-pressed')==='true',includeSelection:chips[1].getAttribute('aria-pressed')==='true',includeGit:chips[2].getAttribute('aria-pressed')==='true'});
const preview=()=>vscode.postMessage({type:'context-preview',...options()});
chips.forEach(chip=>chip.addEventListener('click',()=>{chip.setAttribute('aria-pressed',chip.getAttribute('aria-pressed')!=='true');preview();}));
const submit=()=>{if(!question.value.trim()||send.disabled)return;send.disabled=true;send.textContent='Asking…';vscode.postMessage({type:'ask',question:question.value,...options()});};
form.addEventListener('submit',event=>{event.preventDefault();submit();});question.addEventListener('keydown',event=>{if(event.key==='Enter'&&(event.metaKey||event.ctrlKey)){event.preventDefault();submit();}});
document.getElementById('setup').addEventListener('click',()=>vscode.postMessage({type:'setup'}));
window.addEventListener('message',event=>{const data=event.data;if(data.type!=='render')return;const unavailable=Boolean(data.availability);readiness.textContent=data.model?'Ready · '+data.model:(unavailable?'Chat unavailable · Setup required':'Ready');availabilityText.textContent=data.availability&&data.availability.includes('AI and Knowledge')?'Chat requires AI and Knowledge.':data.availability||'';availability.style.display=unavailable?'flex':'none';document.getElementById('setup').textContent=data.setupAction||'Set up';history.innerHTML=data.html||'<section class="empty"><div class="suggestions"><button class="suggestion" type="button">Understand architecture</button><button class="suggestion" type="button">Explain selected code</button><button class="suggestion" type="button">Ask about project decisions</button></div></section>';document.getElementById('preview').textContent=data.preview;send.disabled=unavailable;send.textContent='→';});
window.addEventListener('message',event=>{const data=event.data;if(data.type!=='render')return;const unavailable=Boolean(data.availability),action=data.setupAction||'Set up';if(data.model){readiness.textContent='Ready - '+data.model;}else if(unavailable){readiness.textContent=action==='Reconnect'?'Chat unavailable - service disconnected':action==='Repair'?'Chat unavailable - repair required':'Chat unavailable - setup required';}else{readiness.textContent='Ready';}send.textContent='->';});
history.addEventListener('click',event=>{const target=event.target;if(target.classList&&target.classList.contains('suggestion')){question.value=target.textContent;question.focus();}});
</script></body></html>`;
}
