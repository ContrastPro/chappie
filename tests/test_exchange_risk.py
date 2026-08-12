import copy
import pickle
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal, localcontext

import pytest

from app.exchange_execution.lifecycle import aggregate_pending_exposure
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
from app.risk.exceptions import RiskConfigurationError, RiskInvariantError
from app.risk.exchange import (
    ExchangeReconciliationStatus,
    ExchangeRiskAssessment,
    ExchangeRiskContext,
    ExchangeRiskOutcome,
    ExchangeRiskPolicy,
    ExchangeRiskReason,
    ExchangeSubmissionDecision,
    ExchangeSubmissionReason,
    ReconciledAccountState,
    evaluate_exchange_risk,
    evaluate_exchange_submission_safety,
)
from app.trading.models import OrderIntentAction, RiskDecisionOutcome
from tests.test_risk_manager import (
    KEY,
    intent,
    manager,
    market_snapshot,
    policy,
    portfolio_snapshot,
)

D = Decimal


def exchange_policy(**overrides: object) -> ExchangeRiskPolicy:
    values: dict[str, object] = {
        "max_order_notional": D("10000"),
        "max_position_notional": D("20000"),
        "max_session_submitted_notional": D("50000"),
        "max_session_executed_turnover": D("50000"),
        "max_session_submission_count": 10,
        "max_session_execution_failures": 3,
        "max_drawdown": D("1000"),
    }
    return ExchangeRiskPolicy(**{**values, **overrides})  # type: ignore[arg-type]


def approved_buy(exposure: str = "0.5"):
    return manager().evaluate(intent(exposure), portfolio_snapshot(), market_snapshot())


def approved_sell(*, base: str = "1", quote: str = "9900"):
    portfolio = portfolio_snapshot(base=base, quote=quote)
    decision = manager().evaluate(
        intent("0", action=OrderIntentAction.EXIT_LONG), portfolio, market_snapshot()
    )
    return decision, portfolio


def with_candidate(decision, *, quantity: str, required_quote: str | None = None):
    approved = decision.approved_order
    assert approved is not None
    amount = D(quantity)
    gross = amount * approved.reference_price
    return replace(
        decision,
        approved_order=replace(
            approved,
            quantity=amount,
            gross_notional=gross,
            required_quote=D(required_quote) if required_quote is not None else D(0),
        ),
    )


def risk_context(
    *,
    portfolio=None,
    pending: PendingExposure | None = None,
    pending_side: ExchangeOrderSide | None = None,
    session: ExecutionSession | None = None,
    reconciliation: ExchangeReconciliationStatus = ExchangeReconciliationStatus.SAFE,
    reconciliation_generation: int = 2,
    control_generation: int = 3,
    pending_buy_remaining_base: Decimal | None = None,
    pending_sell_remaining_base: Decimal | None = None,
) -> ExchangeRiskContext:
    snapshot = portfolio or portfolio_snapshot()
    exposure = pending or PendingExposure()
    account = ReconciledAccountState(
        account_fingerprint="account-lineage-1",
        stream_key=KEY,
        base_asset="BTC",
        quote_asset="USDT",
        free_base=snapshot.base_balance - exposure.reserved_base_quantity,
        locked_base=exposure.reserved_base_quantity,
        free_quote=snapshot.quote_balance - exposure.reserved_quote_amount,
        locked_quote=exposure.reserved_quote_amount,
        status=reconciliation,
        reconciliation_generation=reconciliation_generation,
    )
    current_session = session or ExecutionSession(
        "session-1",
        "account-lineage-1",
        100,
        ExecutionSessionState.ACTIVE,
    )
    return ExchangeRiskContext(
        account=account,
        pending_exposure=exposure,
        execution_session=current_session,
        pending_side=pending_side,
        non_terminal_order_count=exposure.order_count,
        in_flight_submission_count=exposure.inflight_submission_count,
        control_generation=control_generation,
        pending_buy_remaining_base=pending_buy_remaining_base,
        pending_sell_remaining_base=pending_sell_remaining_base,
    )


def assessment(**kwargs: object) -> ExchangeRiskAssessment:
    portfolio = kwargs.pop("portfolio", portfolio_snapshot())
    decision = kwargs.pop("decision", approved_buy())
    context = kwargs.pop("context", risk_context(portfolio=portfolio))
    policy = kwargs.pop("policy", exchange_policy())
    assert not kwargs
    return evaluate_exchange_risk(decision, portfolio, context, policy)


def test_clean_exchange_financial_risk_is_approved_with_exact_audit() -> None:
    result = assessment()
    assert result.outcome is ExchangeRiskOutcome.APPROVED
    assert result.reasons == ()
    assert result.audit is not None
    assert result.audit.effective_available_quote == D("10000")
    assert result.audit.candidate_quantity == D("50.00000")
    assert result.audit.candidate_notional == D("5000.00")
    assert result.audit.projected_position_quantity == D("50.00000")
    assert result.audit.projected_session_submitted_notional == D("5000.00")
    assert result.audit.projected_submission_count == 1


def test_financial_rejection_remains_separate_from_submission_safety() -> None:
    rejected = manager().evaluate(intent(), portfolio_snapshot(), market_snapshot(stale=True))
    assert rejected.outcome is RiskDecisionOutcome.REJECTED
    result = assessment(decision=rejected)
    assert result.outcome is ExchangeRiskOutcome.BLOCKED
    assert result.reasons == (ExchangeRiskReason.RISK_NOT_APPROVED,)
    assert result.audit is None


def test_pending_buy_blocks_second_order_and_uses_remaining_quantity() -> None:
    pending = PendingExposure(
        reserved_quote_amount=D("6000"),
        pending_notional=D("6000"),
        original_base_quantity=D("60"),
        executed_base_quantity=D("20"),
        remaining_base_quantity=D("40"),
        order_count=1,
    )
    result = assessment(context=risk_context(pending=pending, pending_side=ExchangeOrderSide.BUY))
    assert result.audit is not None
    assert result.audit.effective_available_quote == D("4000")
    assert result.audit.pending_buy_remaining_base == D("40")
    assert result.audit.projected_position_quantity == D("90.00000")
    assert result.reasons[:2] == (
        ExchangeRiskReason.NON_TERMINAL_ORDER_LIMIT,
        ExchangeRiskReason.QUOTE_RESERVATION_EXCEEDED,
    )


def test_pending_sell_prevents_accidental_short_and_uses_remaining_quantity() -> None:
    decision, portfolio = approved_sell()
    pending = PendingExposure(
        reserved_base_quantity=D("0.7"),
        pending_notional=D("70"),
        original_base_quantity=D("1"),
        executed_base_quantity=D("0.3"),
        remaining_base_quantity=D("0.7"),
        order_count=1,
    )
    result = assessment(
        decision=decision,
        portfolio=portfolio,
        context=risk_context(
            portfolio=portfolio, pending=pending, pending_side=ExchangeOrderSide.SELL
        ),
    )
    assert result.audit is not None
    assert result.audit.effective_available_base == D("0.3")
    assert result.audit.pending_sell_remaining_base == D("0.7")
    assert ExchangeRiskReason.BASE_RESERVATION_EXCEEDED in result.reasons
    assert ExchangeRiskReason.NON_TERMINAL_ORDER_LIMIT in result.reasons


def test_pending_buy_quote_boundary_uses_required_quote_once() -> None:
    zero_envelope = manager(risk_policy=policy(max_fee_rate=D(0), max_slippage_bps=D(0))).evaluate(
        intent("0.4"), portfolio_snapshot(), market_snapshot()
    )
    assert zero_envelope.approved_order is not None
    assert zero_envelope.approved_order.required_quote == D("4000")
    pending = PendingExposure(
        reserved_quote_amount=D("6000"),
        pending_notional=D("6000"),
        original_base_quantity=D("60"),
        remaining_base_quantity=D("60"),
        order_count=1,
    )
    context = risk_context(pending=pending, pending_side=ExchangeOrderSide.BUY)
    exact = assessment(decision=zero_envelope, context=context)
    assert exact.audit is not None
    assert exact.audit.effective_available_quote == D("4000")
    assert ExchangeRiskReason.QUOTE_RESERVATION_EXCEEDED not in exact.reasons
    over = assessment(
        decision=with_candidate(zero_envelope, quantity="40", required_quote="4000.00000001"),
        context=context,
    )
    assert ExchangeRiskReason.QUOTE_RESERVATION_EXCEEDED in over.reasons


def test_pending_sell_exact_base_boundary_does_not_short() -> None:
    decision, portfolio = approved_sell()
    pending = PendingExposure(
        reserved_base_quantity=D("0.7"),
        pending_notional=D("70"),
        original_base_quantity=D("0.7"),
        remaining_base_quantity=D("0.7"),
        order_count=1,
    )
    context = risk_context(
        portfolio=portfolio, pending=pending, pending_side=ExchangeOrderSide.SELL
    )
    exact = assessment(
        decision=with_candidate(decision, quantity="0.3"),
        portfolio=portfolio,
        context=context,
    )
    assert exact.audit is not None
    assert exact.audit.effective_available_base == D("0.3")
    assert exact.audit.projected_position_quantity == D(0)
    assert ExchangeRiskReason.BASE_RESERVATION_EXCEEDED not in exact.reasons
    over = assessment(
        decision=with_candidate(decision, quantity="0.5"),
        portfolio=portfolio,
        context=context,
    )
    assert ExchangeRiskReason.BASE_RESERVATION_EXCEEDED in over.reasons


def test_partial_fill_and_multiple_pending_aggregation_remain_exact() -> None:
    buy_partial = PendingExposure(
        reserved_quote_amount=D("60"),
        pending_notional=D("60"),
        original_base_quantity=D("1"),
        executed_base_quantity=D("0.4"),
        remaining_base_quantity=D("0.6"),
        order_count=1,
    )
    sell_partial = PendingExposure(
        reserved_base_quantity=D("0.3"),
        pending_notional=D("30"),
        original_base_quantity=D("0.5"),
        executed_base_quantity=D("0.2"),
        remaining_base_quantity=D("0.3"),
        order_count=1,
        inflight_submission_count=1,
    )
    aggregate = aggregate_pending_exposure((buy_partial, sell_partial))
    assert aggregate.original_base_quantity == D("1.5")
    assert aggregate.executed_base_quantity == D("0.6")
    assert aggregate.remaining_base_quantity == D("0.9")
    assert aggregate.reserved_quote_amount == D("60")
    assert aggregate.reserved_base_quantity == D("0.3")
    assert aggregate.order_count == 2
    portfolio = portfolio_snapshot(base="1", quote="9900")
    result = assessment(
        portfolio=portfolio,
        context=risk_context(
            portfolio=portfolio,
            pending=aggregate,
            pending_buy_remaining_base=D("0.6"),
            pending_sell_remaining_base=D("0.3"),
        ),
    )
    assert result.audit is not None
    assert result.audit.pending_buy_remaining_base == D("0.6")
    assert result.audit.pending_sell_remaining_base == D("0.3")
    assert result.reasons[:2] == (
        ExchangeRiskReason.NON_TERMINAL_ORDER_LIMIT,
        ExchangeRiskReason.IN_FLIGHT_SUBMISSION_LIMIT,
    )


def test_projected_buy_position_includes_current_pending_and_candidate() -> None:
    portfolio = portfolio_snapshot(base="0.10", quote="9990")
    candidate = manager().evaluate(intent("0.002"), portfolio, market_snapshot())
    pending = PendingExposure(
        reserved_quote_amount=D("5"),
        pending_notional=D("5"),
        original_base_quantity=D("0.05"),
        remaining_base_quantity=D("0.05"),
        order_count=1,
    )
    context = risk_context(portfolio=portfolio, pending=pending, pending_side=ExchangeOrderSide.BUY)
    exact = assessment(
        portfolio=portfolio,
        decision=candidate,
        context=context,
        policy=exchange_policy(max_order_notional=D("20"), max_position_notional=D("25")),
    )
    assert exact.audit is not None
    assert exact.audit.projected_position_quantity == D("0.25000")
    assert ExchangeRiskReason.POSITION_LIMIT not in exact.reasons
    over = assessment(
        portfolio=portfolio,
        decision=candidate,
        context=context,
        policy=exchange_policy(max_order_notional=D("20"), max_position_notional=D("24.999")),
    )
    assert ExchangeRiskReason.POSITION_LIMIT in over.reasons


def test_inflight_submission_always_blocks_another_candidate() -> None:
    pending = PendingExposure(
        reserved_quote_amount=D("1"),
        pending_notional=D("1"),
        original_base_quantity=D("1"),
        remaining_base_quantity=D("1"),
        order_count=1,
        inflight_submission_count=1,
    )
    result = assessment(context=risk_context(pending=pending, pending_side=ExchangeOrderSide.BUY))
    assert ExchangeRiskReason.IN_FLIGHT_SUBMISSION_LIMIT in result.reasons


@pytest.mark.parametrize(
    ("policy_overrides", "reason"),
    [
        (
            {"max_order_notional": D("4999"), "max_position_notional": D("20000")},
            ExchangeRiskReason.ORDER_NOTIONAL_LIMIT,
        ),
        (
            {"max_order_notional": D("4999"), "max_position_notional": D("4999")},
            ExchangeRiskReason.POSITION_LIMIT,
        ),
        (
            {"max_session_submitted_notional": D("4999")},
            ExchangeRiskReason.SESSION_SUBMITTED_NOTIONAL_LIMIT,
        ),
        ({"max_session_executed_turnover": D("4999")}, ExchangeRiskReason.SESSION_TURNOVER_LIMIT),
        ({"max_session_submission_count": 1}, ExchangeRiskReason.SESSION_SUBMISSION_COUNT_LIMIT),
        ({"max_session_execution_failures": 1}, ExchangeRiskReason.SESSION_FAILURE_LIMIT),
    ],
)
def test_exchange_session_and_financial_limits(
    policy_overrides: dict[str, object], reason: ExchangeRiskReason
) -> None:
    session = ExecutionSession(
        "session-1",
        "account-lineage-1",
        100,
        ExecutionSessionState.ACTIVE,
        cumulative_submitted_notional=D("1")
        if "max_session_submitted_notional" in policy_overrides
        else D(0),
        cumulative_executed_turnover=D("1")
        if "max_session_executed_turnover" in policy_overrides
        else D(0),
        submission_count=1 if "max_session_submission_count" in policy_overrides else 0,
        execution_failure_count=1 if "max_session_execution_failures" in policy_overrides else 0,
    )
    result = assessment(
        context=risk_context(session=session), policy=exchange_policy(**policy_overrides)
    )
    assert reason in result.reasons


def test_session_boundaries_allow_exact_equality() -> None:
    result = assessment(
        policy=exchange_policy(
            max_order_notional=D("5000"),
            max_position_notional=D("5000"),
            max_session_submitted_notional=D("5000"),
            max_session_executed_turnover=D("5000"),
            max_session_submission_count=1,
        )
    )
    assert result.outcome is ExchangeRiskOutcome.APPROVED


def test_session_counter_and_decimal_notional_boundaries_are_exact() -> None:
    candidate = with_candidate(approved_buy(), quantity="10", required_quote="1001")
    at_boundary = ExecutionSession(
        "session-1",
        "account-lineage-1",
        100,
        ExecutionSessionState.ACTIVE,
        cumulative_submitted_notional=D("9000"),
        cumulative_executed_turnover=D("9000"),
        submission_count=9,
    )
    exact = assessment(
        decision=candidate,
        context=risk_context(session=at_boundary),
        policy=exchange_policy(
            max_session_submitted_notional=D("10000"),
            max_session_executed_turnover=D("10000"),
            max_session_submission_count=10,
        ),
    )
    assert exact.outcome is ExchangeRiskOutcome.APPROVED
    over = replace(
        at_boundary,
        cumulative_submitted_notional=D("9000.00000001"),
        cumulative_executed_turnover=D("9000.00000001"),
    )
    blocked = assessment(
        decision=candidate,
        context=risk_context(session=over),
        policy=exchange_policy(
            max_session_submitted_notional=D("10000"),
            max_session_executed_turnover=D("10000"),
            max_session_submission_count=10,
        ),
    )
    assert ExchangeRiskReason.SESSION_SUBMITTED_NOTIONAL_LIMIT in blocked.reasons
    assert ExchangeRiskReason.SESSION_TURNOVER_LIMIT in blocked.reasons
    reached_count = replace(at_boundary, submission_count=10)
    assert (
        ExchangeRiskReason.SESSION_SUBMISSION_COUNT_LIMIT
        in assessment(decision=candidate, context=risk_context(session=reached_count)).reasons
    )


def test_closed_session_and_drawdown_threshold_block() -> None:
    closed = ExecutionSession("session-1", "account-lineage-1", 100, ExecutionSessionState.CLOSED)
    assert (
        ExchangeRiskReason.SESSION_NOT_ACTIVE
        in assessment(context=risk_context(session=closed)).reasons
    )
    portfolio = replace(
        portfolio_snapshot(quote="9000"),
        initial_equity=D("10000"),
        peak_equity=D("10000"),
        drawdown=D("1000"),
        max_drawdown=D("1000"),
    )
    result = assessment(
        portfolio=portfolio,
        decision=manager().evaluate(intent(), portfolio, market_snapshot()),
        context=risk_context(portfolio=portfolio),
        policy=exchange_policy(max_drawdown=D("1000")),
    )
    assert ExchangeRiskReason.DRAWDOWN_LIMIT in result.reasons


@pytest.mark.parametrize(
    ("drawdown", "blocked"),
    [("999.99999999", False), ("1000", True), ("1000.00000001", True)],
)
def test_drawdown_boundary_has_no_tolerance(drawdown: str, blocked: bool) -> None:
    current_equity = D("10000") - D(drawdown)
    portfolio = replace(
        portfolio_snapshot(quote=str(current_equity)),
        initial_equity=D("10000"),
        peak_equity=D("10000"),
        drawdown=D(drawdown),
        max_drawdown=D(drawdown),
    )
    result = assessment(
        portfolio=portfolio,
        decision=manager().evaluate(intent(), portfolio, market_snapshot()),
        context=risk_context(portfolio=portfolio),
    )
    assert (ExchangeRiskReason.DRAWDOWN_LIMIT in result.reasons) is blocked


def test_submission_safety_authorizes_only_all_clean_gates() -> None:
    context = risk_context()
    result = assessment(context=context)
    capability = SubmissionCapability._issue(reconciliation_generation=2, control_generation=3)
    decision = evaluate_exchange_submission_safety(
        result,
        context,
        KillSwitch(KillSwitchState.CLEARED, 3, "operator", 100),
        capability,
        environment=ExecutionEnvironment.TESTNET,
        pending_control_safe=True,
    )
    assert decision.authorized
    assert decision.reasons == ()


def test_submission_preserves_financial_result_and_cannot_override_rejection() -> None:
    context = risk_context()
    rejected = manager().evaluate(intent(), portfolio_snapshot(), market_snapshot(stale=True))
    financial = assessment(decision=rejected, context=context)
    submission = evaluate_exchange_submission_safety(
        financial,
        context,
        KillSwitch(KillSwitchState.CLEARED, 3, "operator", 100),
        SubmissionCapability._issue(reconciliation_generation=2, control_generation=3),
        environment=ExecutionEnvironment.TESTNET,
        pending_control_safe=True,
    )
    assert financial.source_risk_outcome is RiskDecisionOutcome.REJECTED
    assert financial.reasons == (ExchangeRiskReason.RISK_NOT_APPROVED,)
    assert not submission.authorized
    assert submission.financial_outcome is ExchangeRiskOutcome.BLOCKED
    assert submission.reasons == (ExchangeSubmissionReason.FINANCIAL_RISK_BLOCKED,)
    assert rejected.outcome is RiskDecisionOutcome.REJECTED


def test_modified_financial_order_remains_distinct_from_submission_block() -> None:
    modified = manager(
        risk_policy=policy(max_order_notional=D("2500"), max_position_notional=D("10000"))
    ).evaluate(intent("0.5"), portfolio_snapshot(), market_snapshot())
    assert modified.outcome is RiskDecisionOutcome.MODIFIED
    context = risk_context()
    financial = assessment(decision=modified, context=context)
    assert financial.source_risk_outcome is RiskDecisionOutcome.MODIFIED
    assert financial.audit is not None
    assert financial.audit.candidate_quantity == modified.approved_order.quantity  # type: ignore[union-attr]
    submission = evaluate_exchange_submission_safety(
        financial,
        context,
        KillSwitch(KillSwitchState.ACTIVE, 3, "operator", 100),
        SubmissionCapability._issue(reconciliation_generation=2, control_generation=3),
        environment=ExecutionEnvironment.TESTNET,
        pending_control_safe=True,
    )
    assert financial.outcome is ExchangeRiskOutcome.APPROVED
    assert not submission.authorized
    assert submission.reasons == (ExchangeSubmissionReason.KILL_SWITCH_ACTIVE,)


def test_kill_switch_generation_and_assessment_session_lineage_are_enforced() -> None:
    context = risk_context()
    financial = assessment(context=context)
    capability = SubmissionCapability._issue(reconciliation_generation=2, control_generation=3)
    stale_switch = evaluate_exchange_submission_safety(
        financial,
        context,
        KillSwitch(KillSwitchState.CLEARED, 4, "changed", 100),
        capability,
        environment=ExecutionEnvironment.TESTNET,
        pending_control_safe=True,
    )
    assert stale_switch.reasons == (ExchangeSubmissionReason.CONTROL_GENERATION_MISMATCH,)
    with pytest.raises(RiskInvariantError, match="session lineage"):
        evaluate_exchange_submission_safety(
            replace(financial, session_id="other-session"),
            context,
            KillSwitch(KillSwitchState.CLEARED, 3, "operator", 100),
            capability,
            environment=ExecutionEnvironment.TESTNET,
            pending_control_safe=True,
        )


def test_capability_is_authentic_process_local_and_cannot_authorize_live() -> None:
    context = risk_context()
    financial = assessment(context=context)
    capability = SubmissionCapability._issue(reconciliation_generation=2, control_generation=3)
    for operation in (copy.copy, copy.deepcopy, pickle.dumps):
        with pytest.raises(TypeError):
            operation(capability)
    blocked = evaluate_exchange_submission_safety(
        financial,
        context,
        KillSwitch(KillSwitchState.CLEARED, 3, "operator", 100),
        capability,
        environment="LIVE",  # type: ignore[arg-type]
        pending_control_safe=True,
    )
    assert blocked.reasons == (
        ExchangeSubmissionReason.UNSUPPORTED_ENVIRONMENT,
        ExchangeSubmissionReason.CAPABILITY_ENVIRONMENT_MISMATCH,
    )


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"environment": "live"}, ExchangeSubmissionReason.UNSUPPORTED_ENVIRONMENT),
        ({"financial_block": True}, ExchangeSubmissionReason.FINANCIAL_RISK_BLOCKED),
        ({"kill_active": True}, ExchangeSubmissionReason.KILL_SWITCH_ACTIVE),
        (
            {"reconciliation": ExchangeReconciliationStatus.REQUIRED},
            ExchangeSubmissionReason.RECONCILIATION_NOT_SAFE,
        ),
        ({"capability": None}, ExchangeSubmissionReason.CAPABILITY_MISSING),
        (
            {"reconciliation_generation": 9},
            ExchangeSubmissionReason.RECONCILIATION_GENERATION_MISMATCH,
        ),
        ({"control_generation": 9}, ExchangeSubmissionReason.CONTROL_GENERATION_MISMATCH),
        ({"pending_control_safe": False}, ExchangeSubmissionReason.PENDING_CONTROL_UNSAFE),
    ],
)
def test_each_submission_gate_blocks_independently(
    change: dict[str, object], reason: ExchangeSubmissionReason
) -> None:
    context = risk_context(
        reconciliation=change.get("reconciliation", ExchangeReconciliationStatus.SAFE),
    )
    result = assessment(context=context)
    if change.get("financial_block"):
        result = replace(
            result,
            outcome=ExchangeRiskOutcome.BLOCKED,
            reasons=(ExchangeRiskReason.ORDER_NOTIONAL_LIMIT,),
        )
    capability = change.get(
        "capability",
        SubmissionCapability._issue(
            reconciliation_generation=change.get("reconciliation_generation", 2),
            control_generation=change.get("control_generation", 3),
        ),
    )
    decision = evaluate_exchange_submission_safety(
        result,
        context,
        KillSwitch(
            KillSwitchState.ACTIVE if change.get("kill_active") else KillSwitchState.CLEARED,
            3,
            "test",
            100,
        ),
        capability,  # type: ignore[arg-type]
        environment=change.get("environment", ExecutionEnvironment.TESTNET),  # type: ignore[arg-type]
        pending_control_safe=change.get("pending_control_safe", True),  # type: ignore[arg-type]
    )
    assert not decision.authorized
    assert reason in decision.reasons


def test_risk_is_stateless_and_decimal_context_independent() -> None:
    clean = risk_context()
    pending = PendingExposure(
        reserved_quote_amount=D("6000"),
        pending_notional=D("6000"),
        original_base_quantity=D("60"),
        remaining_base_quantity=D("60"),
        order_count=1,
    )
    blocked_context = risk_context(pending=pending, pending_side=ExchangeOrderSide.BUY)
    with localcontext() as decimal_context:
        decimal_context.prec = 2
        blocked = assessment(context=blocked_context)
    with localcontext() as decimal_context:
        decimal_context.prec = 50
        allowed = assessment(context=clean)
    assert blocked.outcome is ExchangeRiskOutcome.BLOCKED
    assert allowed.outcome is ExchangeRiskOutcome.APPROVED
    assert assessment(context=clean) == allowed


def test_many_simultaneous_blockers_have_stable_nonduplicated_order() -> None:
    portfolio = replace(
        portfolio_snapshot(quote="9000"),
        initial_equity=D("10000"),
        peak_equity=D("10000"),
        drawdown=D("1000"),
        max_drawdown=D("1000"),
    )
    pending = PendingExposure(
        reserved_quote_amount=D("6000"),
        original_base_quantity=D("60"),
        remaining_base_quantity=D("60"),
        order_count=1,
        inflight_submission_count=1,
    )
    session = ExecutionSession(
        "session-1",
        "account-lineage-1",
        100,
        ExecutionSessionState.ACTIVE,
        submission_count=10,
    )
    context = risk_context(
        portfolio=portfolio,
        pending=pending,
        pending_side=ExchangeOrderSide.BUY,
        session=session,
        reconciliation=ExchangeReconciliationStatus.CONFLICT,
    )
    financial = assessment(
        portfolio=portfolio,
        decision=manager().evaluate(intent(), portfolio, market_snapshot()),
        context=context,
    )
    assert financial.reasons == (
        ExchangeRiskReason.NON_TERMINAL_ORDER_LIMIT,
        ExchangeRiskReason.IN_FLIGHT_SUBMISSION_LIMIT,
        ExchangeRiskReason.QUOTE_RESERVATION_EXCEEDED,
        ExchangeRiskReason.SESSION_SUBMISSION_COUNT_LIMIT,
        ExchangeRiskReason.DRAWDOWN_LIMIT,
    )
    submission = evaluate_exchange_submission_safety(
        financial,
        context,
        KillSwitch(KillSwitchState.ACTIVE, 4, "test", 100),
        SubmissionCapability._issue(reconciliation_generation=9, control_generation=9),
        environment="LIVE",  # type: ignore[arg-type]
        pending_control_safe=False,
    )
    assert submission.reasons == (
        ExchangeSubmissionReason.UNSUPPORTED_ENVIRONMENT,
        ExchangeSubmissionReason.FINANCIAL_RISK_BLOCKED,
        ExchangeSubmissionReason.KILL_SWITCH_ACTIVE,
        ExchangeSubmissionReason.CONTROL_GENERATION_MISMATCH,
        ExchangeSubmissionReason.RECONCILIATION_NOT_SAFE,
        ExchangeSubmissionReason.CAPABILITY_ENVIRONMENT_MISMATCH,
        ExchangeSubmissionReason.RECONCILIATION_GENERATION_MISMATCH,
        ExchangeSubmissionReason.PENDING_CONTROL_UNSAFE,
    )
    assert len(submission.reasons) == len(set(submission.reasons))


def test_audit_captures_policy_and_negative_zero_is_normalized() -> None:
    policy_value = exchange_policy(min_cash_reserve=D("-0"))
    result = assessment(policy=policy_value)
    assert result.audit is not None
    assert result.audit.applied_policy is policy_value
    assert result.audit.applied_policy.min_cash_reserve == D(0)
    assert not result.audit.effective_available_base.is_signed()


def test_huge_and_small_scientific_decimal_inputs_remain_exact() -> None:
    portfolio = portfolio_snapshot(price="1E+20", quote="1E+40")
    decision = manager(
        risk_policy=policy(max_order_notional=D("1E+40"), max_position_notional=D("1E+40"))
    ).evaluate(intent("1E-20"), portfolio, market_snapshot(price="1E+20"))
    result = assessment(
        portfolio=portfolio,
        decision=decision,
        context=risk_context(portfolio=portfolio),
        policy=exchange_policy(
            max_order_notional=D("1E+40"),
            max_position_notional=D("1E+40"),
            max_session_submitted_notional=D("1E+40"),
            max_session_executed_turnover=D("1E+40"),
        ),
    )
    assert result.audit is not None
    assert result.audit.candidate_notional.is_finite()
    assert isinstance(result.audit.candidate_notional, Decimal)


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_order_notional": D(0)},
        {"max_position_notional": D("9999")},
        {"max_session_submission_count": 0},
        {"max_session_execution_failures": True},
        {"max_non_terminal_orders": 2},
        {"max_in_flight_submissions": 2},
        {"max_drawdown": D("NaN")},
    ],
)
def test_exchange_policy_rejects_invalid_values(overrides: dict[str, object]) -> None:
    with pytest.raises(RiskConfigurationError):
        exchange_policy(**overrides)


def test_context_account_and_result_models_reject_corruption_and_are_immutable() -> None:
    context = risk_context()
    for field, value in (
        ("pending_exposure", object()),
        ("pending_side", ExchangeOrderSide.BUY),
        ("non_terminal_order_count", 1),
        ("control_generation", -1),
    ):
        with pytest.raises(RiskInvariantError):
            replace(context, **{field: value})
    with pytest.raises(RiskInvariantError, match="balance"):
        evaluate_exchange_risk(
            approved_buy(),
            portfolio_snapshot(),
            replace(context, account=replace(context.account, free_quote=D("9999"))),
            exchange_policy(),
        )
    with pytest.raises(FrozenInstanceError):
        context.control_generation = 2  # type: ignore[misc]
    with pytest.raises(RiskInvariantError):
        ExchangeRiskAssessment(
            outcome=ExchangeRiskOutcome.APPROVED,
            source_risk_outcome=RiskDecisionOutcome.APPROVED,
            risk_decision_id="risk",
            session_id="session",
            reasons=(ExchangeRiskReason.POSITION_LIMIT,),
            audit=None,
        )
    with pytest.raises(RiskInvariantError):
        ExchangeSubmissionDecision(
            authorized=True,
            financial_outcome=ExchangeRiskOutcome.APPROVED,
            risk_decision_id="risk",
            session_id="session",
            reconciliation_generation=1,
            control_generation=1,
            reasons=(ExchangeSubmissionReason.KILL_SWITCH_ACTIVE,),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("account_fingerprint", ""),
        ("stream_key", object()),
        ("free_base", D("NaN")),
        ("locked_quote", D("-1")),
        ("status", "SAFE"),
        ("reconciliation_generation", True),
    ],
)
def test_reconciled_account_rejects_invalid_fields(field: str, value: object) -> None:
    with pytest.raises(RiskInvariantError):
        replace(risk_context().account, **{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("account", object()),
        ("execution_session", object()),
        ("pending_side", "BUY"),
        ("in_flight_submission_count", 1),
        ("context_version", ""),
    ],
)
def test_exchange_context_rejects_invalid_fields(field: str, value: object) -> None:
    with pytest.raises(RiskInvariantError):
        replace(risk_context(), **{field: value})


def test_exchange_context_rejects_missing_side_lineage_and_symbol_mismatch() -> None:
    with pytest.raises(RiskInvariantError, match="requires pending_side"):
        risk_context(pending=PendingExposure(order_count=1))
    context = risk_context()
    with pytest.raises(RiskInvariantError, match="lineage"):
        replace(
            context,
            execution_session=replace(context.execution_session, account_fingerprint="other"),
        )
    with pytest.raises(RiskInvariantError, match="BTCUSDT"):
        replace(
            context,
            account=replace(context.account, stream_key=replace(KEY, symbol="ETHUSDT")),
        )


@pytest.mark.parametrize(
    "values",
    [
        {"outcome": "BLOCKED"},
        {"source_risk_outcome": "APPROVED"},
        {"reasons": [ExchangeRiskReason.POSITION_LIMIT]},
        {"reasons": (ExchangeRiskReason.POSITION_LIMIT,) * 2},
        {"outcome": ExchangeRiskOutcome.BLOCKED, "reasons": ()},
    ],
)
def test_exchange_assessment_rejects_invalid_results(values: dict[str, object]) -> None:
    with pytest.raises(RiskInvariantError):
        replace(assessment(), **values)


@pytest.mark.parametrize(
    "values",
    [
        {"authorized": 1},
        {"financial_outcome": "APPROVED"},
        {"authorized": True, "financial_outcome": ExchangeRiskOutcome.BLOCKED},
        {"risk_decision_id": ""},
        {"control_generation": -1},
        {"reasons": [ExchangeSubmissionReason.KILL_SWITCH_ACTIVE]},
        {"reasons": (ExchangeSubmissionReason.KILL_SWITCH_ACTIVE,) * 2},
        {"authorized": False, "reasons": ()},
    ],
)
def test_submission_decision_rejects_invalid_results(values: dict[str, object]) -> None:
    valid = ExchangeSubmissionDecision(
        True, ExchangeRiskOutcome.APPROVED, "risk", "session", 1, 1, ()
    )
    with pytest.raises(RiskInvariantError):
        replace(valid, **values)


def test_exchange_risk_rejects_invalid_inputs_and_reconciliation_mismatch() -> None:
    decision = approved_buy()
    portfolio = portfolio_snapshot()
    context = risk_context()
    policy = exchange_policy()
    for args in (
        (object(), portfolio, context, policy),
        (decision, object(), context, policy),
        (decision, portfolio, object(), policy),
        (decision, portfolio, context, object()),
    ):
        with pytest.raises(RiskInvariantError):
            evaluate_exchange_risk(*args)  # type: ignore[arg-type]
    with pytest.raises(RiskInvariantError, match="stream mismatch"):
        evaluate_exchange_risk(
            replace(decision, stream_key=replace(KEY, symbol="ETHUSDT")),
            portfolio,
            context,
            policy,
        )
    with pytest.raises(RiskInvariantError, match="assets mismatch"):
        evaluate_exchange_risk(
            decision,
            portfolio,
            replace(context, account=replace(context.account, base_asset="X")),
            policy,
        )
    with pytest.raises(RiskInvariantError, match="base balance"):
        evaluate_exchange_risk(
            decision,
            portfolio,
            replace(context, account=replace(context.account, free_base=D("1"))),
            policy,
        )


def test_submission_safety_rejects_invalid_input_types() -> None:
    context = risk_context()
    result = assessment(context=context)
    kill_switch = KillSwitch(KillSwitchState.CLEARED, 3, "test", 100)
    capability = SubmissionCapability._issue(reconciliation_generation=2, control_generation=3)
    for args in (
        (object(), context, kill_switch, capability, True),
        (result, object(), kill_switch, capability, True),
        (result, context, object(), capability, True),
        (result, context, kill_switch, capability, 1),
    ):
        with pytest.raises(RiskInvariantError):
            evaluate_exchange_submission_safety(
                args[0],  # type: ignore[arg-type]
                args[1],  # type: ignore[arg-type]
                args[2],  # type: ignore[arg-type]
                args[3],  # type: ignore[arg-type]
                environment=ExecutionEnvironment.TESTNET,
                pending_control_safe=args[4],  # type: ignore[arg-type]
            )
