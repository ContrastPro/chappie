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
    assert "Read-Host 'Allow external AI downloads/installations" in script
    assert "$setupArgs = @('setup', '--yes', '--interactive') + $args" in script
    assert "$setupArgs += '--allow-external'" in script
    assert ".venv\\Scripts\\chappie.exe') @setupArgs" in script
    assert "$setupExit = $LASTEXITCODE" in script and "exit $setupExit" in script
    assert "if (-not (Test-Path $venvPython))" in script
    assert "OllamaProvider" not in script and "ollama pull" not in script


def test_setup_profile_prompt_is_cli_owned_and_plans_never_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from app import cli

    monkeypatch.setattr("builtins.input", lambda _: "LIGHT")
    assert cli._interactive_ai_profile(_paths(tmp_path), cli._parser()) == AIModelProfileV1.LIGHT
    (tmp_path / "plan").mkdir()
    assert main(["setup", "--plan"], paths=_paths(tmp_path / "plan")) == 0
    assert "actions" in capsys.readouterr().out


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
