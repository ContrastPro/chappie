from decimal import Decimal

import pytest

from app.market.models import Candle, MarketStreamKey
from app.research.strategy.lab import StrategyComparisonConfig, compare_all, compare_strategy
from app.strategies import (
    BreakoutConfig,
    BreakoutStrategy,
    MeanReversionConfig,
    MeanReversionStrategy,
    StrategySettings,
    TrendFilteredConfig,
    TrendFilteredStrategy,
)
from app.trading.models import SignalDirection
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
            high=Decimal(value + 1),
            low=Decimal(value - 1),
            close=Decimal(value),
            volume=Decimal(1),
        )
        for index, value in enumerate(values)
    )


def _snapshot(values: list[int]):
    candles = _candles(values)
    return MarketSnapshotFactory(120_000).create(
        MarketStreamKey("BTCUSDT", "1m"),
        candles,
        candles[-1].close_time_ms + 1,
        "test",
        correlation_cycle_id="cycle",
    )


def test_trend_filter_accepts_confirmed_crossover_and_filters_flat_market() -> None:
    strategy = TrendFilteredStrategy(
        TrendFilteredConfig(2, 3, 2, Decimal(0), Decimal(0)), SignalIds()
    )
    assert strategy.generate_signal(_snapshot([3, 2, 1, 4])).direction is SignalDirection.BUY
    flat = TrendFilteredStrategy(
        TrendFilteredConfig(2, 3, 2, Decimal(10_000), Decimal(10_000)), SignalIds()
    )
    assert flat.generate_signal(_snapshot([3, 2, 1, 4])).direction is SignalDirection.HOLD


def test_breakout_and_mean_reversion_emit_long_only_entry_and_exit_signals() -> None:
    breakout = BreakoutStrategy(BreakoutConfig(5, 2, Decimal(0)), SignalIds())
    assert breakout.generate_signal(_snapshot([100] * 25 + [130])).direction is SignalDirection.BUY
    assert (
        breakout.generate_signal(_snapshot([100, 101, 102, 103, 104, 104, 104])).direction
        is SignalDirection.HOLD
    )
    mean = MeanReversionStrategy(MeanReversionConfig(5, Decimal(10), Decimal(0)), SignalIds())
    assert mean.generate_signal(_snapshot([100] * 20 + [90])).direction is SignalDirection.BUY
    assert mean.generate_signal(_snapshot([100] * 20 + [90, 101])).direction is SignalDirection.SELL


def test_comparison_is_offline_same_input_and_includes_fee_economics() -> None:
    candles = _candles([100 + ((index % 8) - 4) * 3 + index // 8 for index in range(80)])
    config = StrategyComparisonConfig(Decimal(10_000), Decimal("0.001"))
    results = compare_all(candles, config)
    assert {result.strategy_name for result in results} == {
        "ema_crossover",
        "trend_filtered",
        "breakout",
        "mean_reversion",
    }
    assert all(result.starting_equity == Decimal(10_000) and result.fees >= 0 for result in results)
    assert all(result.turnover_equity_percent >= 0 for result in results)
    # Deterministic and pure: identical inputs/configuration give identical metrics.
    assert compare_strategy(candles, StrategySettings(name="ema_crossover"), config) == results[0]


def test_mean_reversion_replay_charges_quote_fees_without_reusing_invested_cash() -> None:
    result = compare_strategy(
        _candles([100] * 20 + [90, 89, 101]),
        StrategySettings(name="mean_reversion"),
        StrategyComparisonConfig(Decimal(10_000), Decimal("0.001")),
    )
    buy_gross = Decimal(10_000) / Decimal("1.001")
    quantity = buy_gross / Decimal(90)
    sell_gross = quantity * Decimal(101)
    expected_fees = buy_gross * Decimal("0.001") + sell_gross * Decimal("0.001")

    assert result.completed_round_trips == 1
    assert result.fills == 2
    assert result.fees == expected_fees
    assert result.ending_equity == sell_gross - (sell_gross * Decimal("0.001"))
    assert result.ending_equity < Decimal(20_000)  # no leverage or duplicated capital


def test_boundary_replay_uses_prior_history_without_counting_prior_accounting() -> None:
    candles = _candles([100] * 20 + [90, 89, 101] + [100] * 10)
    config = StrategyComparisonConfig(Decimal(10_000), Decimal("0.001"))
    full = compare_strategy(candles, StrategySettings(name="mean_reversion"), config)
    boundary = compare_strategy(
        candles,
        StrategySettings(name="mean_reversion"),
        config,
        evaluation_start_index=23,
    )
    assert full.fills == 2 and full.fees > 0
    assert boundary.starting_equity == Decimal(10_000)
    assert boundary.fills == 0
    assert boundary.fees == 0
    assert boundary.net_pnl == 0
    assert boundary.turnover == 0


def test_boundary_replay_fails_closed_without_required_prior_history() -> None:
    with pytest.raises(ValueError, match="warmup"):
        compare_strategy(
            _candles([100] * 40),
            StrategySettings(name="ema_crossover"),
            evaluation_start_index=26,
        )
