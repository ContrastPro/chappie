from __future__ import annotations

from app.ai.model_policy import DevelopmentModelRoleV1
from app.development_analysis.context import DevelopmentContextMapV1, DevelopmentEvidenceHandleV1
from app.development_analysis.implementation_context import (
    build_implementation_context_from_development,
    compare_development_projection,
)
from app.development_analysis.models import (
    DevelopmentAnalysisReadinessV1,
    DevelopmentAnalysisStateV1,
    DevelopmentAnalysisV1,
    DevelopmentEvidencePackV1,
    DevelopmentProposedScopeV1,
)
from app.development_analysis.task import CodexTaskStateV1, CodexTaskV1
from app.implementation_context import (
    ImplementationContextStateV1,
    ImplementationContextSufficiencyStateV1,
    assess_implementation_context_sufficiency,
)

COMMIT = "a" * 40
DIGEST = "b" * 64


def _context_map() -> DevelopmentContextMapV1:
    handles = (
        DevelopmentEvidenceHandleV1(
            "E1",
            "knowledge.development.analysis.statement",
            "knowledge-development",
            DIGEST,
            "CONTRACT",
            "POST /v1/development/analyze exposes DevelopmentAnalysisV1.",
        ),
        DevelopmentEvidenceHandleV1(
            "E2",
            "knowledge.development.task.statement",
            "knowledge-task",
            DIGEST,
            "CONTRACT",
            "POST /v1/development/task produces CodexHandoffV1.",
        ),
    )
    return DevelopmentContextMapV1(
        COMMIT,
        "chappie_knowledge/1.0.0",
        DIGEST,
        DIGEST,
        DIGEST,
        "CODE",
        "qwen3-coder:30b",
        handles,
        ("app.development_analysis",),
        ("DevelopmentAnalysisV1", "CodexHandoffV1"),
        ("app/development_analysis/models.py", "app/development_analysis/task.py"),
        ("tests/test_codex_task.py",),
        ("No file, shell, Git, Knowledge, trading, Risk, or execution authority.",),
        DIGEST,
    )


def _analysis(*, diff_review: bool = False, specific_test: bool = True) -> DevelopmentAnalysisV1:
    context_map = _context_map()
    evidence = DevelopmentEvidencePackV1(
        COMMIT,
        True,
        context_map.knowledge_identity,
        DIGEST,
        DIGEST,
        None,
        DevelopmentModelRoleV1.CODE,
        "qwen3-coder:30b",
        "qwen3-coder:30b",
        "ADVANCED",
        "LOCAL",
        ("knowledge-development",),
        (DIGEST,),
        DIGEST,
        ("knowledge.development.analysis.statement",),
    )
    required_tests = (
        (
            (
                "tests/test_codex_task.py::test_valid_analysis_prepares_deterministic_advisory_task "
                "| preserves advisory projection | handoff is advisory | invalid source is rejected"
            ),
        )
        if specific_test
        else ("Verify advisory handoff behavior.",)
    )
    return DevelopmentAnalysisV1(
        DevelopmentAnalysisStateV1.OK,
        "Prepare a read-only bounded Development implementation context.",
        evidence,
        facts=(
            "POST /v1/development/analyze exposes DevelopmentAnalysisV1.",
            "POST /v1/development/task produces CodexHandoffV1.",
        ),
        unknowns=("Exact adapter signature remains unknown.",),
        affected_contracts=("DevelopmentAnalysisV1", "CodexHandoffV1")
        + ((_DIFF_REVIEW_NAME,) if diff_review else ()),
        likely_files=("app/development_analysis/models.py",),
        risks=("Risk of contract drift.",),
        required_tests=required_tests,
        acceptance_criteria=("Human approval remains required.",),
        validation_status="VALID",
        readiness=DevelopmentAnalysisReadinessV1.READY_FOR_TASK,
        context_map=context_map,
        current_evidence_handles=("E1", "E2"),
        proposed_scope=DevelopmentProposedScopeV1(
            "app.development_analysis",
            ("app/development_analysis/models.py",),
            (("tests/test_development_diff_review.py",) if diff_review else ()),
            ((_DIFF_REVIEW_NAME,) if diff_review else ("DevelopmentAnalysisV1",)),
            ("implementation context projection",),
            ("preserve bounded provenance",),
            ("bounded current/proposed split",),
        ),
    )


_DIFF_REVIEW_NAME = "DevelopmentDiffReviewV1"


def _handoff(analysis: DevelopmentAnalysisV1) -> CodexTaskV1:
    return CodexTaskV1(
        "c" * 64,
        CodexTaskStateV1.ADVISORY,
        "chappie_development_analysis/2.0.0",
        COMMIT,
        DIGEST,
        analysis.context_map.context_digest if analysis.context_map else None,
        "CODE",
        analysis.request,
        approval_status="HUMAN_APPROVAL_REQUIRED",
        proposed_owner="app.development_analysis",
    )


def test_successful_handoff_projects_a_deterministic_consumer_neutral_context() -> None:
    analysis = _analysis()
    context = build_implementation_context_from_development(
        _context_map(), analysis, _handoff(analysis)
    )
    repeat = build_implementation_context_from_development(
        _context_map(), analysis, _handoff(analysis)
    )
    assert context.digest == repeat.digest
    assert context.canonical_owner == "app.development_analysis"
    assert context.source_commit == COMMIT
    assert context.evidence_digest == DIGEST
    assert {item.state for item in context.current_items} == {ImplementationContextStateV1.CURRENT}
    assert any(
        item.state is ImplementationContextStateV1.PROPOSED for item in context.proposed_items
    )
    assert any(item.state is ImplementationContextStateV1.UNKNOWN for item in context.unknown_items)
    assert "DevelopmentAnalysisV1" in {item.qualified_identifier for item in context.symbols}
    assert "CodexHandoffV1" in {item.qualified_identifier for item in context.symbols}
    assert any(item.relation.value == "OWNS" for item in context.relations)
    assert (
        assess_implementation_context_sufficiency(context).state
        is not ImplementationContextSufficiencyStateV1.INSUFFICIENT
    )


def test_planned_diff_review_remains_proposed_and_requires_targeted_expansion() -> None:
    analysis = _analysis(diff_review=True, specific_test=False)
    context = build_implementation_context_from_development(_context_map(), analysis)
    contract = next(
        item
        for item in context.contracts
        if item.identity == "chappie_development_diff_review/1.0.0"
    )
    assert contract.state is ImplementationContextStateV1.PROPOSED
    assert "DevelopmentDiffReviewV1" in {item.qualified_identifier for item in context.symbols}
    result = assess_implementation_context_sufficiency(context)
    assert result.state is ImplementationContextSufficiencyStateV1.TARGETED_EXPANSION_REQUIRED
    assert {item.requested_kind.value for item in result.missing_context} >= {"SYMBOL", "TEST"}


def test_generic_tests_become_exact_missing_context_and_supported_tests_project_directly() -> None:
    specific = build_implementation_context_from_development(_context_map(), _analysis())
    generic = build_implementation_context_from_development(
        _context_map(), _analysis(specific_test=False)
    )
    assert specific.tests[0].module_path == "tests/test_codex_task.py"
    assert (
        specific.tests[0].target_ref == "test_valid_analysis_prepares_deterministic_advisory_task"
    )
    assert any(item.missing_kind.value == "TEST" for item in generic.unknown_items)


def test_projection_excludes_handoff_presentation_and_reports_coverage() -> None:
    analysis = _analysis()
    handoff = _handoff(analysis)
    context = build_implementation_context_from_development(_context_map(), analysis, handoff)
    payload = context.stable_payload()
    assert handoff.task_id not in str(payload)
    assert handoff.projection() not in str(payload)
    report = compare_development_projection(context, analysis, handoff)
    assert "handoff_task_id" in report.intentionally_consumer_specific
    assert "current_facts" in report.preserved


def test_projection_is_pure_and_rejects_unbound_handoff() -> None:
    analysis = _analysis()
    invalid = CodexTaskV1(
        "c" * 64,
        CodexTaskStateV1.ADVISORY,
        "chappie_development_analysis/2.0.0",
        COMMIT,
        DIGEST,
        "d" * 64,
        "CODE",
        analysis.request,
    )
    import pytest

    with pytest.raises(ValueError, match="not bound"):
        build_implementation_context_from_development(_context_map(), analysis, invalid)
