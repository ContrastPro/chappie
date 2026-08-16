"""Run a bounded, offline-only walk-forward strategy preset study."""

from __future__ import annotations

import argparse
from decimal import Decimal
from pathlib import Path

from app.strategy_lab import StrategyComparisonConfig
from app.strategy_walk_forward import load_split_manifest, report_payload, run_walk_forward
from app.tools.strategy_evaluate import write_report_atomic


def _print_split(name: str, rows: object) -> None:
    print(name.upper())
    print(f"{'Candidate':<48} {'Act':<11} {'Median':>9} {'Worst':>9} {'Fees':>11} {'Trips':>6}")
    for row in rows:  # type: ignore[union-attr]
        print(
            f"{row.candidate.identifier:<48} {row.activity:<11} "
            f"{row.median_return_percent:>8.2f}% {row.worst_return_percent:>8.2f}% "
            f"{row.total_fees:>11.2f} {row.total_completed_round_trips:>6}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline bounded walk-forward strategy research")
    parser.add_argument(
        "manifest", type=Path, help="JSON with separate train, validation, test lists"
    )
    parser.add_argument("--starting-equity", type=Decimal, default=Decimal(10000))
    parser.add_argument("--fee-rate", type=Decimal, default=Decimal("0.001"))
    parser.add_argument("--output", type=Path, help="optional ignored local JSON report")
    args = parser.parse_args(argv)
    report = run_walk_forward(
        load_split_manifest(args.manifest),
        StrategyComparisonConfig(args.starting_equity, args.fee_rate),
    )
    _print_split("train", report.train)
    print(
        "TRAIN SELECTED:",
        ", ".join(candidate.identifier for candidate in report.train_selected) or "none",
    )
    _print_split("validation", report.validation)
    print(
        "FROZEN BEFORE TEST:",
        ", ".join(candidate.identifier for candidate in report.frozen) or "none",
    )
    _print_split("test", report.test)
    if args.output:
        write_report_atomic(args.output, report_payload(report))
        print(f"wrote report to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
