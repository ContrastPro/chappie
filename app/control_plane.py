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
from app.bootstrap import (
    ChappieDoctorReportV1,
    ChappieLocalConfigV1,
    ChappieStatusV1,
    ComponentStateV1,
    doctor,
    load_local_config,
)

CONTROL_PLANE_IDENTITY = "chappie_control_plane/1.2.0"
PROJECT_QA_MAX_QUESTION_CHARACTERS = 1000


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
        "methods": [
            "get_status",
            "get_diagnostics",
            "get_ai_status",
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
        project_qa_runner: Callable[[object, str, object | None], ProjectQAControlPlaneResponseV1]
        | None = None,
    ) -> None:
        self._paths = paths
        self._status_collector = status_collector
        self._diagnostics_collector = diagnostics_collector
        self._config_loader = config_loader
        self._project_qa_runner = project_qa_runner or _run_project_qa

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
        self, question: str, workspace_context: object | None = None
    ) -> ProjectQAControlPlaneResponseV1:
        if (
            not isinstance(question, str)
            or not question.strip()
            or len(question) > PROJECT_QA_MAX_QUESTION_CHARACTERS
        ):
            return ProjectQAControlPlaneResponseV1(
                ProjectQAStateV1.VALIDATION_ERROR,
                question if isinstance(question, str) else "",
                error=ControlPlaneErrorV1(
                    ControlPlaneStateV1.UNAVAILABLE, "INVALID_PROJECT_QA_QUESTION"
                ),
            )
        try:
            from app.workspace_context import parse_workspace_context

            context = (
                parse_workspace_context(workspace_context)
                if workspace_context is not None
                else None
            )
            return self._project_qa_runner(self._paths, question.strip(), context)
        except (OSError, RuntimeError, TypeError, ValueError):
            return _project_qa_error(
                ProjectQAStateV1.INTERNAL_ERROR, question.strip(), "PROJECT_QA_UNAVAILABLE"
            )


def _run_project_qa(
    paths: object, question: str, workspace_context: object | None = None
) -> ProjectQAControlPlaneResponseV1:
    """Execute one bounded Project QA request over an existing verified Knowledge store."""
    from app.ai.errors import AIProviderError, AIProviderTimeoutError, AIValidationError
    from app.ai_setup_smoke import build_configured_chappie_ai, valid_setup_ai_config
    from app.knowledge import read_store
    from app.knowledge_retrieval import KnowledgeRetrievalRequestV1, retrieve

    config = load_local_config(paths)
    if config is None or not valid_setup_ai_config(config):
        return _project_qa_error(ProjectQAStateV1.AI_UNAVAILABLE, question, "AI_UNAVAILABLE")
    try:
        records, knowledge_manifest = read_store(paths.knowledge_file)
        _result, context = retrieve(
            records,
            knowledge_manifest,
            KnowledgeRetrievalRequestV1(topic_tokens=_question_tokens(question)),
        )
    except ValueError:
        return _project_qa_error(
            ProjectQAStateV1.KNOWLEDGE_UNAVAILABLE, question, "KNOWLEDGE_UNAVAILABLE"
        )
    if not context.records:
        return _project_qa_error(
            ProjectQAStateV1.KNOWLEDGE_UNAVAILABLE, question, "KNOWLEDGE_UNAVAILABLE"
        )
    ai = None
    try:
        ai = build_configured_chappie_ai(config)
        answer = ai.answer_project_qa(
            question=question, knowledge_context=context, workspace_context=workspace_context
        )
    except AIProviderTimeoutError:
        return _project_qa_error(ProjectQAStateV1.TIMEOUT, question, "AI_TIMEOUT")
    except AIProviderError:
        return _project_qa_error(ProjectQAStateV1.AI_UNAVAILABLE, question, "AI_UNAVAILABLE")
    except AIValidationError:
        return _project_qa_error(
            ProjectQAStateV1.VALIDATION_ERROR, question, "PROJECT_QA_VALIDATION_ERROR"
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
        ProjectQAStateV1.OK,
        question,
        answer.answer,
        answer.supporting_knowledge_fact_ids,
        sources,
        answer.unknowns,
        answer.context_digest,
        config.provider,
        config.model,
        "VALID",
        None,
        workspace_context.context_digest if workspace_context else None,
        _workspace_provenance(workspace_context),
    )


def _question_tokens(question: str) -> tuple[str, ...]:
    return tuple(sorted({token.casefold() for token in question.split() if token.isalnum()}))[:16]


def _project_qa_error(
    state: ProjectQAStateV1, question: str, reason_code: str
) -> ProjectQAControlPlaneResponseV1:
    return ProjectQAControlPlaneResponseV1(
        state,
        question,
        error=ControlPlaneErrorV1(ControlPlaneStateV1.UNAVAILABLE, reason_code),
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
