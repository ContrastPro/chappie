from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext

from app.trading.exceptions import (
    InvalidDecimalValueError,
    InvalidStrategyConfigurationError,
    StrategyInvariantError,
)
from app.trading.models import (
    ImmutableMapping,
    MarketSnapshot,
    Signal,
    SignalDirection,
    freeze_mapping,
)
from app.trading.strategy import SignalIdFactory

EMA_DECIMAL_PRECISION = 28


@dataclass(frozen=True, slots=True)
class EmaCrossoverConfig:
    fast_window: int = 12
    slow_window: int = 26
    strategy_name: str = "ema_crossover"
    strategy_version: str = "1.0.0"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.fast_window, int)
            or isinstance(self.fast_window, bool)
            or self.fast_window < 2
        ):
            raise InvalidStrategyConfigurationError("fast_window must be at least 2")
        if (
            not isinstance(self.slow_window, int)
            or isinstance(self.slow_window, bool)
            or self.slow_window <= self.fast_window
        ):
            raise InvalidStrategyConfigurationError("slow_window must be greater than fast_window")
        if not self.strategy_name.strip() or not self.strategy_version.strip():
            raise InvalidStrategyConfigurationError("strategy name and version must not be empty")


def calculate_ema(values: Sequence[Decimal], window: int) -> tuple[Decimal, ...]:
    """Return EMA values starting at the SMA-seeded index ``window - 1``."""

    if not isinstance(window, int) or isinstance(window, bool) or window < 2:
        raise InvalidStrategyConfigurationError("EMA window must be at least 2")
    prices = tuple(values)
    if len(prices) < window:
        raise StrategyInvariantError("Not enough values to calculate EMA")
    if any(not isinstance(value, Decimal) or not value.is_finite() for value in prices):
        raise InvalidDecimalValueError("EMA values must be finite Decimal values")

    with localcontext() as context:
        context.prec = EMA_DECIMAL_PRECISION
        initial = sum(prices[:window], Decimal(0)) / Decimal(window)
        alpha = Decimal(2) / Decimal(window + 1)
        result = [initial]
        current = initial
        for price in prices[window:]:
            current = price * alpha + current * (Decimal(1) - alpha)
            result.append(current)
    return tuple(result)


class EmaCrossoverStrategy:
    def __init__(
        self,
        config: EmaCrossoverConfig,
        signal_id_factory: SignalIdFactory,
    ) -> None:
        self._config = config
        self._signal_id_factory = signal_id_factory
        self._configuration = freeze_mapping(
            {
                "fast_window": config.fast_window,
                "slow_window": config.slow_window,
            }
        )

    @property
    def name(self) -> str:
        return self._config.strategy_name

    @property
    def version(self) -> str:
        return self._config.strategy_version

    @property
    def required_history(self) -> int:
        return self._config.slow_window + 1

    @property
    def configuration(self) -> ImmutableMapping:
        return self._configuration

    def generate_signal(self, snapshot: MarketSnapshot) -> Signal:
        if not isinstance(snapshot, MarketSnapshot):
            raise StrategyInvariantError("snapshot must be a MarketSnapshot")
        if snapshot.latest_closed_candle is None:
            return self._signal(snapshot, SignalDirection.HOLD, "no_closed_candles", {})
        if snapshot.is_stale:
            return self._signal(snapshot, SignalDirection.HOLD, "stale_snapshot", {})
        if len(snapshot.closed_candles) < self.required_history:
            return self._signal(
                snapshot,
                SignalDirection.HOLD,
                "insufficient_history",
                {"available": len(snapshot.closed_candles), "required": self.required_history},
            )

        closes = tuple(candle.close for candle in snapshot.closed_candles)
        fast = calculate_ema(closes, self._config.fast_window)
        slow = calculate_ema(closes, self._config.slow_window)
        previous_fast = fast[-2]
        current_fast = fast[-1]
        previous_slow = slow[-2]
        current_slow = slow[-1]
        values = {
            "previous_fast": previous_fast,
            "previous_slow": previous_slow,
            "current_fast": current_fast,
            "current_slow": current_slow,
        }
        if previous_fast <= previous_slow and current_fast > current_slow:
            direction = SignalDirection.BUY
            reason = "bullish_crossover"
        elif previous_fast >= previous_slow and current_fast < current_slow:
            direction = SignalDirection.SELL
            reason = "bearish_crossover"
        elif current_fast == current_slow:
            direction = SignalDirection.HOLD
            reason = "equal_emas"
        else:
            direction = SignalDirection.HOLD
            reason = "no_crossover"
        return self._signal(snapshot, direction, reason, values)

    def _signal(
        self,
        snapshot: MarketSnapshot,
        direction: SignalDirection,
        reason: str,
        values: dict[str, int | Decimal],
    ) -> Signal:
        details = " ".join(f"{key}={value}" for key, value in values.items())
        explanation = (
            f"reason={reason} fast_window={self._config.fast_window} "
            f"slow_window={self._config.slow_window}"
        )
        if details:
            explanation = f"{explanation} {details}"
        return Signal(
            signal_id=self._signal_id_factory.next_id(),
            snapshot_id=snapshot.snapshot_id,
            stream_key=snapshot.stream_key,
            created_at_ms=snapshot.as_of_ms,
            effective_at_ms=snapshot.as_of_ms,
            direction=direction,
            reason_code=reason,
            explanation=explanation,
            strategy_name=self.name,
            strategy_version=self.version,
            strategy_parameters=self.configuration,
            correlation_id=snapshot.correlation_cycle_id,
            metadata=values,
        )
