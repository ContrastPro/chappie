from collections.abc import Iterable
from dataclasses import replace
from decimal import Decimal, localcontext

from app.exchange_execution.exceptions import (
    ExchangeOrderIdentityError,
    ExchangeOrderStateError,
    ExchangeSubmissionSafetyError,
    ExchangeTradeConflictError,
)
from app.exchange_execution.models import (
    ExchangeOrder,
    ExchangeOrderStatus,
    ExchangeTrade,
    ExecutionEnvironment,
    KillSwitch,
    KillSwitchState,
    PendingExposure,
    SettlementClassification,
    TradeAggregate,
    TransportOutcomeKind,
)
from app.exchange_execution.ports import SubmissionCapability

ALLOWED_TRANSITIONS: dict[ExchangeOrderStatus, frozenset[ExchangeOrderStatus]] = {
    ExchangeOrderStatus.PREPARED: frozenset(
        {
            ExchangeOrderStatus.SUBMITTING,
            ExchangeOrderStatus.RECONCILIATION_REQUIRED,
            ExchangeOrderStatus.DISPOSED_NO_EXECUTION,
        }
    ),
    ExchangeOrderStatus.SUBMITTING: frozenset(
        {
            ExchangeOrderStatus.ACKNOWLEDGED,
            ExchangeOrderStatus.PARTIALLY_FILLED,
            ExchangeOrderStatus.FILLED,
            ExchangeOrderStatus.CANCELED,
            ExchangeOrderStatus.REJECTED,
            ExchangeOrderStatus.EXPIRED,
            ExchangeOrderStatus.RECONCILIATION_REQUIRED,
        }
    ),
    ExchangeOrderStatus.ACKNOWLEDGED: frozenset(
        {
            ExchangeOrderStatus.PARTIALLY_FILLED,
            ExchangeOrderStatus.FILLED,
            ExchangeOrderStatus.CANCELED,
            ExchangeOrderStatus.EXPIRED,
            ExchangeOrderStatus.RECONCILIATION_REQUIRED,
        }
    ),
    ExchangeOrderStatus.PARTIALLY_FILLED: frozenset(
        {
            ExchangeOrderStatus.PARTIALLY_FILLED,
            ExchangeOrderStatus.FILLED,
            ExchangeOrderStatus.CANCELED,
            ExchangeOrderStatus.EXPIRED,
            ExchangeOrderStatus.RECONCILIATION_REQUIRED,
        }
    ),
    ExchangeOrderStatus.RECONCILIATION_REQUIRED: frozenset(
        {
            ExchangeOrderStatus.ACKNOWLEDGED,
            ExchangeOrderStatus.PARTIALLY_FILLED,
            ExchangeOrderStatus.FILLED,
            ExchangeOrderStatus.CANCELED,
            ExchangeOrderStatus.REJECTED,
            ExchangeOrderStatus.EXPIRED,
        }
    ),
    ExchangeOrderStatus.FILLED: frozenset(),
    ExchangeOrderStatus.CANCELED: frozenset(),
    ExchangeOrderStatus.REJECTED: frozenset(),
    ExchangeOrderStatus.EXPIRED: frozenset(),
    ExchangeOrderStatus.DISPOSED_NO_EXECUTION: frozenset(),
}


def transition_order(current: ExchangeOrder, **changes: object) -> ExchangeOrder:
    next_status = changes.get("status")
    if not isinstance(next_status, ExchangeOrderStatus):
        raise ExchangeOrderStateError("transition requires an ExchangeOrderStatus")
    if next_status not in ALLOWED_TRANSITIONS[current.status]:
        raise ExchangeOrderStateError(f"transition {current.status} -> {next_status} is forbidden")
    candidate = replace(current, **changes)
    if candidate.durable_command_key != current.durable_command_key:
        raise ExchangeOrderIdentityError("durable command identity cannot change")
    if candidate.client_order_id != current.client_order_id:
        raise ExchangeOrderIdentityError("client order identity cannot change")
    if candidate.account_fingerprint != current.account_fingerprint:
        raise ExchangeOrderIdentityError("account lineage cannot change")
    if candidate.venue != current.venue or candidate.environment is not current.environment:
        raise ExchangeOrderIdentityError("venue/environment identity cannot change")
    if candidate.stream_key != current.stream_key or candidate.side is not current.side:
        raise ExchangeOrderIdentityError("stream/side identity cannot change")
    if candidate.original_base_quantity != current.original_base_quantity:
        raise ExchangeOrderIdentityError("original quantity cannot change")
    if (
        candidate.exchange_order_id != current.exchange_order_id
        and current.exchange_order_id is not None
    ):
        raise ExchangeOrderIdentityError("exchange order identity cannot change")
    if candidate.cumulative_executed_base_quantity < current.cumulative_executed_base_quantity:
        raise ExchangeOrderStateError("executed quantity cannot decrease")
    if candidate.cumulative_quote_quantity < current.cumulative_quote_quantity:
        raise ExchangeOrderStateError("quote quantity cannot decrease")
    return candidate


def status_after_transport(outcome: TransportOutcomeKind) -> ExchangeOrderStatus | None:
    if not isinstance(outcome, TransportOutcomeKind):
        raise ExchangeOrderStateError("outcome must be a TransportOutcomeKind")
    if outcome is TransportOutcomeKind.DEFINITELY_NOT_TRANSMITTED:
        return None
    if outcome is TransportOutcomeKind.MAY_HAVE_BEEN_TRANSMITTED:
        return ExchangeOrderStatus.RECONCILIATION_REQUIRED
    if outcome is TransportOutcomeKind.DETERMINISTIC_REJECTION:
        return ExchangeOrderStatus.REJECTED
    return ExchangeOrderStatus.ACKNOWLEDGED


def _add(left: Decimal, right: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = max(50, len(left.as_tuple().digits) + len(right.as_tuple().digits) + 16)
        return left + right


def aggregate_trades(order: ExchangeOrder, trades: Iterable[ExchangeTrade]) -> TradeAggregate:
    values = tuple(trades)
    if not values:
        raise ExchangeTradeConflictError("at least one trade is required")
    identities: dict[object, ExchangeTrade] = {}
    base = Decimal(0)
    quote = Decimal(0)
    commissions: dict[str, Decimal] = {}
    for trade in values:
        if not isinstance(trade, ExchangeTrade):
            raise ExchangeTradeConflictError("trades must contain ExchangeTrade values")
        if trade.client_order_id != order.client_order_id:
            raise ExchangeTradeConflictError("trade belongs to a different client order")
        if trade.stream_key != order.stream_key or trade.side is not order.side:
            raise ExchangeTradeConflictError("trade belongs to a different stream or side")
        if (
            order.exchange_order_id is None
            or trade.identity.exchange_order_id != order.exchange_order_id
        ):
            raise ExchangeTradeConflictError("trade belongs to a different exchange order")
        previous = identities.get(trade.identity)
        if previous is not None:
            if previous != trade:
                raise ExchangeTradeConflictError("duplicate trade identity has conflicting facts")
            continue
        identities[trade.identity] = trade
        base = _add(base, trade.quantity)
        quote = _add(quote, trade.quote_quantity)
        commissions[trade.commission_asset] = _add(
            commissions.get(trade.commission_asset, Decimal(0)), trade.commission
        )
    if base > order.original_base_quantity:
        raise ExchangeTradeConflictError("trade total exceeds original order quantity")
    return TradeAggregate(base, quote, commissions, len(identities))


def classify_settlement(
    order: ExchangeOrder,
    aggregate: TradeAggregate,
    *,
    base_asset: str,
    quote_asset: str,
) -> SettlementClassification:
    if aggregate.executed_base_quantity != order.cumulative_executed_base_quantity:
        return SettlementClassification.CONFLICT
    if order.status not in {ExchangeOrderStatus.PARTIALLY_FILLED, ExchangeOrderStatus.FILLED}:
        return SettlementClassification.INCOMPLETE
    assets = set(aggregate.commissions_by_asset)
    if base_asset in assets or any(asset != quote_asset for asset in assets):
        return SettlementClassification.UNSUPPORTED_FEE_ASSET
    return SettlementClassification.SETTLEABLE


def assert_submission_gates(
    *,
    environment: ExecutionEnvironment,
    kill_switch: KillSwitch,
    reconciliation_safe: bool,
    capability: SubmissionCapability | None,
    expected_reconciliation_generation: int,
    expected_control_generation: int,
    risk_approved: bool,
    pending_control_safe: bool,
) -> None:
    if environment is not ExecutionEnvironment.TESTNET:
        raise ExchangeSubmissionSafetyError("only TESTNET submission is supported")
    if not all(
        isinstance(value, bool)
        for value in (reconciliation_safe, risk_approved, pending_control_safe)
    ):
        raise ExchangeSubmissionSafetyError("submission gate flags must be booleans")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in (expected_reconciliation_generation, expected_control_generation)
    ):
        raise ExchangeSubmissionSafetyError("expected generations must be non-negative integers")
    if kill_switch.state is not KillSwitchState.CLEARED:
        raise ExchangeSubmissionSafetyError("kill switch is active")
    if not reconciliation_safe:
        raise ExchangeSubmissionSafetyError("reconciliation is not safe")
    if not isinstance(capability, SubmissionCapability):
        raise ExchangeSubmissionSafetyError("process-local capability is absent")
    if (
        capability.reconciliation_generation != expected_reconciliation_generation
        or capability.control_generation != expected_control_generation
    ):
        raise ExchangeSubmissionSafetyError("process-local capability generation is stale")
    if not risk_approved:
        raise ExchangeSubmissionSafetyError("Risk approval is absent")
    if not pending_control_safe:
        raise ExchangeSubmissionSafetyError("pending-order control is not safe")


def aggregate_pending_exposure(values: Iterable[PendingExposure]) -> PendingExposure:
    exposures = tuple(values)
    quote = Decimal(0)
    base = Decimal(0)
    notional = Decimal(0)
    original = Decimal(0)
    executed = Decimal(0)
    remaining = Decimal(0)
    orders = 0
    inflight = 0
    for exposure in exposures:
        if not isinstance(exposure, PendingExposure):
            raise ExchangeOrderStateError("values must contain PendingExposure records")
        quote = _add(quote, exposure.reserved_quote_amount)
        base = _add(base, exposure.reserved_base_quantity)
        notional = _add(notional, exposure.pending_notional)
        original = _add(original, exposure.original_base_quantity)
        executed = _add(executed, exposure.executed_base_quantity)
        remaining = _add(remaining, exposure.remaining_base_quantity)
        orders += exposure.order_count
        inflight += exposure.inflight_submission_count
    return PendingExposure(quote, base, notional, original, executed, remaining, orders, inflight)
