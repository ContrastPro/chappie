from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable

from app.exchange_execution.exceptions import (
    ExchangeCapabilityError,
    ExchangeExecutionConfigurationError,
    ExchangeExecutionInvariantError,
)
from app.exchange_execution.models import (
    ExchangeOrder,
    ExchangeOrderSide,
    ExchangeOrderStatus,
    ExchangeOrderType,
    ExchangeResponseType,
    ExchangeTrade,
    ExecutionEnvironment,
    QueryResultKind,
    TransportFailureCategory,
    TransportOutcomeKind,
    require_account_fingerprint,
    require_client_order_id,
    require_decimal,
    require_hash,
    require_text,
)
from app.market.models import MarketStreamKey


@dataclass(frozen=True, slots=True)
class MarketOrderSubmission:
    durable_command_key: str
    client_order_id: str
    account_fingerprint: str
    stream_key: MarketStreamKey
    side: ExchangeOrderSide
    base_quantity: Decimal
    environment: ExecutionEnvironment = ExecutionEnvironment.TESTNET
    venue: str = "binance-spot"
    order_type: ExchangeOrderType = ExchangeOrderType.MARKET
    response_type: ExchangeResponseType = ExchangeResponseType.ACK

    def __post_init__(self) -> None:
        require_hash(self.durable_command_key, "durable_command_key")
        require_client_order_id(self.client_order_id)
        require_account_fingerprint(self.account_fingerprint)
        if not isinstance(self.stream_key, MarketStreamKey) or self.stream_key.symbol != "BTCUSDT":
            raise ExchangeExecutionConfigurationError("initial submission stream must be BTCUSDT")
        if not isinstance(self.side, ExchangeOrderSide):
            raise ExchangeExecutionInvariantError("side must be an ExchangeOrderSide")
        require_decimal(self.base_quantity, "base_quantity", positive=True)
        if self.environment is not ExecutionEnvironment.TESTNET or self.venue != "binance-spot":
            raise ExchangeExecutionConfigurationError(
                "submission supports Binance Spot Testnet only"
            )
        if (
            self.order_type is not ExchangeOrderType.MARKET
            or self.response_type is not ExchangeResponseType.ACK
        ):
            raise ExchangeExecutionConfigurationError(
                "submission requires MARKET with ACK response"
            )


@dataclass(frozen=True, slots=True)
class SubmissionObservation:
    transport_outcome: TransportOutcomeKind
    client_order_id: str
    exchange_order_id: str | None = None
    exchange_status: ExchangeOrderStatus | None = None
    transact_time_ms: int | None = None
    failure_category: TransportFailureCategory | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.transport_outcome, TransportOutcomeKind):
            raise ExchangeExecutionInvariantError(
                "transport_outcome must be a TransportOutcomeKind"
            )
        require_client_order_id(self.client_order_id)
        if self.exchange_order_id is not None:
            require_text(self.exchange_order_id, "exchange_order_id")
        if self.exchange_status is not None and not isinstance(
            self.exchange_status, ExchangeOrderStatus
        ):
            raise ExchangeExecutionInvariantError(
                "exchange_status must be an ExchangeOrderStatus or None"
            )
        if self.transact_time_ms is not None and (
            isinstance(self.transact_time_ms, bool)
            or not isinstance(self.transact_time_ms, int)
            or self.transact_time_ms < 0
        ):
            raise ExchangeExecutionInvariantError(
                "transact_time_ms must be a non-negative integer or None"
            )
        if self.transport_outcome is TransportOutcomeKind.MAY_HAVE_BEEN_TRANSMITTED:
            if not isinstance(self.failure_category, TransportFailureCategory):
                raise ExchangeExecutionInvariantError(
                    "ambiguous transport requires a failure category"
                )
        elif self.failure_category is not None:
            raise ExchangeExecutionInvariantError(
                "failure_category is only valid for ambiguous transport"
            )


@dataclass(frozen=True, slots=True)
class OrderQueryResult:
    kind: QueryResultKind
    order: ExchangeOrder | None = None

    def __post_init__(self) -> None:
        if self.kind is QueryResultKind.FOUND and self.order is None:
            raise ExchangeExecutionInvariantError("FOUND requires an order")
        if self.kind is not QueryResultKind.FOUND and self.order is not None:
            raise ExchangeExecutionInvariantError("only FOUND may contain an order")


@dataclass(frozen=True, slots=True)
class AccountBalance:
    asset: str
    free: Decimal
    locked: Decimal

    def __post_init__(self) -> None:
        require_text(self.asset, "asset")
        require_decimal(self.free, "free", non_negative=True)
        require_decimal(self.locked, "locked", non_negative=True)


@dataclass(frozen=True, slots=True)
class ExchangeInstrumentRules:
    stream_key: MarketStreamKey
    quantity_step: Decimal
    min_quantity: Decimal
    min_notional: Decimal
    rules_version: str

    def __post_init__(self) -> None:
        if not isinstance(self.stream_key, MarketStreamKey) or self.stream_key.symbol != "BTCUSDT":
            raise ExchangeExecutionConfigurationError("instrument rules must identify BTCUSDT")
        for name in ("quantity_step", "min_quantity", "min_notional"):
            require_decimal(getattr(self, name), name, positive=True)
        require_text(self.rules_version, "rules_version")


@dataclass(frozen=True, slots=True)
class ExchangeTradeHistory:
    trades: tuple[ExchangeTrade, ...]
    coverage_start_ms: int
    coverage_end_ms: int
    complete: bool

    def __post_init__(self) -> None:
        if not isinstance(self.trades, tuple) or any(
            not isinstance(value, ExchangeTrade) for value in self.trades
        ):
            raise ExchangeExecutionInvariantError("trade history must contain ExchangeTrade values")
        for value, name in (
            (self.coverage_start_ms, "coverage_start_ms"),
            (self.coverage_end_ms, "coverage_end_ms"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ExchangeExecutionInvariantError(f"{name} must be non-negative")
        if self.coverage_end_ms < self.coverage_start_ms:
            raise ExchangeExecutionInvariantError("trade history coverage is reversed")
        if not isinstance(self.complete, bool):
            raise ExchangeExecutionInvariantError("trade history complete flag must be bool")


@runtime_checkable
class ExchangeOrderGateway(Protocol):
    def submit_market_order(self, request: MarketOrderSubmission) -> SubmissionObservation: ...


@runtime_checkable
class ExchangeAccountReader(Protocol):
    def query_order(
        self, *, stream_key: MarketStreamKey, client_order_id: str
    ) -> OrderQueryResult: ...

    def list_order_trades(
        self, *, stream_key: MarketStreamKey, exchange_order_id: str
    ) -> tuple[ExchangeTrade, ...]: ...

    def list_open_orders(self, *, stream_key: MarketStreamKey) -> tuple[ExchangeOrder, ...]: ...

    def read_trade_history(
        self, *, stream_key: MarketStreamKey, start_at_ms: int
    ) -> ExchangeTradeHistory: ...

    def read_balances(self, assets: tuple[str, ...]) -> tuple[AccountBalance, ...]: ...

    def read_instrument_rules(self, stream_key: MarketStreamKey) -> ExchangeInstrumentRules: ...


class SubmissionCapability:
    """Opaque process-local authority; deliberately non-serializable."""

    __slots__ = (
        "_nonce",
        "account_fingerprint",
        "control_generation",
        "environment",
        "reconciliation_generation",
        "session_id",
    )

    _ISSUER = object()

    def __init__(
        self,
        *,
        reconciliation_generation: int,
        control_generation: int,
        account_fingerprint: str = "legacy-process-scope",
        session_id: str = "legacy-session",
        environment: ExecutionEnvironment = ExecutionEnvironment.TESTNET,
        _issuer: object | None = None,
    ) -> None:
        if _issuer is not self._ISSUER:
            raise ExchangeCapabilityError(
                "SubmissionCapability can only be issued by the control service"
            )
        for value in (reconciliation_generation, control_generation):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ExchangeCapabilityError(
                    "capability generations must be non-negative integers"
                )
        self._nonce = object()
        require_account_fingerprint(account_fingerprint)
        require_text(session_id, "session_id")
        if environment is not ExecutionEnvironment.TESTNET:
            raise ExchangeCapabilityError("capability supports TESTNET only")
        self.account_fingerprint = account_fingerprint
        self.session_id = session_id
        self.environment = environment
        self.reconciliation_generation = reconciliation_generation
        self.control_generation = control_generation

    @classmethod
    def _issue(
        cls,
        *,
        reconciliation_generation: int,
        control_generation: int,
        account_fingerprint: str = "legacy-process-scope",
        session_id: str = "legacy-session",
    ) -> "SubmissionCapability":
        return cls(
            reconciliation_generation=reconciliation_generation,
            control_generation=control_generation,
            account_fingerprint=account_fingerprint,
            session_id=session_id,
            _issuer=cls._ISSUER,
        )

    def __copy__(self) -> object:
        raise TypeError("SubmissionCapability cannot be copied")

    def __deepcopy__(self, memo: object) -> object:
        raise TypeError("SubmissionCapability cannot be copied")

    def __reduce__(self) -> object:
        raise TypeError("SubmissionCapability is process-local and cannot be serialized")


class PreSubmitAuthorization:
    """Opaque, single-use process authority for one exact prepared order."""

    __slots__ = (
        "_consumed",
        "_nonce",
        "account_fingerprint",
        "client_order_id",
        "control_generation",
        "durable_command_key",
        "environment",
        "execution_root_id",
        "reconciliation_generation",
        "risk_decision_id",
        "rules_fingerprint",
        "session_id",
    )
    _ISSUER = object()

    def __init__(
        self,
        *,
        execution_root_id: str,
        session_id: str,
        account_fingerprint: str,
        durable_command_key: str,
        client_order_id: str,
        risk_decision_id: str,
        rules_fingerprint: str,
        reconciliation_generation: int,
        control_generation: int,
        _issuer: object | None = None,
    ) -> None:
        if _issuer is not self._ISSUER:
            raise ExchangeCapabilityError("pre-submit authorization requires the final gate")
        for value, name in (
            (execution_root_id, "execution_root_id"),
            (session_id, "session_id"),
            (risk_decision_id, "risk_decision_id"),
        ):
            require_text(value, name)
        require_account_fingerprint(account_fingerprint)
        require_hash(durable_command_key, "durable_command_key")
        require_client_order_id(client_order_id)
        require_hash(rules_fingerprint, "rules_fingerprint")
        for value in (reconciliation_generation, control_generation):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ExchangeCapabilityError("authorization generations must be non-negative")
        self._nonce = object()
        self._consumed = False
        self.execution_root_id = execution_root_id
        self.session_id = session_id
        self.account_fingerprint = account_fingerprint
        self.durable_command_key = durable_command_key
        self.client_order_id = client_order_id
        self.risk_decision_id = risk_decision_id
        self.rules_fingerprint = rules_fingerprint
        self.reconciliation_generation = reconciliation_generation
        self.control_generation = control_generation
        self.environment = ExecutionEnvironment.TESTNET

    @classmethod
    def _issue(cls, **values: object) -> "PreSubmitAuthorization":
        return cls(**values, _issuer=cls._ISSUER)  # type: ignore[arg-type]

    def _consume(self) -> None:
        if self._consumed:
            raise ExchangeCapabilityError("pre-submit authorization was already consumed")
        self._consumed = True

    def __copy__(self) -> object:
        raise TypeError("PreSubmitAuthorization cannot be copied")

    def __deepcopy__(self, memo: object) -> object:
        raise TypeError("PreSubmitAuthorization cannot be copied")

    def __reduce__(self) -> object:
        raise TypeError("PreSubmitAuthorization cannot be serialized")
