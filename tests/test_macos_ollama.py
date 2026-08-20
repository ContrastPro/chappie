from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.bootstrap import ChappiePaths
from app.macos_ollama import (
    MACOS_OLLAMA_DMG_URL,
    MACOS_OLLAMA_POLICY_DIGEST,
    MACOS_OLLAMA_POLICY_IDENTITY,
    MacOllamaInstallerV1,
)
from app.ollama_readiness import OllamaReadinessResultV1, OllamaReadinessStateV1
from app.setup_engine import (
    ActionSafetyV1,
    SetupActionCodeV1,
    SetupActionV1,
    SetupApprovalV1,
    SetupEngineV1,
)
from app.windows_ollama import ExternalActionResultV1, ExternalActionStatusV1


def _runner(calls: list[list[str]], mount: Path | None = None):
    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[1:2] == ["attach"]:
            assert mount is not None
            executable = mount / "Ollama.app" / "Contents" / "Resources" / "ollama"
            executable.parent.mkdir(parents=True)
            executable.touch()
        if argv[-1] == "--version":
            return subprocess.CompletedProcess(argv, 0, "ollama version is 1.0", "")
        if argv[-1] == "ls":
            return subprocess.CompletedProcess(
                argv, 0, "NAME ID SIZE MODIFIED\nqwen3:8b fixture 2 GB now\n", ""
            )
        return subprocess.CompletedProcess(argv, 0, "", "")

    return run


def test_policy_is_fixed_tls_only_and_external_denial_is_safe(tmp_path: Path) -> None:
    assert MACOS_OLLAMA_POLICY_IDENTITY == "ollama_macos_install_policy/1.0.0"
    assert len(MACOS_OLLAMA_POLICY_DIGEST) == 64
    installer = MacOllamaInstallerV1(applications_dir=tmp_path / "Applications")
    assert installer.install(allow_external=False).error_code == "EXTERNAL_APPROVAL_REQUIRED"
    with pytest.raises(ValueError):
        installer.downloader("https://example.invalid/Ollama.dmg", tmp_path / "bad.dmg")


def test_current_user_dmg_install_uses_argv_and_discovers_app_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.macos_ollama.shutil.which", lambda _: None)
    applications = tmp_path / "Applications"
    calls: list[list[str]] = []

    def downloader(url: str, destination: Path) -> None:
        assert url == MACOS_OLLAMA_DMG_URL
        destination.write_bytes(b"fixture")

    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[1:2] == ["attach"]:
            mount = Path(argv[argv.index("-mountpoint") + 1])
            executable = mount / "Ollama.app" / "Contents" / "Resources" / "ollama"
            executable.parent.mkdir(parents=True)
            executable.touch()
        if argv[-1] == "--version":
            return subprocess.CompletedProcess(argv, 0, "ollama version is 1.0", "")
        assert kwargs.get("shell") is None
        return subprocess.CompletedProcess(argv, 0, "", "")

    installer = MacOllamaInstallerV1(
        applications_dir=applications,
        system_applications_dir=tmp_path / "SystemApplications",
        downloader=downloader,
        runner=runner,
    )
    assert installer.install(allow_external=True).status == "COMPLETED"
    assert installer.app_bundle() == applications / "Ollama.app"
    assert (
        installer.executable() == applications / "Ollama.app" / "Contents" / "Resources" / "ollama"
    )
    assert calls[0][:4] == ["/usr/bin/hdiutil", "attach", "-nobrowse", "-readonly"]
    assert calls[-1][-1] == "--version"
    assert calls[-1][0].endswith("Ollama.app/Contents/Resources/ollama")


def test_model_operations_are_exact_and_argv_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.macos_ollama.shutil.which", lambda _: None)
    executable = tmp_path / "Applications" / "Ollama.app" / "Contents" / "Resources" / "ollama"
    executable.parent.mkdir(parents=True)
    executable.touch()
    calls: list[list[str]] = []
    installer = MacOllamaInstallerV1(
        applications_dir=tmp_path / "Applications", runner=_runner(calls)
    )
    assert installer.model_available("qwen3:8b") is True
    assert installer.model_available("qwen3:14b") is False
    assert installer.pull_model("qwen3:8b", allow_external=True).status == "COMPLETED"
    assert calls[-1] == [str(executable), "pull", "qwen3:8b"]
    with pytest.raises(ValueError):
        installer.pull_model("qwen3:8b;whoami", allow_external=True)


class _ProviderOnlyEngine(SetupEngineV1):
    def plan(self, *, developer: bool = False, ai_profile=None):  # type: ignore[no-untyped-def]
        del developer, ai_profile
        return (
            SetupActionV1(
                SetupActionCodeV1.INSTALL_AI_PROVIDER,
                ActionSafetyV1.EXTERNAL,
                "provider",
                None,
            ),
        )


class _Coordinator:
    executable = "ollama"

    def __init__(self, state: OllamaReadinessStateV1) -> None:
        self.state = state

    def ensure_ready(self) -> OllamaReadinessResultV1:
        return OllamaReadinessResultV1(self.state, "http://127.0.0.1:11434")


def _paths(tmp_path: Path) -> ChappiePaths:
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='fixture'\n")
    return ChappiePaths.resolve(root)


def test_engine_rejects_macos_installer_claim_without_durable_app(tmp_path: Path) -> None:
    class Installer(MacOllamaInstallerV1):
        def executable(self) -> Path | None:
            return None

        def version(self) -> str | None:
            return None

        def install(self, *, allow_external: bool, allow_privileged: bool = False):
            del allow_external, allow_privileged
            return ExternalActionResultV1(
                "INSTALL_OLLAMA", ExternalActionStatusV1.COMPLETED, "Darwin", "fixture"
            )

    result = _ProviderOnlyEngine(
        _paths(tmp_path),
        macos_installer=Installer(applications_dir=tmp_path / "Applications"),
        ollama_readiness_coordinator=_Coordinator(OllamaReadinessStateV1.READY),
    ).execute(approval=SetupApprovalV1(allow_external=True))

    assert result.steps[0].state.value == "FAILED"


def test_engine_rejects_durable_macos_provider_when_runtime_is_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.macos_ollama.shutil.which", lambda _: None)
    executable = tmp_path / "Applications/Ollama.app/Contents/Resources/ollama"
    executable.parent.mkdir(parents=True)
    executable.touch()
    installer = MacOllamaInstallerV1(
        applications_dir=tmp_path / "Applications",
        system_applications_dir=tmp_path / "SystemApplications",
        runner=lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 0, "ollama 1.0", ""),
    )

    result = _ProviderOnlyEngine(
        _paths(tmp_path),
        macos_installer=installer,
        ollama_readiness_coordinator=_Coordinator(OllamaReadinessStateV1.NOT_READY),
    ).execute(approval=SetupApprovalV1(allow_external=True))

    assert result.steps[0].state.value == "FAILED"
