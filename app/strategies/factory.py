"""Explicit construction of the small active strategy set."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from app.strategies.breakout import BreakoutConfig, BreakoutStrategy
from app.strategies.cost_aware_momentum import CostAwareMomentumConfig, CostAwareMomentumStrategy
from app.strategies.ema_crossover import EmaCrossoverConfig, EmaCrossoverStrategy
from app.strategies.mean_reversion import MeanReversionConfig, MeanReversionStrategy
from app.strategies.multi_timeframe_trend import (
    MultiTimeframeTrendConfig,
    MultiTimeframeTrendStrategy,
)
from app.strategies.regime_mean_reversion import (
    RegimeMeanReversionConfig,
    RegimeMeanReversionStrategy,
)
from app.strategies.trend_filtered import TrendFilteredConfig, TrendFilteredStrategy
from app.trading.exceptions import InvalidStrategyConfigurationError
from app.trading.strategy import SignalIdFactory, Strategy

StrategyName = Literal[
    "ema_crossover",
    "trend_filtered",
    "breakout",
    "mean_reversion",
    "cost_aware_momentum",
    "regime_mean_reversion",
    "multi_timeframe_trend",
]


@dataclass(frozen=True, slots=True)
class StrategySettings:
    name: StrategyName = "ema_crossover"
    ema_fast_window: int = 12
    ema_slow_window: int = 26
    trend_atr_window: int = 14
    trend_minimum_spread_bps: Decimal = Decimal(8)
    trend_minimum_atr_bps: Decimal = Decimal(5)
    breakout_lookback: int = 20
    breakout_atr_window: int = 14
    breakout_minimum_atr_bps: Decimal = Decimal(5)
    breakout_exit_fraction: Decimal = Decimal("0.5")
    mean_reversion_lookback: int = 20
    mean_reversion_entry_deviation_bps: Decimal = Decimal(35)
    mean_reversion_exit_deviation_bps: Decimal = Decimal(0)
    cost_momentum_fast_window: int = 8
    cost_momentum_slow_window: int = 21
    cost_momentum_minimum_strength_bps: Decimal = Decimal(35)
    cost_momentum_round_trip_cost_bps: Decimal = Decimal(20)
    cost_momentum_safety_margin_bps: Decimal = Decimal(15)
    regime_reversion_lookback: int = 20
    regime_reversion_entry_deviation_bps: Decimal = Decimal(60)
    regime_reversion_exit_deviation_bps: Decimal = Decimal(0)
    multi_timeframe_fast_window: int = 3
    multi_timeframe_slow_window: int = 5


def build_strategy(config: StrategySettings, signal_id_factory: SignalIdFactory) -> Strategy:
    if config.name == "ema_crossover":
        return EmaCrossoverStrategy(
            EmaCrossoverConfig(config.ema_fast_window, config.ema_slow_window), signal_id_factory
        )
    if config.name == "trend_filtered":
        return TrendFilteredStrategy(
            TrendFilteredConfig(
                config.ema_fast_window,
                config.ema_slow_window,
                config.trend_atr_window,
                config.trend_minimum_spread_bps,
                config.trend_minimum_atr_bps,
            ),
            signal_id_factory,
        )
    if config.name == "breakout":
        return BreakoutStrategy(
            BreakoutConfig(
                config.breakout_lookback,
                config.breakout_atr_window,
                config.breakout_minimum_atr_bps,
                config.breakout_exit_fraction,
            ),
            signal_id_factory,
        )
    if config.name == "mean_reversion":
        return MeanReversionStrategy(
            MeanReversionConfig(
                config.mean_reversion_lookback,
                config.mean_reversion_entry_deviation_bps,
                config.mean_reversion_exit_deviation_bps,
            ),
            signal_id_factory,
        )
    if config.name == "cost_aware_momentum":
        return CostAwareMomentumStrategy(
            CostAwareMomentumConfig(
                config.cost_momentum_fast_window,
                config.cost_momentum_slow_window,
                14,
                config.cost_momentum_minimum_strength_bps,
                config.cost_momentum_round_trip_cost_bps,
                config.cost_momentum_safety_margin_bps,
            ),
            signal_id_factory,
        )
    if config.name == "regime_mean_reversion":
        return RegimeMeanReversionStrategy(
            RegimeMeanReversionConfig(
                config.regime_reversion_lookback,
                config.regime_reversion_entry_deviation_bps,
                config.regime_reversion_exit_deviation_bps,
                config.cost_momentum_round_trip_cost_bps,
                config.cost_momentum_safety_margin_bps,
            ),
            signal_id_factory,
        )
    if config.name == "multi_timeframe_trend":
        return MultiTimeframeTrendStrategy(
            MultiTimeframeTrendConfig(
                fast_window=config.multi_timeframe_fast_window,
                slow_window=config.multi_timeframe_slow_window,
                round_trip_cost_bps=config.cost_momentum_round_trip_cost_bps,
                safety_margin_bps=config.cost_momentum_safety_margin_bps,
            ),
            signal_id_factory,
        )
    raise InvalidStrategyConfigurationError(f"unsupported strategy: {config.name}")
