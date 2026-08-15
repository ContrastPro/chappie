import hashlib
import json
from dataclasses import asdict, dataclass
from decimal import Decimal, localcontext

from app.exchange_execution.exceptions import ExchangeCapabilityError, ExchangeExecutionError
from app.exchange_execution.models import (
    ExchangeCommand,
    ExchangeOrderSide,
    ExecutionEnvironment,
    require_account_fingerprint,
    require_hash,
    require_text,
    require_timestamp,
)
from app.exchange_execution.ports import ExchangeInstrumentRules, quantity_is_step_aligned


class Stage4ManagedSellReviewError(ExchangeExecutionError):
    pass


class Stage4ManagedSellQuantityMismatchError(Stage4ManagedSellReviewError):
    pass


class Stage4ManagedSellQuantityNotTradableError(Stage4ManagedSellReviewError):
    pass


class Stage4ManagedSellMaxQuantityExceededError(Stage4ManagedSellReviewError):
    pass


STAGE4_SELL_POLICY_VERSION = "stage4-managed-full-exit/v1"
STAGE4_SELL_SUCCESSOR_POLICY_VERSION = "stage4-managed-sell-successor/v1"


def _decimal(value: object, name: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or isinstance(value, float):
        raise Stage4ManagedSellReviewError(f"{name} must be a finite Decimal")
    if positive and value <= 0:
        raise Stage4ManagedSellReviewError(f"{name} must be positive")
    return value


def _canonical(value: object) -> object:
    if isinstance(value, Decimal):
        if value.is_zero():
            text = "0"
        else:
            text = format(value, "f")
            if "." in text:
                text = text.rstrip("0").rstrip(".")
        return {"$decimal": text}
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in sorted(value.items())}
    return value


def _digest(value: object, version: str) -> str:
    payload = json.dumps(
        {"algorithm_version": version, "payload": _canonical(value)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def derive_stage4_sell_economic_slot(
    execution_root_id: str,
    account_fingerprint: str,
    portfolio_id: str,
    predecessor_portfolio_version: int,
    predecessor_portfolio_digest: str,
    source_buy_trade_id: str,
) -> str:
    require_text(execution_root_id, "execution_root_id")
    require_account_fingerprint(account_fingerprint)
    require_text(portfolio_id, "portfolio_id")
    if (
        isinstance(predecessor_portfolio_version, bool)
        or not isinstance(predecessor_portfolio_version, int)
        or predecessor_portfolio_version < 1
    ):
        raise Stage4ManagedSellReviewError("predecessor Portfolio version is invalid")
    require_hash(predecessor_portfolio_digest, "predecessor_portfolio_digest")
    require_text(source_buy_trade_id, "source_buy_trade_id")
    return _digest(
        {
            "root": execution_root_id,
            "account": account_fingerprint,
            "portfolio": portfolio_id,
            "portfolio_version": predecessor_portfolio_version,
            "portfolio_digest": predecessor_portfolio_digest,
            "source_buy_trade": source_buy_trade_id,
            "slot": "stage4-managed-full-exit",
            "policy": STAGE4_SELL_POLICY_VERSION,
        },
        "stage4-managed-sell-slot/v1",
    )


def derive_stage4_sell_successor_economic_slot(
    predecessor_economic_slot_key: str, successor_generation: int
) -> str:
    """Derive the one reviewed successor slot; callers cannot supply slot entropy."""
    require_hash(predecessor_economic_slot_key, "predecessor_economic_slot_key")
    if successor_generation != 1:
        raise Stage4ManagedSellReviewError("only managed SELL successor generation 1 is supported")
    return _digest(
        {
            "predecessor_slot": predecessor_economic_slot_key,
            "successor_generation": successor_generation,
            "policy": STAGE4_SELL_SUCCESSOR_POLICY_VERSION,
        },
        "stage4-managed-sell-successor-slot/v1",
    )


@dataclass(frozen=True, slots=True)
class Stage4ManagedSellRiskEnvelope:
    quantity: Decimal
    reviewed_notional: Decimal
    predecessor_submitted_notional: Decimal
    predecessor_turnover: Decimal
    max_incremental_submissions: int = 1
    max_new_non_terminal: int = 1
    max_new_in_flight: int = 1
    symbol: str = "BTCUSDT"
    side: str = "SELL"
    order_type: str = "MARKET"
    policy_version: str = STAGE4_SELL_POLICY_VERSION

    def __post_init__(self) -> None:
        for name in (
            "quantity",
            "reviewed_notional",
            "predecessor_submitted_notional",
            "predecessor_turnover",
        ):
            _decimal(getattr(self, name), name, positive=name in {"quantity", "reviewed_notional"})
        if (
            self.max_incremental_submissions,
            self.max_new_non_terminal,
            self.max_new_in_flight,
        ) != (
            1,
            1,
            1,
        ):
            raise Stage4ManagedSellReviewError("SELL closure count limits must all equal one")
        if (self.symbol, self.side, self.order_type, self.policy_version) != (
            "BTCUSDT",
            "SELL",
            "MARKET",
            STAGE4_SELL_POLICY_VERSION,
        ):
            raise Stage4ManagedSellReviewError("unsupported SELL closure envelope")

    @property
    def digest(self) -> str:
        return _digest(asdict(self), "stage4-managed-sell-envelope/v1")


@dataclass(frozen=True, slots=True)
class ReviewedStage4ManagedSellRequest:
    authority_id: str
    operator_run_id: str
    execution_root_id: str
    session_id: str
    account_fingerprint: str
    portfolio_id: str
    portfolio_version: int
    portfolio_digest: str
    exact_managed_quantity: Decimal
    expected_submission_count: int
    permitted_submission_count: int
    control_generation: int
    reconciliation_generation: int
    settlement_state_digest: str
    source_buy_command_key: str
    source_buy_trade_id: str
    economic_slot_key: str
    rules: ExchangeInstrumentRules
    risk_fingerprint: str
    config_fingerprint: str
    ownership_scope_digest: str
    command: ExchangeCommand
    envelope: Stage4ManagedSellRiskEnvelope
    created_at_ms: int
    policy_version: str = STAGE4_SELL_POLICY_VERSION
    predecessor_authority_id: str | None = None
    predecessor_economic_slot_key: str | None = None
    successor_generation: int = 0

    def __post_init__(self) -> None:
        for name in (
            "authority_id",
            "operator_run_id",
            "execution_root_id",
            "session_id",
            "portfolio_id",
            "source_buy_trade_id",
        ):
            require_text(getattr(self, name), name)
        require_account_fingerprint(self.account_fingerprint)
        for name in (
            "portfolio_digest",
            "settlement_state_digest",
            "source_buy_command_key",
            "economic_slot_key",
            "risk_fingerprint",
            "config_fingerprint",
            "ownership_scope_digest",
        ):
            require_hash(getattr(self, name), name)
        require_timestamp(self.created_at_ms, "created_at_ms")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (
                self.portfolio_version,
                self.expected_submission_count,
                self.permitted_submission_count,
                self.control_generation,
                self.reconciliation_generation,
            )
        ):
            raise Stage4ManagedSellReviewError("SELL predecessor counters are invalid")
        if (self.expected_submission_count, self.permitted_submission_count) != (1, 2):
            raise Stage4ManagedSellReviewError(
                "SELL requires the sole BUY predecessor and permits only submission two"
            )
        quantity = _decimal(self.exact_managed_quantity, "exact_managed_quantity", positive=True)
        if not isinstance(self.rules, ExchangeInstrumentRules):
            raise Stage4ManagedSellReviewError("SELL rules are invalid")
        if self.command.side is not ExchangeOrderSide.SELL:
            raise Stage4ManagedSellReviewError("SELL authority requires a SELL command")
        if self.command.base_quantity != quantity or self.envelope.quantity != quantity:
            raise Stage4ManagedSellQuantityMismatchError(
                "SELL quantity must equal exact managed inventory"
            )
        with localcontext() as context:
            context.prec = max(
                50,
                len(quantity.as_tuple().digits)
                + len(self.command.reference_price.as_tuple().digits)
                + abs(quantity.as_tuple().exponent)
                + abs(self.command.reference_price.as_tuple().exponent)
                + 4,
            )
            exact_reviewed_notional = quantity * self.command.reference_price
        if (
            self.command.approved_target_exposure != 0
            or self.command.approved_notional != self.envelope.reviewed_notional
            or self.command.approved_notional != exact_reviewed_notional
        ):
            raise Stage4ManagedSellReviewError(
                "SELL reviewed notional and full-exit command must be exact"
            )
        if quantity < self.rules.min_quantity or not quantity_is_step_aligned(
            quantity, self.rules.quantity_step
        ):
            raise Stage4ManagedSellQuantityNotTradableError(
                "managed quantity is not exactly tradable"
            )
        if self.rules.max_quantity is not None and quantity > self.rules.max_quantity:
            raise Stage4ManagedSellMaxQuantityExceededError("managed quantity exceeds maxQty")
        if self.command.approved_notional < self.rules.min_notional:
            raise Stage4ManagedSellReviewError("managed quantity is below minNotional")
        if self.command.instrument_rules_fingerprint != self.rules.rules_version:
            raise Stage4ManagedSellReviewError("SELL command rules fingerprint mismatch")
        if self.command.risk_authorization_fingerprint != self.risk_fingerprint:
            raise Stage4ManagedSellReviewError("SELL command risk fingerprint mismatch")
        if self.command.execution_configuration_fingerprint != self.config_fingerprint:
            raise Stage4ManagedSellReviewError("SELL command config fingerprint mismatch")
        if self.successor_generation == 0:
            if (
                self.predecessor_authority_id is not None
                or self.predecessor_economic_slot_key is not None
            ):
                raise Stage4ManagedSellReviewError("original SELL cannot carry successor lineage")
            expected_slot = derive_stage4_sell_economic_slot(
                self.execution_root_id,
                self.account_fingerprint,
                self.portfolio_id,
                self.portfolio_version,
                self.portfolio_digest,
                self.source_buy_trade_id,
            )
        elif self.successor_generation == 1:
            if self.predecessor_authority_id is None:
                raise Stage4ManagedSellReviewError("successor SELL requires predecessor authority")
            require_text(self.predecessor_authority_id, "predecessor_authority_id")
            if self.predecessor_economic_slot_key is None:
                raise Stage4ManagedSellReviewError("successor SELL requires predecessor slot")
            expected_slot = derive_stage4_sell_successor_economic_slot(
                self.predecessor_economic_slot_key, self.successor_generation
            )
        else:
            raise Stage4ManagedSellReviewError("unsupported managed SELL successor generation")
        if self.economic_slot_key != expected_slot:
            raise Stage4ManagedSellReviewError("SELL economic slot is not deterministic")
        if self.policy_version != STAGE4_SELL_POLICY_VERSION:
            raise Stage4ManagedSellReviewError("unsupported SELL authority policy")


@dataclass(frozen=True, slots=True)
class Stage4ManagedSellAuthority:
    reviewed_request: ReviewedStage4ManagedSellRequest
    consumed_durable_command_key: str
    consumed_at_ms: int
    payload_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.reviewed_request, ReviewedStage4ManagedSellRequest):
            raise Stage4ManagedSellReviewError("authority requires a reviewed SELL request")
        require_hash(self.consumed_durable_command_key, "consumed_durable_command_key")
        require_timestamp(self.consumed_at_ms, "consumed_at_ms")
        require_hash(self.payload_digest, "payload_digest")
        if self.consumed_at_ms < self.reviewed_request.created_at_ms:
            raise Stage4ManagedSellReviewError("authority consumption precedes creation")


@dataclass(frozen=True, slots=True)
class Stage4ManagedSellAuthorityEvent:
    event_id: str
    authority_id: str
    durable_command_key: str
    operator_run_id: str
    occurred_at_ms: int
    payload_digest: str
    event_ordinal: int = 1
    event_type: str = "CONSUMED"

    def __post_init__(self) -> None:
        for name in ("event_id", "authority_id", "operator_run_id"):
            require_text(getattr(self, name), name)
        require_hash(self.durable_command_key, "durable_command_key")
        require_hash(self.payload_digest, "payload_digest")
        require_timestamp(self.occurred_at_ms, "occurred_at_ms")
        if self.event_ordinal != 1 or self.event_type != "CONSUMED":
            raise Stage4ManagedSellReviewError("unsupported SELL authority event")


class Stage4ManagedSellProcessClaim:
    __slots__ = (
        "_consumed",
        "_nonce",
        "account_fingerprint",
        "authority_id",
        "command_key",
        "config_fingerprint",
        "control_generation",
        "economic_slot_key",
        "envelope_digest",
        "environment",
        "execution_root_id",
        "ownership_scope_digest",
        "portfolio_digest",
        "portfolio_id",
        "portfolio_version",
        "reconciliation_generation",
        "risk_fingerprint",
        "rules_fingerprint",
        "session_id",
        "settlement_state_digest",
    )
    _ISSUER = object()

    def __init__(
        self, request: ReviewedStage4ManagedSellRequest, *, _issuer: object | None = None
    ) -> None:
        if _issuer is not self._ISSUER:
            raise ExchangeCapabilityError("SELL claim can only be issued by persistence")
        self._nonce = object()
        self._consumed = False
        self.authority_id = request.authority_id
        self.command_key = request.command.durable_command_key
        self.execution_root_id = request.execution_root_id
        self.session_id = request.session_id
        self.account_fingerprint = request.account_fingerprint
        self.portfolio_id = request.portfolio_id
        self.portfolio_version = request.portfolio_version
        self.portfolio_digest = request.portfolio_digest
        self.control_generation = request.control_generation
        self.reconciliation_generation = request.reconciliation_generation
        self.settlement_state_digest = request.settlement_state_digest
        self.economic_slot_key = request.economic_slot_key
        self.rules_fingerprint = request.rules.rules_version
        self.risk_fingerprint = request.risk_fingerprint
        self.config_fingerprint = request.config_fingerprint
        self.envelope_digest = request.envelope.digest
        self.ownership_scope_digest = request.ownership_scope_digest
        self.environment = ExecutionEnvironment.TESTNET

    @classmethod
    def _issue(cls, request: ReviewedStage4ManagedSellRequest) -> "Stage4ManagedSellProcessClaim":
        return cls(request, _issuer=cls._ISSUER)

    def _consume(self) -> None:
        if self._consumed:
            raise ExchangeCapabilityError("SELL claim has already been consumed")
        self._consumed = True

    def __copy__(self):
        raise TypeError("SELL claim cannot be copied")

    def __deepcopy__(self, memo):
        raise TypeError("SELL claim cannot be copied")

    def __reduce__(self):
        raise TypeError("SELL claim is process-local")

    def __getstate__(self):
        raise TypeError("SELL claim is process-local")


@dataclass(frozen=True, slots=True)
class Stage4ManagedSellClaimReceipt:
    authority_id: str
    durable_command_key: str
    client_order_id: str
    claim: Stage4ManagedSellProcessClaim | None
    recovered: bool = False
