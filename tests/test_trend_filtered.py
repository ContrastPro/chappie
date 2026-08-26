from decimal import Decimal

import pytest

from app.market.models import Candle, MarketStreamKey
from app.strategies.trend_filtered import (
    TREND_FILTERED_STATE_IDENTITY,
    TrendFilteredConfig,
    TrendFilteredStateV1,
    TrendFilteredStrategy,
)
from app.trading.exceptions import StrategyInvariantError
from app.trading.snapshot import MarketSnapshotFactory


class SignalIds:
    def __init__(self) -> None:
        self.value = 0

    def next_id(self) -> str:
        self.value += 1
        return f"signal-{self.value}"


def _candles(values: list[int]) -> tuple[Candle, ...]:
    return tuple(
        Candle(
            open_time_ms=index * 60_000,
            close_time_ms=index * 60_000 + 59_999,
            open=Decimal(value),
            high=Decimal(value + 2),
            low=Decimal(value - 2),
            close=Decimal(value),
            volume=Decimal(1),
        )
        for index, value in enumerate(values)
    )


def _strategy() -> TrendFilteredStrategy:
    return TrendFilteredStrategy(TrendFilteredConfig(2, 3, 2, Decimal(0), Decimal(0)), SignalIds())


def _reference(strategy: TrendFilteredStrategy, values: list[int]):
    candles = _candles(values)
    snapshot = MarketSnapshotFactory(120_000).create(
        MarketStreamKey("BTCUSDT", "1m"),
        candles,
        candles[-1].close_time_ms + 1,
        "reference",
        correlation_cycle_id="reference",
    )
    return strategy.generate_signal(snapshot)


def test_state_round_trip_and_continuation_match_reference_signal_and_values() -> None:
    values = [4, 3, 2, 1, 5]
    strategy = _strategy()
    state = strategy.create_state(
        _candles(values[:4]),
        symbol="BTCUSDT",
        interval="1m",
        dataset_digest="dataset",
        boundary_index=4,
    )
    restored = TrendFilteredStateV1.from_serialized(state.serialized())
    continued = strategy.continue_state(
        restored,
        _candles(values)[4],
        symbol="BTCUSDT",
        interval="1m",
        dataset_digest="dataset",
        boundary_index=4,
    )
    reference = _reference(_strategy(), values)

    assert state.identity == TREND_FILTERED_STATE_IDENTITY
    assert restored == state
    assert continued.direction is reference.direction
    assert continued.reason_code == reference.reason_code
    assert continued.fast_ema == reference.metadata["fast_ema"]
    assert continued.slow_ema == reference.metadata["slow_ema"]
    assert continued.spread_bps == reference.metadata["spread_bps"]
    assert continued.atr_bps == reference.metadata["atr_bps"]


@pytest.mark.parametrize(
    ("symbol", "interval", "dataset_digest", "boundary_index"),
    [
        ("ETHUSDT", "1m", "dataset", 4),
        ("BTCUSDT", "5m", "dataset", 4),
        ("BTCUSDT", "1m", "other", 4),
        ("BTCUSDT", "1m", "dataset", 5),
    ],
)
def test_stateful_continuation_rejects_stale_bindings(
    symbol: str, interval: str, dataset_digest: str, boundary_index: int
) -> None:
    strategy = _strategy()
    state = strategy.create_state(
        _candles([4, 3, 2, 1]),
        symbol="BTCUSDT",
        interval="1m",
        dataset_digest="dataset",
        boundary_index=4,
    )
    with pytest.raises(StrategyInvariantError, match="trend filtered state binding"):
        strategy.continue_state(
            state,
            _candles([4, 3, 2, 1, 5])[4],
            symbol=symbol,
            interval=interval,
            dataset_digest=dataset_digest,
            boundary_index=boundary_index,
        )


def test_stateful_continuation_rejects_changed_configuration() -> None:
    state = _strategy().create_state(
        _candles([4, 3, 2, 1]),
        symbol="BTCUSDT",
        interval="1m",
        dataset_digest="dataset",
        boundary_index=4,
    )
    changed = TrendFilteredStrategy(
        TrendFilteredConfig(2, 3, 2, Decimal(1), Decimal(0)), SignalIds()
    )
    with pytest.raises(StrategyInvariantError, match="trend filtered state binding"):
        changed.continue_state(
            state,
            _candles([4, 3, 2, 1, 5])[4],
            symbol="BTCUSDT",
            interval="1m",
            dataset_digest="dataset",
            boundary_index=4,
        )
