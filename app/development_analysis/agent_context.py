"""Bounded project-context adapter for read-only external coding agents."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum

from app.ai.model_policy import DevelopmentModelRoleV1
from app.development_analysis.implementation_context import (
    build_implementation_context_from_development,
    build_partial_implementation_context_from_development,
    is_discovery_safe_partial_development_analysis,
)
from app.development_analysis.models import (
    DevelopmentAnalysisStateV1,
    DevelopmentAnalysisV1,
    DevelopmentTaskIntentKindV1,
)
from app.implementation_context import (
    ImplementationContextSufficiencyStateV1,
    ImplementationContextV1,
    assess_implementation_context_sufficiency,
)

AGENT_TASK_CONTEXT_IDENTITY = "chappie_agent_task_context/1.0.0"
MAX_AGENT_TASK_CONTEXT_SERIALIZED_BYTES = 32 * 1024


class AgentTaskContextStateV1(StrEnum):
    READY = "READY"
    NEEDS_CONTEXT = "NEEDS_CONTEXT"
    TASK_TOO_BROAD = "TASK_TOO_BROAD"
    NEEDS_HUMAN_REVIEW = "NEEDS_HUMAN_REVIEW"
    TEMPORARY_FAILURE = "TEMPORARY_FAILURE"


@dataclass(frozen=True, slots=True)
class AgentTaskContextRequestV1:
    """Minimal request accepted by the local coding-agent context endpoint."""

    task: str
    requested_role: DevelopmentModelRoleV1
    requested_intent: DevelopmentTaskIntentKindV1
    workspace_context: object | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.task, str) or not self.task.strip() or len(self.task) > 12_000:
            raise ValueError("agent task is invalid")
        if self.requested_intent is not DevelopmentTaskIntentKindV1.IMPLEMENTATION:
            raise ValueError("agent context requires IMPLEMENTATION intent")


@dataclass(frozen=True, slots=True)
class AgentTaskContextResponseV1:
    """Machine-readable envelope around the existing neutral context DTO."""

    state: AgentTaskContextStateV1
    implementation_context: ImplementationContextV1 | None = None
    sufficiency: str | None = None
    readiness: str | None = None
    reasons: tuple[str, ...] = ()
    missing_context: tuple[dict[str, object], ...] = ()
    reason_code: str | None = None
    identity: str = AGENT_TASK_CONTEXT_IDENTITY

    def __post_init__(self) -> None:
        if self.identity != AGENT_TASK_CONTEXT_IDENTITY:
            raise ValueError("agent task context identity is invalid")
        if self.state is AgentTaskContextStateV1.READY and self.implementation_context is None:
            raise ValueError("ready agent task context requires implementation context")
        if self.implementation_context is not None:
            size = _serialized_size(self.implementation_context.stable_payload())
            if size > MAX_AGENT_TASK_CONTEXT_SERIALIZED_BYTES:
                raise ValueError("agent task context exceeds serialization budget")

    @property
    def context_digest(self) -> str | None:
        return self.implementation_context.digest if self.implementation_context else None

    @property
    def context_available(self) -> bool:
        return self.implementation_context is not None

    @property
    def serialized_context_bytes(self) -> int:
        return (
            _serialized_size(self.implementation_context.stable_payload())
            if self.implementation_context
            else 0
        )

    @property
    def serialized_context_characters(self) -> int:
        return (
            _serialized_characters(self.implementation_context.stable_payload())
            if self.implementation_context
            else 0
        )

    def payload(self) -> dict[str, object]:
        return {
            "identity": self.identity,
            "state": self.state.value,
            "implementation_context": (
                self.implementation_context.stable_payload()
                if self.implementation_context
                else None
            ),
            "context_available": self.context_available,
            "implementation_authorization": "IMPLEMENTATION_NOT_AUTHORIZED",
            "context_digest": self.context_digest,
            "serialized_context_bytes": self.serialized_context_bytes,
            "serialized_context_characters": self.serialized_context_characters,
            "sufficiency": self.sufficiency,
            "readiness": self.readiness,
            "reasons": list(self.reasons),
            "missing_context": list(self.missing_context),
            "reason_code": self.reason_code,
        }


def project_agent_task_context(analysis: DevelopmentAnalysisV1) -> AgentTaskContextResponseV1:
    """Project one completed Development result without retrieval or model calls."""

    if analysis.state in {
        DevelopmentAnalysisStateV1.TIMEOUT,
        DevelopmentAnalysisStateV1.MODEL_UNAVAILABLE,
    }:
        return AgentTaskContextResponseV1(
            AgentTaskContextStateV1.TEMPORARY_FAILURE,
            reason_code=analysis.reason_code or "DEVELOPMENT_UNAVAILABLE",
        )
    if analysis.state is not DevelopmentAnalysisStateV1.OK:
        if is_discovery_safe_partial_development_analysis(analysis):
            try:
                context = build_partial_implementation_context_from_development(
                    analysis.context_map, analysis
                )
            except ValueError:
                context = None
            if context is not None:
                return _project_context_response(
                    AgentTaskContextStateV1.NEEDS_HUMAN_REVIEW,
                    context,
                    analysis,
                    analysis.reason_code or "IMPLEMENTATION_NOT_AUTHORIZED",
                    preserve_state=True,
                )
        return AgentTaskContextResponseV1(
            AgentTaskContextStateV1.NEEDS_HUMAN_REVIEW,
            reason_code=analysis.reason_code or "DEVELOPMENT_ANALYSIS_UNAVAILABLE",
        )
    if analysis.context_map is None:
        return AgentTaskContextResponseV1(
            AgentTaskContextStateV1.NEEDS_CONTEXT,
            reasons=("CONTEXT_MAP_MISSING",),
            reason_code="CONTEXT_MAP_MISSING",
        )
    try:
        context = build_implementation_context_from_development(analysis.context_map, analysis)
    except ValueError:
        return AgentTaskContextResponseV1(
            AgentTaskContextStateV1.NEEDS_CONTEXT,
            reasons=("IMPLEMENTATION_CONTEXT_UNAVAILABLE",),
            reason_code=analysis.reason_code or "IMPLEMENTATION_CONTEXT_UNAVAILABLE",
        )
    return _project_context_response(AgentTaskContextStateV1.READY, context, analysis)


def _project_context_response(
    ready_state: AgentTaskContextStateV1,
    context: ImplementationContextV1,
    analysis: DevelopmentAnalysisV1,
    reason_code: str | None = None,
    *,
    preserve_state: bool = False,
) -> AgentTaskContextResponseV1:
    sufficiency = assess_implementation_context_sufficiency(context)
    missing_context = tuple(item.payload() for item in sufficiency.missing_context)
    if preserve_state or (
        sufficiency.state
        is ImplementationContextSufficiencyStateV1.IMPLEMENTATION_CONTEXT_SUFFICIENT
    ):
        state = ready_state
    elif sufficiency.state is ImplementationContextSufficiencyStateV1.TARGETED_EXPANSION_REQUIRED:
        state = AgentTaskContextStateV1.NEEDS_CONTEXT
    else:
        state = AgentTaskContextStateV1.TASK_TOO_BROAD
    return AgentTaskContextResponseV1(
        state,
        context,
        sufficiency.state.value,
        analysis.readiness.value if analysis.readiness else None,
        sufficiency.reasons,
        missing_context,
        reason_code,
    )


def _serialized_size(value: object) -> int:
    return len(_serialized_json(value).encode("utf-8"))


def _serialized_characters(value: object) -> int:
    return len(_serialized_json(value))


def _serialized_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
