"""Focused fail-closed contracts for staged Development Reasoning 2.0."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.development_analysis.context import (
    DevelopmentContextMapV1,
    DevelopmentEvidenceHandleV1,
    build_context_digest,
)
from app.development_analysis.synthesis import (
    PROPOSAL_SCHEMA,
    PROPOSED_SCOPE_FIELDS,
    REVIEW_OBSERVABLE_CONDITIONS,
    REVIEW_VERIFICATION_ACTIONS,
    DevelopmentProposalV1,
    DevelopmentStageError,
    _grounding_instruction,
    _observable_condition_category,
    _review_instruction,
    parse_development_grounding,
    parse_development_proposal,
    parse_development_review,
    proposal_provider_schema,
    public_grounding_evidence_handles,
)


def _context_map() -> DevelopmentContextMapV1:
    value = DevelopmentContextMapV1(
        "a" * 40,
        "chappie_knowledge/1.0.0",
        "b" * 64,
        "c" * 64,
        "d" * 64,
        "CODE",
        "qwen3-coder:30b",
        (
            DevelopmentEvidenceHandleV1(
                "E1",
                "knowledge.development.statement",
                "development",
                "e" * 64,
                "MODULE_RESPONSIBILITY",
                "app.development_analysis owns DevelopmentAnalysisV1.",
            ),
        ),
        ("app.development_analysis",),
        ("DevelopmentAnalysisV1",),
        ("app/development_analysis/models.py",),
        ("tests/test_development_analysis.py",),
        ("Advisory only.",),
        "",
    )
    return replace(value, context_digest=build_context_digest(value))


def _scope(owner="app.development_analysis", path="app/development_analysis/diff_review.py"):
    return {
        "proposed_owner": owner,
        "proposed_modified_files": [],
        "proposed_new_files": [path],
        "proposed_contract_changes": ["Add DevelopmentDiffReviewV1."],
        "proposed_components": ["Bounded diff review"],
        "proposed_behavior": ["Remain advisory."],
        "rationale": ["The diff review is future scope."],
    }


def test_grounding_accepts_only_supplied_evidence_and_unknowns() -> None:
    result = parse_development_grounding(
        {
            "relevant_evidence_handles": ["E1"],
            "task_interpretation": "Review DevelopmentAnalysisV1.",
            "unknowns": ["Exact future endpoint is unknown."],
        },
        _context_map(),
    )
    assert result.relevant_evidence_handles == ("E1",)
    with pytest.raises(DevelopmentStageError) as error:
        parse_development_grounding(
            {
                "relevant_evidence_handles": ["E9"],
                "task_interpretation": "Review.",
                "unknowns": ["Unknown."],
            },
            _context_map(),
        )
    assert error.value.code == "GROUNDING_UNSUPPORTED_EVIDENCE"


def _inventory_attachment(*handles: str):
    return SimpleNamespace(
        evidence_groups=tuple(SimpleNamespace(handle=handle) for handle in handles)
    )


@pytest.mark.parametrize("handles", (("RI1",), ("RI5",), ("E1", "RI1")))
def test_grounding_accepts_exact_public_inventory_handles(handles) -> None:
    attachment = _inventory_attachment("RI1", "RI2", "RI3", "RI4", "RI5")
    result = parse_development_grounding(
        {
            "relevant_evidence_handles": list(handles),
            "task_interpretation": "Review repository evidence.",
            "unknowns": [],
        },
        _context_map(),
        attachment,
    )
    assert result.relevant_evidence_handles == handles


@pytest.mark.parametrize(
    ("handle", "category"),
    (
        ("RI6", "UNKNOWN_HANDLE"),
        ("E9", "UNKNOWN_HANDLE"),
        ("knowledge.development.statement", "INTERNAL_IDENTIFIER"),
        ("app/development_analysis/models.py", "INTERNAL_IDENTIFIER"),
        ("E1+RI1", "MALFORMED_HANDLE"),
    ),
)
def test_grounding_rejects_handles_outside_exact_public_set(handle, category) -> None:
    with pytest.raises(DevelopmentStageError) as error:
        parse_development_grounding(
            {
                "relevant_evidence_handles": [handle],
                "task_interpretation": "Review.",
                "unknowns": [],
            },
            _context_map(),
            _inventory_attachment("RI1", "RI2", "RI3", "RI4", "RI5"),
        )
    assert error.value.code == "GROUNDING_UNSUPPORTED_EVIDENCE"
    diagnostics = error.value.grounding_diagnostics
    assert diagnostics is not None
    assert diagnostics.bounded_error_path == "$.relevant_evidence_handles[0]"
    assert diagnostics.bounded_error_category == category
    assert handle not in str(diagnostics.payload())


def test_grounding_prompt_and_parser_share_deterministic_public_handles() -> None:
    attachment = _inventory_attachment("RI1", "RI2", "RI3", "RI4", "RI5")
    handles = public_grounding_evidence_handles(_context_map(), attachment)
    assert handles == ("E1", "RI1", "RI2", "RI3", "RI4", "RI5")
    instruction = _grounding_instruction(handles)
    assert "E1, RI1, RI2, RI3, RI4, RI5" in instruction
    for handle in handles:
        result = parse_development_grounding(
            {
                "relevant_evidence_handles": [handle],
                "task_interpretation": "Review.",
                "unknowns": [],
            },
            _context_map(),
            attachment,
        )
        assert result.relevant_evidence_handles == (handle,)


def test_non_inventory_grounding_does_not_accept_inventory_handles() -> None:
    assert public_grounding_evidence_handles(_context_map()) == ("E1",)
    with pytest.raises(DevelopmentStageError) as error:
        parse_development_grounding(
            {
                "relevant_evidence_handles": ["RI1"],
                "task_interpretation": "Review.",
                "unknowns": [],
            },
            _context_map(),
        )
    assert error.value.grounding_diagnostics.bounded_error_category == "UNKNOWN_HANDLE"


@pytest.mark.parametrize(
    ("owner", "path", "code"),
    (
        ("UNKNOWN", "app/development_analysis/diff_review.py", "PROPOSAL_UNKNOWN_OWNER"),
        ("app.development_analysis", "../escape.py", "PROPOSAL_UNSAFE_PATH"),
    ),
)
def test_proposal_rejects_unknown_owner_and_unsafe_path(owner, path, code) -> None:
    with pytest.raises(DevelopmentStageError) as error:
        parse_development_proposal(
            {"proposed_scope": _scope(owner, path), "proposal_unknowns": ["Unknown."]},
            _context_map(),
        )
    assert error.value.code == code


def test_proposed_new_file_is_not_treated_as_current_evidence() -> None:
    result = parse_development_proposal(
        {"proposed_scope": _scope(), "proposal_unknowns": ["Exact implementation is unknown."]},
        _context_map(),
    )
    assert result.proposed_scope.proposed_new_files == ("app/development_analysis/diff_review.py",)


def test_provider_proposal_schema_exactly_matches_the_strict_parser_contract() -> None:
    assert PROPOSAL_SCHEMA["additionalProperties"] is False
    assert set(PROPOSAL_SCHEMA["required"]) == {"proposed_scope", "proposal_unknowns"}
    assert set(PROPOSAL_SCHEMA["properties"]) == {"proposed_scope", "proposal_unknowns"}
    assert PROPOSAL_SCHEMA["properties"]["proposal_unknowns"] == {
        "type": "array",
        "items": {"type": "string"},
    }

    scope = PROPOSAL_SCHEMA["properties"]["proposed_scope"]
    assert scope["type"] == "object"
    assert scope["additionalProperties"] is False
    assert tuple(scope["required"]) == PROPOSED_SCOPE_FIELDS
    assert set(scope["properties"]) == set(PROPOSED_SCOPE_FIELDS)
    assert scope["properties"]["proposed_owner"] == {"type": ["string", "null"]}
    for field in PROPOSED_SCOPE_FIELDS[1:]:
        assert scope["properties"][field] == {"type": "array", "items": {"type": "string"}}

    assert {"risks", "tests", "acceptance_criteria", "non_goals"}.isdisjoint(scope["properties"])


def test_provider_schema_binds_proposed_owner_to_request_local_candidates() -> None:
    schema = proposal_provider_schema(("app.development_analysis", "app.example"))
    owner = schema["properties"]["proposed_scope"]["properties"]["proposed_owner"]
    assert owner == {
        "type": ["string", "null"],
        "enum": ["app.development_analysis", "app.example", None],
    }
    assert proposal_provider_schema(())["properties"]["proposed_scope"]["properties"][
        "proposed_owner"
    ]["enum"] == [None]


def test_dogfood_alternative_proposal_shape_remains_fail_closed() -> None:
    with pytest.raises(DevelopmentStageError) as error:
        parse_development_proposal(
            {
                "proposed_scope": {
                    "description": "Alternative scope.",
                    "ownership": "MODULE_RESPONSIBILITY",
                    "contracts": [],
                    "boundaries": [],
                    "risks": [],
                    "tests": [],
                    "acceptance_criteria": [],
                    "non_goals": [],
                },
                "proposal_unknowns": [],
            },
            _context_map(),
        )
    assert error.value.code == "PROPOSAL_SCHEMA_INVALID"
    assert error.value.proposal_diagnostics is not None
    assert (
        error.value.proposal_diagnostics.bounded_error_path == "$.proposed_scope.proposed_behavior"
    )
    assert error.value.proposal_diagnostics.bounded_error_category == "MISSING_REQUIRED_FIELD"


@pytest.mark.parametrize(
    "owner", ("INVENTED_OWNER", "Development Intelligence", "MODULE_RESPONSIBILITY")
)
def test_proposal_rejects_noncanonical_owner_representations(owner: str) -> None:
    with pytest.raises(DevelopmentStageError) as error:
        parse_development_proposal(
            {"proposed_scope": _scope(owner), "proposal_unknowns": ["Owner is unresolved."]},
            _context_map(),
        )
    assert error.value.code == "PROPOSAL_UNKNOWN_OWNER"


def test_null_owner_requires_an_explicit_proposal_unknown() -> None:
    accepted = parse_development_proposal(
        {"proposed_scope": _scope(None), "proposal_unknowns": ["Owner is unresolved."]},
        _context_map(),
    )
    assert accepted.proposed_scope.proposed_owner is None
    with pytest.raises(DevelopmentStageError) as error:
        parse_development_proposal(
            {"proposed_scope": _scope(None), "proposal_unknowns": []}, _context_map()
        )
    assert error.value.code == "PROPOSAL_SCHEMA_INVALID"
    assert error.value.proposal_diagnostics is not None
    assert error.value.proposal_diagnostics.bounded_error_path == "$.proposal_unknowns"
    assert error.value.proposal_diagnostics.bounded_error_category == "REQUIRED_UNKNOWN_MISSING"


@pytest.mark.parametrize(
    ("field", "index", "phrase"),
    (
        ("proposed_behavior", 1, "Use shell to apply the reviewed change."),
        ("rationale", 1, "Create a git commit after review."),
    ),
)
def test_proposal_authority_violations_preserve_typed_reason_and_safe_location(
    field: str, index: int, phrase: str
) -> None:
    scope = _scope()
    scope[field] = ["Remain advisory.", phrase]
    with pytest.raises(DevelopmentStageError) as error:
        parse_development_proposal(
            {"proposed_scope": scope, "proposal_unknowns": ["No ownership unknown."]},
            _context_map(),
        )
    assert error.value.code == "PROPOSAL_AUTHORITY_VIOLATION"
    diagnostics = error.value.proposal_diagnostics
    assert diagnostics is not None
    assert diagnostics.bounded_error_path == f"$.proposed_scope.{field}[{index}]"
    assert diagnostics.bounded_error_category == "AUTHORITY_VIOLATION"
    payload = diagnostics.payload()
    assert phrase not in str(payload)
    assert "shell" not in str(payload).casefold()
    assert "git commit" not in str(payload).casefold()


@pytest.mark.parametrize(
    ("value", "path", "category"),
    (
        ({"proposal_unknowns": []}, "$.proposed_scope", "MISSING_REQUIRED_FIELD"),
        (
            {"proposed_scope": _scope(), "proposal_unknowns": [], "extra": True},
            "$.extra",
            "UNEXPECTED_FIELD",
        ),
        ({"proposed_scope": None, "proposal_unknowns": []}, "$.proposed_scope", "INVALID_NULL"),
        (
            {"proposed_scope": _scope(), "proposal_unknowns": None},
            "$.proposal_unknowns",
            "INVALID_NULL",
        ),
        (
            {"proposed_scope": {**_scope(), "rationale": "not an array"}, "proposal_unknowns": []},
            "$.proposed_scope.rationale",
            "WRONG_FIELD_TYPE",
        ),
    ),
)
def test_proposal_schema_failures_expose_only_bounded_structure(value, path, category) -> None:
    with pytest.raises(DevelopmentStageError) as error:
        parse_development_proposal(value, _context_map(), response_length=123)
    diagnostics = error.value.proposal_diagnostics
    assert diagnostics is not None
    assert diagnostics.parser_stage == "PROPOSAL_PARSE"
    assert diagnostics.response_length == 123
    assert diagnostics.bounded_error_path == path
    assert diagnostics.bounded_error_category == category
    assert "statement" not in str(diagnostics.payload())


def _review_proposal(*, analysis_only: bool = False) -> DevelopmentProposalV1:
    scope = _scope()
    if analysis_only:
        scope.update(
            {
                "proposed_modified_files": [],
                "proposed_new_files": [],
                "proposed_contract_changes": [],
                "proposed_components": [],
                "proposed_behavior": ["Classify cleanup candidates from bounded evidence."],
                "rationale": ["Candidate evidence must be verified before removal."],
            }
        )
    return parse_development_proposal(
        {"proposed_scope": scope, "proposal_unknowns": []}, _context_map()
    )


@pytest.mark.parametrize(
    ("tests", "criteria", "code"),
    (
        (["Run tests."], ["DevelopmentDiffReviewV1 remains valid."], "REVIEW_TESTS_NOT_SPECIFIC"),
        (
            ["Verify RT1 rejects invalid input."],
            ["The result remains valid."],
            "REVIEW_CRITERIA_NOT_SPECIFIC",
        ),
    ),
)
def test_review_requires_proposal_specific_tests_and_criteria(tests, criteria, code) -> None:
    with pytest.raises(DevelopmentStageError) as error:
        parse_development_review(
            {
                "risks": ["Scope drift."],
                "required_tests": tests,
                "non_goals": ["No mutation."],
                "acceptance_criteria": criteria,
                "unresolved_unknowns": ["Unknown."],
                "confidence": 0.5,
            },
            _review_proposal(),
        )
    assert error.value.code == code


def _review_payload(test: str, criterion: str) -> dict[str, object]:
    return {
        "risks": ["Scope drift."],
        "required_tests": [test],
        "non_goals": ["No mutation."],
        "acceptance_criteria": [criterion],
        "unresolved_unknowns": [],
        "confidence": 0.5,
    }


def test_implementation_review_accepts_exact_target_and_condition_without_request_tokens() -> None:
    result = parse_development_review(
        _review_payload(
            "Verify RT1 rejects invalid input.",
            "RT1 remains advisory.",
        ),
        _review_proposal(),
    )
    assert result.confidence == 0.5


@pytest.mark.parametrize("action", REVIEW_VERIFICATION_ACTIONS)
def test_shared_review_actions_require_and_accept_target_and_condition(action) -> None:
    result = parse_development_review(
        _review_payload(
            f"{action.title()} RT1 rejects invalid input.",
            "RT1 remains advisory.",
        ),
        _review_proposal(),
    )
    assert result.required_tests[0].casefold().startswith(action)


@pytest.mark.parametrize(
    ("test", "category", "recognized"),
    (
        (
            "Verify app/unrelated.py rejects invalid input.",
            "TARGET_NOT_RECOGNIZED",
            (True, False, True),
        ),
        (
            "Verify RT1.",
            "MISSING_VERIFICATION_CONDITION",
            (True, True, False),
        ),
        ("Review repository hygiene tests.", "GENERIC_VERIFICATION", (False, False, False)),
    ),
)
def test_implementation_review_rejects_unrelated_missing_or_lexical_only_items(
    test, category, recognized
) -> None:
    with pytest.raises(DevelopmentStageError) as error:
        parse_development_review(
            _review_payload(test, "DevelopmentDiffReviewV1 remains advisory."),
            _review_proposal(),
        )
    diagnostics = error.value.review_diagnostics
    assert diagnostics is not None
    assert diagnostics.bounded_error_path == "$.required_tests[0]"
    assert diagnostics.bounded_error_category == category
    assert (
        diagnostics.action_recognized,
        diagnostics.target_recognized,
        diagnostics.condition_recognized,
    ) == recognized
    assert test not in str(diagnostics.payload())


@pytest.mark.parametrize(
    "test",
    (
        "Verify RT1 and fail if consumers remain.",
        "Confirm RT1 references are present before removal.",
    ),
)
def test_analysis_only_review_accepts_candidate_specific_verification(test) -> None:
    result = parse_development_review(
        _review_payload(test, "RT1 remains evidence-bound before removal."),
        _review_proposal(analysis_only=True),
    )
    assert result.required_tests == (test,)


@pytest.mark.parametrize(
    "test",
    (
        "Inspect RT1 consumers and fail if consumers remain.",
        "Compare RT1 references against the expected absence state.",
        "Ensure RT1 references are absent before removal.",
    ),
)
def test_analysis_only_observable_action_vocabulary(test) -> None:
    result = parse_development_review(
        _review_payload(test, "RT1 remains evidence-bound before removal."),
        _review_proposal(analysis_only=True),
    )
    assert result.required_tests == (test,)


@pytest.mark.parametrize("test", ("Validate repository.", "Check everything."))
def test_analysis_only_review_rejects_generic_verification(test) -> None:
    with pytest.raises(DevelopmentStageError) as error:
        parse_development_review(
            _review_payload(test, "Cleanup candidates remain evidence-bound before removal."),
            _review_proposal(analysis_only=True),
        )
    assert error.value.review_diagnostics.bounded_error_category in {
        "TARGET_NOT_RECOGNIZED",
        "MISSING_VERIFICATION_CONDITION",
    }


@pytest.mark.parametrize(
    "test",
    (
        "Review repository.",
        "Consider cleanup candidate consumers remain.",
        "Look at cleanup candidate references remain.",
        "Ensure quality.",
        "Ensure correctness.",
        "Run tests.",
    ),
)
def test_vague_or_unsupported_actions_remain_rejected(test) -> None:
    with pytest.raises(DevelopmentStageError):
        parse_development_review(
            _review_payload(test, "Cleanup candidates remain evidence-bound before removal."),
            _review_proposal(analysis_only=True),
        )


def test_review_instruction_matches_proposal_aware_specificity_rules() -> None:
    concrete = _review_instruction(_review_proposal())
    assert "Allowed Proposal targets" in concrete and "RT1 | FILE" in concrete
    assert "exact action from" in concrete and "result or failure condition" in concrete
    analysis_only = _review_instruction(_review_proposal(analysis_only=True))
    assert "RT1 | BEHAVIOR" in analysis_only and "RT2 | RATIONALE" in analysis_only
    assert "repeat" not in analysis_only.casefold()
    vocabulary = ", ".join(REVIEW_VERIFICATION_ACTIONS)
    assert vocabulary in concrete and vocabulary in analysis_only
    assert set(REVIEW_VERIFICATION_ACTIONS) == {
        "assert",
        "check",
        "compare",
        "confirm",
        "ensure",
        "inspect",
        "test",
        "validate",
        "verify",
    }
    for definition in REVIEW_OBSERVABLE_CONDITIONS:
        assert definition.category in concrete and definition.category in analysis_only


@pytest.mark.parametrize(
    ("condition", "category"),
    (
        ("is present", "PRESENCE"),
        ("is absent", "ABSENCE"),
        ("equals expected state", "EQUALITY"),
        ("differs from declared state", "DIFFERENCE"),
        ("passes", "SUCCESS"),
        ("succeeds", "SUCCESS"),
        ("fails", "FAILURE"),
        ("errors", "FAILURE"),
        ("reference remains", "REFERENCE_REMAINS"),
        ("consumer exists", "REFERENCE_REMAINS"),
        ("reference removed", "REFERENCE_REMOVED"),
        ("consumer missing", "REFERENCE_REMOVED"),
    ),
)
def test_shared_observable_condition_categories(condition, category) -> None:
    result = parse_development_review(
        _review_payload(
            f"Verify RT1 {condition}.",
            "RT1 remains advisory.",
        ),
        _review_proposal(),
    )
    assert result.required_tests
    assert _observable_condition_category(frozenset(condition.casefold().split())) == category


@pytest.mark.parametrize(
    "condition",
    ("looks good", "works correctly", "seems fine", "quality is acceptable", "everything is okay"),
)
def test_vague_observable_conditions_remain_rejected(condition) -> None:
    with pytest.raises(DevelopmentStageError) as error:
        parse_development_review(
            _review_payload(
                f"Verify RT1 {condition}.",
                "RT1 remains advisory.",
            ),
            _review_proposal(),
        )
    diagnostics = error.value.review_diagnostics
    assert diagnostics.bounded_error_category == "MISSING_VERIFICATION_CONDITION"
    assert diagnostics.condition_category is None
    assert condition not in str(diagnostics.payload())


@pytest.mark.parametrize("subject", ("reference", "consumer"))
def test_bare_reference_subject_is_not_an_observable_condition(subject) -> None:
    with pytest.raises(DevelopmentStageError) as error:
        parse_development_review(
            _review_payload(
                f"Inspect RT1 {subject}.",
                "RT1 remains advisory.",
            ),
            _review_proposal(),
        )
    assert error.value.review_diagnostics.condition_recognized is False
