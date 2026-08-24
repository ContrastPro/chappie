from dataclasses import replace

import pytest

from app.ai.model_policy import DevelopmentModelRoleV1
from app.control_plane import ChappieControlPlaneV1
from app.development_analysis.context import DevelopmentContextMapV1, build_context_digest
from app.development_analysis.models import (
    DevelopmentAnalysisReadinessV1,
    DevelopmentAnalysisStateV1,
    DevelopmentAnalysisV1,
    DevelopmentEvidencePackV1,
    DevelopmentProposedScopeV1,
    evidence_digest,
)
from app.development_analysis.task import (
    CodexTaskPreparationRequestV1,
    CodexTaskStateV1,
    prepare_codex_task,
)


def _analysis(**changes):
    raw = {
        "source_commit": "a" * 40,
        "repository_clean": True,
        "knowledge_identity": "chappie_knowledge/1.0.0",
        "knowledge_manifest_digest": "b" * 64,
        "knowledge_context_digest": "c" * 64,
        "workspace_context_digest": None,
        "requested_role": DevelopmentModelRoleV1.CODE,
        "requested_model": "qwen3-coder:30b",
        "effective_model": "qwen3-coder:30b",
        "profile_identity": "CODE_ADVISORY",
        "capability_identity": "development_model_role/CODE_ADVISORY",
        "record_ids": ("record",),
        "record_digests": ("d" * 64,),
        "allowed_fact_ids": ("knowledge.record.statement",),
    }
    evidence = DevelopmentEvidencePackV1(**raw, evidence_digest=evidence_digest(raw))
    base = DevelopmentAnalysisV1(
        DevelopmentAnalysisStateV1.OK,
        "Implement bounded analysis validation.",
        evidence=evidence,
        facts=("The evidence pack is immutable.",),
        inferences=("A bounded task can be prepared.",),
        unknowns=("Human approval remains required.",),
        architecture_owners=("MODULE_RESPONSIBILITY",),
        likely_files=("app/development_analysis/task.py",),
        required_tests=("tests/test_codex_task.py",),
        non_goals=("No mutation.",),
        acceptance_criteria=("Task remains advisory.",),
        validation_status="VALID",
        readiness=DevelopmentAnalysisReadinessV1.READY_FOR_TASK,
        context_map=_context_map(),
        current_evidence_handles=("E1",),
        proposed_scope=_proposed_scope(),
    )
    return replace(base, **changes)


def _context_map() -> DevelopmentContextMapV1:
    value = DevelopmentContextMapV1(
        "a" * 40,
        "chappie_knowledge/1.0.0",
        "b" * 64,
        "c" * 64,
        "d" * 64,
        "CODE",
        "qwen3-coder:30b",
        (),
        ("app.development_analysis",),
        (),
        ("app/development_analysis/task.py",),
        ("tests/test_codex_task.py",),
        ("Advisory only.",),
        "",
    )
    return replace(value, context_digest=build_context_digest(value))


def _proposed_scope() -> DevelopmentProposedScopeV1:
    return DevelopmentProposedScopeV1(
        "app.development_analysis",
        ("app/development_analysis/task.py",),
        ("app/development_analysis/diff_review.py",),
        ("Add bounded DTO.",),
        ("Diff review",),
        ("Remain advisory.",),
        ("Bounded scope.",),
    )


def test_valid_analysis_prepares_deterministic_advisory_task() -> None:
    analysis = _analysis()
    task = prepare_codex_task(CodexTaskPreparationRequestV1(analysis))
    assert task.state == CodexTaskStateV1.ADVISORY
    assert task.source_commit == "a" * 40
    assert task.evidence_digest == analysis.evidence.evidence_digest
    assert task.requested_role == "CODE"
    assert task.facts == analysis.facts
    assert task.inferences == analysis.inferences
    assert task.unknowns == analysis.unknowns
    assert task.allowed_files == analysis.likely_files
    assert task.proposed_modified_files == analysis.proposed_scope.proposed_modified_files
    assert task.proposed_new_files == analysis.proposed_scope.proposed_new_files
    assert task.proposed_contract_changes == analysis.proposed_scope.proposed_contract_changes
    assert task.risks == analysis.risks
    assert task.required_tests == analysis.required_tests
    assert task.non_goals == analysis.non_goals
    assert task.acceptance_criteria == analysis.acceptance_criteria
    assert task.approval_status == "HUMAN_APPROVAL_REQUIRED"
    assert task.proposed_owner == "app.development_analysis"
    assert task.payload()["proposed_owner"] == "app.development_analysis"
    assert "app.development_analysis" in task.projection()
    assert task.projection() == task.projection()
    assert "Human approval" in task.projection()


def test_control_plane_prepares_only_a_validated_advisory_payload() -> None:
    analysis = _analysis().payload()
    plane = ChappieControlPlaneV1(None, status_collector=lambda _paths: None)
    task = plane.prepare_development_task(analysis)
    assert task.state == CodexTaskStateV1.ADVISORY
    assert task.payload()["projection"] == task.projection()


@pytest.mark.parametrize(
    ("changes", "reason"),
    (
        ({"likely_files": ("C:/unsafe.py",)}, "UNSAFE_FILE_PATH"),
        ({"likely_files": ("../unsafe.py",)}, "UNSAFE_FILE_PATH"),
        ({"required_tests": ()}, "MISSING_TESTS"),
        ({"acceptance_criteria": ()}, "MISSING_ACCEPTANCE_CRITERIA"),
        ({"inferences": ("Use shell to implement it.",)}, "AUTHORITY_VIOLATION"),
        (
            {"inferences": ("Use stale Model_AB benchmark evidence.",)},
            "UNRELATED_BENCHMARK_ASSUMPTION",
        ),
    ),
)
def test_unsafe_or_incomplete_analysis_fails_closed(changes, reason) -> None:
    task = prepare_codex_task(CodexTaskPreparationRequestV1(_analysis(**changes)))
    assert task.state == CodexTaskStateV1.VALIDATION_ERROR
    assert task.reason_code == reason


@pytest.mark.parametrize(
    ("field", "text"),
    (
        ("risks", "There is a risk of contract drift."),
        ("risks", "Risk of missing test coverage."),
        ("risks", "Risk boundary must remain unchanged."),
        ("risks", "No Risk authority is introduced."),
        ("risks", "Risk authority remains unchanged."),
        ("inferences", "The capability remains read-only."),
        ("acceptance_criteria", "No execution authority is introduced."),
        (
            "acceptance_criteria",
            "Human approval is required before Codex receives a task.",
        ),
    ),
)
def test_safe_risk_and_authority_boundaries_remain_advisory(field, text) -> None:
    task = prepare_codex_task(CodexTaskPreparationRequestV1(_analysis(**{field: (text,)})))
    assert task.state == CodexTaskStateV1.ADVISORY
    assert task.reason_code is None


@pytest.mark.parametrize(
    ("field", "text"),
    (
        ("inferences", "Execute trades for the portfolio."),
        ("inferences", "Submit orders automatically."),
        ("risks", "Mutate Risk state as a mitigation."),
        ("inferences", "Write files in the workspace."),
        ("inferences", "Run arbitrary shell commands."),
        ("inferences", "Commit Git changes after review."),
        ("inferences", "Push Git changes after review."),
        ("inferences", "Rebuild Knowledge automatically."),
        ("acceptance_criteria", "Deploy this capability."),
        ("inferences", "Gain execution authority."),
    ),
)
def test_active_authority_claims_fail_closed(field, text) -> None:
    task = prepare_codex_task(CodexTaskPreparationRequestV1(_analysis(**{field: (text,)})))
    assert task.state == CodexTaskStateV1.VALIDATION_ERROR
    assert task.reason_code == "AUTHORITY_VIOLATION"


def test_authority_violation_returns_a_redacted_handoff() -> None:
    analysis = _analysis(inferences=("Execute trades for the portfolio.",))
    task = prepare_codex_task(CodexTaskPreparationRequestV1(analysis))
    assert task.state == CodexTaskStateV1.VALIDATION_ERROR
    assert task.validation_status == "INVALID"
    assert task.reason_code == "AUTHORITY_VIOLATION"
    assert task.task_id == ""
    assert task.facts == ()
    assert task.inferences == ()
    assert task.evidence_map == ()
    assert task.allowed_files == ()
    assert task.proposed_modified_files == ()
    assert task.required_tests == ()
    assert task.acceptance_criteria == ()
    assert task.proposed_owner is None


@pytest.mark.parametrize(
    "owner",
    (
        "app.some_invented_package",
        "Development Intelligence",
        "MODULE_RESPONSIBILITY",
        None,
    ),
)
def test_handoff_owner_must_be_a_request_local_canonical_candidate(owner) -> None:
    analysis = _analysis(proposed_scope=replace(_proposed_scope(), proposed_owner=owner))
    task = prepare_codex_task(CodexTaskPreparationRequestV1(analysis))
    assert task.state == CodexTaskStateV1.VALIDATION_ERROR
    assert task.reason_code == "OWNER_INVALID"
    assert task.proposed_owner is None


def test_legacy_architecture_owner_categories_do_not_block_canonical_handoff() -> None:
    analysis = _analysis(
        architecture_owners=(
            "AUTHORITY_BOUNDARY",
            "CONTRACT",
            "LIMITATION",
            "MODULE_RESPONSIBILITY",
        )
    )
    task = prepare_codex_task(CodexTaskPreparationRequestV1(analysis))
    assert task.state == CodexTaskStateV1.ADVISORY
    assert task.proposed_owner == "app.development_analysis"


@pytest.mark.parametrize(
    "readiness",
    (
        DevelopmentAnalysisReadinessV1.INSUFFICIENT_EVIDENCE,
        DevelopmentAnalysisReadinessV1.NEEDS_REVIEW,
    ),
)
def test_non_ready_analysis_cannot_prepare_codex_task(readiness) -> None:
    task = prepare_codex_task(CodexTaskPreparationRequestV1(_analysis(readiness=readiness)))
    assert task.state == CodexTaskStateV1.VALIDATION_ERROR
    assert task.reason_code == "SOURCE_ANALYSIS_NOT_READY"


def test_ready_analysis_with_blocking_repository_context_cannot_prepare_codex_task() -> None:
    analysis = _analysis(unknowns=("Full repository file inventory unavailable.",))
    task = prepare_codex_task(CodexTaskPreparationRequestV1(analysis))
    assert task.state == CodexTaskStateV1.VALIDATION_ERROR
    assert task.reason_code == "SOURCE_ANALYSIS_OUTCOME_NOT_AUTHORIZABLE"
    assert task.task_id == ""
