from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, runtime_checkable
from uuid import uuid4

from app.execution.exceptions import (
    ExecutionConfigurationError,
    ExecutionIdFactoryError,
    ExecutionModelValidationError,
    FillIdFactoryError,
)
from app.market.cache import MarketStreamKey
from app.trading.models import ImmutableMapping, freeze_mapping


class InvalidFillError(ValueError):
    """Raised when an execution fill is structurally invalid."""


class FillSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class PaperExecutionStatus(StrEnum):
    FILLED = "FILLED"
    NOT_EXECUTED = "NOT_EXECUTED"


@dataclass(frozen=True, slots=True)
class PaperExecutionConfig:
    provider_name: str = "deterministic_paper"
    provider_version: str = "1.0.0"
    fee_rate: Decimal = Decimal("0.001")
    latency_ms: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.provider_name, str) or not self.provider_name.strip():
            raise ExecutionConfigurationError("provider_name must be a non-empty string")
        if not isinstance(self.provider_version, str) or not self.provider_version.strip():
            raise ExecutionConfigurationError("provider_version must be a non-empty string")
        if not isinstance(self.fee_rate, Decimal) or not self.fee_rate.is_finite():
            raise ExecutionConfigurationError("fee_rate must be a finite Decimal")
        if self.fee_rate < 0 or self.fee_rate >= 1:
            raise ExecutionConfigurationError("fee_rate must be in [0, 1)")
        if (
            isinstance(self.latency_ms, bool)
            or not isinstance(self.latency_ms, int)
            or self.latency_ms < 0
        ):
            raise ExecutionConfigurationError("latency_ms must be a non-negative integer")


@runtime_checkable
class ExecutionIdFactory(Protocol):
    def new_execution_id(self) -> str: ...


@runtime_checkable
class FillIdFactory(Protocol):
    def new_fill_id(self) -> str: ...


class UUIDExecutionIdFactory:
    def new_execution_id(self) -> str:
        return str(uuid4())


class UUIDFillIdFactory:
    def new_fill_id(self) -> str:
        return str(uuid4())


def create_execution_id(factory: ExecutionIdFactory) -> str:
    if not isinstance(factory, ExecutionIdFactory):
        raise ExecutionIdFactoryError("execution ID factory does not implement the protocol")
    try:
        value = factory.new_execution_id()
    except Exception as error:
        raise ExecutionIdFactoryError("execution ID factory failed") from error
    if not isinstance(value, str) or not value.strip():
        raise ExecutionIdFactoryError("execution ID factory returned an invalid ID")
    return value


def create_fill_id(factory: FillIdFactory) -> str:
    if not isinstance(factory, FillIdFactory):
        raise FillIdFactoryError("fill ID factory does not implement the protocol")
    try:
        value = factory.new_fill_id()
    except Exception as error:
        raise FillIdFactoryError("fill ID factory failed") from error
    if not isinstance(value, str) or not value.strip():
        raise FillIdFactoryError("fill ID factory returned an invalid ID")
    return value


def _require_text(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise InvalidFillError(f"{name} must be a non-empty string")


def _require_decimal(value: object, name: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise InvalidFillError(f"{name} must be a finite Decimal")
    if positive and value <= 0:
        raise InvalidFillError(f"{name} must be positive")
    return value


@dataclass(frozen=True, slots=True)
class Fill:
    fill_id: str
    order_id: str
    risk_decision_id: str
    intent_id: str
    stream_key: MarketStreamKey
    side: FillSide
    quantity: Decimal
    price: Decimal
    gross_notional: Decimal
    fee: Decimal
    fee_asset: str
    net_quote_change: Decimal
    net_base_change: Decimal
    filled_at_ms: int
    correlation_id: str | None = None
    metadata: ImmutableMapping = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        for name in ("fill_id", "order_id", "risk_decision_id", "intent_id", "fee_asset"):
            _require_text(getattr(self, name), name)
        if not isinstance(self.stream_key, MarketStreamKey):
            raise InvalidFillError("stream_key must be a MarketStreamKey")
        if not isinstance(self.side, FillSide):
            raise InvalidFillError("side must be a FillSide")
        _require_decimal(self.quantity, "quantity", positive=True)
        _require_decimal(self.price, "price", positive=True)
        _require_decimal(self.gross_notional, "gross_notional", positive=True)
        fee = _require_decimal(self.fee, "fee")
        quote_change = _require_decimal(self.net_quote_change, "net_quote_change")
        base_change = _require_decimal(self.net_base_change, "net_base_change")
        if fee < 0:
            raise InvalidFillError("fee must not be negative")
        if isinstance(self.filled_at_ms, bool) or not isinstance(self.filled_at_ms, int):
            raise InvalidFillError("filled_at_ms must be an integer")
        if self.filled_at_ms < 0:
            raise InvalidFillError("filled_at_ms must not be negative")
        if self.side is FillSide.BUY and not (quote_change < 0 < base_change):
            raise InvalidFillError("BUY requires negative quote and positive base changes")
        if self.side is FillSide.SELL and not (base_change < 0 < quote_change):
            raise InvalidFillError("SELL requires positive quote and negative base changes")
        if self.correlation_id is not None:
            _require_text(self.correlation_id, "correlation_id")
        try:
            object.__setattr__(self, "metadata", freeze_mapping(self.metadata))
        except (TypeError, ValueError) as error:
            raise InvalidFillError("metadata must contain immutable values") from error


@dataclass(frozen=True, slots=True)
class PaperExecutionResult:
    execution_id: str
    risk_decision_id: str
    intent_id: str
    status: PaperExecutionStatus
    fill: Fill | None
    started_at_ms: int
    completed_at_ms: int
    latency_ms: int
    provider_name: str
    provider_version: str
    reason_code: str
    risk_rejection_reasons: tuple[str, ...] = ()
    correlation_id: str | None = None
    metadata: ImmutableMapping = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        for name in (
            "execution_id",
            "risk_decision_id",
            "intent_id",
            "provider_name",
            "provider_version",
            "reason_code",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ExecutionModelValidationError(f"{name} must be a non-empty string")
        if not isinstance(self.status, PaperExecutionStatus):
            raise ExecutionModelValidationError("status must be a PaperExecutionStatus")
        for name in ("started_at_ms", "completed_at_ms", "latency_ms"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ExecutionModelValidationError(f"{name} must be non-negative integer")
        if self.completed_at_ms < self.started_at_ms:
            raise ExecutionModelValidationError("completed_at_ms precedes started_at_ms")
        if self.latency_ms != self.completed_at_ms - self.started_at_ms:
            raise ExecutionModelValidationError("latency_ms does not match timestamps")
        if self.correlation_id is not None and (
            not isinstance(self.correlation_id, str) or not self.correlation_id.strip()
        ):
            raise ExecutionModelValidationError("correlation_id must be a non-empty string")
        if not isinstance(self.risk_rejection_reasons, tuple) or any(
            not isinstance(reason, str) or not reason.strip()
            for reason in self.risk_rejection_reasons
        ):
            raise ExecutionModelValidationError(
                "risk_rejection_reasons must contain non-empty strings"
            )
        if len(set(self.risk_rejection_reasons)) != len(self.risk_rejection_reasons):
            raise ExecutionModelValidationError("risk rejection reasons must not duplicate")
        if self.status is PaperExecutionStatus.FILLED:
            if self.risk_rejection_reasons:
                raise ExecutionModelValidationError("FILLED cannot contain rejection reasons")
            if not isinstance(self.fill, Fill):
                raise ExecutionModelValidationError("FILLED requires a Fill")
            if self.fill.order_id != self.execution_id:
                raise ExecutionModelValidationError("fill order ID does not match execution ID")
            if self.fill.risk_decision_id != self.risk_decision_id:
                raise ExecutionModelValidationError("fill risk decision ID does not match")
            if self.fill.intent_id != self.intent_id:
                raise ExecutionModelValidationError("fill intent ID does not match")
            if self.fill.filled_at_ms != self.completed_at_ms:
                raise ExecutionModelValidationError("fill timestamp does not match completion")
            if self.fill.correlation_id != self.correlation_id:
                raise ExecutionModelValidationError("fill correlation ID does not match")
        else:
            if self.fill is not None:
                raise ExecutionModelValidationError("NOT_EXECUTED must not contain a Fill")
            if not self.risk_rejection_reasons:
                raise ExecutionModelValidationError(
                    "NOT_EXECUTED requires source rejection reasons"
                )
        try:
            object.__setattr__(self, "metadata", freeze_mapping(self.metadata))
        except (TypeError, ValueError) as error:
            raise ExecutionModelValidationError("metadata must contain immutable values") from error
