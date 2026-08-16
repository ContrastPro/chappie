"""Conservative EMA crossover with volatility and separation confirmation."""

from dataclasses import dataclass
from decimal import Decimal

from app.strategies.common import PureStrategy, StrategyIdentity, parameters
from app.strategies.ema_crossover import calculate_ema
from app.strategies.indicators import average_true_range
from app.trading.exceptions import InvalidStrategyConfigurationError, StrategyInvariantError
from app.trading.models import MarketSnapshot, Signal, SignalDirection
from app.trading.strategy import SignalIdFactory


@dataclass(frozen=True, slots=True)
class TrendFilteredConfig:
    fast_window: int = 12
    slow_window: int = 26
    atr_window: int = 14
    minimum_spread_bps: Decimal = Decimal(8)
    minimum_atr_bps: Decimal = Decimal(5)
    strategy_name: str = "trend_filtered"
    strategy_version: str = "1.0.0"

    def __post_init__(self) -> None:
        if self.fast_window < 2 or self.slow_window <= self.fast_window or self.atr_window < 2:
            raise InvalidStrategyConfigurationError("trend windows are invalid")
        if any(
            not isinstance(value, Decimal) or not value.is_finite() or value < 0
            for value in (self.minimum_spread_bps, self.minimum_atr_bps)
        ):
            raise InvalidStrategyConfigurationError(
                "trend thresholds must be finite and non-negative"
            )


class TrendFilteredStrategy(PureStrategy):
    def __init__(self, config: TrendFilteredConfig, signal_id_factory: SignalIdFactory) -> None:
        self._config = config
        self._signal_id_factory = signal_id_factory
        self._identity = StrategyIdentity(config.strategy_name, config.strategy_version)
        self._configuration = parameters(
            fast_window=config.fast_window,
            slow_window=config.slow_window,
            atr_window=config.atr_window,
            minimum_spread_bps=config.minimum_spread_bps,
            minimum_atr_bps=config.minimum_atr_bps,
        )

    @property
    def required_history(self) -> int:
        return max(self._config.slow_window + 1, self._config.atr_window + 1)

    def generate_signal(self, snapshot: MarketSnapshot) -> Signal:
        if snapshot.latest_closed_candle is None:
            return self._signal(snapshot, SignalDirection.HOLD, "no_closed_candles", {})
        if snapshot.is_stale:
            return self._signal(snapshot, SignalDirection.HOLD, "stale_snapshot", {})
        if len(snapshot.closed_candles) < self.required_history:
            return self._signal(snapshot, SignalDirection.HOLD, "insufficient_history", {})
        candles = snapshot.closed_candles
        closes = tuple(candle.close for candle in candles)
        fast, slow = (
            calculate_ema(closes, self._config.fast_window),
            calculate_ema(closes, self._config.slow_window),
        )
        price = closes[-1]
        if price <= 0:
            raise StrategyInvariantError("close must be positive")
        atr = average_true_range(
            tuple(c.high for c in candles),
            tuple(c.low for c in candles),
            closes,
            self._config.atr_window,
        )
        spread_bps = abs(fast[-1] - slow[-1]) / price * Decimal(10_000)
        atr_bps = atr / price * Decimal(10_000)
        values = {
            "fast_ema": fast[-1],
            "slow_ema": slow[-1],
            "spread_bps": spread_bps,
            "atr_bps": atr_bps,
        }
        crossed_up = fast[-2] <= slow[-2] and fast[-1] > slow[-1]
        crossed_down = fast[-2] >= slow[-2] and fast[-1] < slow[-1]
        confirmed = (
            spread_bps >= self._config.minimum_spread_bps
            and atr_bps >= self._config.minimum_atr_bps
        )
        if crossed_up and confirmed:
            return self._signal(snapshot, SignalDirection.BUY, "trend_confirmed_entry", values)
        if crossed_down and confirmed:
            return self._signal(snapshot, SignalDirection.SELL, "trend_confirmed_exit", values)
        return self._signal(snapshot, SignalDirection.HOLD, "trend_filter_or_hysteresis", values)
