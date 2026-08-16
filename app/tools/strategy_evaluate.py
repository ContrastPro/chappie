"""Compare the active pure strategies over one or more immutable local datasets."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from statistics import median

from app.strategy_history import HistoricalDataset, load_dataset
from app.strategy_lab import StrategyComparisonConfig, StrategyComparisonResult, compare_all


@dataclass(frozen=True, slots=True)
class DatasetEvaluation:
    dataset: HistoricalDataset
    results: tuple[StrategyComparisonResult, ...]


def _decimal_median(values: list[Decimal]) -> Decimal:
    return Decimal(str(median(values)))


def aggregate(
    evaluations: tuple[DatasetEvaluation, ...],
) -> dict[str, dict[str, Decimal | int | None]]:
    by_strategy: dict[str, list[StrategyComparisonResult]] = {}
    for evaluation in evaluations:
        for result in evaluation.results:
            by_strategy.setdefault(result.strategy_name, []).append(result)
    summary: dict[str, dict[str, Decimal | int | None]] = {}
    for strategy, results in by_strategy.items():
        returns = [result.return_percent for result in results]
        drawdowns = [result.max_drawdown_percent for result in results]
        factors = [result.profit_factor for result in results if result.profit_factor is not None]
        summary[strategy] = {
            "profitable_windows": sum(value > 0 for value in returns),
            "total_windows": len(results),
            "median_return_percent": _decimal_median(returns),
            "mean_return_percent": sum(returns, Decimal(0)) / len(returns),
            "worst_window_return_percent": min(returns),
            "best_window_return_percent": max(returns),
            "median_max_drawdown_percent": _decimal_median(drawdowns),
            "total_fees": sum((result.fees for result in results), Decimal(0)),
            "median_profit_factor": _decimal_median(factors) if factors else None,
            "total_completed_round_trips": sum(result.completed_round_trips for result in results),
        }
    return summary


def evaluate(
    paths: tuple[Path, ...], config: StrategyComparisonConfig
) -> tuple[DatasetEvaluation, ...]:
    if not paths:
        raise ValueError("at least one dataset is required")
    return tuple(
        DatasetEvaluation(dataset, compare_all(dataset.candles, config))
        for dataset in (load_dataset(path) for path in paths)
    )


def _json_value(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


def report_payload(evaluations: tuple[DatasetEvaluation, ...]) -> dict[str, object]:
    return {
        "format": "chappie-strategy-evaluation/v1",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "windows": [
            {
                "metadata": _json_value(asdict(evaluation.dataset.metadata)),
                "strategies": [_json_value(asdict(result)) for result in evaluation.results],
            }
            for evaluation in evaluations
        ],
        "aggregate": _json_value(aggregate(evaluations)),
    }


def write_report_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _print(evaluations: tuple[DatasetEvaluation, ...]) -> None:
    for evaluation in evaluations:
        meta = evaluation.dataset.metadata
        print(
            f"DATASET: {meta.symbol} {meta.interval} candles={meta.candle_count} sha256={meta.candles_sha256[:12]}"
        )
        print(f"{'Strategy':<18} {'Return':>9} {'DD':>9} {'Fees':>12} {'PF':>8} {'Trips':>6}")
        for result in evaluation.results:
            factor = "—" if result.profit_factor is None else f"{result.profit_factor:.2f}"
            print(
                f"{result.strategy_name:<18} {result.return_percent:>8.2f}% {result.max_drawdown_percent:>8.2f}% {result.fees:>12.2f} {factor:>8} {result.completed_round_trips:>6}"
            )
    print("AGGREGATE (independent, non-compounded windows)")
    for strategy, values in aggregate(evaluations).items():
        print(
            f"{strategy:<18} profitable={values['profitable_windows']}/{values['total_windows']} median_return={values['median_return_percent']:.2f}% median_dd={values['median_max_drawdown_percent']:.2f}%"
        )


def _manifest_paths(path: Path) -> tuple[Path, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    datasets = payload.get("datasets") if isinstance(payload, dict) else None
    if (
        not isinstance(datasets, list)
        or not datasets
        or not all(isinstance(item, str) for item in datasets)
    ):
        raise ValueError("research manifest requires a non-empty datasets string list")
    return tuple((path.parent / item).resolve() for item in datasets)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate active pure strategies over offline historical datasets"
    )
    parser.add_argument("datasets", nargs="*", type=Path)
    parser.add_argument(
        "--manifest", type=Path, help='JSON manifest: {"datasets": ["window.json"]}'
    )
    parser.add_argument("--starting-equity", type=Decimal, default=Decimal(10000))
    parser.add_argument("--fee-rate", type=Decimal, default=Decimal("0.001"))
    parser.add_argument("--output", type=Path, help="optional local JSON research report")
    args = parser.parse_args(argv)
    if bool(args.datasets) == bool(args.manifest):
        parser.error("provide datasets or exactly one --manifest")
    paths = _manifest_paths(args.manifest) if args.manifest else tuple(args.datasets)
    evaluations = evaluate(paths, StrategyComparisonConfig(args.starting_equity, args.fee_rate))
    _print(evaluations)
    if args.output:
        write_report_atomic(args.output, report_payload(evaluations))
        print(f"wrote report to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
