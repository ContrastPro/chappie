from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.ai.errors import AIProviderTimeoutError, AIValidationError
from app.ai.model_policy import DevelopmentModelRoleV1
from app.development_analysis.models import (
    DevelopmentAnalysisReadinessV1,
    DevelopmentAnalysisRequestV1,
    DevelopmentAnalysisStateV1,
    DevelopmentAnalysisV1,
    assess_development_analysis_readiness,
    validate_development_analysis,
)
from app.development_analysis.service import (
    DevelopmentAnalysisServiceV1,
    _development_retrieval_request,
)
from app.knowledge import RepositorySnapshotV1, initial_records, manifest
from app.knowledge.retrieval import retrieve
from app.platform.bootstrap import ChappiePaths


def _paths(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    return ChappiePaths.resolve(root)


@pytest.mark.parametrize("role", tuple(DevelopmentModelRoleV1))
def test_analysis_builds_evidence_before_stubbed_inference(tmp_path, monkeypatch, role) -> None:
    from app.development_analysis import service

    snapshot = RepositorySnapshotV1("a" * 40, True)
    records = initial_records(snapshot=snapshot)
    observed = []
    monkeypatch.setattr(service, "committed_snapshot", lambda _root: snapshot)
    monkeypatch.setattr(service, "read_store", lambda _path: (records, manifest(records, snapshot)))

    def runner(request, context, evidence):
        observed.append((request, context, evidence))
        return SimpleNamespace(
            answer="The bounded evidence identifies the relevant architecture.",
            unknowns=("Exact implementation details require human review.",),
            confidence=0.5,
        )

    result = DevelopmentAnalysisServiceV1(
        paths=_paths(tmp_path), runner=runner, role_available=lambda _role: True
    ).analyze(DevelopmentAnalysisRequestV1("Understand architecture", role))

    assert result.state == DevelopmentAnalysisStateV1.OK
    assert result.evidence is not None
    assert result.evidence.source_commit == snapshot.commit
    assert result.evidence.knowledge_manifest_digest
    assert result.evidence.evidence_digest
    assert result.payload()["evidence"]["requested_role"] == role.value
    assert observed and observed[0][2] == result.evidence
    assert result.readiness == DevelopmentAnalysisReadinessV1.INSUFFICIENT_EVIDENCE


def test_unavailable_role_fails_closed_without_running_inference(tmp_path) -> None:
    calls = []
    result = DevelopmentAnalysisServiceV1(
        paths=_paths(tmp_path),
        runner=lambda *_args: calls.append(True),
        role_available=lambda _role: False,
    ).analyze(DevelopmentAnalysisRequestV1("Analyze a request", DevelopmentModelRoleV1.CODE))
    assert result.state == DevelopmentAnalysisStateV1.MODEL_UNAVAILABLE
    assert result.reason_code == "MODEL_ROLE_UNAVAILABLE"
    assert calls == []


def test_provider_timeout_is_a_typed_development_analysis_failure(tmp_path, monkeypatch) -> None:
    from app.development_analysis import service

    snapshot = RepositorySnapshotV1("a" * 40, True)
    records = initial_records(snapshot=snapshot)
    monkeypatch.setattr(service, "committed_snapshot", lambda _root: snapshot)
    monkeypatch.setattr(service, "read_store", lambda _path: (records, manifest(records, snapshot)))
    result = DevelopmentAnalysisServiceV1(
        paths=_paths(tmp_path),
        runner=lambda *_args: (_ for _ in ()).throw(AIProviderTimeoutError("timed out")),
        role_available=lambda _role: True,
    ).analyze(DevelopmentAnalysisRequestV1("Understand architecture", DevelopmentModelRoleV1.CODE))

    assert result.state == DevelopmentAnalysisStateV1.TIMEOUT
    assert result.reason_code == "AI_PROVIDER_TIMEOUT"


def test_model_validation_failure_is_a_typed_development_analysis_failure(
    tmp_path, monkeypatch
) -> None:
    from app.development_analysis import service

    snapshot = RepositorySnapshotV1("a" * 40, True)
    records = initial_records(snapshot=snapshot)
    monkeypatch.setattr(service, "committed_snapshot", lambda _root: snapshot)
    monkeypatch.setattr(service, "read_store", lambda _path: (records, manifest(records, snapshot)))
    result = DevelopmentAnalysisServiceV1(
        paths=_paths(tmp_path),
        runner=lambda *_args: (_ for _ in ()).throw(AIValidationError("invalid model output")),
        role_available=lambda _role: True,
    ).analyze(DevelopmentAnalysisRequestV1("Understand architecture", DevelopmentModelRoleV1.CODE))

    assert result.state == DevelopmentAnalysisStateV1.ANALYSIS_INVALID
    assert result.reason_code == "DEVELOPMENT_ANALYSIS_INVALID"


@pytest.mark.parametrize("unsafe", ("C:/secret.py", "../secret.py"))
def test_validator_rejects_unsafe_likely_files(unsafe: str) -> None:
    value = DevelopmentAnalysisV1(
        DevelopmentAnalysisStateV1.OK,
        "request",
        facts=("fact",),
        inferences=("inference",),
        likely_files=(unsafe,),
        required_tests=("test",),
        non_goals=("No mutation.",),
        acceptance_criteria=("criterion",),
    )
    with pytest.raises(ValueError, match="evidence"):
        validate_development_analysis(value)


def test_exact_dogfood_identifiers_select_development_records_deterministically() -> None:
    snapshot = RepositorySnapshotV1("a" * 40, True)
    records = initial_records(snapshot=snapshot)
    store_manifest = manifest(records, snapshot)
    request = "Design a bounded post-implementation diff review workflow using DevelopmentAnalysisV1 and CodexTaskV1."
    first, _ = retrieve(
        records, store_manifest, _development_retrieval_request(records, request, None)
    )
    second, _ = retrieve(
        records, store_manifest, _development_retrieval_request(records, request, None)
    )
    selected = {
        record.title for record in records if record.knowledge_id in first.selected_record_ids
    }
    assert first.selected_record_ids == second.selected_record_ids
    assert {
        "DevelopmentAnalysisV1",
        "CodexTaskV1",
        "Package ownership: Development Intelligence",
        "POST /v1/development/analyze",
        "POST /v1/development/task",
        "Development Intelligence advisory boundary",
        "Development Intelligence analysis tests",
    } <= selected
    assert "Package ownership: Market" not in selected
    assert "Package ownership: Trading" not in selected


@pytest.mark.parametrize(
    "identifier",
    ("DevelopmentAnalysisV1", "CodexTaskV1", "app.development_analysis", "/v1/development/analyze"),
)
def test_development_identifier_has_exact_retrieval_priority(identifier: str) -> None:
    snapshot = RepositorySnapshotV1("a" * 40, True)
    records = initial_records(snapshot=snapshot)
    result, _ = retrieve(
        records,
        manifest(records, snapshot),
        _development_retrieval_request(records, f"Review {identifier}", None),
    )
    assert result.selected_record_ids
    assert result.selection_reasons[0][1][0].value == "EXACT_ID"


def _ready_analysis(**changes) -> DevelopmentAnalysisV1:
    base = DevelopmentAnalysisV1(
        DevelopmentAnalysisStateV1.OK,
        "Design a bounded diff review for DevelopmentAnalysisV1.",
        evidence=object(),  # type: ignore[arg-type]
        facts=(
            "app.development_analysis owns read-only development analysis.",
            "DevelopmentAnalysisV1 is the contract for the requested diff review.",
        ),
        inferences=("The requested diff review must remain advisory.",),
        unknowns=(
            "The exact future diff-review endpoint cannot be determined from current evidence.",
        ),
        architecture_owners=("MODULE_RESPONSIBILITY",),
        affected_contracts=("DevelopmentAnalysisV1",),
        likely_files=("app/development_analysis/models.py",),
        risks=("The requested diff review must preserve its advisory boundary.",),
        required_tests=("Test the requested diff review readiness gate.",),
        non_goals=("No mutation.",),
        acceptance_criteria=("The requested diff review remains evidence-bound.",),
        validation_status="VALID",
    )
    return replace(base, **changes)


def test_structural_validity_is_distinct_from_task_readiness() -> None:
    assert (
        assess_development_analysis_readiness(_ready_analysis())
        == DevelopmentAnalysisReadinessV1.READY_FOR_TASK
    )
    assert (
        assess_development_analysis_readiness(_ready_analysis(likely_files=()))
        == DevelopmentAnalysisReadinessV1.INSUFFICIENT_EVIDENCE
    )
    assert (
        assess_development_analysis_readiness(_ready_analysis(affected_contracts=()))
        == DevelopmentAnalysisReadinessV1.INSUFFICIENT_EVIDENCE
    )
    assert (
        assess_development_analysis_readiness(
            _ready_analysis(required_tests=("Focused deterministic analysis and boundary tests.",))
        )
        == DevelopmentAnalysisReadinessV1.INSUFFICIENT_EVIDENCE
    )
    assert (
        assess_development_analysis_readiness(
            _ready_analysis(
                acceptance_criteria=("Analysis remains bound to the returned evidence digest.",)
            )
        )
        == DevelopmentAnalysisReadinessV1.INSUFFICIENT_EVIDENCE
    )
    assert (
        assess_development_analysis_readiness(
            _ready_analysis(unknowns=("No unknowns identified based on provided facts.",))
        )
        == DevelopmentAnalysisReadinessV1.NEEDS_REVIEW
    )


def test_grounded_unknowns_allow_ready_analysis_without_invented_scope() -> None:
    no_contract = _ready_analysis(
        affected_contracts=(),
        unknowns=("The exact contract cannot be determined from current evidence.",),
    )
    no_file = _ready_analysis(
        likely_files=(),
        unknowns=("The exact file cannot be determined from current evidence.",),
    )
    assert (
        assess_development_analysis_readiness(no_contract)
        == DevelopmentAnalysisReadinessV1.READY_FOR_TASK
    )
    assert (
        assess_development_analysis_readiness(no_file)
        == DevelopmentAnalysisReadinessV1.READY_FOR_TASK
    )
