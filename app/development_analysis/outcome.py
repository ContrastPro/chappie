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

DEVELOPMENT_OUTCOME_IDENTITY = "chappie_development_outcome/1.1.0"


class DevelopmentOutcomeStatusV1(StrEnum):
    ANSWER = "ANSWER"
    NEEDS_CONTEXT = "NEEDS_CONTEXT"
    TASK_TOO_BROAD = "TASK_TOO_BROAD"
    NEEDS_HUMAN_REVIEW = "NEEDS_HUMAN_REVIEW"
    UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    TEMPORARY_FAILURE = "TEMPORARY_FAILURE"
    INTERNAL_FAILURE = "INTERNAL_FAILURE"
    # In-process compatibility for existing strict implementation authorization gates.
    COMPLETED = "NEEDS_HUMAN_REVIEW"


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
            DevelopmentOutcomeStatusV1.INTERNAL_FAILURE,
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
    return _successful_outcome(analysis, unknowns, request_id)


def _successful_outcome(
    analysis: DevelopmentAnalysisV1,
    unknowns: tuple[str, ...],
    request_id: str | None,
) -> DevelopmentOutcomeV1:
    from app.development_analysis.models import DevelopmentTaskIntentKindV1

    intent = analysis.task_intent.intent if analysis.task_intent else None
    if intent is DevelopmentTaskIntentKindV1.QUESTION:
        explanation = "The grounded Development question was answered from bounded evidence."
        action = "Review the answer and ask a more specific follow-up if needed."
        status = DevelopmentOutcomeStatusV1.ANSWER
    elif intent is DevelopmentTaskIntentKindV1.ANALYSIS:
        explanation = "The requested advisory Development analysis completed."
        action = "Review the analysis, including its bounded risks and remaining unknowns."
        status = DevelopmentOutcomeStatusV1.ANSWER
    elif intent is DevelopmentTaskIntentKindV1.PLAN:
        explanation = "The requested plan completed and remains advisory."
        action = (
            "Select one bounded plan item and explicitly request IMPLEMENTATION if you want to "
            "prepare implementation scope."
        )
        status = DevelopmentOutcomeStatusV1.ANSWER
    elif intent is DevelopmentTaskIntentKindV1.UNKNOWN:
        explanation = "The request needs an explicit Development task intent."
        action = "Choose QUESTION, ANALYSIS, PLAN, or IMPLEMENTATION."
        status = DevelopmentOutcomeStatusV1.ANSWER
    else:
        explanation = "The implementation analysis is ready for bounded human review."
        action = "Review the advisory analysis and approve the handoff before implementation."
        status = DevelopmentOutcomeStatusV1.NEEDS_HUMAN_REVIEW
    return DevelopmentOutcomeV1(
        status,
        None,
        explanation,
        None,
        action,
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
            DevelopmentOutcomeStatusV1.UNSUPPORTED_CAPABILITY,
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
    if reason == "REVIEW_TARGET_SET_BUDGET_EXCEEDED":
        return DevelopmentOutcomeV1(
            DevelopmentOutcomeStatusV1.TASK_TOO_BROAD,
            reason,
            "The requested implementation is too broad for one safe development cycle. No files were changed.",
            None,
            "Split the request into smaller bounded scopes before preparing implementation.",
            False,
            request_id,
            failed_stage,
            reason,
            True,
            unknowns,
            "The unchanged safety bound rejected the oversized implementation scope.",
        )
    if reason == "AI_PROVIDER_TIMEOUT":
        return DevelopmentOutcomeV1(
            DevelopmentOutcomeStatusV1.TEMPORARY_FAILURE,
            reason,
            "The selected model did not respond in time. No project changes were made.",
            None,
            "Retry this request when the selected model is responsive.",
            True,
            request_id,
            failed_stage,
            reason,
            True,
            unknowns,
            "The bounded provider call exceeded its configured timeout.",
        )
    if reason in {"AI_UNAVAILABLE", "MODEL_ROLE_UNAVAILABLE"}:
        return DevelopmentOutcomeV1(
            DevelopmentOutcomeStatusV1.MODEL_UNAVAILABLE,
            reason,
            "The selected Development role or model is unavailable. No project changes were made.",
            None,
            "Retry when it is available or explicitly choose another available role.",
            True,
            request_id,
            failed_stage,
            reason,
            True,
            unknowns,
            "No silent model substitution or fallback is permitted.",
        )
    partial = analysis.validation_status == "PARTIAL"
    return DevelopmentOutcomeV1(
        DevelopmentOutcomeStatusV1.INTERNAL_FAILURE,
        reason,
        (
            "I could analyze part of the request, but could not safely validate the later "
            "implementation stage. No implementation was authorized."
            if partial
            else "Development analysis could not complete safely. No project changes were made."
        ),
        None,
        "Review the available bounded result and the separate technical diagnostic before retrying.",
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
