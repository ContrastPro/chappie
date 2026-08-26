from decimal import Decimal

import pytest

from app.market.models import Candle, MarketStreamKey
from app.strategies.trend_filtered_v2 import (
    TREND_FILTERED_V2_STATE_IDENTITY,
    TrendFilteredV2Config,
    TrendFilteredV2StateV1,
    TrendFilteredV2Strategy,
)
from app.trading.exceptions import InvalidStrategyConfigurationError, StrategyInvariantError
from app.trading.models import SignalDirection
from app.trading.snapshot import MarketSnapshotFactory


class _Ids:
    def next_id(self) -> str:
        return "test"


def _candles(values: list[int]) -> tuple[Candle, ...]:
    return tuple(
        Candle(
            open_time_ms=i * 60_000,
            close_time_ms=i * 60_000 + 59_999,
            open=Decimal(v),
            high=Decimal(v + 2),
            low=Decimal(v - 2),
            close=Decimal(v),
            volume=Decimal(1),
        )
        for i, v in enumerate(values)
    )


def _strategy(window: int = 2, spread: int = 0, atr: int = 0) -> TrendFilteredV2Strategy:
    return TrendFilteredV2Strategy(
        TrendFilteredV2Config(2, 3, 2, Decimal(spread), Decimal(atr), window), _Ids()
    )


def _advance(strategy: TrendFilteredV2Strategy, values: list[int]):
    candles = _candles(values)
    state = strategy.create_state(
        candles[:4], symbol="BTCUSDT", interval="1m", dataset_digest="data", boundary_index=4
    )
    results = []
    for candle in candles[4:]:
        result = strategy.continue_state(
            state, candle, symbol="BTCUSDT", interval="1m", dataset_digest="data", boundary_index=4
        )
        results.append(result)
        state = result.state
    return results


def test_identity_validation_and_no_same_candle_confirmation() -> None:
    with pytest.raises(InvalidStrategyConfigurationError):
        TrendFilteredV2Config(confirmation_window_candles=0)
    result = _advance(_strategy(), [4, 3, 2, 1, 5])[0]
    assert result.direction is SignalDirection.HOLD
    assert result.reason_code == "pending_crossover"
    assert result.state.identity == TREND_FILTERED_V2_STATE_IDENTITY
    assert result.state.pending_direction == "BULLISH"
    assert result.state.pending_age_candles == 0


def test_delayed_confirmation_expiration_and_relation_cancellation() -> None:
    delayed = _advance(_strategy(window=2), [4, 3, 2, 1, 5, 6])
    assert delayed[-1].direction is SignalDirection.BUY
    assert delayed[-1].state.pending_direction is None
    expired = _advance(_strategy(window=1, spread=10_000), [4, 3, 2, 1, 5, 6])
    assert expired[-1].reason_code == "pending_confirmation_expired"
    reversed_state = _advance(_strategy(window=3), [4, 3, 2, 1, 5, 1])
    assert reversed_state[-1].reason_code == "pending_crossover"
    assert reversed_state[-1].state.pending_direction == "BEARISH"


def test_state_round_trip_stale_rejection_and_reference_equivalence() -> None:
    values = [4, 3, 2, 1, 5, 6, 5, 4, 3, 2, 5]
    strategy = _strategy(window=2)
    results = _advance(strategy, values)
    restored = TrendFilteredV2StateV1.from_serialized(results[-1].state.serialized())
    assert restored == results[-1].state
    with pytest.raises(StrategyInvariantError):
        strategy.continue_state(
            restored,
            _candles(values + [6])[-1],
            symbol="BTCUSDT",
            interval="1m",
            dataset_digest="other",
            boundary_index=4,
        )
    candles = _candles(values)
    snapshot = MarketSnapshotFactory(120_000).create(
        MarketStreamKey("BTCUSDT", "1m"),
        candles,
        candles[-1].close_time_ms + 1,
        "reference",
        correlation_cycle_id="reference",
    )
    signal = _strategy(window=2).generate_signal(snapshot)
    assert signal.direction is results[-1].direction
