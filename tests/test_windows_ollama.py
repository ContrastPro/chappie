import subprocess

import pytest

from app.windows_ollama import (
    WINDOWS_OLLAMA_POLICY_DIGEST,
    WINDOWS_OLLAMA_POLICY_IDENTITY,
    WindowsOllamaInstallerV1,
)


def _runner(calls):
    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "ollama version is 1.0", "")

    return run


def test_policy_and_external_denial(tmp_path):
    assert (
        WINDOWS_OLLAMA_POLICY_IDENTITY == "ollama_windows_install_policy/1.0.0"
        and len(WINDOWS_OLLAMA_POLICY_DIGEST) == 64
    )
    installer = WindowsOllamaInstallerV1(local_app_data=tmp_path)
    assert installer.install(allow_external=False).error_code == "EXTERNAL_APPROVAL_REQUIRED"


def test_installer_and_model_commands_use_argv(tmp_path, monkeypatch):
    monkeypatch.setattr("app.windows_ollama.shutil.which", lambda _: None)
    executable = tmp_path / "Programs" / "Ollama" / "ollama.exe"
    executable.parent.mkdir(parents=True)
    executable.touch()
    calls = []
    installer = WindowsOllamaInstallerV1(local_app_data=tmp_path, runner=_runner(calls))
    assert installer.pull_model("qwen3:8b", allow_external=True).status == "COMPLETED"
    assert calls[-1] == [str(executable), "pull", "qwen3:8b"]
    with pytest.raises(ValueError):
        installer.pull_model("qwen3:8b;whoami", allow_external=True)


def test_untrusted_download_source_is_impossible(tmp_path):
    installer = WindowsOllamaInstallerV1(local_app_data=tmp_path)
    assert installer.source == "https://ollama.com/download/OllamaSetup.exe"
