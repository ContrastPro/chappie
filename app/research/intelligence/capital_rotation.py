"""Offline-only, causal capital-rotation research primitives.

``CAPITAL_FLOW_PROXY`` is deliberately descriptive: it summarizes observable
price, volume, breadth, and optional funding context.  It does not claim to
observe literal transfers of capital and it never creates a trading signal.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from statistics import median
from typing import Final

from app.market.models import Candle
from app.research.intelligence.market import FUNDING_MAX_AGE_MS, ContextDataset

ASSETS: Final = ("BTC", "ETH", "SOL", "BNB")
RELATIVE_PAIRS: Final = {"ETH": "ETHBTC", "SOL": "SOLBTC", "BNB": "BNBBTC"}
STATES: Final = (
    "BTC_LEADERSHIP",
    "ETH_LEADERSHIP",
    "ALT_LEADERSHIP",
    "BROAD_RISK_ON",
    "BROAD_RISK_OFF",
    "MIXED",
)


@dataclass(frozen=True, slots=True)
class RotationObservation:
    """Facts known at one completed, fully aligned candle close."""

    timestamp_ms: int
    close_prices: dict[str, Decimal]
    absolute_returns_bps: dict[str, Decimal]
    relative_returns_bps: dict[str, Decimal]
    relative_volume: dict[str, Decimal]
    breadth_positive: int
    breadth_outperforming_btc: int
    median_return_bps: Decimal
    dispersion_bps: Decimal
    leader: str
    laggard: str
    state: str


def _return_bps(now: Decimal, then: Decimal) -> Decimal:
    return (now / then - 1) * Decimal(10_000)


def _contiguous_aligned_segments(
    spot: dict[str, tuple[Candle, ...]], *, interval_ms: int
) -> tuple[tuple[tuple[int, dict[str, Candle]], ...], ...]:
    """Intersect assets then split at gaps; calculations never bridge a gap."""
    if set(spot) != set(ASSETS):
        raise ValueError("rotation study requires BTC, ETH, SOL, and BNB USDT candles")
    indexes = {
        asset: {item.close_time_ms: item for item in candles} for asset, candles in spot.items()
    }
    shared = sorted(set.intersection(*(set(index) for index in indexes.values())))
    segments: list[list[tuple[int, dict[str, Candle]]]] = []
    rows: list[tuple[int, dict[str, Candle]]] = []
    previous: int | None = None
    for timestamp in shared:
        if previous is not None and timestamp != previous + interval_ms:
            # A later row cannot use earlier rolling state across this gap.
            if rows:
                segments.append(rows)
            rows = []
        rows.append((timestamp, {asset: indexes[asset][timestamp] for asset in ASSETS}))
        previous = timestamp
    if rows:
        segments.append(rows)
    return tuple(tuple(segment) for segment in segments)


def rotation_observations(
    spot: dict[str, tuple[Candle, ...]], *, interval_ms: int, lookback_bars: int = 6
) -> tuple[RotationObservation, ...]:
    """Build causal strength/breadth observations from completed candles only."""
    if lookback_bars < 1:
        raise ValueError("lookback_bars must be positive")
    result: list[RotationObservation] = []
    for rows in _contiguous_aligned_segments(spot, interval_ms=interval_ms):
        for index in range(lookback_bars, len(rows)):
            timestamp, current = rows[index]
            _, previous = rows[index - lookback_bars]
            absolute = {
                asset: _return_bps(current[asset].close, previous[asset].close) for asset in ASSETS
            }
            relative = {asset: absolute[asset] - absolute["BTC"] for asset in ASSETS}
            volumes = {}
            for asset in ASSETS:
                average_volume = sum(
                    rows[offset][1][asset].volume for offset in range(index - lookback_bars, index)
                ) / Decimal(lookback_bars)
                # A zero-volume source interval is explicit neutral participation,
                # never an invented value or an exception that changes price facts.
                volumes[asset] = (
                    current[asset].volume / average_volume if average_volume else Decimal(0)
                )
            values = tuple(absolute.values())
            leader = max(ASSETS, key=lambda asset: (relative[asset], asset))
            laggard = min(ASSETS, key=lambda asset: (relative[asset], asset))
            positive = sum(value > 0 for value in values)
            outperforming = sum(relative[asset] > 0 for asset in ASSETS if asset != "BTC")
            spread = max(values) - min(values)
            if positive == len(ASSETS):
                state = "BROAD_RISK_ON"
            elif positive == 0:
                state = "BROAD_RISK_OFF"
            elif leader == "BTC" and all(
                relative[asset] <= 0 for asset in ASSETS if asset != "BTC"
            ):
                state = "BTC_LEADERSHIP"
            elif leader == "ETH" and relative["ETH"] > 0:
                state = "ETH_LEADERSHIP"
            elif leader in {"SOL", "BNB"} and relative[leader] > 0:
                state = "ALT_LEADERSHIP"
            else:
                state = "MIXED"
            result.append(
                RotationObservation(
                    timestamp,
                    {asset: current[asset].close for asset in ASSETS},
                    absolute,
                    relative,
                    volumes,
                    positive,
                    outperforming,
                    Decimal(str(median(values))),
                    spread,
                    leader,
                    laggard,
                    state,
                )
            )
    return tuple(result)


def transition_matrix(
    observations: tuple[RotationObservation, ...], *, bars: int
) -> dict[str, dict[str, object]]:
    if bars < 1:
        raise ValueError("transition bars must be positive")
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    for index, item in enumerate(observations[:-bars]):
        future = observations[index + bars]
        if not _contiguous_horizon(item, future, bars):
            continue
        counts[item.state][future.state] += 1
    return {
        state: {
            "count": sum(counts[state].values()),
            "probabilities": {
                target: str(Decimal(counts[state][target]) / Decimal(sum(counts[state].values())))
                if counts[state]
                else None
                for target in STATES
            },
        }
        for state in STATES
    }


def leadership_outcomes(
    observations: tuple[RotationObservation, ...], *, bars: int
) -> dict[str, object]:
    """Same-period conditional/baseline outcomes, split by calendar year."""
    rows = [
        (current, future)
        for current, future in zip(observations[:-bars], observations[bars:], strict=True)
        if _contiguous_horizon(current, future, bars)
    ]
    output: dict[str, object] = {}
    for asset in ASSETS:
        future_relative = [_future_relative_bps(asset, current, future) for current, future in rows]
        future_absolute = [_future_absolute_bps(asset, current, future) for current, future in rows]
        baseline = _probability_summary(future_relative)
        conditioned = [
            _future_relative_bps(asset, current, future)
            for current, future in rows
            if current.leader == asset
        ]
        output[asset] = {
            "leader_samples": len(conditioned),
            "leader_condition": _probability_summary(conditioned),
            "same_period_baseline": baseline,
            "future_absolute_bps_mean": (
                str(sum(future_absolute) / Decimal(len(future_absolute)))
                if future_absolute
                else None
            ),
            "annual": _annual_outcomes(asset, rows),
        }
    return output


def _future_absolute_bps(
    asset: str, current: RotationObservation, future: RotationObservation
) -> Decimal:
    return _return_bps(future.close_prices[asset], current.close_prices[asset])


def _future_relative_bps(
    asset: str, current: RotationObservation, future: RotationObservation
) -> Decimal:
    return _future_absolute_bps(asset, current, future) - _future_absolute_bps(
        "BTC", current, future
    )


def _contiguous_horizon(
    current: RotationObservation, future: RotationObservation, bars: int
) -> bool:
    """Reject labels that would bridge one of the source-derived gaps."""
    return (
        future.timestamp_ms - current.timestamp_ms == bars * 900_000
        or future.timestamp_ms - current.timestamp_ms == bars * 3_600_000
    )


def _probability_summary(values: list[Decimal]) -> dict[str, object]:
    thresholds = (20, 40, 60, 100, 150)
    return {
        "count": len(values),
        "mean_bps": str(sum(values) / Decimal(len(values))) if values else None,
        "threshold_probabilities": {
            str(threshold): str(
                Decimal(sum(value >= threshold for value in values)) / Decimal(len(values))
            )
            if values
            else None
            for threshold in thresholds
        },
    }


def _annual_outcomes(
    asset: str, rows: list[tuple[RotationObservation, RotationObservation]]
) -> dict[str, object]:
    by_year: dict[str, list[Decimal]] = defaultdict(list)
    for current, future in rows:
        if current.leader == asset:
            by_year[str(datetime.fromtimestamp(current.timestamp_ms / 1000, tz=UTC).year)].append(
                _future_relative_bps(asset, current, future)
            )
    return {year: _probability_summary(values) for year, values in sorted(by_year.items())}


def direct_pair_cross_check(
    spot: dict[str, tuple[Candle, ...]],
    direct: dict[str, tuple[Candle, ...]],
    *,
    interval_ms: int,
    bars: int = 6,
) -> dict[str, dict[str, object]]:
    """Compare direct BTC-pair return against the derived USDT relative return."""
    aligned = _contiguous_aligned_segments(spot, interval_ms=interval_ms)
    output: dict[str, dict[str, object]] = {}
    for asset, pair in RELATIVE_PAIRS.items():
        candles = {item.close_time_ms: item for item in direct.get(pair, ())}
        errors: list[Decimal] = []
        for segment in aligned:
            for index in range(bars, len(segment)):
                now_time, now = segment[index]
                then_time, then = segment[index - bars]
                if now_time not in candles or then_time not in candles:
                    continue
                # Relative-strength ranking is reported as the simple return
                # difference.  A direct pair is cross-checked against the exact
                # ratio return, avoiding a compounding approximation in integrity
                # diagnostics.
                derived = _return_bps(
                    now[asset].close / now["BTC"].close,
                    then[asset].close / then["BTC"].close,
                )
                errors.append(
                    _return_bps(candles[now_time].close, candles[then_time].close) - derived
                )
        output[pair] = {
            "samples": len(errors),
            "mean_difference_bps": str(sum(errors) / Decimal(len(errors))) if errors else None,
            "max_absolute_difference_bps": str(max(map(abs, errors))) if errors else None,
        }
    return output


def capital_flow_proxy(
    observation: RotationObservation, *, funding_state: str | None = None
) -> dict[str, object]:
    """Transparent descriptive proxy; intentionally not a signal or money-flow claim."""
    return {
        "name": "CAPITAL_FLOW_PROXY",
        "meaning": "probable observable market leadership/rotation state, not literal capital flow",
        "state": observation.state,
        "leader": observation.leader,
        "laggard": observation.laggard,
        "breadth_positive": observation.breadth_positive,
        "breadth_outperforming_btc": observation.breadth_outperforming_btc,
        "dispersion_bps": str(observation.dispersion_bps),
        "relative_volume": {key: str(value) for key, value in observation.relative_volume.items()},
        "btc_funding_state": funding_state,
    }


def funding_state(value: Decimal) -> str:
    """Static descriptive funding buckets, not optimized research thresholds."""
    if value >= Decimal("0.0005"):
        return "EXTREME_POSITIVE"
    if value > 0:
        return "POSITIVE"
    if value < 0:
        return "NEGATIVE"
    return "NEUTRAL"


def funding_conditioned_outcomes(
    observations: tuple[RotationObservation, ...],
    funding: ContextDataset,
    *,
    bars: int,
) -> dict[str, object]:
    """Causal, bounded BTC funding conditioning; stale observations are absent."""
    published = tuple(item.published_at_ms for item in funding.observations)
    buckets: dict[str, dict[str, list[Decimal]]] = defaultdict(lambda: defaultdict(list))
    for current, future in zip(observations[:-bars], observations[bars:], strict=True):
        if not _contiguous_horizon(current, future, bars):
            continue
        index = bisect_right(published, current.timestamp_ms) - 1
        if index < 0 or current.timestamp_ms - published[index] > FUNDING_MAX_AGE_MS:
            continue
        state = funding_state(funding.observations[index].value)
        for asset in ASSETS[1:]:
            buckets[state][asset].append(_future_relative_bps(asset, current, future))
    return {
        state: {asset: _probability_summary(values) for asset, values in sorted(by_asset.items())}
        for state, by_asset in sorted(buckets.items())
    }


def volume_breadth_outcomes(
    observations: tuple[RotationObservation, ...], *, bars: int
) -> dict[str, object]:
    """Bounded participation comparison for each current relative leader."""
    result: dict[str, dict[str, list[Decimal]]] = defaultdict(lambda: defaultdict(list))
    for current, future in zip(observations[:-bars], observations[bars:], strict=True):
        if not _contiguous_horizon(current, future, bars) or current.leader == "BTC":
            continue
        condition = (
            "RELATIVE_VOLUME_EXPANSION_AND_BREADTH"
            if current.relative_volume[current.leader] >= 1
            and current.breadth_outperforming_btc >= 2
            else "LEADERSHIP_ALONE"
        )
        result[current.leader][condition].append(
            _future_relative_bps(current.leader, current, future)
        )
    return {
        leader: {
            condition: _probability_summary(values) for condition, values in sorted(groups.items())
        }
        for leader, groups in sorted(result.items())
    }
