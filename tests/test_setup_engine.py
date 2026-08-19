"""Offline contract tests for the resumable, approval-bound setup engine."""

from __future__ import annotations

import subprocess
from pathlib import Path

from app.ai_setup_smoke import AISetupSmokeResultV1, AISetupSmokeStateV1
from app.bootstrap import (
    AIModelProfileV1,
    ChappieLocalConfigV1,
    ChappiePaths,
    ComponentStateV1,
    SetupStepStateV1,
    load_local_config,
)
from app.ollama_readiness import OllamaReadinessResultV1, OllamaReadinessStateV1
from app.project_qa_setup_smoke import ProjectQASetupSmokeResultV1, ProjectQASetupSmokeStateV1
from app.setup_engine import (
    SETUP_ENGINE_DIGEST,
    SETUP_ENGINE_IDENTITY,
    ActionSafetyV1,
    OllamaInstallerV1,
    SetupActionCodeV1,
    SetupActionV1,
    SetupApprovalV1,
    SetupEngineV1,
    SetupRunStateV1,
    SetupStepResultV1,
    default_actions,
)
from app.windows_ollama import (
    ExternalActionResultV1,
    ExternalActionStatusV1,
    WindowsOllamaInstallerV1,
)


class FakeSetupEngine(SetupEngineV1):
    def __init__(self, paths: ChappiePaths, *, fail: SetupActionCodeV1 | None = None) -> None:
        super().__init__(paths)
        self.done: set[SetupActionCodeV1] = set()
        self.applied: list[SetupActionCodeV1] = []
        self.fail = fail

    def _check(self, action):  # type: ignore[no-untyped-def]
        return action.code in self.done

    def _apply(self, action, *, developer, ai_profile):  # type: ignore[no-untyped-def]
        self.applied.append(action.code)
        if action.code == self.fail:
            raise RuntimeError("fixture failure")
        self.done.add(action.code)


def _paths(tmp_path: Path) -> ChappiePaths:
    root = tmp_path / "checkout"
    root.mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='fixture'\n")
    return ChappiePaths.resolve(root)


def test_engine_identity_and_safe_external_action_classification() -> None:
    actions = default_actions(developer=True, ai_profile=AIModelProfileV1.BALANCED)
    assert SETUP_ENGINE_IDENTITY == "chappie_setup_engine/1.0.0"
    assert len(SETUP_ENGINE_DIGEST) == 64
    assert actions[0].safety == ActionSafetyV1.SAFE
    assert (
        next(item for item in actions if item.code == SetupActionCodeV1.INSTALL_AI_PROVIDER).safety
        == ActionSafetyV1.EXTERNAL
    )


def test_existing_knowledge_store_requires_current_canonical_snapshot(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """A readable old store must not suppress the canonical rebuild step."""
    from app.knowledge import KnowledgeManifestV1, RepositorySnapshotV1

    engine = SetupEngineV1(_paths(tmp_path))
    action = SetupActionV1(SetupActionCodeV1.BUILD_KNOWLEDGE, ActionSafetyV1.SAFE, "x", None)
    current = RepositorySnapshotV1("b" * 40, True)
    old = KnowledgeManifestV1(RepositorySnapshotV1("a" * 40, True), (), "fixture")
    monkeypatch.setattr("app.knowledge.read_store", lambda _path: ((), old))
    monkeypatch.setattr("app.knowledge.committed_snapshot", lambda _root: current)
    assert engine._check(action) is False

    matching = KnowledgeManifestV1(current, (), "fixture")
    monkeypatch.setattr("app.knowledge.read_store", lambda _path: ((), matching))
    assert engine._check(action) is True


def test_dry_run_is_non_mutating_and_external_needs_separate_approval(tmp_path: Path) -> None:
    engine = FakeSetupEngine(_paths(tmp_path))
    dry = engine.execute(
        approval=SetupApprovalV1(), ai_profile=AIModelProfileV1.LIGHT, dry_run=True
    )
    assert not engine.applied
    assert all(item.message == "DRY_RUN" for item in dry.steps)
    run = engine.execute(
        approval=SetupApprovalV1(allow_safe=True), ai_profile=AIModelProfileV1.LIGHT
    )
    assert SetupActionCodeV1.INITIALIZE_DIRECTORIES in engine.applied
    assert any(item.message == "APPROVAL_REQUIRED:EXTERNAL" for item in run.steps)
    assert SetupActionCodeV1.INSTALL_AI_PROVIDER not in engine.applied


def test_setup_progress_receives_start_and_result_for_each_action(tmp_path: Path) -> None:
    engine = FakeSetupEngine(_paths(tmp_path))
    events: list[tuple[SetupActionCodeV1, str]] = []

    def progress(action, result):  # type: ignore[no-untyped-def]
        events.append((action.code, "START" if result is None else result.state.value))

    run = engine.execute(
        approval=SetupApprovalV1(allow_safe=True, allow_external=True),
        ai_profile=AIModelProfileV1.BALANCED,
        progress=progress,
    )

    assert len(events) == len(run.steps) * 2
    assert all(events[index][1] == "START" for index in range(0, len(events), 2))
    assert all(events[index][1] == "COMPLETED" for index in range(1, len(events), 2))


def test_clean_machine_second_run_and_resume_do_not_duplicate_work(tmp_path: Path) -> None:
    engine = FakeSetupEngine(_paths(tmp_path))
    approval = SetupApprovalV1(allow_safe=True, allow_external=True)
    first = engine.execute(approval=approval, ai_profile=AIModelProfileV1.BALANCED)
    assert all(item.state.value == "COMPLETED" for item in first.steps)
    count = len(engine.applied)
    second = engine.execute(approval=approval, ai_profile=AIModelProfileV1.BALANCED)
    assert len(engine.applied) == count
    assert all(item.message == "ALREADY_READY" for item in second.steps)

    failed = FakeSetupEngine(
        _paths(tmp_path / "failure"), fail=SetupActionCodeV1.INSTALL_AI_PROVIDER
    )
    result = failed.execute(approval=approval, ai_profile=AIModelProfileV1.LIGHT)
    assert result.steps[-1].state.value == "FAILED"
    failed.fail = None
    resumed = failed.execute(approval=approval, ai_profile=AIModelProfileV1.LIGHT)
    assert resumed.steps[-1].code == SetupActionCodeV1.PROJECT_QA_SMOKE
    assert resumed.steps[-1].state.value == "COMPLETED"


def test_ollama_installer_is_a_provider_aware_offline_planner(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("app.setup_engine.shutil.which", lambda _: None)
    installer = OllamaInstallerV1()
    assert installer.detect() == ComponentStateV1.NOT_INSTALLED
    action = installer.plan_install()
    assert action.safety == ActionSafetyV1.EXTERNAL
    assert action.source == "https://ollama.com/download"


def test_bootstrap_scripts_hand_control_to_chappie_engine() -> None:
    shell = Path("setup.sh").read_text(encoding="utf-8")
    powershell = Path("setup.ps1").read_text(encoding="utf-8")
    assert "Python 3.13" in shell and '"$ROOT/.venv/bin/chappie" setup' in shell
    assert "Python 3.13" in powershell and "chappie.exe') @setupArgs" in powershell
    assert "sudo" not in shell.casefold() + powershell.casefold()


class _FakeWindowsInstaller(WindowsOllamaInstallerV1):
    def __init__(
        self,
        events: list[str],
        *,
        installed: bool,
        model_installed: bool = False,
        pull_fails: bool = False,
        verification_fails: bool = False,
    ) -> None:
        self.events = events
        self.installed = installed
        self.model_installed = model_installed
        self.pull_fails = pull_fails
        self.verification_fails = verification_fails
        self.install_calls = 0
        self.model_checks = 0
        self.pull_calls = 0

    def executable(self) -> Path | None:
        self.events.append("PROVIDER_EXECUTABLE_VERIFIED")
        return (
            Path("C:/Users/test/AppData/Local/Programs/Ollama/ollama.exe")
            if self.installed
            else None
        )

    def version(self) -> str | None:
        self.events.append("PROVIDER_VERSION_VERIFIED")
        return "ollama version 0.test" if self.installed else None

    def install(
        self, *, allow_external: bool, allow_privileged: bool = False
    ) -> ExternalActionResultV1:
        self.install_calls += 1
        self.events.append("PROVIDER_INSTALLED")
        self.installed = True
        return ExternalActionResultV1(
            "INSTALL_OLLAMA",
            ExternalActionStatusV1.COMPLETED,
            "Windows",
            "fixture",
        )

    def model_available(self, model: str) -> bool:
        self.model_checks += 1
        self.events.append("MODEL_VERIFIED" if self.model_installed else "MODEL_CHECKED")
        return self.model_installed and not self.verification_fails

    def pull_model(self, model: str, *, allow_external: bool) -> ExternalActionResultV1:
        self.pull_calls += 1
        self.events.append("MODEL_PULL_STARTED")
        if not self.pull_fails:
            self.model_installed = True
        return ExternalActionResultV1(
            "PULL_MODEL",
            ExternalActionStatusV1.FAILED if self.pull_fails else ExternalActionStatusV1.COMPLETED,
            "Windows",
            "fixture",
        )


class _FakeReadinessCoordinator:
    def __init__(self, events: list[str], state: OllamaReadinessStateV1) -> None:
        self.events = events
        self.state = state
        self.executable = "ollama"
        self.calls = 0

    def ensure_ready(self) -> OllamaReadinessResultV1:
        self.calls += 1
        self.events.append("READINESS_COORDINATOR_CALLED")
        return OllamaReadinessResultV1(self.state, "http://localhost:11434")


class _FakeSetupSmoke:
    def __init__(self, states: list[AISetupSmokeStateV1], events: list[str]) -> None:
        self.states = iter(states)
        self.events = events
        self.calls = 0

    def run(self, config: ChappieLocalConfigV1) -> AISetupSmokeResultV1:
        self.calls += 1
        self.events.append("AI_SMOKE")
        return AISetupSmokeResultV1(next(self.states), config.provider, config.model)


class _ProviderOnlyEngine(SetupEngineV1):
    def plan(self, *, developer: bool = False, ai_profile: AIModelProfileV1 | None = None):
        return (
            SetupActionV1(
                SetupActionCodeV1.INSTALL_AI_PROVIDER,
                ActionSafetyV1.EXTERNAL,
                "fixture provider",
                None,
            ),
        )


class _AiSetupEngine(SetupEngineV1):
    def __init__(self, *args, events: list[str], **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self.events = events

    def plan(self, *, developer: bool = False, ai_profile: AIModelProfileV1 | None = None):
        return (
            SetupActionV1(
                SetupActionCodeV1.INSTALL_AI_PROVIDER,
                ActionSafetyV1.EXTERNAL,
                "fixture provider",
                None,
            ),
            SetupActionV1(
                SetupActionCodeV1.INSTALL_AI_MODEL,
                ActionSafetyV1.EXTERNAL,
                "fixture model",
                None,
                depends_on=(SetupActionCodeV1.INSTALL_AI_PROVIDER,),
            ),
            SetupActionV1(
                SetupActionCodeV1.VERIFY_AI,
                ActionSafetyV1.EXTERNAL,
                "fixture smoke",
                None,
                depends_on=(SetupActionCodeV1.INSTALL_AI_MODEL,),
            ),
        )

    def _bind_config(self, model: str, profile: AIModelProfileV1 | None) -> None:
        self.events.append("CONFIG_WRITTEN")
        super()._bind_config(model, profile)


class _FullAiSetupEngine(_AiSetupEngine):
    def plan(self, *, developer: bool = False, ai_profile: AIModelProfileV1 | None = None):
        return super().plan(developer=developer, ai_profile=ai_profile) + (
            SetupActionV1(
                SetupActionCodeV1.PROJECT_QA_SMOKE,
                ActionSafetyV1.EXTERNAL,
                "fixture Project QA smoke",
                None,
                depends_on=(SetupActionCodeV1.VERIFY_AI,),
            ),
        )


class _FakeProjectQASmoke:
    def __init__(self, states: list[ProjectQASetupSmokeStateV1], events: list[str]) -> None:
        self.states = iter(states)
        self.events = events
        self.calls = 0

    def run(
        self, config: ChappieLocalConfigV1, knowledge_store: Path
    ) -> ProjectQASetupSmokeResultV1:
        self.calls += 1
        self.events.append("PROJECT_QA_SMOKE")
        return ProjectQASetupSmokeResultV1(next(self.states))


def test_existing_windows_provider_requires_ready_coordinator(tmp_path: Path) -> None:
    events: list[str] = []
    coordinator = _FakeReadinessCoordinator(events, OllamaReadinessStateV1.READY)
    result = _ProviderOnlyEngine(
        _paths(tmp_path / "run"),
        windows_installer=_FakeWindowsInstaller(events, installed=True),
        ollama_readiness_coordinator=coordinator,
    ).execute(approval=SetupApprovalV1(allow_external=True), ai_profile=AIModelProfileV1.BALANCED)

    assert result.steps[0].state == SetupStepStateV1.SKIPPED
    assert result.steps[0].message == "ALREADY_READY"
    assert coordinator.calls == 1
    assert events == [
        "PROVIDER_EXECUTABLE_VERIFIED",
        "PROVIDER_VERSION_VERIFIED",
        "READINESS_COORDINATOR_CALLED",
    ]


def test_existing_windows_provider_non_ready_is_not_false_ready(tmp_path: Path) -> None:
    events: list[str] = []
    coordinator = _FakeReadinessCoordinator(events, OllamaReadinessStateV1.NOT_READY)
    result = _ProviderOnlyEngine(
        _paths(tmp_path),
        windows_installer=_FakeWindowsInstaller(events, installed=True),
        ollama_readiness_coordinator=coordinator,
    ).execute(approval=SetupApprovalV1(allow_external=True), ai_profile=AIModelProfileV1.BALANCED)

    assert result.steps[0].state == SetupStepStateV1.FAILED
    assert coordinator.calls == 1


def test_windows_provider_without_coordinator_fails_closed(tmp_path: Path) -> None:
    events: list[str] = []
    result = _ProviderOnlyEngine(
        _paths(tmp_path),
        windows_installer=_FakeWindowsInstaller(events, installed=True),
    ).execute(approval=SetupApprovalV1(allow_external=True), ai_profile=AIModelProfileV1.BALANCED)

    assert result.steps[0].state == SetupStepStateV1.FAILED
    assert "READINESS_COORDINATOR_CALLED" not in events


def test_new_windows_provider_installs_then_requires_ready_coordinator(tmp_path: Path) -> None:
    events: list[str] = []
    coordinator = _FakeReadinessCoordinator(events, OllamaReadinessStateV1.READY)
    engine = _ProviderOnlyEngine(
        _paths(tmp_path),
        windows_installer=_FakeWindowsInstaller(events, installed=False),
        ollama_readiness_coordinator=coordinator,
    )

    result = engine.execute(
        approval=SetupApprovalV1(allow_external=True), ai_profile=AIModelProfileV1.BALANCED
    )

    assert result.steps == (
        SetupStepResultV1(
            SetupActionCodeV1.INSTALL_AI_PROVIDER,
            SetupStepStateV1.COMPLETED,
            "VERIFIED",
            result.steps[0].duration_ms,
        ),
    )
    assert coordinator.calls == 1
    assert events == [
        "PROVIDER_EXECUTABLE_VERIFIED",
        "PROVIDER_VERSION_VERIFIED",
        "PROVIDER_INSTALLED",
        "PROVIDER_EXECUTABLE_VERIFIED",
        "PROVIDER_VERSION_VERIFIED",
        "READINESS_COORDINATOR_CALLED",
    ]
    assert coordinator.executable.endswith("ollama.exe")


def _ai_engine(
    tmp_path: Path,
    events: list[str],
    *,
    readiness: OllamaReadinessStateV1,
    model_installed: bool = False,
    pull_fails: bool = False,
    verification_fails: bool = False,
    smoke=lambda _: True,
) -> tuple[_AiSetupEngine, _FakeWindowsInstaller, _FakeReadinessCoordinator]:
    installer = _FakeWindowsInstaller(
        events,
        installed=True,
        model_installed=model_installed,
        pull_fails=pull_fails,
        verification_fails=verification_fails,
    )
    coordinator = _FakeReadinessCoordinator(events, readiness)
    return (
        _AiSetupEngine(
            _paths(tmp_path),
            events=events,
            windows_installer=installer,
            ollama_readiness_coordinator=coordinator,
            smoke=smoke,
        ),
        installer,
        coordinator,
    )


def test_non_ready_provider_cannot_check_pull_bind_or_smoke_model(tmp_path: Path) -> None:
    events: list[str] = []
    smoke_calls: list[object] = []
    engine, installer, _ = _ai_engine(
        tmp_path,
        events,
        readiness=OllamaReadinessStateV1.NOT_READY,
        model_installed=True,
        smoke=smoke_calls.append,
    )
    engine._bind_config("qwen3:8b", AIModelProfileV1.BALANCED)
    events.clear()

    result = engine.execute(
        approval=SetupApprovalV1(allow_external=True), ai_profile=AIModelProfileV1.BALANCED
    )

    assert result.steps[0].state == SetupStepStateV1.FAILED
    assert installer.model_checks == 0
    assert installer.pull_calls == 0
    assert not smoke_calls
    assert "CONFIG_WRITTEN" not in events


def test_stale_provider_completion_does_not_bypass_current_runtime_readiness(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    engine, installer, _ = _ai_engine(
        tmp_path, events, readiness=OllamaReadinessStateV1.NOT_READY, model_installed=True
    )
    engine._persist(
        SetupRunStateV1(
            SETUP_ENGINE_IDENTITY,
            SETUP_ENGINE_DIGEST,
            (
                SetupStepResultV1(
                    SetupActionCodeV1.INSTALL_AI_PROVIDER,
                    SetupStepStateV1.COMPLETED,
                    "historical fixture",
                ),
            ),
        )
    )

    result = engine.execute(
        approval=SetupApprovalV1(allow_external=True), ai_profile=AIModelProfileV1.BALANCED
    )

    assert result.steps[0].state == SetupStepStateV1.FAILED
    assert installer.model_checks == 0
    assert installer.pull_calls == 0


def test_ready_provider_allows_verified_model_then_config(tmp_path: Path) -> None:
    events: list[str] = []
    engine, installer, _ = _ai_engine(
        tmp_path,
        events,
        readiness=OllamaReadinessStateV1.READY,
        smoke=lambda _: events.append("AI_SMOKE") or True,
    )

    result = engine.execute(
        approval=SetupApprovalV1(allow_external=True), ai_profile=AIModelProfileV1.BALANCED
    )

    assert [step.state for step in result.steps] == [
        SetupStepStateV1.SKIPPED,
        SetupStepStateV1.COMPLETED,
        SetupStepStateV1.COMPLETED,
    ]
    assert installer.pull_calls == 1
    assert events.index("READINESS_COORDINATOR_CALLED") < events.index("MODEL_CHECKED")
    assert events.index("MODEL_PULL_STARTED") < events.index("MODEL_VERIFIED")
    assert events.index("MODEL_VERIFIED") < events.index("CONFIG_WRITTEN")
    assert events.index("CONFIG_WRITTEN") < events.index("AI_SMOKE")


def test_model_pull_failure_never_binds_config_or_smokes(tmp_path: Path) -> None:
    events: list[str] = []
    smoke_calls: list[object] = []
    engine, installer, _ = _ai_engine(
        tmp_path,
        events,
        readiness=OllamaReadinessStateV1.READY,
        pull_fails=True,
        smoke=smoke_calls.append,
    )

    result = engine.execute(
        approval=SetupApprovalV1(allow_external=True), ai_profile=AIModelProfileV1.BALANCED
    )

    assert result.steps[-1].code == SetupActionCodeV1.INSTALL_AI_MODEL
    assert result.steps[-1].state == SetupStepStateV1.FAILED
    assert installer.pull_calls == 1
    assert "CONFIG_WRITTEN" not in events
    assert not smoke_calls


def test_existing_model_binds_config_without_pull_after_provider_ready(tmp_path: Path) -> None:
    events: list[str] = []
    engine, installer, _ = _ai_engine(
        tmp_path, events, readiness=OllamaReadinessStateV1.READY, model_installed=True
    )

    result = engine.execute(
        approval=SetupApprovalV1(allow_external=True), ai_profile=AIModelProfileV1.BALANCED
    )

    assert result.steps[1].state == SetupStepStateV1.COMPLETED
    assert installer.pull_calls == 0
    assert "CONFIG_WRITTEN" in events


def test_actual_windows_installer_completes_missing_model_setup_path(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    local_app_data = tmp_path / "local-app-data"
    executable = local_app_data / "Programs" / "Ollama" / "ollama.exe"
    executable.parent.mkdir(parents=True)
    executable.touch()
    installed_models: set[str] = set()
    calls: list[list[str]] = []

    def runner(argv: list[str], **kwargs: object):
        calls.append(argv)
        if argv[-1] == "--version":
            return subprocess.CompletedProcess(argv, 0, "ollama version fixture", "")
        if argv[-1] == "ls":
            rows = "".join(f"{model} fixture 1 GB now\n" for model in sorted(installed_models))
            return subprocess.CompletedProcess(argv, 0, f"NAME ID SIZE MODIFIED\n{rows}", "")
        assert argv == [str(executable), "pull", "qwen3:8b"]
        installed_models.add("qwen3:8b")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("app.windows_ollama.shutil.which", lambda _: None)
    events: list[str] = []
    engine = _AiSetupEngine(
        _paths(tmp_path / "actual-production"),
        events=events,
        windows_installer=WindowsOllamaInstallerV1(local_app_data=local_app_data, runner=runner),
        ollama_readiness_coordinator=_FakeReadinessCoordinator(
            events, OllamaReadinessStateV1.READY
        ),
        smoke=lambda _: True,
    )

    result = engine.execute(
        approval=SetupApprovalV1(allow_external=True), ai_profile=AIModelProfileV1.BALANCED
    )

    assert [step.state for step in result.steps] == [
        SetupStepStateV1.SKIPPED,
        SetupStepStateV1.COMPLETED,
        SetupStepStateV1.COMPLETED,
    ]
    assert calls.count([str(executable), "pull", "qwen3:8b"]) == 1
    assert installed_models == {"qwen3:8b"}


def test_model_command_success_without_exact_verification_never_binds_config(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    smoke_calls: list[object] = []
    engine, installer, _ = _ai_engine(
        tmp_path,
        events,
        readiness=OllamaReadinessStateV1.READY,
        verification_fails=True,
        smoke=smoke_calls.append,
    )

    result = engine.execute(
        approval=SetupApprovalV1(allow_external=True), ai_profile=AIModelProfileV1.BALANCED
    )

    assert result.steps[-1].state == SetupStepStateV1.FAILED
    assert installer.pull_calls == 1
    assert "CONFIG_WRITTEN" not in events
    assert not smoke_calls


def test_model_failure_resume_revalidates_provider_and_then_binds_config(tmp_path: Path) -> None:
    events: list[str] = []
    engine, installer, coordinator = _ai_engine(
        tmp_path, events, readiness=OllamaReadinessStateV1.READY, pull_fails=True
    )
    approval = SetupApprovalV1(allow_external=True)
    first = engine.execute(approval=approval, ai_profile=AIModelProfileV1.BALANCED)
    assert first.steps[-1].state == SetupStepStateV1.FAILED
    assert "CONFIG_WRITTEN" not in events

    installer.pull_fails = False
    events.clear()
    second = engine.execute(approval=approval, ai_profile=AIModelProfileV1.BALANCED)

    assert [step.state for step in second.steps] == [
        SetupStepStateV1.SKIPPED,
        SetupStepStateV1.COMPLETED,
        SetupStepStateV1.COMPLETED,
    ]
    assert installer.install_calls == 0
    assert coordinator.calls == 2
    assert events.index("READINESS_COORDINATOR_CALLED") < events.index("MODEL_PULL_STARTED")
    assert "CONFIG_WRITTEN" in events


def test_interrupted_after_model_verification_resumes_without_pull(tmp_path: Path) -> None:
    events: list[str] = []
    engine, installer, _ = _ai_engine(
        tmp_path, events, readiness=OllamaReadinessStateV1.READY, model_installed=True
    )

    result = engine.execute(
        approval=SetupApprovalV1(allow_external=True), ai_profile=AIModelProfileV1.BALANCED
    )

    assert result.steps[1].state == SetupStepStateV1.COMPLETED
    assert installer.pull_calls == 0
    assert "CONFIG_WRITTEN" in events


def test_semantically_valid_config_is_not_rewritten_on_second_run(tmp_path: Path) -> None:
    events: list[str] = []
    engine, installer, coordinator = _ai_engine(
        tmp_path, events, readiness=OllamaReadinessStateV1.READY, model_installed=True
    )
    engine._bind_config("qwen3:8b", AIModelProfileV1.BALANCED)
    original = engine.paths.config_file.read_bytes()
    events.clear()

    result = engine.execute(
        approval=SetupApprovalV1(allow_external=True), ai_profile=AIModelProfileV1.BALANCED
    )

    assert [step.message for step in result.steps] == [
        "ALREADY_READY",
        "ALREADY_READY",
        "VERIFIED",
    ]
    assert installer.install_calls == 0
    assert installer.pull_calls == 0
    assert coordinator.calls == 1
    assert "CONFIG_WRITTEN" not in events
    assert engine.paths.config_file.read_bytes() == original


def test_different_old_config_switches_only_after_selected_model_is_verified(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    engine, installer, _ = _ai_engine(
        tmp_path, events, readiness=OllamaReadinessStateV1.READY, model_installed=True
    )
    engine._bind_config("qwen3:4b", AIModelProfileV1.LIGHT)
    events.clear()

    engine.execute(
        approval=SetupApprovalV1(allow_external=True), ai_profile=AIModelProfileV1.BALANCED
    )

    config = load_local_config(engine.paths)
    assert config is not None
    assert (config.provider, config.model, config.enabled_capabilities) == (
        "ollama",
        "qwen3:8b",
        (AIModelProfileV1.BALANCED.value,),
    )
    assert installer.pull_calls == 0
    assert events.index("MODEL_VERIFIED") < events.index("CONFIG_WRITTEN")


def test_atomic_config_binding_leaves_previous_config_unchanged_on_replace_failure(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    events: list[str] = []
    engine, _, _ = _ai_engine(tmp_path, events, readiness=OllamaReadinessStateV1.READY)
    engine._bind_config("qwen3:4b", AIModelProfileV1.LIGHT)
    original = engine.paths.config_file.read_bytes()
    original_replace = Path.replace

    def fail_config_replace(source: Path, target: Path) -> Path:
        if source == engine.paths.config_file.with_suffix(".tmp"):
            raise OSError("interrupted fixture")
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", fail_config_replace)

    try:
        engine._bind_config("qwen3:8b", AIModelProfileV1.BALANCED)
    except OSError:
        pass
    else:
        raise AssertionError("fixture replace must fail")

    assert engine.paths.config_file.read_bytes() == original
    assert engine.paths.config_file.with_suffix(".tmp").is_file()


def test_stale_config_cannot_replace_missing_model_verification(tmp_path: Path) -> None:
    events: list[str] = []
    engine, installer, _ = _ai_engine(
        tmp_path,
        events,
        readiness=OllamaReadinessStateV1.READY,
        pull_fails=True,
    )
    engine._bind_config("qwen3:8b", AIModelProfileV1.BALANCED)
    events.clear()

    result = engine.execute(
        approval=SetupApprovalV1(allow_external=True), ai_profile=AIModelProfileV1.BALANCED
    )

    assert result.steps[-1].state == SetupStepStateV1.FAILED
    assert installer.pull_calls == 1
    assert "CONFIG_WRITTEN" not in events


def test_smoke_failure_resume_reuses_provider_model_and_config(tmp_path: Path) -> None:
    events: list[str] = []
    smoke = _FakeSetupSmoke([AISetupSmokeStateV1.PROVIDER_ERROR, AISetupSmokeStateV1.VALID], events)
    engine, installer, _ = _ai_engine(
        tmp_path,
        events,
        readiness=OllamaReadinessStateV1.READY,
        smoke=smoke,
    )
    approval = SetupApprovalV1(allow_external=True)

    first = engine.execute(approval=approval, ai_profile=AIModelProfileV1.BALANCED)
    assert first.steps[-1].state == SetupStepStateV1.FAILED
    assert installer.install_calls == 0
    assert installer.pull_calls == 1
    config_after_failure = engine.paths.config_file.read_bytes()

    events.clear()
    second = engine.execute(approval=approval, ai_profile=AIModelProfileV1.BALANCED)
    assert second.steps[-1].state == SetupStepStateV1.COMPLETED
    assert installer.install_calls == 0
    assert installer.pull_calls == 1
    assert smoke.calls == 2
    assert engine.paths.config_file.read_bytes() == config_after_failure


def test_missing_smoke_fails_closed_after_provider_and_model_are_valid(tmp_path: Path) -> None:
    events: list[str] = []
    engine, _, _ = _ai_engine(
        tmp_path,
        events,
        readiness=OllamaReadinessStateV1.READY,
        model_installed=True,
        smoke=None,
    )

    result = engine.execute(
        approval=SetupApprovalV1(allow_external=True), ai_profile=AIModelProfileV1.BALANCED
    )

    assert result.steps[-1].code == SetupActionCodeV1.VERIFY_AI
    assert result.steps[-1].state == SetupStepStateV1.FAILED


def test_project_qa_failure_resume_does_not_reinstall_or_redownload(tmp_path: Path) -> None:
    events: list[str] = []
    installer = _FakeWindowsInstaller(events, installed=True)
    coordinator = _FakeReadinessCoordinator(events, OllamaReadinessStateV1.READY)
    project_qa = _FakeProjectQASmoke(
        [ProjectQASetupSmokeStateV1.PROVIDER_ERROR, ProjectQASetupSmokeStateV1.VALID], events
    )
    engine = _FullAiSetupEngine(
        _paths(tmp_path),
        events=events,
        windows_installer=installer,
        ollama_readiness_coordinator=coordinator,
        smoke=lambda _: True,
        project_qa_smoke=project_qa,
    )
    approval = SetupApprovalV1(allow_external=True)

    first = engine.execute(approval=approval, ai_profile=AIModelProfileV1.BALANCED)
    assert first.steps[-1].state == SetupStepStateV1.FAILED
    assert installer.pull_calls == 1

    second = engine.execute(approval=approval, ai_profile=AIModelProfileV1.BALANCED)
    assert second.steps[-1].state == SetupStepStateV1.COMPLETED
    assert installer.install_calls == 0
    assert installer.pull_calls == 1
    assert project_qa.calls == 2
