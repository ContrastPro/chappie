"""Long-only momentum research with an explicit transaction-cost hurdle."""

from dataclasses import dataclass
from decimal import Decimal

from app.strategies.common import PureStrategy, StrategyIdentity, parameters
from app.strategies.ema_crossover import calculate_ema
from app.strategies.indicators import average_true_range
from app.trading.exceptions import InvalidStrategyConfigurationError
from app.trading.models import MarketSnapshot, Signal, SignalDirection
from app.trading.strategy import SignalIdFactory


@dataclass(frozen=True, slots=True)
class CostAwareMomentumConfig:
    fast_window: int = 8
    slow_window: int = 21
    atr_window: int = 14
    minimum_strength_bps: Decimal = Decimal(35)
    round_trip_cost_bps: Decimal = Decimal(20)
    safety_margin_bps: Decimal = Decimal(15)
    exit_strength_bps: Decimal = Decimal(5)
    strategy_name: str = "cost_aware_momentum"
    strategy_version: str = "1.0.0"

    def __post_init__(self) -> None:
        if self.fast_window < 2 or self.slow_window <= self.fast_window or self.atr_window < 2:
            raise InvalidStrategyConfigurationError("cost-aware momentum windows are invalid")
        if any(
            not isinstance(value, Decimal) or not value.is_finite() or value < 0
            for value in (
                self.minimum_strength_bps,
                self.round_trip_cost_bps,
                self.safety_margin_bps,
                self.exit_strength_bps,
            )
        ):
            raise InvalidStrategyConfigurationError("cost-aware momentum thresholds are invalid")


class CostAwareMomentumStrategy(PureStrategy):
    """Pure signal strategy.  ``expected_move_bps`` is a hurdle proxy, not a forecast."""

    def __init__(self, config: CostAwareMomentumConfig, signal_id_factory: SignalIdFactory) -> None:
        self._config, self._signal_id_factory = config, signal_id_factory
        self._identity = StrategyIdentity(config.strategy_name, config.strategy_version)
        self._configuration = parameters(
            fast_window=config.fast_window,
            slow_window=config.slow_window,
            atr_window=config.atr_window,
            minimum_strength_bps=config.minimum_strength_bps,
            round_trip_cost_bps=config.round_trip_cost_bps,
            safety_margin_bps=config.safety_margin_bps,
            exit_strength_bps=config.exit_strength_bps,
        )

    @property
    def required_history(self) -> int:
        return max(self._config.slow_window + 1, self._config.atr_window + 1)

    def generate_signal(self, snapshot: MarketSnapshot) -> Signal:
        if snapshot.latest_closed_candle is None or snapshot.is_stale:
            return self._signal(snapshot, SignalDirection.HOLD, "stale_or_empty", {})
        if len(snapshot.closed_candles) < self.required_history:
            return self._signal(snapshot, SignalDirection.HOLD, "insufficient_history", {})
        candles = snapshot.closed_candles
        closes = tuple(c.close for c in candles)
        fast, slow = (
            calculate_ema(closes, self._config.fast_window),
            calculate_ema(closes, self._config.slow_window),
        )
        price = closes[-1]
        spread_bps = (fast[-1] - slow[-1]) / price * Decimal(10_000)
        slope_bps = (fast[-1] - fast[-2]) / price * Decimal(10_000)
        atr = average_true_range(
            tuple(c.high for c in candles),
            tuple(c.low for c in candles),
            closes,
            self._config.atr_window,
        )
        # Conservative, transparent proxy: trend separation plus current range.
        expected_move_bps = max(Decimal(0), spread_bps) + (atr / price * Decimal(10_000))
        hurdle_bps = self._config.round_trip_cost_bps + self._config.safety_margin_bps
        values = {
            "spread_bps": spread_bps,
            "slope_bps": slope_bps,
            "expected_move_bps": expected_move_bps,
            "cost_hurdle_bps": hurdle_bps,
        }
        if (
            fast[-2] <= slow[-2]
            and fast[-1] > slow[-1]
            and slope_bps >= self._config.minimum_strength_bps
            and expected_move_bps > hurdle_bps
        ):
            return self._signal(snapshot, SignalDirection.BUY, "trend_confirmed_entry", values)
        if spread_bps <= self._config.exit_strength_bps or slope_bps < 0:
            return self._signal(snapshot, SignalDirection.SELL, "trend_confirmed_exit", values)
        return self._signal(snapshot, SignalDirection.HOLD, "cost_hurdle_or_momentum_wait", values)
