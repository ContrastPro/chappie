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
    recommend_model_profiles,
    status_exit_code,
)
from app.ollama_readiness import OllamaReadinessProbe
from app.ollama_start import OllamaReadinessCoordinatorV1
from app.project_qa_setup_smoke import ProjectQASetupSmokeV1
from app.setup_engine import AIModelProfileV1, SetupActionCodeV1, SetupApprovalV1, SetupEngineV1
from app.windows_ollama import WindowsOllamaInstallerV1


def _launch_current_user_ollama(argv: list[str]) -> None:
    """Start Ollama without waiting for its long-running server process."""
    subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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
        )
    return SetupEngineV1(paths)


class _SetupProgress:
    """Human-only bounded progress for potentially silent setup actions."""

    _LONG: ClassVar[set[SetupActionCodeV1]] = {
        SetupActionCodeV1.CREATE_VENV,
        SetupActionCodeV1.INSTALL_PROJECT,
        SetupActionCodeV1.BUILD_KNOWLEDGE,
        SetupActionCodeV1.INSTALL_AI_PROVIDER,
        SetupActionCodeV1.VERIFY_AI,
        SetupActionCodeV1.PROJECT_QA_SMOKE,
    }
    _LABELS: ClassVar[dict[SetupActionCodeV1, str]] = {
        SetupActionCodeV1.BUILD_KNOWLEDGE: "Building Chappie Knowledge",
        SetupActionCodeV1.INSTALL_AI_PROVIDER: "Installing and starting Ollama",
        SetupActionCodeV1.INSTALL_AI_MODEL: "Installing selected local model",
        SetupActionCodeV1.VERIFY_AI: "Verifying Chappie AI structured response",
        SetupActionCodeV1.PROJECT_QA_SMOKE: "Verifying Chappie Project Knowledge",
    }

    def __init__(self, total: int) -> None:
        self._total, self._number, self._stop = total, 0, threading.Event()
        self._thread: threading.Thread | None = None

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
        print("      Complete." if result.state.value != "FAILED" else f"      {result.message}.")

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
    if args.command == "setup":
        profile = (
            AIModelProfileV1(args.ai_profile.upper())
            if args.ai_profile
            else None
            if args.non_interactive or args.plan or args.dry_run or not args.interactive
            else _interactive_ai_profile(paths, parser)
        )
        engine = _setup_engine(paths, interactive_output=not args.json)
        actions = engine.plan(developer=args.developer, ai_profile=profile)
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
            print(
                "CHAPPIE SETUP COMPLETE"
                if all(x.state.value != "FAILED" for x in result.steps)
                else "CHAPPIE SETUP FAILED"
            )
        return 0 if all(item.state != "FAILED" for item in result.steps) else 1
    if args.command == "ai" and args.ai_command in {"status", "models"}:
        if args.ai_command == "status":
            status = collect_status(paths)
            data = {
                item.component: item.payload()
                for item in status.components
                if item.component in {"AI_PROVIDER", "AI_MODEL"}
            }
            print(json_output(data), end="") if args.json else print(
                "\n".join(f"{key}: {value['state']}" for key, value in data.items())
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
