from decimal import Decimal
from pathlib import Path

import pytest

from app.market.models import Candle
from app.research.strategy.history import (
    HistoricalDataset,
    HistoricalDatasetMetadata,
    candles_sha256,
)
from app.research.strategy.long_history import (
    manifest_payload,
    merge_verified_chunks,
    summarize_timeframe,
)


def _candles(start: int, count: int) -> tuple[Candle, ...]:
    return tuple(
        Candle(
            open_time_ms=(start + index) * 3_600_000,
            close_time_ms=(start + index + 1) * 3_600_000 - 1,
            open=Decimal(100 + index),
            high=Decimal(101 + index),
            low=Decimal(99 + index),
            close=Decimal(100 + index),
            volume=Decimal(10 + index),
        )
        for index in range(count)
    )


def _dataset(candles: tuple[Candle, ...]) -> HistoricalDataset:
    return HistoricalDataset(
        HistoricalDatasetMetadata(
            "BTCUSDT",
            "1h",
            candles[0].open_time_ms,
            candles[-1].open_time_ms + 3_600_000,
            candles[0].open_time_ms,
            candles[-1].open_time_ms,
            len(candles),
            "x",
            "test",
            candles_sha256(candles),
        ),
        candles,
    )


def test_long_history_summary_is_causal_and_forward_labels_are_separate() -> None:
    dataset = _dataset(_candles(0, 90))
    summary = summarize_timeframe((dataset,), "1h")
    assert summary["lookahead_audit"]["features_use_only_at_or_before_t"] is True  # type: ignore[index]
    assert summary["lookahead_audit"]["forward_returns_are_separate_labels"] is True  # type: ignore[index]
    assert summary["coverage"]["candle_count"] == 90  # type: ignore[index]
    assert summary["forward_return_labels_percent"]["1"]["count"] == 89  # type: ignore[index]
    assert summary["transaction_cost_context"]["round_trip_cost_bps"] == "20"  # type: ignore[index]


def test_long_history_chunk_boundary_gap_fails_closed() -> None:
    with pytest.raises(ValueError, match="gap"):
        merge_verified_chunks((_dataset(_candles(0, 20)), _dataset(_candles(21, 20))), "1h")


def test_long_history_manifest_contract_is_stable() -> None:
    dataset = _dataset(_candles(0, 20))

    payload = manifest_payload(
        Path("data/research/source/spot/BTCUSDT/long_history"), {"1h": (dataset,)}
    )

    assert payload["format"] == "chappie-long-history-manifest/v1"
    assert payload["integrity_status"] == "VERIFIED"
