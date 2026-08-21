from app.research.data.event_bridge import trace_compact_match
from app.research.intelligence.multi_asset_flow import (
    ALL_HYPOTHESES,
    COMBINATION_NAMES,
    SAMPLE_GATES,
    STATE_NAMES,
    select_control,
    state_contract,
)


def test_multi_asset_experiment_contract_is_bounded_and_predeclared():
    contract = state_contract()
    assert len(STATE_NAMES) == 9
    assert len(COMBINATION_NAMES) == 5
    assert len(ALL_HYPOTHESES) == 14
    assert set(contract["states"]) == set(STATE_NAMES[:5]) | set(STATE_NAMES[5:])
    assert set(contract["combinations"]) == set(COMBINATION_NAMES)
    assert SAMPLE_GATES == {"ROBUST": 100, "MODERATE": 40, "SMALL": 15, "SPARSE": 0}


def test_baseline_excludes_expanded_assets_and_contract_is_descriptive():
    contract = state_contract()
    assert contract["baseline"]["assets"] == ["BTC", "ETH"]
    text = json_text = str(contract)
    assert "exact-timestamp" in text
    assert "profit" not in json_text.lower()


def test_optimized_selector_is_exactly_equivalent_to_frozen_trace():
    snapshot = {
        "year": 2024,
        "hour_utc": 12,
        "trend": "UP",
        "volatility_bps": 10.25,
        "recent_return_bps": 30.0,
        "relative_volume": 1.1,
    }
    pool = [
        (
            index,
            {
                **snapshot,
                "volatility_bps": float(index % 17),
                "recent_return_bps": float(index % 11),
                "relative_volume": float(index % 7) / 3,
            },
        )
        for index in range(100)
    ]
    used, event_times, available = {8}, {9, 10}, set(range(100)) - {11}
    expected = trace_compact_match(
        event_id="event",
        asset="BTCUSDT",
        horizon_minutes=60,
        event_timestamp=500,
        event_snapshot=snapshot,
        pool=pool,
        used=used,
        event_times=event_times,
        available=available,
    ).selected_control_timestamp
    ordered = sorted(pool, key=lambda item: (float(item[1]["volatility_bps"]), item[0]))
    assert (
        select_control(
            event_snapshot=snapshot,
            ordered_pool=ordered,
            used=used,
            event_times=event_times,
            available=available,
        )
        == expected
    )
