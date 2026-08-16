"""Pure selectable strategies; none perform I/O or execution."""

from app.strategies.breakout import BreakoutConfig, BreakoutStrategy
from app.strategies.cost_aware_momentum import CostAwareMomentumConfig, CostAwareMomentumStrategy
from app.strategies.ema_crossover import (
    EmaCrossoverConfig,
    EmaCrossoverStrategy,
    calculate_ema,
)
from app.strategies.factory import StrategyName, StrategySettings, build_strategy
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

__all__ = [
    "BreakoutConfig",
    "BreakoutStrategy",
    "CostAwareMomentumConfig",
    "CostAwareMomentumStrategy",
    "EmaCrossoverConfig",
    "EmaCrossoverStrategy",
    "MeanReversionConfig",
    "MeanReversionStrategy",
    "MultiTimeframeTrendConfig",
    "MultiTimeframeTrendStrategy",
    "RegimeMeanReversionConfig",
    "RegimeMeanReversionStrategy",
    "StrategyName",
    "StrategySettings",
    "TrendFilteredConfig",
    "TrendFilteredStrategy",
    "build_strategy",
    "calculate_ema",
]
