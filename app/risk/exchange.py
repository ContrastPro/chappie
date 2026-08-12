from dataclasses import dataclass
from decimal import Decimal, localcontext
from enum import StrEnum

from app.exchange_execution.models import (
    ExchangeOrderSide,
    ExecutionEnvironment,
    ExecutionSession,
    ExecutionSessionState,
    KillSwitch,
    KillSwitchState,
    PendingExposure,
)
from app.exchange_execution.ports import SubmissionCapability
from app.market.models import MarketStreamKey
from app.portfolio.models import PortfolioSnapshot, decimal_precision
from app.risk.exceptions import RiskConfigurationError, RiskInvariantError
from app.risk.models import ApprovedOrderParameters, RiskOrderSide
from app.trading.models import RiskDecision, RiskDecisionOutcome

ZERO = Decimal(0)


def _decimal(
    value: object, name: str, error: type[Exception], *, positive: bool = False
) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise error(f"{name} must be a finite Decimal")
    if positive and value <= ZERO:
        raise error(f"{name} must be positive")
    if not positive and value < ZERO:
        raise error(f"{name} must not be negative")
    return ZERO if value.is_zero() else value


def _integer(value: object, name: str, error: type[Exception], *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise error(f"{name} must be an integer")
    if (positive and value <= 0) or (not positive and value < 0):
        qualifier = "positive" if positive else "non-negative"
        raise error(f"{name} must be {qualifier}")
    return value


def _text(value: object, name: str, error: type[Exception]) -> str:
    if not isinstance(value, str) or not value.strip():
        raise error(f"{name} must be a non-empty string")
    return value


def _add(left: Decimal, right: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = decimal_precision(left, right, guard_digits=50)
        return left + right


def _subtract(left: Decimal, right: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = decimal_precision(left, right, guard_digits=50)
        return left - right


def _multiply(left: Decimal, right: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = decimal_precision(left, right, guard_digits=50)
        return left * right


class ExchangeReconciliationStatus(StrEnum):
    SAFE = "SAFE"
    REQUIRED = "REQUIRED"
    CONFLICT = "CONFLICT"
    FAILED = "FAILED"


class ExchangeRiskOutcome(StrEnum):
    APPROVED = "APPROVED"
    BLOCKED = "BLOCKED"


class ExchangeRiskReason(StrEnum):
    RISK_NOT_APPROVED = "RISK_NOT_APPROVED"
    SESSION_NOT_ACTIVE = "SESSION_NOT_ACTIVE"
    NON_TERMINAL_ORDER_LIMIT = "NON_TERMINAL_ORDER_LIMIT"
    IN_FLIGHT_SUBMISSION_LIMIT = "IN_FLIGHT_SUBMISSION_LIMIT"
    QUOTE_RESERVATION_EXCEEDED = "QUOTE_RESERVATION_EXCEEDED"
    BASE_RESERVATION_EXCEEDED = "BASE_RESERVATION_EXCEEDED"
    POSITION_LIMIT = "POSITION_LIMIT"
    ORDER_NOTIONAL_LIMIT = "ORDER_NOTIONAL_LIMIT"
    SESSION_SUBMITTED_NOTIONAL_LIMIT = "SESSION_SUBMITTED_NOTIONAL_LIMIT"
    SESSION_TURNOVER_LIMIT = "SESSION_TURNOVER_LIMIT"
    SESSION_SUBMISSION_COUNT_LIMIT = "SESSION_SUBMISSION_COUNT_LIMIT"
    SESSION_FAILURE_LIMIT = "SESSION_FAILURE_LIMIT"
    DRAWDOWN_LIMIT = "DRAWDOWN_LIMIT"


class ExchangeSubmissionReason(StrEnum):
    UNSUPPORTED_ENVIRONMENT = "UNSUPPORTED_ENVIRONMENT"
    FINANCIAL_RISK_BLOCKED = "FINANCIAL_RISK_BLOCKED"
    KILL_SWITCH_ACTIVE = "KILL_SWITCH_ACTIVE"
    RECONCILIATION_NOT_SAFE = "RECONCILIATION_NOT_SAFE"
    CAPABILITY_MISSING = "CAPABILITY_MISSING"
    RECONCILIATION_GENERATION_MISMATCH = "RECONCILIATION_GENERATION_MISMATCH"
    CONTROL_GENERATION_MISMATCH = "CONTROL_GENERATION_MISMATCH"
    PENDING_CONTROL_UNSAFE = "PENDING_CONTROL_UNSAFE"
    CAPABILITY_ACCOUNT_MISMATCH = "CAPABILITY_ACCOUNT_MISMATCH"
    CAPABILITY_SESSION_MISMATCH = "CAPABILITY_SESSION_MISMATCH"
    CAPABILITY_ENVIRONMENT_MISMATCH = "CAPABILITY_ENVIRONMENT_MISMATCH"


@dataclass(frozen=True, slots=True)
class ExchangeRiskPolicy:
    max_order_notional: Decimal
    max_position_notional: Decimal
    max_session_submitted_notional: Decimal
    max_session_executed_turnover: Decimal
    max_session_submission_count: int
    max_session_execution_failures: int
    max_drawdown: Decimal
    min_cash_reserve: Decimal = ZERO
    max_non_terminal_orders: int = 1
    max_in_flight_submissions: int = 1
    policy_version: str = "exchange-risk/v1"

    def __post_init__(self) -> None:
        for name in (
            "max_order_notional",
            "max_position_notional",
            "max_session_submitted_notional",
            "max_session_executed_turnover",
            "max_drawdown",
        ):
            object.__setattr__(
                self,
                name,
                _decimal(getattr(self, name), name, RiskConfigurationError, positive=True),
            )
        object.__setattr__(
            self,
            "min_cash_reserve",
            _decimal(self.min_cash_reserve, "min_cash_reserve", RiskConfigurationError),
        )
        for name in (
            "max_session_submission_count",
            "max_session_execution_failures",
            "max_non_terminal_orders",
            "max_in_flight_submissions",
        ):
            _integer(getattr(self, name), name, RiskConfigurationError, positive=True)
        if self.max_non_terminal_orders != 1 or self.max_in_flight_submissions != 1:
            raise RiskConfigurationError("initial exchange order and in-flight limits must equal 1")
        if self.max_position_notional < self.max_order_notional:
            raise RiskConfigurationError("max_position_notional must cover max_order_notional")
        _text(self.policy_version, "policy_version", RiskConfigurationError)


@dataclass(frozen=True, slots=True)
class ReconciledAccountState:
    account_fingerprint: str
    stream_key: MarketStreamKey
    base_asset: str
    quote_asset: str
    free_base: Decimal
    locked_base: Decimal
    free_quote: Decimal
    locked_quote: Decimal
    status: ExchangeReconciliationStatus
    reconciliation_generation: int

    def __post_init__(self) -> None:
        for name in ("account_fingerprint", "base_asset", "quote_asset"):
            _text(getattr(self, name), name, RiskInvariantError)
        if not isinstance(self.stream_key, MarketStreamKey):
            raise RiskInvariantError("stream_key must be a MarketStreamKey")
        for name in ("free_base", "locked_base", "free_quote", "locked_quote"):
            object.__setattr__(self, name, _decimal(getattr(self, name), name, RiskInvariantError))
        if not isinstance(self.status, ExchangeReconciliationStatus):
            raise RiskInvariantError("status must be an ExchangeReconciliationStatus")
        _integer(
            self.reconciliation_generation,
            "reconciliation_generation",
            RiskInvariantError,
        )


@dataclass(frozen=True, slots=True)
class ExchangeRiskContext:
    account: ReconciledAccountState
    pending_exposure: PendingExposure
    execution_session: ExecutionSession
    pending_side: ExchangeOrderSide | None
    non_terminal_order_count: int
    in_flight_submission_count: int
    control_generation: int
    pending_buy_remaining_base: Decimal | None = None
    pending_sell_remaining_base: Decimal | None = None
    context_version: str = "exchange-risk-context/v1"

    def __post_init__(self) -> None:
        if not isinstance(self.account, ReconciledAccountState):
            raise RiskInvariantError("account must be ReconciledAccountState")
        if not isinstance(self.pending_exposure, PendingExposure):
            raise RiskInvariantError("pending_exposure must be PendingExposure")
        if not isinstance(self.execution_session, ExecutionSession):
            raise RiskInvariantError("execution_session must be ExecutionSession")
        if self.pending_side is not None and not isinstance(self.pending_side, ExchangeOrderSide):
            raise RiskInvariantError("pending_side must be ExchangeOrderSide or None")
        for name in (
            "non_terminal_order_count",
            "in_flight_submission_count",
            "control_generation",
        ):
            _integer(getattr(self, name), name, RiskInvariantError)
        if self.non_terminal_order_count != self.pending_exposure.order_count:
            raise RiskInvariantError("non-terminal count must match pending exposure")
        if self.in_flight_submission_count != self.pending_exposure.inflight_submission_count:
            raise RiskInvariantError("in-flight count must match pending exposure")
        if self.non_terminal_order_count == 0 and self.pending_side is not None:
            raise RiskInvariantError("pending_side requires a non-terminal order")
        if (
            self.non_terminal_order_count > 0
            and self.pending_side is None
            and (
                self.pending_buy_remaining_base is None or self.pending_sell_remaining_base is None
            )
        ):
            raise RiskInvariantError(
                "non-terminal order requires pending_side or explicit side aggregates"
            )
        for name in ("pending_buy_remaining_base", "pending_sell_remaining_base"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _decimal(value, name, RiskInvariantError))
        explicit_buy = self.pending_buy_remaining_base
        explicit_sell = self.pending_sell_remaining_base
        if (explicit_buy is None) != (explicit_sell is None):
            raise RiskInvariantError("pending side aggregates must be provided together")
        if explicit_buy is not None and explicit_sell is not None:
            if self.pending_side is not None:
                raise RiskInvariantError("pending_side and explicit side aggregates are exclusive")
            if _add(explicit_buy, explicit_sell) != self.pending_exposure.remaining_base_quantity:
                raise RiskInvariantError("pending side aggregates must equal remaining exposure")
        if self.execution_session.account_fingerprint != self.account.account_fingerprint:
            raise RiskInvariantError("session/account lineage mismatch")
        if self.account.stream_key.symbol != "BTCUSDT":
            raise RiskInvariantError("initial exchange risk stream must be BTCUSDT")
        _text(self.context_version, "context_version", RiskInvariantError)


@dataclass(frozen=True, slots=True)
class ExchangeRiskAudit:
    effective_available_quote: Decimal
    effective_available_base: Decimal
    pending_buy_remaining_base: Decimal
    pending_sell_remaining_base: Decimal
    candidate_quantity: Decimal
    candidate_notional: Decimal
    projected_position_quantity: Decimal
    projected_position_notional: Decimal
    projected_session_submitted_notional: Decimal
    projected_session_turnover: Decimal
    projected_submission_count: int
    applied_policy: ExchangeRiskPolicy

    def __post_init__(self) -> None:
        for name in (
            "effective_available_quote",
            "effective_available_base",
            "pending_buy_remaining_base",
            "pending_sell_remaining_base",
            "candidate_quantity",
            "candidate_notional",
            "projected_position_quantity",
            "projected_position_notional",
            "projected_session_submitted_notional",
            "projected_session_turnover",
        ):
            object.__setattr__(self, name, _decimal(getattr(self, name), name, RiskInvariantError))
        _integer(self.projected_submission_count, "projected_submission_count", RiskInvariantError)
        if not isinstance(self.applied_policy, ExchangeRiskPolicy):
            raise RiskInvariantError("applied_policy must be an ExchangeRiskPolicy")


@dataclass(frozen=True, slots=True)
class ExchangeRiskAssessment:
    outcome: ExchangeRiskOutcome
    source_risk_outcome: RiskDecisionOutcome
    risk_decision_id: str
    session_id: str
    reasons: tuple[ExchangeRiskReason, ...]
    audit: ExchangeRiskAudit | None

    def __post_init__(self) -> None:
        _text(self.risk_decision_id, "risk_decision_id", RiskInvariantError)
        _text(self.session_id, "session_id", RiskInvariantError)
        if not isinstance(self.outcome, ExchangeRiskOutcome):
            raise RiskInvariantError("outcome must be ExchangeRiskOutcome")
        if not isinstance(self.source_risk_outcome, RiskDecisionOutcome):
            raise RiskInvariantError("source_risk_outcome must be RiskDecisionOutcome")
        if not isinstance(self.reasons, tuple) or any(
            not isinstance(reason, ExchangeRiskReason) for reason in self.reasons
        ):
            raise RiskInvariantError("reasons must contain ExchangeRiskReason values")
        if len(set(self.reasons)) != len(self.reasons):
            raise RiskInvariantError("reasons must not duplicate")
        if self.outcome is ExchangeRiskOutcome.APPROVED and self.reasons:
            raise RiskInvariantError("APPROVED cannot contain reasons")
        if self.outcome is ExchangeRiskOutcome.APPROVED and self.source_risk_outcome not in {
            RiskDecisionOutcome.APPROVED,
            RiskDecisionOutcome.MODIFIED,
        }:
            raise RiskInvariantError("APPROVED requires executable source Risk outcome")
        if self.outcome is ExchangeRiskOutcome.BLOCKED and not self.reasons:
            raise RiskInvariantError("BLOCKED requires reasons")
        if self.audit is not None and not isinstance(self.audit, ExchangeRiskAudit):
            raise RiskInvariantError("audit must be ExchangeRiskAudit or None")


@dataclass(frozen=True, slots=True)
class ExchangeSubmissionDecision:
    authorized: bool
    financial_outcome: ExchangeRiskOutcome
    risk_decision_id: str
    session_id: str
    reconciliation_generation: int
    control_generation: int
    reasons: tuple[ExchangeSubmissionReason, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.authorized, bool):
            raise RiskInvariantError("authorized must be a bool")
        if not isinstance(self.financial_outcome, ExchangeRiskOutcome):
            raise RiskInvariantError("financial_outcome must be ExchangeRiskOutcome")
        for name in ("risk_decision_id", "session_id"):
            _text(getattr(self, name), name, RiskInvariantError)
        for name in ("reconciliation_generation", "control_generation"):
            _integer(getattr(self, name), name, RiskInvariantError)
        if not isinstance(self.reasons, tuple) or any(
            not isinstance(reason, ExchangeSubmissionReason) for reason in self.reasons
        ):
            raise RiskInvariantError("reasons must contain ExchangeSubmissionReason values")
        if len(set(self.reasons)) != len(self.reasons):
            raise RiskInvariantError("reasons must not duplicate")
        if self.authorized == bool(self.reasons):
            raise RiskInvariantError("authorized requires no reasons; blocked requires reasons")
        if self.authorized and self.financial_outcome is not ExchangeRiskOutcome.APPROVED:
            raise RiskInvariantError("authorization requires approved financial Risk")


def evaluate_exchange_risk(
    decision: RiskDecision,
    portfolio: PortfolioSnapshot,
    context: ExchangeRiskContext,
    policy: ExchangeRiskPolicy,
) -> ExchangeRiskAssessment:
    if not isinstance(decision, RiskDecision):
        raise RiskInvariantError("decision must be a RiskDecision")
    if not isinstance(portfolio, PortfolioSnapshot):
        raise RiskInvariantError("portfolio must be a PortfolioSnapshot")
    if not isinstance(context, ExchangeRiskContext):
        raise RiskInvariantError("context must be an ExchangeRiskContext")
    if not isinstance(policy, ExchangeRiskPolicy):
        raise RiskInvariantError("policy must be an ExchangeRiskPolicy")
    reasons: list[ExchangeRiskReason] = []
    approved = decision.approved_order
    if decision.outcome is RiskDecisionOutcome.REJECTED or not isinstance(
        approved, ApprovedOrderParameters
    ):
        return ExchangeRiskAssessment(
            outcome=ExchangeRiskOutcome.BLOCKED,
            source_risk_outcome=decision.outcome,
            risk_decision_id=decision.decision_id,
            session_id=context.execution_session.session_id,
            reasons=(ExchangeRiskReason.RISK_NOT_APPROVED,),
            audit=None,
        )
    if (
        decision.stream_key != portfolio.stream_key
        or context.account.stream_key != portfolio.stream_key
    ):
        raise RiskInvariantError("risk/portfolio/account stream mismatch")
    if (
        context.account.base_asset != portfolio.base_asset
        or context.account.quote_asset != portfolio.quote_asset
    ):
        raise RiskInvariantError("risk account assets mismatch Portfolio")
    if _add(context.account.free_base, context.account.locked_base) != portfolio.base_balance:
        raise RiskInvariantError("reconciled base balance does not match Portfolio")
    if _add(context.account.free_quote, context.account.locked_quote) != portfolio.quote_balance:
        raise RiskInvariantError("reconciled quote balance does not match Portfolio")
    pending = context.pending_exposure
    session = context.execution_session
    if session.state is not ExecutionSessionState.ACTIVE:
        reasons.append(ExchangeRiskReason.SESSION_NOT_ACTIVE)
    if context.non_terminal_order_count >= policy.max_non_terminal_orders:
        reasons.append(ExchangeRiskReason.NON_TERMINAL_ORDER_LIMIT)
    if context.in_flight_submission_count >= policy.max_in_flight_submissions:
        reasons.append(ExchangeRiskReason.IN_FLIGHT_SUBMISSION_LIMIT)

    effective_quote = _subtract(
        _subtract(portfolio.quote_balance, pending.reserved_quote_amount),
        policy.min_cash_reserve,
    )
    effective_base = _subtract(portfolio.base_balance, pending.reserved_base_quantity)
    pending_buy = context.pending_buy_remaining_base
    pending_sell = context.pending_sell_remaining_base
    if pending_buy is None or pending_sell is None:
        pending_buy = (
            pending.remaining_base_quantity
            if context.pending_side is ExchangeOrderSide.BUY
            else ZERO
        )
        pending_sell = (
            pending.remaining_base_quantity
            if context.pending_side is ExchangeOrderSide.SELL
            else ZERO
        )
    current_position = portfolio.base_balance
    if approved.side is RiskOrderSide.BUY:
        if approved.required_quote > effective_quote:
            reasons.append(ExchangeRiskReason.QUOTE_RESERVATION_EXCEEDED)
        projected_position = _add(_add(current_position, pending_buy), approved.quantity)
    else:
        if approved.quantity > effective_base:
            reasons.append(ExchangeRiskReason.BASE_RESERVATION_EXCEEDED)
        projected_position = _subtract(_subtract(current_position, pending_sell), approved.quantity)
        if projected_position < ZERO:
            if ExchangeRiskReason.BASE_RESERVATION_EXCEEDED not in reasons:
                reasons.append(ExchangeRiskReason.BASE_RESERVATION_EXCEEDED)
            projected_position = ZERO
    projected_position_notional = _multiply(projected_position, approved.reference_price)
    if projected_position_notional > policy.max_position_notional:
        reasons.append(ExchangeRiskReason.POSITION_LIMIT)
    if approved.gross_notional > policy.max_order_notional:
        reasons.append(ExchangeRiskReason.ORDER_NOTIONAL_LIMIT)
    projected_submitted = _add(session.cumulative_submitted_notional, approved.gross_notional)
    if projected_submitted > policy.max_session_submitted_notional:
        reasons.append(ExchangeRiskReason.SESSION_SUBMITTED_NOTIONAL_LIMIT)
    projected_turnover = _add(session.cumulative_executed_turnover, approved.gross_notional)
    if projected_turnover > policy.max_session_executed_turnover:
        reasons.append(ExchangeRiskReason.SESSION_TURNOVER_LIMIT)
    projected_count = session.submission_count + 1
    if projected_count > policy.max_session_submission_count:
        reasons.append(ExchangeRiskReason.SESSION_SUBMISSION_COUNT_LIMIT)
    if session.execution_failure_count >= policy.max_session_execution_failures:
        reasons.append(ExchangeRiskReason.SESSION_FAILURE_LIMIT)
    if portfolio.drawdown >= policy.max_drawdown:
        reasons.append(ExchangeRiskReason.DRAWDOWN_LIMIT)
    audit = ExchangeRiskAudit(
        effective_quote,
        effective_base,
        pending_buy,
        pending_sell,
        approved.quantity,
        approved.gross_notional,
        projected_position,
        projected_position_notional,
        projected_submitted,
        projected_turnover,
        projected_count,
        policy,
    )
    return ExchangeRiskAssessment(
        outcome=ExchangeRiskOutcome.BLOCKED if reasons else ExchangeRiskOutcome.APPROVED,
        source_risk_outcome=decision.outcome,
        risk_decision_id=decision.decision_id,
        session_id=session.session_id,
        reasons=tuple(reasons),
        audit=audit,
    )


def evaluate_exchange_submission_safety(
    assessment: ExchangeRiskAssessment,
    context: ExchangeRiskContext,
    kill_switch: KillSwitch,
    capability: SubmissionCapability | None,
    *,
    environment: ExecutionEnvironment,
    pending_control_safe: bool,
) -> ExchangeSubmissionDecision:
    if not isinstance(assessment, ExchangeRiskAssessment):
        raise RiskInvariantError("assessment must be ExchangeRiskAssessment")
    if not isinstance(context, ExchangeRiskContext):
        raise RiskInvariantError("context must be ExchangeRiskContext")
    if not isinstance(kill_switch, KillSwitch):
        raise RiskInvariantError("kill_switch must be KillSwitch")
    if not isinstance(pending_control_safe, bool):
        raise RiskInvariantError("pending_control_safe must be a bool")
    if assessment.session_id != context.execution_session.session_id:
        raise RiskInvariantError("assessment/session lineage mismatch")
    reasons: list[ExchangeSubmissionReason] = []
    if environment is not ExecutionEnvironment.TESTNET:
        reasons.append(ExchangeSubmissionReason.UNSUPPORTED_ENVIRONMENT)
    if assessment.outcome is not ExchangeRiskOutcome.APPROVED:
        reasons.append(ExchangeSubmissionReason.FINANCIAL_RISK_BLOCKED)
    if kill_switch.state is not KillSwitchState.CLEARED:
        reasons.append(ExchangeSubmissionReason.KILL_SWITCH_ACTIVE)
    if kill_switch.generation != context.control_generation:
        reasons.append(ExchangeSubmissionReason.CONTROL_GENERATION_MISMATCH)
    if context.account.status is not ExchangeReconciliationStatus.SAFE:
        reasons.append(ExchangeSubmissionReason.RECONCILIATION_NOT_SAFE)
    if not isinstance(capability, SubmissionCapability):
        reasons.append(ExchangeSubmissionReason.CAPABILITY_MISSING)
    else:
        if capability.environment is not environment:
            reasons.append(ExchangeSubmissionReason.CAPABILITY_ENVIRONMENT_MISMATCH)
        if (
            capability.account_fingerprint != "legacy-process-scope"
            and capability.account_fingerprint != context.account.account_fingerprint
        ):
            reasons.append(ExchangeSubmissionReason.CAPABILITY_ACCOUNT_MISMATCH)
        if (
            capability.session_id != "legacy-session"
            and capability.session_id != context.execution_session.session_id
        ):
            reasons.append(ExchangeSubmissionReason.CAPABILITY_SESSION_MISMATCH)
        if capability.reconciliation_generation != context.account.reconciliation_generation:
            reasons.append(ExchangeSubmissionReason.RECONCILIATION_GENERATION_MISMATCH)
        if (
            capability.control_generation != context.control_generation
            and ExchangeSubmissionReason.CONTROL_GENERATION_MISMATCH not in reasons
        ):
            reasons.append(ExchangeSubmissionReason.CONTROL_GENERATION_MISMATCH)
    if not pending_control_safe:
        reasons.append(ExchangeSubmissionReason.PENDING_CONTROL_UNSAFE)
    return ExchangeSubmissionDecision(
        authorized=not reasons,
        financial_outcome=assessment.outcome,
        risk_decision_id=assessment.risk_decision_id,
        session_id=context.execution_session.session_id,
        reconciliation_generation=context.account.reconciliation_generation,
        control_generation=context.control_generation,
        reasons=tuple(reasons),
    )
