"""Conservative EMA crossover with volatility and separation confirmation."""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from hashlib import sha256
from itertools import pairwise

from app.market.models import Candle
from app.strategies.common import PureStrategy, StrategyIdentity, parameters
from app.strategies.ema_crossover import EMA_DECIMAL_PRECISION, calculate_ema
from app.strategies.indicators import average_true_range
from app.trading.exceptions import InvalidStrategyConfigurationError, StrategyInvariantError
from app.trading.models import MarketSnapshot, Signal, SignalDirection
from app.trading.strategy import SignalIdFactory

TREND_FILTERED_STATE_IDENTITY = "chappie_trend_filtered_state/1.0.0"
TREND_FILTERED_STATE_SEMANTICS = "trend-filtered-state-semantics/1.0.0"


def _digest(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class TrendFilteredOhlcV1:
    close_time_ms: int
    high: Decimal
    low: Decimal
    close: Decimal

    def payload(self) -> dict[str, object]:
        return {
            "close_time_ms": self.close_time_ms,
            "high": str(self.high),
            "low": str(self.low),
            "close": str(self.close),
        }


@dataclass(frozen=True, slots=True)
class TrendFilteredStateV1:
    identity: str
    semantics_identity: str
    strategy_identity: str
    strategy_config_fingerprint: str
    fast_window: int
    slow_window: int
    atr_window: int
    minimum_spread_bps: Decimal
    minimum_atr_bps: Decimal
    symbol: str
    interval: str
    dataset_digest: str
    boundary_index: int
    processed_count: int
    last_processed_timestamp: int
    fast_ema: Decimal
    slow_ema: Decimal
    atr_tail: tuple[TrendFilteredOhlcV1, ...]
    state_digest: str

    def payload(self) -> dict[str, object]:
        return {
            "identity": self.identity,
            "semantics_identity": self.semantics_identity,
            "strategy_identity": self.strategy_identity,
            "strategy_config_fingerprint": self.strategy_config_fingerprint,
            "fast_window": self.fast_window,
            "slow_window": self.slow_window,
            "atr_window": self.atr_window,
            "minimum_spread_bps": str(self.minimum_spread_bps),
            "minimum_atr_bps": str(self.minimum_atr_bps),
            "symbol": self.symbol,
            "interval": self.interval,
            "dataset_digest": self.dataset_digest,
            "boundary_index": self.boundary_index,
            "processed_count": self.processed_count,
            "last_processed_timestamp": self.last_processed_timestamp,
            "fast_ema": str(self.fast_ema),
            "slow_ema": str(self.slow_ema),
            "atr_tail": [item.payload() for item in self.atr_tail],
        }

    def serialized(self) -> dict[str, object]:
        return {**self.payload(), "state_digest": self.state_digest}

    @classmethod
    def from_serialized(cls, value: object) -> "TrendFilteredStateV1":
        if not isinstance(value, dict):
            raise StrategyInvariantError("trend filtered state payload is invalid")
        expected = set(cls.__dataclass_fields__)
        if set(value) != expected or not isinstance(value.get("atr_tail"), list):
            raise StrategyInvariantError("trend filtered state payload shape is invalid")
        try:
            tail = tuple(
                TrendFilteredOhlcV1(
                    close_time_ms=item["close_time_ms"],
                    high=Decimal(item["high"]),
                    low=Decimal(item["low"]),
                    close=Decimal(item["close"]),
                )
                for item in value["atr_tail"]
            )
            state = cls(
                identity=value["identity"],
                semantics_identity=value["semantics_identity"],
                strategy_identity=value["strategy_identity"],
                strategy_config_fingerprint=value["strategy_config_fingerprint"],
                fast_window=value["fast_window"],
                slow_window=value["slow_window"],
                atr_window=value["atr_window"],
                minimum_spread_bps=Decimal(value["minimum_spread_bps"]),
                minimum_atr_bps=Decimal(value["minimum_atr_bps"]),
                symbol=value["symbol"],
                interval=value["interval"],
                dataset_digest=value["dataset_digest"],
                boundary_index=value["boundary_index"],
                processed_count=value["processed_count"],
                last_processed_timestamp=value["last_processed_timestamp"],
                fast_ema=Decimal(value["fast_ema"]),
                slow_ema=Decimal(value["slow_ema"]),
                atr_tail=tail,
                state_digest=value["state_digest"],
            )
        except (InvalidOperation, KeyError, TypeError, ValueError) as error:
            raise StrategyInvariantError(
                "trend filtered state payload values are invalid"
            ) from error
        if state.state_digest != _digest(state.payload()):
            raise StrategyInvariantError("trend filtered state digest is invalid")
        return state


@dataclass(frozen=True, slots=True)
class TrendFilteredContinuationV1:
    direction: SignalDirection
    reason_code: str
    fast_ema: Decimal
    slow_ema: Decimal
    spread_bps: Decimal
    atr_bps: Decimal
    state: TrendFilteredStateV1


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

    def _config_fingerprint(self) -> str:
        return _digest(
            {
                "strategy_identity": f"{self.name}/{self.version}",
                "fast_window": self._config.fast_window,
                "slow_window": self._config.slow_window,
                "atr_window": self._config.atr_window,
                "minimum_spread_bps": str(self._config.minimum_spread_bps),
                "minimum_atr_bps": str(self._config.minimum_atr_bps),
            }
        )

    @staticmethod
    def _ohlc(candle: Candle) -> TrendFilteredOhlcV1:
        return TrendFilteredOhlcV1(candle.close_time_ms, candle.high, candle.low, candle.close)

    def create_state(
        self,
        candles: Sequence[Candle],
        *,
        symbol: str,
        interval: str,
        dataset_digest: str,
        boundary_index: int,
    ) -> TrendFilteredStateV1:
        if len(candles) < self.required_history or boundary_index != len(candles):
            raise StrategyInvariantError("trend filtered state boundary is invalid")
        if not all(
            isinstance(value, str) and value for value in (symbol, interval, dataset_digest)
        ):
            raise StrategyInvariantError("trend filtered state binding is invalid")
        if any(right.close_time_ms <= left.close_time_ms for left, right in pairwise(candles)):
            raise StrategyInvariantError("trend filtered state candle ordering is invalid")
        closes = tuple(candle.close for candle in candles)
        if any(not value.is_finite() for value in closes):
            raise StrategyInvariantError("trend filtered state candles are invalid")
        fast = calculate_ema(closes, self._config.fast_window)[-1]
        slow = calculate_ema(closes, self._config.slow_window)[-1]
        tail = tuple(self._ohlc(candle) for candle in candles[-self._config.atr_window :])
        payload = {
            "identity": TREND_FILTERED_STATE_IDENTITY,
            "semantics_identity": TREND_FILTERED_STATE_SEMANTICS,
            "strategy_identity": f"{self.name}/{self.version}",
            "strategy_config_fingerprint": self._config_fingerprint(),
            "fast_window": self._config.fast_window,
            "slow_window": self._config.slow_window,
            "atr_window": self._config.atr_window,
            "minimum_spread_bps": str(self._config.minimum_spread_bps),
            "minimum_atr_bps": str(self._config.minimum_atr_bps),
            "symbol": symbol,
            "interval": interval,
            "dataset_digest": dataset_digest,
            "boundary_index": boundary_index,
            "processed_count": len(candles),
            "last_processed_timestamp": candles[-1].close_time_ms,
            "fast_ema": str(fast),
            "slow_ema": str(slow),
            "atr_tail": [item.payload() for item in tail],
        }
        return TrendFilteredStateV1(
            **{
                **payload,
                "minimum_spread_bps": self._config.minimum_spread_bps,
                "minimum_atr_bps": self._config.minimum_atr_bps,
                "fast_ema": fast,
                "slow_ema": slow,
                "atr_tail": tail,
                "state_digest": _digest(payload),
            }
        )

    def _validate_state(
        self,
        state: TrendFilteredStateV1,
        *,
        symbol: str,
        interval: str,
        dataset_digest: str,
        boundary_index: int,
    ) -> None:
        if not isinstance(state, TrendFilteredStateV1) or state.state_digest != _digest(
            state.payload()
        ):
            raise StrategyInvariantError("trend filtered state digest is invalid")
        if (
            state.identity != TREND_FILTERED_STATE_IDENTITY
            or state.semantics_identity != TREND_FILTERED_STATE_SEMANTICS
            or state.strategy_identity != f"{self.name}/{self.version}"
            or state.strategy_config_fingerprint != self._config_fingerprint()
            or state.fast_window != self._config.fast_window
            or state.slow_window != self._config.slow_window
            or state.atr_window != self._config.atr_window
            or state.minimum_spread_bps != self._config.minimum_spread_bps
            or state.minimum_atr_bps != self._config.minimum_atr_bps
            or state.symbol != symbol
            or state.interval != interval
            or state.dataset_digest != dataset_digest
            or state.boundary_index != boundary_index
            or len(state.atr_tail) != self._config.atr_window
        ):
            raise StrategyInvariantError("trend filtered state binding is invalid")

    def continue_state(
        self,
        state: TrendFilteredStateV1,
        candle: Candle,
        *,
        symbol: str,
        interval: str,
        dataset_digest: str,
        boundary_index: int,
    ) -> TrendFilteredContinuationV1:
        self._validate_state(
            state,
            symbol=symbol,
            interval=interval,
            dataset_digest=dataset_digest,
            boundary_index=boundary_index,
        )
        if candle.close_time_ms <= state.last_processed_timestamp or candle.close <= 0:
            raise StrategyInvariantError("trend filtered continuation candle is invalid")
        with localcontext() as context:
            context.prec = EMA_DECIMAL_PRECISION
            fast_alpha = Decimal(2) / Decimal(self._config.fast_window + 1)
            slow_alpha = Decimal(2) / Decimal(self._config.slow_window + 1)
            fast = candle.close * fast_alpha + state.fast_ema * (Decimal(1) - fast_alpha)
            slow = candle.close * slow_alpha + state.slow_ema * (Decimal(1) - slow_alpha)
        tail = (*state.atr_tail, self._ohlc(candle))
        atr = average_true_range(
            tuple(item.high for item in tail),
            tuple(item.low for item in tail),
            tuple(item.close for item in tail),
            self._config.atr_window,
        )
        spread_bps = abs(fast - slow) / candle.close * Decimal(10_000)
        atr_bps = atr / candle.close * Decimal(10_000)
        crossed_up = state.fast_ema <= state.slow_ema and fast > slow
        crossed_down = state.fast_ema >= state.slow_ema and fast < slow
        confirmed = (
            spread_bps >= self._config.minimum_spread_bps
            and atr_bps >= self._config.minimum_atr_bps
        )
        if crossed_up and confirmed:
            direction, reason = SignalDirection.BUY, "trend_confirmed_entry"
        elif crossed_down and confirmed:
            direction, reason = SignalDirection.SELL, "trend_confirmed_exit"
        else:
            direction, reason = SignalDirection.HOLD, "trend_filter_or_hysteresis"
        payload = {
            **state.payload(),
            "processed_count": state.processed_count + 1,
            "last_processed_timestamp": candle.close_time_ms,
            "fast_ema": str(fast),
            "slow_ema": str(slow),
            "atr_tail": [item.payload() for item in tail[-self._config.atr_window :]],
        }
        next_state = TrendFilteredStateV1(
            **{
                **payload,
                "minimum_spread_bps": self._config.minimum_spread_bps,
                "minimum_atr_bps": self._config.minimum_atr_bps,
                "fast_ema": fast,
                "slow_ema": slow,
                "atr_tail": tail[-self._config.atr_window :],
                "state_digest": _digest(payload),
            }
        )
        return TrendFilteredContinuationV1(
            direction, reason, fast, slow, spread_bps, atr_bps, next_state
        )

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
