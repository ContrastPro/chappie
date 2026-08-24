import {
  CONTROL_PLANE_DIGEST,
  CONTROL_PLANE_IDENTITY,
  ControlPlaneAIStatusV1,
  ControlPlaneCapabilitiesV1,
  ControlPlaneDiagnosticsV1,
  ControlPlaneModelsV1,
  ControlPlaneStatusV1,
  CodexTaskPreparationRequestV1,
  CodexTaskV1,
  DevelopmentAnalysisRequestV1,
  DevelopmentAnalysisV1,
  DevelopmentFailureDiagnosticsV1,
  HealthV1,
  LOOPBACK_HOST,
  TRANSPORT_DIGEST,
  TRANSPORT_IDENTITY,
  TransportEnvelopeV1,
  ProjectQAResponseV1,
} from "./contracts";
import { WorkspaceContextV1 } from "../workspace/context";

export class ControlPlaneClientError extends Error {}
export class ControlPlaneIncompatibleError extends ControlPlaneClientError {}
export class ControlPlaneDependencyError extends ControlPlaneClientError {
  public constructor(public readonly reasonCode: string, public readonly requestId?: string, public readonly diagnostics?: DevelopmentFailureDiagnosticsV1) { super(reasonCode); }
}
export class ControlPlaneRequestError extends ControlPlaneClientError {
  public constructor(public readonly reasonCode: string, message: string, public readonly requestId?: string, public readonly diagnostics?: DevelopmentFailureDiagnosticsV1) { super(message); }
}
export class ControlPlaneTransportTimeoutError extends ControlPlaneClientError {
  public constructor(public readonly reasonCode = "CLIENT_TRANSPORT_TIMEOUT") { super("Control Plane request timed out"); }
}

export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

/** Health stays fast; the first local status refresh may load a ready runtime snapshot. */
export const CONTROL_PLANE_HEALTH_TIMEOUT_MS = 1_500;
export const CONTROL_PLANE_REFRESH_TIMEOUT_MS = 5_000;
export const PROJECT_QA_HTTP_TIMEOUT_MS = 60_000;
/** Development has the same bounded backend model call plus evidence and validation overhead. */
// A CODE analysis consists of three sequential, independently bounded 90-second calls.
// Keep a small transport margin so a valid staged operation is not aborted mid-review.
export const DEVELOPMENT_ANALYSIS_HTTP_TIMEOUT_MS = 285_000;
export const PROJECT_QA_MAX_REQUEST_CHARACTERS = 12_000;

export class ChappieControlPlaneClient {
  public readonly endpoint: string;

  public constructor(
    port: number,
    private readonly fetcher: FetchLike = fetch,
    private readonly healthTimeoutMs = CONTROL_PLANE_HEALTH_TIMEOUT_MS,
    private readonly projectQaTimeoutMs = PROJECT_QA_HTTP_TIMEOUT_MS,
    private readonly developmentAnalysisTimeoutMs = DEVELOPMENT_ANALYSIS_HTTP_TIMEOUT_MS,
  ) {
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      throw new ControlPlaneClientError("Control Plane port must be between 1 and 65535");
    }
    this.endpoint = `http://${LOOPBACK_HOST}:${port}`;
  }

  public async health(): Promise<HealthV1> {
    const response = await this.get<HealthV1>("/v1/health");
    if (response.status !== "OK" || response.data?.status !== "OK") {
      throw new ControlPlaneClientError("Control Plane health response is invalid");
    }
    if (
      response.transport_contract !== TRANSPORT_IDENTITY ||
      response.transport_contract_digest !== TRANSPORT_DIGEST ||
      response.control_plane_contract !== CONTROL_PLANE_IDENTITY ||
      response.control_plane_contract_digest !== CONTROL_PLANE_DIGEST
    ) {
      throw new ControlPlaneIncompatibleError("Chappie Control Plane version is incompatible with this extension");
    }
    return response.data;
  }

  public status(): Promise<ControlPlaneStatusV1> { return this.data("/v1/status", { method: "GET" }, CONTROL_PLANE_REFRESH_TIMEOUT_MS); }
  public diagnostics(): Promise<ControlPlaneDiagnosticsV1> { return this.data("/v1/diagnostics", { method: "GET" }, CONTROL_PLANE_REFRESH_TIMEOUT_MS); }
  public aiStatus(): Promise<ControlPlaneAIStatusV1> { return this.data("/v1/ai/status", { method: "GET" }, CONTROL_PLANE_REFRESH_TIMEOUT_MS); }
  public models(): Promise<ControlPlaneModelsV1> { return this.data("/v1/ai/models", { method: "GET" }, CONTROL_PLANE_REFRESH_TIMEOUT_MS); }
  public capabilities(): Promise<ControlPlaneCapabilitiesV1> { return this.data("/v1/capabilities", { method: "GET" }, CONTROL_PLANE_REFRESH_TIMEOUT_MS); }
  public async askProject(question: string, workspaceContext?: WorkspaceContextV1, requestedModelRole?: "FAST" | "CODE" | "DEEP"): Promise<ProjectQAResponseV1> {
    if (!question.trim() || question.length > PROJECT_QA_MAX_REQUEST_CHARACTERS) {
      throw new ControlPlaneClientError(`Project question must be between 1 and ${PROJECT_QA_MAX_REQUEST_CHARACTERS} characters`);
    }
    return parseProjectQA(await this.data(
      "/v1/project/qa",
      { method: "POST", body: JSON.stringify({ question, workspace_context: workspaceContext ?? null, ...(requestedModelRole ? { requested_model_role: requestedModelRole } : {}) }) },
      this.projectQaTimeoutMs,
    ));
  }
  public analyzeDevelopment(request: DevelopmentAnalysisRequestV1): Promise<DevelopmentAnalysisV1> {
    return this.data("/v1/development/analyze", { method: "POST", body: JSON.stringify(request) }, this.developmentAnalysisTimeoutMs).then(parseDevelopmentAnalysis);
  }
  public prepareCodexTask(request: CodexTaskPreparationRequestV1): Promise<CodexTaskV1> {
    return this.data("/v1/development/task", { method: "POST", body: JSON.stringify(request) }, this.projectQaTimeoutMs).then(parseCodexTask);
  }

  private async data<T>(path: string, init: RequestInit = { method: "GET" }, timeoutMs = this.healthTimeoutMs): Promise<T> {
    const response = await this.get<T>(path, init, timeoutMs);
    if (response.status !== "OK" || response.data === null) {
      throw new ControlPlaneClientError(response.error?.message ?? "Control Plane request failed");
    }
    return response.data;
  }

  private async get<T>(path: string, init: RequestInit = { method: "GET" }, timeoutMs = this.healthTimeoutMs): Promise<TransportEnvelopeV1<T>> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await this.fetcher(`${this.endpoint}${path}`, { ...init, signal: controller.signal, headers: { "content-type": "application/json" } });
      const value: unknown = await response.json();
      if (!response.ok) {
        const error = isEnvelope(value) ? value.error : null;
        const envelope = isRecord(value) ? value : {};
        const requestId = typeof envelope.request_id === "string" ? envelope.request_id : undefined;
        const diagnostics = parseDevelopmentDiagnostics(envelope.diagnostics);
        if (response.status === 503 && isRecord(error) && typeof error.code === "string") {
          throw new ControlPlaneDependencyError(error.code, requestId, diagnostics);
        }
        if (response.status === 400 && isRecord(error) && typeof error.code === "string" && typeof error.message === "string") {
          throw new ControlPlaneRequestError(error.code, projectQaRequestFailureMessage(error.code, error.message), requestId, diagnostics);
        }
        if (response.status === 500 && diagnostics && isRecord(error) && typeof error.code === "string") {
          throw new ControlPlaneRequestError(error.code, "Development Analysis request failed", requestId, diagnostics);
        }
        throw new ControlPlaneClientError(`Control Plane returned HTTP ${response.status}`);
      }
      if (!isEnvelope(value)) { throw new ControlPlaneClientError("Control Plane returned a malformed envelope"); }
      return value as unknown as TransportEnvelopeV1<T>;
    } catch (error) {
      if (error instanceof ControlPlaneClientError) { throw error; }
      if (error instanceof Error && error.name === "AbortError") { throw new ControlPlaneTransportTimeoutError(); }
      throw new ControlPlaneClientError("Control Plane is unavailable");
    } finally {
      clearTimeout(timer);
    }
  }
}

function projectQaRequestFailureMessage(code: string, fallback: string): string {
  if (code === "PROJECT_QA_REQUEST_TOO_LARGE") return `Question exceeds the ${PROJECT_QA_MAX_REQUEST_CHARACTERS}-character Project QA request limit.`;
  if (code === "REQUEST_BODY_TOO_LARGE") return "This request exceeds the Control Plane transport budget. Shorten the question or remove attached context.";
  if (code === "WORKSPACE_CONTEXT_INVALID") return "Attached workspace context is invalid or exceeds the canonical context limit. Capture it again before sending.";
  if (code === "INVALID_PROJECT_QA_QUESTION") return "Project QA requires a non-empty question.";
  if (code === "PROJECT_QA_NO_RELEVANT_KNOWLEDGE") return "Chappie could not find relevant canonical Knowledge for this question.";
  return fallback;
}

export function developmentAnalysisFailureMessage(code: string | undefined): string {
  if (code === "AI_PROVIDER_TIMEOUT") return "Chappie Development Analysis timed out during local model inference.";
  if (code === "INVALID_REQUEST") return "Chappie Development Analysis request is invalid. Review the request and try again.";
  if (code === "ANALYSIS_INVALID" || code === "DEVELOPMENT_ANALYSIS_INVALID" || code === "DEVELOPMENT_SCHEMA_INVALID") return "Chappie Development Analysis returned an invalid bounded result.";
  if (code === "GROUNDING_SCHEMA_INVALID" || code === "PROPOSAL_SCHEMA_INVALID" || code === "REVIEW_SCHEMA_INVALID") return "Chappie Development Analysis could not produce a valid bounded result.";
  if (code === "UNSUPPORTED_KNOWLEDGE_CITATION") return "Chappie Development Analysis cited material outside the supplied canonical Knowledge evidence.";
  if (code === "FORBIDDEN_AUTHORITY_TEXT") return "Chappie Development Analysis claimed authority outside its read-only advisory boundary.";
  if (code === "MODEL_ROLE_UNAVAILABLE") return "The selected Chappie development model role is unavailable; no fallback was used.";
  if (code === "KNOWLEDGE_STALE" || code === "KNOWLEDGE_UNAVAILABLE" || code === "NO_RELEVANT_KNOWLEDGE") return "Chappie Knowledge is not ready for Development Analysis.";
  if (code === "AI_UNAVAILABLE") return "Chappie AI model is currently unavailable for Development Analysis.";
  if (code === "INTERNAL_ERROR") return "Chappie Development Analysis is temporarily unavailable.";
  if (code) return `Chappie Development Analysis was rejected: ${code}.`;
  return "Chappie Development Analysis is temporarily unavailable.";
}

function parseDevelopmentDiagnostics(value: unknown): DevelopmentFailureDiagnosticsV1 | undefined {
  if (!isRecord(value) || value.operation !== "DEVELOPMENT_ANALYSIS" || value.status !== "FAILED" || !Array.isArray(value.lifecycle)) return undefined;
  const lifecycle = value.lifecycle.flatMap((item) => isRecord(item) && typeof item.stage === "string" && typeof item.status === "string" && typeof item.elapsed_ms === "number" && (item.reason_code === null || typeof item.reason_code === "string") ? [{ stage: item.stage, status: item.status, elapsed_ms: item.elapsed_ms, reason_code: item.reason_code }] : []);
  if (lifecycle.length !== value.lifecycle.length) return undefined;
  const evidenceValue = isRecord(value.evidence) ? value.evidence : undefined;
  const evidence = evidenceValue ? Object.fromEntries(["source_commit", "knowledge_identity", "knowledge_manifest_digest", "knowledge_context_digest", "evidence_digest", "requested_role", "effective_model"].flatMap((key) => typeof evidenceValue[key] === "string" ? [[key, evidenceValue[key]]] : [])) : null;
  return {
    operation: "DEVELOPMENT_ANALYSIS",
    request_id: typeof value.request_id === "string" ? value.request_id : null,
    analysis_request_id: typeof value.analysis_request_id === "string" ? value.analysis_request_id : null,
    status: "FAILED",
    reason_code: typeof value.reason_code === "string" ? value.reason_code : null,
    failed_stage: typeof value.failed_stage === "string" ? value.failed_stage : null,
    completed_stages: Array.isArray(value.completed_stages) && value.completed_stages.every((item) => typeof item === "string") ? [...value.completed_stages] : [],
    lifecycle,
    evidence,
    readiness: typeof value.readiness === "string" ? value.readiness : null,
    handoff_status: "NOT_REACHED",
  };
}

function parseProjectQA(value: unknown): ProjectQAResponseV1 {
  if (!isRecord(value) || typeof value.answer !== "string" || !Array.isArray(value.unknowns) || !Array.isArray(value.supporting_records) || !value.supporting_records.every(isSource)) {
    throw new ControlPlaneClientError("Control Plane returned an invalid Project QA response");
  }
  return value as unknown as ProjectQAResponseV1;
}

function parseDevelopmentAnalysis(value: unknown): DevelopmentAnalysisV1 {
  if (!isRecord(value) || value.identity !== "chappie_development_analysis/2.0.0" || typeof value.state !== "string" || typeof value.request !== "string" || !isStringArray(value.facts) || !isStringArray(value.inferences) || !isStringArray(value.unknowns) || !isStringArray(value.architecture_owners) || !isStringArray(value.affected_contracts) || !isStringArray(value.likely_files) || !isStringArray(value.risks) || !isStringArray(value.required_tests) || !isStringArray(value.non_goals) || !isStringArray(value.acceptance_criteria) || !isStringArray(value.current_evidence_handles) || (value.context_map !== null && !isRecord(value.context_map)) || (value.proposed_scope !== null && !isRecord(value.proposed_scope)) || typeof value.validation_status !== "string" || (value.readiness !== null && value.readiness !== "READY_FOR_TASK" && value.readiness !== "INSUFFICIENT_EVIDENCE" && value.readiness !== "NEEDS_REVIEW") || (value.reason_code !== null && typeof value.reason_code !== "string")) {
    throw new ControlPlaneClientError("Control Plane returned an invalid Development Analysis response");
  }
  if (value.lifecycle !== undefined && value.lifecycle !== null && !isDevelopmentLifecycle(value.lifecycle)) {
    throw new ControlPlaneClientError("Control Plane returned invalid Development lifecycle telemetry");
  }
  return value as unknown as DevelopmentAnalysisV1;
}

function isDevelopmentLifecycle(value: unknown): boolean {
  if (!isRecord(value) || typeof value.request_id !== "string" || !/^[a-f0-9]{32}$/.test(value.request_id) || value.operation !== "DEVELOPMENT_ANALYSIS" || typeof value.requested_role !== "string" || (value.effective_model !== null && typeof value.effective_model !== "string") || !Array.isArray(value.events)) return false;
  const stages = new Set([
    "REQUEST_ACCEPTED",
    "ROLE_RESOLUTION",
    "EVIDENCE_BUILD",
    "SYNTHESIS",
    "GROUNDING_SYNTHESIS",
    "GROUNDING_PARSE",
    "PROPOSAL_SYNTHESIS",
    "PROPOSAL_PARSE",
    "REVIEW_SYNTHESIS",
    "REVIEW_PARSE",
    "STRUCTURAL_VALIDATION",
    "READINESS_EVALUATION",
    "COMPLETE",
  ]);
  return value.events.every((event) => isRecord(event) && stages.has(String(event.stage)) && ["STARTED", "COMPLETED", "FAILED"].includes(String(event.status)) && typeof event.elapsed_ms === "number" && Number.isInteger(event.elapsed_ms) && event.elapsed_ms >= 0 && event.elapsed_ms <= 120_000 && (event.reason_code === null || typeof event.reason_code === "string"));
}

function parseCodexTask(value: unknown): CodexTaskV1 {
  if (!isRecord(value) || value.identity !== "chappie_codex_handoff/1.0.0" || typeof value.task_id !== "string" || (value.state !== "ADVISORY" && value.state !== "VALIDATION_ERROR") || typeof value.projection !== "string" || value.approval_status !== "HUMAN_APPROVAL_REQUIRED") {
    throw new ControlPlaneClientError("Control Plane returned an invalid Codex task response");
  }
  return value as unknown as CodexTaskV1;
}

function isStringArray(value: unknown): value is string[] { return Array.isArray(value) && value.every((item) => typeof item === "string"); }

function isSource(value: unknown): boolean {
  return isRecord(value) && [value.record_id, value.title, value.authority, value.status].every((item) => typeof item === "string");
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isEnvelope(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null &&
    typeof (value as Record<string, unknown>).transport_contract === "string" &&
    typeof (value as Record<string, unknown>).request_id === "string" &&
    typeof (value as Record<string, unknown>).status === "string";
}
