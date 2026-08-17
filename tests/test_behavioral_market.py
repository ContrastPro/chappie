from __future__ import annotations

from decimal import Decimal

from app.behavioral_market import (
    HORIZONS,
    analyze_asset,
    collapse_events,
    independent_events,
)
from app.market_intelligence_flow import FIVE_MINUTES_MS, FlowObservation, SpotFlowObservation


def _row(
    index: int,
    *,
    close: int = 100,
    state: str | None = None,
    funding: str | None = None,
    positioning: str | None = None,
    imbalance: str = "0",
) -> FlowObservation:
    timestamp = index * FIVE_MINUTES_MS
    spot = SpotFlowObservation(
        timestamp,
        Decimal(close),
        Decimal(100),
        Decimal(50),
        Decimal(50),
        Decimal("0.5"),
        Decimal(imbalance),
        10,
        Decimal(10),
        Decimal(2),
    )
    return FlowObservation(
        timestamp,
        "BTCUSDT",
        Decimal(close),
        spot,
        None,
        Decimal(funding) if funding else None,
        None,
        None,
        None,
        None,
        state,
        positioning,
        None,
    )


def _series(count: int = 80, *, stress_at: set[int] = frozenset()) -> tuple[FlowObservation, ...]:
    return tuple(
        _row(
            index,
            close=100 + index,
            state="PRICE_DOWN_OI_UP" if index in stress_at else "FLAT_OR_MIXED",
            funding="-0.001" if index in stress_at else None,
            positioning="EXTREME_LONG" if index in stress_at else None,
            imbalance="-0.1",
        )
        for index in range(count)
    )


def test_adjacent_rows_collapse_to_one_deterministic_event() -> None:
    rows = _series(stress_at={20, 21, 22})
    first = collapse_events(rows)
    second = collapse_events(rows)
    stress = [item for item in first if item.event_type == "CROWDED_STRESS"]
    assert len(stress) == 1
    assert stress[0].observations == 3
    assert first == second


def test_gap_never_connects_events_or_outcomes() -> None:
    rows = _series(stress_at={20, 34})
    events = [
        item
        for item in collapse_events(rows, gaps_after_ms=(22 * FIVE_MINUTES_MS,))
        if item.event_type == "CROWDED_STRESS"
    ]
    assert len(events) == 2
    selected = independent_events(
        events, horizon_bars=HORIZONS["1h"], gaps_after_ms=(22 * FIVE_MINUTES_MS,)
    )
    assert [item.start_ms for item in selected] == [34 * FIVE_MINUTES_MS]


def test_independent_selection_has_cooldown_and_no_outcome_overlap() -> None:
    rows = _series(stress_at={20, 35, 60})
    events = [item for item in collapse_events(rows) if item.event_type == "CROWDED_STRESS"]
    selected = independent_events(events, horizon_bars=HORIZONS["3h"], gaps_after_ms=())
    assert [item.start_ms for item in selected] == [20 * FIVE_MINUTES_MS, 60 * FIVE_MINUTES_MS]


def test_analysis_is_causal_and_reports_intensity_and_years() -> None:
    rows = _series(400, stress_at={20, 100, 180, 260, 340})
    report = analyze_asset(rows)
    result = report["event_types"]["CROWDED_STRESS"]["horizons"]["1h"]
    assert result["independent_events"] == 5
    assert set(result["threshold_outcomes"]) == {"60", "100", "150"}
    assert result["years"]


def test_future_price_change_does_not_change_event_identity() -> None:
    rows = list(_series(80, stress_at={20}))
    baseline = collapse_events(rows)
    rows[-1] = _row(79, close=999_999, state="FLAT_OR_MIXED")
    assert [
        item.event_id for item in collapse_events(rows) if item.event_type == "CROWDED_STRESS"
    ] == [item.event_id for item in baseline if item.event_type == "CROWDED_STRESS"]
