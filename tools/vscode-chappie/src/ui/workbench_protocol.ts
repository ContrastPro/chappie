import { CodexTaskV1, ControlPlaneModelV1, DevelopmentAnalysisV1, DevelopmentFailureDiagnosticsV1, DevelopmentModelRoleV1, ProjectQAResponseV1 } from "../api/contracts";

export const WORKBENCH_PROTOCOL_VERSION = "chappie_workbench/1.4.0";
/** Local editing capacity, intentionally independent of the request capacity. */
export const MAX_WORKBENCH_DRAFT_CHARACTERS = 50_000;
/** Conservative cap for a request to the supported 16K-context local models. */
export const MAX_WORKBENCH_REQUEST_CHARACTERS = 12_000;

export interface ContextOptionsV1 { includeFile: boolean; includeSelection: boolean; includeGit: boolean; }
export type WorkbenchContextKindV1 = keyof ContextOptionsV1;
/** A bounded, host-captured context snapshot. `preview` is exactly the safe text sent for this attachment. */
export interface WorkbenchContextAttachmentV1 {
  id: string;
  kind: WorkbenchContextKindV1;
  label: string;
  detail: string;
  preview: string;
  characterCount: number;
  truncated: boolean;
}
export interface RuntimeTelemetryV1 { available: boolean; provider: string | null; gpuName: string | null; utilizationPercent: number | null; memoryUsedMb: number | null; memoryTotalMb: number | null; temperatureC: number | null; sampledAtMs: number; }
export interface WorkbenchConnectionSnapshotV1 {
  state: "DISCONNECTED" | "CONNECTING" | "CONNECTED" | "INCOMPATIBLE" | "ERROR";
  /** Overall local Chappie system state, distinct from transport connection. */
  system: string;
  model: string | null;
  modelRole: DevelopmentModelRoleV1;
  mode: WorkbenchModeV1;
  models: readonly ControlPlaneModelV1[];
  knowledge: string;
  availability: string | null;
  setupAction: "Set up" | "Repair" | "Reconnect";
}
export type WorkbenchActionV1 = "setup" | "repair" | "reconnect" | "diagnostics";
export type WorkbenchModeV1 = "CHAT" | "DEVELOPMENT";
export type WorkbenchTurnStatusV1 = "pending" | "complete" | "failed";
export type WorkbenchFeedbackV1 = "good" | "bad" | null;
export type DevelopmentFailureCategoryV1 = "VALIDATION" | "GROUNDING" | "PROPOSAL" | "REVIEW" | "PROVIDER" | "DEPENDENCY" | "INTERNAL" | "TRANSPORT" | "UNKNOWN";
export interface WorkbenchDevelopmentFailureV1 {
  requestId: string | null;
  reasonCode: string | null;
  failedStage: string | null;
  category: DevelopmentFailureCategoryV1;
  completedStages: readonly string[];
  lifecycle: DevelopmentFailureDiagnosticsV1["lifecycle"];
  requestedRole: string | null;
  effectiveModel: string | null;
  readiness: string | null;
  handoffStatus: "NOT_REACHED";
}
export interface WorkbenchTurnV1 {
  turnId: string; requestId: string; question: string; context: ContextOptionsV1;
  attachments: readonly WorkbenchContextAttachmentV1[];
  status: WorkbenchTurnStatusV1; response?: ProjectQAResponseV1; error?: string;
  mode: WorkbenchModeV1; development?: DevelopmentAnalysisV1; developmentFailure?: WorkbenchDevelopmentFailureV1; codexTask?: CodexTaskV1;
  startedAtMs: number; completedAtMs?: number; feedback?: WorkbenchFeedbackV1;
}

export type WebviewMessageV1 =
  | { type: "ready"; protocol: typeof WORKBENCH_PROTOCOL_VERSION }
  | { type: "ask"; requestId: string; question: string; context: ContextOptionsV1 }
  | { type: "attachContext"; kind: WorkbenchContextKindV1 }
  | { type: "previewContext"; kind: WorkbenchContextKindV1 }
  | { type: "removeContext"; kind: WorkbenchContextKindV1 }
  | { type: "clearConversation" }
  | { type: "retry"; turnId: string }
  | { type: "feedback"; turnId: string; value: Exclude<WorkbenchFeedbackV1, null> }
  | { type: "runAction"; action: WorkbenchActionV1 }
  | { type: "selectModelRole" }
  | { type: "selectWorkbenchMode" }
  | { type: "prepareCodexTask"; turnId: string };

export type HostMessageV1 =
  | { type: "initialize"; protocol: typeof WORKBENCH_PROTOCOL_VERSION; connection: WorkbenchConnectionSnapshotV1; session: readonly WorkbenchTurnV1[]; sessionRevision: number; attachments: readonly WorkbenchContextAttachmentV1[] }
  | { type: "connectionChanged"; connection: WorkbenchConnectionSnapshotV1 }
  | { type: "contextPreview"; preview: string }
  | { type: "attachmentsChanged"; attachments: readonly WorkbenchContextAttachmentV1[] }
  | { type: "notice"; message: string }
  | { type: "runtimeTelemetry"; telemetry: RuntimeTelemetryV1 }
  | { type: "turnFeedback"; turn: WorkbenchTurnV1 }
  /** The sole host-owned lifecycle projection for conversation, Inspector and Activity. */
  | { type: "replaceSession"; session: readonly WorkbenchTurnV1[]; sessionRevision: number };

const contextKeys = new Set<WorkbenchContextKindV1>(["includeFile", "includeSelection", "includeGit"]);
const actions = new Set<WorkbenchActionV1>(["setup", "repair", "reconnect", "diagnostics"]);
const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null;

export function parseWebviewMessage(value: unknown): WebviewMessageV1 | undefined {
  if (!isRecord(value) || typeof value.type !== "string") return undefined;
  if (value.type === "ready") return value.protocol === WORKBENCH_PROTOCOL_VERSION ? { type: "ready", protocol: WORKBENCH_PROTOCOL_VERSION } : undefined;
  if (value.type === "clearConversation") return Object.keys(value).length === 1 ? { type: "clearConversation" } : undefined;
  if (value.type === "selectModelRole") return Object.keys(value).length === 1 ? { type: "selectModelRole" } : undefined;
  if (value.type === "selectWorkbenchMode") return Object.keys(value).length === 1 ? { type: "selectWorkbenchMode" } : undefined;
  if (value.type === "prepareCodexTask" && Object.keys(value).length === 2 && typeof value.turnId === "string" && /^[a-zA-Z0-9_-]{1,64}$/.test(value.turnId)) return { type: "prepareCodexTask", turnId: value.turnId };
  if (value.type === "retry" && Object.keys(value).length === 2 && typeof value.turnId === "string" && /^[a-zA-Z0-9_-]{1,64}$/.test(value.turnId)) return { type: "retry", turnId: value.turnId };
  if (value.type === "feedback" && Object.keys(value).length === 3 && typeof value.turnId === "string" && /^[a-zA-Z0-9_-]{1,64}$/.test(value.turnId) && (value.value === "good" || value.value === "bad")) return { type: "feedback", turnId: value.turnId, value: value.value };
  if ((value.type === "attachContext" || value.type === "previewContext" || value.type === "removeContext") && Object.keys(value).length === 2 && typeof value.kind === "string" && contextKeys.has(value.kind as WorkbenchContextKindV1)) return { type: value.type, kind: value.kind as WorkbenchContextKindV1 };
  if (value.type === "runAction") return Object.keys(value).length === 2 && typeof value.action === "string" && actions.has(value.action as WorkbenchActionV1) ? { type: "runAction", action: value.action as WorkbenchActionV1 } : undefined;
  if (value.type === "ask" && Object.keys(value).length === 4 && typeof value.requestId === "string" && /^[a-zA-Z0-9_-]{1,64}$/.test(value.requestId) && typeof value.question === "string" && value.question.trim().length > 0 && value.question.length <= MAX_WORKBENCH_REQUEST_CHARACTERS && contextOptions(value.context)) return { type: "ask", requestId: value.requestId, question: value.question, context: contextOptions(value.context)! };
  return undefined;
}

function contextOptions(value: unknown): ContextOptionsV1 | undefined {
  if (!isRecord(value) || Object.keys(value).length !== 3 || !["includeFile", "includeSelection", "includeGit"].every((key) => typeof value[key] === "boolean")) return undefined;
  return { includeFile: value.includeFile as boolean, includeSelection: value.includeSelection as boolean, includeGit: value.includeGit as boolean };
}
