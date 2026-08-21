import * as vscode from "vscode";
import { ProjectQAResponseV1 } from "../api/contracts";
import { workbenchHtml } from "./workbench_html";
import { ContextOptionsV1, HostMessageV1, parseWebviewMessage, RuntimeTelemetryV1, WorkbenchActionV1, WorkbenchConnectionSnapshotV1, WorkbenchContextAttachmentV1, WorkbenchContextKindV1 } from "./workbench_protocol";
import { WorkbenchSessionV1 } from "./workbench_state";

export interface WorkbenchDependenciesV1 {
  askProject(question: string, context: ContextOptionsV1): Promise<ProjectQAResponseV1>;
  attachContext(kind: WorkbenchContextKindV1): Promise<WorkbenchContextAttachmentV1>;
  removeContext(kind: WorkbenchContextKindV1): void;
  previewContext(kind: WorkbenchContextKindV1): string;
  clearContexts(): void;
  runtimeTelemetry(): Promise<RuntimeTelemetryV1>;
  runAction(action: WorkbenchActionV1): Promise<void>;
  workspaceName(): string | undefined;
  errorMessage(error: unknown): string;
}

export class ChappieWorkbenchV1 implements vscode.Disposable {
  private panel: vscode.WebviewPanel | undefined;
  private readonly session = new WorkbenchSessionV1();
  private readonly attachments = new Map<WorkbenchContextKindV1, WorkbenchContextAttachmentV1>();
  private connection: WorkbenchConnectionSnapshotV1 = { state: "DISCONNECTED", system: "UNKNOWN", model: null, knowledge: "UNKNOWN", availability: "Chappie setup is required before the local service can connect.", setupAction: "Set up" };
  private sequence = 0;

  public constructor(private readonly dependencies: WorkbenchDependenciesV1) {}

  public open(): void {
    if (this.panel) { this.panel.reveal(vscode.ViewColumn.Active, true); return; }
    const workspace = this.dependencies.workspaceName();
    this.panel = vscode.window.createWebviewPanel("chappie.workbench", workspace ? `Chappie Workbench - ${workspace}` : "Chappie Workbench", vscode.ViewColumn.Active, { enableScripts: true, retainContextWhenHidden: true, localResourceRoots: [] });
    this.panel.webview.html = workbenchHtml(this.panel.webview);
    this.panel.webview.onDidReceiveMessage((value: unknown) => { void this.receive(value); });
    this.panel.onDidDispose(() => { this.panel = undefined; });
    this.initialize();
  }

  public updateConnection(connection: WorkbenchConnectionSnapshotV1): void { this.connection = connection; this.post({ type: "connectionChanged", connection }); }
  public newChat(): void { if (this.session.active) return; this.session.clear(); this.clearAttachments(); this.open(); this.post({ type: "replaceSession", session: this.session.snapshot(), sessionRevision: this.session.currentRevision }); }
  public focusComposer(): void { this.open(); this.post({ type: "notice", message: "Focus the composer to ask Chappie about this project." }); }
  public dispose(): void { this.panel?.dispose(); this.panel = undefined; }

  private initialize(): void { void this.post({ type: "initialize", protocol: "chappie_workbench/1.3.0", connection: this.connection, session: this.session.snapshot(), sessionRevision: this.session.currentRevision, attachments: this.attachmentSnapshot() }); }
  private attachmentSnapshot(): readonly WorkbenchContextAttachmentV1[] { return [...this.attachments.values()].map((value) => ({ ...value })); }
  private clearAttachments(): void { this.attachments.clear(); this.dependencies.clearContexts(); this.post({ type: "attachmentsChanged", attachments: [] }); }

  private async receive(value: unknown): Promise<void> {
    const message = parseWebviewMessage(value);
    if (!message) return;
    if (message.type === "ready") { this.initialize(); return; }
    if (message.type === "attachContext") {
      try { this.attachments.set(message.kind, await this.dependencies.attachContext(message.kind)); this.post({ type: "attachmentsChanged", attachments: this.attachmentSnapshot() }); }
      catch (error) { this.post({ type: "notice", message: this.dependencies.errorMessage(error) }); }
      return;
    }
    if (message.type === "previewContext") { this.post({ type: "contextPreview", preview: this.safePreview(message.kind) }); return; }
    if (message.type === "removeContext") { this.attachments.delete(message.kind); this.dependencies.removeContext(message.kind); this.post({ type: "contextPreview", preview: "" }); this.post({ type: "attachmentsChanged", attachments: this.attachmentSnapshot() }); return; }
    if (message.type === "clearConversation") { this.session.clear(); this.clearAttachments(); await this.postSession(); return; }
    if (message.type === "feedback") { const updated = this.session.feedback(message.turnId, message.value); if (updated) await this.post({ type: "turnFeedback", turn: updated }); return; }
    if (message.type === "runAction") { await this.dependencies.runAction(message.action); return; }
    const original = message.type === "retry" ? this.session.find(message.turnId) : undefined;
    if (message.type === "retry" && (!original || original.status === "pending")) return;
    const question = message.type === "retry" ? original!.question : message.question.trim();
    const context = message.type === "retry" ? original!.context : message.context;
    const requestId = message.type === "retry" ? `retry_${Date.now().toString(36)}` : message.requestId;
    if (this.connection.availability) {
      await this.post({ type: "notice", message: this.connection.availability });
      return;
    }
    const turn = this.session.begin(`turn_${++this.sequence}`, requestId, question, context);
    if (!turn) return;
    await this.postSession();
    const telemetry = setInterval(() => { void this.publishTelemetry(); }, 1_500); void this.publishTelemetry();
    try {
      const response = await this.dependencies.askProject(turn.question, turn.context);
      if (response.state !== "OK" || !(response.answer ?? "").trim()) throw new Error(response.error?.message ?? "Project QA did not return a final answer.");
      if (!this.session.complete(turn.turnId, response)) throw new Error("Project QA completed after its Workbench turn was no longer pending.");
    } catch (error) { this.session.fail(turn.turnId, this.dependencies.errorMessage(error)); }
    finally { clearInterval(telemetry); await this.postSession(); }
  }

  private safePreview(kind: WorkbenchContextKindV1): string { try { return this.dependencies.previewContext(kind); } catch { return "Workspace context is unavailable."; } }
  private async publishTelemetry(): Promise<void> { try { this.post({ type: "runtimeTelemetry", telemetry: await this.dependencies.runtimeTelemetry() }); } catch { /* optional diagnostics never affect Project QA */ } }
  private postSession(): Promise<void> { return this.post({ type: "replaceSession", session: this.session.snapshot(), sessionRevision: this.session.currentRevision }); }
  private async post(message: HostMessageV1): Promise<void> { await this.panel?.webview.postMessage(message); }
}
