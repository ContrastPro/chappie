"""Generic retracement/reclaim continuation research strategy; no Fibonacci levels."""

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
class TrendRetracementContinuationConfig:
    lower_bars_per_medium: int = 3
    lower_bars_per_higher: int = 12
    fast_window: int = 4
    slow_window: int = 8
    pullback_window: int = 6
    atr_window: int = 5
    minimum_retracement_atr: Decimal = Decimal("0.5")
    maximum_retracement_atr: Decimal = Decimal("3.0")
    cost_hurdle_bps: Decimal = Decimal(80)
    max_hold_bars: int = 144
    strategy_name: str = "trend_retracement_continuation"
    strategy_version: str = "1.0.0"

    def __post_init__(self) -> None:
        if (
            self.lower_bars_per_medium < 2
            or self.lower_bars_per_higher <= self.lower_bars_per_medium
            or self.fast_window < 2
            or self.slow_window <= self.fast_window
            or self.pullback_window < 3
            or self.atr_window < 2
            or self.max_hold_bars < 1
        ):
            raise InvalidStrategyConfigurationError("trend retracement windows are invalid")
        if (
            any(
                not isinstance(value, Decimal) or not value.is_finite() or value < 0
                for value in (
                    self.minimum_retracement_atr,
                    self.maximum_retracement_atr,
                    self.cost_hurdle_bps,
                )
            )
            or self.maximum_retracement_atr < self.minimum_retracement_atr
        ):
            raise InvalidStrategyConfigurationError("trend retracement thresholds are invalid")


class TrendRetracementContinuationStrategy(PureStrategy):
    def __init__(
        self, config: TrendRetracementContinuationConfig, signal_id_factory: SignalIdFactory
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
        if len(candles) < self.required_history or not synchronized(candles):
            return self._signal(
                snapshot, SignalDirection.HOLD, "insufficient_or_unsynchronized_history", {}
            )
        medium, higher = (
            completed_groups(candles, self._config.lower_bars_per_medium),
            completed_groups(candles, self._config.lower_bars_per_higher),
        )
        if (
            len(medium) < max(self._config.pullback_window, self._config.atr_window + 1)
            or len(higher) < self._config.slow_window + 1
        ):
            return self._signal(snapshot, SignalDirection.HOLD, "incomplete_higher_timeframe", {})
        high_closes, medium_closes = tuple(c.close for c in higher), tuple(c.close for c in medium)
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
        bullish = fast[-1] > slow[-1] and fast[-1] > fast[-2]
        prior_peak = max(medium_closes[-self._config.pullback_window : -1])
        depth_atr = (
            (prior_peak - min(medium_closes[-self._config.pullback_window :])) / atr
            if atr
            else Decimal(0)
        )
        intact = price >= slow[-1]
        reclaim = (
            min(medium_closes[-self._config.pullback_window : -1]) < medium_closes[-2]
            and price > medium_closes[-2]
        )
        opportunity_bps = (
            (prior_peak - min(medium_closes[-self._config.pullback_window :]))
            / price
            * Decimal(10_000)
        )
        depth_ok = (
            self._config.minimum_retracement_atr
            <= depth_atr
            <= self._config.maximum_retracement_atr
        )
        values = {
            "higher_bullish": str(bullish).lower(),
            "retracement_depth_atr": depth_atr,
            "opportunity_proxy_bps": opportunity_bps,
            "cost_hurdle_bps": self._config.cost_hurdle_bps,
            "diag_valid_trend": str(bullish).lower(),
            "diag_pullback_observed": str(depth_atr > 0).lower(),
            "diag_depth_medium": str(depth_ok).lower(),
            "diag_reclaim": str(reclaim).lower(),
            "diag_cost_hurdle_reject": str(opportunity_bps < self._config.cost_hurdle_bps).lower(),
        }
        if (
            bullish
            and intact
            and depth_ok
            and reclaim
            and opportunity_bps >= self._config.cost_hurdle_bps
        ):
            return self._signal(snapshot, SignalDirection.BUY, "trend_confirmed_entry", values)
        if not bullish or not intact or price < min(medium_closes[-3:]):
            return self._signal(snapshot, SignalDirection.SELL, "trend_confirmed_exit", values)
        return self._signal(snapshot, SignalDirection.HOLD, "trend_retracement_wait", values)
