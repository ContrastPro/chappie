from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.development_analysis.models import (
    DevelopmentAnalysisReadinessV1,
    DevelopmentAnalysisStateV1,
    DevelopmentAnalysisV1,
    DevelopmentTaskIntentKindV1,
)
from app.development_analysis.outcome import (
    DevelopmentOutcomeStatusV1,
    deduplicate_development_unknowns,
    project_development_outcome,
)
from tools.acceptance.development_authorization_pair import persist_authorization_pair


def _analysis(**changes: object) -> DevelopmentAnalysisV1:
    base: dict[str, object] = {
        "state": DevelopmentAnalysisStateV1.OK,
        "request": "Analyze a bounded implementation task.",
        "validation_status": "VALID",
        "readiness": DevelopmentAnalysisReadinessV1.READY_FOR_TASK,
        "unknowns": ("A non-blocking implementation detail remains unknown.",),
    }
    base.update(changes)
    return DevelopmentAnalysisV1(**base)  # type: ignore[arg-type]


def _intent_analysis(intent: DevelopmentTaskIntentKindV1) -> DevelopmentAnalysisV1:
    evidence = SimpleNamespace(evidence_digest="e" * 64, repository_inventory_digest=None)
    context_map = SimpleNamespace(context_digest="c" * 64)
    task_intent = SimpleNamespace(
        intent=intent,
        requesting_evidence_digest=evidence.evidence_digest,
        requesting_context_digest=context_map.context_digest,
    )
    return _analysis(
        evidence=evidence,
        context_map=context_map,
        task_intent=task_intent,
        readiness=None,
        answer="Bounded response.",
    )


@pytest.mark.parametrize(
    "intent",
    (
        DevelopmentTaskIntentKindV1.QUESTION,
        DevelopmentTaskIntentKindV1.ANALYSIS,
        DevelopmentTaskIntentKindV1.PLAN,
    ),
)
def test_non_implementation_success_has_intent_appropriate_answer_outcome(intent) -> None:
    outcome = project_development_outcome(_intent_analysis(intent))
    assert outcome.status is DevelopmentOutcomeStatusV1.ANSWER
    assert "approval before implementation" not in outcome.suggested_next_action.casefold()
    if intent is DevelopmentTaskIntentKindV1.PLAN:
        assert "advisory" in outcome.explanation.casefold()
        assert "explicitly request implementation" in outcome.suggested_next_action.casefold()


def test_target_overflow_is_a_human_readable_task_too_broad_outcome() -> None:
    outcome = project_development_outcome(
        _analysis(
            state=DevelopmentAnalysisStateV1.ANALYSIS_INVALID,
            validation_status="PARTIAL",
            readiness=None,
            reason_code="REVIEW_TARGET_SET_BUDGET_EXCEEDED",
        )
    )
    assert outcome.status is DevelopmentOutcomeStatusV1.TASK_TOO_BROAD
    assert outcome.safe_to_retry is False
    assert "REVIEW_TARGET_SET_BUDGET_EXCEEDED" not in outcome.explanation


def test_sufficient_valid_implementation_requires_human_review() -> None:
    outcome = project_development_outcome(_analysis())
    assert outcome.status is DevelopmentOutcomeStatusV1.NEEDS_HUMAN_REVIEW
    assert outcome.safe_to_retry is False
    assert outcome.no_changes_made is True


@pytest.mark.parametrize(
    "unknown",
    (
        "The exact file cannot be determined from current evidence.",
        "The exact contract cannot be determined from current evidence.",
    ),
)
def test_non_blocking_context_unknown_does_not_automatically_block(unknown: str) -> None:
    assert (
        project_development_outcome(_analysis(unknowns=(unknown,))).status
        is DevelopmentOutcomeStatusV1.NEEDS_HUMAN_REVIEW
    )


@pytest.mark.parametrize("kind", ("FILE", "CONTRACT"))
def test_explicit_bounded_context_gap_needs_context(kind: str) -> None:
    outcome = project_development_outcome(
        _analysis(unknowns=(f"Missing context: {kind.lower()} app/example.py",))
    )
    assert outcome.status is DevelopmentOutcomeStatusV1.NEEDS_CONTEXT
    assert outcome.required_context == kind
    assert outcome.safe_to_retry is False


def test_repository_hygiene_unknowns_require_inventory_without_broad_discovery() -> None:
    outcome = project_development_outcome(
        _analysis(
            request="Review repository hygiene for safe cleanup candidates.",
            unknowns=(
                "Full repository file inventory unavailable.",
                "Complete file usage patterns unavailable.",
                "Insufficient evidence of unused/redundant files.",
                "Incomplete knowledge of non-import consumers.",
                "Uncertain legacy/diagnostic external consumers.",
                "Full repository file inventory unavailable.",
            ),
        )
    )
    assert outcome.status is DevelopmentOutcomeStatusV1.NEEDS_CONTEXT
    assert outcome.blocker_code == "REPOSITORY_INVENTORY_REQUIRED"
    assert outcome.required_context == "REPOSITORY_INVENTORY"
    assert outcome.safe_to_retry is False
    assert outcome.why_it_blocks == (
        "Safe cleanup classification cannot be proven from the current bounded context."
    )
    assert "Review repository hygiene" not in outcome.explanation
    assert outcome.unknowns == (
        "Full repository file inventory unavailable.",
        "Complete file usage patterns unavailable.",
        "Insufficient evidence of unused/redundant files.",
        "Incomplete knowledge of non-import consumers.",
        "Uncertain legacy/diagnostic external consumers.",
    )


def test_readiness_human_review_remains_distinct_from_context_and_system_failure() -> None:
    outcome = project_development_outcome(
        _analysis(readiness=DevelopmentAnalysisReadinessV1.NEEDS_REVIEW)
    )
    assert outcome.status is DevelopmentOutcomeStatusV1.NEEDS_HUMAN_REVIEW
    assert outcome.safe_to_retry is False


@pytest.mark.parametrize(
    ("state", "reason", "expected", "retry"),
    (
        (
            DevelopmentAnalysisStateV1.TIMEOUT,
            "AI_PROVIDER_TIMEOUT",
            DevelopmentOutcomeStatusV1.TEMPORARY_FAILURE,
            True,
        ),
        (
            DevelopmentAnalysisStateV1.MODEL_UNAVAILABLE,
            "AI_UNAVAILABLE",
            DevelopmentOutcomeStatusV1.MODEL_UNAVAILABLE,
            True,
        ),
        (
            DevelopmentAnalysisStateV1.ANALYSIS_INVALID,
            "DEVELOPMENT_SCHEMA_INVALID",
            DevelopmentOutcomeStatusV1.INTERNAL_FAILURE,
            False,
        ),
    ),
)
def test_typed_system_failures_remain_system_failures(
    state: DevelopmentAnalysisStateV1,
    reason: str,
    expected: DevelopmentOutcomeStatusV1,
    retry: bool,
) -> None:
    outcome = project_development_outcome(
        _analysis(state=state, validation_status="UNAVAILABLE", readiness=None, reason_code=reason)
    )
    assert outcome.status is expected
    assert outcome.reason_code == reason
    assert outcome.safe_to_retry is retry


def test_unsupported_capability_stays_distinct_from_context_and_system_failure() -> None:
    outcome = project_development_outcome(
        _analysis(
            state=DevelopmentAnalysisStateV1.ANALYSIS_INVALID,
            validation_status="UNAVAILABLE",
            readiness=None,
            reason_code="REPOSITORY_INVENTORY_UNSUPPORTED",
        )
    )
    assert outcome.status is DevelopmentOutcomeStatusV1.UNSUPPORTED_CAPABILITY
    assert outcome.safe_to_retry is False


def test_needs_context_cannot_persist_authorization_pair(tmp_path) -> None:
    analysis = _analysis(
        unknowns=(
            "Full repository file inventory unavailable.",
            "Complete file usage patterns unavailable.",
            "Incomplete knowledge of non-import consumers.",
            "Insufficient evidence of unused/redundant files.",
        ),
    )
    with pytest.raises(ValueError, match="not implementation-authorizable"):
        persist_authorization_pair(tmp_path / "authorization_pair.json", analysis, object())  # type: ignore[arg-type]


def test_unknown_deduplication_is_stable_and_preserves_distinct_values() -> None:
    assert deduplicate_development_unknowns(("One.", "Two.", "One.", "  Two.  ")) == (
        "One.",
        "Two.",
    )
