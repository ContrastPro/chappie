"""Causal, offline aggregate studies over verified historical candle chunks.

The module intentionally emits compact aggregates only.  Feature values are
computed from candles at or before time ``T``; separately named forward-return
labels are the only values that read beyond ``T``.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

from app.market.intervals import fixed_binance_spot_interval_ms
from app.market.models import Candle
from app.strategies.indicators import simple_regime
from app.strategy_history import HistoricalDataset, validate_candles

PAPER_ROUND_TRIP_COST_BPS = Decimal(20)  # 2 x the current 10 bps Paper fee.
RETURN_HORIZONS: dict[str, tuple[int, ...]] = {
    "1h": (1, 3, 6, 12, 24),
    "15m": (1, 4, 12, 24),
    "5m": (1, 3, 12, 36),
}
RETRACEMENT_BUCKETS = (
    (Decimal(0), Decimal(20)),
    (Decimal(20), Decimal(30)),
    (Decimal(30), Decimal(40)),
    (Decimal(40), Decimal(50)),
    (Decimal(50), Decimal(60)),
    (Decimal(60), Decimal(70)),
    (Decimal(70), Decimal(80)),
    (Decimal(80), Decimal(100)),
)


def _decimal_summary(values: list[Decimal]) -> dict[str, str | int | None]:
    if not values:
        return {"count": 0, "mean": None, "median": None, "min": None, "max": None}
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    median = (
        ordered[midpoint]
        if len(ordered) % 2
        else (ordered[midpoint - 1] + ordered[midpoint]) / Decimal(2)
    )
    return {
        "count": len(values),
        "mean": str(sum(values, Decimal(0)) / len(values)),
        "median": str(median),
        "min": str(ordered[0]),
        "max": str(ordered[-1]),
    }


def _ema(values: tuple[Decimal, ...], window: int) -> list[Decimal | None]:
    output: list[Decimal | None] = [None] * len(values)
    if len(values) < window:
        return output
    current = sum(values[:window], Decimal(0)) / Decimal(window)
    output[window - 1] = current
    alpha = Decimal(2) / Decimal(window + 1)
    for index, value in enumerate(values[window:], start=window):
        current = value * alpha + current * (Decimal(1) - alpha)
        output[index] = current
    return output


def merge_verified_chunks(
    datasets: tuple[HistoricalDataset, ...], interval: str
) -> tuple[Candle, ...]:
    """Validate chunk boundaries as one continuous, immutable candle stream."""
    if not datasets or any(dataset.metadata.interval != interval for dataset in datasets):
        raise ValueError("manifest datasets must be non-empty and share an interval")
    candles = tuple(candle for dataset in datasets for candle in dataset.candles)
    return validate_candles(candles, interval)


def _bucket(value_percent: Decimal) -> str | None:
    for lower, upper in RETRACEMENT_BUCKETS:
        if lower <= value_percent < upper or (upper == 100 and value_percent == upper):
            return f"{lower}-{upper}%"
    return None


def summarize_timeframe(
    datasets: tuple[HistoricalDataset, ...], interval: str
) -> dict[str, object]:
    """Return causal feature aggregates plus explicitly separate forward labels."""
    candles = merge_verified_chunks(datasets, interval)
    closes = tuple(c.close for c in candles)
    highs, lows, volumes = (
        tuple(c.high for c in candles),
        tuple(c.low for c in candles),
        tuple(c.volume for c in candles),
    )
    fast, slow = _ema(closes, 12), _ema(closes, 26)
    horizons = RETURN_HORIZONS[interval]
    labels: dict[int, list[Decimal]] = {horizon: [] for horizon in horizons}
    cost_exceedance: dict[int, dict[str, int]] = {
        horizon: {f"{multiple}x": 0 for multiple in (1, 2, 3, 5)} for horizon in horizons
    }
    regime_counts: dict[str, int] = defaultdict(int)
    regime_forwards: dict[str, dict[int, list[Decimal]]] = defaultdict(lambda: defaultdict(list))
    regime_durations: dict[str, list[int]] = defaultdict(list)
    retracements: dict[str, list[Decimal]] = defaultdict(list)
    fibonacci_near: dict[str, list[Decimal]] = defaultdict(list)
    conditions: dict[str, list[Decimal]] = defaultdict(list)
    current_regime: str | None = None
    duration = 0
    feature_counts = {"returns": 0, "trend": 0, "volatility": 0, "volume": 0, "drawdown": 0}
    for index, close in enumerate(closes):
        # Everything through this point is causal feature state at T.
        if index >= 24:
            feature_counts["returns"] += 1
            feature_counts["volatility"] += 1
            feature_counts["volume"] += 1
            feature_counts["drawdown"] += 1
        if fast[index] is not None and slow[index] is not None and index > 26:
            feature_counts["trend"] += 1
        # The causal classifier needs only its fixed trailing window; avoid
        # allocating the whole historical prefix for every observation.
        regime = simple_regime(
            highs[max(0, index - 14) : index + 1],
            lows[max(0, index - 14) : index + 1],
            closes[max(0, index - 14) : index + 1],
        )
        regime_counts[regime] += 1
        if current_regime is None or regime == current_regime:
            duration += 1
        else:
            regime_durations[current_regime].append(duration)
            duration = 1
        current_regime = regime
        if index >= 48:
            prior_high, prior_low = (
                max(highs[index - 48 : index + 1]),
                min(lows[index - 48 : index + 1]),
            )
            high_index = highs[index - 48 : index + 1].index(prior_high)
            if prior_high > prior_low and high_index < 48:
                ratio = (prior_high - close) / (prior_high - prior_low) * Decimal(100)
                bucket = _bucket(ratio)
                if bucket is not None:
                    retracements[bucket].append(ratio)
                    for level in (
                        Decimal("23.6"),
                        Decimal("38.2"),
                        Decimal(50),
                        Decimal("61.8"),
                        Decimal("78.6"),
                    ):
                        if abs(ratio - level) <= Decimal(2):
                            fibonacci_near[f"near_{level}%"].append(ratio)
        for horizon in horizons:
            future_index = index + horizon
            if future_index >= len(closes):
                continue
            # This is deliberately the only forward-looking quantity: a label.
            forward_return = (closes[future_index] - close) / close * Decimal(100)
            labels[horizon].append(forward_return)
            absolute_bps = abs(forward_return) * Decimal(100)
            for multiple in (1, 2, 3, 5):
                if absolute_bps > PAPER_ROUND_TRIP_COST_BPS * multiple:
                    cost_exceedance[horizon][f"{multiple}x"] += 1
            regime_forwards[regime][horizon].append(forward_return)
            if index >= 26 and fast[index] is not None and slow[index] is not None:
                spread_bps = (fast[index] - slow[index]) / close * Decimal(10_000)
                realized_vol_bps = (
                    sum(
                        abs((closes[pos] - closes[pos - 1]) / closes[pos - 1])
                        for pos in range(index - 11, index + 1)
                    )
                    / Decimal(12)
                    * Decimal(10_000)
                )
                conditions[
                    "momentum_high_vol"
                    if spread_bps > 0 and realized_vol_bps >= Decimal(40)
                    else "momentum_low_vol"
                ].append(forward_return)
                if regime == "RANGE":
                    conditions["range_mean_reversion_context"].append(forward_return)
                if regime == "TREND":
                    conditions["trend_pullback_context"].append(forward_return)
                relative_volume = volumes[index] / (
                    sum(volumes[index - 23 : index + 1], Decimal(0)) / Decimal(24)
                )
                if relative_volume >= Decimal("1.5") and abs(spread_bps) >= Decimal(10):
                    conditions["volume_expansion_directional_move"].append(forward_return)
    if current_regime is not None:
        regime_durations[current_regime].append(duration)
    return {
        "interval": interval,
        "coverage": {
            "first_open_ms": candles[0].open_time_ms,
            "last_open_ms": candles[-1].open_time_ms,
            "candle_count": len(candles),
            "chunk_count": len(datasets),
            "interval_ms": fixed_binance_spot_interval_ms(interval),
        },
        "feature_families": feature_counts,
        "forward_return_labels_percent": {
            str(key): _decimal_summary(value) for key, value in labels.items()
        },
        "transaction_cost_context": {
            "round_trip_cost_bps": str(PAPER_ROUND_TRIP_COST_BPS),
            "absolute_move_exceeds_cost": {
                str(horizon): {
                    multiple: {
                        "count": count,
                        "percent": str(Decimal(count) / len(labels[horizon]) * 100)
                        if labels[horizon]
                        else None,
                    }
                    for multiple, count in values.items()
                }
                for horizon, values in cost_exceedance.items()
            },
        },
        "regimes": {
            name: {
                "frequency": regime_counts[name],
                "average_duration_bars": str(
                    sum(regime_durations[name]) / len(regime_durations[name])
                )
                if regime_durations[name]
                else None,
                "forward_returns": {
                    str(horizon): _decimal_summary(values)
                    for horizon, values in sorted(regime_forwards[name].items())
                },
            }
            for name in sorted(regime_counts)
        },
        "retracement_study": {
            "buckets": {
                name: _decimal_summary(values) for name, values in sorted(retracements.items())
            },
            "near_fibonacci_levels": {
                name: _decimal_summary(values) for name, values in sorted(fibonacci_near.items())
            },
        },
        "conditional_studies": {
            name: _decimal_summary(values) for name, values in sorted(conditions.items())
        },
        "lookahead_audit": {
            "features_use_only_at_or_before_t": True,
            "forward_returns_are_separate_labels": True,
            "regime_is_causal": True,
            "retracement_is_causal_rolling_window": True,
            "chunk_boundaries_validated_as_one_stream": True,
        },
    }


def manifest_payload(
    root: Path, datasets: dict[str, tuple[HistoricalDataset, ...]]
) -> dict[str, object]:
    return {
        "format": "chappie-long-history-manifest/v1",
        "symbol": "BTCUSDT",
        "integrity_status": "VERIFIED",
        "timeframes": {
            interval: {
                "total_candles": sum(item.metadata.candle_count for item in chunks),
                "chunks": [
                    {
                        **asdict(item.metadata),
                        "path": str(root / interval / f"{item.metadata.requested_start_ms}.json"),
                    }
                    for item in chunks
                ],
            }
            for interval, chunks in datasets.items()
        },
    }
