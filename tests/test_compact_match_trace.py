from __future__ import annotations

from app.research.data.event_bridge import trace_compact_match


def _snapshot(volatility: float, recent: float = 0, volume: float | None = 1):
    return {"volatility_bps": volatility, "recent_return_bps": recent, "relative_volume": volume}


def test_trace_exposes_window_exclusions_ranking_and_determinism() -> None:
    pool = [(index, _snapshot(float(index))) for index in range(60)]
    trace = trace_compact_match(
        event_id="event",
        asset="BTCUSDT",
        horizon_minutes=60,
        event_timestamp=99,
        event_snapshot=_snapshot(30),
        pool=pool,
        used={29},
        event_times={30},
        available=set(range(60)) - {31},
    )
    assert trace.bisect_center == 30 and trace.slice_start == 6 and trace.slice_end == 54
    assert dict(trace.exclusions)[29] == "USED"
    assert dict(trace.exclusions)[30] == "EVENT_OVERLAP"
    assert dict(trace.exclusions)[31] == "OUTCOME_UNAVAILABLE"
    assert trace.selected_control_timestamp == 28
    assert trace == trace_compact_match(
        event_id="event",
        asset="BTCUSDT",
        horizon_minutes=60,
        event_timestamp=99,
        event_snapshot=_snapshot(30),
        pool=pool,
        used={29},
        event_times={30},
        available=set(range(60)) - {31},
    )


def test_global_best_outside_local_window_and_missing_volume_are_visible() -> None:
    pool = [
        (index, _snapshot(float(index), volume=None if index == 0 else 5)) for index in range(100)
    ]
    trace = trace_compact_match(
        event_id="event",
        asset="BTCUSDT",
        horizon_minutes=60,
        event_timestamp=999,
        event_snapshot=_snapshot(50, volume=None),
        pool=pool,
        available=set(range(100)),
    )
    assert 0 not in trace.local_timestamps
    assert trace.selected_control_timestamp == 50
    assert trace.rankings[0][1][2] == 5
