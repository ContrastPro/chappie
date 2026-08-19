"""Frozen, Windows-only Ollama external executor with explicit consent boundaries."""

from __future__ import annotations

import os
import re
import shutil
import ssl
import subprocess
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from app.ai.schemas import digest

WINDOWS_OLLAMA_POLICY_IDENTITY = "ollama_windows_install_policy/1.0.0"
WINDOWS_OLLAMA_SETUP_URL = "https://ollama.com/download/OllamaSetup.exe"
WINDOWS_OLLAMA_POLICY_DIGEST = digest(
    {
        "identity": WINDOWS_OLLAMA_POLICY_IDENTITY,
        "source": WINDOWS_OLLAMA_SETUP_URL,
        "method": "interactive OllamaSetup.exe",
        "admin_required": False,
        "integrity": "OFFICIAL_SOURCE_TLS_ONLY",
    }
)
MODEL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*(?::[a-z0-9][a-z0-9._-]*)?$")


class ExternalActionStatusV1(StrEnum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    MANUAL_FOLLOWUP = "MANUAL_FOLLOWUP"


@dataclass(frozen=True, slots=True)
class ExternalActionResultV1:
    action: str
    status: ExternalActionStatusV1
    platform: str
    source: str
    version: str | None = None
    duration_ms: int = 0
    requires_restart: bool = False
    requires_relogin: bool = False
    requires_manual_followup: bool = False
    error_code: str | None = None


class CommandRunner(Protocol):
    def __call__(
        self, argv: list[str], *, check: bool = True, **kwargs: object
    ) -> subprocess.CompletedProcess[str]: ...


class Downloader(Protocol):
    def __call__(self, url: str, destination: Path) -> None: ...


def download_official_windows_installer(url: str, destination: Path) -> None:
    if url != WINDOWS_OLLAMA_SETUP_URL or not url.startswith("https://"):
        raise ValueError("untrusted installer source")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".partial")
    request = urllib.request.Request(url, headers={"User-Agent": "Chappie/0.1"})
    with (
        urllib.request.urlopen(
            request, context=ssl.create_default_context(), timeout=120
        ) as response,
        temporary.open("wb") as output,
    ):
        shutil.copyfileobj(response, output)
    temporary.replace(destination)


class WindowsOllamaInstallerV1:
    source = WINDOWS_OLLAMA_SETUP_URL

    def __init__(
        self,
        *,
        runner: CommandRunner = subprocess.run,
        downloader: Downloader = download_official_windows_installer,
        local_app_data: Path | None = None,
    ) -> None:
        self.runner, self.downloader = runner, downloader
        self.local_app_data = local_app_data or Path(os.environ.get("LOCALAPPDATA", ""))

    def executable(self) -> Path | None:
        found = shutil.which("ollama")
        expected = self.local_app_data / "Programs" / "Ollama" / "ollama.exe"
        return Path(found) if found else expected if expected.is_file() else None

    def version(self) -> str | None:
        executable = self.executable()
        if executable is None:
            return None
        try:
            return (
                self.runner(
                    [str(executable), "--version"], capture_output=True, text=True
                ).stdout.strip()
                or None
            )
        except subprocess.SubprocessError:
            return None

    def install(
        self, *, allow_external: bool, allow_privileged: bool = False
    ) -> ExternalActionResultV1:
        if not allow_external:
            return ExternalActionResultV1(
                "INSTALL_OLLAMA",
                ExternalActionStatusV1.CANCELLED,
                "Windows",
                self.source,
                error_code="EXTERNAL_APPROVAL_REQUIRED",
            )
        if self.executable():
            return ExternalActionResultV1(
                "INSTALL_OLLAMA",
                ExternalActionStatusV1.COMPLETED,
                "Windows",
                self.source,
                self.version(),
            )
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="chappie-ollama-") as directory:
            installer = Path(directory) / "OllamaSetup.exe"
            try:
                self.downloader(self.source, installer)
                # The official docs do not document a silent flag. Launch only the UI installer.
                self.runner([str(installer)], check=True)
            except (OSError, subprocess.SubprocessError):
                return ExternalActionResultV1(
                    "INSTALL_OLLAMA",
                    ExternalActionStatusV1.FAILED,
                    "Windows",
                    self.source,
                    duration_ms=round((time.monotonic() - started) * 1000),
                    error_code="OLLAMA_INSTALL_CANCELLED",
                )
        version = self.version()
        return ExternalActionResultV1(
            "INSTALL_OLLAMA",
            ExternalActionStatusV1.COMPLETED if version else ExternalActionStatusV1.FAILED,
            "Windows",
            self.source,
            version,
            round((time.monotonic() - started) * 1000),
            error_code=None if version else "OLLAMA_VERSION_UNVERIFIED",
        )

    def pull_model(self, model: str, *, allow_external: bool) -> ExternalActionResultV1:
        if not MODEL_RE.fullmatch(model):
            raise ValueError("invalid model identifier")
        executable = self.executable()
        if not allow_external:
            return ExternalActionResultV1(
                "PULL_MODEL",
                ExternalActionStatusV1.CANCELLED,
                "Windows",
                "ollama CLI",
                error_code="EXTERNAL_APPROVAL_REQUIRED",
            )
        if executable is None:
            return ExternalActionResultV1(
                "PULL_MODEL",
                ExternalActionStatusV1.FAILED,
                "Windows",
                "ollama CLI",
                error_code="OLLAMA_NOT_INSTALLED",
            )
        try:
            self.runner([str(executable), "pull", model], check=True)
        except subprocess.SubprocessError:
            return ExternalActionResultV1(
                "PULL_MODEL",
                ExternalActionStatusV1.FAILED,
                "Windows",
                "ollama CLI",
                error_code="MODEL_PULL_FAILED",
            )
        return ExternalActionResultV1(
            "PULL_MODEL", ExternalActionStatusV1.COMPLETED, "Windows", "ollama CLI"
        )
