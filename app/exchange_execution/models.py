from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType

from app.exchange_execution.exceptions import (
    ExchangeExecutionConfigurationError,
    ExchangeExecutionInvariantError,
    ExchangeOrderIdentityError,
    ExchangeOrderStateError,
)
from app.market.models import MarketStreamKey

ZERO = Decimal(0)
SUPPORTED_SYMBOL = "BTCUSDT"
SUPPORTED_VENUE = "binance-spot"


def require_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExchangeExecutionInvariantError(f"{name} must be a non-empty string")
    return value


def require_hash(value: object, name: str) -> str:
    text = require_text(value, name)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ExchangeOrderIdentityError(f"{name} must be a lowercase SHA-256 hex digest")
    return text


def require_account_fingerprint(value: object) -> str:
    text = require_text(value, "account_fingerprint")
    if text != text.strip():
        raise ExchangeOrderIdentityError("account_fingerprint must be normalized")
    if (
        len(text) > 64
        or not text.isascii()
        or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
            for character in text
        )
    ):
        raise ExchangeOrderIdentityError("account_fingerprint has invalid characters or length")
    return text


def require_client_order_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 32
        or not value.startswith("ctt1t-")
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in value)
    ):
        raise ExchangeOrderIdentityError("client_order_id is not the accepted M4.2 format")
    return value


def require_decimal(
    value: object, name: str, *, positive: bool = False, non_negative: bool = False
) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ExchangeExecutionInvariantError(f"{name} must be a finite Decimal")
    if positive and value <= ZERO:
        raise ExchangeExecutionInvariantError(f"{name} must be positive")
    if non_negative and value < ZERO:
        raise ExchangeExecutionInvariantError(f"{name} must not be negative")
    return ZERO if value.is_zero() else value


def require_timestamp(value: object, name: str, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ExchangeExecutionInvariantError(f"{name} must be a non-negative integer")
    return value


def normalize_decimal_fields(instance: object, names: tuple[str, ...]) -> None:
    for name in names:
        value = getattr(instance, name)
        if isinstance(value, Decimal) and value.is_zero() and value.is_signed():
            object.__setattr__(instance, name, ZERO)


class ExecutionEnvironment(StrEnum):
    TESTNET = "testnet"


class ExchangeOrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class ExchangeOrderType(StrEnum):
    MARKET = "MARKET"


class ExchangeResponseType(StrEnum):
    ACK = "ACK"


class ExchangeOrderStatus(StrEnum):
    PREPARED = "PREPARED"
    SUBMITTING = "SUBMITTING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"

    @property
    def is_exchange_terminal(self) -> bool:
        return self in {self.FILLED, self.CANCELED, self.REJECTED, self.EXPIRED}


class TransportOutcomeKind(StrEnum):
    DEFINITELY_NOT_TRANSMITTED = "DEFINITELY_NOT_TRANSMITTED"
    MAY_HAVE_BEEN_TRANSMITTED = "MAY_HAVE_BEEN_TRANSMITTED"
    RESPONSE_RECEIVED = "RESPONSE_RECEIVED"
    DETERMINISTIC_REJECTION = "DETERMINISTIC_REJECTION"

    @property
    def requires_reconciliation(self) -> bool:
        return self is self.MAY_HAVE_BEEN_TRANSMITTED


class TransportFailureCategory(StrEnum):
    TIMEOUT = "TIMEOUT"
    HTTP_5XX = "HTTP_5XX"
    CONNECTION_RESET = "CONNECTION_RESET"
    RESPONSE_LOST = "RESPONSE_LOST"
    UNKNOWN_TRANSMISSION = "UNKNOWN_TRANSMISSION"


class QueryResultKind(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    AMBIGUOUS = "AMBIGUOUS"


class SettlementClassification(StrEnum):
    SETTLEABLE = "SETTLEABLE"
    UNSUPPORTED_FEE_ASSET = "UNSUPPORTED_FEE_ASSET"
    INCOMPLETE = "INCOMPLETE"
    CONFLICT = "CONFLICT"


class ExecutionSessionState(StrEnum):
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"


class KillSwitchState(StrEnum):
    ACTIVE = "ACTIVE"
    CLEARED = "CLEARED"


class ExchangeOrderEventSource(StrEnum):
    LOCAL_PREPARATION = "LOCAL_PREPARATION"
    TRANSPORT = "TRANSPORT"
    EXCHANGE_QUERY = "EXCHANGE_QUERY"
    EXCHANGE_TRADE = "EXCHANGE_TRADE"
    RECONCILIATION = "RECONCILIATION"


@dataclass(frozen=True, slots=True)
class ExchangeCommand:
    durable_command_key: str
    stable_decision_key: str
    decision_slot_key: str
    stream_key: MarketStreamKey
    side: ExchangeOrderSide
    base_quantity: Decimal
    reference_price: Decimal
    approved_target_exposure: Decimal
    approved_notional: Decimal
    risk_authorization_fingerprint: str
    instrument_rules_fingerprint: str
    execution_configuration_fingerprint: str
    authorized_at_ms: int
    correlation_id: str | None = None
    model_version: str = "exchange-command/v1"

    def __post_init__(self) -> None:
        require_hash(self.durable_command_key, "durable_command_key")
        require_hash(self.stable_decision_key, "stable_decision_key")
        require_hash(self.decision_slot_key, "decision_slot_key")
        if not isinstance(self.stream_key, MarketStreamKey):
            raise ExchangeExecutionInvariantError("stream_key must be a MarketStreamKey")
        if self.stream_key.symbol != SUPPORTED_SYMBOL:
            raise ExchangeExecutionConfigurationError("initial exchange symbol must be BTCUSDT")
        if not isinstance(self.side, ExchangeOrderSide):
            raise ExchangeExecutionInvariantError("side must be an ExchangeOrderSide")
        require_decimal(self.base_quantity, "base_quantity", positive=True)
        require_decimal(self.reference_price, "reference_price", positive=True)
        exposure = require_decimal(
            self.approved_target_exposure, "approved_target_exposure", non_negative=True
        )
        if exposure > Decimal(1):
            raise ExchangeExecutionInvariantError("approved_target_exposure must not exceed 1")
        require_decimal(self.approved_notional, "approved_notional", positive=True)
        for name in (
            "risk_authorization_fingerprint",
            "instrument_rules_fingerprint",
            "execution_configuration_fingerprint",
        ):
            require_hash(getattr(self, name), name)
        require_timestamp(self.authorized_at_ms, "authorized_at_ms")
        if self.correlation_id is not None:
            require_text(self.correlation_id, "correlation_id")
        require_text(self.model_version, "model_version")
        normalize_decimal_fields(
            self,
            (
                "base_quantity",
                "reference_price",
                "approved_target_exposure",
                "approved_notional",
            ),
        )


@dataclass(frozen=True, slots=True)
class ExchangeOrder:
    durable_command_key: str
    client_order_id: str
    account_fingerprint: str
    stream_key: MarketStreamKey
    side: ExchangeOrderSide
    original_base_quantity: Decimal
    status: ExchangeOrderStatus
    created_at_ms: int
    exchange_order_id: str | None = None
    cumulative_executed_base_quantity: Decimal = ZERO
    cumulative_quote_quantity: Decimal = ZERO
    submitted_at_ms: int | None = None
    acknowledged_at_ms: int | None = None
    latest_exchange_update_ms: int | None = None
    environment: ExecutionEnvironment = ExecutionEnvironment.TESTNET
    venue: str = SUPPORTED_VENUE
    model_version: str = "exchange-order/v1"

    def __post_init__(self) -> None:
        require_hash(self.durable_command_key, "durable_command_key")
        require_client_order_id(self.client_order_id)
        require_account_fingerprint(self.account_fingerprint)
        if (
            not isinstance(self.stream_key, MarketStreamKey)
            or self.stream_key.symbol != SUPPORTED_SYMBOL
        ):
            raise ExchangeExecutionInvariantError("stream_key must identify BTCUSDT")
        if not isinstance(self.side, ExchangeOrderSide):
            raise ExchangeExecutionInvariantError("side must be an ExchangeOrderSide")
        original = require_decimal(
            self.original_base_quantity, "original_base_quantity", positive=True
        )
        executed = require_decimal(
            self.cumulative_executed_base_quantity,
            "cumulative_executed_base_quantity",
            non_negative=True,
        )
        require_decimal(
            self.cumulative_quote_quantity, "cumulative_quote_quantity", non_negative=True
        )
        if executed > original:
            raise ExchangeOrderStateError("executed quantity exceeds original quantity")
        if not isinstance(self.status, ExchangeOrderStatus):
            raise ExchangeOrderStateError("status must be an ExchangeOrderStatus")
        if not isinstance(self.environment, ExecutionEnvironment):
            raise ExchangeExecutionConfigurationError("only TESTNET is supported")
        if self.venue != SUPPORTED_VENUE:
            raise ExchangeExecutionConfigurationError("venue must be binance-spot")
        require_timestamp(self.created_at_ms, "created_at_ms")
        for name in ("submitted_at_ms", "acknowledged_at_ms", "latest_exchange_update_ms"):
            require_timestamp(getattr(self, name), name, optional=True)
        if self.exchange_order_id is not None:
            require_text(self.exchange_order_id, "exchange_order_id")
        if self.status is self.status.PREPARED and (
            self.exchange_order_id is not None
            or executed != ZERO
            or self.submitted_at_ms is not None
        ):
            raise ExchangeOrderStateError("PREPARED cannot contain submission/execution facts")
        if (
            self.status
            in {
                self.status.ACKNOWLEDGED,
                self.status.PARTIALLY_FILLED,
                self.status.FILLED,
            }
            and self.exchange_order_id is None
        ):
            raise ExchangeOrderStateError(f"{self.status} requires exchange_order_id")
        if self.status is self.status.SUBMITTING and executed != ZERO:
            raise ExchangeOrderStateError("SUBMITTING cannot contain executed quantity")
        if self.status is self.status.PARTIALLY_FILLED and not ZERO < executed < original:
            raise ExchangeOrderStateError("PARTIALLY_FILLED requires a partial quantity")
        if self.status is self.status.FILLED and executed != original:
            raise ExchangeOrderStateError("FILLED requires exact original quantity")
        if self.status is self.status.REJECTED and executed != ZERO:
            raise ExchangeOrderStateError("REJECTED cannot contain executed quantity")
        require_text(self.model_version, "model_version")
        normalize_decimal_fields(
            self,
            (
                "original_base_quantity",
                "cumulative_executed_base_quantity",
                "cumulative_quote_quantity",
            ),
        )


@dataclass(frozen=True, slots=True)
class ExchangeOrderEvent:
    event_id: str
    durable_command_key: str
    client_order_id: str
    previous_status: ExchangeOrderStatus | None
    new_status: ExchangeOrderStatus
    source: ExchangeOrderEventSource
    occurred_at_ms: int
    reason_code: str
    exchange_timestamp_ms: int | None = None
    model_version: str = "exchange-order-event/v1"

    def __post_init__(self) -> None:
        require_text(self.event_id, "event_id")
        require_hash(self.durable_command_key, "durable_command_key")
        require_client_order_id(self.client_order_id)
        if self.previous_status is not None and not isinstance(
            self.previous_status, ExchangeOrderStatus
        ):
            raise ExchangeOrderStateError("previous_status must be an ExchangeOrderStatus")
        if not isinstance(self.new_status, ExchangeOrderStatus):
            raise ExchangeOrderStateError("new_status must be an ExchangeOrderStatus")
        if not isinstance(self.source, ExchangeOrderEventSource):
            raise ExchangeOrderStateError("source must be an ExchangeOrderEventSource")
        require_timestamp(self.occurred_at_ms, "occurred_at_ms")
        require_timestamp(self.exchange_timestamp_ms, "exchange_timestamp_ms", optional=True)
        require_text(self.reason_code, "reason_code")
        require_text(self.model_version, "model_version")


@dataclass(frozen=True, slots=True)
class ExchangeTradeIdentity:
    venue: str
    environment: ExecutionEnvironment
    account_fingerprint: str
    symbol: str
    exchange_order_id: str
    exchange_trade_id: str

    def __post_init__(self) -> None:
        for name in (
            "venue",
            "account_fingerprint",
            "symbol",
            "exchange_order_id",
            "exchange_trade_id",
        ):
            if name == "account_fingerprint":
                require_account_fingerprint(getattr(self, name))
            else:
                require_text(getattr(self, name), name)
        if self.venue != SUPPORTED_VENUE or self.environment is not ExecutionEnvironment.TESTNET:
            raise ExchangeExecutionConfigurationError("trade identity must be Binance Spot Testnet")
        if self.symbol != SUPPORTED_SYMBOL:
            raise ExchangeExecutionConfigurationError("trade identity symbol must be BTCUSDT")


@dataclass(frozen=True, slots=True)
class ExchangeTrade:
    identity: ExchangeTradeIdentity
    client_order_id: str
    stream_key: MarketStreamKey
    side: ExchangeOrderSide
    quantity: Decimal
    price: Decimal
    quote_quantity: Decimal
    commission: Decimal
    commission_asset: str
    traded_at_ms: int
    is_maker: bool | None = None
    model_version: str = "exchange-trade/v1"

    def __post_init__(self) -> None:
        if not isinstance(self.identity, ExchangeTradeIdentity):
            raise ExchangeExecutionInvariantError("identity must be an ExchangeTradeIdentity")
        require_text(self.client_order_id, "client_order_id")
        if (
            not isinstance(self.stream_key, MarketStreamKey)
            or self.stream_key.symbol != self.identity.symbol
        ):
            raise ExchangeExecutionInvariantError("trade stream does not match identity")
        if not isinstance(self.side, ExchangeOrderSide):
            raise ExchangeExecutionInvariantError("side must be an ExchangeOrderSide")
        require_decimal(self.quantity, "quantity", positive=True)
        require_decimal(self.price, "price", positive=True)
        require_decimal(self.quote_quantity, "quote_quantity", positive=True)
        require_decimal(self.commission, "commission", non_negative=True)
        require_text(self.commission_asset, "commission_asset")
        require_timestamp(self.traded_at_ms, "traded_at_ms")
        if self.is_maker is not None and not isinstance(self.is_maker, bool):
            raise ExchangeExecutionInvariantError("is_maker must be bool or None")
        require_text(self.model_version, "model_version")
        normalize_decimal_fields(self, ("quantity", "price", "quote_quantity", "commission"))


@dataclass(frozen=True, slots=True)
class TradeAggregate:
    executed_base_quantity: Decimal
    quote_quantity: Decimal
    commissions_by_asset: Mapping[str, Decimal]
    trade_count: int

    def __post_init__(self) -> None:
        require_decimal(self.executed_base_quantity, "executed_base_quantity", positive=True)
        require_decimal(self.quote_quantity, "quote_quantity", positive=True)
        if (
            isinstance(self.trade_count, bool)
            or not isinstance(self.trade_count, int)
            or self.trade_count <= 0
        ):
            raise ExchangeExecutionInvariantError("trade_count must be positive")
        frozen = dict(self.commissions_by_asset)
        for asset, value in frozen.items():
            require_text(asset, "commission asset")
            require_decimal(value, f"commission[{asset}]", non_negative=True)
            if value.is_zero() and value.is_signed():
                frozen[asset] = ZERO
        object.__setattr__(self, "commissions_by_asset", MappingProxyType(frozen))
        normalize_decimal_fields(self, ("executed_base_quantity", "quote_quantity"))


@dataclass(frozen=True, slots=True)
class PendingExposure:
    reserved_quote_amount: Decimal = ZERO
    reserved_base_quantity: Decimal = ZERO
    pending_notional: Decimal = ZERO
    original_base_quantity: Decimal = ZERO
    executed_base_quantity: Decimal = ZERO
    remaining_base_quantity: Decimal = ZERO
    order_count: int = 0
    inflight_submission_count: int = 0

    def __post_init__(self) -> None:
        for name in (
            "reserved_quote_amount",
            "reserved_base_quantity",
            "pending_notional",
            "original_base_quantity",
            "executed_base_quantity",
            "remaining_base_quantity",
        ):
            require_decimal(getattr(self, name), name, non_negative=True)
        if (
            self.original_base_quantity
            != self.executed_base_quantity + self.remaining_base_quantity
        ):
            raise ExchangeExecutionInvariantError(
                "original_base_quantity must equal executed plus remaining quantity"
            )
        for name in ("order_count", "inflight_submission_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ExchangeExecutionInvariantError(f"{name} must be non-negative")
        if self.inflight_submission_count > self.order_count:
            raise ExchangeExecutionInvariantError("in-flight submissions require an order")
        normalize_decimal_fields(
            self,
            (
                "reserved_quote_amount",
                "reserved_base_quantity",
                "pending_notional",
                "original_base_quantity",
                "executed_base_quantity",
                "remaining_base_quantity",
            ),
        )


@dataclass(frozen=True, slots=True)
class ExecutionSession:
    session_id: str
    account_fingerprint: str
    authorized_at_ms: int
    state: ExecutionSessionState
    cumulative_submitted_notional: Decimal = ZERO
    cumulative_executed_turnover: Decimal = ZERO
    submission_count: int = 0
    execution_failure_count: int = 0
    environment: ExecutionEnvironment = ExecutionEnvironment.TESTNET
    model_version: str = "execution-session/v1"

    def __post_init__(self) -> None:
        require_text(self.session_id, "session_id")
        require_account_fingerprint(self.account_fingerprint)
        require_timestamp(self.authorized_at_ms, "authorized_at_ms")
        if not isinstance(self.state, ExecutionSessionState):
            raise ExchangeExecutionInvariantError("state must be an ExecutionSessionState")
        if self.environment is not ExecutionEnvironment.TESTNET:
            raise ExchangeExecutionConfigurationError("execution sessions support TESTNET only")
        for name in ("cumulative_submitted_notional", "cumulative_executed_turnover"):
            require_decimal(getattr(self, name), name, non_negative=True)
        for name in ("submission_count", "execution_failure_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ExchangeExecutionInvariantError(f"{name} must be non-negative")
        require_text(self.model_version, "model_version")
        normalize_decimal_fields(
            self, ("cumulative_submitted_notional", "cumulative_executed_turnover")
        )


@dataclass(frozen=True, slots=True)
class KillSwitch:
    state: KillSwitchState
    generation: int
    reason_code: str
    changed_at_ms: int
    model_version: str = "kill-switch/v1"

    def __post_init__(self) -> None:
        if not isinstance(self.state, KillSwitchState):
            raise ExchangeExecutionInvariantError("state must be a KillSwitchState")
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 0
        ):
            raise ExchangeExecutionInvariantError("generation must be non-negative")
        require_text(self.reason_code, "reason_code")
        require_timestamp(self.changed_at_ms, "changed_at_ms")
        require_text(self.model_version, "model_version")
