"""Verified local VS Code extension installation for a Chappie checkout.

This module deliberately owns only an optional workstation convenience.  It
never builds the extension, uses Node, contacts a marketplace, or changes core
Chappie readiness.  The checked-in VSIX and its SHA-256 manifest are the sole
installation inputs.
"""

from __future__ import annotations

import hashlib
import json
import platform
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

VSCODE_EXTENSION_ID = "chappie-local.vscode-chappie"
VSCODE_EXTENSION_DIST = Path("tools/vscode-chappie/dist")


class VscodeExtensionStateV1(StrEnum):
    READY = "READY"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    NOT_INSTALLED = "NOT_INSTALLED"
    UPDATE_REQUIRED = "UPDATE_REQUIRED"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class VscodeExtensionResultV1:
    state: VscodeExtensionStateV1
    message: str
    bundled_version: str | None = None
    installed_version: str | None = None


class VscodeExtensionInstallerV1:
    """Install a verified bundled extension with argv-only local CLI calls."""

    def __init__(
        self,
        root: Path,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        which: Callable[[str], str | None] = shutil.which,
        platform_name: str | None = None,
    ) -> None:
        self._root = root.resolve()
        self._runner = runner
        self._which = which
        self._platform_name = platform_name or platform.system()

    def inspect(self) -> VscodeExtensionResultV1:
        code = self._code_command()
        if code is None:
            return VscodeExtensionResultV1(
                VscodeExtensionStateV1.NOT_APPLICABLE,
                "VS Code CLI is not available; workstation integration is optional.",
            )
        try:
            artifact, bundled = self._verified_artifact()
        except (OSError, ValueError) as error:
            return VscodeExtensionResultV1(VscodeExtensionStateV1.ERROR, str(error))
        try:
            installed = self._installed_version(code)
        except (OSError, subprocess.SubprocessError) as error:
            return VscodeExtensionResultV1(
                VscodeExtensionStateV1.ERROR,
                f"could not inspect VS Code extensions: {type(error).__name__}",
                bundled,
            )
        del artifact
        if installed is None:
            return VscodeExtensionResultV1(
                VscodeExtensionStateV1.NOT_INSTALLED,
                "Chappie VS Code extension is not installed.",
                bundled,
            )
        if _version_key(installed) < _version_key(bundled):
            return VscodeExtensionResultV1(
                VscodeExtensionStateV1.UPDATE_REQUIRED,
                "Bundled Chappie VS Code extension is newer.",
                bundled,
                installed,
            )
        return VscodeExtensionResultV1(
            VscodeExtensionStateV1.READY,
            "Chappie VS Code extension is installed.",
            bundled,
            installed,
        )

    def ensure_installed(self) -> VscodeExtensionResultV1:
        result = self.inspect()
        if result.state not in {
            VscodeExtensionStateV1.NOT_INSTALLED,
            VscodeExtensionStateV1.UPDATE_REQUIRED,
        }:
            return result
        code = self._code_command()
        assert code is not None  # inspected above
        try:
            artifact, bundled = self._verified_artifact()
            self._runner(
                [code, "--install-extension", str(artifact), "--force"],
                check=True,
                capture_output=True,
                text=True,
            )
            installed = self._installed_version(code)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            return VscodeExtensionResultV1(
                VscodeExtensionStateV1.ERROR,
                f"could not install verified Chappie VS Code extension: {type(error).__name__}",
                result.bundled_version,
                result.installed_version,
            )
        if installed is None or _version_key(installed) < _version_key(bundled):
            return VscodeExtensionResultV1(
                VscodeExtensionStateV1.ERROR,
                "VS Code did not report the verified Chappie extension after installation.",
                bundled,
                installed,
            )
        return VscodeExtensionResultV1(
            VscodeExtensionStateV1.READY,
            "Chappie VS Code extension is installed.",
            bundled,
            installed,
        )

    def _code_command(self) -> str | None:
        names = ("code.cmd", "code.exe", "code") if self._platform_name == "Windows" else ("code",)
        return next((resolved for name in names if (resolved := self._which(name))), None)

    def _verified_artifact(self) -> tuple[Path, str]:
        package = self._root / "tools/vscode-chappie/package.json"
        version = json.loads(package.read_text(encoding="utf-8"))["version"]
        if not isinstance(version, str) or not version:
            raise ValueError("bundled VS Code extension version is invalid")
        artifact = self._root / VSCODE_EXTENSION_DIST / f"chappie-vscode-{version}.vsix"
        manifest = artifact.parent / "SHA256SUMS"
        expected = _manifest_digest(manifest, artifact.name)
        actual = _sha256(artifact)
        if actual != expected:
            raise ValueError("bundled VS Code extension SHA-256 verification failed")
        return artifact, version

    def _installed_version(self, code: str) -> str | None:
        completed = self._runner(
            [code, "--list-extensions", "--show-versions"],
            check=True,
            capture_output=True,
            text=True,
        )
        prefix = f"{VSCODE_EXTENSION_ID}@"
        return next(
            (
                line.removeprefix(prefix)
                for line in completed.stdout.splitlines()
                if line.startswith(prefix)
            ),
            None,
        )


def _manifest_digest(path: Path, artifact_name: str) -> str:
    entries = {
        filename: checksum
        for line in path.read_text(encoding="utf-8").splitlines()
        if (parts := line.split(maxsplit=1)) and len(parts) == 2
        for checksum, filename in [(parts[0], parts[1].removeprefix("*"))]
    }
    value = entries.get(f"dist/{artifact_name}") or entries.get(artifact_name)
    if (
        value is None
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value.lower())
    ):
        raise ValueError("bundled VS Code extension SHA-256 manifest entry is invalid")
    return value.lower()


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _version_key(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in value.split("."))
    except ValueError:
        return ()
