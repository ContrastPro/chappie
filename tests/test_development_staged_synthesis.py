"""Focused fail-closed contracts for staged Development Reasoning 2.0."""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.development_analysis.context import (
    DevelopmentContextMapV1,
    DevelopmentEvidenceHandleV1,
    build_context_digest,
)
from app.development_analysis.synthesis import (
    PROPOSAL_SCHEMA,
    PROPOSED_SCOPE_FIELDS,
    DevelopmentStageError,
    parse_development_grounding,
    parse_development_proposal,
    parse_development_review,
    proposal_provider_schema,
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


@pytest.mark.parametrize(
    ("tests", "criteria", "code"),
    (
        (["Run focused tests."], ["Diff review works."], "REVIEW_TESTS_NOT_SPECIFIC"),
        (
            ["Review DevelopmentAnalysisV1 regression."],
            ["The result is valid."],
            "REVIEW_CRITERIA_NOT_SPECIFIC",
        ),
    ),
)
def test_review_requires_task_specific_tests_and_criteria(tests, criteria, code) -> None:
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
            "Review DevelopmentAnalysisV1 diff review.",
        )
    assert error.value.code == code
