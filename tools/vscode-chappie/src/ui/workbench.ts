import * as vscode from "vscode";
import { CodexTaskV1, DevelopmentAnalysisV1, DevelopmentModelRoleV1, ProjectQAResponseV1 } from "../api/contracts";
import { ControlPlaneDependencyError, ControlPlaneRequestError } from "../api/client";
import { workbenchHtml } from "./workbench_html";
import { ContextOptionsV1, DevelopmentFailureCategoryV1, HostMessageV1, parseWebviewMessage, RuntimeTelemetryV1, WorkbenchActionV1, WorkbenchConnectionSnapshotV1, WorkbenchContextAttachmentV1, WorkbenchContextKindV1, WorkbenchDevelopmentFailureV1, WorkbenchModeV1 } from "./workbench_protocol";
import { WorkbenchSessionV1 } from "./workbench_state";

export interface WorkbenchDependenciesV1 {
  askProject(question: string, context: ContextOptionsV1, attachments: readonly WorkbenchContextAttachmentV1[], role: DevelopmentModelRoleV1): Promise<ProjectQAResponseV1>;
  analyzeDevelopment(request: string, context: ContextOptionsV1, attachments: readonly WorkbenchContextAttachmentV1[], role: DevelopmentModelRoleV1): Promise<DevelopmentAnalysisV1>;
  prepareCodexTask(analysis: DevelopmentAnalysisV1): Promise<CodexTaskV1>;
  selectModelRole(): Promise<void>;
  selectWorkbenchMode(): Promise<WorkbenchModeV1>;
  workbenchMode(): WorkbenchModeV1;
  attachContext(kind: WorkbenchContextKindV1): Promise<WorkbenchContextAttachmentV1>;
  removeContext(kind: WorkbenchContextKindV1): void;
  clearContexts(): void;
  runtimeTelemetry(): Promise<RuntimeTelemetryV1>;
  runAction(action: WorkbenchActionV1): Promise<void>;
  workspaceName(): string | undefined;
  errorMessage(error: unknown, domain?: WorkbenchModeV1): string;
}

export class ChappieWorkbenchV1 implements vscode.Disposable {
  private panel: vscode.WebviewPanel | undefined;
  private contextPreviewPanel: vscode.WebviewPanel | undefined;
  private readonly session = new WorkbenchSessionV1();
  private readonly attachments = new Map<WorkbenchContextKindV1, WorkbenchContextAttachmentV1>();
  private connection: WorkbenchConnectionSnapshotV1 = { state: "DISCONNECTED", system: "UNKNOWN", model: null, modelRole: "FAST", mode: "CHAT", models: [], knowledge: "UNKNOWN", availability: "Chappie setup is required before the local service can connect.", setupAction: "Set up" };
  private mode: WorkbenchModeV1;
  private sequence = 0;

  public constructor(private readonly dependencies: WorkbenchDependenciesV1) { this.mode = dependencies.workbenchMode(); this.connection = { ...this.connection, mode: this.mode }; }

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
  public dispose(): void { this.panel?.dispose(); this.contextPreviewPanel?.dispose(); this.panel = undefined; this.contextPreviewPanel = undefined; }

  private initialize(): void { void this.post({ type: "initialize", protocol: "chappie_workbench/1.4.0", connection: this.connection, session: this.session.snapshot(), sessionRevision: this.session.currentRevision, attachments: this.attachmentSnapshot() }); }
  private attachmentSnapshot(): readonly WorkbenchContextAttachmentV1[] { return [...this.attachments.values()].map((value) => ({ ...value })); }
  private clearAttachments(): void { this.attachments.clear(); this.dependencies.clearContexts(); this.post({ type: "notice", message: "" }); this.post({ type: "attachmentsChanged", attachments: [] }); }

  private async receive(value: unknown): Promise<void> {
    const message = parseWebviewMessage(value);
    if (!message) return;
    if (message.type === "ready") { this.initialize(); return; }
    if (message.type === "attachContext") {
      try {
        const attachment = await this.dependencies.attachContext(message.kind);
        if (message.kind === "includeFile") this.attachments.delete("includeSelection");
        if (message.kind === "includeSelection") this.attachments.delete("includeFile");
        this.attachments.set(message.kind, attachment);
        this.post({ type: "notice", message: "" });
        this.post({ type: "attachmentsChanged", attachments: this.attachmentSnapshot() });
      }
      catch (error) {
        if (error instanceof Error && error.message === "CONTEXT_PICKER_CANCELLED") return;
        this.post({ type: "notice", message: this.dependencies.errorMessage(error) });
      }
      return;
    }
    if (message.type === "previewContext") { const attachment = this.attachments.get(message.kind); if (attachment) this.openContextPreview(attachment); return; }
    if (message.type === "removeContext") { this.attachments.delete(message.kind); this.dependencies.removeContext(message.kind); this.post({ type: "notice", message: "" }); this.post({ type: "attachmentsChanged", attachments: this.attachmentSnapshot() }); return; }
    if (message.type === "clearConversation") { this.session.clear(); this.clearAttachments(); await this.postSession(); return; }
    if (message.type === "selectModelRole") { await this.dependencies.selectModelRole(); return; }
    if (message.type === "selectWorkbenchMode") {
      this.mode = await this.dependencies.selectWorkbenchMode();
      this.connection = { ...this.connection, mode: this.mode };
      await this.post({ type: "connectionChanged", connection: this.connection });
      return;
    }
    if (message.type === "prepareCodexTask") {
      const turn = this.session.find(message.turnId);
      if (!turn?.development || turn.development.state !== "OK" || turn.development.validation_status !== "VALID" || turn.development.readiness !== "READY_FOR_TASK" || (turn.development.outcome !== undefined && turn.development.outcome.status !== "NEEDS_HUMAN_REVIEW")) return;
      try {
        const task = await this.dependencies.prepareCodexTask(turn.development);
        if (task.state !== "ADVISORY") throw new Error(task.reason_code ?? "Codex task preparation was rejected.");
        if (!this.session.setCodexTask(turn.turnId, task)) throw new Error("The development analysis is no longer available.");
      } catch (error) { await this.post({ type: "notice", message: this.dependencies.errorMessage(error) }); }
      await this.postSession();
      return;
    }
    if (message.type === "feedback") { const updated = this.session.feedback(message.turnId, message.value); if (updated) await this.post({ type: "turnFeedback", turn: updated }); return; }
    if (message.type === "runAction") { await this.dependencies.runAction(message.action); return; }
    const original = message.type === "retry" ? this.session.find(message.turnId) : undefined;
    if (message.type === "retry" && (!original || original.status === "pending")) return;
    const question = message.type === "retry" ? original!.question : message.question.trim();
    const attachments = message.type === "retry" ? original!.attachments : this.attachmentSnapshot();
    const context = contextFor(attachments);
    const requestId = message.type === "retry" ? `retry_${Date.now().toString(36)}` : message.requestId;
    if (this.connection.availability) {
      await this.post({ type: "notice", message: this.connection.availability });
      return;
    }
    const turn = this.session.begin(`turn_${++this.sequence}`, requestId, question, context, Date.now(), attachments, this.mode);
    if (!turn) return;
    if (message.type === "ask") this.clearAttachments();
    await this.postSession();
    const telemetry = setInterval(() => { void this.publishTelemetry(); }, 1_500); void this.publishTelemetry();
    try {
      if (turn.mode === "DEVELOPMENT") {
        const analysis = await this.dependencies.analyzeDevelopment(turn.question, turn.context, turn.attachments, this.connection.modelRole);
        if (analysis.state !== "OK") throw new Error(analysis.reason_code ?? "Development analysis did not return a valid result.");
        if (!this.session.completeDevelopment(turn.turnId, analysis)) throw new Error("Development analysis completed after its Workbench turn was no longer pending.");
      } else {
        const response = await this.dependencies.askProject(turn.question, turn.context, turn.attachments, this.connection.modelRole);
        if (response.state !== "OK" || !(response.answer ?? "").trim()) throw new Error(response.error?.message ?? "Project QA did not return a final answer.");
        if (!this.session.complete(turn.turnId, response)) throw new Error("Project QA completed after its Workbench turn was no longer pending.");
      }
    } catch (error) {
      this.session.fail(
        turn.turnId,
        this.dependencies.errorMessage(error, turn.mode),
        Date.now(),
        turn.mode === "DEVELOPMENT" ? developmentFailureFor(error) : undefined,
      );
    }
    finally { clearInterval(telemetry); await this.postSession(); }
  }

  private async publishTelemetry(): Promise<void> { try { this.post({ type: "runtimeTelemetry", telemetry: await this.dependencies.runtimeTelemetry() }); } catch { /* optional diagnostics never affect Project QA */ } }
  private postSession(): Promise<void> { return this.post({ type: "replaceSession", session: this.session.snapshot(), sessionRevision: this.session.currentRevision }); }
  private async post(message: HostMessageV1): Promise<void> { await this.panel?.webview.postMessage(message); }

  private openContextPreview(attachment: WorkbenchContextAttachmentV1): void {
    if (!this.contextPreviewPanel) {
      this.contextPreviewPanel = vscode.window.createWebviewPanel("chappie.contextPreview", "Chappie Context Preview", vscode.ViewColumn.Beside, { enableScripts: false, localResourceRoots: [] });
      this.contextPreviewPanel.onDidDispose(() => { this.contextPreviewPanel = undefined; });
    }
    this.contextPreviewPanel.title = `Context · ${attachment.label}`;
    this.contextPreviewPanel.webview.html = contextPreviewHtml(attachment);
    this.contextPreviewPanel.reveal(vscode.ViewColumn.Beside, true);
  }
}

function developmentFailureFor(error: unknown): WorkbenchDevelopmentFailureV1 | undefined {
  const typed = error instanceof ControlPlaneRequestError || error instanceof ControlPlaneDependencyError ? error : undefined;
  if (!typed) return undefined;
  const diagnostics = typed.diagnostics;
  const reasonCode = typed.reasonCode;
  const failedStage = diagnostics?.failed_stage ?? (reasonCode === "INVALID_REQUEST" ? "TRANSPORT" : null);
  return {
    requestId: typed.requestId ?? diagnostics?.request_id ?? null,
    reasonCode,
    failedStage,
    category: developmentFailureCategory(reasonCode, failedStage),
    completedStages: diagnostics?.completed_stages ?? [],
    lifecycle: diagnostics?.lifecycle ?? [],
    requestedRole: diagnostics?.evidence?.requested_role ?? null,
    effectiveModel: diagnostics?.evidence?.effective_model ?? null,
    readiness: diagnostics?.readiness ?? null,
    handoffStatus: diagnostics?.handoff_status ?? "NOT_REACHED",
  };
}

function developmentFailureCategory(reasonCode: string, failedStage: string | null): DevelopmentFailureCategoryV1 {
  if (reasonCode === "AI_PROVIDER_TIMEOUT") return "PROVIDER";
  if (reasonCode === "AI_UNAVAILABLE" || reasonCode === "MODEL_ROLE_UNAVAILABLE") return "DEPENDENCY";
  if (reasonCode === "INVALID_REQUEST" || failedStage === "TRANSPORT") return "TRANSPORT";
  if (failedStage?.startsWith("GROUNDING")) return "GROUNDING";
  if (failedStage?.startsWith("PROPOSAL")) return "PROPOSAL";
  if (failedStage?.startsWith("REVIEW")) return "REVIEW";
  if (reasonCode.includes("SCHEMA") || reasonCode.includes("VALIDATION")) return "VALIDATION";
  if (reasonCode === "INTERNAL_ERROR") return "INTERNAL";
  return "UNKNOWN";
}

function contextPreviewHtml(attachment: WorkbenchContextAttachmentV1): string {
  const kind = attachment.kind === "includeFile" ? "FILE" : attachment.kind === "includeSelection" ? "SELECTION" : "GIT";
  const state = attachment.truncated ? "Bounded snapshot" : "Captured snapshot";
  const escape = (value: string): string => value.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#39;");
  return `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'"><style>body{margin:0;padding:24px;color:var(--vscode-foreground);background:var(--vscode-editor-background);font:13px var(--vscode-font-family)}header{border-bottom:1px solid var(--vscode-panel-border);padding-bottom:14px}h1{font-size:16px;margin:0 0 12px}.kind{font-size:11px;font-weight:700;color:var(--vscode-descriptionForeground);letter-spacing:.06em}.meta{color:var(--vscode-descriptionForeground);line-height:1.5;margin-top:6px}.note{margin-top:14px;padding:8px 10px;border-left:2px solid var(--vscode-textLink-foreground);color:var(--vscode-descriptionForeground)}pre{white-space:pre-wrap;overflow-wrap:anywhere;margin:18px 0 0;padding:14px;border:1px solid var(--vscode-panel-border);border-radius:6px;background:var(--vscode-textCodeBlock-background);font:12px var(--vscode-editor-font-family)}</style></head><body><header><h1>Chappie Context Preview</h1><div class="kind">${escape(kind)}</div><div>${escape(attachment.label)}</div><div class="meta">${escape(attachment.detail)} · ${attachment.characterCount} chars · ${escape(state)}</div></header><p class="note">Captured snapshot — this is the context captured for the current Chappie request.</p><pre>${escape(attachment.preview)}</pre></body></html>`;
}

function contextFor(attachments: readonly WorkbenchContextAttachmentV1[]): ContextOptionsV1 {
  return {
    includeFile: attachments.some((attachment) => attachment.kind === "includeFile"),
    includeSelection: attachments.some((attachment) => attachment.kind === "includeSelection"),
    includeGit: attachments.some((attachment) => attachment.kind === "includeGit"),
  };
}
