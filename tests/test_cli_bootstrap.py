"""Deterministic contract coverage for the offline Chappie product CLI."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from app.bootstrap import (
    INSTALLATION_CONTRACT_DIGEST,
    INSTALLATION_IDENTITY,
    AIModelCatalogEntryV1,
    AIModelCatalogV1,
    AIModelProfileV1,
    ChappieLocalConfigV1,
    ChappiePaths,
    ComponentStateV1,
    ComponentStatusV1,
    HardwareCapabilityV1,
    HardwareProfileV1,
    InstallationStateV1,
    RemediationCodeV1,
    aggregate_installation_state,
    collect_status,
    detect_hardware,
    doctor,
    load_local_config,
    recommend_model_profiles,
    setup_plan,
)
from app.cli import main
from app.setup_engine import (
    SetupActionCodeV1,
    SetupApprovalV1,
    SetupRunStateV1,
    SetupStepResultV1,
    SetupStepStateV1,
)


def _paths(tmp_path: Path) -> ChappiePaths:
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    (root / "pyproject.toml").write_text("[project]\nname = 'fixture'\n")
    return ChappiePaths.resolve(root)


def test_installation_contract_is_versioned_and_deterministic() -> None:
    assert INSTALLATION_IDENTITY == "chappie_installation_state/1.0.0"
    assert len(INSTALLATION_CONTRACT_DIGEST) == 64


def test_project_declares_official_console_entry_point() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["scripts"]["chappie"] == "app.cli:main"


def test_windows_bootstrap_hands_off_to_authorized_venv_cli_without_ai_duplication() -> None:
    script = Path("setup.ps1").read_text(encoding="utf-8")
    assert "$setupArgs = @('setup', '--yes', '--interactive') + $args" in script
    assert ".venv\\Scripts\\chappie.exe') @setupArgs" in script
    assert "$setupExit = $LASTEXITCODE" in script and "exit $setupExit" in script
    assert "if (-not (Test-Path $venvPython))" in script
    assert "OllamaProvider" not in script and "ollama pull" not in script


class _DummySetupEngine:
    def __init__(
        self, profile_to_actions: dict[AIModelProfileV1 | None, tuple[SetupActionCodeV1, ...]]
    ) -> None:
        self.profile_to_actions = profile_to_actions
        self.executed: list[AIModelProfileV1 | None] = []

    def plan(self, *, developer: bool = False, ai_profile: AIModelProfileV1 | None = None):
        return ()

    def required_external_actions(self, *, ai_profile: AIModelProfileV1 | None = None):
        return self.profile_to_actions.get(ai_profile or AIModelProfileV1.BALANCED, ())

    def execute(
        self,
        *,
        approval: SetupApprovalV1,
        developer: bool = False,
        ai_profile: AIModelProfileV1 | None = None,
        dry_run: bool = False,
        progress=None,
    ):
        del approval, developer, dry_run, progress
        self.executed.append(ai_profile)
        return SetupRunStateV1(
            "chappie_setup_engine/1.0.0",
            "0000000000000000000000000000000000000000000000000000000000000000",
            (
                SetupStepResultV1(
                    SetupActionCodeV1.INSTALL_AI_PROVIDER,
                    SetupStepStateV1.SKIPPED,
                    "SKIPPED",
                ),
            ),
        )


def test_setup_profile_prompt_is_cli_owned_and_plans_never_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from app import cli

    monkeypatch.setattr("builtins.input", lambda _: "LIGHT")
    assert cli._interactive_ai_profile(_paths(tmp_path), cli._parser()) == AIModelProfileV1.LIGHT
    (tmp_path / "plan").mkdir()
    assert main(["setup", "--plan"], paths=_paths(tmp_path / "plan")) == 0
    assert "actions" in capsys.readouterr().out


def test_cli_reuses_verified_existing_ai_config_without_profile_prompt(
    tmp_path: Path, monkeypatch, capsys
):
    paths = _paths(tmp_path)
    paths.config_file.parent.mkdir(parents=True)
    paths.config_file.write_text(
        '{"schema":"chappie_local_config/1.0.0","setup_version":"1.0.0","provider":"ollama","model":"qwen3:8b","enabled_capabilities":["balanced"]}\n'
    )
    from app import cli

    engine = _DummySetupEngine({AIModelProfileV1.BALANCED: ()})
    monkeypatch.setattr(cli, "_setup_engine", lambda *_args, **_kwargs: engine)
    monkeypatch.setattr(
        "builtins.input", lambda _: (_ for _ in ()).throw(AssertionError("prompted unexpectedly"))
    )

    assert main(["setup", "--interactive", "--yes"], paths=paths) == 0
    output = capsys.readouterr().out
    assert "Current AI configuration:" in output
    assert "Profile: BALANCED" in output
    assert "External work   NOT REQUIRED" in output
    assert "CHAPPIE SETUP COMPLETE" in output
    assert engine.executed == [AIModelProfileV1.BALANCED]


def test_cli_reuses_balanced_config_even_when_hardware_prefers_light(tmp_path: Path, monkeypatch):
    paths = _paths(tmp_path)
    paths.config_file.parent.mkdir(parents=True)
    paths.config_file.write_text(
        '{"schema":"chappie_local_config/1.0.0","setup_version":"1.0.0","provider":"ollama","model":"qwen3:8b","enabled_capabilities":["balanced"]}\n'
    )
    from app import cli

    engine = _DummySetupEngine({AIModelProfileV1.BALANCED: ()})
    monkeypatch.setattr(cli, "_setup_engine", lambda *_args, **_kwargs: engine)
    monkeypatch.setattr(
        "app.bootstrap.recommend_model_profiles",
        lambda *_a, **_k: {"recommended": ("qwen3:4b",), "unsupported": ()},
    )
    monkeypatch.setattr(
        "builtins.input", lambda _: (_ for _ in ()).throw(AssertionError("prompted unexpectedly"))
    )

    assert main(["setup", "--interactive", "--yes"], paths=paths) == 0
    assert engine.executed == [AIModelProfileV1.BALANCED]


def test_cli_external_consent_required_for_fresh_windows_like_state(tmp_path: Path, monkeypatch):
    from app import cli

    engine = _DummySetupEngine(
        {
            AIModelProfileV1.BALANCED: (
                SetupActionCodeV1.INSTALL_AI_PROVIDER,
                SetupActionCodeV1.INSTALL_AI_MODEL,
            )
        }
    )
    monkeypatch.setattr(cli, "_setup_engine", lambda *_args, **_kwargs: engine)

    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert (
        main(
            ["setup", "--interactive", "--yes", "--ai-profile", "balanced"], paths=_paths(tmp_path)
        )
        == 1
    )
    assert engine.executed == []


def test_cli_freshly_prompts_for_provider_and_model_if_missing(tmp_path: Path, monkeypatch):
    from app import cli

    engine = _DummySetupEngine(
        {
            AIModelProfileV1.BALANCED: (
                SetupActionCodeV1.INSTALL_AI_PROVIDER,
                SetupActionCodeV1.INSTALL_AI_MODEL,
            )
        }
    )
    monkeypatch.setattr(cli, "_setup_engine", lambda *_args, **_kwargs: engine)

    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert main(["setup", "--non-interactive", "--yes"], paths=_paths(tmp_path)) == 1


def test_cli_noninteractive_external_needed_fails_with_json_payload(
    tmp_path: Path, monkeypatch, capsys
):
    from app import cli

    engine = _DummySetupEngine(
        {AIModelProfileV1.BALANCED: (SetupActionCodeV1.INSTALL_AI_PROVIDER,)}
    )
    monkeypatch.setattr(cli, "_setup_engine", lambda *_args, **_kwargs: engine)

    assert (
        main(
            ["setup", "--non-interactive", "--json", "--ai-profile", "balanced", "--yes"],
            paths=_paths(tmp_path),
        )
        == 1
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "SETUP_REQUIRED"
    assert payload["external_authorization_required"]


def test_cli_explicit_profile_overrides_existing_config_and_requests_external_when_needed(
    tmp_path: Path, monkeypatch
):
    paths = _paths(tmp_path)
    paths.config_file.parent.mkdir(parents=True)
    paths.config_file.write_text(
        '{"schema":"chappie_local_config/1.0.0","setup_version":"1.0.0","provider":"ollama","model":"qwen3:8b","enabled_capabilities":["balanced"]}\n'
    )
    from app import cli

    engine = _DummySetupEngine(
        {
            AIModelProfileV1.BALANCED: (SetupActionCodeV1.INSTALL_AI_PROVIDER,),
            AIModelProfileV1.ADVANCED: (SetupActionCodeV1.INSTALL_AI_MODEL,),
        }
    )
    monkeypatch.setattr(cli, "_setup_engine", lambda *_args, **_kwargs: engine)
    monkeypatch.setattr("builtins.input", lambda _: "n")

    assert main(["setup", "--interactive", "--yes", "--ai-profile", "advanced"], paths=paths) == 1
    assert engine.executed == []


@pytest.mark.parametrize(
    ("system", "memory", "cpus", "expected"),
    [
        ("Darwin", 16 * 1024**3, 8, HardwareCapabilityV1.BALANCED),
        ("Windows", 64 * 1024**3, 16, HardwareCapabilityV1.HIGH),
        ("Linux", 4 * 1024**3, 2, HardwareCapabilityV1.MINIMAL),
        ("Linux", None, 8, HardwareCapabilityV1.UNKNOWN),
    ],
)
def test_hardware_profiles_are_best_effort_and_conservative(
    tmp_path: Path, system: str, memory: int | None, cpus: int, expected: HardwareCapabilityV1
) -> None:
    profile = detect_hardware(
        _paths(tmp_path), system=system, machine="fixture", memory_bytes=memory, cpu_count=cpus
    )
    assert profile.capability == expected


def test_hardware_fixtures_keep_gpu_unknown_separate_from_failure(tmp_path: Path) -> None:
    windows = detect_hardware(
        _paths(tmp_path),
        system="Windows",
        machine="AMD64",
        memory_bytes=64 * 1024**3,
        cpu_count=16,
        gpu_names=("NVIDIA RTX fixture",),
        gpu_memory_bytes=16 * 1024**3,
        cuda_available=True,
    )
    linux = detect_hardware(
        _paths(tmp_path), system="Linux", machine="x86_64", memory_bytes=16 * 1024**3, cpu_count=8
    )
    assert windows.gpu_names == ("NVIDIA RTX fixture",)
    assert windows.cuda_available is True
    assert linux.gpu_names == ()
    assert linux.cuda_available is None


def test_paths_are_portable_and_config_is_non_secret(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    assert paths.config_file == paths.root / "data/config/chappie.json"
    paths.config_file.parent.mkdir(parents=True)
    paths.config_file.write_text(
        json.dumps(ChappieLocalConfigV1(provider="ollama", model="qwen3:8b").payload())
    )
    assert load_local_config(paths) == ChappieLocalConfigV1(provider="ollama", model="qwen3:8b")
    paths.config_file.write_text('{"api_key":"secret"}')
    with pytest.raises(ValueError, match="invalid"):
        load_local_config(paths)


def test_overall_aggregation_has_stable_state_policy() -> None:
    ready = ComponentStatusV1("x", ComponentStateV1.READY, True, None, None, "ok")
    setup = ComponentStatusV1("x", ComponentStateV1.NOT_CONFIGURED, True, None, None, "missing")
    unsupported = ComponentStatusV1("x", ComponentStateV1.UNSUPPORTED, True, None, None, "bad")
    assert aggregate_installation_state((ready,)) == InstallationStateV1.READY
    assert aggregate_installation_state((setup,)) == InstallationStateV1.SETUP_REQUIRED
    assert aggregate_installation_state((unsupported,)) == InstallationStateV1.UNSUPPORTED


def test_status_doctor_and_setup_plan_are_offline_and_read_only(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    status = collect_status(paths)
    assert status.installation_contract_digest == INSTALLATION_CONTRACT_DIGEST
    assert not paths.state_root.exists()
    report = doctor(paths)
    assert report.status == status
    plan = setup_plan(paths)
    assert [step.code for step in plan.steps] == [
        "CHECK_PYTHON",
        "CREATE_VENV",
        "INSTALL_DEPENDENCIES",
        "INITIALIZE_CONFIG",
        "BUILD_KNOWLEDGE",
        "INSTALL_AI_PROVIDER",
        "SELECT_AI_MODEL",
        "VERIFY_AI",
        "FINAL_HEALTH_CHECK",
    ]
    assert all(
        step.remediation_code != RemediationCodeV1.START_AI_PROVIDER or step.code == "VERIFY_AI"
        for step in plan.steps
    )


def test_model_recommendations_do_not_require_provider_calls() -> None:
    profile = HardwareProfileV1("Linux", "x", "x86_64", None, 8, 32 * 1024**3)
    catalog = AIModelCatalogV1(
        "test",
        (
            AIModelCatalogEntryV1(
                AIModelProfileV1.LIGHT, "mock", "small", 8, 16, False, "8k", "x", True
            ),
            AIModelCatalogEntryV1(
                AIModelProfileV1.ADVANCED, "mock", "large", 64, 128, False, "8k", "x", True
            ),
        ),
    )
    result = recommend_model_profiles(profile, catalog)
    assert result["recommended"] == ("small",)
    assert result["unsupported"] == ("large",)


def test_phase_two_acceptance_hardware_recommendations_are_platform_neutral() -> None:
    from app.bootstrap import TEST_MODEL_CATALOG

    apple = HardwareProfileV1("Darwin", "x", "arm64", None, 8, 18 * 1024**3)
    rtx = HardwareProfileV1(
        "Windows", "x", "x86_64", None, 16, 32 * 1024**3, ("RTX 4070 Ti",), 12 * 1024**3, True
    )
    server = HardwareProfileV1("Linux", "x", "x86_64", None, 8, 32 * 1024**3)
    assert "qwen3:8b" in recommend_model_profiles(apple, TEST_MODEL_CATALOG)["recommended"]
    assert "qwen3:8b" in recommend_model_profiles(rtx, TEST_MODEL_CATALOG)["recommended"]
    assert "qwen3:8b" in recommend_model_profiles(server, TEST_MODEL_CATALOG)["recommended"]


def test_windows_rtx_4070_ti_is_recommended_when_windows_memory_is_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import bootstrap

    monkeypatch.setattr(bootstrap, "_memory_bytes", lambda _: 32 * 1024**3)
    monkeypatch.setattr(
        bootstrap, "_windows_gpu_info", lambda: (("NVIDIA GeForce RTX 4070 Ti",), 12 * 1024**3)
    )
    hardware = detect_hardware(_paths(tmp_path), system="Windows", machine="AMD64", cpu_count=16)
    recommendation = recommend_model_profiles(hardware, bootstrap.TEST_MODEL_CATALOG)
    assert hardware.gpu_names == ("NVIDIA GeForce RTX 4070 Ti",)
    assert hardware.gpu_memory_bytes == 12 * 1024**3
    assert "qwen3:8b" in recommendation["recommended"]


def test_unknown_windows_vram_keeps_safe_cpu_compatible_recommendations() -> None:
    profile = HardwareProfileV1(
        "Windows", "x", "x86_64", None, 12, 32 * 1024**3, ("NVIDIA fixture",), None, None
    )
    from app.bootstrap import TEST_MODEL_CATALOG

    assert "qwen3:8b" in recommend_model_profiles(profile, TEST_MODEL_CATALOG)["recommended"]


def test_cli_status_json_and_namespaces(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    paths = _paths(tmp_path)
    exit_code = main(["status", "--json"], paths=paths)
    parsed = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert parsed["installation_identity"] == INSTALLATION_IDENTITY
    assert main(["research"], paths=paths) == 1
    assert capsys.readouterr().out.strip() == "NOT_IMPLEMENTED"


def test_cli_bridges_knowledge_without_rebuilding_store(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = _paths(tmp_path)
    assert main(["knowledge", "verify", "--json"], paths=paths) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["component"] == "KNOWLEDGE"
    assert not paths.knowledge_file.exists()


def test_model_pull_progress_is_a_single_writer_stage(capsys: pytest.CaptureFixture[str]) -> None:
    from app.cli import _SetupProgress
    from app.setup_engine import ActionSafetyV1, SetupActionCodeV1, SetupActionV1

    progress = _SetupProgress(1)
    action = SetupActionV1(
        SetupActionCodeV1.INSTALL_AI_MODEL, ActionSafetyV1.EXTERNAL, "model", None
    )
    progress(action, None)
    progress.close()
    output = capsys.readouterr().out
    assert "Installing selected local model" in output
    assert "Still working" not in output


def _write_balanced_ollama_config(paths: ChappiePaths) -> None:
    paths.config_file.parent.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text(
        json.dumps(
            ChappieLocalConfigV1(
                provider="ollama", model="qwen3:8b", enabled_capabilities=("BALANCED",)
            ).payload()
        ),
        encoding="utf-8",
    )


def _component(status, name: str) -> ComponentStatusV1:
    return next(item for item in status.components if item.component == name)


def test_windows_ready_ai_status_is_observational_and_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The real Windows setup result is reported without setup side effects."""
    from app import bootstrap
    from app.knowledge import KnowledgeManifestV1, RepositorySnapshotV1
    from app.ollama_readiness import OllamaReadinessResultV1, OllamaReadinessStateV1

    paths = _paths(tmp_path)
    _write_balanced_ollama_config(paths)
    paths.knowledge_file.parent.mkdir(parents=True)
    paths.knowledge_file.touch()
    paths.research_dir.mkdir(parents=True)
    snapshot = RepositorySnapshotV1("a" * 40, True)
    manifest = KnowledgeManifestV1(snapshot, (), "fixture")
    calls: list[str] = []

    class Probe:
        def __init__(self) -> None:
            calls.append("probe-created")

        def probe(self):
            calls.append("probe")
            return OllamaReadinessResultV1(OllamaReadinessStateV1.READY, "http://localhost:11434")

        def close(self) -> None:
            calls.append("probe-closed")

    class Installer:
        def __init__(self, **_kwargs: object) -> None:
            calls.append("installer-created")

        def executable(self) -> Path:
            calls.append("executable")
            return Path("C:/fixture/ollama.exe")

        def model_available(self, model: str) -> bool:
            calls.append(f"model:{model}")
            return model == "qwen3:8b"

    hardware = HardwareProfileV1(
        "Windows",
        "fixture",
        "AMD64",
        "AMD",
        16,
        32 * 1024**3,
        ("NVIDIA GeForce RTX 4070 Ti",),
        12 * 1024**3,
        True,
        100 * 1024**3,
    )
    monkeypatch.setattr(bootstrap, "detect_hardware", lambda _paths: hardware)
    monkeypatch.setattr(
        bootstrap,
        "_repository_status",
        lambda _paths: ComponentStatusV1(
            "PROJECT_REPOSITORY", ComponentStateV1.READY, True, None, None, "clean"
        ),
    )
    monkeypatch.setattr("app.knowledge.read_store", lambda _path: ((), manifest))
    monkeypatch.setattr("app.knowledge.committed_snapshot", lambda _root: snapshot)
    monkeypatch.setattr("app.ollama_readiness.OllamaReadinessProbe", Probe)
    monkeypatch.setattr("app.windows_ollama.WindowsOllamaInstallerV1", Installer)

    status = collect_status(paths)
    assert _component(status, "KNOWLEDGE").state == ComponentStateV1.READY
    assert _component(status, "AI_PROVIDER").state == ComponentStateV1.READY
    assert _component(status, "AI_MODEL").state == ComponentStateV1.READY
    assert status.overall_state == InstallationStateV1.READY
    assert _component(status, "RESEARCH_STORAGE").state == ComponentStateV1.PARTIAL
    assert calls == [
        "installer-created",
        "executable",
        "probe-created",
        "probe",
        "probe-closed",
        "installer-created",
        "model:qwen3:8b",
    ]

    assert main(["ai", "status"], paths=paths) == 0
    output = capsys.readouterr().out
    assert "AI_PROVIDER: READY" in output
    assert "AI_MODEL: READY" in output
    assert "PROFILE: BALANCED" in output
    assert "MODEL: qwen3:8b" in output
    report = doctor(paths)
    assert not any(
        issue.remediation_code
        in {
            RemediationCodeV1.BUILD_KNOWLEDGE_STORE,
            RemediationCodeV1.START_AI_PROVIDER,
            RemediationCodeV1.INSTALL_AI_MODEL,
        }
        for issue in report.issues
    )


def test_status_provider_down_and_missing_model_get_current_remediation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.ollama_readiness import OllamaReadinessResultV1, OllamaReadinessStateV1

    paths = _paths(tmp_path)
    _write_balanced_ollama_config(paths)

    class Probe:
        def probe(self):
            return OllamaReadinessResultV1(
                OllamaReadinessStateV1.NOT_READY,
                "http://localhost:11434",
                error_category="UNAVAILABLE",
            )

        def close(self) -> None:
            pass

    class Installer:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def executable(self) -> Path:
            return Path("C:/fixture/ollama.exe")

        def model_available(self, _model: str) -> bool:
            raise AssertionError("model check while down")

    monkeypatch.setattr("app.ollama_readiness.OllamaReadinessProbe", Probe)
    monkeypatch.setattr("app.windows_ollama.WindowsOllamaInstallerV1", Installer)
    status = collect_status(paths)
    assert _component(status, "AI_PROVIDER").state == ComponentStateV1.DEGRADED
    assert status.overall_state != InstallationStateV1.READY
    assert RemediationCodeV1.START_AI_PROVIDER in {
        issue.remediation_code for issue in doctor(paths).issues
    }

    monkeypatch.setattr(
        "app.ollama_readiness.OllamaReadinessProbe",
        lambda: type(
            "Probe",
            (),
            {
                "probe": lambda self: OllamaReadinessResultV1(
                    OllamaReadinessStateV1.READY, "http://localhost:11434"
                ),
                "close": lambda self: None,
            },
        )(),
    )
    monkeypatch.setattr(Installer, "model_available", lambda _self, _model: False)
    status = collect_status(paths)
    assert _component(status, "AI_MODEL").state == ComponentStateV1.NOT_INSTALLED
    assert RemediationCodeV1.INSTALL_AI_MODEL in {
        issue.remediation_code for issue in doctor(paths).issues
    }


def test_knowledge_stale_keeps_rebuild_remediation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.knowledge import KnowledgeManifestV1, RepositorySnapshotV1

    paths = _paths(tmp_path)
    paths.knowledge_file.parent.mkdir(parents=True)
    paths.knowledge_file.touch()
    monkeypatch.setattr(
        "app.knowledge.read_store",
        lambda _path: (
            (),
            KnowledgeManifestV1(RepositorySnapshotV1("a" * 40, True), (), "fixture"),
        ),
    )
    monkeypatch.setattr(
        "app.knowledge.committed_snapshot", lambda _root: RepositorySnapshotV1("b" * 40, True)
    )
    status = collect_status(paths)
    assert _component(status, "KNOWLEDGE").state == ComponentStateV1.STALE
    assert RemediationCodeV1.BUILD_KNOWLEDGE_STORE in {
        issue.remediation_code for issue in doctor(paths).issues
    }
