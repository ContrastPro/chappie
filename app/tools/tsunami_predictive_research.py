"""Research-only operator entry point for the frozen Tsunami predictive bundle."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.research.capital_flow.free_v2_analytical_view import ANALYTICAL_ROOT
from app.research.capital_flow.tsunami_predictive_research import (
    PRODUCTION_CONTRACT,
    RESEARCH_CONTRACT_DIGEST,
    RESULT_ROOT,
    bounded_synthetic_benchmark,
    verify_bound_manifests,
)
from app.research.capital_flow.tsunami_research_execution import (
    execute_production_v2,
    production_status_v2,
)
from app.research.capital_flow.tsunami_research_targets import RESEARCH_TARGET_ROOT


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the immutable Research-only Tsunami bundle.")
    parser.add_argument("--analytical-root", type=Path, default=ANALYTICAL_ROOT)
    parser.add_argument("--target-root", type=Path, default=RESEARCH_TARGET_ROOT)
    parser.add_argument("--output-root", type=Path, default=RESULT_ROOT)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--describe", action="store_true")
    parser.add_argument("--benchmark", action="store_true")
    command = parser.add_mutually_exclusive_group()
    command.add_argument("--execute", action="store_true")
    command.add_argument("--status", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.status:
        print(
            json.dumps(
                production_status_v2(args.output_root, args.analytical_root, args.target_root),
                sort_keys=True,
            )
        )
        return 0
    if args.execute:
        if args.workers < 1:
            raise SystemExit("--workers must be positive")
        verify_bound_manifests(args.analytical_root, args.target_root)
        result = execute_production_v2(
            args.output_root, args.analytical_root, args.target_root, max_workers=args.workers
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    if not args.describe and not args.benchmark:
        raise SystemExit("choose --execute, --status, --describe, or --benchmark")
    payload: dict[str, object] = {
        "contract": PRODUCTION_CONTRACT.payload(),
        "contract_digest": RESEARCH_CONTRACT_DIGEST,
        "output_root": str(args.output_root),
        "real_research_executed": False,
    }
    if args.benchmark:
        payload["benchmark"] = bounded_synthetic_benchmark()
    if not args.describe:
        verify_bound_manifests(args.analytical_root, args.target_root)
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
