"""Current-user macOS Ollama executor with a fixed official DMG source."""

from __future__ import annotations

import re
import shutil
import ssl
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

from app.ai.schemas import digest
from app.windows_ollama import ExternalActionResultV1, ExternalActionStatusV1

MACOS_OLLAMA_POLICY_IDENTITY = "ollama_macos_install_policy/1.0.0"
MACOS_OLLAMA_DMG_URL = "https://ollama.com/download/Ollama.dmg"
MACOS_OLLAMA_POLICY_DIGEST = digest(
    {
        "identity": MACOS_OLLAMA_POLICY_IDENTITY,
        "source": MACOS_OLLAMA_DMG_URL,
        "method": "official DMG mounted read-only; Ollama.app copied to current-user Applications",
        "admin_required": False,
        "integrity": "OFFICIAL_SOURCE_TLS_ONLY",
    }
)
MODEL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*(?::[a-z0-9][a-z0-9._-]*)?$")


def download_official_macos_dmg(url: str, destination: Path) -> None:
    """Download only Ollama's fixed official macOS distribution over default TLS."""
    if url != MACOS_OLLAMA_DMG_URL or not url.startswith("https://"):
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


class MacOllamaInstallerV1:
    """Install under ``~/Applications``; no Homebrew, shell, or elevation is used."""

    source = MACOS_OLLAMA_DMG_URL

    def __init__(
        self,
        *,
        runner=subprocess.run,
        downloader=download_official_macos_dmg,
        applications_dir: Path | None = None,
        system_applications_dir: Path = Path("/Applications"),
        interactive_output: bool = True,
    ) -> None:
        self.runner = runner
        self.downloader = downloader
        self.applications_dir = applications_dir or Path.home() / "Applications"
        self.system_applications_dir = system_applications_dir
        self.interactive_output = interactive_output

    def app_bundle(self) -> Path | None:
        user_bundle = self.applications_dir / "Ollama.app"
        system_bundle = self.system_applications_dir / "Ollama.app"
        return (
            user_bundle
            if user_bundle.is_dir()
            else system_bundle
            if system_bundle.is_dir()
            else None
        )

    def executable(self) -> Path | None:
        found = shutil.which("ollama")
        if found:
            return Path(found)
        bundle = self.app_bundle()
        executable = bundle / "Contents" / "Resources" / "ollama" if bundle else None
        return executable if executable and executable.is_file() else None

    def version(self) -> str | None:
        executable = self.executable()
        if executable is None:
            return None
        try:
            output = self.runner([str(executable), "--version"], capture_output=True, text=True)
        except (OSError, subprocess.SubprocessError):
            return None
        return output.stdout.strip() or None

    def install(
        self, *, allow_external: bool, allow_privileged: bool = False
    ) -> ExternalActionResultV1:
        del allow_privileged
        if not allow_external:
            return ExternalActionResultV1(
                "INSTALL_OLLAMA",
                ExternalActionStatusV1.CANCELLED,
                "Darwin",
                self.source,
                error_code="EXTERNAL_APPROVAL_REQUIRED",
            )
        if self.executable():
            return ExternalActionResultV1(
                "INSTALL_OLLAMA",
                ExternalActionStatusV1.COMPLETED,
                "Darwin",
                self.source,
                self.version(),
            )
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="chappie-ollama-") as directory:
            root = Path(directory)
            dmg, mount = root / "Ollama.dmg", root / "mount"
            mount.mkdir()
            try:
                self.downloader(self.source, dmg)
                self.runner(
                    [
                        "/usr/bin/hdiutil",
                        "attach",
                        "-nobrowse",
                        "-readonly",
                        "-mountpoint",
                        str(mount),
                        str(dmg),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                source_bundle = mount / "Ollama.app"
                if not source_bundle.is_dir():
                    raise OSError("official Ollama DMG did not contain Ollama.app")
                self.applications_dir.mkdir(parents=True, exist_ok=True)
                shutil.copytree(source_bundle, self.applications_dir / "Ollama.app", symlinks=True)
            except (OSError, subprocess.SubprocessError):
                return ExternalActionResultV1(
                    "INSTALL_OLLAMA",
                    ExternalActionStatusV1.FAILED,
                    "Darwin",
                    self.source,
                    duration_ms=round((time.monotonic() - started) * 1000),
                    error_code="OLLAMA_INSTALL_FAILED",
                )
            finally:
                try:
                    self.runner(
                        ["/usr/bin/hdiutil", "detach", str(mount)],
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                except (OSError, subprocess.SubprocessError):
                    pass
        version = self.version()
        return ExternalActionResultV1(
            "INSTALL_OLLAMA",
            ExternalActionStatusV1.COMPLETED if version else ExternalActionStatusV1.FAILED,
            "Darwin",
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
                "Darwin",
                "ollama CLI",
                error_code="EXTERNAL_APPROVAL_REQUIRED",
            )
        if executable is None:
            return ExternalActionResultV1(
                "PULL_MODEL",
                ExternalActionStatusV1.FAILED,
                "Darwin",
                "ollama CLI",
                error_code="OLLAMA_NOT_INSTALLED",
            )
        try:
            kwargs: dict[str, object] = {"check": True}
            if not self.interactive_output:
                kwargs.update(capture_output=True, text=True)
            self.runner([str(executable), "pull", model], **kwargs)
        except (OSError, subprocess.SubprocessError):
            return ExternalActionResultV1(
                "PULL_MODEL",
                ExternalActionStatusV1.FAILED,
                "Darwin",
                "ollama CLI",
                error_code="MODEL_PULL_FAILED",
            )
        return ExternalActionResultV1(
            "PULL_MODEL", ExternalActionStatusV1.COMPLETED, "Darwin", "ollama CLI"
        )

    def model_available(self, model: str) -> bool:
        if not MODEL_RE.fullmatch(model):
            raise ValueError("invalid model identifier")
        executable = self.executable()
        if executable is None:
            return False
        try:
            output = self.runner([str(executable), "ls"], capture_output=True, text=True).stdout
        except (OSError, subprocess.SubprocessError):
            return False
        return any(
            fields and fields[0] == model
            for line in output.splitlines()[1:]
            if (fields := line.split())
        )
