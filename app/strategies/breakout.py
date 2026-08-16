"""Long-only rolling-range breakout strategy for comparison research."""

from dataclasses import dataclass
from decimal import Decimal

from app.strategies.common import PureStrategy, StrategyIdentity, parameters
from app.strategies.indicators import average_true_range
from app.trading.exceptions import InvalidStrategyConfigurationError
from app.trading.models import MarketSnapshot, Signal, SignalDirection
from app.trading.strategy import SignalIdFactory


@dataclass(frozen=True, slots=True)
class BreakoutConfig:
    lookback: int = 20
    atr_window: int = 14
    minimum_atr_bps: Decimal = Decimal(5)
    exit_fraction: Decimal = Decimal("0.5")
    strategy_name: str = "breakout"
    strategy_version: str = "1.0.0"

    def __post_init__(self) -> None:
        if self.lookback < 2 or self.atr_window < 2:
            raise InvalidStrategyConfigurationError("breakout windows must be at least 2")
        if (
            any(
                not isinstance(value, Decimal) or not value.is_finite() or value < 0
                for value in (self.minimum_atr_bps, self.exit_fraction)
            )
            or self.exit_fraction > 1
        ):
            raise InvalidStrategyConfigurationError("breakout thresholds are invalid")


class BreakoutStrategy(PureStrategy):
    def __init__(self, config: BreakoutConfig, signal_id_factory: SignalIdFactory) -> None:
        self._config, self._signal_id_factory = config, signal_id_factory
        self._identity = StrategyIdentity(config.strategy_name, config.strategy_version)
        self._configuration = parameters(
            lookback=config.lookback,
            atr_window=config.atr_window,
            minimum_atr_bps=config.minimum_atr_bps,
            exit_fraction=config.exit_fraction,
        )

    @property
    def required_history(self) -> int:
        return max(self._config.lookback + 2, self._config.atr_window + 1)

    def generate_signal(self, snapshot: MarketSnapshot) -> Signal:
        if snapshot.latest_closed_candle is None or snapshot.is_stale:
            return self._signal(snapshot, SignalDirection.HOLD, "stale_or_empty", {})
        if len(snapshot.closed_candles) < self.required_history:
            return self._signal(snapshot, SignalDirection.HOLD, "insufficient_history", {})
        candles = snapshot.closed_candles
        closes = tuple(c.close for c in candles)
        prior = candles[-self._config.lookback - 1 : -1]
        prior_to_previous = candles[-self._config.lookback - 2 : -2]
        range_high, range_low = max(c.high for c in prior), min(c.low for c in prior)
        previous_range_high = max(c.high for c in prior_to_previous)
        price = closes[-1]
        atr = average_true_range(
            tuple(c.high for c in candles),
            tuple(c.low for c in candles),
            closes,
            self._config.atr_window,
        )
        atr_bps = atr / price * Decimal(10_000)
        exit_level = range_low + (range_high - range_low) * self._config.exit_fraction
        values = {
            "range_high": range_high,
            "range_low": range_low,
            "exit_level": exit_level,
            "atr_bps": atr_bps,
            "distance_to_breakout": price - range_high,
        }
        if (
            price > range_high
            and atr_bps >= self._config.minimum_atr_bps
            and candles[-2].close <= previous_range_high
        ):
            return self._signal(snapshot, SignalDirection.BUY, "breakout_entry", values)
        if price < exit_level:
            return self._signal(snapshot, SignalDirection.SELL, "breakout_structure_failed", values)
        return self._signal(snapshot, SignalDirection.HOLD, "inside_breakout_range", values)
