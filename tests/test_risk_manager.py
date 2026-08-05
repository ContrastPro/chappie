from dataclasses import FrozenInstanceError, replace
from decimal import Decimal, localcontext

import pytest

from app.market.models import CandleSnapshot, MarketStreamKey
from app.portfolio.models import InstrumentRules, PortfolioSnapshot, Position
from app.risk.exceptions import (
    RiskConfigurationError,
    RiskDecisionFactoryError,
    RiskInvariantError,
    RiskModelValidationError,
)
from app.risk.manager import RiskManager
from app.risk.models import (
    ApprovedOrderParameters,
    PortfolioRiskPolicy,
    RiskDecisionIdFactory,
    RiskModificationReason,
    RiskOrderSide,
    RiskRejectionReason,
    RiskSizingAudit,
    UUIDRiskDecisionIdFactory,
    create_decision_id,
)
from app.trading.exceptions import InvalidRiskDecisionError
from app.trading.models import (
    MarketSnapshot,
    OrderIntent,
    OrderIntentAction,
    RiskDecision,
    RiskDecisionOutcome,
)

D = Decimal
KEY = MarketStreamKey("BTCUSDT", "1m")
OTHER_KEY = MarketStreamKey("ETHUSDT", "1m")
AS_OF = 1_000


class SequentialIds:
    def __init__(self) -> None:
        self.value = 0

    def new_risk_decision_id(self) -> str:
        self.value += 1
        return f"risk-{self.value}"


class ConstantId:
    def new_risk_decision_id(self) -> str:
        return "risk-fixed"


def instrument_rules(**overrides: object) -> InstrumentRules:
    values: dict[str, object] = {
        "stream_key": KEY,
        "base_asset": "BTC",
        "quote_asset": "USDT",
        "price_tick": D("0.01"),
        "quantity_step": D("0.00001"),
        "min_quantity": D("0.00001"),
        "min_notional": D("10"),
        "base_asset_precision": 5,
        "quote_asset_precision": 2,
        "rules_version": "btc-usdt/1",
    }
    return InstrumentRules(**{**values, **overrides})  # type: ignore[arg-type]


def policy(**overrides: object) -> PortfolioRiskPolicy:
    values: dict[str, object] = {
        "max_order_notional": D("10000"),
        "max_position_notional": D("10000"),
    }
    return PortfolioRiskPolicy(**{**values, **overrides})  # type: ignore[arg-type]


def market_snapshot(
    price: str = "100.00",
    *,
    key: MarketStreamKey = KEY,
    as_of_ms: int = AS_OF,
    stale: bool = False,
    with_candle: bool = True,
) -> MarketSnapshot:
    candles: tuple[CandleSnapshot, ...] = ()
    latest = None
    if with_candle:
        value = D(price)
        latest = CandleSnapshot(
            open_time_ms=as_of_ms - 60_000,
            close_time_ms=as_of_ms - 1,
            open=value,
            high=value,
            low=value,
            close=value,
            volume=D("1"),
        )
        candles = (latest,)
    return MarketSnapshot(
        snapshot_id="snapshot-1",
        stream_key=key,
        as_of_ms=as_of_ms,
        closed_candles=candles,
        latest_closed_candle=latest,
        latest_open_candle=None,
        source_event_id="event-1",
        correlation_cycle_id="cycle-1",
        data_version="1",
        staleness_ms=None if latest is None else 0,
        is_stale=stale,
    )


def portfolio_snapshot(
    price: str = "100.00",
    *,
    base: str = "0",
    quote: str = "10000",
    key: MarketStreamKey = KEY,
    as_of_ms: int = AS_OF,
) -> PortfolioSnapshot:
    mark = D(price)
    base_value = D(base)
    quote_value = D(quote)
    position = None
    if base_value > 0:
        cost = base_value * mark
        position = Position(
            stream_key=key,
            quantity=base_value,
            average_entry_price=mark,
            cost_basis=cost,
            realised_pnl=D("0"),
            unrealised_pnl=D("0"),
            last_mark_price=mark,
            opened_at_ms=0,
            updated_at_ms=as_of_ms,
        )
    equity = quote_value + base_value * mark
    return PortfolioSnapshot(
        portfolio_id="paper-1",
        version=1,
        stream_key=key,
        as_of_ms=as_of_ms,
        base_asset="BTC" if key == KEY else "ETH",
        quote_asset="USDT",
        base_balance=base_value,
        quote_balance=quote_value,
        position=position,
        realised_pnl=D("0"),
        unrealised_pnl=D("0"),
        total_fees=D("0"),
        initial_equity=equity,
        current_equity=equity,
        peak_equity=equity,
        drawdown=D("0"),
        max_drawdown=D("0"),
        turnover=D("0"),
        trade_count=0,
        applied_fill_count=0,
    )


def intent(
    exposure: str = "0.5",
    *,
    action: OrderIntentAction = OrderIntentAction.ENTER_LONG,
    key: MarketStreamKey = KEY,
    created_at_ms: int = AS_OF,
) -> OrderIntent:
    return OrderIntent(
        intent_id="intent-1",
        source_signal_id="signal-1",
        stream_key=key,
        created_at_ms=created_at_ms,
        action=action,
        target_exposure=D(exposure),
        reason="baseline",
        strategy_name="ema",
        strategy_version="1",
        correlation_id="decision-1",
    )


def manager(
    *,
    risk_policy: PortfolioRiskPolicy | None = None,
    rules: InstrumentRules | None = None,
    ids: object | None = None,
) -> RiskManager:
    return RiskManager(
        risk_policy or policy(),
        rules or instrument_rules(),
        ConstantId() if ids is None else ids,  # type: ignore[arg-type]
    )


def reason(decision: RiskDecision) -> RiskRejectionReason:
    assert decision.outcome is RiskDecisionOutcome.REJECTED
    assert len(decision.rejection_reasons) == 1
    return decision.rejection_reasons[0]  # type: ignore[return-value]


def test_policy_defaults_properties_protocol_and_immutability() -> None:
    value = policy()
    service = manager(risk_policy=value)
    assert value.policy_name == service.name == "long_only_spot"
    assert value.policy_version == service.version == "1.0.0"
    assert service.policy is value
    assert service.instrument_rules == instrument_rules()
    assert isinstance(ConstantId(), RiskDecisionIdFactory)
    assert isinstance(UUIDRiskDecisionIdFactory(), RiskDecisionIdFactory)
    assert UUIDRiskDecisionIdFactory().new_risk_decision_id().strip()
    with pytest.raises(FrozenInstanceError):
        value.max_fee_rate = D("0")  # type: ignore[misc]


@pytest.mark.parametrize(
    "overrides",
    [
        {"policy_name": ""},
        {"policy_version": " "},
        {"max_target_exposure": D("0")},
        {"max_target_exposure": D("1.1")},
        {"max_order_notional": D("0")},
        {"max_position_notional": D("9999")},
        {"min_cash_reserve": D("-1")},
        {"max_fee_rate": D("1")},
        {"max_slippage_bps": D("-1")},
        {"max_fee_rate": D("NaN")},
        {"max_fee_rate": 0.1},
        {"reject_stale_market": 1},
    ],
)
def test_policy_rejects_invalid_values(overrides: dict[str, object]) -> None:
    with pytest.raises(RiskConfigurationError):
        policy(**overrides)


def test_approved_buy_exact_target_and_enriched_decision() -> None:
    decision = manager().evaluate(intent(), portfolio_snapshot(), market_snapshot())
    assert decision.outcome is RiskDecisionOutcome.APPROVED
    assert decision.approved_target_exposure == D("0.5")
    assert isinstance(decision.approved_order, ApprovedOrderParameters)
    assert decision.approved_order.side is RiskOrderSide.BUY
    assert decision.approved_order.quantity == D("50.00000")
    assert decision.approved_order.gross_notional == D("5000.00")
    assert decision.modification_reasons == ()
    assert decision.market_snapshot_id == "snapshot-1"
    assert decision.portfolio_id == "paper-1"
    assert decision.portfolio_version == 1
    assert decision.decided_at_ms == AS_OF
    assert decision.risk_policy_name == "long_only_spot"
    assert decision.applied_limits["max_fee_rate"] == D("0.001")


def test_manual_worst_case_buy_envelope() -> None:
    decision = manager().evaluate(
        intent("0.5"), portfolio_snapshot("50000.00"), market_snapshot("50000.00")
    )
    assert decision.outcome is RiskDecisionOutcome.APPROVED
    order = decision.approved_order
    assert isinstance(order, ApprovedOrderParameters)
    assert order.quantity == D("0.10000")
    assert order.worst_case_price == D("50025.000000")
    assert decision.sizing_audit.worst_case_fee == D("5.00250000000")  # type: ignore[union-attr]
    assert order.required_quote == D("5007.50250000000")


def test_manual_exact_buy_audit_and_order_fields() -> None:
    decision = manager().evaluate(
        intent("0.4"), portfolio_snapshot("50000.00"), market_snapshot("50000.00")
    )
    assert decision.outcome is RiskDecisionOutcome.APPROVED
    assert decision.modification_reasons == ()
    assert decision.approved_target_exposure == D("0.4")
    order = decision.approved_order
    audit = decision.sizing_audit
    assert isinstance(order, ApprovedOrderParameters)
    assert isinstance(audit, RiskSizingAudit)
    assert audit.requested_target_exposure == D("0.4")
    assert audit.capped_target_exposure == D("0.4")
    assert audit.portfolio_equity == D("10000")
    assert audit.reference_price == D("50000.00")
    assert audit.requested_target_notional == D("4000.0")
    assert audit.capped_target_notional == D("4000.0")
    assert audit.current_position_quantity == D("0")
    assert audit.current_position_notional == D("0.00")
    assert audit.raw_target_quantity == D("0.08")
    assert audit.rounded_target_quantity == D("0.08000")
    assert audit.raw_delta_quantity == D("0.08000")
    assert audit.rounded_order_quantity == D("0.08000")
    assert audit.approved_order_notional == D("4000.00")
    assert audit.approved_target_exposure == D("0.400000")
    assert audit.worst_case_price == D("50025.000000")
    assert audit.worst_case_fee == D("4.00200000000")
    assert audit.required_quote == D("4006.00200000000")
    assert audit.dust_liquidation is False
    assert audit.instrument_rules_version == "btc-usdt/1"
    assert order == ApprovedOrderParameters(
        side=RiskOrderSide.BUY,
        quantity=D("0.08000"),
        reference_price=D("50000.00"),
        gross_notional=D("4000.00"),
        approved_target_exposure=D("0.400000"),
        worst_case_price=D("50025.000000"),
        max_fee_rate=D("0.001"),
        max_slippage_bps=D("5"),
        required_quote=D("4006.00200000000"),
        dust_liquidation=False,
    )


def test_manual_order_and_position_caps() -> None:
    order_cap = manager(
        risk_policy=policy(max_order_notional=D("1000"), max_position_notional=D("10000"))
    ).evaluate(intent("1"), portfolio_snapshot("50000.00"), market_snapshot("50000.00"))
    assert order_cap.outcome is RiskDecisionOutcome.MODIFIED
    assert order_cap.approved_order.quantity == D("0.02000")  # type: ignore[union-attr]
    assert order_cap.approved_order.gross_notional == D("1000.00")  # type: ignore[union-attr]
    assert order_cap.approved_target_exposure == D("0.100000")
    assert order_cap.modification_reasons == (RiskModificationReason.ORDER_NOTIONAL_CAPPED,)

    empty = manager(
        risk_policy=policy(max_order_notional=D("3000"), max_position_notional=D("3000"))
    ).evaluate(intent("0.5"), portfolio_snapshot(), market_snapshot())
    existing = manager(
        risk_policy=policy(max_order_notional=D("3000"), max_position_notional=D("3000"))
    ).evaluate(intent("0.5"), portfolio_snapshot(base="20", quote="8000"), market_snapshot())
    for decision, quantity in ((empty, D("30.00000")), (existing, D("10.00000"))):
        assert decision.outcome is RiskDecisionOutcome.MODIFIED
        assert decision.approved_order.quantity == quantity  # type: ignore[union-attr]
        assert decision.approved_target_exposure == D("0.3")
        assert (
            decision.modification_reasons.count(RiskModificationReason.POSITION_NOTIONAL_CAPPED)
            == 1
        )


def test_manual_quote_budget_cap_is_maximal_step_quantity() -> None:
    decision = manager(risk_policy=policy(min_cash_reserve=D("500"))).evaluate(
        intent("1"),
        portfolio_snapshot("50000.00", quote="2500"),
        market_snapshot("50000.00"),
    )
    assert decision.outcome is RiskDecisionOutcome.MODIFIED
    assert decision.modification_reasons == (RiskModificationReason.QUOTE_BUDGET_CAPPED,)
    order = decision.approved_order
    assert isinstance(order, ApprovedOrderParameters)
    assert order.quantity == D("0.03994")
    assert order.required_quote == D("1999.99649850000")
    assert order.required_quote <= D("2000")
    assert D("0.03995") * D("50025") * D("1.001") > D("2000")


def test_increase_existing_position() -> None:
    decision = manager().evaluate(
        intent("0.5"), portfolio_snapshot(base="10", quote="9000"), market_snapshot()
    )
    assert decision.outcome is RiskDecisionOutcome.APPROVED
    assert decision.approved_order.quantity == D("40.00000")  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("exposure", "base", "expected"),
    [
        ("0.5", "50", RiskRejectionReason.ALREADY_AT_TARGET),
        ("0.4", "50", RiskRejectionReason.ACTION_DELTA_MISMATCH),
    ],
)
def test_enter_rejects_non_positive_delta(
    exposure: str, base: str, expected: RiskRejectionReason
) -> None:
    result = manager().evaluate(
        intent(exposure), portfolio_snapshot(base=base, quote="5000"), market_snapshot()
    )
    assert reason(result) is expected


def test_target_position_order_and_quote_caps_are_modified() -> None:
    target_cap = manager(risk_policy=policy(max_target_exposure=D("0.4"))).evaluate(
        intent("0.5"), portfolio_snapshot(), market_snapshot()
    )
    assert RiskModificationReason.TARGET_EXPOSURE_CAPPED in target_cap.modification_reasons

    position_cap = manager(
        risk_policy=policy(max_order_notional=D("1000"), max_position_notional=D("2000"))
    ).evaluate(intent("0.5"), portfolio_snapshot(), market_snapshot())
    assert RiskModificationReason.POSITION_NOTIONAL_CAPPED in position_cap.modification_reasons
    assert RiskModificationReason.ORDER_NOTIONAL_CAPPED in position_cap.modification_reasons

    order_cap = manager(
        risk_policy=policy(max_order_notional=D("1000"), max_position_notional=D("10000"))
    ).evaluate(intent("0.5"), portfolio_snapshot(), market_snapshot())
    assert order_cap.approved_order.quantity == D("10.00000")  # type: ignore[union-attr]
    assert RiskModificationReason.ORDER_NOTIONAL_CAPPED in order_cap.modification_reasons

    quote_cap = manager().evaluate(intent("1"), portfolio_snapshot(), market_snapshot())
    assert quote_cap.outcome is RiskDecisionOutcome.MODIFIED
    assert RiskModificationReason.QUOTE_BUDGET_CAPPED in quote_cap.modification_reasons
    assert quote_cap.approved_order.required_quote <= D("10000")  # type: ignore[union-attr]


def test_quantity_rounding_is_modified_and_global_context_independent() -> None:
    first = manager().evaluate(intent("0.33333333"), portfolio_snapshot(), market_snapshot())
    with localcontext() as context:
        context.prec = 6
        second = manager().evaluate(intent("0.33333333"), portfolio_snapshot(), market_snapshot())
    assert first.sizing_audit == second.sizing_audit
    assert RiskModificationReason.QUANTITY_ROUNDED in first.modification_reasons
    assert first.approved_target_exposure == D("0.3333333")


@pytest.mark.parametrize(
    ("exposure", "rules", "expected"),
    [
        ("0.00000001", instrument_rules(), RiskRejectionReason.ROUNDED_QUANTITY_ZERO),
        (
            "0.0005",
            instrument_rules(min_quantity=D("0.1")),
            RiskRejectionReason.BELOW_MIN_QUANTITY,
        ),
        ("0.0005", instrument_rules(), RiskRejectionReason.BELOW_MIN_NOTIONAL),
    ],
)
def test_minimum_rejections(
    exposure: str, rules: InstrumentRules, expected: RiskRejectionReason
) -> None:
    result = manager(rules=rules).evaluate(
        intent(exposure), portfolio_snapshot(), market_snapshot()
    )
    assert reason(result) is expected


def test_quote_budget_exact_boundary_reserve_and_insufficient() -> None:
    exact_policy = policy(min_cash_reserve=D("4992.4975"))
    exact = manager(risk_policy=exact_policy).evaluate(
        intent("0.5"), portfolio_snapshot("50000.00"), market_snapshot("50000.00")
    )
    assert exact.approved_order.required_quote == D("5007.50250000000")  # type: ignore[union-attr]

    reserve = manager(risk_policy=policy(min_cash_reserve=D("10000"))).evaluate(
        intent(), portfolio_snapshot(), market_snapshot()
    )
    assert reason(reserve) is RiskRejectionReason.CASH_RESERVE_LIMIT

    insufficient = manager().evaluate(intent("1"), portfolio_snapshot(quote="5"), market_snapshot())
    assert reason(insufficient) in {
        RiskRejectionReason.ROUNDED_QUANTITY_ZERO,
        RiskRejectionReason.BELOW_MIN_NOTIONAL,
        RiskRejectionReason.INSUFFICIENT_QUOTE,
    }


@pytest.mark.parametrize(
    ("risk_policy", "snapshot", "portfolio", "order_intent", "expected"),
    [
        (
            policy(),
            market_snapshot(stale=True),
            portfolio_snapshot(),
            intent(),
            RiskRejectionReason.STALE_MARKET,
        ),
        (
            policy(reject_stale_market=False),
            market_snapshot(with_candle=False, stale=True),
            portfolio_snapshot(),
            intent(),
            RiskRejectionReason.NO_CLOSED_CANDLE,
        ),
        (
            policy(),
            market_snapshot(key=OTHER_KEY),
            portfolio_snapshot(),
            intent(),
            RiskRejectionReason.STREAM_MISMATCH,
        ),
        (
            policy(),
            market_snapshot(),
            portfolio_snapshot(key=OTHER_KEY),
            intent(),
            RiskRejectionReason.PORTFOLIO_STREAM_MISMATCH,
        ),
        (
            policy(),
            market_snapshot(),
            portfolio_snapshot(as_of_ms=AS_OF - 1),
            intent(),
            RiskRejectionReason.PORTFOLIO_TIME_MISMATCH,
        ),
        (
            policy(),
            market_snapshot(),
            portfolio_snapshot(),
            intent(created_at_ms=AS_OF - 1),
            RiskRejectionReason.INTENT_TIME_MISMATCH,
        ),
    ],
)
def test_market_and_cross_model_rejections(
    risk_policy: PortfolioRiskPolicy,
    snapshot: MarketSnapshot,
    portfolio: PortfolioSnapshot,
    order_intent: OrderIntent,
    expected: RiskRejectionReason,
) -> None:
    result = manager(risk_policy=risk_policy).evaluate(order_intent, portfolio, snapshot)
    assert reason(result) is expected


def test_reference_price_alignment_and_portfolio_valuation() -> None:
    misaligned = manager().evaluate(
        intent(), portfolio_snapshot("100.001"), market_snapshot("100.001")
    )
    assert reason(misaligned) is RiskRejectionReason.INVALID_REFERENCE_PRICE

    portfolio = portfolio_snapshot("99.00", base="1", quote="9901")
    mismatch = manager().evaluate(intent(), portfolio, market_snapshot("100.00"))
    assert reason(mismatch) is RiskRejectionReason.PORTFOLIO_VALUATION_MISMATCH


def test_non_positive_equity_and_non_positive_reference_price() -> None:
    no_equity = manager().evaluate(intent(), portfolio_snapshot(quote="0"), market_snapshot())
    assert reason(no_equity) is RiskRejectionReason.NON_POSITIVE_EQUITY

    invalid_market = market_snapshot("-1.00")
    rejected = manager().evaluate(intent(), portfolio_snapshot("-1.00"), invalid_market)
    assert reason(rejected) is RiskRejectionReason.INVALID_REFERENCE_PRICE


def test_rules_asset_mismatch_and_corrupted_position_are_invariants() -> None:
    mismatched_assets = replace(portfolio_snapshot(), base_asset="XBT")
    with pytest.raises(RiskInvariantError, match="assets"):
        manager().evaluate(intent(), mismatched_assets, market_snapshot())

    corrupted = portfolio_snapshot(base="1", quote="9900")
    object.__setattr__(corrupted, "base_balance", D("2"))
    with pytest.raises(RiskInvariantError, match="quantity"):
        manager().evaluate(intent(), corrupted, market_snapshot())


def test_full_exit_and_dust_liquidation() -> None:
    exit_intent = intent("0", action=OrderIntentAction.EXIT_LONG)
    normal = manager().evaluate(
        exit_intent, portfolio_snapshot(base="1", quote="9900"), market_snapshot()
    )
    assert normal.outcome is RiskDecisionOutcome.APPROVED
    assert normal.approved_order.side is RiskOrderSide.SELL  # type: ignore[union-attr]
    assert normal.approved_order.quantity == D("1")  # type: ignore[union-attr]
    assert normal.approved_target_exposure == D("0")
    assert normal.modification_reasons == ()

    dust_rules = instrument_rules(min_quantity=D("0.1"), min_notional=D("10"))
    dust = manager(rules=dust_rules).evaluate(
        exit_intent,
        portfolio_snapshot(base="0.01", quote="9999"),
        market_snapshot(),
    )
    assert dust.outcome is RiskDecisionOutcome.MODIFIED
    assert dust.approved_order.quantity == D("0.01")  # type: ignore[union-attr]
    assert dust.approved_order.dust_liquidation is True  # type: ignore[union-attr]
    assert dust.modification_reasons == (RiskModificationReason.DUST_LIQUIDATION,)

    notional_dust_rules = instrument_rules(min_quantity=D("0.00001"), min_notional=D("10"))
    notional_dust = manager(rules=notional_dust_rules).evaluate(
        exit_intent,
        portfolio_snapshot(base="0.05", quote="9995"),
        market_snapshot(),
    )
    assert notional_dust.outcome is RiskDecisionOutcome.MODIFIED
    assert notional_dust.approved_order.dust_liquidation is True  # type: ignore[union-attr]


def test_exit_without_position_is_rejected() -> None:
    result = manager().evaluate(
        intent("0", action=OrderIntentAction.EXIT_LONG),
        portfolio_snapshot(),
        market_snapshot(),
    )
    assert reason(result) is RiskRejectionReason.NO_POSITION


def test_quote_shortfall_caps_quantity_and_below_minimum_rejects() -> None:
    short = manager(risk_policy=policy(min_cash_reserve=D("4992.50"))).evaluate(
        intent("0.5"), portfolio_snapshot("50000.00"), market_snapshot("50000.00")
    )
    assert short.outcome is RiskDecisionOutcome.MODIFIED
    assert short.approved_order.quantity == D("0.09999")  # type: ignore[union-attr]
    assert RiskModificationReason.QUOTE_BUDGET_CAPPED in short.modification_reasons

    below = manager(risk_policy=policy(min_cash_reserve=D("9999.50"))).evaluate(
        intent("0.5"), portfolio_snapshot(), market_snapshot()
    )
    assert reason(below) is RiskRejectionReason.BELOW_MIN_NOTIONAL


def test_minimum_checks_run_after_order_and_budget_caps() -> None:
    zero_order_cap = manager(
        risk_policy=policy(max_order_notional=D("0.0001"), max_position_notional=D("10000"))
    ).evaluate(intent("0.5"), portfolio_snapshot(), market_snapshot())
    assert reason(zero_order_cap) is RiskRejectionReason.ROUNDED_QUANTITY_ZERO

    zero_budget_cap = manager(risk_policy=policy(min_cash_reserve=D("9999.9999"))).evaluate(
        intent("0.5"), portfolio_snapshot(), market_snapshot()
    )
    assert reason(zero_budget_cap) is RiskRejectionReason.ROUNDED_QUANTITY_ZERO

    exact = manager(rules=instrument_rules(min_quantity=D("0.1"), min_notional=D("10"))).evaluate(
        intent("0.001"), portfolio_snapshot(), market_snapshot()
    )
    assert exact.outcome is RiskDecisionOutcome.APPROVED
    assert exact.approved_order.quantity == D("0.10000")  # type: ignore[union-attr]
    assert exact.approved_order.gross_notional == D("10.00")  # type: ignore[union-attr]

    below_quantity = manager(
        rules=instrument_rules(min_quantity=D("0.1"), min_notional=D("1"))
    ).evaluate(intent("0.0009999"), portfolio_snapshot(), market_snapshot())
    assert reason(below_quantity) is RiskRejectionReason.BELOW_MIN_QUANTITY

    rounded_below_notional = manager(
        rules=instrument_rules(min_quantity=D("0.00001"), min_notional=D("10.10"))
    ).evaluate(intent("0.001009"), portfolio_snapshot(), market_snapshot())
    assert reason(rounded_below_notional) is RiskRejectionReason.BELOW_MIN_NOTIONAL


class BrokenFactory:
    def new_risk_decision_id(self) -> str:
        raise RuntimeError("boom")


@pytest.mark.parametrize(
    "factory",
    [
        BrokenFactory(),
        type("Empty", (), {"new_risk_decision_id": lambda self: " "})(),
        type("WrongType", (), {"new_risk_decision_id": lambda self: 1})(),
    ],
)
def test_factory_failures_are_typed_and_manager_remains_stateless(factory: object) -> None:
    service = manager(ids=factory)
    with pytest.raises(RiskDecisionFactoryError):
        service.evaluate(intent(), portfolio_snapshot(), market_snapshot())
    assert service.policy == policy()


def test_factory_exception_preserves_cause_and_next_evaluation_succeeds() -> None:
    broken = manager(ids=BrokenFactory())
    with pytest.raises(RiskDecisionFactoryError) as captured:
        broken.evaluate(intent(), portfolio_snapshot(), market_snapshot())
    assert isinstance(captured.value.__cause__, RuntimeError)
    assert (
        manager().evaluate(intent(), portfolio_snapshot(), market_snapshot()).outcome
        is RiskDecisionOutcome.APPROVED
    )


def test_repeated_evaluation_does_not_mutate_inputs() -> None:
    order_intent = intent()
    portfolio = portfolio_snapshot()
    snapshot = market_snapshot()
    service = manager(ids=SequentialIds())
    first = service.evaluate(order_intent, portfolio, snapshot)
    second = service.evaluate(order_intent, portfolio, snapshot)
    assert first.decision_id != second.decision_id
    assert first.sizing_audit == second.sizing_audit
    assert order_intent == intent()
    assert portfolio == portfolio_snapshot()
    assert snapshot == market_snapshot()


def test_risk_models_and_decision_cross_field_invariants() -> None:
    decision = manager().evaluate(intent(), portfolio_snapshot(), market_snapshot())
    audit = decision.sizing_audit
    order = decision.approved_order
    assert isinstance(audit, RiskSizingAudit)
    assert isinstance(order, ApprovedOrderParameters)
    with pytest.raises(FrozenInstanceError):
        audit.required_quote = D("0")  # type: ignore[misc]
    with pytest.raises(RiskModelValidationError):
        replace(audit, required_quote=D("NaN"))
    with pytest.raises(RiskModelValidationError):
        replace(order, side="BUY")
    with pytest.raises(RiskModelValidationError):
        replace(order, approved_target_exposure=D("2"))
    with pytest.raises(InvalidRiskDecisionError):
        replace(decision, approved_order=None)
    with pytest.raises(InvalidRiskDecisionError, match="duplicates"):
        replace(
            decision,
            outcome=RiskDecisionOutcome.REJECTED,
            approved_target_exposure=None,
            approved_order=None,
            rejection_reasons=(
                RiskRejectionReason.STALE_MARKET,
                RiskRejectionReason.STALE_MARKET,
            ),
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"capped_target_exposure": D("0.6")},
        {"capped_target_notional": D("6000")},
        {"current_position_notional": D("1")},
        {"rounded_order_quantity": D("0"), "approved_order_notional": D("1")},
        {"worst_case_price": D("99")},
        {"required_quote": D("1"), "worst_case_fee": D("2")},
        {"reference_price": D("0")},
        {"portfolio_equity": D("0")},
        {"dust_liquidation": 1},
    ],
)
def test_sizing_audit_rejects_cross_field_inconsistency(changes: dict[str, Decimal]) -> None:
    audit = manager().evaluate(intent(), portfolio_snapshot(), market_snapshot()).sizing_audit
    assert isinstance(audit, RiskSizingAudit)
    with pytest.raises(RiskModelValidationError):
        replace(audit, **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"dust_liquidation": True},
        {"worst_case_price": D("99")},
        {"required_quote": D("1")},
        {"max_fee_rate": D("1")},
        {"dust_liquidation": 1},
        {"side": RiskOrderSide.SELL, "approved_target_exposure": D("0.5")},
        {"side": RiskOrderSide.SELL, "approved_target_exposure": D("0"), "required_quote": D("1")},
        {
            "side": RiskOrderSide.SELL,
            "approved_target_exposure": D("0"),
            "required_quote": D("0"),
            "worst_case_price": D("101"),
        },
    ],
)
def test_approved_order_rejects_side_specific_inconsistency(changes: dict[str, object]) -> None:
    order = manager().evaluate(intent(), portfolio_snapshot(), market_snapshot()).approved_order
    assert isinstance(order, ApprovedOrderParameters)
    with pytest.raises(RiskModelValidationError):
        replace(order, **changes)


def test_constructor_and_input_types_are_checked() -> None:
    with pytest.raises(RiskDecisionFactoryError):
        create_decision_id(object())  # type: ignore[arg-type]
    with pytest.raises(RiskConfigurationError):
        RiskManager("bad", instrument_rules(), ConstantId())  # type: ignore[arg-type]
    with pytest.raises(RiskConfigurationError):
        RiskManager(policy(), "bad", ConstantId())  # type: ignore[arg-type]
    with pytest.raises(RiskConfigurationError):
        RiskManager(policy(), instrument_rules(), object())  # type: ignore[arg-type]
    service = manager()
    with pytest.raises(RiskInvariantError):
        service.evaluate("bad", portfolio_snapshot(), market_snapshot())  # type: ignore[arg-type]
    with pytest.raises(RiskInvariantError):
        service.evaluate(intent(), "bad", market_snapshot())  # type: ignore[arg-type]
    with pytest.raises(RiskInvariantError):
        service.evaluate(intent(), portfolio_snapshot(), "bad")  # type: ignore[arg-type]
