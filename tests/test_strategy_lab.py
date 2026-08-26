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


def test_stateful_ema_replay_is_exactly_equivalent_and_skips_prefix_snapshots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = [100 + ((index * 17) % 23) - 11 for index in range(180)]
    candles = _candles(values)
    settings = StrategySettings(name="ema_crossover")
    reference = compare_strategy(
        candles,
        settings,
        evaluation_start_index=80,
        use_stateful_ema=False,
    )
    calls = 0
    original = MarketSnapshotFactory.create

    def counted_create(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(MarketSnapshotFactory, "create", counted_create)
    stateful = compare_strategy(candles, settings, evaluation_start_index=80)

    assert stateful == reference
    assert calls == 0
    assert compare_strategy(candles, settings, evaluation_start_index=80) == stateful


@pytest.mark.parametrize(
    ("values", "boundary"),
    [
        ([100] * 80, 27),
        ([100 + ((index * 19) % 29) - 14 for index in range(180)], 77),
        ([100 + ((index * 13) % 31) - 15 for index in range(220)], 120),
    ],
)
def test_stateful_ema_replay_matches_reference_across_boundaries(
    values: list[int], boundary: int
) -> None:
    candles = _candles(values)
    settings = StrategySettings(name="ema_crossover")
    assert compare_strategy(candles, settings, evaluation_start_index=boundary) == compare_strategy(
        candles,
        settings,
        evaluation_start_index=boundary,
        use_stateful_ema=False,
    )


@pytest.mark.parametrize(
    ("values", "boundary"),
    [
        ([100] * 80, 27),
        ([100 + ((index * 19) % 29) - 14 for index in range(180)], 77),
        ([100 + ((index * 13) % 31) - 15 for index in range(220)], 120),
    ],
)
def test_stateful_trend_filtered_replay_matches_full_history_reference(
    values: list[int], boundary: int
) -> None:
    candles = _candles(values)
    settings = StrategySettings(name="trend_filtered")
    assert compare_strategy(candles, settings, evaluation_start_index=boundary) == compare_strategy(
        candles,
        settings,
        evaluation_start_index=boundary,
        use_stateful_trend_filtered=False,
    )


def test_stateful_trend_filtered_replay_does_not_create_snapshots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candles = _candles([100 + ((index * 19) % 29) - 14 for index in range(180)])
    calls = 0
    original = MarketSnapshotFactory.create

    def counted_create(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(MarketSnapshotFactory, "create", counted_create)
    compare_strategy(candles, StrategySettings(name="trend_filtered"), evaluation_start_index=80)
    assert calls == 0


def test_stateful_trend_filtered_v2_replay_matches_reference_without_snapshots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candles = _candles([100 + ((index * 19) % 29) - 14 for index in range(180)])
    settings = StrategySettings(name="trend_filtered_v2", trend_confirmation_window_candles=2)
    reference = compare_strategy(
        candles, settings, evaluation_start_index=80, use_stateful_trend_filtered_v2=False
    )
    calls = 0
    original = MarketSnapshotFactory.create

    def counted_create(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(MarketSnapshotFactory, "create", counted_create)
    assert compare_strategy(candles, settings, evaluation_start_index=80) == reference
    assert calls == 0
