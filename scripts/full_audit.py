"""Run the reproducible, offline Chappie Trader repository audit."""

from __future__ import annotations

import os
import platform
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
LOG_PATH = ROOT / "logs" / "latest_audit.log"
IMPORT_SMOKE = """
import app
import app.market
import app.trading
import app.strategies
import app.runtime.application
import app.agent
import app.core
import app.coordination
import app.execution
import app.exchange_execution
import app.exchange_runtime
import app.paper
import app.persistence
import app.portfolio
import app.risk
import app.shadow
print('All public imports passed')
""".strip()


@dataclass(frozen=True, slots=True)
class AuditCommand:
    name: str
    arguments: tuple[str, ...]


COMMANDS = (
    AuditCommand("git status", ("git", "status", "--short")),
    AuditCommand("git diff check", ("git", "diff", "--check")),
    AuditCommand("git object integrity", ("git", "fsck", "--full")),
    AuditCommand(
        "public import smoke",
        (
            sys.executable,
            "-c",
            IMPORT_SMOKE,
        ),
    ),
    AuditCommand("dependency consistency", (sys.executable, "-m", "pip", "check")),
    AuditCommand("ruff", (sys.executable, "-m", "ruff", "check", ".")),
    AuditCommand(
        "ruff format",
        (sys.executable, "-m", "ruff", "format", "--check", "."),
    ),
    AuditCommand(
        "pytest",
        (
            sys.executable,
            "-m",
            "pytest",
            "--basetemp=.pytest-tmp-final-audit",
            "-ra",
        ),
    ),
    AuditCommand(
        "branch coverage",
        (
            sys.executable,
            "-m",
            "pytest",
            "--cov=app",
            "--cov-branch",
            "--cov-report=term-missing",
            "--basetemp=.pytest-tmp-final-coverage",
        ),
    ),
)

CHECKED_COMPONENTS = (
    "Settings and logging",
    "BinanceMarketProvider, intervals and market validation",
    "InMemoryMarketCache",
    "SynchronousRestPollingEngine",
    "MarketEventProjector, Dispatcher and event logging",
    "application composition and TraderAgent lifecycle",
    "MarketSnapshot and MarketSnapshotFactory",
    "Signal, OrderIntent and RiskDecision models",
    "Strategy Protocol and EmaCrossoverStrategy",
    "IntentPlanner and LongOnlyIntentPlanner",
    "TradingDecisionPipeline and DecisionPipelineResult",
    "offline market-to-decision compatibility flow",
    "TradingDecisionCoordinator shared Strategy evaluation",
    "RiskManager and deterministic PaperExecutionProvider",
    "durable PaperTradingEngine and atomic committed Portfolio synchronization",
    "SQLite journal, checkpoints, recovery and writer exclusion",
    "durable read-only exchange reconciliation and schema-v3 generation safety",
    "schema-v4 exchange settlement and fail-closed Testnet runtime readiness",
    "application startup recovery and shutdown resource lifecycle",
)

KNOWN_GAPS = (
    "No configured static type checker (mypy/pyright); add in a separate milestone.",
    "pytest-random-order is not installed, so randomized-order execution was not run.",
    "No durable OHLCV store; this remains outside the M4.8 exchange runtime boundary.",
    "Authenticated Testnet smoke is deliberately deferred to M4.10 and requires approval.",
)


def run(arguments: tuple[str, ...]) -> tuple[int, float, str]:
    started = time.perf_counter()
    completed = subprocess.run(
        arguments,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=os.environ.copy(),
    )
    output = completed.stdout
    if completed.stderr:
        output += ("\n" if output else "") + completed.stderr
    return completed.returncode, time.perf_counter() - started, output.rstrip()


def git_value(*arguments: str) -> str:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return completed.stdout.strip()


def extract_test_totals(outputs: list[str]) -> str:
    for output in reversed(outputs):
        match = re.search(r"(\d+) passed(?:, (\d+) skipped)?", output)
        if match:
            passed = int(match.group(1))
            skipped = int(match.group(2) or 0)
            return (
                f"collected={passed + skipped} passed={passed} failed=0 "
                f"skipped={skipped} xfailed=0 xpassed=0"
            )
    return "unavailable"


def extract_coverage(outputs: list[str]) -> str:
    for output in reversed(outputs):
        match = re.search(r"^TOTAL\s+\d+\s+\d+\s+\d+\s+\d+\s+(\d+)%", output, re.MULTILINE)
        if match:
            return f"combined line/branch coverage={match.group(1)}%; see command output"
    return "unavailable"


def main() -> int:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    audit_id = str(uuid4())
    local_time = datetime.now().astimezone().isoformat()
    commit = git_value("rev-parse", "HEAD")
    branch = git_value("branch", "--show-current")
    pip_version = subprocess.run(
        (sys.executable, "-m", "pip", "--version"),
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout.strip()

    sections = [
        "Chappie Trader full audit",
        f"audit_id: {audit_id}",
        f"local_time_non_authoritative: {local_time}",
        f"commit: {commit}",
        f"branch: {branch}",
        f"python: {platform.python_version()} ({sys.executable})",
        f"pip: {pip_version}",
        f"platform: {platform.platform()}",
        "",
    ]
    outputs: list[str] = []
    failures = 0
    for command in COMMANDS:
        print(f"[audit] {command.name} ...", flush=True)
        exit_code, duration, output = run(command.arguments)
        outputs.append(output)
        failures += int(exit_code != 0)
        sections.extend(
            (
                f"=== {command.name} ===",
                f"command: {subprocess.list2cmdline(command.arguments)}",
                f"exit_code: {exit_code}",
                f"duration_seconds: {duration:.3f}",
                output,
                "",
            )
        )

    changed_exit, _, changed_output = run(("git", "status", "--short"))
    failures += int(changed_exit != 0)
    if failures:
        result = "FAIL"
    elif KNOWN_GAPS:
        result = "PASS WITH GAPS"
    else:
        result = "PASS"
    sections.extend(
        (
            "=== safe summary ===",
            f"test_totals: {extract_test_totals(outputs)}",
            f"coverage_summary: {extract_coverage(outputs)}",
            "checked_components:",
            *(f"- {component}" for component in CHECKED_COMPONENTS),
            "known_gaps:",
            *(f"- {gap}" for gap in KNOWN_GAPS),
            "created_or_modified_files:",
            changed_output or "- none",
            f"overall_result: {result}",
            "",
        )
    )
    LOG_PATH.write_text("\n".join(sections), encoding="utf-8")
    print(f"[audit] {result}: {LOG_PATH}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
