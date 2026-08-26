import json
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from hashlib import sha256
from itertools import pairwise

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
EMA_STATE_IDENTITY = "chappie_ema_crossover_state/1.0.0"
EMA_STATE_SEMANTICS = "ema-crossover-state-semantics/1.0.0"


def _canonical_digest(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class EmaCrossoverStateV1:
    """Exact post-candle EMA state for a bound research continuation."""

    identity: str
    semantics_identity: str
    strategy_identity: str
    strategy_config_fingerprint: str
    fast_window: int
    slow_window: int
    symbol: str
    interval: str
    dataset_digest: str
    boundary_index: int
    processed_count: int
    last_processed_timestamp: int
    fast_ema: Decimal
    slow_ema: Decimal
    state_digest: str

    def payload(self) -> dict[str, object]:
        return {
            "identity": self.identity,
            "semantics_identity": self.semantics_identity,
            "strategy_identity": self.strategy_identity,
            "strategy_config_fingerprint": self.strategy_config_fingerprint,
            "fast_window": self.fast_window,
            "slow_window": self.slow_window,
            "symbol": self.symbol,
            "interval": self.interval,
            "dataset_digest": self.dataset_digest,
            "boundary_index": self.boundary_index,
            "processed_count": self.processed_count,
            "last_processed_timestamp": self.last_processed_timestamp,
            "fast_ema": str(self.fast_ema),
            "slow_ema": str(self.slow_ema),
        }

    def serialized(self) -> dict[str, object]:
        return {**self.payload(), "state_digest": self.state_digest}

    @classmethod
    def from_serialized(cls, value: object) -> "EmaCrossoverStateV1":
        if not isinstance(value, dict):
            raise StrategyInvariantError("EMA state payload is invalid")
        expected = {
            "identity",
            "semantics_identity",
            "strategy_identity",
            "strategy_config_fingerprint",
            "fast_window",
            "slow_window",
            "symbol",
            "interval",
            "dataset_digest",
            "boundary_index",
            "processed_count",
            "last_processed_timestamp",
            "fast_ema",
            "slow_ema",
            "state_digest",
        }
        if set(value) != expected:
            raise StrategyInvariantError("EMA state payload shape is invalid")
        try:
            state = cls(
                identity=value["identity"],
                semantics_identity=value["semantics_identity"],
                strategy_identity=value["strategy_identity"],
                strategy_config_fingerprint=value["strategy_config_fingerprint"],
                fast_window=value["fast_window"],
                slow_window=value["slow_window"],
                symbol=value["symbol"],
                interval=value["interval"],
                dataset_digest=value["dataset_digest"],
                boundary_index=value["boundary_index"],
                processed_count=value["processed_count"],
                last_processed_timestamp=value["last_processed_timestamp"],
                fast_ema=Decimal(value["fast_ema"]),
                slow_ema=Decimal(value["slow_ema"]),
                state_digest=value["state_digest"],
            )
        except (InvalidOperation, TypeError, ValueError) as error:
            raise StrategyInvariantError("EMA state payload values are invalid") from error
        if state.state_digest != _canonical_digest(state.payload()):
            raise StrategyInvariantError("EMA state digest is invalid")
        return state


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

    def _config_fingerprint(self) -> str:
        return _canonical_digest(
            {
                "strategy_identity": f"{self.name}/{self.version}",
                "fast_window": self._config.fast_window,
                "slow_window": self._config.slow_window,
            }
        )

    def create_state(
        self,
        candles: Sequence[object],
        *,
        symbol: str,
        interval: str,
        dataset_digest: str,
        boundary_index: int,
    ) -> EmaCrossoverStateV1:
        """Create exact EMA state after the supplied historical prefix."""
        if len(candles) < self._config.slow_window:
            raise StrategyInvariantError("insufficient candles for EMA state")
        if boundary_index != len(candles) or boundary_index < self._config.slow_window:
            raise StrategyInvariantError("EMA state boundary is invalid")
        if not all(
            isinstance(value, str) and value for value in (symbol, interval, dataset_digest)
        ):
            raise StrategyInvariantError("EMA state binding is invalid")
        closes = tuple(getattr(candle, "close", None) for candle in candles)
        timestamps = tuple(getattr(candle, "close_time_ms", None) for candle in candles)
        if (
            any(not isinstance(value, Decimal) or not value.is_finite() for value in closes)
            or any(not isinstance(value, int) for value in timestamps)
            or any(right <= left for left, right in pairwise(timestamps))
        ):
            raise StrategyInvariantError("EMA state candle history is invalid")
        fast = calculate_ema(closes, self._config.fast_window)[-1]
        slow = calculate_ema(closes, self._config.slow_window)[-1]
        payload = {
            "identity": EMA_STATE_IDENTITY,
            "semantics_identity": EMA_STATE_SEMANTICS,
            "strategy_identity": f"{self.name}/{self.version}",
            "strategy_config_fingerprint": self._config_fingerprint(),
            "fast_window": self._config.fast_window,
            "slow_window": self._config.slow_window,
            "symbol": symbol,
            "interval": interval,
            "dataset_digest": dataset_digest,
            "boundary_index": boundary_index,
            "processed_count": len(candles),
            "last_processed_timestamp": timestamps[-1],
            "fast_ema": str(fast),
            "slow_ema": str(slow),
        }
        return EmaCrossoverStateV1(
            **{
                **payload,
                "fast_ema": fast,
                "slow_ema": slow,
                "state_digest": _canonical_digest(payload),
            }
        )

    def _validate_state(
        self,
        state: EmaCrossoverStateV1,
        *,
        symbol: str,
        interval: str,
        dataset_digest: str,
        boundary_index: int,
    ) -> None:
        if not isinstance(state, EmaCrossoverStateV1) or state.state_digest != _canonical_digest(
            state.payload()
        ):
            raise StrategyInvariantError("EMA state digest is invalid")
        if (
            state.identity != EMA_STATE_IDENTITY
            or state.semantics_identity != EMA_STATE_SEMANTICS
            or state.strategy_identity != f"{self.name}/{self.version}"
            or state.strategy_config_fingerprint != self._config_fingerprint()
            or state.fast_window != self._config.fast_window
            or state.slow_window != self._config.slow_window
            or state.symbol != symbol
            or state.interval != interval
            or state.dataset_digest != dataset_digest
            or state.boundary_index != boundary_index
            or state.processed_count < self._config.slow_window
            or not state.fast_ema.is_finite()
            or not state.slow_ema.is_finite()
        ):
            raise StrategyInvariantError("EMA state binding is invalid")

    def continue_state(
        self,
        state: EmaCrossoverStateV1,
        candle: object,
        *,
        symbol: str,
        interval: str,
        dataset_digest: str,
        boundary_index: int,
    ) -> tuple[SignalDirection, str, EmaCrossoverStateV1]:
        """Advance a validated state with one candle using existing EMA arithmetic."""
        self._validate_state(
            state,
            symbol=symbol,
            interval=interval,
            dataset_digest=dataset_digest,
            boundary_index=boundary_index,
        )
        close = getattr(candle, "close", None)
        timestamp = getattr(candle, "close_time_ms", None)
        if (
            not isinstance(close, Decimal)
            or not close.is_finite()
            or not isinstance(timestamp, int)
        ):
            raise StrategyInvariantError("EMA continuation candle is invalid")
        if timestamp <= state.last_processed_timestamp:
            raise StrategyInvariantError("EMA continuation candle ordering is invalid")
        with localcontext() as context:
            context.prec = EMA_DECIMAL_PRECISION
            fast_alpha = Decimal(2) / Decimal(self._config.fast_window + 1)
            slow_alpha = Decimal(2) / Decimal(self._config.slow_window + 1)
            fast = close * fast_alpha + state.fast_ema * (Decimal(1) - fast_alpha)
            slow = close * slow_alpha + state.slow_ema * (Decimal(1) - slow_alpha)
        if state.fast_ema <= state.slow_ema and fast > slow:
            direction, reason = SignalDirection.BUY, "bullish_crossover"
        elif state.fast_ema >= state.slow_ema and fast < slow:
            direction, reason = SignalDirection.SELL, "bearish_crossover"
        elif fast == slow:
            direction, reason = SignalDirection.HOLD, "equal_emas"
        else:
            direction, reason = SignalDirection.HOLD, "no_crossover"
        payload = {
            **state.payload(),
            "processed_count": state.processed_count + 1,
            "last_processed_timestamp": timestamp,
            "fast_ema": str(fast),
            "slow_ema": str(slow),
        }
        return (
            direction,
            reason,
            EmaCrossoverStateV1(
                **{
                    **payload,
                    "fast_ema": fast,
                    "slow_ema": slow,
                    "state_digest": _canonical_digest(payload),
                }
            ),
        )

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
