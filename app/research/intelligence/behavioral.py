"""Offline behavioural-market event research built from completed V3B observations.

This module is deliberately descriptive: its labels name observable market-state
conjunctions, not trader emotions or trading instructions.  It is not imported by
the server, strategy factory, risk layer, or execution path.
"""

from __future__ import annotations

import hashlib
import json
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from statistics import median

from app.research.intelligence.flow import FIVE_MINUTES_MS, FlowObservation

HORIZONS = {"1h": 12, "3h": 36, "6h": 72, "12h": 144, "24h": 288}
THRESHOLDS_BPS = (60, 100, 150)
EVENT_TYPES = (
    "CROWDED_STRESS",
    "CROWDED_EXPANSION",
    "OI_ACTIVE_VOLATILITY_CLUSTER",
    "DELEVERAGING",
    "PANIC_SELLING",
    "FOMO_BUYING",
    "FLOW_EXHAUSTION",
)


@dataclass(frozen=True, slots=True)
class Snapshot:
    timestamp_ms: int
    asset: str
    close: Decimal
    return_1h_bps: Decimal
    volatility_bps: Decimal
    trend: str
    volatility_bucket: str
    hour_utc: int
    price_oi_state: str | None
    funding: Decimal | None
    positioning: str | None
    imbalance: Decimal | None
    relative_volume: Decimal | None


@dataclass(frozen=True, slots=True)
class BehavioralEvent:
    event_id: str
    event_type: str
    asset: str
    start_ms: int
    end_ms: int
    observations: int
    peak_intensity: Decimal
    snapshot: Snapshot


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()


def _event_id(event_type: str, asset: str, start_ms: int, end_ms: int) -> str:
    return hashlib.sha256(_canonical([event_type, asset, start_ms, end_ms])).hexdigest()


def _breaks(rows: Sequence[FlowObservation], index: int, gaps: set[int]) -> bool:
    return (
        index == 0
        or rows[index].timestamp_ms in gaps
        or (rows[index].timestamp_ms - rows[index - 1].timestamp_ms != FIVE_MINUTES_MS)
    )


def _snapshots(
    rows: Sequence[FlowObservation], gaps_after_ms: Iterable[int]
) -> tuple[Snapshot | None, ...]:
    gaps = set(gaps_after_ms)
    returns: list[Decimal] = []
    result: list[Snapshot | None] = []
    for index, row in enumerate(rows):
        if _breaks(rows, index, gaps):
            returns = []
        if index and not _breaks(rows, index, gaps):
            returns.append((row.close / rows[index - 1].close - 1) * Decimal(10_000))
        if len(returns) < 12:
            result.append(None)
            continue
        recent = returns[-12:]
        one_hour = sum(recent, Decimal())
        volatility = (sum((value * value for value in recent), Decimal()) / Decimal(12)).sqrt()
        trend = "UP" if one_hour >= 20 else "DOWN" if one_hour <= -20 else "FLAT"
        bucket = "LOW" if volatility < 20 else "MEDIUM" if volatility < 50 else "HIGH"
        result.append(
            Snapshot(
                row.timestamp_ms,
                row.symbol,
                row.close,
                one_hour,
                volatility,
                trend,
                bucket,
                datetime.fromtimestamp(row.timestamp_ms / 1000, UTC).hour,
                row.price_oi_state,
                row.funding,
                row.positioning_state,
                row.spot.signed_taker_quote_imbalance,
                row.spot.relative_quote_volume,
            )
        )
    return tuple(result)


def _conditions(row: FlowObservation, snapshot: Snapshot) -> dict[str, Decimal]:
    imbalance = row.spot.signed_taker_quote_imbalance or Decimal()
    relative_volume = row.spot.relative_quote_volume or Decimal()
    oi_up = row.price_oi_state in {"PRICE_UP_OI_UP", "PRICE_DOWN_OI_UP"}
    oi_down = row.price_oi_state in {"PRICE_UP_OI_DOWN", "PRICE_DOWN_OI_DOWN"}
    intensity = (
        abs(snapshot.return_1h_bps) / 100
        + snapshot.volatility_bps / 100
        + max(relative_volume - 1, Decimal())
    )
    active = row.price_oi_state not in {None, "FLAT_OR_MIXED"}
    return {
        "CROWDED_STRESS": intensity
        if row.price_oi_state == "PRICE_DOWN_OI_UP"
        and row.funding is not None
        and row.funding < 0
        and row.positioning_state == "EXTREME_LONG"
        else Decimal(),
        "CROWDED_EXPANSION": intensity
        if row.price_oi_state == "PRICE_UP_OI_UP"
        and row.funding is not None
        and row.funding > 0
        and row.positioning_state == "EXTREME_LONG"
        else Decimal(),
        "OI_ACTIVE_VOLATILITY_CLUSTER": intensity if active else Decimal(),
        "DELEVERAGING": intensity
        if oi_down and snapshot.return_1h_bps <= -20 and imbalance < 0
        else Decimal(),
        "PANIC_SELLING": intensity
        if snapshot.return_1h_bps <= -40
        and imbalance < Decimal("-0.05")
        and snapshot.volatility_bucket == "HIGH"
        else Decimal(),
        "FOMO_BUYING": intensity
        if oi_up and snapshot.return_1h_bps >= 40 and imbalance > Decimal("0.05")
        else Decimal(),
        "FLOW_EXHAUSTION": intensity
        if abs(snapshot.return_1h_bps) >= 50
        and (
            (snapshot.return_1h_bps > 0 and imbalance < 0)
            or (snapshot.return_1h_bps < 0 and imbalance > 0)
        )
        else Decimal(),
    }


def collapse_events(
    rows: Sequence[FlowObservation], *, gaps_after_ms: Iterable[int] = ()
) -> tuple[BehavioralEvent, ...]:
    """Collapse adjacent state rows without joining any source discontinuity."""
    snapshots = _snapshots(rows, gaps_after_ms)
    gaps = set(gaps_after_ms)
    open_events: dict[str, tuple[int, Decimal, Snapshot]] = {}
    result: list[BehavioralEvent] = []

    def close(kind: str, end_index: int) -> None:
        start, peak, snapshot = open_events.pop(kind)
        end = rows[end_index]
        result.append(
            BehavioralEvent(
                _event_id(kind, end.symbol, rows[start].timestamp_ms, end.timestamp_ms),
                kind,
                end.symbol,
                rows[start].timestamp_ms,
                end.timestamp_ms,
                end_index - start + 1,
                peak,
                snapshot,
            )
        )

    for index, row in enumerate(rows):
        if _breaks(rows, index, gaps):
            for kind in tuple(open_events):
                close(kind, index - 1)
        snapshot = snapshots[index]
        active = _conditions(row, snapshot) if snapshot else {}
        for kind in EVENT_TYPES:
            intensity = active.get(kind, Decimal())
            if intensity > 0:
                if kind not in open_events:
                    open_events[kind] = (index, intensity, snapshot)  # type: ignore[arg-type]
                else:
                    start, peak, initial = open_events[kind]
                    open_events[kind] = (start, max(peak, intensity), initial)
            elif kind in open_events:
                close(kind, index - 1)
    for kind in tuple(open_events):
        close(kind, len(rows) - 1)
    return tuple(sorted(result, key=lambda event: (event.asset, event.event_type, event.start_ms)))


def _crosses_gap(start_ms: int, end_ms: int, gaps: Iterable[int]) -> bool:
    return any(start_ms < gap <= end_ms for gap in gaps)


def independent_events(
    events: Sequence[BehavioralEvent], *, horizon_bars: int, gaps_after_ms: Iterable[int]
) -> tuple[BehavioralEvent, ...]:
    """Select non-overlapping eligible event starts for one outcome horizon."""
    cooldown = horizon_bars * FIVE_MINUTES_MS
    selected: list[BehavioralEvent] = []
    for event in sorted(events, key=lambda item: item.start_ms):
        if _crosses_gap(event.start_ms, event.start_ms + cooldown, gaps_after_ms):
            continue
        if not selected or event.start_ms >= selected[-1].start_ms + cooldown:
            selected.append(event)
    return tuple(selected)


def _outcome(
    rows_by_time: dict[int, FlowObservation], event: BehavioralEvent, bars: int, gaps: Iterable[int]
) -> dict[str, Decimal] | None:
    end_ms = event.start_ms + bars * FIVE_MINUTES_MS
    if _crosses_gap(event.start_ms, end_ms, gaps) or end_ms not in rows_by_time:
        return None
    start = rows_by_time[event.start_ms].close
    timestamps = tuple(event.start_ms + step * FIVE_MINUTES_MS for step in range(1, bars + 1))
    if any(timestamp not in rows_by_time for timestamp in timestamps):
        return None
    path = [rows_by_time[timestamp].close for timestamp in timestamps]
    returns = [(value / start - 1) * Decimal(10_000) for value in path]
    return {"return_bps": returns[-1], "max_up_bps": max(returns), "max_down_bps": min(returns)}


def _matching_control(
    event: BehavioralEvent,
    candidates: dict[tuple[int, int, str], Sequence[Snapshot]],
    *,
    used: set[int],
    excluded: set[int],
) -> Snapshot | None:
    source = event.snapshot
    year = datetime.fromtimestamp(event.start_ms / 1000, UTC).year
    pool = candidates.get((year, source.hour_utc, source.trend), ())
    positions = [item.volatility_bps for item in pool]
    center = bisect_left(positions, source.volatility_bps)
    eligible = [
        candidate
        for candidate in pool[max(0, center - 24) : center + 24]
        if candidate.timestamp_ms not in used and candidate.timestamp_ms not in excluded
    ]
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda item: (
            abs(item.volatility_bps - source.volatility_bps),
            abs(item.return_1h_bps - source.return_1h_bps),
            abs((item.relative_volume or Decimal()) - (source.relative_volume or Decimal())),
            item.timestamp_ms,
        ),
    )


def _summary(values: Sequence[Decimal]) -> dict[str, object]:
    if not values:
        return {"count": 0, "median_bps": None, "mean_bps": None}
    return {
        "count": len(values),
        "median_bps": str(median(values)),
        "mean_bps": str(sum(values, Decimal()) / len(values)),
    }


def analyze_asset(
    rows: Sequence[FlowObservation], *, gaps_after_ms: Iterable[int] = ()
) -> dict[str, object]:
    """Event de-clustered, causally matched research results for one asset."""
    gaps = tuple(gaps_after_ms)
    events = collapse_events(rows, gaps_after_ms=gaps)
    snapshots = tuple(item for item in _snapshots(rows, gaps) if item is not None)
    controls_by_state: dict[tuple[int, int, str], list[Snapshot]] = defaultdict(list)
    for snapshot in snapshots:
        controls_by_state[
            (
                datetime.fromtimestamp(snapshot.timestamp_ms / 1000, UTC).year,
                snapshot.hour_utc,
                snapshot.trend,
            )
        ].append(snapshot)
    ordered_controls = {
        key: tuple(sorted(value, key=lambda item: (item.volatility_bps, item.timestamp_ms)))
        for key, value in controls_by_state.items()
    }
    by_time = {row.timestamp_ms: row for row in rows}
    output: dict[str, object] = {"raw_events": len(events), "event_types": {}}
    for kind in EVENT_TYPES:
        typed = tuple(event for event in events if event.event_type == kind)
        condition_times = {
            row.timestamp_ms
            for row, snapshot in zip(rows, _snapshots(rows, gaps), strict=True)
            if snapshot is not None and _conditions(row, snapshot).get(kind, Decimal()) > 0
        }
        type_report: dict[str, object] = {
            "raw_condition_observations": sum(event.observations for event in typed),
            "collapsed_events": len(typed),
            "median_duration_bars": str(median([event.observations for event in typed]))
            if typed
            else None,
            "horizons": {},
        }
        for horizon, bars in HORIZONS.items():
            selected = independent_events(typed, horizon_bars=bars, gaps_after_ms=gaps)
            used: set[int] = set()
            event_outcomes: list[dict[str, Decimal]] = []
            controls: list[dict[str, Decimal]] = []
            years: dict[str, list[Decimal]] = defaultdict(list)
            intensities: dict[str, list[Decimal]] = defaultdict(list)
            for event in selected:
                outcome = _outcome(by_time, event, bars, gaps)
                if outcome is None:
                    continue
                event_outcomes.append(outcome)
                years[str(datetime.fromtimestamp(event.start_ms / 1000, UTC).year)].append(
                    outcome["return_bps"]
                )
                bucket = (
                    "LOW"
                    if event.peak_intensity < 2
                    else "MEDIUM"
                    if event.peak_intensity < 4
                    else "HIGH"
                )
                intensities[bucket].append(outcome["return_bps"])
                control = _matching_control(
                    event, ordered_controls, used=used, excluded=condition_times
                )
                if control is not None:
                    control_outcome = _outcome(
                        by_time,
                        BehavioralEvent(
                            "control",
                            kind,
                            control.asset,
                            control.timestamp_ms,
                            control.timestamp_ms,
                            1,
                            Decimal(),
                            control,
                        ),
                        bars,
                        gaps,
                    )
                    if control_outcome is not None:
                        used.add(control.timestamp_ms)
                        controls.append(control_outcome)
            returns = [item["return_bps"] for item in event_outcomes]
            control_returns = [item["return_bps"] for item in controls]
            lift = (
                (
                    sum(returns, Decimal()) / len(returns)
                    - sum(control_returns, Decimal()) / len(control_returns)
                )
                if returns and controls
                else None
            )
            classification = (
                "REJECTED"
                if len(returns) < 8
                else "VOLATILITY_EXPLAINED"
                if kind == "OI_ACTIVE_VOLATILITY_CLUSTER" and lift is not None and abs(lift) < 20
                else "STRONG_RESEARCH_CANDIDATE"
                if lift is not None and abs(lift) >= 100 and len(years) >= 3
                else "SURVIVES_EVENT_VALIDATION"
                if lift is not None and abs(lift) >= 40
                else "WEAK"
            )
            type_report["horizons"][horizon] = {
                "independent_events": len(returns),
                "matched_controls": len(controls),
                "outcomes": _summary(returns),
                "controls": _summary(control_returns),
                "matched_mean_lift_bps": str(lift) if lift is not None else None,
                "max_favorable_bps": _summary([item["max_up_bps"] for item in event_outcomes]),
                "max_adverse_bps": _summary([item["max_down_bps"] for item in event_outcomes]),
                "threshold_outcomes": {
                    str(threshold): sum(value >= threshold for value in returns)
                    for threshold in THRESHOLDS_BPS
                },
                "intensity": {
                    bucket: _summary(values) for bucket, values in sorted(intensities.items())
                },
                "years": {year: _summary(values) for year, values in sorted(years.items())},
                "classification": classification,
            }
        output["event_types"][kind] = type_report
    return output


def analyze_behavioral_market(
    datasets: dict[str, Sequence[FlowObservation]], *, gaps_after_ms: dict[str, Iterable[int]]
) -> dict[str, object]:
    return {
        "format": "chappie-behavioral-market-v1",
        "methodology": {
            "event_definition": "adjacent completed 5m condition rows are collapsed; outcome horizons select non-overlapping starts",
            "controls": "same asset, calendar year, UTC hour and causal trend, nearest realized volatility/recent-return/volume state; controls never satisfy the target condition",
            "temporal_note": "calendar-year results are descriptive; 2026 is an inspected period, not pristine held-out evidence",
            "multiple_testing": {
                "event_types": len(EVENT_TYPES),
                "horizons": len(HORIZONS),
                "thresholds": len(THRESHOLDS_BPS),
            },
        },
        "assets": {
            asset: analyze_asset(rows, gaps_after_ms=gaps_after_ms.get(asset, ()))
            for asset, rows in sorted(datasets.items())
        },
        "limitations": [
            "Labels are observable-state shorthand, not claims about trader psychology.",
            "The event study is research-only and not a strategy, signal, or deployment approval.",
            "Controls are causal state matches, not randomized counterfactuals.",
        ],
    }
