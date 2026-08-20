import {
  CONTROL_PLANE_DIGEST,
  CONTROL_PLANE_IDENTITY,
  ControlPlaneAIStatusV1,
  ControlPlaneCapabilitiesV1,
  ControlPlaneDiagnosticsV1,
  ControlPlaneStatusV1,
  HealthV1,
  LOOPBACK_HOST,
  TRANSPORT_DIGEST,
  TRANSPORT_IDENTITY,
  TransportEnvelopeV1,
  ProjectQAResponseV1,
} from "./contracts";
import { WorkspaceContextV1 } from "./workspace_context";

export class ControlPlaneClientError extends Error {}
export class ControlPlaneIncompatibleError extends ControlPlaneClientError {}
export class ControlPlaneDependencyError extends ControlPlaneClientError {
  public constructor(public readonly reasonCode: string) { super(reasonCode); }
}

export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

/** Status calls stay responsive; bounded local inference is allowed its backend budget. */
export const CONTROL_PLANE_HEALTH_TIMEOUT_MS = 1_500;
export const PROJECT_QA_HTTP_TIMEOUT_MS = 60_000;

export class ChappieControlPlaneClient {
  public readonly endpoint: string;

  public constructor(
    port: number,
    private readonly fetcher: FetchLike = fetch,
    private readonly healthTimeoutMs = CONTROL_PLANE_HEALTH_TIMEOUT_MS,
    private readonly projectQaTimeoutMs = PROJECT_QA_HTTP_TIMEOUT_MS,
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

  public status(): Promise<ControlPlaneStatusV1> { return this.data("/v1/status"); }
  public diagnostics(): Promise<ControlPlaneDiagnosticsV1> { return this.data("/v1/diagnostics"); }
  public aiStatus(): Promise<ControlPlaneAIStatusV1> { return this.data("/v1/ai/status"); }
  public capabilities(): Promise<ControlPlaneCapabilitiesV1> { return this.data("/v1/capabilities"); }
  public async askProject(question: string, workspaceContext?: WorkspaceContextV1): Promise<ProjectQAResponseV1> {
    if (!question.trim() || question.length > 1000) {
      throw new ControlPlaneClientError("Project question must be between 1 and 1000 characters");
    }
    return parseProjectQA(await this.data(
      "/v1/project/qa",
      { method: "POST", body: JSON.stringify({ question, workspace_context: workspaceContext ?? null }) },
      this.projectQaTimeoutMs,
    ));
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
        if (response.status === 503 && isRecord(error) && typeof error.code === "string") {
          throw new ControlPlaneDependencyError(error.code);
        }
        throw new ControlPlaneClientError(`Control Plane returned HTTP ${response.status}`);
      }
      if (!isEnvelope(value)) { throw new ControlPlaneClientError("Control Plane returned a malformed envelope"); }
      return value as unknown as TransportEnvelopeV1<T>;
    } catch (error) {
      if (error instanceof ControlPlaneClientError) { throw error; }
      if (error instanceof Error && error.name === "AbortError") { throw new ControlPlaneClientError("Control Plane request timed out"); }
      throw new ControlPlaneClientError("Control Plane is unavailable");
    } finally {
      clearTimeout(timer);
    }
  }
}

function parseProjectQA(value: unknown): ProjectQAResponseV1 {
  if (!isRecord(value) || typeof value.answer !== "string" || !Array.isArray(value.unknowns) || !Array.isArray(value.supporting_records) || !value.supporting_records.every(isSource)) {
    throw new ControlPlaneClientError("Control Plane returned an invalid Project QA response");
  }
  return value as unknown as ProjectQAResponseV1;
}

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
