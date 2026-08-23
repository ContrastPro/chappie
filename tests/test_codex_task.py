from dataclasses import replace

import pytest

from app.ai.model_policy import DevelopmentModelRoleV1
from app.control_plane import ChappieControlPlaneV1
from app.development_analysis.models import (
    DevelopmentAnalysisReadinessV1,
    DevelopmentAnalysisStateV1,
    DevelopmentAnalysisV1,
    DevelopmentEvidencePackV1,
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
    }
    evidence = DevelopmentEvidencePackV1(**raw, evidence_digest=evidence_digest(raw))
    base = DevelopmentAnalysisV1(
        DevelopmentAnalysisStateV1.OK,
        "Implement bounded analysis validation.",
        evidence=evidence,
        facts=("The evidence pack is immutable.",),
        inferences=("A bounded task can be prepared.",),
        unknowns=("Human approval remains required.",),
        architecture_owners=("ARCHITECTURE",),
        likely_files=("app/development_analysis/task.py",),
        required_tests=("tests/test_codex_task.py",),
        non_goals=("No mutation.",),
        acceptance_criteria=("Task remains advisory.",),
        validation_status="VALID",
        readiness=DevelopmentAnalysisReadinessV1.READY_FOR_TASK,
    )
    return replace(base, **changes)


def test_valid_analysis_prepares_deterministic_advisory_task() -> None:
    task = prepare_codex_task(CodexTaskPreparationRequestV1(_analysis()))
    assert task.state == CodexTaskStateV1.ADVISORY
    assert task.source_commit == "a" * 40
    assert task.evidence_digest == _analysis().evidence.evidence_digest
    assert task.requested_role == "CODE"
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
        ({"architecture_owners": ("UNKNOWN",)}, "OWNER_INVALID"),
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
