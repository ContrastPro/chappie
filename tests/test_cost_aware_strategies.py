from decimal import Decimal

from app.market.models import Candle, MarketStreamKey
from app.strategies.cost_aware_momentum import CostAwareMomentumConfig, CostAwareMomentumStrategy
from app.strategies.multi_timeframe_trend import (
    MultiTimeframeTrendConfig,
    MultiTimeframeTrendStrategy,
)
from app.strategies.regime_mean_reversion import (
    RegimeMeanReversionConfig,
    RegimeMeanReversionStrategy,
)
from app.trading.models import SignalDirection
from app.trading.snapshot import MarketSnapshotFactory


class SignalIds:
    def __init__(self) -> None:
        self.value = 0

    def next_id(self) -> str:
        self.value += 1
        return f"signal-{self.value}"


def _snapshot(values: list[Decimal], *, gap_at: int | None = None):
    candles = tuple(
        Candle(
            open_time_ms=(index + (1 if gap_at is not None and index >= gap_at else 0)) * 60_000,
            close_time_ms=(index + (1 if gap_at is not None and index >= gap_at else 0)) * 60_000
            + 59_999,
            open=value,
            high=value + Decimal("0.1"),
            low=value - Decimal("0.1"),
            close=value,
            volume=Decimal(1),
        )
        for index, value in enumerate(values)
    )
    return MarketSnapshotFactory(120_000).create(
        MarketStreamKey("BTCUSDT", "5m"),
        candles,
        candles[-1].close_time_ms + 1,
        "candidate-test",
        correlation_cycle_id="candidate-test",
    )


def test_cost_aware_momentum_suppresses_marginal_signal_and_exits_deterministically() -> None:
    weak = CostAwareMomentumStrategy(
        CostAwareMomentumConfig(2, 4, 2, Decimal(0), Decimal(10_000), Decimal(0), Decimal(0)),
        SignalIds(),
    )
    strong = CostAwareMomentumStrategy(
        CostAwareMomentumConfig(2, 4, 2, Decimal(0), Decimal(1), Decimal(0), Decimal(0)),
        SignalIds(),
    )
    entry = [Decimal(value) for value in (10, 9, 8, 7, 8, 10)]
    assert weak.generate_signal(_snapshot(entry)).direction is SignalDirection.HOLD
    assert strong.generate_signal(_snapshot(entry)).direction is SignalDirection.BUY
    # The pure strategy is edge-triggered: an already-positive trend is HOLD, not a second BUY.
    assert (
        strong.generate_signal(_snapshot(entry + [Decimal(11)])).direction is SignalDirection.HOLD
    )
    assert strong.generate_signal(_snapshot(entry + [Decimal(6)])).direction is SignalDirection.SELL


def test_regime_mean_reversion_only_enters_range_and_exits_toward_mean() -> None:
    strategy = RegimeMeanReversionStrategy(
        RegimeMeanReversionConfig(20, Decimal(40), Decimal(0), Decimal(1), Decimal(0)), SignalIds()
    )
    ranged_oversold = [Decimal(100)] * 20 + [Decimal("99.5")]
    assert strategy.generate_signal(_snapshot(ranged_oversold)).direction is SignalDirection.BUY
    assert (
        strategy.generate_signal(_snapshot(ranged_oversold + [Decimal("100.1")])).direction
        is SignalDirection.SELL
    )
    # A materially directional/high-volatility history is gated even if the final candle is low.
    trended_oversold = [Decimal(120) - Decimal(index) for index in range(20)] + [Decimal(90)]
    assert strategy.generate_signal(_snapshot(trended_oversold)).direction is SignalDirection.HOLD


def test_multi_timeframe_trend_requires_aligned_confirmation_and_has_deterministic_exit() -> None:
    strategy = MultiTimeframeTrendStrategy(
        MultiTimeframeTrendConfig(3, 12, 2, 3, Decimal(0), Decimal(0)), SignalIds()
    )
    # Each 12-bar higher group rises, while the final lower bars make one timely entry crossover.
    values = [Decimal(100 + index) for index in range(68)] + [
        Decimal(162),
        Decimal(161),
        Decimal(160),
        Decimal(165),
    ]
    signal = strategy.generate_signal(_snapshot(values))
    assert signal.direction is SignalDirection.BUY
    assert signal.metadata["higher_confirmed"] == "true"
    assert signal.metadata["medium_confirmed"] == "true"
    # A synchronized lower-stream drop invalidates the medium confirmation deterministically.
    assert (
        strategy.generate_signal(_snapshot(values + [Decimal(120)] * 12)).direction
        is SignalDirection.SELL
    )
    assert strategy.generate_signal(_snapshot(values, gap_at=20)).direction is SignalDirection.HOLD
