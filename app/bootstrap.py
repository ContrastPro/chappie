"""Offline, provider-neutral product bootstrap diagnostics for Chappie.

This module intentionally has no installer, network, or model-generation path.  It is
the small control-plane contract used by ``chappie status``, ``doctor``, and the future
setup engine.
"""

from __future__ import annotations

import ctypes
import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from app.ai.schemas import canonical_json, digest

INSTALLATION_IDENTITY = "chappie_installation_state/1.0.0"
CHAPPIE_VERSION = "0.1.0"


class InstallationStateV1(StrEnum):
    UNINITIALIZED = "UNINITIALIZED"
    SETUP_REQUIRED = "SETUP_REQUIRED"
    DEGRADED = "DEGRADED"
    READY = "READY"
    UNSUPPORTED = "UNSUPPORTED"


class ComponentStateV1(StrEnum):
    READY = "READY"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    NOT_INSTALLED = "NOT_INSTALLED"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"
    DEGRADED = "DEGRADED"
    UNSUPPORTED = "UNSUPPORTED"
    INVALID = "INVALID"
    STALE = "STALE"


class HardwareCapabilityV1(StrEnum):
    MINIMAL = "MINIMAL"
    LIGHT = "LIGHT"
    BALANCED = "BALANCED"
    HIGH = "HIGH"
    UNKNOWN = "UNKNOWN"


class SetupStepStateV1(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class RemediationCodeV1(StrEnum):
    RUN_CHAPPIE_SETUP = "RUN_CHAPPIE_SETUP"
    INSTALL_SUPPORTED_PYTHON = "INSTALL_SUPPORTED_PYTHON"
    CREATE_VIRTUAL_ENVIRONMENT = "CREATE_VIRTUAL_ENVIRONMENT"
    INSTALL_PROJECT_DEPENDENCIES = "INSTALL_PROJECT_DEPENDENCIES"
    BUILD_KNOWLEDGE_STORE = "BUILD_KNOWLEDGE_STORE"
    INSTALL_AI_PROVIDER = "INSTALL_AI_PROVIDER"
    START_AI_PROVIDER = "START_AI_PROVIDER"
    INSTALL_AI_MODEL = "INSTALL_AI_MODEL"
    FREE_DISK_SPACE = "FREE_DISK_SPACE"


INSTALLATION_CONTRACT_DIGEST = digest(
    {
        "identity": INSTALLATION_IDENTITY,
        "overall_states": [item.value for item in InstallationStateV1],
        "component_states": [item.value for item in ComponentStateV1],
        "remediations": [item.value for item in RemediationCodeV1],
        "python": ">=3.13",
        "setup_execution": "not_implemented_phase_1",
    }
)


@dataclass(frozen=True, slots=True)
class ChappiePaths:
    """Portable product paths; project-local in a checkout, user-local when packaged."""

    root: Path
    state_root: Path

    @classmethod
    def resolve(cls, root: Path | None = None) -> ChappiePaths:
        root = (root or Path.cwd()).resolve()
        # Development checkouts keep disposable state inside ignored data/.  A packaged
        # installation has no repository marker and uses a platform-neutral user home.
        state_root = (
            root / "data" if (root / "pyproject.toml").is_file() else Path.home() / ".chappie"
        )
        return cls(root=root, state_root=state_root)

    @property
    def config_file(self) -> Path:
        return self.state_root / "config" / "chappie.json"

    @property
    def logs_dir(self) -> Path:
        return self.state_root / "logs"

    @property
    def knowledge_file(self) -> Path:
        return self.state_root / "knowledge" / "chappie_knowledge_v1.json"

    @property
    def research_dir(self) -> Path:
        return self.state_root / "research"

    @property
    def cache_dir(self) -> Path:
        return self.research_dir / "cache"

    @property
    def models_dir(self) -> Path:
        return self.state_root / "models"


@dataclass(frozen=True, slots=True)
class ChappieLocalConfigV1:
    setup_version: str = "1.0.0"
    provider: str | None = None
    model: str | None = None
    enabled_capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.setup_version or any(not value for value in self.enabled_capabilities):
            raise ValueError("invalid local Chappie configuration")
        if (self.provider is None) != (self.model is None):
            raise ValueError("AI provider and model must be configured together")

    def payload(self) -> dict[str, object]:
        return {
            "schema": "chappie_local_config/1.0.0",
            "setup_version": self.setup_version,
            "provider": self.provider,
            "model": self.model,
            "enabled_capabilities": list(self.enabled_capabilities),
        }


@dataclass(frozen=True, slots=True)
class ComponentStatusV1:
    component: str
    state: ComponentStateV1
    required: bool
    detected_version: str | None
    expected_version: str | None
    message: str
    remediation_code: RemediationCodeV1 | None = None
    details: tuple[tuple[str, str], ...] = ()

    def payload(self) -> dict[str, object]:
        return {
            "component": self.component,
            "state": self.state.value,
            "required": self.required,
            "detected_version": self.detected_version,
            "expected_version": self.expected_version,
            "message": self.message,
            "remediation_code": self.remediation_code.value if self.remediation_code else None,
            "details": dict(self.details),
        }


@dataclass(frozen=True, slots=True)
class HardwareProfileV1:
    operating_system: str
    operating_system_version: str
    architecture: str
    cpu_model: str | None
    logical_cpu_count: int | None
    memory_bytes: int | None
    gpu_names: tuple[str, ...] = ()
    gpu_memory_bytes: int | None = None
    cuda_available: bool | None = None
    disk_free_bytes: int | None = None
    apple_unified_memory: bool | None = None

    @property
    def capability(self) -> HardwareCapabilityV1:
        if self.memory_bytes is None or self.logical_cpu_count is None:
            return HardwareCapabilityV1.UNKNOWN
        gib = self.memory_bytes / 1024**3
        if gib < 8 or self.logical_cpu_count < 4:
            return HardwareCapabilityV1.MINIMAL
        if gib < 16 or self.logical_cpu_count < 8:
            return HardwareCapabilityV1.LIGHT
        if gib < 32 or self.logical_cpu_count < 12:
            return HardwareCapabilityV1.BALANCED
        return HardwareCapabilityV1.HIGH

    def payload(self) -> dict[str, object]:
        return {**asdict(self), "capability": self.capability.value}


@dataclass(frozen=True, slots=True)
class ChappieStatusV1:
    installation_identity: str
    installation_contract_digest: str
    overall_state: InstallationStateV1
    platform: str
    architecture: str
    hardware: HardwareProfileV1
    components: tuple[ComponentStatusV1, ...]
    configured_capabilities: tuple[str, ...]
    warnings: tuple[str, ...]
    recommended_next_actions: tuple[RemediationCodeV1, ...]

    @property
    def status_digest(self) -> str:
        return digest(self.payload(include_digest=False))

    def payload(self, *, include_digest: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "installation_identity": self.installation_identity,
            "installation_contract_digest": self.installation_contract_digest,
            "overall_state": self.overall_state.value,
            "platform": self.platform,
            "architecture": self.architecture,
            "hardware": self.hardware.payload(),
            "components": [item.payload() for item in self.components],
            "configured_capabilities": list(self.configured_capabilities),
            "warnings": list(self.warnings),
            "recommended_next_actions": [item.value for item in self.recommended_next_actions],
        }
        if include_digest:
            value["status_digest"] = self.status_digest
        return value


@dataclass(frozen=True, slots=True)
class DoctorIssueV1:
    severity: str
    component: str
    remediation_code: RemediationCodeV1 | None
    message: str

    def payload(self) -> dict[str, object]:
        return {
            "severity": self.severity,
            "component": self.component,
            "remediation_code": self.remediation_code.value if self.remediation_code else None,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class ChappieDoctorReportV1:
    status: ChappieStatusV1
    issues: tuple[DoctorIssueV1, ...]

    def payload(self) -> dict[str, object]:
        return {"status": self.status.payload(), "issues": [item.payload() for item in self.issues]}


@dataclass(frozen=True, slots=True)
class SetupStepV1:
    sequence: int
    code: str
    state: SetupStepStateV1
    message: str
    remediation_code: RemediationCodeV1 | None = None


@dataclass(frozen=True, slots=True)
class SetupStateV1:
    identity: str = "chappie_setup_state/1.0.0"
    steps: tuple[SetupStepV1, ...] = ()


class AIModelProfileV1(StrEnum):
    LIGHT = "LIGHT"
    BALANCED = "BALANCED"
    ADVANCED = "ADVANCED"
    CUSTOM = "CUSTOM"


@dataclass(frozen=True, slots=True)
class AIModelCatalogEntryV1:
    profile: AIModelProfileV1
    provider: str
    model: str
    minimum_memory_gib: int
    recommended_memory_gib: int
    requires_gpu: bool
    context_recommendation: str
    description: str
    structured_output_compatible: bool
    supported_platforms: tuple[str, ...] = ("Darwin", "Windows", "Linux")
    project_qa_suitable: bool = False


@dataclass(frozen=True, slots=True)
class AIModelCatalogV1:
    identity: str
    entries: tuple[AIModelCatalogEntryV1, ...]


TEST_MODEL_CATALOG = AIModelCatalogV1(
    "chappie_model_catalog/1.0.0",
    (
        AIModelCatalogEntryV1(
            AIModelProfileV1.LIGHT,
            "ollama",
            "qwen3:4b",
            8,
            12,
            False,
            "8k",
            "Conservative local structured-output profile (~2.5 GiB download).",
            True,
        ),
        AIModelCatalogEntryV1(
            AIModelProfileV1.BALANCED,
            "ollama",
            "qwen3:8b",
            16,
            18,
            False,
            "16k",
            "Initial local development profile (~5 GiB download).",
            True,
            project_qa_suitable=True,
        ),
        AIModelCatalogEntryV1(
            AIModelProfileV1.ADVANCED,
            "ollama",
            "qwen3:14b",
            32,
            40,
            False,
            "32k",
            "Higher-memory local research profile (~9 GiB download).",
            True,
            project_qa_suitable=True,
        ),
    ),
)


def recommend_model_profiles(
    profile: HardwareProfileV1, catalog: AIModelCatalogV1
) -> dict[str, tuple[str, ...]]:
    memory = profile.memory_bytes / 1024**3 if profile.memory_bytes is not None else None
    compatible, recommended, unsupported = [], [], []
    for entry in catalog.entries:
        if profile.operating_system not in entry.supported_platforms or (
            memory is None
            or memory < entry.minimum_memory_gib
            or (entry.requires_gpu and not profile.gpu_names)
        ):
            unsupported.append(entry.model)
        else:
            compatible.append(entry.model)
            if memory >= entry.recommended_memory_gib:
                recommended.append(entry.model)
    return {
        "compatible": tuple(compatible),
        "recommended": tuple(recommended),
        "not_recommended": (),
        "unsupported": tuple(unsupported),
    }


def load_local_config(paths: ChappiePaths) -> ChappieLocalConfigV1 | None:
    if not paths.config_file.is_file():
        return None
    try:
        raw = json.loads(paths.config_file.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise TypeError("configuration must be an object")
        allowed = {"setup_version", "provider", "model", "enabled_capabilities", "schema"}
        if set(raw).difference(allowed):
            raise TypeError("configuration contains unsupported fields")
        return ChappieLocalConfigV1(
            setup_version=str(raw.get("setup_version", "")),
            provider=raw.get("provider"),
            model=raw.get("model"),
            enabled_capabilities=tuple(raw.get("enabled_capabilities", ())),
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("local Chappie configuration is invalid") from error


def detect_hardware(
    paths: ChappiePaths,
    *,
    system: str | None = None,
    machine: str | None = None,
    cpu_count: int | None = None,
    memory_bytes: int | None = None,
    gpu_names: tuple[str, ...] | None = None,
    gpu_memory_bytes: int | None = None,
    cuda_available: bool | None = None,
) -> HardwareProfileV1:
    system = system or platform.system() or "UNKNOWN"
    machine = machine or platform.machine() or "UNKNOWN"
    memory_bytes = memory_bytes if memory_bytes is not None else _memory_bytes(system)
    if gpu_names is None and system == "Windows":
        gpu_names, gpu_memory_bytes = _windows_gpu_info()
    try:
        disk = shutil.disk_usage(paths.state_root if paths.state_root.exists() else paths.root).free
    except OSError:
        disk = None
    return HardwareProfileV1(
        system,
        platform.version() or "UNKNOWN",
        machine,
        platform.processor() or None,
        cpu_count if cpu_count is not None else os.cpu_count(),
        memory_bytes,
        gpu_names or (),
        gpu_memory_bytes,
        _cuda_available() if cuda_available is None else cuda_available,
        disk,
        system == "Darwin" and machine.startswith("arm"),
    )


def _memory_bytes(system: str) -> int | None:
    try:
        if system == "Darwin":
            return int(
                subprocess.check_output(
                    ["sysctl", "-n", "hw.memsize"], text=True, stderr=subprocess.DEVNULL
                ).strip()
            )
        if system == "Linux":
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
        if system == "Windows":

            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("length", ctypes.c_ulong),
                    ("memory_load", ctypes.c_ulong),
                    ("total_physical", ctypes.c_ulonglong),
                    ("available_physical", ctypes.c_ulonglong),
                    ("total_page_file", ctypes.c_ulonglong),
                    ("available_page_file", ctypes.c_ulonglong),
                    ("total_virtual", ctypes.c_ulonglong),
                    ("available_virtual", ctypes.c_ulonglong),
                    ("available_extended_virtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatus()
            status.length = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.total_physical)
    except (AttributeError, OSError, ValueError, subprocess.SubprocessError):
        return None
    return None


def _windows_gpu_info() -> tuple[tuple[str, ...], int | None]:
    """Best-effort built-in CIM discovery; unknown VRAM stays unknown, never zero."""
    command = (
        "Get-CimInstance Win32_VideoController | "
        "Select-Object Name,AdapterRAM | ConvertTo-Json -Compress"
    )
    try:
        raw = subprocess.check_output(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        rows = json.loads(raw)
        rows = rows if isinstance(rows, list) else [rows]
        names = tuple(
            str(row["Name"])
            for row in rows
            if isinstance(row, dict) and isinstance(row.get("Name"), str) and row["Name"].strip()
        )
        vram = [
            int(row["AdapterRAM"])
            for row in rows
            if isinstance(row, dict) and isinstance(row.get("AdapterRAM"), (int, float))
        ]
        return names, max(vram) if vram else None
    except (OSError, ValueError, TypeError, json.JSONDecodeError, subprocess.SubprocessError):
        return (), None


def _cuda_available() -> bool | None:
    try:
        import torch  # type: ignore[import-not-found]

        return bool(torch.cuda.is_available())
    except ImportError:
        return None


def collect_status(paths: ChappiePaths | None = None) -> ChappieStatusV1:
    paths = paths or ChappiePaths.resolve()
    hardware = detect_hardware(paths)
    config, config_status = _config_status(paths)
    ai_provider = _ai_provider_status(config, platform_name=hardware.operating_system)
    components = (
        _operating_system_status(hardware),
        _python_status(),
        _venv_status(),
        _dependency_status(),
        _repository_status(paths),
        config_status,
        _knowledge_status(paths),
        _research_status(paths),
        ai_provider,
        _ai_model_status(config, ai_provider, platform_name=hardware.operating_system),
        _disk_status(hardware),
    )
    state = aggregate_installation_state(components)
    actions = tuple(
        dict.fromkeys(
            item.remediation_code for item in components if item.remediation_code is not None
        )
    )
    return ChappieStatusV1(
        INSTALLATION_IDENTITY,
        INSTALLATION_CONTRACT_DIGEST,
        state,
        hardware.operating_system,
        hardware.architecture,
        hardware,
        components,
        config.enabled_capabilities if config else (),
        tuple(
            item.message
            for item in components
            if item.state not in {ComponentStateV1.READY, ComponentStateV1.UNKNOWN}
        ),
        actions,
    )


def aggregate_installation_state(components: tuple[ComponentStatusV1, ...]) -> InstallationStateV1:
    if any(item.state == ComponentStateV1.UNSUPPORTED and item.required for item in components):
        return InstallationStateV1.UNSUPPORTED
    if any(item.state == ComponentStateV1.INVALID and item.required for item in components):
        return InstallationStateV1.DEGRADED
    if any(
        item.required
        and item.state in {ComponentStateV1.NOT_CONFIGURED, ComponentStateV1.NOT_INSTALLED}
        for item in components
    ):
        return InstallationStateV1.SETUP_REQUIRED
    if any(
        item.required
        and item.state
        in {
            ComponentStateV1.DEGRADED,
            ComponentStateV1.PARTIAL,
            ComponentStateV1.STALE,
            ComponentStateV1.UNKNOWN,
        }
        for item in components
    ):
        return InstallationStateV1.DEGRADED
    return InstallationStateV1.READY


def doctor(paths: ChappiePaths | None = None) -> ChappieDoctorReportV1:
    status = collect_status(paths)
    severity = {
        InstallationStateV1.UNSUPPORTED: "ERROR",
        InstallationStateV1.DEGRADED: "WARNING",
        InstallationStateV1.SETUP_REQUIRED: "INFO",
    }
    issues = tuple(
        DoctorIssueV1(
            severity.get(status.overall_state, "INFO"),
            item.component,
            item.remediation_code,
            item.message,
        )
        for item in status.components
        if item.state not in {ComponentStateV1.READY, ComponentStateV1.UNKNOWN}
    )
    return ChappieDoctorReportV1(status, issues)


def setup_plan(paths: ChappiePaths | None = None) -> SetupStateV1:
    _ = paths or ChappiePaths.resolve()
    steps = (
        ("CHECK_PYTHON", "Check supported Python", RemediationCodeV1.INSTALL_SUPPORTED_PYTHON),
        (
            "CREATE_VENV",
            "Create project virtual environment",
            RemediationCodeV1.CREATE_VIRTUAL_ENVIRONMENT,
        ),
        (
            "INSTALL_DEPENDENCIES",
            "Install project dependencies",
            RemediationCodeV1.INSTALL_PROJECT_DEPENDENCIES,
        ),
        (
            "INITIALIZE_CONFIG",
            "Initialize non-secret local configuration",
            RemediationCodeV1.RUN_CHAPPIE_SETUP,
        ),
        (
            "BUILD_KNOWLEDGE",
            "Build and verify Knowledge store",
            RemediationCodeV1.BUILD_KNOWLEDGE_STORE,
        ),
        (
            "INSTALL_AI_PROVIDER",
            "Install configured AI provider",
            RemediationCodeV1.INSTALL_AI_PROVIDER,
        ),
        ("SELECT_AI_MODEL", "Select an AI model", RemediationCodeV1.INSTALL_AI_MODEL),
        (
            "VERIFY_AI",
            "Verify configured AI provider and model",
            RemediationCodeV1.START_AI_PROVIDER,
        ),
        ("FINAL_HEALTH_CHECK", "Run Chappie doctor", None),
    )
    return SetupStateV1(
        steps=tuple(
            SetupStepV1(index, code, SetupStepStateV1.PENDING, message, remediation)
            for index, (code, message, remediation) in enumerate(steps, 1)
        )
    )


def _status(
    component: str,
    state: ComponentStateV1,
    required: bool,
    message: str,
    remediation: RemediationCodeV1 | None = None,
    detected: str | None = None,
    expected: str | None = None,
    **details: str,
) -> ComponentStatusV1:
    return ComponentStatusV1(
        component,
        state,
        required,
        detected,
        expected,
        message,
        remediation,
        tuple(sorted(details.items())),
    )


def _operating_system_status(hardware: HardwareProfileV1) -> ComponentStatusV1:
    return _status(
        "OPERATING_SYSTEM",
        ComponentStateV1.READY
        if hardware.operating_system in {"Darwin", "Windows", "Linux"}
        else ComponentStateV1.UNSUPPORTED,
        True,
        f"{hardware.operating_system} detected",
        None,
        hardware.operating_system,
    )


def _python_status() -> ComponentStatusV1:
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    state = ComponentStateV1.READY if sys.version_info >= (3, 13) else ComponentStateV1.UNSUPPORTED
    return _status(
        "PYTHON",
        state,
        True,
        "Python version supported"
        if state == ComponentStateV1.READY
        else "Python 3.13 or newer is required",
        None if state == ComponentStateV1.READY else RemediationCodeV1.INSTALL_SUPPORTED_PYTHON,
        version,
        ">=3.13",
    )


def _venv_status() -> ComponentStatusV1:
    active = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    return _status(
        "VIRTUAL_ENVIRONMENT",
        ComponentStateV1.READY if active else ComponentStateV1.NOT_CONFIGURED,
        True,
        "virtual environment active" if active else "run Chappie from its virtual environment",
        None if active else RemediationCodeV1.CREATE_VIRTUAL_ENVIRONMENT,
    )


def _dependency_status() -> ComponentStatusV1:
    try:
        importlib.metadata.version("pydantic-settings")
        importlib.metadata.version("httpx")
    except importlib.metadata.PackageNotFoundError:
        return _status(
            "PYTHON_DEPENDENCIES",
            ComponentStateV1.NOT_INSTALLED,
            True,
            "required project dependencies are missing",
            RemediationCodeV1.INSTALL_PROJECT_DEPENDENCIES,
        )
    return _status(
        "PYTHON_DEPENDENCIES",
        ComponentStateV1.READY,
        True,
        "required project dependencies detected",
    )


def _repository_status(paths: ChappiePaths) -> ComponentStatusV1:
    if not (paths.root / "pyproject.toml").is_file():
        return _status(
            "PROJECT_REPOSITORY",
            ComponentStateV1.UNKNOWN,
            False,
            "installed distribution; repository metadata unavailable",
        )
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=paths.root,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        dirty = bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"],
                cwd=paths.root,
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        )
        return _status(
            "PROJECT_REPOSITORY",
            ComponentStateV1.DEGRADED if dirty else ComponentStateV1.READY,
            True,
            "repository has local changes" if dirty else "repository checkout clean",
            None,
            commit,
            None,
            dirty=str(dirty).lower(),
        )
    except (OSError, subprocess.SubprocessError):
        return _status(
            "PROJECT_REPOSITORY", ComponentStateV1.UNKNOWN, False, "Git metadata unavailable"
        )


def _config_status(paths: ChappiePaths) -> tuple[ChappieLocalConfigV1 | None, ComponentStatusV1]:
    try:
        config = load_local_config(paths)
    except ValueError:
        return None, _status(
            "PROJECT_CONFIG",
            ComponentStateV1.INVALID,
            True,
            "local configuration is invalid",
            RemediationCodeV1.RUN_CHAPPIE_SETUP,
        )
    if config is None:
        return None, _status(
            "PROJECT_CONFIG",
            ComponentStateV1.NOT_CONFIGURED,
            True,
            "local configuration is not initialized",
            RemediationCodeV1.RUN_CHAPPIE_SETUP,
        )
    return config, _status(
        "PROJECT_CONFIG",
        ComponentStateV1.READY,
        True,
        "local configuration valid",
        detected=config.setup_version,
    )


def _knowledge_status(paths: ChappiePaths) -> ComponentStatusV1:
    if not paths.knowledge_file.exists():
        return _status(
            "KNOWLEDGE",
            ComponentStateV1.NOT_CONFIGURED,
            True,
            "Knowledge store is not built",
            RemediationCodeV1.BUILD_KNOWLEDGE_STORE,
        )
    try:
        from app.knowledge import committed_snapshot, read_store

        _, stored_manifest = read_store(paths.knowledge_file)
        try:
            current = committed_snapshot(paths.root)
        except (OSError, subprocess.SubprocessError):
            current = None
        # Both setup and status use the complete provenance snapshot.  Git ignores
        # generated local state, so .venv/, data/, and local configuration do not
        # affect this comparison; tracked source changes do.
        if current is not None and current != stored_manifest.snapshot:
            return _status(
                "KNOWLEDGE",
                ComponentStateV1.STALE,
                True,
                "Knowledge store snapshot is stale",
                RemediationCodeV1.BUILD_KNOWLEDGE_STORE,
            )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return _status(
            "KNOWLEDGE",
            ComponentStateV1.INVALID,
            False,
            "Knowledge store is invalid",
            RemediationCodeV1.BUILD_KNOWLEDGE_STORE,
        )
    return _status("KNOWLEDGE", ComponentStateV1.READY, True, "Knowledge store verified")


def _research_status(paths: ChappiePaths) -> ComponentStatusV1:
    root = paths.research_dir
    if not root.exists():
        return _status(
            "RESEARCH_STORAGE",
            ComponentStateV1.NOT_INSTALLED,
            False,
            "research storage is not installed",
        )
    if (root / "compact" / "research.sqlite3").exists():
        return _status(
            "RESEARCH_STORAGE", ComponentStateV1.READY, False, "compact research store available"
        )
    return _status(
        "RESEARCH_STORAGE",
        ComponentStateV1.PARTIAL,
        False,
        "research storage is present but compact store is unavailable",
    )


def _ai_provider_status(
    config: ChappieLocalConfigV1 | None, *, platform_name: str | None = None
) -> ComponentStatusV1:
    if config is None or config.provider is None:
        return _status(
            "AI_PROVIDER", ComponentStateV1.NOT_CONFIGURED, False, "AI provider not configured"
        )
    if config.provider != "ollama":
        return _status(
            "AI_PROVIDER",
            ComponentStateV1.UNKNOWN,
            False,
            "provider configured; readiness probe unavailable",
            detected=config.provider,
        )
    from app.platform.ollama.readiness import OllamaReadinessProbe, OllamaReadinessStateV1

    installer = _ollama_installer(platform_name)
    if installer is None:
        return _status(
            "AI_PROVIDER",
            ComponentStateV1.UNKNOWN,
            True,
            "Ollama inspection is unsupported on this platform",
            config.provider,
        )
    executable = installer.executable()
    if executable is None:
        return _status(
            "AI_PROVIDER",
            ComponentStateV1.NOT_INSTALLED,
            True,
            "Ollama is not installed",
            RemediationCodeV1.INSTALL_AI_PROVIDER,
            config.provider,
        )
    probe = OllamaReadinessProbe()
    try:
        result = probe.probe()
    finally:
        probe.close()
    if result.state == OllamaReadinessStateV1.READY:
        return _status(
            "AI_PROVIDER",
            ComponentStateV1.READY,
            True,
            "Ollama runtime ready",
            detected=config.provider,
            endpoint=result.endpoint,
            latency_ms=str(result.latency_ms or 0),
        )
    message = (
        "Ollama runtime unavailable"
        if result.state == OllamaReadinessStateV1.NOT_READY
        else "Ollama runtime probe failed"
    )
    return _status(
        "AI_PROVIDER",
        ComponentStateV1.DEGRADED,
        True,
        message,
        RemediationCodeV1.START_AI_PROVIDER,
        config.provider,
        endpoint=result.endpoint,
        error_category=result.error_category or "UNKNOWN",
    )


def _ollama_installer(platform_name: str | None = None):
    """Return the current platform's local Ollama inspector, never an installer action."""
    if (platform_name or platform.system()) == "Windows":
        from app.platform.ollama.windows import WindowsOllamaInstallerV1

        return WindowsOllamaInstallerV1(interactive_output=False)
    if (platform_name or platform.system()) == "Darwin":
        from app.platform.ollama.macos import MacOllamaInstallerV1

        return MacOllamaInstallerV1(interactive_output=False)
    return None


def _ai_model_status(
    config: ChappieLocalConfigV1 | None,
    provider_status: ComponentStatusV1,
    *,
    platform_name: str | None = None,
) -> ComponentStatusV1:
    if config is None or config.model is None:
        return _status(
            "AI_MODEL", ComponentStateV1.NOT_CONFIGURED, False, "AI model not configured"
        )
    if config.provider != "ollama":
        return _status(
            "AI_MODEL",
            ComponentStateV1.UNKNOWN,
            True,
            "configured provider does not support model inspection",
            detected=config.model,
        )
    if provider_status.state != ComponentStateV1.READY:
        return _status(
            "AI_MODEL",
            ComponentStateV1.UNKNOWN,
            True,
            "model availability cannot be verified while Ollama is unavailable",
            RemediationCodeV1.START_AI_PROVIDER,
            config.model,
        )
    installer = _ollama_installer(platform_name)
    available = installer.model_available(config.model) if installer is not None else False
    return _status(
        "AI_MODEL",
        ComponentStateV1.READY if available else ComponentStateV1.NOT_INSTALLED,
        True,
        "configured Ollama model available"
        if available
        else "configured Ollama model is not installed",
        None if available else RemediationCodeV1.INSTALL_AI_MODEL,
        config.model,
        profile=_configured_ai_profile(config) or "UNKNOWN",
    )


def _configured_ai_profile(config: ChappieLocalConfigV1 | None) -> str | None:
    if config is None or not config.enabled_capabilities:
        return None
    return config.enabled_capabilities[0].upper()


def _disk_status(hardware: HardwareProfileV1) -> ComponentStatusV1:
    if hardware.disk_free_bytes is None:
        return _status(
            "DISK_SPACE", ComponentStateV1.UNKNOWN, True, "disk availability unavailable"
        )
    minimum = 2 * 1024**3
    return _status(
        "DISK_SPACE",
        ComponentStateV1.READY
        if hardware.disk_free_bytes >= minimum
        else ComponentStateV1.DEGRADED,
        True,
        "sufficient free disk space"
        if hardware.disk_free_bytes >= minimum
        else "less than 2 GiB free disk space",
        None if hardware.disk_free_bytes >= minimum else RemediationCodeV1.FREE_DISK_SPACE,
        str(hardware.disk_free_bytes),
        ">=2147483648",
    )


def format_status(status: ChappieStatusV1) -> str:
    separator = "-" * 24
    components = {item.component: item for item in status.components}
    provider = components.get("AI_PROVIDER")
    model = components.get("AI_MODEL")
    profile = dict(model.details).get("profile", "NOT_CONFIGURED") if model else "NOT_CONFIGURED"
    lines = [
        "CHAPPIE STATUS",
        "",
        "System",
        separator,
        f"OS              {status.platform}",
        f"Architecture    {status.architecture}",
        f"Hardware class  {status.hardware.capability.value}",
        f"AI profile      {profile}",
        "",
        "AI",
        separator,
        f"Provider        {provider.detected_version if provider else 'UNKNOWN'}",
        f"Runtime         {provider.state.value if provider else 'UNKNOWN'}",
        f"Model           {model.detected_version if model else 'UNKNOWN'}",
        f"Model status    {model.state.value if model else 'UNKNOWN'}",
        "",
        "Components",
        separator,
    ]
    lines.extend(f"{item.component:<20}{item.state.value}" for item in status.components)
    lines.extend(["", "Overall", separator, status.overall_state.value])
    return "\n".join(lines)


def status_exit_code(status: ChappieStatusV1) -> int:
    return (
        0
        if status.overall_state == InstallationStateV1.READY
        else 2
        if status.overall_state == InstallationStateV1.UNSUPPORTED
        else 1
    )


def json_output(value: Any) -> str:
    return canonical_json(value) + "\n"
