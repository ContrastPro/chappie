"""Deterministic read-only contracts for the local Control Plane."""

from __future__ import annotations

import json
from pathlib import Path

from app.control_plane import (
    CONTROL_PLANE_CONTRACT_DIGEST,
    CONTROL_PLANE_IDENTITY,
    CapabilityStatusV1,
    ChappieControlPlaneV1,
    ControlPlaneAIStatusV1,
    ControlPlaneStateV1,
    ProjectQAControlPlaneResponseV1,
    ProjectQASourceV1,
    ProjectQAStateV1,
)
from app.knowledge import RepositorySnapshotV1, initial_records, manifest
from app.platform.bootstrap import (
    ChappieDoctorReportV1,
    ChappieLocalConfigV1,
    ChappiePaths,
    ChappieStatusV1,
    ComponentStateV1,
    ComponentStatusV1,
    HardwareProfileV1,
    InstallationStateV1,
)


def _paths(tmp_path: Path) -> ChappiePaths:
    root = tmp_path / "project"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname = 'fixture'\n", encoding="utf-8")
    return ChappiePaths.resolve(root)


def _status(*, ready: bool = True) -> ChappieStatusV1:
    component_state = ComponentStateV1.READY if ready else ComponentStateV1.NOT_CONFIGURED
    components = tuple(
        ComponentStatusV1(name, component_state, True, None, None, "fixture")
        for name in ("KNOWLEDGE", "AI_PROVIDER", "AI_MODEL", "RESEARCH_STORAGE")
    )
    return ChappieStatusV1(
        "chappie_installation_state/1.0.0",
        "a" * 64,
        InstallationStateV1.READY if ready else InstallationStateV1.SETUP_REQUIRED,
        "Windows",
        "AMD64",
        HardwareProfileV1("Windows", "fixture", "AMD64", None, 16, 32 * 1024**3),
        components,
        ("BALANCED",) if ready else (),
        (),
        (),
    )


def test_control_plane_is_versioned_json_safe_and_read_only(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    calls: list[str] = []

    def status_source(_paths: object) -> ChappieStatusV1:
        calls.append("status")
        return _status()

    def diagnostics_source(_paths: object) -> ChappieDoctorReportV1:
        calls.append("diagnostics")
        return ChappieDoctorReportV1(_status(), ())

    plane = ChappieControlPlaneV1(
        paths,
        status_collector=status_source,
        diagnostics_collector=diagnostics_source,
        config_loader=lambda _paths: ChappieLocalConfigV1(
            provider="ollama", model="qwen3:8b", enabled_capabilities=("BALANCED",)
        ),
    )
    status = plane.get_status()
    diagnostics = plane.get_diagnostics()
    ai = plane.get_ai_status()
    capabilities = plane.get_capabilities()

    assert status.state == ControlPlaneStateV1.OK
    assert diagnostics.state == ControlPlaneStateV1.OK
    assert ai == ControlPlaneAIStatusV1(
        ControlPlaneStateV1.OK, "ollama", "BALANCED", "qwen3:8b", "READY", "READY"
    )
    assert [item.capability_id for item in capabilities.capabilities] == [
        "system.status",
        "system.doctor",
        "ai.status",
        "knowledge.query",
        "project.qa",
        "research.existing_cache",
    ]
    assert all(item.read_only for item in capabilities.capabilities)
    assert calls == ["status", "diagnostics", "status", "status"]
    assert not paths.state_root.exists()
    for response in (status, diagnostics, ai, capabilities):
        payload = response.payload()
        assert payload["identity"] == CONTROL_PLANE_IDENTITY
        assert payload["contract_digest"] == CONTROL_PLANE_CONTRACT_DIGEST
        assert json.loads(json.dumps(payload)) == payload


def test_control_plane_degrades_capabilities_without_invoking_ai_or_research(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    invoked: list[str] = []
    plane = ChappieControlPlaneV1(
        paths,
        status_collector=lambda _paths: _status(ready=False),
        diagnostics_collector=lambda _paths: ChappieDoctorReportV1(_status(ready=False), ()),
        config_loader=lambda _paths: (_ for _ in ()).throw(AssertionError("config not needed")),
    )

    capabilities = plane.get_capabilities()
    assert capabilities.state == ControlPlaneStateV1.DEGRADED
    states = {item.capability_id: item.status for item in capabilities.capabilities}
    assert states["knowledge.query"] == CapabilityStatusV1.UNAVAILABLE
    assert states["project.qa"] == CapabilityStatusV1.DEGRADED
    assert states["research.existing_cache"] == CapabilityStatusV1.UNAVAILABLE
    assert invoked == []


def test_control_plane_normalizes_implementation_failures(tmp_path: Path) -> None:
    plane = ChappieControlPlaneV1(
        _paths(tmp_path), status_collector=lambda _paths: (_ for _ in ()).throw(RuntimeError())
    )
    response = plane.get_status()
    assert response.state == ControlPlaneStateV1.INTERNAL_ERROR
    assert response.error is not None
    assert response.error.reason_code == "STATUS_UNAVAILABLE"


def test_project_qa_is_injected_read_only_and_validates_questions(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    calls: list[str] = []

    def runner(
        _paths: object, question: str, _workspace_context: object | None
    ) -> ProjectQAControlPlaneResponseV1:
        calls.append(question)
        return ProjectQAControlPlaneResponseV1(
            ProjectQAStateV1.OK,
            question,
            "RDE owns verified evidence.",
            ("knowledge.record.statement",),
            (ProjectQASourceV1("record", "Evidence", "VERSIONED_CONTRACT", "FROZEN"),),
            (),
            "a" * 64,
            "mock",
            "mock-model",
            "VALID",
        )

    plane = ChappieControlPlaneV1(
        paths, status_collector=lambda _paths: _status(), project_qa_runner=runner
    )
    response = plane.ask_project(" What owns evidence? ")
    assert response.state == ProjectQAStateV1.OK
    assert calls == ["What owns evidence?"]
    assert response.payload()["authority"] == "AI_WITH_KNOWLEDGE_READ_ONLY"
    assert plane.ask_project(" ").state == ProjectQAStateV1.VALIDATION_ERROR
    too_large = plane.ask_project("x" * 12_001)
    assert too_large.state == ProjectQAStateV1.VALIDATION_ERROR
    assert too_large.error is not None
    assert too_large.error.reason_code == "PROJECT_QA_REQUEST_TOO_LARGE"
    assert calls == ["What owns evidence?"]


def test_ready_knowledge_with_no_relevant_deep_records_is_not_reported_as_unconfigured(
    tmp_path: Path, monkeypatch
) -> None:
    from app.control_plane import service
    from app.knowledge import store
    from app.platform.setup import ai_smoke

    snapshot = RepositorySnapshotV1("a" * 40, True)
    records = initial_records(snapshot=snapshot)
    monkeypatch.setattr(service, "load_local_config", lambda _paths: object())
    monkeypatch.setattr(ai_smoke, "valid_setup_ai_config", lambda _config: True)
    monkeypatch.setattr(store, "read_store", lambda _path: (records, manifest(records, snapshot)))

    response = service._run_project_qa(
        _paths(tmp_path),
        "Design News and Event Intelligence architecture across ownership dependencies authority data contracts persistence failure model Workbench UX implementation sequence",
    )

    assert response.state == ProjectQAStateV1.VALIDATION_ERROR
    assert response.error is not None
    assert response.error.reason_code == "PROJECT_QA_NO_RELEVANT_KNOWLEDGE"


def test_cli_ai_status_uses_control_plane_adapter(tmp_path: Path, monkeypatch, capsys) -> None:
    from app import cli

    class Plane:
        def get_ai_status(self) -> ControlPlaneAIStatusV1:
            return ControlPlaneAIStatusV1(
                ControlPlaneStateV1.OK, "ollama", "BALANCED", "qwen3:8b", "READY", "READY"
            )

    monkeypatch.setattr(cli, "_control_plane", lambda _paths: Plane())
    assert cli.main(["ai", "status"], paths=_paths(tmp_path)) == 0
    assert capsys.readouterr().out.splitlines() == [
        "AI_PROVIDER: READY",
        "AI_MODEL: READY",
        "PROVIDER: ollama",
        "PROFILE: BALANCED",
        "MODEL: qwen3:8b",
    ]
