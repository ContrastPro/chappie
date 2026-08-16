"""Research-only multi-timeframe trend adapter derived from one closed candle stream."""

from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise

from app.strategies.common import PureStrategy, StrategyIdentity, parameters
from app.strategies.ema_crossover import calculate_ema
from app.trading.exceptions import InvalidStrategyConfigurationError
from app.trading.models import MarketSnapshot, Signal, SignalDirection
from app.trading.strategy import SignalIdFactory


@dataclass(frozen=True, slots=True)
class MultiTimeframeTrendConfig:
    lower_bars_per_medium: int = 3
    lower_bars_per_higher: int = 12
    fast_window: int = 3
    slow_window: int = 5
    round_trip_cost_bps: Decimal = Decimal(20)
    safety_margin_bps: Decimal = Decimal(15)
    strategy_name: str = "multi_timeframe_trend"
    strategy_version: str = "1.0.0"

    def __post_init__(self) -> None:
        if (
            self.lower_bars_per_medium < 2
            or self.lower_bars_per_higher <= self.lower_bars_per_medium
        ):
            raise InvalidStrategyConfigurationError("multi-timeframe aggregation is invalid")
        if self.fast_window < 2 or self.slow_window <= self.fast_window:
            raise InvalidStrategyConfigurationError("multi-timeframe EMA windows are invalid")
        if any(
            not isinstance(value, Decimal) or not value.is_finite() or value < 0
            for value in (self.round_trip_cost_bps, self.safety_margin_bps)
        ):
            raise InvalidStrategyConfigurationError("multi-timeframe cost hurdle is invalid")


def _sampled_closes(closes: tuple[Decimal, ...], multiple: int) -> tuple[Decimal, ...]:
    """Use only completed aligned groups; never blend an incomplete higher candle."""
    usable = len(closes) - (len(closes) % multiple)
    return tuple(closes[index - 1] for index in range(multiple, usable + 1, multiple))


def _is_synchronized(snapshot: MarketSnapshot) -> bool:
    """Reject gapped lower-stream input rather than inventing higher-timeframe bars."""
    candles = snapshot.closed_candles
    if len(candles) < 2:
        return True
    interval_ms = candles[0].close_time_ms - candles[0].open_time_ms + 1
    return all(
        candle.close_time_ms - candle.open_time_ms + 1 == interval_ms
        and candle.open_time_ms == previous.close_time_ms + 1
        for previous, candle in pairwise(candles)
    )


class MultiTimeframeTrendStrategy(PureStrategy):
    def __init__(
        self, config: MultiTimeframeTrendConfig, signal_id_factory: SignalIdFactory
    ) -> None:
        self._config, self._signal_id_factory = config, signal_id_factory
        self._identity = StrategyIdentity(config.strategy_name, config.strategy_version)
        self._configuration = parameters(
            lower_bars_per_medium=config.lower_bars_per_medium,
            lower_bars_per_higher=config.lower_bars_per_higher,
            fast_window=config.fast_window,
            slow_window=config.slow_window,
            round_trip_cost_bps=config.round_trip_cost_bps,
            safety_margin_bps=config.safety_margin_bps,
        )

    @property
    def required_history(self) -> int:
        return self._config.lower_bars_per_higher * (self._config.slow_window + 1)

    def generate_signal(self, snapshot: MarketSnapshot) -> Signal:
        if snapshot.latest_closed_candle is None or snapshot.is_stale:
            return self._signal(snapshot, SignalDirection.HOLD, "stale_or_empty", {})
        if len(snapshot.closed_candles) < self.required_history:
            return self._signal(snapshot, SignalDirection.HOLD, "insufficient_history", {})
        if not _is_synchronized(snapshot):
            return self._signal(
                snapshot, SignalDirection.HOLD, "unsynchronized_lower_timeframe", {}
            )
        closes = tuple(c.close for c in snapshot.closed_candles)
        medium = _sampled_closes(closes, self._config.lower_bars_per_medium)
        higher = _sampled_closes(closes, self._config.lower_bars_per_higher)
        lower_fast, lower_slow = (
            calculate_ema(closes, self._config.fast_window),
            calculate_ema(closes, self._config.slow_window),
        )
        medium_fast, medium_slow = (
            calculate_ema(medium, self._config.fast_window),
            calculate_ema(medium, self._config.slow_window),
        )
        higher_fast, higher_slow = (
            calculate_ema(higher, self._config.fast_window),
            calculate_ema(higher, self._config.slow_window),
        )
        price = closes[-1]
        lower_spread_bps = (lower_fast[-1] - lower_slow[-1]) / price * Decimal(10_000)
        hurdle_bps = self._config.round_trip_cost_bps + self._config.safety_margin_bps
        higher_long = higher_fast[-1] > higher_slow[-1]
        medium_long = medium_fast[-1] > medium_slow[-1]
        lower_entry = lower_fast[-2] <= lower_slow[-2] and lower_fast[-1] > lower_slow[-1]
        values = {
            "higher_confirmed": str(higher_long).lower(),
            "medium_confirmed": str(medium_long).lower(),
            "lower_entry": str(lower_entry).lower(),
            "lower_spread_bps": lower_spread_bps,
            "cost_hurdle_bps": hurdle_bps,
        }
        if higher_long and medium_long and lower_entry and lower_spread_bps > hurdle_bps:
            return self._signal(snapshot, SignalDirection.BUY, "trend_confirmed_entry", values)
        if not higher_long or not medium_long:
            return self._signal(snapshot, SignalDirection.SELL, "trend_confirmed_exit", values)
        return self._signal(snapshot, SignalDirection.HOLD, "timeframe_confirmation_wait", values)
