"""Exact public Proposal target projection for Development Review."""

from dataclasses import replace

import pytest

from app.ai.schemas import digest
from app.development_analysis.models import DevelopmentProposedScopeV1
from app.development_analysis.review_targets import (
    REVIEW_PROPOSAL_TARGETS_IDENTITY,
    ReviewProposalTargetKindV1,
    ReviewProposalTargetProjectionError,
    build_review_proposal_target_set,
)
from app.development_analysis.synthesis import (
    DEVELOPMENT_PROPOSAL_IDENTITY,
    DevelopmentProposalV1,
    DevelopmentStageError,
    _review_instruction,
    parse_development_review,
)


def _proposal(**changes) -> DevelopmentProposalV1:
    scope = DevelopmentProposedScopeV1(
        "app.development_analysis",
        ("app/development_analysis/models.py",),
        ("app/development_analysis/review_targets.py",),
        ("chappie_review_proposal_targets/1.0.0",),
        ("Review Proposal target projection",),
        ("Expose exact request-local Review targets.",),
        ("Review must verify the selected target before acceptance.",),
    )
    return DevelopmentProposalV1(replace(scope, **changes), ())


def _targets(proposal: DevelopmentProposalV1):
    return build_review_proposal_target_set(
        proposal, proposal_identity=DEVELOPMENT_PROPOSAL_IDENTITY
    )


def _review(test: str, criterion: str = "RT1 remains present.") -> dict[str, object]:
    return {
        "risks": ["Scope drift."],
        "required_tests": [test],
        "non_goals": ["No mutation."],
        "acceptance_criteria": [criterion],
        "unresolved_unknowns": [],
        "confidence": 0.8,
    }


def test_projects_exact_union_in_source_order() -> None:
    target_set = _targets(_proposal())
    assert target_set.identity == REVIEW_PROPOSAL_TARGETS_IDENTITY
    assert tuple(target.public_id for target in target_set.targets) == tuple(
        f"RT{index}" for index in range(1, 7)
    )
    assert tuple(target.kind for target in target_set.targets) == (
        ReviewProposalTargetKindV1.FILE,
        ReviewProposalTargetKindV1.FILE,
        ReviewProposalTargetKindV1.CONTRACT,
        ReviewProposalTargetKindV1.COMPONENT,
        ReviewProposalTargetKindV1.BEHAVIOR,
        ReviewProposalTargetKindV1.RATIONALE,
    )
    assert target_set.targets[0].exact_identifier == "app/development_analysis/models.py"
    assert all(target.state == "PROPOSED" for target in target_set.targets)


def test_analysis_only_hygiene_shape_projects_behavior_and_rationale() -> None:
    proposal = _proposal(
        proposed_modified_files=(),
        proposed_new_files=(),
        proposed_contract_changes=(),
        proposed_components=(),
        proposed_behavior=(
            "Classify selected repository cleanup candidates using bounded evidence.",
        ),
        rationale=("Candidate references must be verified before removal.",),
    )
    target_set = _targets(proposal)
    assert [(item.public_id, item.kind.value) for item in target_set.targets] == [
        ("RT1", "BEHAVIOR"),
        ("RT2", "RATIONALE"),
    ]
    result = parse_development_review(
        _review("Verify RT1 and fail if direct consumers remain."), proposal, target_set
    )
    assert result.required_tests


def test_digest_is_deterministic_and_bound_to_proposal_semantics() -> None:
    proposal = _proposal()
    first = _targets(proposal)
    second = _targets(proposal)
    changed = _targets(
        _proposal(proposed_behavior=("Expose a changed exact request-local target.",))
    )
    assert first == second
    assert first.proposal_digest == digest(proposal.payload())
    assert first.target_set_digest == second.target_set_digest
    assert changed.proposal_digest != first.proposal_digest
    assert changed.target_set_digest != first.target_set_digest


def test_prompt_and_parser_share_exact_target_set() -> None:
    proposal = _proposal()
    target_set = _targets(proposal)
    instruction = _review_instruction(proposal, target_set)
    for target in target_set.targets:
        assert f"{target.public_id} | {target.kind.value} |" in instruction
    assert parse_development_review(
        _review("Verify RT1 rejects invalid input."), proposal, target_set
    )


@pytest.mark.parametrize(
    ("item", "category"),
    (
        ("Verify RT99 rejects invalid input.", "TARGET_NOT_RECOGNIZED"),
        ("Verify rt1 rejects invalid input.", "TARGET_NOT_RECOGNIZED"),
        ("Verify RT1 and RT2 reject invalid input.", "MULTIPLE_TARGETS"),
        (
            "Verify app/development_analysis/models.py rejects invalid input.",
            "TARGET_NOT_RECOGNIZED",
        ),
        ("Verify model target rejects invalid input.", "TARGET_NOT_RECOGNIZED"),
        ("Review RT1 rejects invalid input.", "GENERIC_VERIFICATION"),
        ("RT1 rejects invalid input.", "MISSING_VERIFICATION_ACTION"),
        ("Verify RT1 carefully.", "MISSING_VERIFICATION_CONDITION"),
    ),
)
def test_exact_handle_action_and_condition_fail_closed(item: str, category: str) -> None:
    proposal = _proposal()
    with pytest.raises(DevelopmentStageError) as error:
        parse_development_review(_review(item), proposal, _targets(proposal))
    diagnostics = error.value.review_diagnostics
    assert diagnostics.bounded_error_category == category
    assert item not in str(diagnostics.payload())


def test_empty_target_set_fails_explicitly() -> None:
    proposal = _proposal(
        proposed_modified_files=(),
        proposed_new_files=(),
        proposed_contract_changes=(),
        proposed_components=(),
        proposed_behavior=(),
        rationale=(),
    )
    with pytest.raises(DevelopmentStageError) as error:
        parse_development_review(_review("Verify RT1 fails."), proposal, _targets(proposal))
    assert error.value.review_diagnostics.bounded_error_category == "EMPTY_TARGET_SET"
    assert error.value.review_diagnostics.target_count == 0


def test_inventory_identifiers_do_not_become_targets() -> None:
    target_set = _targets(_proposal())
    assert not any(target.public_id.startswith("RI") for target in target_set.targets)
    assert "RI1" not in {target.exact_identifier for target in target_set.targets}


def test_duplicate_identifier_with_distinct_provenance_fails_closed() -> None:
    proposal = _proposal(
        proposed_new_files=("app/development_analysis/models.py",),
    )
    with pytest.raises(ReviewProposalTargetProjectionError) as error:
        _targets(proposal)
    assert error.value.code == "REVIEW_TARGET_SET_INVALID"


def test_per_field_and_total_target_budgets_fail_closed() -> None:
    with pytest.raises(ReviewProposalTargetProjectionError) as error:
        _targets(
            _proposal(
                proposed_modified_files=tuple(f"app/file_{index}.py" for index in range(17)),
                proposed_new_files=(),
            )
        )
    assert error.value.code == "REVIEW_TARGET_SET_BUDGET_EXCEEDED"

    with pytest.raises(ReviewProposalTargetProjectionError) as error:
        _targets(
            _proposal(
                proposed_modified_files=tuple(f"app/a_{index}.py" for index in range(16)),
                proposed_new_files=tuple(f"app/b_{index}.py" for index in range(16)),
                proposed_contract_changes=("one extra target",),
                proposed_components=(),
                proposed_behavior=(),
                rationale=(),
            )
        )
    assert error.value.code == "REVIEW_TARGET_SET_BUDGET_EXCEEDED"


def test_identifier_and_serialized_budgets_fail_closed(monkeypatch) -> None:
    with pytest.raises(ReviewProposalTargetProjectionError) as error:
        _targets(_proposal(proposed_behavior=("x" * 257,)))
    assert error.value.code == "REVIEW_TARGET_SET_BUDGET_EXCEEDED"

    monkeypatch.setattr(
        "app.development_analysis.review_targets.MAX_REVIEW_TARGET_SET_SERIALIZED_CHARS", 10
    )
    with pytest.raises(ReviewProposalTargetProjectionError) as error:
        _targets(_proposal())
    assert error.value.code == "REVIEW_TARGET_SET_BUDGET_EXCEEDED"


def test_display_summary_is_bounded_and_not_in_semantic_digest() -> None:
    proposal = _proposal(proposed_behavior=("x" * 200,))
    target_set = _targets(proposal)
    behavior = next(item for item in target_set.targets if item.kind.value == "BEHAVIOR")
    assert len(behavior.display_summary) == 160
    assert "display_summary" not in behavior.semantic_payload()
