from types import SimpleNamespace

import pytest

from app.ai.errors import AIProviderTimeoutError, AIValidationError
from app.ai.model_policy import DevelopmentModelRoleV1
from app.development_analysis.models import (
    DevelopmentAnalysisRequestV1,
    DevelopmentAnalysisStateV1,
    DevelopmentAnalysisV1,
    validate_development_analysis,
)
from app.development_analysis.service import DevelopmentAnalysisServiceV1
from app.knowledge import RepositorySnapshotV1, initial_records, manifest
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
