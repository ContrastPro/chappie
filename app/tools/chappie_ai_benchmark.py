"""Run the research-only Chappie AI benchmark locally (Ollama is opt-in)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.ai.benchmark import (
    BenchmarkTrack,
    TemporalSplit,
    benchmark_root,
    completed_runs,
    controlled_cases,
    execute_case_isolated,
    sample_market_only_cases,
    select_case_invocation,
    write_aggregate_report,
    write_case_manifest,
)
from app.ai.providers import OllamaProvider
from app.ai.router import ModelRouter
from app.ai.runtime import AIConfig, ChappieAI
from app.ai.tasks import AITask, ModelProfile, task_contract
from app.ai.trace import AITraceEventType, AITraceRenderer, TraceVerbosity, trace_event
from app.research.data.store import ResearchStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("ollama",), required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--track", choices=("reasoning-safety", "historical"), required=True)
    parser.add_argument("--split", choices=("development", "validation", "held-out"))
    parser.add_argument(
        "--limit", type=int, help="bounded invocation selection; default is full track"
    )
    parser.add_argument("--case-id")
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=30.0,
        help="provider-neutral request timeout; default: 30 seconds",
    )
    parser.add_argument(
        "--reasoning-contract",
        choices=("1.0.0", "1.1.0", "1.1.1"),
        default="1.0.0",
        help="explicit reasoning-output contract; v1.0.0 remains the frozen default",
    )
    parser.add_argument("--output-dir", type=Path, default=benchmark_root())
    parser.add_argument(
        "--store", type=Path, help="verified compact research SQLite store for historical track"
    )
    parser.add_argument("--asset", default="BTCUSDT")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--rerun", action="store_true")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--verbose", action="store_true")
    output.add_argument("--trace", action="store_true")
    output.add_argument("--quiet", action="store_true")
    output.add_argument("--json", action="store_true", help="preserve machine-only console output")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.reasoning_contract in {"1.1.0", "1.1.1"} and args.track != "reasoning-safety":
        raise SystemExit("--reasoning-contract 1.1.x currently supports reasoning-safety only")
    if args.timeout_seconds <= 0:
        raise SystemExit("--timeout-seconds must be positive")
    store: ResearchStore | None = None
    if args.track == "historical":
        if args.store is None:
            raise SystemExit("--store is required for the historical track; no implicit data read")
        if not args.store.is_file():
            raise SystemExit("--store must name an existing verified compact SQLite store")
        if args.split is None:
            raise SystemExit("--split is required for the historical track")
        store = ResearchStore(args.store)
        universe = [
            item.case
            for item in sample_market_only_cases(
                store,
                asset=args.asset,
                limit=100,
                split=TemporalSplit(args.split.upper().replace("-", "_")),
            )
        ]
        track = BenchmarkTrack.BLIND_HISTORICAL
    else:
        universe = list(controlled_cases())
        track = BenchmarkTrack.REASONING_SAFETY
    task = {
        "1.0.0": AITask.MARKET_INTERPRETATION,
        "1.1.0": AITask.RESEARCH_REASONING_V11,
        "1.1.1": AITask.RESEARCH_REASONING_V111,
    }[args.reasoning_contract]
    selected = list(select_case_invocation(tuple(universe), case_id=args.case_id, limit=args.limit))
    provider = OllamaProvider(
        endpoint="http://localhost:11434", model=args.model, timeout_seconds=args.timeout_seconds
    )
    ai = ChappieAI(
        ModelRouter({ModelProfile.LOCAL_DEFAULT: provider}),
        AIConfig(model=args.model, timeout_seconds=args.timeout_seconds),
    )
    verbosity = (
        TraceVerbosity.TRACE
        if args.trace
        else TraceVerbosity.VERBOSE
        if args.verbose
        else TraceVerbosity.QUIET
        if args.quiet or args.json
        else TraceVerbosity.COMPACT
    )
    renderer = AITraceRenderer(verbosity, sys.stdout)
    try:
        write_case_manifest(args.output_dir, tuple(universe), track=track)
        renderer.emit(
            trace_event(
                AITraceEventType.BENCHMARK_STARTED,
                provider=provider.provider_identity,
                model=provider.model_identity,
                metadata={
                    "benchmark_identity": "chappie_ai_market_benchmark/1.0.0",
                    "case_count": len(selected),
                    "resume": not args.rerun,
                    "reasoning_contract": args.reasoning_contract,
                    "timeout_seconds": args.timeout_seconds,
                },
            )
        )
        result = []
        summary = {
            "selected": len(selected),
            "completed": 0,
            "resumed": 0,
            "provider_timeouts": 0,
            "provider_errors": 0,
            "validation_rejects": 0,
        }
        for index, case in enumerate(selected, start=1):
            execution = execute_case_isolated(
                ai,
                case,
                root=args.output_dir,
                rerun=args.rerun,
                trace=renderer.emit,
                selection_index=index,
                selection_total=len(selected),
                task=task,
            )
            if execution.run is not None:
                summary["resumed" if execution.resumed else "completed"] += 1
                result.append(
                    {
                        "case_id": case.case_id,
                        "resumed": execution.resumed,
                        "run_identity": execution.run.identity,
                        "score": execution.run.score.payload(),
                    }
                )
            else:
                assert execution.attempt is not None
                status_key = {
                    "PROVIDER_TIMEOUT": "provider_timeouts",
                    "PROVIDER_ERROR": "provider_errors",
                    "VALIDATION_REJECTED": "validation_rejects",
                }[execution.attempt.status.value]
                summary[status_key] += 1
                result.append(
                    {
                        "case_id": case.case_id,
                        "status": execution.attempt.status.value,
                        "attempt_identity": execution.attempt.identity,
                    }
                )
        runs_for_contract = completed_runs(
            args.output_dir, task=task, prompt_version=task_contract(task).prompt_identity
        )
        report = write_aggregate_report(args.output_dir, runs_for_contract)
        renderer.emit(
            trace_event(
                AITraceEventType.BENCHMARK_COMPLETED,
                provider=provider.provider_identity,
                model=provider.model_identity,
                metadata={
                    "completed_runs": len(runs_for_contract),
                    "remaining_incomplete": summary["provider_timeouts"]
                    + summary["provider_errors"]
                    + summary["validation_rejects"],
                    **summary,
                },
            )
        )
        if args.json:
            print(
                json.dumps(
                    {"runs": result, "aggregate_report": str(report), "summary": summary},
                    sort_keys=True,
                )
            )
    finally:
        ai.close()
        if store is not None:
            store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
