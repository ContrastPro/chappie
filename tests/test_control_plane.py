"""Deterministic read-only contracts for the local Control Plane."""

from __future__ import annotations

import json
from pathlib import Path

from app.ai.schemas import digest
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
from app.control_plane.workspace_context import (
    WORKSPACE_CONTEXT_IDENTITY,
    WorkspaceContextV1,
    WorkspaceRangeV1,
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


def test_project_qa_accepts_canonical_file_selection_git_and_combined_context_snapshots(
    tmp_path: Path,
) -> None:
    seen: list[WorkspaceContextV1 | None] = []

    def runner(
        _paths: object, question: str, workspace_context: WorkspaceContextV1 | None
    ) -> ProjectQAControlPlaneResponseV1:
        seen.append(workspace_context)
        return ProjectQAControlPlaneResponseV1(
            ProjectQAStateV1.OK,
            question,
            "Grounded answer.",
            ("knowledge.record.statement",),
            (ProjectQASourceV1("record", "Evidence", "VERSIONED_CONTRACT", "FROZEN"),),
            (),
            "a" * 64,
            "mock",
            "mock-model",
            "VALID",
        )

    plane = ChappieControlPlaneV1(
        _paths(tmp_path), status_collector=lambda _paths: _status(), project_qa_runner=runner
    )
    base = {"workspace_name": "chappie", "workspace_id": "a" * 64}
    contexts = (
        WorkspaceContextV1(
            **base,
            active_file_relative_path="app/knowledge/store.py",
            language_id="python",
            active_file_excerpt="file snapshot",
            excerpt_range=WorkspaceRangeV1(0, 0),
        ),
        WorkspaceContextV1(
            **base,
            active_file_relative_path="app/knowledge/store.py",
            language_id="python",
            selected_text="selection snapshot",
            selected_text_range=WorkspaceRangeV1(10, 11),
        ),
        WorkspaceContextV1(**base, git_branch="main", git_commit="b" * 40, git_dirty=True),
        WorkspaceContextV1(
            **base,
            active_file_relative_path="app/knowledge/store.py",
            language_id="python",
            active_file_excerpt="file snapshot",
            excerpt_range=WorkspaceRangeV1(0, 0),
            git_branch="main",
            git_commit="b" * 40,
            git_dirty=True,
        ),
        WorkspaceContextV1(
            **base,
            active_file_relative_path="app/knowledge/store.py",
            language_id="python",
            selected_text="selection snapshot",
            selected_text_range=WorkspaceRangeV1(10, 11),
            git_branch="main",
            git_commit="b" * 40,
            git_dirty=True,
        ),
    )
    for context in contexts:
        assert (
            plane.ask_project("Understand architecture", context.payload()).state
            == ProjectQAStateV1.OK
        )
    assert [context.context_digest for context in seen if context] == [
        context.context_digest for context in contexts
    ]


def test_project_qa_rejects_invalid_or_over_limit_workspace_context_without_running_qa(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    plane = ChappieControlPlaneV1(
        _paths(tmp_path),
        status_collector=lambda _paths: _status(),
        project_qa_runner=lambda _paths, question, _context: calls.append(question),
    )
    invalid = {"schema": "workspace_context/1.0.0"}
    over_limit = WorkspaceContextV1(
        "chappie",
        "a" * 64,
        active_file_relative_path="app/knowledge/store.py",
        language_id="python",
    ).payload()
    over_limit["selected_text"] = "x" * 12_001
    over_limit["selected_text_range"] = {"start_line": 0, "end_line": 0}
    over_limit["context_digest"] = digest(
        {
            "identity": WORKSPACE_CONTEXT_IDENTITY,
            **{key: value for key, value in over_limit.items() if key != "context_digest"},
        }
    )
    for payload in (invalid, over_limit):
        response = plane.ask_project("Understand architecture", payload)
        assert response.state == ProjectQAStateV1.VALIDATION_ERROR
        assert response.error is not None
        assert response.error.reason_code == "WORKSPACE_CONTEXT_INVALID"
    assert calls == []


def test_ready_knowledge_with_no_relevant_records_is_not_reported_as_unconfigured(
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
        "What is the Chappie banana protocol?",
    )

    assert response.state == ProjectQAStateV1.VALIDATION_ERROR
    assert response.error is not None
    assert response.error.reason_code == "PROJECT_QA_NO_RELEVANT_KNOWLEDGE"


def test_project_qa_returns_honest_unknown_current_capability_without_ai(
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
    from app.knowledge import retrieval

    monkeypatch.setattr(
        retrieval, "architecture_query_tokens", lambda _question, **_kwargs: ("banana",)
    )

    response = service._run_project_qa(
        _paths(tmp_path), "Which current Economic Calendar storage schema is active?"
    )

    assert response.state == ProjectQAStateV1.OK
    assert response.validation_status == "UNKNOWN_CURRENT_CAPABILITY"
    assert response.supporting_records == ()
    assert (
        response.answer == "Current canonical Knowledge does not establish such an implementation."
    )


def test_project_qa_returns_workspace_observation_without_ai(tmp_path: Path, monkeypatch) -> None:
    from app.control_plane import service
    from app.knowledge import store
    from app.platform.setup import ai_smoke

    snapshot = RepositorySnapshotV1("a" * 40, True)
    records = initial_records(snapshot=snapshot)
    monkeypatch.setattr(service, "load_local_config", lambda _paths: object())
    monkeypatch.setattr(ai_smoke, "valid_setup_ai_config", lambda _config: True)
    monkeypatch.setattr(store, "read_store", lambda _path: (records, manifest(records, snapshot)))
    from app.knowledge import retrieval

    monkeypatch.setattr(
        retrieval, "architecture_query_tokens", lambda _question, **_kwargs: ("banana",)
    )
    context = WorkspaceContextV1(
        "chappie", "a" * 64, git_branch="main", git_commit="b" * 40, git_dirty=False
    )

    response = service._run_project_qa(
        _paths(tmp_path), "What can be concluded from this Git snapshot?", context
    )

    assert response.state == ProjectQAStateV1.OK
    assert response.validation_status == "CONTEXT_OBSERVATION"
    assert response.supporting_records == ()
    assert "No diff or source change was supplied" in response.answer


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


def test_list_models_projects_only_policy_models_without_mutation(tmp_path: Path) -> None:
    from app.platform.ollama.model_state import LocalOllamaModelStateV1

    config = ChappieLocalConfigV1(
        provider="ollama", model="qwen3:4b", enabled_capabilities=("LIGHT",)
    )
    plane = ChappieControlPlaneV1(
        _paths(tmp_path),
        status_collector=lambda _paths: _status(),
        config_loader=lambda _paths: config,
        model_state_reader=lambda: LocalOllamaModelStateV1(
            ("qwen3:4b", "qwen3:14b", "random-model:latest"), True
        ),
    )
    items = plane.list_models()
    assert [(x.model_id.value, x.installed, x.ready, x.default) for x in items] == [
        ("QWEN3_4B", True, True, True),
        ("QWEN3_14B", True, True, False),
    ]
    assert config.model == "qwen3:4b"


def test_list_models_is_honest_when_runtime_is_not_ready(tmp_path: Path) -> None:
    from app.platform.ollama.model_state import LocalOllamaModelStateV1

    plane = ChappieControlPlaneV1(
        _paths(tmp_path),
        status_collector=lambda _paths: _status(),
        config_loader=lambda _paths: ChappieLocalConfigV1(
            provider="ollama", model="qwen3:4b", enabled_capabilities=("LIGHT",)
        ),
        model_state_reader=lambda: LocalOllamaModelStateV1(("qwen3:4b",), False),
    )
    assert [(x.installed, x.ready, x.default) for x in plane.list_models()] == [
        (True, False, True),
        (False, False, False),
    ]


def test_project_qa_response_serializes_execution_model_metadata() -> None:
    response = ProjectQAControlPlaneResponseV1(
        ProjectQAStateV1.OK,
        "Understand architecture",
        "Grounded answer.",
        provider="ollama",
        model="qwen3:14b",
        requested_model_id="QWEN3_14B",
        effective_model_id="QWEN3_14B",
        effective_model="qwen3:14b",
        validation_status="VALID",
    )

    payload = response.payload()

    assert payload["provider"] == "ollama"
    assert payload["model"] == "qwen3:14b"
    assert payload["requested_model_id"] == "QWEN3_14B"
    assert payload["effective_model_id"] == "QWEN3_14B"
    assert payload["effective_model"] == "qwen3:14b"


def test_project_qa_resolves_policy_budget_with_the_selected_model(
    tmp_path: Path, monkeypatch
) -> None:
    from app.ai.errors import AIProviderTimeoutError
    from app.control_plane import service
    from app.knowledge import store
    from app.platform.setup import ai_smoke

    snapshot = RepositorySnapshotV1("a" * 40, True)
    records = initial_records(snapshot=snapshot)
    config = ChappieLocalConfigV1(
        provider="ollama", model="qwen3:4b", enabled_capabilities=("LIGHT",)
    )
    captured: list[tuple[str, float]] = []

    class TimeoutAI:
        def answer_project_qa(self, **_kwargs: object) -> object:
            raise AIProviderTimeoutError("fixture timeout")

        def close(self) -> None:
            pass

    def build(_config: object, *, model: str, timeout_seconds: float) -> TimeoutAI:
        captured.append((model, timeout_seconds))
        return TimeoutAI()

    monkeypatch.setattr(service, "load_local_config", lambda _paths: config)
    monkeypatch.setattr(ai_smoke, "valid_setup_ai_config", lambda _config: True)
    monkeypatch.setattr(ai_smoke, "build_configured_chappie_ai", build)
    monkeypatch.setattr(store, "read_store", lambda _path: (records, manifest(records, snapshot)))
    paths = _paths(tmp_path)

    for requested_model_id in (None, "QWEN3_4B", "QWEN3_14B", None):
        response = service._run_project_qa(
            paths,
            "What owns verified research evidence authority?",
            requested_model_id=requested_model_id,
        )
        assert response.state == ProjectQAStateV1.TIMEOUT

    assert captured == [
        ("qwen3:4b", 45.0),
        ("qwen3:4b", 45.0),
        ("qwen3:14b", 75.0),
        ("qwen3:4b", 45.0),
    ]
