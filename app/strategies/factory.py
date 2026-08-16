"""Explicit construction of the small active strategy set."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from app.strategies.breakout import BreakoutConfig, BreakoutStrategy
from app.strategies.ema_crossover import EmaCrossoverConfig, EmaCrossoverStrategy
from app.strategies.mean_reversion import MeanReversionConfig, MeanReversionStrategy
from app.strategies.trend_filtered import TrendFilteredConfig, TrendFilteredStrategy
from app.trading.exceptions import InvalidStrategyConfigurationError
from app.trading.strategy import SignalIdFactory, Strategy

StrategyName = Literal["ema_crossover", "trend_filtered", "breakout", "mean_reversion"]


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
    raise InvalidStrategyConfigurationError(f"unsupported strategy: {config.name}")
