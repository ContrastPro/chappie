"""Pure selectable strategies; none perform I/O or execution."""

from app.strategies.breakout import BreakoutConfig, BreakoutStrategy
from app.strategies.ema_crossover import (
    EmaCrossoverConfig,
    EmaCrossoverStrategy,
    calculate_ema,
)
from app.strategies.factory import StrategyName, StrategySettings, build_strategy
from app.strategies.mean_reversion import MeanReversionConfig, MeanReversionStrategy
from app.strategies.trend_filtered import TrendFilteredConfig, TrendFilteredStrategy

__all__ = [
    "BreakoutConfig",
    "BreakoutStrategy",
    "EmaCrossoverConfig",
    "EmaCrossoverStrategy",
    "MeanReversionConfig",
    "MeanReversionStrategy",
    "StrategyName",
    "StrategySettings",
    "TrendFilteredConfig",
    "TrendFilteredStrategy",
    "build_strategy",
    "calculate_ema",
]
