"""Official, small Chappie product CLI; development tooling stays behind ``dev``."""

from __future__ import annotations

import argparse
import platform
import subprocess
import threading
from collections.abc import Sequence
from typing import ClassVar

from app.ai_setup_smoke import ChappieAISetupSmokeV1
from app.bootstrap import (
    CHAPPIE_VERSION,
    TEST_MODEL_CATALOG,
    ChappiePaths,
    collect_status,
    detect_hardware,
    doctor,
    format_status,
    json_output,
    load_local_config,
    recommend_model_profiles,
    status_exit_code,
)
from app.control_plane import ChappieControlPlaneV1
from app.control_plane_transport import (
    CONTROL_PLANE_TRANSPORT_IDENTITY,
    DEFAULT_CONTROL_PLANE_PORT,
    ChappieControlPlaneServerV1,
    ControlPlaneTransportErrorV1,
)
from app.macos_ollama import MacOllamaInstallerV1
from app.ollama_readiness import OllamaReadinessProbe
from app.ollama_start import OllamaReadinessCoordinatorV1
from app.project_qa_setup_smoke import ProjectQASetupSmokeV1
from app.setup_engine import (
    KNOWLEDGE_BUILD_BLOCKED_DIRTY_REPOSITORY,
    PROJECT_QA_SMOKE_FAILED_PREFIX,
    AIModelProfileV1,
    SetupActionCodeV1,
    SetupApprovalV1,
    SetupEngineV1,
)
from app.vscode_integration import VscodeExtensionInstallerV1
from app.windows_ollama import WindowsOllamaInstallerV1


def _launch_current_user_ollama(argv: list[str]) -> None:
    """Start Ollama without waiting for its long-running server process."""
    subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _launch_macos_ollama(_argv: list[str]) -> None:
    """Open the locally installed Ollama application; never invoke a shell or sudo."""
    installer = MacOllamaInstallerV1()
    bundle = installer.app_bundle()
    if bundle is None:
        raise RuntimeError("Ollama.app is not installed")
    subprocess.Popen(
        ["/usr/bin/open", "-a", str(bundle)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


def _setup_engine(paths: ChappiePaths, *, interactive_output: bool = True) -> SetupEngineV1:
    """Construct platform-owned setup dependencies; Windows has a real executor."""
    if platform.system() == "Windows":
        return SetupEngineV1(
            paths,
            windows_installer=WindowsOllamaInstallerV1(interactive_output=interactive_output),
            ollama_readiness_coordinator=OllamaReadinessCoordinatorV1(
                OllamaReadinessProbe(), "ollama", _launch_current_user_ollama
            ),
            smoke=ChappieAISetupSmokeV1(),
            project_qa_smoke=ProjectQASetupSmokeV1(),
            vscode_extension=VscodeExtensionInstallerV1(paths.root),
        )
    if platform.system() == "Darwin":
        installer = MacOllamaInstallerV1(interactive_output=interactive_output)
        return SetupEngineV1(
            paths,
            macos_installer=installer,
            ollama_readiness_coordinator=OllamaReadinessCoordinatorV1(
                OllamaReadinessProbe(),
                str(installer.executable() or "ollama"),
                _launch_macos_ollama,
            ),
            smoke=ChappieAISetupSmokeV1(),
            project_qa_smoke=ProjectQASetupSmokeV1(),
            vscode_extension=VscodeExtensionInstallerV1(paths.root),
        )
    return SetupEngineV1(paths)


def _control_plane(paths: ChappiePaths) -> ChappieControlPlaneV1:
    """Build the frontend-neutral read-only service used by CLI adapters."""
    return ChappieControlPlaneV1(paths, status_collector=collect_status)


def _control_plane_server(paths: ChappiePaths, *, port: int) -> ChappieControlPlaneServerV1:
    return ChappieControlPlaneServerV1(_control_plane(paths), port=port)


def _serve_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("port must be an integer") from error
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


class _SetupProgress:
    """Human-only bounded progress for potentially silent setup actions."""

    _LONG: ClassVar[set[SetupActionCodeV1]] = {
        SetupActionCodeV1.CREATE_VENV,
        SetupActionCodeV1.INSTALL_PROJECT,
        SetupActionCodeV1.BUILD_KNOWLEDGE,
        SetupActionCodeV1.INSTALL_AI_PROVIDER,
        SetupActionCodeV1.VERIFY_AI,
        SetupActionCodeV1.PROJECT_QA_SMOKE,
        SetupActionCodeV1.INSTALL_VSCODE_EXTENSION,
    }
    _LABELS: ClassVar[dict[SetupActionCodeV1, str]] = {
        SetupActionCodeV1.BUILD_KNOWLEDGE: "Building Chappie Knowledge",
        SetupActionCodeV1.INSTALL_AI_PROVIDER: "Installing and starting Ollama",
        SetupActionCodeV1.INSTALL_AI_MODEL: "Installing selected local model",
        SetupActionCodeV1.VERIFY_AI: "Verifying Chappie AI structured response",
        SetupActionCodeV1.PROJECT_QA_SMOKE: "Verifying Chappie Project Knowledge",
        SetupActionCodeV1.INSTALL_VSCODE_EXTENSION: "Installing Chappie VS Code extension",
    }

    def __init__(self, total: int) -> None:
        self._total, self._number, self._stop = total, 0, threading.Event()
        self._thread: threading.Thread | None = None
        self._last_step_complete_emitted = False

    def __call__(self, action, result) -> None:  # type: ignore[no-untyped-def]
        if result is None:
            self.close()
            self._number += 1
            label = self._LABELS.get(action.code, action.description)
            print(f"[{self._number}/{self._total}] {label}...")
            if action.code in self._LONG:
                self._stop.clear()
                self._thread = threading.Thread(target=self._heartbeat, daemon=True)
                self._thread.start()
            return
        self.close()
        if result.state.value == "FAILED":
            if result.message == KNOWLEDGE_BUILD_BLOCKED_DIRTY_REPOSITORY:
                print(
                    "      Knowledge build blocked: canonical Knowledge requires a clean committed source snapshot."
                )
                print("      Uncommitted source changes detected.")
                print(
                    "      Remediation: commit or stash the intended source changes, then rerun setup."
                )
                return
            if result.message.startswith(PROJECT_QA_SMOKE_FAILED_PREFIX):
                state = result.message.removeprefix(PROJECT_QA_SMOKE_FAILED_PREFIX)
                messages = {
                    "KNOWLEDGE_ERROR": "Project QA smoke failed: canonical Knowledge validation failed.",
                    "RETRIEVAL_ERROR": "Project QA smoke failed: deterministic Knowledge retrieval failed.",
                    "CONFIG_ERROR": "Project QA smoke failed: local AI configuration is invalid.",
                    "PROVIDER_ERROR": "Project QA smoke failed: local AI provider is unavailable.",
                    "TIMEOUT": "Project QA smoke failed: local inference timed out.",
                    "VALIDATION_ERROR": "Project QA smoke failed: local model response did not satisfy the canonical Project QA schema.",
                    "GROUNDING_ERROR": "Project QA smoke failed: grounding or authority validation failed.",
                }
                print(
                    f"      {messages.get(state, 'Project QA smoke failed: internal validation error.')}"
                )
                return
            print(f"      {result.message}.")
            return
        self._last_step_complete_emitted = self._number == self._total
        print("      Complete.")

    def emit_last_step_complete_if_missing(self) -> None:
        if not self._last_step_complete_emitted and self._number == self._total:
            print("      Complete.")
            self._last_step_complete_emitted = True

    def _heartbeat(self) -> None:
        elapsed = 0
        while not self._stop.wait(15):
            elapsed += 15
            print(f"      Still working... {elapsed}s")

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.1)
            self._thread = None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chappie", description="Chappie product control plane")
    parser.add_argument("--version", action="version", version=f"Chappie {CHAPPIE_VERSION}")
    sub = parser.add_subparsers(dest="command")
    for name in ("status", "doctor"):
        command = sub.add_parser(name)
        command.add_argument("--json", action="store_true")
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=_serve_port, default=DEFAULT_CONTROL_PLANE_PORT)
    serve.add_argument("--json", action="store_true")
    setup = sub.add_parser("setup")
    setup.add_argument("--plan", action="store_true")
    setup.add_argument("--dry-run", action="store_true")
    setup.add_argument("--yes", action="store_true")
    setup.add_argument("--allow-external", action="store_true")
    setup.add_argument("--developer", action="store_true")
    setup.add_argument("--non-interactive", action="store_true")
    setup.add_argument("--interactive", action="store_true")
    setup.add_argument("--json", action="store_true")
    setup.add_argument("--ai-profile", choices=[item.value.lower() for item in AIModelProfileV1])
    ai = sub.add_parser("ai")
    ai_sub = ai.add_subparsers(dest="ai_command")
    for name in ("status", "models"):
        command = ai_sub.add_parser(name)
        command.add_argument("--json", action="store_true")
    knowledge = sub.add_parser("knowledge")
    knowledge_sub = knowledge.add_subparsers(dest="knowledge_command")
    for name in ("status", "verify"):
        command = knowledge_sub.add_parser(name)
        command.add_argument("--json", action="store_true")
    sub.add_parser("research")
    sub.add_parser("dev")
    return parser


def _interactive_ai_profile(
    paths: ChappiePaths, parser: argparse.ArgumentParser
) -> AIModelProfileV1 | None:
    recommendation = recommend_model_profiles(detect_hardware(paths), TEST_MODEL_CATALOG)
    recommended = ", ".join(recommendation["recommended"]) or "none"
    print(f"Recommended local models: {recommended}")
    value = input("Select AI profile [LIGHT/BALANCED/ADVANCED] or SKIP: ").strip().upper()
    if value in {"", "SKIP"}:
        return None
    try:
        return AIModelProfileV1(value)
    except ValueError:
        parser.error("AI profile must be LIGHT, BALANCED, ADVANCED, or SKIP")
        raise AssertionError("argparse.error must exit")


def _config_profile(paths: ChappiePaths) -> AIModelProfileV1 | None:
    try:
        config = load_local_config(paths)
    except ValueError:
        return None
    if config is None or config.provider != "ollama":
        return None
    if not config.enabled_capabilities:
        return None
    try:
        return AIModelProfileV1(config.enabled_capabilities[0].upper())
    except ValueError:
        return None


def _config_represented_model(
    config_profile: AIModelProfileV1, config_model: str | None
) -> str | None:
    if config_model is None:
        return None
    expected = {
        AIModelProfileV1.LIGHT: "qwen3:4b",
        AIModelProfileV1.BALANCED: "qwen3:8b",
        AIModelProfileV1.ADVANCED: "qwen3:14b",
    }.get(config_profile)
    return expected


def _can_reuse_existing_ai_profile(
    engine: SetupEngineV1, paths: ChappiePaths
) -> AIModelProfileV1 | None:
    config_profile = _config_profile(paths)
    if config_profile is None:
        return None
    config = load_local_config(paths)
    if config is None:
        return None
    expected_model = _config_represented_model(config_profile, config.model)
    if expected_model is None or config.model != expected_model:
        return None
    if engine.required_external_actions(ai_profile=config_profile):
        return None
    return config_profile


def _adopt_unique_local_ai_profile(engine: SetupEngineV1) -> AIModelProfileV1 | None:
    """Adopt one unambiguous installed catalog model without starting or changing Ollama."""
    profiles = engine.installed_catalog_profiles()
    return profiles[0] if len(profiles) == 1 else None


def _ask_for_external_authorization(needed: tuple[SetupActionCodeV1, ...]) -> bool:
    if not needed:
        print("External work   NOT REQUIRED")
        return True
    print("External work   REQUIRED")
    for action in needed:
        if action == SetupActionCodeV1.INSTALL_AI_PROVIDER:
            print(" - Ollama provider install/update")
        elif action == SetupActionCodeV1.INSTALL_AI_MODEL:
            print(" - AI model selection/download")
    answer = (
        input("Allow Chappie to download/install external AI components? [y/N] ").strip().lower()
    )
    return answer in {"y", "yes"}


def main(argv: Sequence[str] | None = None, *, paths: ChappiePaths | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    paths = paths or ChappiePaths.resolve()
    if args.command is None:
        status = collect_status(paths)
        print(f"Chappie {CHAPPIE_VERSION}\n")
        print(format_status(status))
        print("\nCommands: status, setup --plan, doctor, ai, knowledge, research, dev")
        return status_exit_code(status)
    if args.command == "status":
        status = collect_status(paths)
        print(json_output(status.payload()), end="") if args.json else print(format_status(status))
        return status_exit_code(status)
    if args.command == "doctor":
        report = doctor(paths)
        if args.json:
            print(json_output(report.payload()), end="")
        else:
            print("CHAPPIE DOCTOR")
            for issue in report.issues:
                action = f" [{issue.remediation_code.value}]" if issue.remediation_code else ""
                print(f"{issue.severity} {issue.component}: {issue.message}{action}")
            if not report.issues:
                print("No issues detected.")
        return status_exit_code(report.status)
    if args.command == "serve":
        server = _control_plane_server(paths, port=args.port)
        try:
            server.start()
        except ControlPlaneTransportErrorV1 as error:
            payload = {
                "transport_contract": CONTROL_PLANE_TRANSPORT_IDENTITY,
                "status": "ERROR",
                "error": {"code": error.code, "message": error.message},
            }
            print(json_output(payload), end="") if args.json else print(error.message)
            return 1
        startup = server.startup_payload()
        if args.json:
            print(json_output(startup), end="")
        else:
            print("Chappie Control Plane")
            print(f"Listening on {startup['endpoint']}")
            print(f"Contract: {startup['transport_contract']}")
            print("Ctrl+C to stop.")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.shutdown()
        return 0
    if args.command == "setup":
        engine = _setup_engine(paths, interactive_output=not args.json)
        profile = None
        if args.ai_profile:
            profile = AIModelProfileV1(args.ai_profile.upper())
        elif not (args.non_interactive or args.plan or args.dry_run):
            profile = _can_reuse_existing_ai_profile(engine, paths)
            if profile is not None:
                config = load_local_config(paths)
                print("Current AI configuration:")
                print("  Provider: ollama")
                print(f"  Profile: {profile.name}")
                print(f"  Model: {config.model if config is not None else 'unknown'}")
                print("Using existing verified AI configuration.")
            else:
                profile = _adopt_unique_local_ai_profile(engine)
                if profile is not None:
                    model = SetupEngineV1._model_for_profile(profile)
                    if args.interactive and not args.json:
                        print("Existing local AI detected:")
                        print("  Provider: ollama")
                        print(f"  Profile: {profile.name}")
                        print(f"  Model: {model}")
                        print("Using existing local AI configuration.")
                elif args.interactive and not args.non_interactive:
                    profile = _interactive_ai_profile(paths, parser)
        actions = engine.plan(developer=args.developer, ai_profile=profile)
        needs_external = engine.required_external_actions(ai_profile=profile)
        if args.plan:
            print(
                json_output(
                    {
                        "identity": "chappie_setup_engine/1.0.0",
                        "actions": [item.payload() for item in actions],
                    }
                ),
                end="",
            )
            return 0
        progress = None if args.json else _SetupProgress(len(actions))
        if needs_external and not args.allow_external:
            if args.non_interactive:
                if args.json:
                    print(
                        json_output(
                            {
                                "identity": "chappie_setup/1.0.0",
                                "status": "SETUP_REQUIRED",
                                "external_authorization_required": True,
                                "missing_external_work": [item.value for item in needs_external],
                            }
                        ),
                        end="",
                    )
                else:
                    print("External authorization required for required AI work.")
                return 1
            if (
                args.interactive
                and not args.json
                and not _ask_for_external_authorization(needs_external)
            ):
                print("External authorization required to continue setup.")
                return 1
        if args.json and args.non_interactive and not needs_external:
            # JSON mode intentionally stays silent; success is represented in result payload.
            pass
        elif not args.json and profile is not None and not needs_external and args.interactive:
            print("External work   NOT REQUIRED")
        try:
            result = engine.execute(
                approval=SetupApprovalV1(
                    allow_safe=args.yes,
                    allow_external=args.allow_external,
                    non_interactive=args.non_interactive,
                ),
                developer=args.developer,
                ai_profile=profile,
                dry_run=args.dry_run,
                progress=progress,
            )
        finally:
            if progress is not None:
                progress.close()
        if args.json:
            print(json_output(result.payload()), end="")
        else:
            if (
                progress is not None
                and all(item.state.value != "FAILED" for item in result.steps)
                and result.steps
                and result.steps[-1].code == SetupActionCodeV1.PROJECT_QA_SMOKE
            ):
                progress.emit_last_step_complete_if_missing()
            print(
                "CHAPPIE SETUP COMPLETE"
                if all(x.state.value != "FAILED" for x in result.steps)
                else "CHAPPIE SETUP FAILED"
            )
        return 0 if all(item.state != "FAILED" for item in result.steps) else 1
    if args.command == "ai" and args.ai_command in {"status", "models"}:
        if args.ai_command == "status":
            ai_status = _control_plane(paths).get_ai_status()
            payload = ai_status.payload()
            if args.json:
                print(json_output(payload), end="")
            else:
                print(
                    "\n".join(
                        (
                            f"AI_PROVIDER: {ai_status.provider_state or 'UNKNOWN'}",
                            f"AI_MODEL: {ai_status.model_state or 'UNKNOWN'}",
                            f"PROVIDER: {ai_status.provider or 'UNKNOWN'}",
                            f"PROFILE: {ai_status.profile or 'UNKNOWN'}",
                            f"MODEL: {ai_status.model or 'UNKNOWN'}",
                        )
                    )
                )
            return 0
        data = {
            "identity": TEST_MODEL_CATALOG.identity,
            "models": [
                {
                    "profile": item.profile.value,
                    "provider": item.provider,
                    "model": item.model,
                    "structured_output_compatible": item.structured_output_compatible,
                }
                for item in TEST_MODEL_CATALOG.entries
            ],
        }
        print(json_output(data), end="") if args.json else print(
            "\n".join(
                f"{row['profile']}: {row['provider']}/{row['model']}" for row in data["models"]
            )
        )
        return 0
    if args.command == "knowledge" and args.knowledge_command in {"status", "verify"}:
        status = collect_status(paths)
        data = next(item.payload() for item in status.components if item.component == "KNOWLEDGE")
        print(json_output(data), end="") if args.json else print(
            f"KNOWLEDGE: {data['state']}\n{data['message']}"
        )
        return 0 if data["state"] == "READY" else 1
    if args.command in {"ai", "knowledge", "research", "dev"}:
        print("NOT_IMPLEMENTED")
        return 1
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
