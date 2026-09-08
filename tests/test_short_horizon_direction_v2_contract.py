from datetime import UTC, datetime, timedelta

from app.research.capital_flow.short_horizon_direction_v2_contract import (
    BLIND_OOS_ACCESSED,
    CAPITAL_FLOW_FEATURE_ORDER,
    DATA_AUDIT,
    FEATURE_FAMILIES,
    INCREMENTAL_METRICS,
    MODEL_COMPARISONS,
    NEW_FEATURES,
    PRIMARY_HORIZON,
    RESEARCH_END_EXCLUSIVE,
    RESEARCH_EXECUTED,
    SECONDARY_HORIZON,
    VALIDATION_ACCESSED,
    VALIDATION_START,
    completed_candle_end,
    validate_contract,
)


def test_v2_contract_is_research_only_and_keeps_the_firewall() -> None:
    assert PRIMARY_HORIZON == timedelta(hours=1)
    assert SECONDARY_HORIZON == timedelta(hours=4)
    assert RESEARCH_END_EXCLUSIVE == VALIDATION_START
    assert not RESEARCH_EXECUTED and not VALIDATION_ACCESSED and not BLIND_OOS_ACCESSED
    validate_contract()


def test_v2_sensor_and_baseline_registry_is_compact_and_complete() -> None:
    assert len(CAPITAL_FLOW_FEATURE_ORDER) == 15
    assert len(NEW_FEATURES) == 45
    assert FEATURE_FAMILIES == ("CAPITAL_FLOW", "TECHNICAL", "VOLUME", "MARKET_STRUCTURE")
    assert "full_v2" in MODEL_COMPARISONS
    assert INCREMENTAL_METRICS == ("full_v2_minus_btc_only", "full_v2_minus_capital_flow_only")
    assert DATA_AUDIT["new_download_required"] is False
    assert DATA_AUDIT["crypto_rotation"].startswith("SOURCE_REQUIRED")


def test_higher_timeframe_features_only_use_completed_candles() -> None:
    timestamp = datetime(2024, 1, 1, 10, 37, tzinfo=UTC)
    assert completed_candle_end(timestamp, timedelta(minutes=15)) == datetime(2024, 1, 1, 10, 30, tzinfo=UTC)
    assert completed_candle_end(timestamp, timedelta(hours=4)) == datetime(2024, 1, 1, 8, tzinfo=UTC)
