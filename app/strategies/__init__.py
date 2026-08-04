"""Pure strategy implementations."""

from app.strategies.ema_crossover import (
    EmaCrossoverConfig,
    EmaCrossoverStrategy,
    calculate_ema,
)

__all__ = ["EmaCrossoverConfig", "EmaCrossoverStrategy", "calculate_ema"]
