"""Run the opt-in local closed-loop calibration benchmark against Ollama."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from app.ai.closed_loop_calibration import (
    CALIBRATION_BENCHMARK_IDENTITY,
    aggregate_calibration_scores,
    calibration_attempt,
    calibration_cases,
    calibration_digest,
    calibration_run,
    calibration_v11_digest,
    calibration_v11_run,
    resumable_run,
    resumable_v11_run,
    write_attempt,
    write_run,
    write_v11_run,
)
from app.ai.errors import AIError
from app.ai.providers import OllamaProvider
from app.ai.router import ModelRouter
from app.ai.runtime import AIConfig, ChappieAI
from app.ai.tasks import ModelProfile
from app.research_paths import results_dir


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("ollama",), required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--contract", choices=("1.0.0", "1.1.0"), default="1.0.0")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--case-id")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--output-dir", type=Path, default=results_dir() / "ai" / "closed_loop")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def _selected(args) -> tuple:
    cases = calibration_cases()
    if args.case_id is not None:
        cases = tuple(case for case in cases if case.case_id == args.case_id)
        if not cases:
            raise ValueError("unknown calibration case ID")
    if args.limit is not None:
        if args.limit <= 0:
            raise ValueError("limit must be positive")
        cases = cases[: args.limit]
    return cases


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        cases = _selected(args)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    provider = OllamaProvider(model=args.model, timeout_seconds=args.timeout_seconds)
    ai = ChappieAI(
        ModelRouter({ModelProfile.LOCAL_DEFAULT: provider}),
        AIConfig(model=args.model, timeout_seconds=args.timeout_seconds),
    )
    results: list[dict[str, object]] = []
    completed = []
    try:
        for case in cases:
            existing = (
                (resumable_v11_run if args.contract == "1.1.0" else resumable_run)(
                    args.output_dir, case, provider=provider.provider_identity, model=args.model
                )
                if args.resume
                else None
            )
            if existing is not None:
                results.append({"case_id": case.case_id, "resumed": True, "path": str(existing)})
                continue
            started = time.monotonic()
            try:
                response = (
                    ai.review_research_evidence_v11
                    if args.contract == "1.1.0"
                    else ai.review_research_evidence
                )(context=case.context)
                run = (calibration_v11_run if args.contract == "1.1.0" else calibration_run)(
                    case, response
                )
                path = (write_v11_run if args.contract == "1.1.0" else write_run)(
                    args.output_dir, run
                )
                completed.append(run)
                result = {"case_id": case.case_id, "path": str(path), "score": run.score}
                results.append(result)
                if args.verbose and not args.json:
                    relation = getattr(run.review, "evidence_relation", None)
                    label = relation.value if relation is not None else run.review.assessment.value
                    print(f"{case.case_id}: {label} Δ={run.score['CONFIDENCE_REVISION_DELTA']:.2f}")
            except AIError as error:
                attempt = calibration_attempt(
                    case,
                    provider=provider.provider_identity,
                    model=args.model,
                    error=error,
                    elapsed_ms=round((time.monotonic() - started) * 1000),
                    contract_version=args.contract,
                )
                attempt_path = write_attempt(args.output_dir, attempt)
                results.append(
                    {
                        "case_id": case.case_id,
                        "status": attempt.status,
                        "attempt_identity": attempt.identity,
                        "path": str(attempt_path),
                    }
                )
                if args.verbose and not args.json:
                    print(f"{case.case_id}: {type(error).__name__}", file=sys.stderr)
    finally:
        ai.close()
    payload = {
        "benchmark_identity": (
            "chappie_ai_closed_loop_calibration/1.1.0"
            if args.contract == "1.1.0"
            else CALIBRATION_BENCHMARK_IDENTITY
        ),
        "benchmark_digest": calibration_v11_digest()
        if args.contract == "1.1.0"
        else calibration_digest(),
        "results": results,
        "aggregate_by_family": aggregate_calibration_scores(tuple(completed)),
    }
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
