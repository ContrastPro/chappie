"""Safe, resumable Chappie setup engine.

Only actions classified SAFE can run with ``--yes``.  EXTERNAL and PRIVILEGED
actions are represented explicitly but are never silently authorised here.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from app.ai.schemas import canonical_json, digest
from app.ai_setup_smoke import AISetupSmokeStateV1, ChappieAISetupSmokeV1
from app.bootstrap import (
    AIModelProfileV1,
    ChappieLocalConfigV1,
    ChappiePaths,
    ComponentStateV1,
    SetupStepStateV1,
    load_local_config,
)
from app.macos_ollama import MacOllamaInstallerV1
from app.ollama_readiness import OllamaReadinessResultV1, OllamaReadinessStateV1
from app.project_qa_setup_smoke import ProjectQASetupSmokeStateV1, ProjectQASetupSmokeV1
from app.windows_ollama import WindowsOllamaInstallerV1

SETUP_ENGINE_IDENTITY = "chappie_setup_engine/1.0.0"
KNOWLEDGE_BUILD_BLOCKED_DIRTY_REPOSITORY = "KNOWLEDGE_BUILD_BLOCKED_DIRTY_REPOSITORY"


class ActionSafetyV1(StrEnum):
    SAFE = "SAFE"
    EXTERNAL = "EXTERNAL"
    PRIVILEGED = "PRIVILEGED"


class SetupActionCodeV1(StrEnum):
    INITIALIZE_DIRECTORIES = "INITIALIZE_DIRECTORIES"
    INITIALIZE_CONFIG = "INITIALIZE_CONFIG"
    CREATE_VENV = "CREATE_VENV"
    INSTALL_PROJECT = "INSTALL_PROJECT"
    BUILD_KNOWLEDGE = "BUILD_KNOWLEDGE"
    INSTALL_AI_PROVIDER = "INSTALL_AI_PROVIDER"
    INSTALL_AI_MODEL = "INSTALL_AI_MODEL"
    VERIFY_AI = "VERIFY_AI"
    PROJECT_QA_SMOKE = "PROJECT_QA_SMOKE"
    DEV_TOOLS = "DEV_TOOLS"


SETUP_ENGINE_DIGEST = digest(
    {
        "identity": SETUP_ENGINE_IDENTITY,
        "execution": "CHECK_APPLY_VERIFY",
        "safety": [item.value for item in ActionSafetyV1],
        "steps": [item.value for item in SetupActionCodeV1],
        "external_requires_explicit_authorization": True,
    }
)


@dataclass(frozen=True, slots=True)
class SetupApprovalV1:
    allow_safe: bool = False
    allow_external: bool = False
    allow_privileged: bool = False
    non_interactive: bool = False

    def permits(self, safety: ActionSafetyV1) -> bool:
        return (
            safety == ActionSafetyV1.SAFE
            and self.allow_safe
            or safety == ActionSafetyV1.EXTERNAL
            and self.allow_external
            or safety == ActionSafetyV1.PRIVILEGED
            and self.allow_privileged
        )


@dataclass(frozen=True, slots=True)
class SetupActionV1:
    code: SetupActionCodeV1
    safety: ActionSafetyV1
    description: str
    source: str | None
    estimated_storage_bytes: int | None = None
    requires_admin: bool = False
    depends_on: tuple[SetupActionCodeV1, ...] = ()

    def payload(self) -> dict[str, object]:
        return {
            "code": self.code.value,
            "safety": self.safety.value,
            "description": self.description,
            "source": self.source,
            "estimated_storage_bytes": self.estimated_storage_bytes,
            "requires_admin": self.requires_admin,
            "depends_on": [item.value for item in self.depends_on],
        }


@dataclass(frozen=True, slots=True)
class SetupStepResultV1:
    code: SetupActionCodeV1
    state: SetupStepStateV1
    message: str
    duration_ms: int = 0


@dataclass(frozen=True, slots=True)
class SetupRunStateV1:
    identity: str
    engine_digest: str
    steps: tuple[SetupStepResultV1, ...]

    def payload(self) -> dict[str, object]:
        return {
            "identity": self.identity,
            "engine_digest": self.engine_digest,
            "steps": [
                {
                    "code": item.code.value,
                    "state": item.state.value,
                    "message": item.message,
                    "duration_ms": item.duration_ms,
                }
                for item in self.steps
            ],
        }


class ProviderInstaller(Protocol):
    provider: str

    def detect(self) -> ComponentStateV1: ...

    def plan_install(self) -> SetupActionV1: ...


class OllamaReadinessCoordinator(Protocol):
    """The provider-owned start/readiness boundary used by Windows setup."""

    executable: str

    def ensure_ready(self) -> OllamaReadinessResultV1: ...


class OllamaInstallerV1:
    """Provider-aware planner. It deliberately does not invoke remote installers."""

    provider = "ollama"
    official_installation_docs = "https://ollama.com/download"

    def detect(self) -> ComponentStateV1:
        return (
            ComponentStateV1.DEGRADED if shutil.which("ollama") else ComponentStateV1.NOT_INSTALLED
        )

    def plan_install(self) -> SetupActionV1:
        return SetupActionV1(
            SetupActionCodeV1.INSTALL_AI_PROVIDER,
            ActionSafetyV1.EXTERNAL,
            "Install Ollama from its official download instructions.",
            self.official_installation_docs,
        )


def setup_state_path(paths: ChappiePaths) -> Path:
    return paths.state_root / "config" / "setup-state-v1.json"


def default_actions(
    *, developer: bool, ai_profile: AIModelProfileV1 | None
) -> tuple[SetupActionV1, ...]:
    actions = [
        SetupActionV1(
            SetupActionCodeV1.INITIALIZE_DIRECTORIES,
            ActionSafetyV1.SAFE,
            "Create Chappie local directories.",
            None,
        ),
        SetupActionV1(
            SetupActionCodeV1.INITIALIZE_CONFIG,
            ActionSafetyV1.SAFE,
            "Create non-secret local Chappie configuration if absent.",
            None,
            depends_on=(SetupActionCodeV1.INITIALIZE_DIRECTORIES,),
        ),
        SetupActionV1(
            SetupActionCodeV1.CREATE_VENV,
            ActionSafetyV1.SAFE,
            "Create or reuse repository .venv.",
            None,
        ),
        SetupActionV1(
            SetupActionCodeV1.INSTALL_PROJECT,
            ActionSafetyV1.SAFE,
            "Install Chappie from project metadata into its virtual environment.",
            None,
            depends_on=(SetupActionCodeV1.CREATE_VENV,),
        ),
        SetupActionV1(
            SetupActionCodeV1.BUILD_KNOWLEDGE,
            ActionSafetyV1.SAFE,
            "Build verified Knowledge from a clean committed snapshot.",
            None,
            depends_on=(SetupActionCodeV1.INITIALIZE_CONFIG,),
        ),
    ]
    if ai_profile is not None:
        installer = OllamaInstallerV1()
        actions.extend(
            (
                installer.plan_install(),
                SetupActionV1(
                    SetupActionCodeV1.INSTALL_AI_MODEL,
                    ActionSafetyV1.EXTERNAL,
                    "Download selected AI model after provider verification.",
                    "provider model catalog",
                    depends_on=(SetupActionCodeV1.INSTALL_AI_PROVIDER,),
                ),
                SetupActionV1(
                    SetupActionCodeV1.VERIFY_AI,
                    ActionSafetyV1.EXTERNAL,
                    "Verify provider and selected model without benchmark execution.",
                    None,
                    depends_on=(SetupActionCodeV1.INSTALL_AI_MODEL,),
                ),
                SetupActionV1(
                    SetupActionCodeV1.PROJECT_QA_SMOKE,
                    ActionSafetyV1.EXTERNAL,
                    "Verify grounded Project QA against canonical Knowledge.",
                    None,
                    depends_on=(
                        SetupActionCodeV1.BUILD_KNOWLEDGE,
                        SetupActionCodeV1.VERIFY_AI,
                    ),
                ),
            )
        )
    if developer:
        actions.append(
            SetupActionV1(
                SetupActionCodeV1.DEV_TOOLS,
                ActionSafetyV1.SAFE,
                "Install project developer extras from metadata; editor tooling remains optional.",
                None,
                depends_on=(SetupActionCodeV1.INSTALL_PROJECT,),
            )
        )
    return tuple(actions)


class SetupEngineV1:
    def __init__(
        self,
        paths: ChappiePaths,
        *,
        runner=subprocess.run,
        windows_installer: WindowsOllamaInstallerV1 | None = None,
        macos_installer: MacOllamaInstallerV1 | None = None,
        ollama_readiness_coordinator: OllamaReadinessCoordinator | None = None,
        smoke: ChappieAISetupSmokeV1 | object | None = None,
        project_qa_smoke: ProjectQASetupSmokeV1 | object | None = None,
    ) -> None:
        self.paths = paths
        self._runner = runner
        self._windows_installer = windows_installer
        self._macos_installer = macos_installer
        self._ollama_readiness_coordinator = ollama_readiness_coordinator
        self._smoke = smoke
        self._project_qa_smoke = project_qa_smoke

    def plan(
        self, *, developer: bool = False, ai_profile: AIModelProfileV1 | None = None
    ) -> tuple[SetupActionV1, ...]:
        return default_actions(developer=developer, ai_profile=ai_profile)

    def required_external_actions(
        self, *, ai_profile: AIModelProfileV1 | None = None
    ) -> tuple[SetupActionCodeV1, ...]:
        """Return only download/install actions still required by the current plan.

        This read-only preflight intentionally differs from execution readiness:
        an installed model without a bound Chappie config still needs binding and
        smoke verification, but it must never be classified as a model pull.
        """

        previous_profile = getattr(self, "_profile", None)
        try:
            profile = ai_profile or AIModelProfileV1.BALANCED
            self._profile = profile
            required: list[SetupActionCodeV1] = []
            for action in self.plan(ai_profile=profile):
                if action.code == SetupActionCodeV1.INSTALL_AI_PROVIDER:
                    if (
                        action.safety == ActionSafetyV1.EXTERNAL
                        and self._provider_install_required()
                    ):
                        required.append(action.code)
                elif (
                    action.code == SetupActionCodeV1.INSTALL_AI_MODEL
                    and self._model_pull_required()
                ):
                    required.append(action.code)
            return tuple(dict.fromkeys(required))
        finally:
            if previous_profile is None:
                if hasattr(self, "_profile"):
                    delattr(self, "_profile")
            else:
                self._profile = previous_profile

    def installed_catalog_profiles(self) -> tuple[AIModelProfileV1, ...]:
        """Read-only discovery of exact locally installed catalog model identities.

        This deliberately does not call the readiness coordinator or pull a model;
        it is safe to use before an interactive profile decision on every platform.
        """
        installer = self._ollama_installer()
        if installer is None or installer.executable() is None or installer.version() is None:
            return ()
        profiles: list[AIModelProfileV1] = []
        for profile in (
            AIModelProfileV1.LIGHT,
            AIModelProfileV1.BALANCED,
            AIModelProfileV1.ADVANCED,
        ):
            model = self._model_for_profile(profile)
            if model is not None and installer.model_available(model):
                profiles.append(profile)
        return tuple(profiles)

    def execute(
        self,
        *,
        approval: SetupApprovalV1,
        developer: bool = False,
        ai_profile: AIModelProfileV1 | None = None,
        dry_run: bool = False,
        progress: Callable[[SetupActionV1, SetupStepResultV1 | None], None] | None = None,
    ) -> SetupRunStateV1:
        self._profile = ai_profile or AIModelProfileV1.BALANCED
        actions = self.plan(developer=developer, ai_profile=ai_profile)
        if dry_run:
            return SetupRunStateV1(
                SETUP_ENGINE_IDENTITY,
                SETUP_ENGINE_DIGEST,
                tuple(
                    SetupStepResultV1(action.code, SetupStepStateV1.PENDING, "DRY_RUN")
                    for action in actions
                ),
            )
        previous = self._load_state()
        results: list[SetupStepResultV1] = []
        self._provider_runtime_ready = False
        self._smoke_verified_this_run = False
        self._project_qa_verified_this_run = False
        completed = {
            item.code for item in previous.steps if item.state == SetupStepStateV1.COMPLETED
        }
        for action in actions:
            if progress is not None:
                progress(action, None)
            if not self._dependencies_complete(action, completed):
                results.append(
                    SetupStepResultV1(
                        action.code, SetupStepStateV1.SKIPPED, "dependent step not verified"
                    )
                )
                continue
            ready = self._check(action)
            if ready:
                result = SetupStepResultV1(action.code, SetupStepStateV1.SKIPPED, "ALREADY_READY")
            elif not self._approval_permits(action, approval):
                result = SetupStepResultV1(
                    action.code,
                    SetupStepStateV1.SKIPPED,
                    f"APPROVAL_REQUIRED:{action.safety.value}",
                )
            else:
                started = time.monotonic()
                try:
                    self._apply(action, developer=developer, ai_profile=ai_profile)
                    ready = self._check(action)
                    if not ready:
                        raise RuntimeError("postcondition not satisfied")
                    result = SetupStepResultV1(
                        action.code,
                        SetupStepStateV1.COMPLETED,
                        "VERIFIED",
                        round((time.monotonic() - started) * 1000),
                    )
                except Exception as error:  # noqa: BLE001 - persist any setup operation failure safely
                    result = SetupStepResultV1(
                        action.code,
                        SetupStepStateV1.FAILED,
                        self._failure_reason(action, error),
                        round((time.monotonic() - started) * 1000),
                    )
            results.append(result)
            if progress is not None:
                progress(action, result)
            if action.code == SetupActionCodeV1.INSTALL_AI_PROVIDER:
                self._provider_runtime_ready = ready
            if (
                result.state
                in {
                    SetupStepStateV1.COMPLETED,
                    SetupStepStateV1.SKIPPED,
                }
                and ready
            ):
                completed.add(action.code)
            self._persist(
                SetupRunStateV1(SETUP_ENGINE_IDENTITY, SETUP_ENGINE_DIGEST, tuple(results))
            )
            if result.state == SetupStepStateV1.FAILED:
                break
        return SetupRunStateV1(SETUP_ENGINE_IDENTITY, SETUP_ENGINE_DIGEST, tuple(results))

    def _dependencies_complete(
        self, action: SetupActionV1, completed: set[SetupActionCodeV1]
    ) -> bool:
        if action.code == SetupActionCodeV1.INSTALL_AI_MODEL and not getattr(
            self, "_provider_runtime_ready", False
        ):
            return False
        return set(action.depends_on).issubset(completed)

    def _check(self, action: SetupActionV1) -> bool:
        if action.code == SetupActionCodeV1.INITIALIZE_DIRECTORIES:
            return all(
                path.is_dir()
                for path in (self.paths.logs_dir, self.paths.cache_dir, self.paths.models_dir)
            )
        if action.code == SetupActionCodeV1.INITIALIZE_CONFIG:
            try:
                return load_local_config(self.paths) is not None
            except ValueError:
                return False
        if action.code == SetupActionCodeV1.CREATE_VENV:
            return (self.paths.root / ".venv" / "pyvenv.cfg").is_file()
        if action.code == SetupActionCodeV1.INSTALL_PROJECT:
            return (self.paths.root / ".venv" / "bin" / "chappie").exists() or (
                self.paths.root / ".venv" / "Scripts" / "chappie.exe"
            ).exists()
        if action.code == SetupActionCodeV1.BUILD_KNOWLEDGE:
            try:
                from app.knowledge import committed_snapshot, read_store

                _, stored_manifest = read_store(self.paths.knowledge_file)
                return stored_manifest.snapshot == committed_snapshot(self.paths.root)
            except (OSError, ValueError, subprocess.SubprocessError):
                return False
        if action.code == SetupActionCodeV1.INSTALL_AI_PROVIDER:
            if installer := self._ollama_installer():
                executable = installer.executable()
                if executable is None or installer.version() is None:
                    return False
                if self._ollama_readiness_coordinator is None:
                    return False
                self._ollama_readiness_coordinator.executable = str(executable)
                return (
                    self._ollama_readiness_coordinator.ensure_ready().state
                    == OllamaReadinessStateV1.READY
                )
            return OllamaInstallerV1().detect() == ComponentStateV1.DEGRADED
        if action.code == SetupActionCodeV1.INSTALL_AI_MODEL:
            model = self._selected_model()
            return bool(
                (installer := self._ollama_installer())
                and model
                and installer.model_available(model)
                and self._config_is_bound(model)
            )
        if action.code == SetupActionCodeV1.VERIFY_AI:
            config = load_local_config(self.paths)
            return bool(
                config
                and config.provider
                and config.model
                and getattr(self, "_smoke_verified_this_run", False)
            )
        if action.code == SetupActionCodeV1.PROJECT_QA_SMOKE:
            return bool(getattr(self, "_project_qa_verified_this_run", False))
        return False

    def _apply(
        self, action: SetupActionV1, *, developer: bool, ai_profile: AIModelProfileV1 | None
    ) -> None:
        if (
            action.code == SetupActionCodeV1.INSTALL_AI_PROVIDER
            and (installer := self._ollama_installer()) is not None
        ):
            if installer.version() is not None:
                raise RuntimeError("installed Ollama provider is not runtime ready")
            result = installer.install(allow_external=True)
            if result.status.value != "COMPLETED":
                raise RuntimeError(result.error_code or "provider install failed")
            return
        if (
            action.code == SetupActionCodeV1.INSTALL_AI_MODEL
            and (installer := self._ollama_installer()) is not None
        ):
            if not getattr(self, "_provider_runtime_ready", False):
                raise RuntimeError("Ollama provider runtime is not ready")
            model = self._selected_model()
            if not model:
                raise RuntimeError("no selected model")
            if not installer.model_available(model):
                result = installer.pull_model(model, allow_external=True)
                if result.status.value != "COMPLETED":
                    raise RuntimeError("model pull failed")
            if not installer.model_available(model):
                raise RuntimeError("model verification failed")
            self._bind_config(model, ai_profile)
            return
        if action.code == SetupActionCodeV1.VERIFY_AI:
            config = load_local_config(self.paths)
            if config is None or not self._smoke_is_valid(config):
                raise RuntimeError("AI smoke failed")
            self._smoke_verified_this_run = True
            return
        if action.code == SetupActionCodeV1.PROJECT_QA_SMOKE:
            if self._project_qa_smoke is None:
                raise RuntimeError("Project QA smoke is not configured")
            config = load_local_config(self.paths)
            if config is None:
                raise RuntimeError("AI configuration is missing")
            result = self._project_qa_smoke.run(config, self.paths.knowledge_file)
            if result.state != ProjectQASetupSmokeStateV1.VALID:
                raise RuntimeError("Project QA smoke failed")
            self._project_qa_verified_this_run = True
            return
        if action.safety != ActionSafetyV1.SAFE:
            raise PermissionError(
                "external and privileged actions require a provider-specific executor"
            )
        if action.code == SetupActionCodeV1.INITIALIZE_DIRECTORIES:
            for path in (
                self.paths.logs_dir,
                self.paths.cache_dir,
                self.paths.models_dir,
                self.paths.research_dir,
            ):
                path.mkdir(parents=True, exist_ok=True)
        elif action.code == SetupActionCodeV1.INITIALIZE_CONFIG:
            self.paths.config_file.parent.mkdir(parents=True, exist_ok=True)
            if not self.paths.config_file.exists():
                self.paths.config_file.write_text(
                    canonical_json(ChappieLocalConfigV1().payload()) + "\n", encoding="utf-8"
                )
        elif action.code == SetupActionCodeV1.CREATE_VENV:
            self._runner([sys.executable, "-m", "venv", str(self.paths.root / ".venv")], check=True)
        elif action.code in {SetupActionCodeV1.INSTALL_PROJECT, SetupActionCodeV1.DEV_TOOLS}:
            python = (
                self.paths.root
                / ".venv"
                / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            )
            target = ".[dev]" if action.code == SetupActionCodeV1.DEV_TOOLS else "."
            self._runner(
                [str(python), "-m", "pip", "install", "-e", target], cwd=self.paths.root, check=True
            )
        elif action.code == SetupActionCodeV1.BUILD_KNOWLEDGE:
            from app.knowledge import committed_snapshot, initial_records, manifest, write_store

            snapshot = committed_snapshot(self.paths.root)
            records = initial_records(self.paths.root, snapshot)
            write_store(self.paths.knowledge_file, records, manifest(records, snapshot))
        else:
            raise RuntimeError("unsupported safe setup action")

    def _ollama_installer(self) -> WindowsOllamaInstallerV1 | MacOllamaInstallerV1 | None:
        return self._windows_installer or self._macos_installer

    def _provider_install_required(self) -> bool:
        """Read-only distinction between a missing provider and a non-ready runtime."""
        installer = self._ollama_installer()
        if installer is not None:
            return installer.executable() is None or installer.version() is None
        return OllamaInstallerV1().detect() != ComponentStateV1.DEGRADED

    def _model_pull_required(self) -> bool:
        """Read-only exact-model check; configuration binding is a later local step."""
        model = self._selected_model()
        installer = self._ollama_installer()
        return not bool(installer and model and installer.model_available(model))

    def _approval_safety(self, action: SetupActionV1) -> ActionSafetyV1:
        """Keep consent scoped to genuine provider installation or model download."""
        if action.code == SetupActionCodeV1.INSTALL_AI_PROVIDER:
            if self._ollama_installer() is None:
                return action.safety
            return (
                ActionSafetyV1.EXTERNAL
                if self._provider_install_required()
                else ActionSafetyV1.SAFE
            )
        if action.code == SetupActionCodeV1.INSTALL_AI_MODEL:
            if self._ollama_installer() is None:
                return action.safety
            return ActionSafetyV1.EXTERNAL if self._model_pull_required() else ActionSafetyV1.SAFE
        if action.code in {SetupActionCodeV1.VERIFY_AI, SetupActionCodeV1.PROJECT_QA_SMOKE}:
            return ActionSafetyV1.SAFE
        return action.safety

    def _approval_permits(self, action: SetupActionV1, approval: SetupApprovalV1) -> bool:
        safety = self._approval_safety(action)
        # Explicit external authorization is stronger than safe local approval,
        # but --yes never authorizes an external install or pull.
        return approval.permits(safety) or (
            safety == ActionSafetyV1.SAFE and approval.allow_external
        )

    @staticmethod
    def _failure_reason(action: SetupActionV1, error: Exception) -> str:
        if (
            action.code == SetupActionCodeV1.BUILD_KNOWLEDGE
            and isinstance(error, ValueError)
            and str(error) == "REFUSE_CANONICAL_INGESTION: repository working tree is dirty"
        ):
            return KNOWLEDGE_BUILD_BLOCKED_DIRTY_REPOSITORY
        return f"FAILED:{type(error).__name__}"

    def _selected_model(self) -> str | None:
        return self._model_for_profile(getattr(self, "_profile", AIModelProfileV1.BALANCED))

    @staticmethod
    def _model_for_profile(profile: AIModelProfileV1) -> str | None:
        return {
            AIModelProfileV1.LIGHT: "qwen3:4b",
            AIModelProfileV1.BALANCED: "qwen3:8b",
            AIModelProfileV1.ADVANCED: "qwen3:14b",
        }.get(profile)

    def _config_is_bound(self, model: str) -> bool:
        try:
            config = load_local_config(self.paths)
        except ValueError:
            return False
        profile = getattr(self, "_profile", AIModelProfileV1.BALANCED)
        return bool(
            config
            and config.provider == "ollama"
            and config.model == model
            and config.enabled_capabilities == (profile.value,)
        )

    def _smoke_is_valid(self, config: ChappieLocalConfigV1) -> bool:
        if self._smoke is None:
            return False
        if hasattr(self._smoke, "run"):
            return self._smoke.run(config).state == AISetupSmokeStateV1.VALID  # type: ignore[union-attr]
        return bool(self._smoke(config))  # type: ignore[operator]

    def _bind_config(self, model: str, profile: AIModelProfileV1 | None) -> None:
        path = self.paths.config_file
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(
            canonical_json(
                ChappieLocalConfigV1(
                    provider="ollama",
                    model=model,
                    enabled_capabilities=((profile or AIModelProfileV1.BALANCED).value,),
                ).payload()
            )
            + "\n",
            encoding="utf-8",
        )
        temp.replace(path)

    def _load_state(self) -> SetupRunStateV1:
        try:
            raw = json.loads(setup_state_path(self.paths).read_text(encoding="utf-8"))
            if (
                raw.get("identity") != SETUP_ENGINE_IDENTITY
                or raw.get("engine_digest") != SETUP_ENGINE_DIGEST
            ):
                raise ValueError
            return SetupRunStateV1(
                SETUP_ENGINE_IDENTITY,
                SETUP_ENGINE_DIGEST,
                tuple(
                    SetupStepResultV1(
                        SetupActionCodeV1(item["code"]),
                        SetupStepStateV1(item["state"]),
                        str(item["message"]),
                        int(item.get("duration_ms", 0)),
                    )
                    for item in raw["steps"]
                ),
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return SetupRunStateV1(SETUP_ENGINE_IDENTITY, SETUP_ENGINE_DIGEST, ())

    def _persist(self, state: SetupRunStateV1) -> None:
        path = setup_state_path(self.paths)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(canonical_json(state.payload()) + "\n", encoding="utf-8")
        temporary.replace(path)
