export const CONTROL_PLANE_IDENTITY = "chappie_control_plane/1.4.0";
export const CONTROL_PLANE_DIGEST = "056beea928b723b319250a363153fc2258263ce19b59f59bc0d75cb44fe65775";
export const TRANSPORT_IDENTITY = "chappie_control_plane_transport/1.2.0";
export const TRANSPORT_DIGEST = "e2328fc9618a4ad95a728b128cdfb0d003e60fcd2b5473c91dbffff053993a96";
export const LOOPBACK_HOST = "127.0.0.1";

export type ConnectionState = "DISCONNECTED" | "CONNECTING" | "CONNECTED" | "INCOMPATIBLE" | "ERROR";

export interface TransportErrorV1 { code: string; message: string; }
export interface DevelopmentFailureDiagnosticsV1 {
  operation: "DEVELOPMENT_ANALYSIS";
  request_id: string | null;
  analysis_request_id: string | null;
  status: "FAILED";
  reason_code: string | null;
  failed_stage: string | null;
  completed_stages: readonly string[];
  lifecycle: readonly { stage: string; status: string; elapsed_ms: number; reason_code: string | null; }[];
  evidence: { source_commit?: string; knowledge_identity?: string; knowledge_manifest_digest?: string; knowledge_context_digest?: string; evidence_digest?: string; requested_role?: string; effective_model?: string; record_ids?: readonly string[]; } | null;
  readiness: string | null;
  handoff_status: "NOT_REACHED";
}
export interface TransportEnvelopeV1<T> {
  transport_contract: string;
  transport_contract_digest: string;
  control_plane_contract: string;
  control_plane_contract_digest: string;
  request_id: string;
  status: "OK" | "ERROR";
  data: T | null;
  error: TransportErrorV1 | null;
  diagnostics?: DevelopmentFailureDiagnosticsV1 | null;
}

export interface HealthV1 { status: "OK"; }
export interface ControlPlaneStatusV1 { identity: string; contract_digest: string; state: string; status: Record<string, unknown> | null; error: TransportErrorV1 | null; }
export interface ControlPlaneAIStatusV1 { identity: string; contract_digest: string; state: string; provider: string | null; profile: string | null; model: string | null; provider_state: string | null; model_state: string | null; error: TransportErrorV1 | null; }
export type DevelopmentModelRoleV1 = "FAST" | "CODE" | "DEEP";
export interface ControlPlaneModelV1 { model_id: string; concrete_model: string; installed: boolean; ready: boolean; default: boolean; role: DevelopmentModelRoleV1 | null; profile_identity: string | null; capability_identity: string | null; availability: "AVAILABLE" | "UNAVAILABLE"; }
export interface ControlPlaneModelsV1 { models: ControlPlaneModelV1[]; }
export interface CapabilityV1 { capability_id: string; category: string; status: string; authority: string; read_only: boolean; requires_ai: boolean; requires_knowledge: boolean; description: string; }
export interface ControlPlaneCapabilitiesV1 { identity: string; contract_digest: string; state: string; capabilities: CapabilityV1[]; error: TransportErrorV1 | null; }
export interface ControlPlaneDiagnosticsV1 { identity: string; contract_digest: string; state: string; diagnostics: Record<string, unknown> | null; error: TransportErrorV1 | null; }
export interface ProjectQASourceV1 { record_id: string; title: string; authority: string; status: string; }
export interface ProjectQAResponseV1 {
  identity: string; contract_digest: string; state: string; question: string; answer: string | null;
  supporting_knowledge_fact_ids: string[]; supporting_records: ProjectQASourceV1[];
  unknowns: string[]; context_digest: string | null; provider: string | null; model: string | null;
  authority: "AI_WITH_KNOWLEDGE_READ_ONLY"; validation_status: string | null; error: TransportErrorV1 | null;
  workspace_context_digest?: string | null; workspace_context_used?: string[];
}
export interface DevelopmentEvidencePackV1 { source_commit: string; repository_clean: boolean; knowledge_identity: string; knowledge_manifest_digest: string; knowledge_context_digest: string; workspace_context_digest: string | null; requested_role: DevelopmentModelRoleV1; requested_model: string; effective_model: string; profile_identity: string; capability_identity: string; record_ids: string[]; record_digests: string[]; evidence_digest: string; }
export interface DevelopmentLifecycleEventV1 { stage: "REQUEST_ACCEPTED" | "ROLE_RESOLUTION" | "EVIDENCE_BUILD" | "SYNTHESIS" | "STRUCTURAL_VALIDATION" | "READINESS_EVALUATION" | "COMPLETE"; status: "STARTED" | "COMPLETED" | "FAILED"; elapsed_ms: number; reason_code: string | null; }
export interface DevelopmentLifecycleV1 { request_id: string; operation: "DEVELOPMENT_ANALYSIS"; requested_role: DevelopmentModelRoleV1; effective_model: string | null; events: DevelopmentLifecycleEventV1[]; }
export interface DevelopmentAnalysisRequestV1 { request: string; requested_role: DevelopmentModelRoleV1; workspace_context: object | null; }
export interface DevelopmentEvidenceHandleV1 { handle: string; fact_id: string; record_id: string; record_digest: string; category: string; statement: string; }
export interface DevelopmentContextMapV1 { identity: "chappie_development_context_map/1.0.0"; source_commit: string; knowledge_identity: string; knowledge_manifest_digest: string; knowledge_context_digest: string; evidence_digest: string; requested_role: string; effective_model: string; evidence_handles: DevelopmentEvidenceHandleV1[]; owner_candidates: string[]; existing_contracts: string[]; existing_files: string[]; relevant_tests: string[]; authority_constraints: string[]; context_digest: string; }
export interface DevelopmentProposedScopeV1 { proposed_owner: string | null; proposed_modified_files: string[]; proposed_new_files: string[]; proposed_contract_changes: string[]; proposed_components: string[]; proposed_behavior: string[]; rationale: string[]; }
export interface DevelopmentAnalysisV1 { identity: "chappie_development_analysis/2.0.0"; state: string; request: string; evidence: DevelopmentEvidencePackV1 | null; facts: string[]; inferences: string[]; unknowns: string[]; architecture_owners: string[]; affected_contracts: string[]; likely_files: string[]; risks: string[]; required_tests: string[]; non_goals: string[]; acceptance_criteria: string[]; confidence: number | null; validation_status: string; readiness: "READY_FOR_TASK" | "INSUFFICIENT_EVIDENCE" | "NEEDS_REVIEW" | null; reason_code: string | null; lifecycle?: DevelopmentLifecycleV1 | null; context_map: DevelopmentContextMapV1 | null; current_evidence_handles: string[]; proposed_scope: DevelopmentProposedScopeV1 | null; }
export interface CodexTaskPreparationRequestV1 { analysis: DevelopmentAnalysisV1; }
export interface CodexTaskV1 { identity: "chappie_codex_handoff/1.0.0"; task_id: string; state: "ADVISORY" | "VALIDATION_ERROR"; source_analysis_identity: string; source_commit: string | null; evidence_digest: string | null; context_digest: string | null; requested_role: string | null; problem: string; facts: string[]; inferences: string[]; unknowns: string[]; architecture_owners: string[]; affected_contracts: string[]; current_evidence_handles: string[]; evidence_map: DevelopmentEvidenceHandleV1[]; allowed_files: string[]; proposed_modified_files: string[]; proposed_new_files: string[]; proposed_contract_changes: string[]; forbidden_scope: string[]; implementation_objective: string; implementation_constraints: string[]; risks: string[]; required_tests: string[]; non_goals: string[]; acceptance_criteria: string[]; approval_status: "HUMAN_APPROVAL_REQUIRED"; validation_status: string; reason_code: string | null; projection: string; }
