from __future__ import annotations

from decimal import Decimal

import pytest

from app.market.models import Candle
from app.research.intelligence.market import (
    ContextDataset,
    ContextObservation,
    asof_observation,
    completed_candle_asof,
    conditional_study,
    context_digest,
    context_to_payload,
    load_context_payload,
    price_oi_state,
)


def _observation(timestamp: int, value: str = "1") -> ContextObservation:
    return ContextObservation(timestamp, timestamp, Decimal(value), "fixture")


def _candle(index: int) -> Candle:
    return Candle(
        open_time_ms=index * 900_000,
        close_time_ms=index * 900_000 + 899_999,
        open=Decimal(100),
        high=Decimal(101),
        low=Decimal(99),
        close=Decimal(100),
        volume=Decimal(1),
    )


def test_asof_funding_alignment_never_reads_a_future_publication() -> None:
    observations = (_observation(8 * 60 * 60 * 1_000, "0.0001"),)
    assert (
        asof_observation(
            observations, available_at_ms=8 * 60 * 60 * 1_000 - 1, max_age_ms=8 * 60 * 60 * 1_000
        )
        is None
    )
    assert (
        asof_observation(
            observations, available_at_ms=8 * 60 * 60 * 1_000, max_age_ms=8 * 60 * 60 * 1_000
        )
        == observations[0]
    )


def test_oi_missing_or_stale_observation_is_explicit_not_forward_filled() -> None:
    observations = (_observation(0),)
    assert (
        asof_observation(observations, available_at_ms=300_000, max_age_ms=300_000)
        == observations[0]
    )
    assert asof_observation(observations, available_at_ms=300_001, max_age_ms=300_000) is None


def test_eth_join_requires_completed_candle_at_btc_timestamp() -> None:
    candles = (_candle(0), _candle(1))
    assert completed_candle_asof(candles, 899_998) is None
    assert completed_candle_asof(candles, 899_999) == candles[0]
    assert completed_candle_asof(candles, 900_000) == candles[0]


def test_context_digest_is_deterministic_and_tampering_fails_closed() -> None:
    dataset = ContextDataset("funding_rate", "BTCUSDT", "8h", "fixture", (_observation(0),))
    payload = context_to_payload(dataset)
    assert payload["format"] == "chappie-market-context/v1"
    assert load_context_payload(payload) == dataset
    payload["observations"][0]["value"] = "2"  # type: ignore[index]
    with pytest.raises(ValueError, match="digest"):
        load_context_payload(payload)
    assert context_digest(dataset) == context_digest(dataset)


def test_conditional_study_reports_same_period_baseline_and_temporal_split() -> None:
    rows = (
        (1672531200000, "price_up_oi_up", Decimal(100)),
        (1704067200000, "price_up_oi_down", Decimal(0)),
    )
    result = conditional_study(rows, threshold_bps=Decimal(20))
    assert result["baseline"] == {"count": 2, "probability": "0.5"}
    assert result["states"]["price_up_oi_up"]["relative_lift"] == "1"  # type: ignore[index]
    assert set(result["temporal_splits"]) == {"2023", "2024"}  # type: ignore[arg-type]
    assert price_oi_state(Decimal(2), Decimal(1), Decimal(2), Decimal(1)) == "price_up_oi_up"
