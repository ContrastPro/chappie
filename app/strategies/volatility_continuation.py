"""Causal, cost-aware medium-horizon continuation research strategy."""

from dataclasses import asdict, dataclass
from decimal import Decimal

from app.strategies.common import PureStrategy, StrategyIdentity, parameters
from app.strategies.ema_crossover import calculate_ema
from app.strategies.indicators import average_true_range
from app.strategies.research_timeframes import completed_groups, synchronized
from app.trading.exceptions import InvalidStrategyConfigurationError
from app.trading.models import MarketSnapshot, Signal, SignalDirection
from app.trading.strategy import SignalIdFactory


@dataclass(frozen=True, slots=True)
class VolatilityContinuationConfig:
    lower_bars_per_medium: int = 3
    lower_bars_per_higher: int = 12
    fast_window: int = 4
    slow_window: int = 8
    atr_window: int = 5
    minimum_volatility_bps: Decimal = Decimal(60)
    maximum_volatility_bps: Decimal = Decimal(500)
    cost_hurdle_bps: Decimal = Decimal(80)
    stop_atr_multiple: Decimal = Decimal("2.0")
    max_hold_bars: int = 288
    strategy_name: str = "volatility_continuation"
    strategy_version: str = "1.0.0"

    def __post_init__(self) -> None:
        if (
            self.lower_bars_per_medium < 2
            or self.lower_bars_per_higher <= self.lower_bars_per_medium
            or self.fast_window < 2
            or self.slow_window <= self.fast_window
            or self.atr_window < 2
            or self.max_hold_bars < 1
        ):
            raise InvalidStrategyConfigurationError("volatility continuation windows are invalid")
        if (
            any(
                not isinstance(value, Decimal) or not value.is_finite() or value < 0
                for value in (
                    self.minimum_volatility_bps,
                    self.maximum_volatility_bps,
                    self.cost_hurdle_bps,
                    self.stop_atr_multiple,
                )
            )
            or self.maximum_volatility_bps < self.minimum_volatility_bps
        ):
            raise InvalidStrategyConfigurationError(
                "volatility continuation thresholds are invalid"
            )


class VolatilityContinuationStrategy(PureStrategy):
    def __init__(
        self, config: VolatilityContinuationConfig, signal_id_factory: SignalIdFactory
    ) -> None:
        self._config, self._signal_id_factory = config, signal_id_factory
        self._identity = StrategyIdentity(config.strategy_name, config.strategy_version)
        self._configuration = parameters(**asdict(config))

    @property
    def required_history(self) -> int:
        return self._config.lower_bars_per_higher * (self._config.slow_window + 1)

    def generate_signal(self, snapshot: MarketSnapshot) -> Signal:
        if snapshot.latest_closed_candle is None or snapshot.is_stale:
            return self._signal(snapshot, SignalDirection.HOLD, "stale_or_empty", {})
        candles = snapshot.closed_candles
        if len(candles) < self.required_history:
            return self._signal(snapshot, SignalDirection.HOLD, "insufficient_history", {})
        if not synchronized(candles):
            return self._signal(
                snapshot, SignalDirection.HOLD, "unsynchronized_lower_timeframe", {}
            )
        medium = completed_groups(candles, self._config.lower_bars_per_medium)
        higher = completed_groups(candles, self._config.lower_bars_per_higher)
        if len(medium) < self._config.atr_window + 1 or len(higher) < self._config.slow_window + 1:
            return self._signal(snapshot, SignalDirection.HOLD, "incomplete_higher_timeframe", {})
        high_closes = tuple(c.close for c in higher)
        medium_closes = tuple(c.close for c in medium)
        fast, slow = (
            calculate_ema(high_closes, self._config.fast_window),
            calculate_ema(high_closes, self._config.slow_window),
        )
        atr = average_true_range(
            tuple(c.high for c in medium),
            tuple(c.low for c in medium),
            medium_closes,
            self._config.atr_window,
        )
        price = medium_closes[-1]
        volatility_bps = atr / price * Decimal(10_000)
        spread_bps = (fast[-1] - slow[-1]) / price * Decimal(10_000)
        slope_bps = (fast[-1] - fast[-2]) / price * Decimal(10_000)
        # Edge-trigger the resumption so a persistent LONG trend does not emit
        # repeated entries on every completed medium candle.
        continuation = (
            medium_closes[-3] <= medium_closes[-4]
            and medium_closes[-2] > medium_closes[-3]
            and medium_closes[-1] > medium_closes[-2]
        )
        opportunity_bps = max(Decimal(0), spread_bps) + volatility_bps
        bullish = fast[-1] > slow[-1] and slope_bps > 0
        volatility_ok = (
            self._config.minimum_volatility_bps
            <= volatility_bps
            <= self._config.maximum_volatility_bps
        )
        values = {
            "higher_bullish": str(bullish).lower(),
            "volatility_bps": volatility_bps,
            "opportunity_proxy_bps": opportunity_bps,
            "cost_hurdle_bps": self._config.cost_hurdle_bps,
            "diag_bullish_context": str(bullish).lower(),
            "diag_volatility_qualified": str(volatility_ok).lower(),
            "diag_continuation_confirmed": str(continuation).lower(),
            "diag_cost_hurdle_reject": str(opportunity_bps < self._config.cost_hurdle_bps).lower(),
        }
        if (
            bullish
            and volatility_ok
            and continuation
            and opportunity_bps >= self._config.cost_hurdle_bps
        ):
            return self._signal(snapshot, SignalDirection.BUY, "trend_confirmed_entry", values)
        if (
            not bullish
            or medium_closes[-1] < medium_closes[-2] - atr * self._config.stop_atr_multiple
        ):
            return self._signal(snapshot, SignalDirection.SELL, "trend_confirmed_exit", values)
        return self._signal(snapshot, SignalDirection.HOLD, "volatility_continuation_wait", values)
