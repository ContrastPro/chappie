"""Deterministic, offline-only coverage for the Sol TOP-3 research hypotheses."""

from decimal import Decimal

from app.market.models import Candle, MarketStreamKey
from app.strategies.compression_expansion_breakout import (
    CompressionExpansionBreakoutConfig,
    CompressionExpansionBreakoutStrategy,
)
from app.strategies.trend_retracement_continuation import (
    TrendRetracementContinuationConfig,
    TrendRetracementContinuationStrategy,
)
from app.strategies.volatility_continuation import (
    VolatilityContinuationConfig,
    VolatilityContinuationStrategy,
)
from app.trading.models import SignalDirection
from app.trading.snapshot import MarketSnapshotFactory


class SignalIds:
    def __init__(self) -> None:
        self.value = 0

    def next_id(self) -> str:
        self.value += 1
        return f"top3-{self.value}"


def _snapshot(values: list[int], volumes: list[int] | None = None, *, gap_at: int | None = None):
    volumes = volumes or [1] * len(values)
    candles = tuple(
        Candle(
            open_time_ms=(index + (1 if gap_at is not None and index >= gap_at else 0)) * 60_000,
            close_time_ms=(index + (1 if gap_at is not None and index >= gap_at else 0)) * 60_000
            + 59_999,
            open=Decimal(value),
            high=Decimal(value + 1),
            low=Decimal(value - 1),
            close=Decimal(value),
            volume=Decimal(volumes[index]),
        )
        for index, value in enumerate(values)
    )
    return MarketSnapshotFactory(120_000).create(
        MarketStreamKey("BTCUSDT", "5m"),
        candles,
        candles[-1].close_time_ms + 1,
        "top3",
        correlation_cycle_id="top3",
    )


def _expand(medium: list[int]) -> list[int]:
    return [value for value in medium for _ in range(2)]


def test_volatility_continuation_requires_volatility_cost_hurdle_and_edge() -> None:
    values = _expand([100, 102, 104, 106, 108, 110, 110, 109, 111, 113])
    configuration = VolatilityContinuationConfig(
        lower_bars_per_medium=2,
        lower_bars_per_higher=4,
        fast_window=2,
        slow_window=3,
        atr_window=2,
        minimum_volatility_bps=Decimal(1),
        maximum_volatility_bps=Decimal(10_000),
        cost_hurdle_bps=Decimal(1),
    )
    strategy = VolatilityContinuationStrategy(configuration, SignalIds())
    assert strategy.generate_signal(_snapshot(values)).direction is SignalDirection.BUY
    # The completed continuation edge is consumed; it is not a repeated BUY.
    assert (
        strategy.generate_signal(_snapshot(values + [114, 114])).direction is SignalDirection.HOLD
    )
    blocked = VolatilityContinuationStrategy(
        VolatilityContinuationConfig(
            lower_bars_per_medium=2,
            lower_bars_per_higher=4,
            fast_window=2,
            slow_window=3,
            atr_window=2,
            minimum_volatility_bps=Decimal(1),
            maximum_volatility_bps=Decimal(10_000),
            cost_hurdle_bps=Decimal(10_000),
        ),
        SignalIds(),
    )
    assert blocked.generate_signal(_snapshot(values)).direction is SignalDirection.HOLD
    assert strategy.generate_signal(_snapshot(values, gap_at=8)).direction is SignalDirection.HOLD


def test_trend_retracement_is_generic_and_exits_on_trend_breakdown() -> None:
    values = _expand([100, 102, 104, 106, 110, 108, 106, 107, 109, 111])
    strategy = TrendRetracementContinuationStrategy(
        TrendRetracementContinuationConfig(
            lower_bars_per_medium=2,
            lower_bars_per_higher=4,
            fast_window=2,
            slow_window=3,
            pullback_window=4,
            atr_window=2,
            minimum_retracement_atr=Decimal("0.25"),
            maximum_retracement_atr=Decimal(10),
            cost_hurdle_bps=Decimal(1),
        ),
        SignalIds(),
    )
    signal = strategy.generate_signal(_snapshot(values))
    assert signal.direction is SignalDirection.BUY
    assert "fibonacci" not in signal.explanation.lower()
    assert (
        strategy.generate_signal(
            _snapshot(_expand([100, 101, 102, 103, 100, 98, 96, 94, 92, 90]))
        ).direction
        is SignalDirection.SELL
    )


def test_compression_breakout_requires_all_causal_components_and_exits() -> None:
    values = _expand([100, 100, 100, 100, 100, 101, 102, 103, 104, 110])
    strategy = CompressionExpansionBreakoutStrategy(
        CompressionExpansionBreakoutConfig(
            lower_bars_per_medium=2,
            lower_bars_per_higher=4,
            trend_fast_window=2,
            trend_slow_window=3,
            compression_window=2,
            breakout_lookback=2,
            atr_window=2,
            compression_ratio=Decimal(10),
            expansion_ratio=Decimal("0.1"),
            volume_ratio=Decimal("0.1"),
            cost_hurdle_bps=Decimal(1),
        ),
        SignalIds(),
    )
    assert strategy.generate_signal(_snapshot(values)).direction is SignalDirection.BUY
    # Without volume confirmation, the same structural candidate is held.
    no_volume = CompressionExpansionBreakoutStrategy(
        CompressionExpansionBreakoutConfig(
            lower_bars_per_medium=2,
            lower_bars_per_higher=4,
            trend_fast_window=2,
            trend_slow_window=3,
            compression_window=2,
            breakout_lookback=2,
            atr_window=2,
            compression_ratio=Decimal(10),
            expansion_ratio=Decimal("0.1"),
            volume_ratio=Decimal(100),
            cost_hurdle_bps=Decimal(1),
        ),
        SignalIds(),
    )
    assert no_volume.generate_signal(_snapshot(values)).direction is SignalDirection.HOLD
    assert (
        strategy.generate_signal(_snapshot(values + [100, 100])).direction is SignalDirection.SELL
    )


def test_top3_uses_only_completed_higher_timeframe_groups() -> None:
    # The final two lower bars cannot make a four-bar higher candle.  Appending
    # them changes the current lower snapshot but never rewrites an earlier
    # completed higher-timeframe value.
    values = _expand([100, 102, 104, 106, 108, 110, 110, 109, 111, 113])
    strategy = VolatilityContinuationStrategy(
        VolatilityContinuationConfig(
            lower_bars_per_medium=2,
            lower_bars_per_higher=4,
            fast_window=2,
            slow_window=3,
            atr_window=2,
            minimum_volatility_bps=Decimal(1),
            maximum_volatility_bps=Decimal(10_000),
            cost_hurdle_bps=Decimal(1),
        ),
        SignalIds(),
    )
    before = strategy.generate_signal(_snapshot(values))
    after = strategy.generate_signal(_snapshot(values + [115, 115]))
    assert before.metadata["higher_bullish"] == after.metadata["higher_bullish"]
