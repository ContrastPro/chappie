from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from types import MappingProxyType

from app.market.models import CandleSnapshot, MarketStreamKey
from app.trading.exceptions import (
    InconsistentTradingTimestampError,
    InvalidDecimalValueError,
    InvalidMarketSnapshotError,
    InvalidOrderIntentError,
    InvalidRiskDecisionError,
    InvalidSignalError,
)

type ImmutableValue = str | int | bool | Decimal | None | tuple[ImmutableValue, ...]
type ImmutableMapping = Mapping[str, ImmutableValue]


class SignalDirection(Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


BUY_REASON_CODE = "bullish_crossover"
SELL_REASON_CODE = "bearish_crossover"


class OrderIntentAction(Enum):
    ENTER_LONG = "enter_long"
    EXIT_LONG = "exit_long"


class RiskDecisionOutcome(Enum):
    APPROVED = "approved"
    REJECTED = "rejected"
    MODIFIED = "modified"


def _require_non_empty(value: object, field_name: str, error_type: type[ValueError]) -> None:
    if not isinstance(value, str) or not value.strip():
        raise error_type(f"{field_name} must be a non-empty string")


def _require_timestamp(value: object, field_name: str, error_type: type[ValueError]) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise error_type(f"{field_name} must be a non-negative integer")


def _validate_exposure(value: object, field_name: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise InvalidDecimalValueError(f"{field_name} must be a finite Decimal")
    if value < Decimal(0) or value > Decimal(1):
        raise InvalidDecimalValueError(f"{field_name} must be between 0 and 1")
    return value


def _freeze_value(value: object) -> ImmutableValue:
    if value is None or isinstance(value, (str, bool, Decimal)):
        if isinstance(value, Decimal) and not value.is_finite():
            raise InvalidDecimalValueError("Mapping Decimal values must be finite")
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, (tuple, list)):
        return tuple(_freeze_value(item) for item in value)
    raise TypeError("Mappings may contain only immutable scalar values and sequences")


def freeze_mapping(value: Mapping[str, object]) -> ImmutableMapping:
    if not isinstance(value, Mapping):
        raise TypeError("Value must be a mapping")
    frozen: dict[str, ImmutableValue] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise TypeError("Mapping keys must be non-empty strings")
        frozen[key] = _freeze_value(item)
    return MappingProxyType(frozen)


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    snapshot_id: str
    stream_key: MarketStreamKey
    as_of_ms: int
    closed_candles: tuple[CandleSnapshot, ...]
    latest_closed_candle: CandleSnapshot | None
    latest_open_candle: CandleSnapshot | None
    source_event_id: str | None
    correlation_cycle_id: str | None
    data_version: str
    staleness_ms: int | None
    is_stale: bool

    def __post_init__(self) -> None:
        _require_non_empty(self.snapshot_id, "snapshot_id", InvalidMarketSnapshotError)
        if not isinstance(self.stream_key, MarketStreamKey):
            raise InvalidMarketSnapshotError("stream_key must be a MarketStreamKey")
        _require_timestamp(self.as_of_ms, "as_of_ms", InvalidMarketSnapshotError)
        _require_non_empty(self.data_version, "data_version", InvalidMarketSnapshotError)
        if self.source_event_id is not None:
            _require_non_empty(
                self.source_event_id,
                "source_event_id",
                InvalidMarketSnapshotError,
            )
        if self.correlation_cycle_id is not None:
            _require_non_empty(
                self.correlation_cycle_id,
                "correlation_cycle_id",
                InvalidMarketSnapshotError,
            )
        if not isinstance(self.closed_candles, tuple) or any(
            not isinstance(candle, CandleSnapshot) for candle in self.closed_candles
        ):
            raise InvalidMarketSnapshotError(
                "closed_candles must be a tuple of CandleSnapshot values"
            )
        previous: CandleSnapshot | None = None
        for candle in self.closed_candles:
            if previous is not None and candle.open_time_ms <= previous.open_time_ms:
                raise InvalidMarketSnapshotError("closed_candles must be strictly ordered")
            if previous is not None and candle.open_time_ms <= previous.close_time_ms:
                raise InvalidMarketSnapshotError("closed_candles must not overlap")
            if self.as_of_ms <= candle.close_time_ms:
                raise InvalidMarketSnapshotError("closed_candles must be closed at as_of_ms")
            previous = candle
        expected_latest = self.closed_candles[-1] if self.closed_candles else None
        if self.latest_closed_candle != expected_latest:
            raise InvalidMarketSnapshotError("latest_closed_candle must match the closed history")
        if self.latest_open_candle is not None:
            if not isinstance(self.latest_open_candle, CandleSnapshot):
                raise InvalidMarketSnapshotError("latest_open_candle must be a CandleSnapshot")
            if self.as_of_ms > self.latest_open_candle.close_time_ms:
                raise InvalidMarketSnapshotError("latest_open_candle is already closed")
            if (
                expected_latest is not None
                and self.latest_open_candle.open_time_ms <= expected_latest.open_time_ms
            ):
                raise InvalidMarketSnapshotError(
                    "latest_open_candle must follow the closed history"
                )
            if (
                expected_latest is not None
                and self.latest_open_candle.open_time_ms <= expected_latest.close_time_ms
            ):
                raise InvalidMarketSnapshotError(
                    "latest_open_candle must not overlap closed history"
                )
        if self.staleness_ms is not None:
            _require_timestamp(
                self.staleness_ms,
                "staleness_ms",
                InvalidMarketSnapshotError,
            )
        if not isinstance(self.is_stale, bool):
            raise InvalidMarketSnapshotError("is_stale must be a boolean")


@dataclass(frozen=True, slots=True)
class Signal:
    signal_id: str
    snapshot_id: str
    stream_key: MarketStreamKey
    created_at_ms: int
    effective_at_ms: int
    direction: SignalDirection
    reason_code: str
    explanation: str
    strategy_name: str
    strategy_version: str
    strategy_parameters: ImmutableMapping
    correlation_id: str | None = None
    metadata: ImmutableMapping = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        for field_name in (
            "signal_id",
            "snapshot_id",
            "reason_code",
            "explanation",
            "strategy_name",
            "strategy_version",
        ):
            _require_non_empty(getattr(self, field_name), field_name, InvalidSignalError)
        if not isinstance(self.stream_key, MarketStreamKey):
            raise InvalidSignalError("stream_key must be a MarketStreamKey")
        if not isinstance(self.direction, SignalDirection):
            raise InvalidSignalError("direction must be a SignalDirection")
        _require_timestamp(self.created_at_ms, "created_at_ms", InvalidSignalError)
        _require_timestamp(self.effective_at_ms, "effective_at_ms", InvalidSignalError)
        if self.effective_at_ms > self.created_at_ms:
            raise InconsistentTradingTimestampError("effective_at_ms must not exceed created_at_ms")
        if self.effective_at_ms != self.created_at_ms:
            raise InconsistentTradingTimestampError(
                "M2.2 signals require effective_at_ms equal to created_at_ms"
            )
        if self.direction is SignalDirection.BUY and self.reason_code != BUY_REASON_CODE:
            raise InvalidSignalError("BUY signals require reason_code bullish_crossover")
        if self.direction is SignalDirection.SELL and self.reason_code != SELL_REASON_CODE:
            raise InvalidSignalError("SELL signals require reason_code bearish_crossover")
        if self.direction is SignalDirection.HOLD and self.reason_code in {
            BUY_REASON_CODE,
            SELL_REASON_CODE,
        }:
            raise InvalidSignalError("HOLD signals cannot use crossover direction reason codes")
        if self.correlation_id is not None:
            _require_non_empty(self.correlation_id, "correlation_id", InvalidSignalError)
        try:
            object.__setattr__(
                self, "strategy_parameters", freeze_mapping(self.strategy_parameters)
            )
            object.__setattr__(self, "metadata", freeze_mapping(self.metadata))
        except (TypeError, InvalidDecimalValueError) as error:
            raise InvalidSignalError("Signal mappings must be immutable values") from error


@dataclass(frozen=True, slots=True)
class OrderIntent:
    intent_id: str
    source_signal_id: str
    stream_key: MarketStreamKey
    created_at_ms: int
    action: OrderIntentAction
    target_exposure: Decimal
    reason: str
    strategy_name: str
    strategy_version: str
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "intent_id",
            "source_signal_id",
            "reason",
            "strategy_name",
            "strategy_version",
        ):
            _require_non_empty(getattr(self, field_name), field_name, InvalidOrderIntentError)
        if not isinstance(self.stream_key, MarketStreamKey):
            raise InvalidOrderIntentError("stream_key must be a MarketStreamKey")
        _require_timestamp(self.created_at_ms, "created_at_ms", InvalidOrderIntentError)
        if not isinstance(self.action, OrderIntentAction):
            raise InvalidOrderIntentError("action must be an OrderIntentAction")
        try:
            exposure = _validate_exposure(self.target_exposure, "target_exposure")
        except InvalidDecimalValueError as error:
            raise InvalidOrderIntentError(str(error)) from error
        if self.action is OrderIntentAction.ENTER_LONG and exposure == 0:
            raise InvalidOrderIntentError("ENTER_LONG requires positive target_exposure")
        if self.action is OrderIntentAction.EXIT_LONG and exposure != 0:
            raise InvalidOrderIntentError("EXIT_LONG requires zero target_exposure")
        if self.correlation_id is not None:
            _require_non_empty(
                self.correlation_id,
                "correlation_id",
                InvalidOrderIntentError,
            )


@dataclass(frozen=True, slots=True)
class RiskDecision:
    decision_id: str
    intent_id: str
    decided_at_ms: int
    outcome: RiskDecisionOutcome
    requested_target_exposure: Decimal
    approved_target_exposure: Decimal | None
    rejection_reasons: tuple[str, ...]
    applied_limits: ImmutableMapping
    risk_policy_version: str
    correlation_id: str | None = None
    market_snapshot_id: str | None = None
    portfolio_id: str | None = None
    portfolio_version: int | None = None
    approved_order: object | None = None
    sizing_audit: object | None = None
    modification_reasons: tuple[str, ...] = ()
    risk_policy_name: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty(self.decision_id, "decision_id", InvalidRiskDecisionError)
        _require_non_empty(self.intent_id, "intent_id", InvalidRiskDecisionError)
        _require_non_empty(
            self.risk_policy_version,
            "risk_policy_version",
            InvalidRiskDecisionError,
        )
        _require_timestamp(self.decided_at_ms, "decided_at_ms", InvalidRiskDecisionError)
        if not isinstance(self.outcome, RiskDecisionOutcome):
            raise InvalidRiskDecisionError("outcome must be a RiskDecisionOutcome")
        try:
            requested = _validate_exposure(
                self.requested_target_exposure,
                "requested_target_exposure",
            )
            approved = (
                None
                if self.approved_target_exposure is None
                else _validate_exposure(
                    self.approved_target_exposure,
                    "approved_target_exposure",
                )
            )
        except InvalidDecimalValueError as error:
            raise InvalidRiskDecisionError(str(error)) from error
        if not isinstance(self.rejection_reasons, tuple) or any(
            not isinstance(reason, str) or not reason.strip() for reason in self.rejection_reasons
        ):
            raise InvalidRiskDecisionError("rejection_reasons must be a tuple of non-empty strings")
        if len(set(self.rejection_reasons)) != len(self.rejection_reasons):
            raise InvalidRiskDecisionError("rejection_reasons must not contain duplicates")
        if self.outcome is RiskDecisionOutcome.APPROVED:
            if approved != requested or self.rejection_reasons:
                raise InvalidRiskDecisionError(
                    "APPROVED requires unchanged exposure and no rejection reasons"
                )
        elif self.outcome is RiskDecisionOutcome.REJECTED:
            if approved is not None or not self.rejection_reasons:
                raise InvalidRiskDecisionError(
                    "REJECTED requires no exposure and at least one reason"
                )
        else:
            if approved is None or (approved == requested and not self.modification_reasons):
                raise InvalidRiskDecisionError("MODIFIED requires a different approved exposure")
            if self.rejection_reasons:
                raise InvalidRiskDecisionError("MODIFIED must not contain rejection reasons")
        if self.correlation_id is not None:
            _require_non_empty(
                self.correlation_id,
                "correlation_id",
                InvalidRiskDecisionError,
            )
        enriched = any(
            value is not None
            for value in (
                self.market_snapshot_id,
                self.portfolio_id,
                self.portfolio_version,
                self.approved_order,
                self.sizing_audit,
                self.risk_policy_name,
            )
        ) or bool(self.modification_reasons)
        if enriched:
            for name in ("market_snapshot_id", "portfolio_id", "risk_policy_name"):
                _require_non_empty(getattr(self, name), name, InvalidRiskDecisionError)
            if (
                isinstance(self.portfolio_version, bool)
                or not isinstance(self.portfolio_version, int)
                or self.portfolio_version < 0
            ):
                raise InvalidRiskDecisionError("portfolio_version must be non-negative")
            if self.sizing_audit is None:
                raise InvalidRiskDecisionError("enriched decisions require sizing_audit")
            if not isinstance(self.modification_reasons, tuple) or any(
                not isinstance(reason, str) or not reason.strip()
                for reason in self.modification_reasons
            ):
                raise InvalidRiskDecisionError(
                    "modification_reasons must contain non-empty string enums"
                )
            if len(set(self.modification_reasons)) != len(self.modification_reasons):
                raise InvalidRiskDecisionError("modification_reasons must not contain duplicates")
            if self.outcome is RiskDecisionOutcome.REJECTED:
                if self.approved_order is not None or self.modification_reasons:
                    raise InvalidRiskDecisionError(
                        "REJECTED enriched decisions cannot contain approval or modifications"
                    )
            elif self.approved_order is None:
                raise InvalidRiskDecisionError(
                    "APPROVED and MODIFIED enriched decisions require approved_order"
                )
            if self.outcome is RiskDecisionOutcome.APPROVED and self.modification_reasons:
                raise InvalidRiskDecisionError("APPROVED cannot contain modification reasons")
            if self.outcome is RiskDecisionOutcome.MODIFIED and not self.modification_reasons:
                raise InvalidRiskDecisionError("MODIFIED requires a modification reason")
        try:
            object.__setattr__(self, "applied_limits", freeze_mapping(self.applied_limits))
        except (TypeError, InvalidDecimalValueError) as error:
            raise InvalidRiskDecisionError(
                "applied_limits must contain immutable values"
            ) from error
