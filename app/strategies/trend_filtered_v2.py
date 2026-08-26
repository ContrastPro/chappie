"""Versioned pending-confirmation trend filter with exact bounded indicator state."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Literal

from app.market.models import Candle
from app.strategies.common import StrategyIdentity, parameters
from app.strategies.trend_filtered import (
    TrendFilteredConfig,
    TrendFilteredStateV1,
    TrendFilteredStrategy,
)
from app.trading.exceptions import InvalidStrategyConfigurationError, StrategyInvariantError
from app.trading.models import MarketSnapshot, Signal, SignalDirection
from app.trading.strategy import SignalIdFactory

TREND_FILTERED_V2_STATE_IDENTITY = "chappie_trend_filtered_v2_state/1.0.0"
TREND_FILTERED_V2_STATE_SEMANTICS = "trend-filtered-v2-pending-confirmation/1.0.0"
PendingDirection = Literal["BULLISH", "BEARISH"]


def _digest(value: object) -> str:
    return sha256(
        json.dumps(
            value, default=str, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class TrendFilteredV2Config:
    fast_window: int = 12
    slow_window: int = 26
    atr_window: int = 14
    minimum_spread_bps: Decimal = Decimal(8)
    minimum_atr_bps: Decimal = Decimal(5)
    confirmation_window_candles: int = 1
    strategy_name: str = "trend_filtered"
    strategy_version: str = "2.0.0"

    def __post_init__(self) -> None:
        TrendFilteredConfig(
            self.fast_window,
            self.slow_window,
            self.atr_window,
            self.minimum_spread_bps,
            self.minimum_atr_bps,
            self.strategy_name,
            self.strategy_version,
        )
        if (
            not isinstance(self.confirmation_window_candles, int)
            or isinstance(self.confirmation_window_candles, bool)
            or self.confirmation_window_candles <= 0
        ):
            raise InvalidStrategyConfigurationError("confirmation window must be positive")


@dataclass(frozen=True, slots=True)
class TrendFilteredV2StateV1:
    identity: str
    semantics_identity: str
    strategy_identity: str
    strategy_config_fingerprint: str
    confirmation_window_candles: int
    indicator_state: TrendFilteredStateV1
    pending_direction: PendingDirection | None
    pending_started_index: int | None
    pending_started_timestamp: int | None
    pending_age_candles: int
    state_digest: str

    def payload(self) -> dict[str, object]:
        return {
            "identity": self.identity,
            "semantics_identity": self.semantics_identity,
            "strategy_identity": self.strategy_identity,
            "strategy_config_fingerprint": self.strategy_config_fingerprint,
            "confirmation_window_candles": self.confirmation_window_candles,
            "indicator_state": self.indicator_state.serialized(),
            "pending_direction": self.pending_direction,
            "pending_started_index": self.pending_started_index,
            "pending_started_timestamp": self.pending_started_timestamp,
            "pending_age_candles": self.pending_age_candles,
        }

    def serialized(self) -> dict[str, object]:
        return {**self.payload(), "state_digest": self.state_digest}

    @classmethod
    def from_serialized(cls, value: object) -> TrendFilteredV2StateV1:
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise StrategyInvariantError("trend filtered V2 state payload shape is invalid")
        try:
            state = cls(
                identity=value["identity"],
                semantics_identity=value["semantics_identity"],
                strategy_identity=value["strategy_identity"],
                strategy_config_fingerprint=value["strategy_config_fingerprint"],
                confirmation_window_candles=value["confirmation_window_candles"],
                indicator_state=TrendFilteredStateV1.from_serialized(value["indicator_state"]),
                pending_direction=value["pending_direction"],
                pending_started_index=value["pending_started_index"],
                pending_started_timestamp=value["pending_started_timestamp"],
                pending_age_candles=value["pending_age_candles"],
                state_digest=value["state_digest"],
            )
        except (KeyError, TypeError, InvalidOperation, ValueError) as error:
            raise StrategyInvariantError("trend filtered V2 state payload is invalid") from error
        if state.state_digest != _digest(state.payload()):
            raise StrategyInvariantError("trend filtered V2 state digest is invalid")
        return state


@dataclass(frozen=True, slots=True)
class TrendFilteredV2ContinuationV1:
    direction: SignalDirection
    reason_code: str
    state: TrendFilteredV2StateV1
    fast_ema: Decimal
    slow_ema: Decimal
    spread_bps: Decimal
    atr_bps: Decimal


class TrendFilteredV2Strategy(TrendFilteredStrategy):
    """Crossover starts a pending state; only later closed candles may confirm it."""

    def __init__(self, config: TrendFilteredV2Config, signal_id_factory: SignalIdFactory) -> None:
        self._v2_config = config
        super().__init__(
            TrendFilteredConfig(
                config.fast_window,
                config.slow_window,
                config.atr_window,
                config.minimum_spread_bps,
                config.minimum_atr_bps,
                config.strategy_name,
                config.strategy_version,
            ),
            signal_id_factory,
        )
        self._identity = StrategyIdentity(config.strategy_name, config.strategy_version)
        self._configuration = parameters(
            fast_window=config.fast_window,
            slow_window=config.slow_window,
            atr_window=config.atr_window,
            minimum_spread_bps=config.minimum_spread_bps,
            minimum_atr_bps=config.minimum_atr_bps,
            confirmation_window_candles=config.confirmation_window_candles,
        )

    def _v2_fingerprint(self) -> str:
        return _digest(
            {"strategy_identity": f"{self.name}/{self.version}", **dict(self.configuration)}
        )

    def _new_state(
        self,
        indicator: TrendFilteredStateV1,
        direction: PendingDirection | None,
        index: int | None,
        timestamp: int | None,
        age: int,
    ) -> TrendFilteredV2StateV1:
        payload = {
            "identity": TREND_FILTERED_V2_STATE_IDENTITY,
            "semantics_identity": TREND_FILTERED_V2_STATE_SEMANTICS,
            "strategy_identity": f"{self.name}/{self.version}",
            "strategy_config_fingerprint": self._v2_fingerprint(),
            "confirmation_window_candles": self._v2_config.confirmation_window_candles,
            "indicator_state": indicator.serialized(),
            "pending_direction": direction,
            "pending_started_index": index,
            "pending_started_timestamp": timestamp,
            "pending_age_candles": age,
        }
        return TrendFilteredV2StateV1(
            identity=TREND_FILTERED_V2_STATE_IDENTITY,
            semantics_identity=TREND_FILTERED_V2_STATE_SEMANTICS,
            strategy_identity=f"{self.name}/{self.version}",
            strategy_config_fingerprint=self._v2_fingerprint(),
            confirmation_window_candles=self._v2_config.confirmation_window_candles,
            indicator_state=indicator,
            pending_direction=direction,
            pending_started_index=index,
            pending_started_timestamp=timestamp,
            pending_age_candles=age,
            state_digest=_digest(payload),
        )

    def create_state(
        self,
        candles: tuple[Candle, ...],
        *,
        symbol: str,
        interval: str,
        dataset_digest: str,
        boundary_index: int,
    ) -> TrendFilteredV2StateV1:
        indicator = super().create_state(
            candles,
            symbol=symbol,
            interval=interval,
            dataset_digest=dataset_digest,
            boundary_index=boundary_index,
        )
        return self._new_state(indicator, None, None, None, 0)

    def _validate_v2(self, state: TrendFilteredV2StateV1) -> None:
        if (
            not isinstance(state, TrendFilteredV2StateV1)
            or state.state_digest != _digest(state.payload())
            or state.identity != TREND_FILTERED_V2_STATE_IDENTITY
            or state.semantics_identity != TREND_FILTERED_V2_STATE_SEMANTICS
            or state.strategy_identity != f"{self.name}/{self.version}"
            or state.strategy_config_fingerprint != self._v2_fingerprint()
            or state.confirmation_window_candles != self._v2_config.confirmation_window_candles
            or (state.pending_direction is None) != (state.pending_started_index is None)
            or (state.pending_direction is None) != (state.pending_started_timestamp is None)
            or (state.pending_direction is None and state.pending_age_candles != 0)
            or (
                state.pending_direction is not None
                and (
                    state.pending_age_candles < 0
                    or state.pending_direction not in {"BULLISH", "BEARISH"}
                )
            )
        ):
            raise StrategyInvariantError("trend filtered V2 state binding is invalid")

    def continue_state(
        self,
        state: TrendFilteredV2StateV1,
        candle: Candle,
        *,
        symbol: str,
        interval: str,
        dataset_digest: str,
        boundary_index: int,
    ) -> TrendFilteredV2ContinuationV1:
        self._validate_v2(state)
        base = super().continue_state(
            state.indicator_state,
            candle,
            symbol=symbol,
            interval=interval,
            dataset_digest=dataset_digest,
            boundary_index=boundary_index,
        )
        previous, current = state.indicator_state, base.state
        bullish = previous.fast_ema <= previous.slow_ema and base.fast_ema > base.slow_ema
        bearish = previous.fast_ema >= previous.slow_ema and base.fast_ema < base.slow_ema
        index = current.processed_count - 1
        if bullish or bearish:
            pending: PendingDirection = "BULLISH" if bullish else "BEARISH"
            next_state = self._new_state(current, pending, index, candle.close_time_ms, 0)
            return TrendFilteredV2ContinuationV1(
                SignalDirection.HOLD,
                "pending_crossover",
                next_state,
                base.fast_ema,
                base.slow_ema,
                base.spread_bps,
                base.atr_bps,
            )
        pending = state.pending_direction
        if pending is None:
            next_state = self._new_state(current, None, None, None, 0)
            return TrendFilteredV2ContinuationV1(
                SignalDirection.HOLD,
                "no_pending_confirmation",
                next_state,
                base.fast_ema,
                base.slow_ema,
                base.spread_bps,
                base.atr_bps,
            )
        relation_holds = (
            base.fast_ema > base.slow_ema if pending == "BULLISH" else base.fast_ema < base.slow_ema
        )
        if not relation_holds:
            next_state = self._new_state(current, None, None, None, 0)
            return TrendFilteredV2ContinuationV1(
                SignalDirection.HOLD,
                "pending_relation_reversed",
                next_state,
                base.fast_ema,
                base.slow_ema,
                base.spread_bps,
                base.atr_bps,
            )
        age = state.pending_age_candles + 1
        confirmed = (
            base.spread_bps >= self._v2_config.minimum_spread_bps
            and base.atr_bps >= self._v2_config.minimum_atr_bps
        )
        if confirmed:
            direction = SignalDirection.BUY if pending == "BULLISH" else SignalDirection.SELL
            next_state = self._new_state(current, None, None, None, 0)
            return TrendFilteredV2ContinuationV1(
                direction,
                "trend_confirmed_entry"
                if direction is SignalDirection.BUY
                else "trend_confirmed_exit",
                next_state,
                base.fast_ema,
                base.slow_ema,
                base.spread_bps,
                base.atr_bps,
            )
        if age >= self._v2_config.confirmation_window_candles:
            next_state = self._new_state(current, None, None, None, 0)
            return TrendFilteredV2ContinuationV1(
                SignalDirection.HOLD,
                "pending_confirmation_expired",
                next_state,
                base.fast_ema,
                base.slow_ema,
                base.spread_bps,
                base.atr_bps,
            )
        next_state = self._new_state(
            current, pending, state.pending_started_index, state.pending_started_timestamp, age
        )
        return TrendFilteredV2ContinuationV1(
            SignalDirection.HOLD,
            "pending_confirmation",
            next_state,
            base.fast_ema,
            base.slow_ema,
            base.spread_bps,
            base.atr_bps,
        )

    def generate_signal(self, snapshot: MarketSnapshot) -> Signal:
        if snapshot.latest_closed_candle is None:
            return self._signal(snapshot, SignalDirection.HOLD, "no_closed_candles", {})
        if snapshot.is_stale:
            return self._signal(snapshot, SignalDirection.HOLD, "stale_snapshot", {})
        candles = snapshot.closed_candles
        if len(candles) < self.required_history:
            return self._signal(snapshot, SignalDirection.HOLD, "insufficient_history", {})
        state = self.create_state(
            tuple(candles[: self.required_history]),
            symbol=snapshot.stream_key.symbol,
            interval=snapshot.stream_key.interval,
            dataset_digest="reference",
            boundary_index=self.required_history,
        )
        continuation: TrendFilteredV2ContinuationV1 | None = None
        for candle in candles[self.required_history :]:
            continuation = self.continue_state(
                state,
                candle,
                symbol=snapshot.stream_key.symbol,
                interval=snapshot.stream_key.interval,
                dataset_digest="reference",
                boundary_index=self.required_history,
            )
            state = continuation.state
        if continuation is None:
            return self._signal(snapshot, SignalDirection.HOLD, "insufficient_history", {})
        return self._signal(
            snapshot,
            continuation.direction,
            continuation.reason_code,
            {
                "fast_ema": continuation.fast_ema,
                "slow_ema": continuation.slow_ema,
                "spread_bps": continuation.spread_bps,
                "atr_bps": continuation.atr_bps,
            },
        )
