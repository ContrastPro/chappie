"""Deterministic user/agent outcome projection for Development Analysis.

This layer does not alter structural validation or task readiness.  It makes
explicit when a valid analysis cannot safely satisfy its requested objective
with the bounded evidence that was supplied.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.development_analysis.models import (
    DevelopmentAnalysisReadinessV1,
    DevelopmentAnalysisStateV1,
    DevelopmentAnalysisV1,
)

DEVELOPMENT_OUTCOME_IDENTITY = "chappie_development_outcome/1.0.0"


class DevelopmentOutcomeStatusV1(StrEnum):
    COMPLETED = "COMPLETED"
    NEEDS_CONTEXT = "NEEDS_CONTEXT"
    NEEDS_HUMAN_REVIEW = "NEEDS_HUMAN_REVIEW"
    UNSUPPORTED_WITH_CURRENT_CAPABILITIES = "UNSUPPORTED_WITH_CURRENT_CAPABILITIES"
    SYSTEM_FAILURE = "SYSTEM_FAILURE"


@dataclass(frozen=True, slots=True)
class DevelopmentOutcomeV1:
    status: DevelopmentOutcomeStatusV1
    blocker_code: str | None
    explanation: str
    required_context: str | None
    suggested_next_action: str
    safe_to_retry: bool
    request_id: str | None
    failed_stage: str | None
    reason_code: str | None
    no_changes_made: bool
    unknowns: tuple[str, ...] = ()
    why_it_blocks: str = ""

    def payload(self) -> dict[str, object]:
        return {
            "identity": DEVELOPMENT_OUTCOME_IDENTITY,
            "status": self.status.value,
            "blocker_code": self.blocker_code,
            "explanation": self.explanation,
            "required_context": self.required_context,
            "suggested_next_action": self.suggested_next_action,
            "safe_to_retry": self.safe_to_retry,
            "request_id": self.request_id,
            "failed_stage": self.failed_stage,
            "reason_code": self.reason_code,
            "no_changes_made": self.no_changes_made,
            "why_it_blocks": self.why_it_blocks,
        }


def deduplicate_development_unknowns(unknowns: tuple[str, ...]) -> tuple[str, ...]:
    """Keep first occurrence order; do not merge semantically different text."""
    return tuple(dict.fromkeys(item.strip() for item in unknowns if item.strip()))


def project_development_outcome(analysis: DevelopmentAnalysisV1) -> DevelopmentOutcomeV1:
    """Project an evidence-bound recovery state without asking a model for recovery advice."""
    unknowns = deduplicate_development_unknowns(analysis.unknowns)
    request_id, failed_stage = _lifecycle_metadata(analysis)
    if analysis.state is not DevelopmentAnalysisStateV1.OK:
        return _failure_outcome(analysis, unknowns, request_id, failed_stage)
    if analysis.validation_status != "VALID":
        return DevelopmentOutcomeV1(
            DevelopmentOutcomeStatusV1.SYSTEM_FAILURE,
            "DEVELOPMENT_ANALYSIS_INVALID",
            "Development analysis did not pass structural validation.",
            None,
            "Correct the bounded analysis failure before retrying the same request.",
            False,
            request_id,
            failed_stage,
            analysis.reason_code,
            True,
            unknowns,
            "Structural validity is required before the advisory result can be used safely.",
        )
    from app.development_analysis.repository_inventory import development_context_requirement

    requirement = development_context_requirement(analysis)
    inventory_supplied = bool(analysis.evidence and analysis.evidence.repository_inventory_digest)
    if requirement is not None and not inventory_supplied:
        return DevelopmentOutcomeV1(
            DevelopmentOutcomeStatusV1.NEEDS_CONTEXT,
            "REPOSITORY_INVENTORY_REQUIRED",
            "Repository-wide file and consumer evidence is unavailable.",
            "REPOSITORY_INVENTORY",
            "Build a bounded read-only repository inventory, then rerun the analysis.",
            False,
            request_id,
            None,
            None,
            True,
            unknowns,
            "Safe cleanup classification cannot be proven from the current bounded context.",
        )
    if requirement is not None:
        return DevelopmentOutcomeV1(
            DevelopmentOutcomeStatusV1.NEEDS_CONTEXT,
            "REPOSITORY_INVENTORY_GAP_UNRESOLVED",
            "Repository inventory was supplied, but the analysis still reports repository-wide evidence gaps.",
            "TARGETED_CONTEXT",
            "Review the exact remaining bounded evidence gaps before rerunning the analysis.",
            False,
            request_id,
            None,
            analysis.reason_code,
            True,
            unknowns,
            "Supplying a capability does not prove that the task-specific evidence gap was resolved.",
        )
    required_context = _blocking_targeted_context(unknowns)
    if required_context is not None:
        return DevelopmentOutcomeV1(
            DevelopmentOutcomeStatusV1.NEEDS_CONTEXT,
            f"{required_context}_CONTEXT_REQUIRED",
            f"The required {required_context.lower()} evidence is unavailable.",
            required_context,
            f"Supply the exact bounded {required_context.lower()} context, then rerun the analysis.",
            False,
            request_id,
            None,
            None,
            True,
            unknowns,
            "The requested task cannot be completed without the exact missing evidence.",
        )
    if analysis.readiness is DevelopmentAnalysisReadinessV1.NEEDS_REVIEW:
        return DevelopmentOutcomeV1(
            DevelopmentOutcomeStatusV1.NEEDS_HUMAN_REVIEW,
            "HUMAN_DECISION_REQUIRED",
            "The bounded evidence leaves an implementation decision ambiguous.",
            None,
            "Provide a human ownership or scope decision before retrying.",
            False,
            request_id,
            None,
            analysis.reason_code,
            True,
            unknowns,
            "A human decision is required before implementation scope can be authorized.",
        )
    if analysis.readiness is DevelopmentAnalysisReadinessV1.INSUFFICIENT_EVIDENCE:
        return DevelopmentOutcomeV1(
            DevelopmentOutcomeStatusV1.NEEDS_CONTEXT,
            "TASK_CONTEXT_INSUFFICIENT",
            "The supplied bounded evidence is insufficient for the requested task.",
            "TARGETED_CONTEXT",
            "Supply the exact missing file, contract, relation, symbol, or test context.",
            False,
            request_id,
            None,
            analysis.reason_code,
            True,
            unknowns,
            "The supplied bounded evidence cannot support the requested task safely.",
        )
    return DevelopmentOutcomeV1(
        DevelopmentOutcomeStatusV1.COMPLETED,
        None,
        "Development analysis completed with sufficient bounded evidence.",
        None,
        "Review the advisory analysis and obtain human approval before implementation.",
        False,
        request_id,
        None,
        None,
        True,
        unknowns,
        "",
    )


def _blocking_targeted_context(unknowns: tuple[str, ...]) -> str | None:
    """Recognize only explicit bounded request markers, never arbitrary unknown text."""
    markers = (
        ("missing context: file", "FILE"),
        ("missing context: contract", "CONTRACT"),
        ("missing context: relation", "RELATION"),
        ("missing context: symbol", "SYMBOL"),
        ("missing context: test", "TEST"),
    )
    normalized = tuple(item.casefold() for item in unknowns)
    for marker, kind in markers:
        if any(item.startswith(marker) for item in normalized):
            return kind
    return None


def _failure_outcome(
    analysis: DevelopmentAnalysisV1,
    unknowns: tuple[str, ...],
    request_id: str | None,
    failed_stage: str | None,
) -> DevelopmentOutcomeV1:
    reason = analysis.reason_code or analysis.state.value
    if reason in {
        "REPOSITORY_INVENTORY_UNSUPPORTED",
        "UNSUPPORTED_WITH_CURRENT_CAPABILITIES",
    }:
        return DevelopmentOutcomeV1(
            DevelopmentOutcomeStatusV1.UNSUPPORTED_WITH_CURRENT_CAPABILITIES,
            reason,
            "The requested evidence cannot be produced by the current bounded capabilities.",
            "REPOSITORY_INVENTORY",
            "Use an approved capability that can supply the required bounded evidence.",
            False,
            request_id,
            failed_stage,
            reason,
            True,
            unknowns,
            "The required evidence capability is not available in the current bounded runtime.",
        )
    if reason in {"AI_PROVIDER_TIMEOUT", "AI_UNAVAILABLE", "MODEL_ROLE_UNAVAILABLE"}:
        explanation = (
            "The selected local AI provider did not complete the bounded Development request."
            if reason == "AI_PROVIDER_TIMEOUT"
            else "The selected local AI dependency is currently unavailable."
        )
        return DevelopmentOutcomeV1(
            DevelopmentOutcomeStatusV1.SYSTEM_FAILURE,
            reason,
            explanation,
            None,
            "Retry after the local AI provider or selected model is available.",
            True,
            request_id,
            failed_stage,
            reason,
            True,
            unknowns,
            "The technical dependency must recover before the request can complete.",
        )
    return DevelopmentOutcomeV1(
        DevelopmentOutcomeStatusV1.SYSTEM_FAILURE,
        reason,
        "Development analysis could not complete safely.",
        None,
        "Review the bounded technical diagnostic before retrying.",
        False,
        request_id,
        failed_stage,
        reason,
        True,
        unknowns,
        "The bounded technical diagnostic must be resolved before retrying.",
    )


def _lifecycle_metadata(analysis: DevelopmentAnalysisV1) -> tuple[str | None, str | None]:
    lifecycle = analysis.lifecycle
    if lifecycle is None:
        return None, None
    failed = next(
        (item.stage.value for item in lifecycle.events if item.status.value == "FAILED"), None
    )
    return lifecycle.request_id, failed
