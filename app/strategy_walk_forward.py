"""Small, deterministic walk-forward research over immutable candle datasets.

This module is deliberately offline-only.  It constructs only pure strategies
and uses the Paper-style replay economics in :mod:`app.strategy_lab`; it never
constructs an exchange client or changes runtime strategy configuration.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from statistics import median

from app.strategies.factory import StrategySettings
from app.strategy_history import HistoricalDataset, load_dataset
from app.strategy_lab import StrategyComparisonConfig, StrategyComparisonResult, compare_strategy

MAX_CANDIDATES_PER_STRATEGY = 24
BASELINE_NAMES = ("ema_crossover", "trend_filtered", "breakout", "mean_reversion")


@dataclass(frozen=True, slots=True)
class Candidate:
    """An explicit, immutable research preset; never an active runtime setting."""

    identifier: str
    settings: StrategySettings
    baseline: bool = False


@dataclass(frozen=True, slots=True)
class CandidateMetrics:
    candidate: Candidate
    windows: int
    profitable_windows: int
    median_return_percent: Decimal
    worst_return_percent: Decimal
    median_max_drawdown_percent: Decimal
    median_profit_factor: Decimal | None
    total_fees: Decimal
    median_fee_drag_percent: Decimal
    median_turnover_equity_percent: Decimal
    total_completed_round_trips: int
    median_average_holding_ms: int | None
    gross_to_fee_ratio: Decimal | None
    activity: str
    regime_observations: dict[str, int]


@dataclass(frozen=True, slots=True)
class WalkForwardReport:
    train: tuple[CandidateMetrics, ...]
    validation: tuple[CandidateMetrics, ...]
    test: tuple[CandidateMetrics, ...]
    train_selected: tuple[Candidate, ...]
    frozen: tuple[Candidate, ...]


def _decimal_median(values: list[Decimal]) -> Decimal:
    return Decimal(str(median(values)))


def baseline_candidates() -> tuple[Candidate, ...]:
    return tuple(
        Candidate(f"baseline/{name}/1.0.0", StrategySettings(name=name), baseline=True)
        for name in BASELINE_NAMES
    )


def research_candidates(strategy: str) -> tuple[Candidate, ...]:
    """Return a deliberately small, economically sensible preset grid.

    EMA remains a control.  Each research strategy has at most 12 candidates,
    substantially below the hard cap and intentionally unsuitable for a
    brute-force search.
    """
    if strategy == "ema_crossover":
        return ()
    if strategy == "trend_filtered":
        values = (
            (8, 21, 5, 3),
            (8, 21, 8, 5),
            (10, 30, 5, 3),
            (10, 30, 10, 5),
            (12, 26, 3, 3),
            (12, 26, 5, 5),
            (14, 35, 5, 3),
            (14, 35, 8, 5),
            (16, 40, 5, 3),
            (16, 40, 10, 5),
        )
        candidates = tuple(
            Candidate(
                f"research/trend_filtered/{fast}-{slow}-spread{spread}-atr{atr}",
                StrategySettings(
                    name="trend_filtered",
                    ema_fast_window=fast,
                    ema_slow_window=slow,
                    trend_minimum_spread_bps=Decimal(spread),
                    trend_minimum_atr_bps=Decimal(atr),
                ),
            )
            for fast, slow, spread, atr in values
        )
    elif strategy == "breakout":
        values = (
            (12, 2, "0.40"),
            (12, 5, "0.50"),
            (16, 2, "0.40"),
            (16, 5, "0.50"),
            (20, 2, "0.50"),
            (20, 5, "0.60"),
            (24, 2, "0.50"),
            (24, 5, "0.60"),
            (30, 2, "0.50"),
            (30, 5, "0.60"),
        )
        candidates = tuple(
            Candidate(
                f"research/breakout/{lookback}-atr{atr}-exit{exit_fraction}",
                StrategySettings(
                    name="breakout",
                    breakout_lookback=lookback,
                    breakout_minimum_atr_bps=Decimal(atr),
                    breakout_exit_fraction=Decimal(exit_fraction),
                ),
            )
            for lookback, atr, exit_fraction in values
        )
    elif strategy == "mean_reversion":
        values = (
            (12, 45, 0),
            (12, 60, 0),
            (16, 45, 0),
            (16, 60, 5),
            (20, 45, 0),
            (20, 60, 5),
            (24, 45, 0),
            (24, 60, 5),
            (30, 45, 0),
            (30, 60, 5),
        )
        candidates = tuple(
            Candidate(
                f"research/mean_reversion/{lookback}-entry{entry}-exit{exit}",
                StrategySettings(
                    name="mean_reversion",
                    mean_reversion_lookback=lookback,
                    mean_reversion_entry_deviation_bps=Decimal(entry),
                    mean_reversion_exit_deviation_bps=Decimal(exit),
                ),
            )
            for lookback, entry, exit in values
        )
    else:
        raise ValueError(f"unsupported strategy for research: {strategy}")
    if len(candidates) > MAX_CANDIDATES_PER_STRATEGY:
        raise ValueError("research candidate limit exceeded")
    return candidates


def default_candidates() -> tuple[Candidate, ...]:
    return baseline_candidates() + tuple(
        candidate
        for name in ("trend_filtered", "breakout", "mean_reversion")
        for candidate in research_candidates(name)
    )


def _validate_candidate_limits(candidates: tuple[Candidate, ...]) -> None:
    counts: dict[str, int] = {}
    for candidate in candidates:
        if candidate.baseline:
            continue
        name = candidate.settings.name
        counts[name] = counts.get(name, 0) + 1
    if any(count > MAX_CANDIDATES_PER_STRATEGY for count in counts.values()):
        raise ValueError("research candidate limit exceeded")


def _classify(results: tuple[StrategyComparisonResult, ...]) -> str:
    trips = sum(result.completed_round_trips for result in results)
    median_turnover = _decimal_median([result.turnover_equity_percent for result in results])
    median_fee_drag = _decimal_median([result.fee_drag_percent for result in results])
    if trips < len(results):
        return "INACTIVE"
    if median_turnover >= Decimal(1000) or median_fee_drag >= Decimal(3):
        return "OVERACTIVE"
    return "REASONABLE"


def metrics_for(
    candidate: Candidate,
    datasets: tuple[HistoricalDataset, ...],
    config: StrategyComparisonConfig,
) -> CandidateMetrics:
    if not datasets:
        raise ValueError("each split needs at least one dataset")
    results = tuple(
        compare_strategy(dataset.candles, candidate.settings, config) for dataset in datasets
    )
    factors = [result.profit_factor for result in results if result.profit_factor is not None]
    holds = [
        result.average_holding_ms for result in results if result.average_holding_ms is not None
    ]
    fees = sum((result.fees for result in results), Decimal(0))
    gross = sum((result.gross_realised_pnl for result in results), Decimal(0))
    regimes: dict[str, int] = {}
    for result in results:
        for regime, count in result.regime_counts.items():
            regimes[regime] = regimes.get(regime, 0) + count
    return CandidateMetrics(
        candidate=candidate,
        windows=len(results),
        profitable_windows=sum(result.return_percent > 0 for result in results),
        median_return_percent=_decimal_median([result.return_percent for result in results]),
        worst_return_percent=min(result.return_percent for result in results),
        median_max_drawdown_percent=_decimal_median(
            [result.max_drawdown_percent for result in results]
        ),
        median_profit_factor=_decimal_median(factors) if factors else None,
        total_fees=fees,
        median_fee_drag_percent=_decimal_median([result.fee_drag_percent for result in results]),
        median_turnover_equity_percent=_decimal_median(
            [result.turnover_equity_percent for result in results]
        ),
        total_completed_round_trips=sum(result.completed_round_trips for result in results),
        median_average_holding_ms=int(median(holds)) if holds else None,
        gross_to_fee_ratio=gross / fees if fees else None,
        activity=_classify(results),
        regime_observations=regimes,
    )


def _rank(metrics: tuple[CandidateMetrics, ...]) -> tuple[CandidateMetrics, ...]:
    return tuple(
        sorted(
            metrics,
            key=lambda item: (
                item.activity != "REASONABLE",
                -item.profitable_windows,
                -item.median_return_percent,
                item.median_max_drawdown_percent,
                item.candidate.identifier,
            ),
        )
    )


def _select(metrics: tuple[CandidateMetrics, ...], per_strategy: int = 2) -> tuple[Candidate, ...]:
    selected: list[Candidate] = []
    for strategy in ("trend_filtered", "breakout", "mean_reversion"):
        eligible = [
            item
            for item in _rank(metrics)
            if item.candidate.settings.name == strategy
            and not item.candidate.baseline
            and item.activity == "REASONABLE"
        ]
        selected.extend(item.candidate for item in eligible[:per_strategy])
    return tuple(selected)


def load_split_manifest(path: Path) -> dict[str, tuple[HistoricalDataset, ...]]:
    import json

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("walk-forward manifest must be an object")
    output: dict[str, tuple[HistoricalDataset, ...]] = {}
    used_paths: set[Path] = set()
    for split in ("train", "validation", "test"):
        values = payload.get(split)
        if (
            not isinstance(values, list)
            or not values
            or not all(isinstance(item, str) for item in values)
        ):
            raise ValueError(f"walk-forward manifest requires non-empty {split} dataset paths")
        paths = tuple((path.parent / item).resolve() for item in values)
        if any(candidate in used_paths for candidate in paths):
            raise ValueError("a dataset may belong to exactly one walk-forward split")
        used_paths.update(paths)
        output[split] = tuple(load_dataset(candidate) for candidate in paths)
    return output


def run_walk_forward(
    splits: dict[str, tuple[HistoricalDataset, ...]],
    config: StrategyComparisonConfig | None = None,
    candidates: tuple[Candidate, ...] | None = None,
) -> WalkForwardReport:
    """Select on TRAIN, select once on VALIDATION, then freeze before TEST."""
    if set(splits) != {"train", "validation", "test"}:
        raise ValueError("walk-forward requires exactly train, validation, and test splits")
    config = config or StrategyComparisonConfig()
    candidates = candidates or default_candidates()
    _validate_candidate_limits(candidates)
    train = _rank(
        tuple(metrics_for(candidate, splits["train"], config) for candidate in candidates)
    )
    selected = _select(train)
    validation_candidates = baseline_candidates() + selected
    validation = _rank(
        tuple(
            metrics_for(candidate, splits["validation"], config)
            for candidate in validation_candidates
        )
    )
    frozen = _select(validation, per_strategy=1)
    test_candidates = baseline_candidates() + frozen
    test = _rank(
        tuple(metrics_for(candidate, splits["test"], config) for candidate in test_candidates)
    )
    return WalkForwardReport(train, validation, test, selected, frozen)


def report_payload(report: WalkForwardReport) -> dict[str, object]:
    def encoded(item: CandidateMetrics) -> dict[str, object]:
        raw = asdict(item)
        raw["candidate"]["settings"] = {
            name: str(value) if isinstance(value, Decimal) else value
            for name, value in raw["candidate"]["settings"].items()
        }
        return _encode(raw)

    return {
        "format": "chappie-strategy-walk-forward/v1",
        "train": [encoded(item) for item in report.train],
        "validation": [encoded(item) for item in report.validation],
        "test": [encoded(item) for item in report.test],
        "train_selected": [item.identifier for item in report.train_selected],
        "frozen_before_test": [item.identifier for item in report.frozen],
    }


def _encode(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _encode(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_encode(item) for item in value]
    return value
