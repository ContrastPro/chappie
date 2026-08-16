"""Causal compression-to-expansion, volume-confirmed breakout research strategy."""

from dataclasses import asdict, dataclass
from decimal import Decimal

from app.strategies.common import PureStrategy, StrategyIdentity, parameters
from app.strategies.ema_crossover import calculate_ema
from app.strategies.indicators import average_true_range, rolling_mean
from app.strategies.research_timeframes import completed_groups, synchronized
from app.trading.exceptions import InvalidStrategyConfigurationError
from app.trading.models import MarketSnapshot, Signal, SignalDirection
from app.trading.strategy import SignalIdFactory


@dataclass(frozen=True, slots=True)
class CompressionExpansionBreakoutConfig:
    lower_bars_per_medium: int = 3
    lower_bars_per_higher: int = 12
    trend_fast_window: int = 4
    trend_slow_window: int = 8
    compression_window: int = 6
    breakout_lookback: int = 8
    atr_window: int = 4
    compression_ratio: Decimal = Decimal("0.70")
    expansion_ratio: Decimal = Decimal("1.25")
    volume_ratio: Decimal = Decimal("1.50")
    cost_hurdle_bps: Decimal = Decimal(80)
    max_hold_bars: int = 144
    strategy_name: str = "compression_expansion_breakout"
    strategy_version: str = "1.0.0"

    def __post_init__(self) -> None:
        if (
            min(
                self.lower_bars_per_medium,
                self.trend_fast_window,
                self.compression_window,
                self.breakout_lookback,
                self.atr_window,
                self.max_hold_bars,
            )
            < 2
            or self.lower_bars_per_higher <= self.lower_bars_per_medium
            or self.trend_slow_window <= self.trend_fast_window
        ):
            raise InvalidStrategyConfigurationError("compression breakout windows are invalid")
        if any(
            not isinstance(value, Decimal) or not value.is_finite() or value <= 0
            for value in (
                self.compression_ratio,
                self.expansion_ratio,
                self.volume_ratio,
                self.cost_hurdle_bps,
            )
        ):
            raise InvalidStrategyConfigurationError("compression breakout thresholds are invalid")


class CompressionExpansionBreakoutStrategy(PureStrategy):
    def __init__(
        self, config: CompressionExpansionBreakoutConfig, signal_id_factory: SignalIdFactory
    ) -> None:
        self._config, self._signal_id_factory = config, signal_id_factory
        self._identity = StrategyIdentity(config.strategy_name, config.strategy_version)
        self._configuration = parameters(**asdict(config))

    @property
    def required_history(self) -> int:
        return self._config.lower_bars_per_higher * (self._config.trend_slow_window + 1)

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
        needed = max(
            self._config.breakout_lookback + 1,
            self._config.compression_window + self._config.atr_window + 1,
        )
        if len(medium) < needed or len(higher) < self._config.trend_slow_window + 1:
            return self._signal(snapshot, SignalDirection.HOLD, "incomplete_higher_timeframe", {})
        closes = tuple(c.close for c in medium)

        def atr_at(last_index: int) -> Decimal:
            # ATR is local to the requested completed 15m bar.  Avoid rebuilding
            # a growing full-history series on every replay candle.
            window = medium[last_index - self._config.atr_window : last_index + 1]
            return average_true_range(
                tuple(c.high for c in window),
                tuple(c.low for c in window),
                tuple(c.close for c in window),
                self._config.atr_window,
            )

        current_atr = atr_at(len(medium) - 1)
        historical_atrs = tuple(
            atr_at(index)
            for index in range(
                len(medium) - self._config.compression_window - 1,
                len(medium) - 1,
            )
        )
        baseline_atr = rolling_mean(
            historical_atrs, min(self._config.compression_window, len(historical_atrs))
        )
        compressed = historical_atrs[-1] <= baseline_atr * self._config.compression_ratio
        expanded = current_atr >= historical_atrs[-1] * self._config.expansion_ratio
        prior_high = max(closes[-self._config.breakout_lookback - 1 : -1])
        breakout = closes[-1] > prior_high
        volume_baseline = rolling_mean(
            tuple(c.volume for c in medium[:-1]), self._config.compression_window
        )
        volume_confirmed = medium[-1].volume >= volume_baseline * self._config.volume_ratio
        high_closes = tuple(c.close for c in higher)
        fast, slow = (
            calculate_ema(high_closes, self._config.trend_fast_window),
            calculate_ema(high_closes, self._config.trend_slow_window),
        )
        compatible = fast[-1] >= slow[-1]
        opportunity_bps = (closes[-1] - prior_high) / closes[-1] * Decimal(
            10_000
        ) + current_atr / closes[-1] * Decimal(10_000)
        values = {
            "compression": str(compressed).lower(),
            "breakout": str(breakout).lower(),
            "expansion": str(expanded).lower(),
            "volume_confirmed": str(volume_confirmed).lower(),
            "opportunity_proxy_bps": opportunity_bps,
            "cost_hurdle_bps": self._config.cost_hurdle_bps,
            "diag_compression": str(compressed).lower(),
            "diag_breakout": str(breakout).lower(),
            "diag_expansion": str(expanded).lower(),
            "diag_volume": str(volume_confirmed).lower(),
            "diag_cost_hurdle_reject": str(opportunity_bps < self._config.cost_hurdle_bps).lower(),
        }
        if (
            compatible
            and compressed
            and breakout
            and expanded
            and volume_confirmed
            and opportunity_bps >= self._config.cost_hurdle_bps
        ):
            return self._signal(snapshot, SignalDirection.BUY, "breakout_entry", values)
        if closes[-1] <= prior_high or (fast[-1] < slow[-1]):
            return self._signal(snapshot, SignalDirection.SELL, "breakout_structure_failed", values)
        return self._signal(snapshot, SignalDirection.HOLD, "compression_expansion_wait", values)
