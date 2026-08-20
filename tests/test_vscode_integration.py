from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from app.vscode_integration import VscodeExtensionInstallerV1, VscodeExtensionStateV1


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    dist = root / "tools/vscode-chappie/dist"
    dist.mkdir(parents=True, exist_ok=True)
    (root / "tools/vscode-chappie/package.json").write_text(json.dumps({"version": "0.1.0"}))
    artifact = dist / "chappie-vscode-0.1.0.vsix"
    artifact.write_bytes(b"verified extension")
    (dist / "SHA256SUMS").write_text(
        f"{hashlib.sha256(artifact.read_bytes()).hexdigest()}  dist/{artifact.name}\n"
    )
    return root


class _Runner:
    def __init__(self, installed: str | None = None) -> None:
        self.installed = installed
        self.calls: list[list[str]] = []

    def __call__(self, argv, **_kwargs):
        self.calls.append(argv)
        if "--install-extension" in argv:
            self.installed = "0.1.0"
            return subprocess.CompletedProcess(argv, 0, "", "")
        output = (
            "" if self.installed is None else f"chappie-local.vscode-chappie@{self.installed}\n"
        )
        return subprocess.CompletedProcess(argv, 0, output, "")


def test_no_vscode_cli_is_optional_and_has_no_subprocess(tmp_path: Path) -> None:
    runner = _Runner()
    result = VscodeExtensionInstallerV1(
        _root(tmp_path), runner=runner, which=lambda _name: None
    ).ensure_installed()
    assert result.state == VscodeExtensionStateV1.NOT_APPLICABLE
    assert runner.calls == []


def test_verified_missing_extension_installs_with_argv_only(tmp_path: Path) -> None:
    runner = _Runner()
    result = VscodeExtensionInstallerV1(
        _root(tmp_path), runner=runner, which=lambda _name: "/usr/bin/code"
    ).ensure_installed()
    assert result.state == VscodeExtensionStateV1.READY
    install = next(call for call in runner.calls if "--install-extension" in call)
    assert install == [
        "/usr/bin/code",
        "--install-extension",
        str(_root(tmp_path) / "tools/vscode-chappie/dist/chappie-vscode-0.1.0.vsix"),
        "--force",
    ]


def test_same_or_newer_extension_is_not_reinstalled(tmp_path: Path) -> None:
    runner = _Runner("0.1.1")
    result = VscodeExtensionInstallerV1(
        _root(tmp_path), runner=runner, which=lambda _name: "/usr/bin/code"
    ).ensure_installed()
    assert result.state == VscodeExtensionStateV1.READY
    assert all("--install-extension" not in call for call in runner.calls)


def test_bad_digest_fails_closed_without_install(tmp_path: Path) -> None:
    root = _root(tmp_path)
    (root / "tools/vscode-chappie/dist/SHA256SUMS").write_text(
        "0" * 64 + "  dist/chappie-vscode-0.1.0.vsix\n"
    )
    runner = _Runner()
    result = VscodeExtensionInstallerV1(
        root, runner=runner, which=lambda _name: "/usr/bin/code"
    ).ensure_installed()
    assert result.state == VscodeExtensionStateV1.ERROR
    assert all("--install-extension" not in call for call in runner.calls)


def test_windows_prefers_windows_code_command(tmp_path: Path) -> None:
    seen: list[str] = []

    def which(name: str) -> str | None:
        seen.append(name)
        return "C:/Code/code.cmd" if name == "code.cmd" else None

    result = VscodeExtensionInstallerV1(
        _root(tmp_path), runner=_Runner("0.1.0"), which=which, platform_name="Windows"
    ).inspect()
    assert result.state == VscodeExtensionStateV1.READY
    assert seen == ["code.cmd"]
