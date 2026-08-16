"""Long-only rolling-mean reversion strategy for range research."""

from dataclasses import dataclass
from decimal import Decimal

from app.strategies.common import PureStrategy, StrategyIdentity, parameters
from app.strategies.indicators import rolling_mean
from app.trading.exceptions import InvalidStrategyConfigurationError
from app.trading.models import MarketSnapshot, Signal, SignalDirection
from app.trading.strategy import SignalIdFactory


@dataclass(frozen=True, slots=True)
class MeanReversionConfig:
    lookback: int = 20
    entry_deviation_bps: Decimal = Decimal(35)
    exit_deviation_bps: Decimal = Decimal(0)
    strategy_name: str = "mean_reversion"
    strategy_version: str = "1.0.0"

    def __post_init__(self) -> None:
        if self.lookback < 2:
            raise InvalidStrategyConfigurationError("mean-reversion lookback must be at least 2")
        if any(
            not isinstance(value, Decimal) or not value.is_finite() or value < 0
            for value in (self.entry_deviation_bps, self.exit_deviation_bps)
        ):
            raise InvalidStrategyConfigurationError(
                "mean-reversion thresholds must be finite and non-negative"
            )


class MeanReversionStrategy(PureStrategy):
    def __init__(self, config: MeanReversionConfig, signal_id_factory: SignalIdFactory) -> None:
        self._config, self._signal_id_factory = config, signal_id_factory
        self._identity = StrategyIdentity(config.strategy_name, config.strategy_version)
        self._configuration = parameters(
            lookback=config.lookback,
            entry_deviation_bps=config.entry_deviation_bps,
            exit_deviation_bps=config.exit_deviation_bps,
        )

    @property
    def required_history(self) -> int:
        # Previous-window comparison makes entry/exit edge-triggered without
        # retaining mutable exposure state in a pure strategy.
        return self._config.lookback + 1

    def generate_signal(self, snapshot: MarketSnapshot) -> Signal:
        if snapshot.latest_closed_candle is None or snapshot.is_stale:
            return self._signal(snapshot, SignalDirection.HOLD, "stale_or_empty", {})
        if len(snapshot.closed_candles) < self.required_history:
            return self._signal(snapshot, SignalDirection.HOLD, "insufficient_history", {})
        closes = tuple(c.close for c in snapshot.closed_candles)
        mean = rolling_mean(closes, self._config.lookback)
        previous_mean = rolling_mean(closes[:-1], self._config.lookback)
        price = closes[-1]
        previous_price = closes[-2]
        deviation_bps = (price - mean) / mean * Decimal(10_000)
        previous_deviation_bps = (previous_price - previous_mean) / previous_mean * Decimal(10_000)
        values = {
            "rolling_mean": mean,
            "deviation_bps": deviation_bps,
            "previous_deviation_bps": previous_deviation_bps,
        }
        if (
            previous_deviation_bps > -self._config.entry_deviation_bps
            and deviation_bps <= -self._config.entry_deviation_bps
        ):
            return self._signal(snapshot, SignalDirection.BUY, "mean_reversion_entry", values)
        if (
            previous_deviation_bps < self._config.exit_deviation_bps
            and deviation_bps >= self._config.exit_deviation_bps
        ):
            return self._signal(snapshot, SignalDirection.SELL, "mean_reversion_exit", values)
        return self._signal(snapshot, SignalDirection.HOLD, "mean_reversion_wait", values)
