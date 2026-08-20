import * as childProcess from "node:child_process";
import * as fs from "node:fs";
import * as vscode from "vscode";
import {
  ConnectionState,
  ControlPlaneAIStatusV1,
  ControlPlaneCapabilitiesV1,
  ControlPlaneStatusV1,
} from "./contracts";
import { ChappieControlPlaneClient, ControlPlaneClientError, ControlPlaneDependencyError, ControlPlaneIncompatibleError } from "./client";
import { appendTurn, ChatTurnV1, renderTurns } from "./chat";
import { capabilitySummary, contextPreview, humanState, projectChatAvailability, projectQaFailureMessage } from "./presentation";
import { WorkspaceContextCollector } from "./workspace_context";
import { collectGitMetadata } from "./git_metadata";
import { isChappieWorkspace, OwnedControlPlaneProcess, resolveChappieExecutable, shouldAutoStartControlPlane } from "./workspace";

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
      `Connection: ${humanState(this.state)}`,
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
        treeItem.command = { command: "chappie.showSetupInstructions", title: "Chappie: Show Setup Instructions" };
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
  private availability = "Chappie Control Plane is not connected.";
  private preview = "No workspace context selected.";

  public constructor(private readonly client: () => ChappieControlPlaneClient) {}

  public resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    view.webview.options = { enableScripts: true };
    view.webview.html = chatHtml();
    view.webview.onDidReceiveMessage(async (message: unknown) => {
      if (isContextPreviewMessage(message)) {
        this.preview = contextPreviewFor(message);
        this.render();
        return;
      }
      if (!isQuestionMessage(message)) { return; }
      const question = message.question.trim();
      if (this.availability) {
        this.add({ question, error: `${this.availability} Run Chappie setup to finish configuration.` });
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

  public update(state: ConnectionState, model: string | null, availability: string | null): void {
    this.state = state;
    this.model = model;
    this.availability = availability ?? "";
    this.render();
  }

  private add(turn: ChatTurnV1): void { this.history = appendTurn(this.history, turn); this.render(); }
  private render(): void {
    this.view?.webview.postMessage({ type: "render", state: this.state, model: this.model, availability: this.availability, preview: this.preview, html: renderTurns(this.history) });
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
  const tree = new ChappieTree();
  const ownedProcess = new OwnedControlPlaneProcess();
  const configuredPort = (): number => vscode.workspace.getConfiguration("chappie").get<number>("controlPlane.port", 8765);
  const chat = new ChappieChatView(() => new ChappieControlPlaneClient(configuredPort()));
  context.subscriptions.push(output, bar, vscode.window.registerTreeDataProvider("chappie.status", tree), vscode.window.registerWebviewViewProvider("chappie.chat", chat), { dispose: () => { ownedProcess.stop(); } });
  bar.command = "chappie.openView";

  const connect = async (): Promise<ConnectionState> => {
    tree.update("CONNECTING");
    renderStatus(bar, "CONNECTING");
    const client = new ChappieControlPlaneClient(configuredPort());
    try {
      await client.health();
      const [status, ai, capabilities] = await Promise.all([client.status(), client.aiStatus(), client.capabilities()]);
      tree.update("CONNECTED", { status, ai, capabilities });
      const projectQa = capabilities.capabilities.find((item) => item.capability_id === "project.qa");
      chat.update("CONNECTED", ai.model, projectChatAvailability("CONNECTED", projectQa?.status, ai.provider_state, componentState(status, "KNOWLEDGE")));
      renderStatus(bar, "CONNECTED");
      output.appendLine("Control Plane connected.");
      return "CONNECTED";
    } catch (error) {
      const state: ConnectionState = error instanceof ControlPlaneIncompatibleError ? "INCOMPATIBLE" : "DISCONNECTED";
      tree.update(state);
      chat.update(state, null, projectChatAvailability(state, undefined, undefined, "UNKNOWN"));
      renderStatus(bar, state);
      const message = error instanceof Error ? error.message : "Control Plane request failed";
      output.appendLine(`${state}: ${message}`);
      if (state === "INCOMPATIBLE") { void vscode.window.showWarningMessage(message, "Chappie: Refresh"); }
      return state;
    }
  };

  const startOwned = async (): Promise<boolean> => {
    const root = workspaceRoot();
    if (!root) {
      output.appendLine("Chappie development environment is not set up.");
      return false;
    }
    const command = resolveChappieExecutable(root, globalThis.process.platform as "win32" | "darwin" | "linux", fs.existsSync);
    if (command.command === "chappie") {
      output.appendLine("Chappie development environment is not set up. Run the project setup first.");
      return false;
    }
    ownedProcess.start(
      { ...command, args: [...command.args, "--port", String(configuredPort())] },
      (file, args, options) => childProcess.spawn(file, args, { cwd: options.cwd, shell: options.shell }),
    );
    for (let attempt = 0; attempt < 10; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 250));
      try { await new ChappieControlPlaneClient(configuredPort()).health(); await connect(); return true; } catch (_error) { /* bounded wait */ }
    }
    output.appendLine("Chappie Control Plane did not become ready. Check the Chappie output.");
    return false;
  };

  context.subscriptions.push(
    vscode.commands.registerCommand("chappie.refresh", connect),
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
    vscode.commands.registerCommand("chappie.showSetupInstructions", () => {
      void vscode.window.showInformationMessage("Chappie macOS local AI setup is not implemented. Run .venv/bin/chappie setup --plan to review safe local setup steps.");
    }),
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
    tree.update("DISCONNECTED"); chat.update("DISCONNECTED", null, projectChatAvailability("DISCONNECTED", undefined, undefined, "UNKNOWN")); renderStatus(bar, "DISCONNECTED");
    output.appendLine("No recognized Chappie workspace is open.");
  } else if (vscode.workspace.getConfiguration("chappie").get<boolean>("autoConnect", true)) {
    const connection = await connect();
    if (shouldAutoStartControlPlane(true, vscode.workspace.getConfiguration("chappie").get<boolean>("autoStartControlPlane", true), connection) && !ownedProcess.running) {
      await startOwned();
    }
  } else { tree.update("DISCONNECTED"); chat.update("DISCONNECTED", null, projectChatAvailability("DISCONNECTED", undefined, undefined, "UNKNOWN")); renderStatus(bar, "DISCONNECTED"); }
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

export function deactivate(): void {}

function isQuestionMessage(value: unknown): value is { type: "ask"; question: string; includeFile?: boolean; includeSelection?: boolean; includeGit?: boolean } {
  return typeof value === "object" && value !== null && (value as { type?: unknown }).type === "ask" && typeof (value as { question?: unknown }).question === "string" && (value as { question: string }).question.trim().length > 0 && (value as { question: string }).question.length <= 1000;
}

function isContextPreviewMessage(value: unknown): value is { type: "context-preview"; includeFile?: boolean; includeSelection?: boolean; includeGit?: boolean } {
  return typeof value === "object" && value !== null && (value as { type?: unknown }).type === "context-preview";
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
.header { align-items: center; border-bottom: 1px solid var(--vscode-panel-border); display: flex; justify-content: space-between; padding-bottom: 10px; }
.brand { font-size: 12px; font-weight: 700; letter-spacing: .08em; } .connection { color: var(--vscode-descriptionForeground); font-size: 12px; }
.dot { color: var(--vscode-testing-iconPassed); font-size: 14px; } .subtle { color: var(--vscode-descriptionForeground); margin: 8px 0 12px; white-space: pre-line; }
#availability { background: color-mix(in srgb, var(--vscode-editorWarning-foreground) 12%, transparent); border-left: 2px solid var(--vscode-editorWarning-foreground); color: var(--vscode-foreground); display: none; margin: 12px 0; padding: 8px; }
#history { display: grid; gap: 10px; } .empty { border: 1px dashed var(--vscode-panel-border); color: var(--vscode-descriptionForeground); padding: 12px; }
.suggestions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; } .suggestion { background: var(--vscode-badge-background); border: 0; color: var(--vscode-badge-foreground); cursor: pointer; padding: 4px 7px; }
.turn { border: 1px solid var(--vscode-panel-border); padding: 9px; } .turn p { margin: 0 0 8px; } .turn .label { color: var(--vscode-descriptionForeground); font-size: 11px; font-weight: 700; text-transform: uppercase; }
details { color: var(--vscode-descriptionForeground); font-size: 12px; margin-top: 6px; } ul { margin: 5px 0 0; padding-left: 18px; }
.composer { background: var(--vscode-editor-background); border: 1px solid var(--vscode-input-border); margin-top: 12px; padding: 8px; } .composer:focus-within { outline: 1px solid var(--vscode-focusBorder); }
textarea { background: transparent; border: 0; color: var(--vscode-input-foreground); font: inherit; min-height: 62px; outline: 0; resize: vertical; width: 100%; }
.context-label { color: var(--vscode-descriptionForeground); font-size: 12px; } .chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 6px 0; }
.chip { background: var(--vscode-input-background); border: 1px solid var(--vscode-input-border); color: var(--vscode-input-foreground); cursor: pointer; padding: 4px 8px; } .chip[aria-pressed="true"] { background: var(--vscode-badge-background); border-color: var(--vscode-focusBorder); color: var(--vscode-badge-foreground); }
#preview { color: var(--vscode-descriptionForeground); font-family: var(--vscode-editor-font-family); font-size: 11px; margin: 6px 0; white-space: pre-line; }
.actions { align-items: center; display: flex; justify-content: space-between; gap: 8px; } .hint { color: var(--vscode-descriptionForeground); font-size: 11px; } button:focus-visible { outline: 1px solid var(--vscode-focusBorder); outline-offset: 2px; }
#send { background: var(--vscode-button-background); border: 0; color: var(--vscode-button-foreground); cursor: pointer; padding: 6px 10px; } #send:hover { background: var(--vscode-button-hoverBackground); } #send:disabled { cursor: default; opacity: .6; }
</style></head><body>
<header class="header"><span class="brand">CHAPPIE</span><span class="connection"><span class="dot">●</span> <span id="connection">Disconnected</span></span></header>
<p id="model" class="subtle">Setup required</p><p id="availability" role="status"></p><main id="history" aria-live="polite"></main>
<form id="ask" class="composer"><label for="question" class="visually-hidden">Ask Chappie</label><textarea id="question" maxlength="1000" placeholder="Ask Chappie..."></textarea><div class="context-label">Context</div><div class="chips" role="group" aria-label="Workspace context"><button class="chip" id="file" type="button" aria-pressed="false">File</button><button class="chip" id="selection" type="button" aria-pressed="false">Selection</button><button class="chip" id="git" type="button" aria-pressed="false">Git</button></div><p id="preview">No workspace context selected.</p><div class="actions"><span class="hint">⌘/Ctrl+Enter to send</span><button id="send" type="submit" aria-label="Send question to Chappie">Send</button></div></form>
<script nonce="${nonce}">
const vscode=acquireVsCodeApi(),form=document.getElementById('ask'),question=document.getElementById('question'),send=document.getElementById('send'),history=document.getElementById('history'),availability=document.getElementById('availability');
const chips=['file','selection','git'].map(id=>document.getElementById(id));
const options=()=>({includeFile:chips[0].getAttribute('aria-pressed')==='true',includeSelection:chips[1].getAttribute('aria-pressed')==='true',includeGit:chips[2].getAttribute('aria-pressed')==='true'});
const preview=()=>vscode.postMessage({type:'context-preview',...options()});
chips.forEach(chip=>chip.addEventListener('click',()=>{chip.setAttribute('aria-pressed',chip.getAttribute('aria-pressed')!=='true');preview();}));
const submit=()=>{if(!question.value.trim()||send.disabled)return;send.disabled=true;send.textContent='Asking…';vscode.postMessage({type:'ask',question:question.value,...options()});};
form.addEventListener('submit',event=>{event.preventDefault();submit();});question.addEventListener('keydown',event=>{if(event.key==='Enter'&&(event.metaKey||event.ctrlKey)){event.preventDefault();submit();}});
window.addEventListener('message',event=>{const data=event.data;if(data.type!=='render')return;document.getElementById('connection').textContent=data.state;document.getElementById('model').textContent=data.model?'Connected • '+data.model:(data.availability?'Setup required':'Connected');availability.textContent=data.availability||'';availability.style.display=data.availability?'block':'none';history.innerHTML=data.html||'<section class="empty"><strong>Ask Chappie about this project</strong><div class="suggestions"><button class="suggestion" type="button">Understand architecture</button><button class="suggestion" type="button">Explain selected code</button><button class="suggestion" type="button">Ask about project decisions</button></div></section>';document.getElementById('preview').textContent=data.preview;send.disabled=Boolean(data.availability);send.textContent=data.availability?'Project Chat Unavailable':'Send';});
history.addEventListener('click',event=>{const target=event.target;if(target.classList&&target.classList.contains('suggestion')){question.value=target.textContent;question.focus();}});
</script></body></html>`;
}
