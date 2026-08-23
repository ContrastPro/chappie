"""Frontend-neutral, read-only Chappie Local Control Plane v1.

This module deliberately contains no transport, setup, provider, Knowledge rebuild,
AI generation, RDE execution, or trading authority.  It projects existing bounded
application diagnostics into stable JSON-safe contracts for the CLI and future local
frontends.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from app.ai.schemas import canonical_json, digest
from app.platform.bootstrap import (
    ChappieDoctorReportV1,
    ChappieLocalConfigV1,
    ChappieStatusV1,
    ComponentStateV1,
    doctor,
    load_local_config,
)

CONTROL_PLANE_IDENTITY = "chappie_control_plane/1.4.0"
PROJECT_QA_MAX_QUESTION_CHARACTERS = 12_000


class ControlPlaneStateV1(StrEnum):
    OK = "OK"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class CapabilityStatusV1(StrEnum):
    AVAILABLE = "AVAILABLE"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"
    PLANNED = "PLANNED"


class ProjectQAStateV1(StrEnum):
    OK = "OK"
    KNOWLEDGE_UNAVAILABLE = "KNOWLEDGE_UNAVAILABLE"
    AI_UNAVAILABLE = "AI_UNAVAILABLE"
    TIMEOUT = "TIMEOUT"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"


CONTROL_PLANE_CONTRACT_DIGEST = digest(
    {
        "identity": CONTROL_PLANE_IDENTITY,
        "states": [item.value for item in ControlPlaneStateV1],
        "capability_states": [item.value for item in CapabilityStatusV1],
        "project_qa_states": [item.value for item in ProjectQAStateV1],
        "project_qa_workspace_context": "workspace_context/1.0.0",
        "project_qa_effective_model_metadata": "1.0.0",
        "methods": [
            "get_status",
            "get_diagnostics",
            "get_ai_status",
            "list_models",
            "get_capabilities",
            "ask_project",
        ],
        "authority": "read_only_application_projection",
    }
)


def _json_safe(value: object) -> object:
    """Normalize nested legacy payload tuples without exposing Python objects."""
    return json.loads(canonical_json(value))


@dataclass(frozen=True, slots=True)
class ControlPlaneErrorV1:
    state: ControlPlaneStateV1
    reason_code: str

    def payload(self) -> dict[str, str]:
        return {"state": self.state.value, "reason_code": self.reason_code}


@dataclass(frozen=True, slots=True)
class ChappieCapabilityV1:
    capability_id: str
    category: str
    status: CapabilityStatusV1
    authority: str
    read_only: bool
    requires_ai: bool
    requires_knowledge: bool
    description: str

    def payload(self) -> dict[str, object]:
        return {
            "capability_id": self.capability_id,
            "category": self.category,
            "status": self.status.value,
            "authority": self.authority,
            "read_only": self.read_only,
            "requires_ai": self.requires_ai,
            "requires_knowledge": self.requires_knowledge,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class ControlPlaneStatusV1:
    state: ControlPlaneStateV1
    status: ChappieStatusV1 | None
    error: ControlPlaneErrorV1 | None = None

    def payload(self) -> dict[str, object]:
        return {
            "identity": CONTROL_PLANE_IDENTITY,
            "contract_digest": CONTROL_PLANE_CONTRACT_DIGEST,
            "state": self.state.value,
            "status": _json_safe(self.status.payload()) if self.status else None,
            "error": self.error.payload() if self.error else None,
        }


@dataclass(frozen=True, slots=True)
class ControlPlaneDiagnosticsV1:
    state: ControlPlaneStateV1
    report: ChappieDoctorReportV1 | None
    error: ControlPlaneErrorV1 | None = None

    def payload(self) -> dict[str, object]:
        return {
            "identity": CONTROL_PLANE_IDENTITY,
            "contract_digest": CONTROL_PLANE_CONTRACT_DIGEST,
            "state": self.state.value,
            "diagnostics": _json_safe(self.report.payload()) if self.report else None,
            "error": self.error.payload() if self.error else None,
        }


@dataclass(frozen=True, slots=True)
class ControlPlaneAIStatusV1:
    state: ControlPlaneStateV1
    provider: str | None
    profile: str | None
    model: str | None
    provider_state: str | None
    model_state: str | None
    error: ControlPlaneErrorV1 | None = None

    def payload(self) -> dict[str, object]:
        return {
            "identity": CONTROL_PLANE_IDENTITY,
            "contract_digest": CONTROL_PLANE_CONTRACT_DIGEST,
            "state": self.state.value,
            "provider": self.provider,
            "profile": self.profile,
            "model": self.model,
            "provider_state": self.provider_state,
            "model_state": self.model_state,
            "error": self.error.payload() if self.error else None,
        }


@dataclass(frozen=True, slots=True)
class ControlPlaneCapabilitiesV1:
    state: ControlPlaneStateV1
    capabilities: tuple[ChappieCapabilityV1, ...]
    error: ControlPlaneErrorV1 | None = None

    def payload(self) -> dict[str, object]:
        return {
            "identity": CONTROL_PLANE_IDENTITY,
            "contract_digest": CONTROL_PLANE_CONTRACT_DIGEST,
            "state": self.state.value,
            "capabilities": [item.payload() for item in self.capabilities],
            "error": self.error.payload() if self.error else None,
        }


@dataclass(frozen=True, slots=True)
class ProjectQASourceV1:
    record_id: str
    title: str
    authority: str
    status: str

    def payload(self) -> dict[str, str]:
        return {
            "record_id": self.record_id,
            "title": self.title,
            "authority": self.authority,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class ProjectQAControlPlaneResponseV1:
    state: ProjectQAStateV1
    question: str
    answer: str | None = None
    supporting_knowledge_fact_ids: tuple[str, ...] = ()
    supporting_records: tuple[ProjectQASourceV1, ...] = ()
    unknowns: tuple[str, ...] = ()
    context_digest: str | None = None
    provider: str | None = None
    model: str | None = None
    validation_status: str | None = None
    error: ControlPlaneErrorV1 | None = None
    workspace_context_digest: str | None = None
    workspace_context_used: tuple[str, ...] = ()
    requested_model_id: str | None = None
    requested_model_role: str | None = None
    requested_model: str | None = None
    effective_model_id: str | None = None
    effective_model: str | None = None
    profile_identity: str | None = None
    capability_identity: str | None = None

    def payload(self) -> dict[str, object]:
        return {
            "identity": CONTROL_PLANE_IDENTITY,
            "contract_digest": CONTROL_PLANE_CONTRACT_DIGEST,
            "state": self.state.value,
            "question": self.question,
            "answer": self.answer,
            "supporting_knowledge_fact_ids": list(self.supporting_knowledge_fact_ids),
            "supporting_records": [item.payload() for item in self.supporting_records],
            "unknowns": list(self.unknowns),
            "context_digest": self.context_digest,
            "workspace_context_digest": self.workspace_context_digest,
            "workspace_context_used": list(self.workspace_context_used),
            "provider": self.provider,
            "model": self.model,
            "requested_model_id": self.requested_model_id,
            "requested_model_role": self.requested_model_role,
            "requested_model": self.requested_model,
            "effective_model_id": self.effective_model_id,
            "effective_model": self.effective_model,
            "profile_identity": self.profile_identity,
            "capability_identity": self.capability_identity,
            "authority": "AI_WITH_KNOWLEDGE_READ_ONLY",
            "validation_status": self.validation_status,
            "error": self.error.payload() if self.error else None,
        }


def _state(status: ChappieStatusV1) -> ControlPlaneStateV1:
    if status.overall_state.value == "READY":
        return ControlPlaneStateV1.OK
    return ControlPlaneStateV1.DEGRADED


def _component_state(status: ChappieStatusV1, name: str) -> ComponentStateV1 | None:
    return next((item.state for item in status.components if item.component == name), None)


def _availability(component: ComponentStateV1 | None) -> CapabilityStatusV1:
    if component == ComponentStateV1.READY:
        return CapabilityStatusV1.AVAILABLE
    if component in {ComponentStateV1.NOT_CONFIGURED, ComponentStateV1.NOT_INSTALLED}:
        return CapabilityStatusV1.UNAVAILABLE
    return CapabilityStatusV1.DEGRADED


class ChappieControlPlaneV1:
    """Read-only facade over existing product status and diagnostics services."""

    def __init__(
        self,
        paths,
        *,
        status_collector: Callable[[object], ChappieStatusV1],
        diagnostics_collector: Callable[[object], ChappieDoctorReportV1] = doctor,
        config_loader: Callable[[object], ChappieLocalConfigV1 | None] = load_local_config,
        model_state_reader: Callable[[], object] | None = None,
        project_qa_runner: Callable[[object, str, object | None], ProjectQAControlPlaneResponseV1]
        | None = None,
    ) -> None:
        self._paths = paths
        self._status_collector = status_collector
        self._diagnostics_collector = diagnostics_collector
        self._config_loader = config_loader
        self._model_state_reader = model_state_reader
        self._project_qa_runner = project_qa_runner or _run_project_qa

    def list_models(self):
        """Project backend-approved local models without provider mutation."""
        from app.ai.model_policy import project_local_inventory
        from app.platform.ollama.model_state import LocalOllamaModelStateAdapterV1

        config = self._config_loader(self._paths)
        if self._model_state_reader is not None:
            state = self._model_state_reader()
        else:
            import httpx

            with httpx.Client(base_url="http://localhost:11434", timeout=2.0) as client:
                state = LocalOllamaModelStateAdapterV1(client).read()
        installed = set(state.installed_models)
        ready = installed if state.runtime_ready else set()
        return project_local_inventory(
            installed_models=installed,
            ready_models=ready,
            default_model=config.model if config else None,
        )

    def get_status(self) -> ControlPlaneStatusV1:
        try:
            status = self._status_collector(self._paths)
        except (OSError, RuntimeError, TypeError, ValueError):
            return ControlPlaneStatusV1(
                ControlPlaneStateV1.INTERNAL_ERROR,
                None,
                ControlPlaneErrorV1(ControlPlaneStateV1.INTERNAL_ERROR, "STATUS_UNAVAILABLE"),
            )
        return ControlPlaneStatusV1(_state(status), status)

    def get_diagnostics(self) -> ControlPlaneDiagnosticsV1:
        try:
            report = self._diagnostics_collector(self._paths)
        except (OSError, RuntimeError, TypeError, ValueError):
            return ControlPlaneDiagnosticsV1(
                ControlPlaneStateV1.INTERNAL_ERROR,
                None,
                ControlPlaneErrorV1(ControlPlaneStateV1.INTERNAL_ERROR, "DIAGNOSTICS_UNAVAILABLE"),
            )
        return ControlPlaneDiagnosticsV1(_state(report.status), report)

    def get_ai_status(self) -> ControlPlaneAIStatusV1:
        status_response = self.get_status()
        if status_response.status is None:
            return ControlPlaneAIStatusV1(
                ControlPlaneStateV1.INTERNAL_ERROR,
                None,
                None,
                None,
                None,
                None,
                status_response.error,
            )
        try:
            config = self._config_loader(self._paths)
        except (OSError, RuntimeError, TypeError, ValueError):
            config = None
        provider_state = _component_state(status_response.status, "AI_PROVIDER")
        model_state = _component_state(status_response.status, "AI_MODEL")
        state = (
            ControlPlaneStateV1.OK
            if provider_state == model_state == ComponentStateV1.READY
            else ControlPlaneStateV1.DEGRADED
        )
        return ControlPlaneAIStatusV1(
            state,
            config.provider if config else None,
            config.enabled_capabilities[0].upper()
            if config and config.enabled_capabilities
            else None,
            config.model if config else None,
            provider_state.value if provider_state else None,
            model_state.value if model_state else None,
        )

    def get_capabilities(self) -> ControlPlaneCapabilitiesV1:
        status_response = self.get_status()
        if status_response.status is None:
            return ControlPlaneCapabilitiesV1(
                ControlPlaneStateV1.INTERNAL_ERROR, (), status_response.error
            )
        status = status_response.status
        knowledge = _availability(_component_state(status, "KNOWLEDGE"))
        ai = _availability(_component_state(status, "AI_MODEL"))
        research = _availability(_component_state(status, "RESEARCH_STORAGE"))
        capabilities = (
            ChappieCapabilityV1(
                "system.status",
                "SYSTEM",
                CapabilityStatusV1.AVAILABLE,
                "CONTROL_PLANE",
                True,
                False,
                False,
                "Read current product status.",
            ),
            ChappieCapabilityV1(
                "system.doctor",
                "SYSTEM",
                CapabilityStatusV1.AVAILABLE,
                "CONTROL_PLANE",
                True,
                False,
                False,
                "Read safe diagnostics and remediation codes.",
            ),
            ChappieCapabilityV1(
                "ai.status",
                "AI",
                CapabilityStatusV1.AVAILABLE,
                "CONTROL_PLANE",
                True,
                False,
                False,
                "Read provider and configured-model status.",
            ),
            ChappieCapabilityV1(
                "knowledge.query",
                "KNOWLEDGE",
                knowledge,
                "KNOWLEDGE",
                True,
                False,
                True,
                "Retrieve bounded canonical Knowledge.",
            ),
            ChappieCapabilityV1(
                "project.qa",
                "AI",
                CapabilityStatusV1.AVAILABLE
                if ai == knowledge == CapabilityStatusV1.AVAILABLE
                else CapabilityStatusV1.DEGRADED,
                "AI_WITH_KNOWLEDGE",
                True,
                True,
                True,
                "Grounded Project QA; AI has no Knowledge write authority.",
            ),
            ChappieCapabilityV1(
                "research.existing_cache",
                "RESEARCH",
                research,
                "RDE",
                True,
                False,
                False,
                "Read verified existing-cache research evidence; execution authority remains RDE.",
            ),
        )
        return ControlPlaneCapabilitiesV1(_state(status), capabilities)

    def ask_project(
        self,
        question: str,
        workspace_context: object | None = None,
        *,
        requested_model_id: str | None = None,
        requested_model_role: str | None = None,
    ) -> ProjectQAControlPlaneResponseV1:
        if not isinstance(question, str) or not question.strip():
            return ProjectQAControlPlaneResponseV1(
                ProjectQAStateV1.VALIDATION_ERROR,
                question if isinstance(question, str) else "",
                error=ControlPlaneErrorV1(
                    ControlPlaneStateV1.UNAVAILABLE, "INVALID_PROJECT_QA_QUESTION"
                ),
            )
        if len(question) > PROJECT_QA_MAX_QUESTION_CHARACTERS:
            return ProjectQAControlPlaneResponseV1(
                ProjectQAStateV1.VALIDATION_ERROR,
                question,
                error=ControlPlaneErrorV1(
                    ControlPlaneStateV1.UNAVAILABLE, "PROJECT_QA_REQUEST_TOO_LARGE"
                ),
            )
        try:
            from app.control_plane.workspace_context import parse_workspace_context

            context = (
                parse_workspace_context(workspace_context)
                if workspace_context is not None
                else None
            )
        except (TypeError, ValueError):
            return _project_qa_error(
                ProjectQAStateV1.VALIDATION_ERROR,
                question.strip(),
                "WORKSPACE_CONTEXT_INVALID",
            )
        if requested_model_id is not None and requested_model_role is not None:
            return _project_qa_error(
                ProjectQAStateV1.VALIDATION_ERROR,
                question.strip(),
                "MODEL_SELECTION_AMBIGUOUS",
            )
        if requested_model_role is not None:
            try:
                from app.ai.model_policy import (
                    DevelopmentModelRoleV1,
                    resolve_development_model_role,
                )

                role = DevelopmentModelRoleV1(requested_model_role)
                role_descriptor, model_descriptor = resolve_development_model_role(role)
                role_item = next(item for item in self.list_models() if item.role == role)
            except (OSError, RuntimeError, TypeError, ValueError, StopIteration):
                return _project_qa_error(
                    ProjectQAStateV1.VALIDATION_ERROR,
                    question.strip(),
                    "MODEL_ROLE_INVALID",
                )
            if not role_item.ready:
                return _project_qa_error(
                    ProjectQAStateV1.AI_UNAVAILABLE,
                    question.strip(),
                    "MODEL_ROLE_UNAVAILABLE",
                    requested_model_role=role.value,
                    requested_model=model_descriptor.concrete_model,
                    profile_identity=role_descriptor.profile_identity,
                    capability_identity=role_descriptor.capability_identity,
                )
        try:
            if requested_model_id is None and requested_model_role is None:
                return self._project_qa_runner(self._paths, question.strip(), context)
            if requested_model_id is not None:
                return self._project_qa_runner(
                    self._paths, question.strip(), context, requested_model_id=requested_model_id
                )
            return self._project_qa_runner(
                self._paths, question.strip(), context, requested_model_role=requested_model_role
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            return _project_qa_error(
                ProjectQAStateV1.INTERNAL_ERROR, question.strip(), "PROJECT_QA_UNAVAILABLE"
            )

    def analyze_development(
        self,
        request: str,
        *,
        requested_role: str,
        workspace_context: object | None = None,
        acceptance_diagnostics_sink: Callable[[object], None] | None = None,
    ):
        """Return bounded read-only analysis; no implementation or repository mutation path exists."""
        from app.ai.model_policy import DevelopmentModelRoleV1
        from app.control_plane.workspace_context import parse_workspace_context
        from app.development_analysis import (
            DevelopmentAnalysisRequestV1,
            DevelopmentAnalysisServiceV1,
        )

        try:
            role = DevelopmentModelRoleV1(requested_role)
            context = parse_workspace_context(workspace_context) if workspace_context else None
            value = DevelopmentAnalysisRequestV1(request, role, context)
        except (TypeError, ValueError):
            from app.development_analysis import DevelopmentAnalysisStateV1, DevelopmentAnalysisV1

            return DevelopmentAnalysisV1(
                DevelopmentAnalysisStateV1.VALIDATION_ERROR,
                request if isinstance(request, str) else "",
                reason_code="INVALID_REQUEST",
            )

        def available(item: object) -> bool:
            return any(model.role == item and model.ready for model in self.list_models())

        return DevelopmentAnalysisServiceV1(
            paths=self._paths,
            role_available=available,
            diagnostics_sink=acceptance_diagnostics_sink,
            runner=lambda item, knowledge, evidence: _run_development_analysis_ai(
                self._paths, item, knowledge, evidence
            ),
        ).analyze(value)

    def prepare_development_task(self, analysis: object):
        """Validate and project an advisory task; it never invokes Codex or mutates state."""
        from app.development_analysis import (
            CodexTaskPreparationRequestV1,
            development_analysis_from_payload,
            prepare_codex_task,
        )

        try:
            return prepare_codex_task(
                CodexTaskPreparationRequestV1(development_analysis_from_payload(analysis))
            )
        except (TypeError, ValueError):
            from app.development_analysis.task import CodexTaskStateV1, CodexTaskV1

            return CodexTaskV1(
                "",
                CodexTaskStateV1.VALIDATION_ERROR,
                "chappie_development_analysis/2.0.0",
                None,
                None,
                None,
                None,
                "",
                validation_status="INVALID",
                reason_code="SOURCE_ANALYSIS_INVALID",
            )


def _run_project_qa(
    paths: object,
    question: str,
    workspace_context: object | None = None,
    *,
    requested_model_id: str | None = None,
    requested_model_role: str | None = None,
) -> ProjectQAControlPlaneResponseV1:
    """Execute one bounded Project QA request over an existing verified Knowledge store."""
    from app.ai.errors import AIProviderError, AIProviderTimeoutError, AIValidationError
    from app.ai.model_policy import (
        DevelopmentModelRoleV1,
        LocalModelIdV1,
        local_model_descriptor_for_concrete_model,
        local_model_id_for_concrete_model,
        resolve_development_model_role,
        resolve_local_model,
    )
    from app.knowledge.retrieval import (
        KnowledgeRetrievalRequestV1,
        architecture_query_tokens,
        retrieve,
        review_record_ids,
    )
    from app.knowledge.store import read_store
    from app.platform.setup.ai_smoke import build_configured_chappie_ai, valid_setup_ai_config

    config = load_local_config(paths)
    if config is None or not valid_setup_ai_config(config):
        return _project_qa_error(ProjectQAStateV1.AI_UNAVAILABLE, question, "AI_UNAVAILABLE")
    try:
        records, knowledge_manifest = read_store(paths.knowledge_file)
        workspace_path = getattr(workspace_context, "active_file_relative_path", None)
        record_ids = review_record_ids(records, workspace_path)
        _result, context = retrieve(
            records,
            knowledge_manifest,
            KnowledgeRetrievalRequestV1(
                topic_tokens=architecture_query_tokens(
                    question,
                    workspace_path=workspace_path,
                ),
                record_ids=record_ids,
                maximum_records=4 if record_ids else 8,
            ),
        )
    except ValueError:
        return _project_qa_error(
            ProjectQAStateV1.KNOWLEDGE_UNAVAILABLE, question, "KNOWLEDGE_UNAVAILABLE"
        )
    if not context.records:
        return _zero_knowledge_response(question, workspace_context)
    try:
        if requested_model_id and requested_model_role:
            raise ValueError("ambiguous model selection")
        requested = LocalModelIdV1(requested_model_id) if requested_model_id else None
        requested_role = (
            DevelopmentModelRoleV1(requested_model_role) if requested_model_role else None
        )
        role_descriptor, role_model_descriptor = (
            resolve_development_model_role(requested_role) if requested_role else (None, None)
        )
        descriptor = (
            role_model_descriptor
            if requested_role
            else resolve_local_model(requested)
            if requested
            else local_model_descriptor_for_concrete_model(config.model or "")
        )
        effective = (
            role_model_descriptor.concrete_model
            if requested_role and role_model_descriptor
            else resolve_local_model(requested).concrete_model
            if requested
            else config.model
        )
        effective = descriptor.concrete_model if descriptor else effective
        effective_model_id = (
            role_model_descriptor.model_id
            if requested_role and role_model_descriptor
            else requested or local_model_id_for_concrete_model(effective)
        )
        timeout_seconds = descriptor.execution_timeout_seconds if descriptor else 45.0
    except ValueError:
        return _project_qa_error(
            ProjectQAStateV1.VALIDATION_ERROR, question, "MODEL_SELECTION_INVALID"
        )
    ai = None
    try:
        ai = build_configured_chappie_ai(config, model=effective, timeout_seconds=timeout_seconds)
        answer = ai.answer_project_qa(
            question=question, knowledge_context=context, workspace_context=workspace_context
        )
    except AIProviderTimeoutError:
        return _project_qa_error(ProjectQAStateV1.TIMEOUT, question, "AI_TIMEOUT")
    except AIProviderError:
        return _project_qa_error(ProjectQAStateV1.AI_UNAVAILABLE, question, "AI_UNAVAILABLE")
    except AIValidationError as error:
        return _project_qa_error(
            ProjectQAStateV1.VALIDATION_ERROR,
            question,
            error.code if error.code != "VALIDATION_ERROR" else "PROJECT_QA_VALIDATION_ERROR",
        )
    finally:
        if ai is not None:
            ai.close()
    supported_ids = {
        fields[1]
        for fact_id in answer.supporting_knowledge_fact_ids
        if len(fields := fact_id.split(".")) >= 3 and fields[0] == "knowledge"
    }
    sources = tuple(
        ProjectQASourceV1(item.knowledge_id, item.title, item.authority.value, item.status.value)
        for item in context.records
        if item.knowledge_id in supported_ids
    )
    return ProjectQAControlPlaneResponseV1(
        state=ProjectQAStateV1.OK,
        question=question,
        answer=answer.answer,
        supporting_knowledge_fact_ids=answer.supporting_knowledge_fact_ids,
        supporting_records=sources,
        unknowns=answer.unknowns,
        context_digest=answer.context_digest,
        provider=config.provider,
        model=effective,
        requested_model_id=requested.value if requested else None,
        requested_model_role=requested_role.value if requested_role else None,
        requested_model=descriptor.concrete_model if descriptor else effective,
        effective_model_id=effective_model_id.value if effective_model_id else None,
        effective_model=effective,
        profile_identity=role_descriptor.profile_identity if role_descriptor else None,
        capability_identity=role_descriptor.capability_identity if role_descriptor else None,
        validation_status="VALID",
        workspace_context_digest=workspace_context.context_digest if workspace_context else None,
        workspace_context_used=_workspace_provenance(workspace_context),
    )


def _run_development_analysis_ai(
    paths: object, request: object, knowledge_context: object, evidence: object
):
    """Reuse the configured local AI only as a read-only synthesis engine."""
    from app.ai.model_policy import resolve_development_model_role
    from app.platform.setup.ai_smoke import build_configured_chappie_ai, valid_setup_ai_config

    config = load_local_config(paths)
    if config is None or not valid_setup_ai_config(config):
        raise RuntimeError("AI_UNAVAILABLE")
    _role, model = resolve_development_model_role(request.requested_role)
    ai = build_configured_chappie_ai(
        config, model=model.concrete_model, timeout_seconds=model.execution_timeout_seconds
    )
    try:
        from app.development_analysis.synthesis import synthesize_development

        return synthesize_development(ai, request, knowledge_context, evidence)
    finally:
        ai.close()


def _project_qa_error(
    state: ProjectQAStateV1,
    question: str,
    reason_code: str,
    *,
    requested_model_role: str | None = None,
    requested_model: str | None = None,
    profile_identity: str | None = None,
    capability_identity: str | None = None,
) -> ProjectQAControlPlaneResponseV1:
    return ProjectQAControlPlaneResponseV1(
        state,
        question,
        error=ControlPlaneErrorV1(ControlPlaneStateV1.UNAVAILABLE, reason_code),
        requested_model_role=requested_model_role,
        requested_model=requested_model,
        profile_identity=profile_identity,
        capability_identity=capability_identity,
    )


def _zero_knowledge_response(
    question: str, workspace_context: object | None
) -> ProjectQAControlPlaneResponseV1:
    """Project deterministic, epistemically-labelled outcomes without invoking AI."""
    if _is_workspace_observation_question(question, workspace_context):
        context = workspace_context
        observed = []
        if context.git_branch:
            observed.append(f"branch {context.git_branch}")
        if context.git_commit:
            observed.append(f"commit {context.git_commit}")
        if context.git_dirty is not None:
            observed.append("working tree dirty" if context.git_dirty else "working tree clean")
        return ProjectQAControlPlaneResponseV1(
            ProjectQAStateV1.OK,
            question,
            "WORKSPACE SNAPSHOT: I can observe "
            + ", ".join(observed)
            + ". No diff or source change was supplied, so I cannot claim either was inspected.",
            unknowns=(
                "No canonical Knowledge record was selected; this is workspace observation.",
            ),
            validation_status="CONTEXT_OBSERVATION",
            workspace_context_digest=context.context_digest,
            workspace_context_used=_workspace_provenance(context),
        )
    if _is_current_capability_question(question):
        return ProjectQAControlPlaneResponseV1(
            ProjectQAStateV1.OK,
            question,
            "Current canonical Knowledge does not establish such an implementation.",
            unknowns=("No relevant canonical Knowledge record was selected.",),
            validation_status="UNKNOWN_CURRENT_CAPABILITY",
        )
    return _project_qa_error(
        ProjectQAStateV1.VALIDATION_ERROR, question, "PROJECT_QA_NO_RELEVANT_KNOWLEDGE"
    )


def _is_current_capability_question(question: str) -> bool:
    words = set(question.casefold().replace("?", " ").split())
    current_markers = {"current", "active", "enabled", "implemented"}
    design_markers = {"architecture", "architectural", "design", "proposed", "inference"}
    return bool(words.intersection(current_markers)) and not words.intersection(design_markers)


def _is_workspace_observation_question(question: str, workspace_context: object | None) -> bool:
    if workspace_context is None:
        return False
    words = set(question.casefold().replace("?", " ").split())
    return bool(words.intersection({"git", "snapshot", "branch", "commit", "dirty", "diff"})) and (
        workspace_context.git_branch is not None or workspace_context.git_commit is not None
    )


def _workspace_provenance(workspace_context: object | None) -> tuple[str, ...]:
    if workspace_context is None:
        return ()
    values = []
    if workspace_context.active_file_relative_path:
        values.append(workspace_context.active_file_relative_path)
    if workspace_context.selected_text_range:
        values.append(
            f"selection lines {workspace_context.selected_text_range.start_line}-{workspace_context.selected_text_range.end_line}"
        )
    elif workspace_context.excerpt_range:
        values.append(
            f"excerpt lines {workspace_context.excerpt_range.start_line}-{workspace_context.excerpt_range.end_line}"
        )
    if workspace_context.git_branch:
        values.append(f"git {workspace_context.git_branch}")
    return tuple(values)
