from __future__ import annotations

import pytest

from app.development_analysis.models import (
    DevelopmentAnalysisReadinessV1,
    DevelopmentAnalysisStateV1,
    DevelopmentAnalysisV1,
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


def test_sufficient_valid_analysis_projects_completed() -> None:
    outcome = project_development_outcome(_analysis())
    assert outcome.status is DevelopmentOutcomeStatusV1.COMPLETED
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
        is DevelopmentOutcomeStatusV1.COMPLETED
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
    ("state", "reason", "retry"),
    (
        (DevelopmentAnalysisStateV1.TIMEOUT, "AI_PROVIDER_TIMEOUT", True),
        (DevelopmentAnalysisStateV1.MODEL_UNAVAILABLE, "AI_UNAVAILABLE", True),
        (DevelopmentAnalysisStateV1.ANALYSIS_INVALID, "DEVELOPMENT_SCHEMA_INVALID", False),
    ),
)
def test_typed_system_failures_remain_system_failures(
    state: DevelopmentAnalysisStateV1, reason: str, retry: bool
) -> None:
    outcome = project_development_outcome(
        _analysis(state=state, validation_status="UNAVAILABLE", readiness=None, reason_code=reason)
    )
    assert outcome.status is DevelopmentOutcomeStatusV1.SYSTEM_FAILURE
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
    assert outcome.status is DevelopmentOutcomeStatusV1.UNSUPPORTED_WITH_CURRENT_CAPABILITIES
    assert outcome.safe_to_retry is False


def test_needs_context_cannot_persist_authorization_pair(tmp_path) -> None:
    analysis = _analysis(
        unknowns=("Full repository file inventory unavailable.",),
    )
    with pytest.raises(ValueError, match="not implementation-authorizable"):
        persist_authorization_pair(tmp_path / "authorization_pair.json", analysis, object())  # type: ignore[arg-type]


def test_unknown_deduplication_is_stable_and_preserves_distinct_values() -> None:
    assert deduplicate_development_unknowns(("One.", "Two.", "One.", "  Two.  ")) == (
        "One.",
        "Two.",
    )
