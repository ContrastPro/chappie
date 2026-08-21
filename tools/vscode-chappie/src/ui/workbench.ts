import * as vscode from "vscode";
import { ProjectQAResponseV1 } from "../api/contracts";
import { workbenchHtml } from "./workbench_html";
import {
  ContextOptionsV1,
  HostMessageV1,
  parseWebviewMessage,
  WorkbenchActionV1,
  WorkbenchConnectionSnapshotV1,
} from "./workbench_protocol";
import { WorkbenchSessionV1 } from "./workbench_state";

export interface WorkbenchDependenciesV1 {
  askProject(question: string, context: ContextOptionsV1): Promise<ProjectQAResponseV1>;
  previewContext(context: ContextOptionsV1): string;
  runAction(action: WorkbenchActionV1): Promise<void>;
  workspaceName(): string | undefined;
  errorMessage(error: unknown): string;
}

export class ChappieWorkbenchV1 implements vscode.Disposable {
  private panel: vscode.WebviewPanel | undefined;
  private readonly session = new WorkbenchSessionV1();
  private connection: WorkbenchConnectionSnapshotV1 = {
    state: "DISCONNECTED", model: null, knowledge: "UNKNOWN",
    availability: "Chappie setup is required before the local service can connect.", setupAction: "Set up",
  };
  private sequence = 0;

  public constructor(private readonly dependencies: WorkbenchDependenciesV1) {}

  public open(): void {
    if (this.panel) { this.panel.reveal(vscode.ViewColumn.Active, true); return; }
    const workspace = this.dependencies.workspaceName();
    const title = workspace ? `Chappie Workbench - ${workspace}` : "Chappie Workbench";
    this.panel = vscode.window.createWebviewPanel(
      "chappie.workbench", title, vscode.ViewColumn.Active,
      { enableScripts: true, retainContextWhenHidden: true, localResourceRoots: [] },
    );
    this.panel.webview.html = workbenchHtml(this.panel.webview);
    this.panel.webview.onDidReceiveMessage((value: unknown) => { void this.receive(value); });
    this.panel.onDidDispose(() => { this.panel = undefined; });
    this.post({ type: "initialize", protocol: "chappie_workbench/1.1.0", connection: this.connection, session: this.session.snapshot() });
  }

  public updateConnection(connection: WorkbenchConnectionSnapshotV1): void {
    this.connection = connection;
    this.post({ type: "connectionChanged", connection });
  }

  public newChat(): void {
    if (this.session.active) return;
    this.session.clear();
    this.open();
    this.post({ type: "replaceSession", session: this.session.snapshot() });
  }

  public dispose(): void { this.panel?.dispose(); this.panel = undefined; }

  private async receive(value: unknown): Promise<void> {
    const message = parseWebviewMessage(value);
    if (!message) return;
    if (message.type === "ready") {
      this.post({ type: "initialize", protocol: "chappie_workbench/1.1.0", connection: this.connection, session: this.session.snapshot() });
      return;
    }
    if (message.type === "previewContext") {
      this.post({ type: "contextPreview", preview: this.safePreview(message.context) });
      return;
    }
    if (message.type === "removeContext") {
      this.post({ type: "contextPreview", preview: "" });
      return;
    }
    if (message.type === "clearConversation") {
      this.session.clear();
      this.post({ type: "replaceSession", session: this.session.snapshot() });
      return;
    }
    if (message.type === "feedback") {
      this.session.feedback(message.turnId, message.value);
      this.post({ type: "replaceSession", session: this.session.snapshot() });
      return;
    }
    if (message.type === "runAction") {
      await this.dependencies.runAction(message.action);
      return;
    }
    const original = message.type === "retry" ? this.session.find(message.turnId) : undefined;
    if (message.type === "retry" && (!original || original.status === "pending")) return;
    const question = message.type === "retry" ? original!.question : message.question.trim();
    const context = message.type === "retry" ? original!.context : message.context;
    const requestId = message.type === "retry" ? `retry_${Date.now().toString(36)}` : message.requestId;
    const turn = this.session.begin(`turn_${++this.sequence}`, requestId, question, context);
    if (!turn) return;
    this.post({ type: "turnStarted", turn });
    this.post({ type: "operationChanged", active: true });
    try {
      const completed = this.session.complete(turn.turnId, await this.dependencies.askProject(turn.question, turn.context));
      if (completed) this.post({ type: "turnCompleted", turn: completed });
    } catch (error) {
      const failed = this.session.fail(turn.turnId, this.dependencies.errorMessage(error));
      if (failed) this.post({ type: "turnFailed", turn: failed });
    } finally {
      this.post({ type: "operationChanged", active: false });
    }
  }

  private safePreview(context: ContextOptionsV1): string {
    try { return this.dependencies.previewContext(context); } catch { return "Workspace context is unavailable for the active editor."; }
  }

  private post(message: HostMessageV1): void { void this.panel?.webview.postMessage(message); }
}
