import subprocess

import pytest

from app.platform.ollama.windows import (
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
    monkeypatch.setattr("app.platform.ollama.windows.shutil.which", lambda _: None)
    executable = tmp_path / "Programs" / "Ollama" / "ollama.exe"
    executable.parent.mkdir(parents=True)
    executable.touch()
    calls = []
    installer = WindowsOllamaInstallerV1(local_app_data=tmp_path, runner=_runner(calls))
    assert installer.pull_model("qwen3:8b", allow_external=True).status == "COMPLETED"
    assert calls[-1] == [str(executable), "pull", "qwen3:8b"]
    with pytest.raises(ValueError):
        installer.pull_model("qwen3:8b;whoami", allow_external=True)


def test_streamed_model_pull_finishes_with_a_clean_newline(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("app.platform.ollama.windows.shutil.which", lambda _: None)
    executable = tmp_path / "Programs" / "Ollama" / "ollama.exe"
    executable.parent.mkdir(parents=True)
    executable.touch()

    def runner(argv, **kwargs):
        print("raw pull progress", end="\r")
        return subprocess.CompletedProcess(argv, 0, "", "")

    installer = WindowsOllamaInstallerV1(local_app_data=tmp_path, runner=runner)
    installer.pull_model("qwen3:8b", allow_external=True)
    assert capsys.readouterr().out == "raw pull progress\r\n"


def test_non_interactive_model_pull_captures_output_for_machine_mode(tmp_path, monkeypatch):
    monkeypatch.setattr("app.platform.ollama.windows.shutil.which", lambda _: None)
    executable = tmp_path / "Programs" / "Ollama" / "ollama.exe"
    executable.parent.mkdir(parents=True)
    executable.touch()
    calls = []
    installer = WindowsOllamaInstallerV1(
        local_app_data=tmp_path, runner=_runner(calls), interactive_output=False
    )
    installer.pull_model("qwen3:8b", allow_external=True)
    assert calls[-1] == [str(executable), "pull", "qwen3:8b"]


def test_non_interactive_installer_launch_captures_console_output(tmp_path):
    calls = []

    def downloader(url, destination):
        destination.write_bytes(b"fixture")

    installer = WindowsOllamaInstallerV1(
        local_app_data=tmp_path,
        runner=lambda argv, **kwargs: (
            calls.append((argv, kwargs)) or subprocess.CompletedProcess(argv, 0, "", "")
        ),
        downloader=downloader,
        interactive_output=False,
    )
    installer.install(allow_external=True)
    assert calls[0][1]["capture_output"] is True
    assert calls[0][1]["text"] is True


def test_model_availability_requires_exact_identity(tmp_path, monkeypatch):
    monkeypatch.setattr("app.platform.ollama.windows.shutil.which", lambda _: None)
    executable = tmp_path / "Programs" / "Ollama" / "ollama.exe"
    executable.parent.mkdir(parents=True)
    executable.touch()
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            "NAME ID SIZE MODIFIED\nqwen3:4b fixture 1 GB now\nqwen3:8b fixture 2 GB now\n",
            "",
        )

    installer = WindowsOllamaInstallerV1(local_app_data=tmp_path, runner=runner)
    assert installer.model_available("qwen3:8b") is True
    assert installer.model_available("qwen3:14b") is False
    assert calls == [[str(executable), "ls"], [str(executable), "ls"]]


def test_production_installer_exposes_setup_engine_model_operations(tmp_path):
    installer = WindowsOllamaInstallerV1(local_app_data=tmp_path, runner=_runner([]))
    assert callable(installer.executable)
    assert callable(installer.version)
    assert callable(installer.install)
    assert callable(installer.model_available)
    assert callable(installer.pull_model)


def test_untrusted_download_source_is_impossible(tmp_path):
    installer = WindowsOllamaInstallerV1(local_app_data=tmp_path)
    assert installer.source == "https://ollama.com/download/OllamaSetup.exe"
