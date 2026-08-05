from dataclasses import FrozenInstanceError, replace
from decimal import Decimal, InvalidOperation, localcontext

import pytest

from app.execution.exceptions import (
    ExecutionArithmeticError,
    ExecutionConfigurationError,
    ExecutionIdFactoryError,
    ExecutionInvariantError,
    ExecutionModelValidationError,
    FillIdFactoryError,
    UnsupportedRiskDecisionError,
)
from app.execution.models import (
    ExecutionIdFactory,
    FillIdFactory,
    PaperExecutionConfig,
    PaperExecutionStatus,
    UUIDExecutionIdFactory,
    UUIDFillIdFactory,
    create_execution_id,
    create_fill_id,
)
from app.execution.paper import PaperExecutionProvider
from app.execution.provider import ExecutionProvider
from app.market.models import CandleSnapshot, MarketStreamKey
from app.portfolio.models import InstrumentRules, PortfolioConfig, PortfolioSnapshot, Position
from app.portfolio.portfolio import Portfolio
from app.risk.manager import RiskManager
from app.risk.models import PortfolioRiskPolicy
from app.trading.models import MarketSnapshot, OrderIntent, OrderIntentAction, RiskDecisionOutcome

D = Decimal
KEY = MarketStreamKey("BTCUSDT", "1m")
NOW = 1_000


class FixedExecutionIds:
    def __init__(self, value: object = "execution-1", calls: list[str] | None = None) -> None:
        self.value = value
        self.calls = calls

    def new_execution_id(self) -> str:
        if self.calls is not None:
            self.calls.append("execution")
        if isinstance(self.value, Exception):
            raise self.value
        return self.value  # type: ignore[return-value]


class FixedFillIds:
    def __init__(self, value: object = "fill-1", calls: list[str] | None = None) -> None:
        self.value = value
        self.calls = calls

    def new_fill_id(self) -> str:
        if self.calls is not None:
            self.calls.append("fill")
        if isinstance(self.value, Exception):
            raise self.value
        return self.value  # type: ignore[return-value]


class FixedRiskIds:
    def new_risk_decision_id(self) -> str:
        return "risk-1"


class SequentialExecutionIds:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self.count = 0

    def new_execution_id(self) -> str:
        self.count += 1
        return f"{self.prefix}-{self.count}"


class SequentialFillIds:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self.count = 0

    def new_fill_id(self) -> str:
        self.count += 1
        return f"{self.prefix}-{self.count}"


def rules(**overrides: object) -> InstrumentRules:
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


def portfolio_snapshot(
    price: str, *, base: str = "0", quote: str = "10000", as_of_ms: int = NOW
) -> PortfolioSnapshot:
    mark = D(price)
    quantity = D(base)
    position = None
    if quantity > 0:
        position = Position(
            stream_key=KEY,
            quantity=quantity,
            average_entry_price=mark,
            cost_basis=quantity * mark,
            realised_pnl=D(0),
            unrealised_pnl=D(0),
            last_mark_price=mark,
            opened_at_ms=0,
            updated_at_ms=as_of_ms,
        )
    equity = D(quote) + quantity * mark
    return PortfolioSnapshot(
        portfolio_id="paper-1",
        version=1,
        stream_key=KEY,
        as_of_ms=as_of_ms,
        base_asset="BTC",
        quote_asset="USDT",
        base_balance=quantity,
        quote_balance=D(quote),
        position=position,
        realised_pnl=D(0),
        unrealised_pnl=D(0),
        total_fees=D(0),
        initial_equity=equity,
        current_equity=equity,
        peak_equity=equity,
        drawdown=D(0),
        max_drawdown=D(0),
        turnover=D(0),
        trade_count=0,
        applied_fill_count=0,
    )


def decision(
    *,
    price: str = "50000.00",
    exposure: str = "0.4",
    action: OrderIntentAction = OrderIntentAction.ENTER_LONG,
    base: str = "0",
    quote: str = "10000",
    stale: bool = False,
    max_order_notional: str = "10000",
) -> object:
    value = D(price)
    candle = CandleSnapshot(
        open_time_ms=0,
        close_time_ms=999,
        open=value,
        high=value,
        low=value,
        close=value,
        volume=D(1),
    )
    market = MarketSnapshot(
        snapshot_id="market-1",
        stream_key=KEY,
        as_of_ms=NOW,
        closed_candles=(candle,),
        latest_closed_candle=candle,
        latest_open_candle=None,
        source_event_id="event-1",
        correlation_cycle_id="cycle-1",
        data_version="1",
        staleness_ms=0,
        is_stale=stale,
    )
    order_intent = OrderIntent(
        intent_id="intent-1",
        source_signal_id="signal-1",
        stream_key=KEY,
        created_at_ms=NOW,
        action=action,
        target_exposure=D(exposure),
        reason="test",
        strategy_name="ema",
        strategy_version="1",
        correlation_id="cycle-1",
    )
    return RiskManager(
        PortfolioRiskPolicy(
            max_order_notional=D(max_order_notional), max_position_notional=D("10000")
        ),
        rules(),
        FixedRiskIds(),
    ).evaluate(order_intent, portfolio_snapshot(price, base=base, quote=quote), market)


def provider(
    *,
    config: PaperExecutionConfig | None = None,
    instrument_rules: InstrumentRules | None = None,
    execution_ids: object | None = None,
    fill_ids: object | None = None,
) -> PaperExecutionProvider:
    return PaperExecutionProvider(
        config or PaperExecutionConfig(),
        instrument_rules or rules(),
        FixedExecutionIds() if execution_ids is None else execution_ids,  # type: ignore[arg-type]
        FixedFillIds() if fill_ids is None else fill_ids,  # type: ignore[arg-type]
    )


def test_config_protocol_properties_and_immutability() -> None:
    config = PaperExecutionConfig(fee_rate=D("0.002"), latency_ms=7)
    service = provider(config=config)
    assert service.name == "deterministic_paper"
    assert service.version == "1.0.0"
    assert service.configuration is config
    assert service.instrument_rules == rules()
    assert isinstance(service, ExecutionProvider)
    assert isinstance(FixedExecutionIds(), ExecutionIdFactory)
    assert isinstance(FixedFillIds(), FillIdFactory)
    assert UUIDExecutionIdFactory().new_execution_id().strip()
    assert UUIDFillIdFactory().new_fill_id().strip()
    with pytest.raises(FrozenInstanceError):
        config.fee_rate = D(0)  # type: ignore[misc]


@pytest.mark.parametrize(
    "changes",
    [
        {"provider_name": ""},
        {"provider_version": " "},
        {"fee_rate": D("-0.1")},
        {"fee_rate": D("1")},
        {"fee_rate": D("NaN")},
        {"fee_rate": 0.1},
        {"latency_ms": -1},
        {"latency_ms": True},
    ],
)
def test_invalid_configuration(changes: dict[str, object]) -> None:
    with pytest.raises(ExecutionConfigurationError):
        PaperExecutionConfig(**changes)  # type: ignore[arg-type]


def test_manual_buy_fill_and_metadata() -> None:
    approved = decision()
    result = provider(config=PaperExecutionConfig(latency_ms=25)).execute(approved)  # type: ignore[arg-type]
    assert result.status is PaperExecutionStatus.FILLED
    assert result.execution_id == "execution-1"
    assert result.reason_code == "filled"
    assert result.started_at_ms == NOW
    assert result.completed_at_ms == NOW + 25
    fill = result.fill
    assert fill is not None
    assert fill.fill_id == "fill-1"
    assert fill.order_id == "execution-1"
    assert fill.risk_decision_id == approved.decision_id  # type: ignore[union-attr]
    assert fill.intent_id == approved.intent_id  # type: ignore[union-attr]
    assert fill.stream_key == KEY
    assert fill.correlation_id == "cycle-1"
    assert fill.filled_at_ms == NOW + 25
    assert fill.quantity == D("0.08000")
    assert fill.price == D("50000.00")
    assert fill.gross_notional == D("4000.00")
    assert fill.fee == D("4.00")
    assert fill.net_quote_change == D("-4004.00")
    assert fill.net_base_change == D("0.08000")
    assert fill.metadata["risk_outcome"] == "approved"
    with pytest.raises(TypeError):
        fill.metadata["x"] = "y"  # type: ignore[index]


def test_manual_sell_fill_and_dust_metadata() -> None:
    sell = decision(
        price="60000.00",
        exposure="0",
        action=OrderIntentAction.EXIT_LONG,
        base="0.10000",
        quote="4000",
    )
    result = provider().execute(sell)  # type: ignore[arg-type]
    fill = result.fill
    assert fill is not None
    assert fill.quantity == D("0.10000")
    assert fill.price == D("60000.00")
    assert fill.gross_notional == D("6000.00")
    assert fill.fee == D("6.00")
    assert fill.net_quote_change == D("5994.00")
    assert fill.net_base_change == D("-0.10000")
    assert fill.metadata["dust_liquidation"] is False

    dust_order = replace(sell.approved_order, dust_liquidation=True)  # type: ignore[union-attr]
    dust_decision = replace(
        sell,
        outcome=RiskDecisionOutcome.MODIFIED,
        approved_order=dust_order,
        modification_reasons=("dust_liquidation",),
    )
    dust_fill = provider().execute(dust_decision).fill
    assert dust_fill is not None
    assert dust_fill.quantity == D("0.10000")
    assert dust_fill.metadata["dust_liquidation"] is True


def test_rejected_is_auditable_without_fill_id() -> None:
    calls: list[str] = []
    rejected = decision(stale=True)
    assert rejected.outcome is RiskDecisionOutcome.REJECTED  # type: ignore[union-attr]
    result = provider(
        config=PaperExecutionConfig(latency_ms=5),
        execution_ids=FixedExecutionIds(calls=calls),
        fill_ids=FixedFillIds(calls=calls),
    ).execute(rejected)  # type: ignore[arg-type]
    assert calls == ["execution"]
    assert result.status is PaperExecutionStatus.NOT_EXECUTED
    assert result.fill is None
    assert result.reason_code == "risk_rejected"
    assert result.completed_at_ms == NOW + 5
    assert result.correlation_id == "cycle-1"
    assert result.risk_rejection_reasons == ("stale_market",)


def test_modified_decision_executes_approved_quantity() -> None:
    modified = decision(exposure="1", max_order_notional="1000")
    result = provider().execute(modified)  # type: ignore[arg-type]
    assert modified.outcome is RiskDecisionOutcome.MODIFIED  # type: ignore[union-attr]
    assert result.status is PaperExecutionStatus.FILLED
    assert result.fill is not None
    assert result.fill.quantity == D("0.02000")
    assert result.fill.metadata["risk_outcome"] == "modified"


def test_three_modified_buy_combinations_use_only_approved_quantity() -> None:
    order_cap = decision(exposure="1", max_order_notional="1000")
    quote_cap = decision(exposure="1", quote="5000")
    rounded = decision(exposure="0.33333333")
    for approved in (order_cap, quote_cap, rounded):
        assert approved.outcome is RiskDecisionOutcome.MODIFIED  # type: ignore[union-attr]
        result = provider().execute(approved)  # type: ignore[arg-type]
        assert result.fill is not None
        assert result.fill.quantity == approved.approved_order.quantity  # type: ignore[union-attr]
        assert result.fill.gross_notional == approved.approved_order.gross_notional  # type: ignore[union-attr]


def test_factory_order_failures_and_reuse() -> None:
    calls: list[str] = []
    service = provider(
        execution_ids=FixedExecutionIds(calls=calls), fill_ids=FixedFillIds(calls=calls)
    )
    service.execute(decision())  # type: ignore[arg-type]
    assert calls == ["execution", "fill"]

    fill_failure = provider(fill_ids=FixedFillIds(RuntimeError("fill boom")))
    with pytest.raises(FillIdFactoryError) as captured:
        fill_failure.execute(decision())  # type: ignore[arg-type]
    assert isinstance(captured.value.__cause__, RuntimeError)
    assert provider().execute(decision()).status is PaperExecutionStatus.FILLED  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["", " ", 1])
def test_invalid_factory_outputs(value: object) -> None:
    with pytest.raises(ExecutionIdFactoryError):
        provider(execution_ids=FixedExecutionIds(value)).execute(decision())  # type: ignore[arg-type]
    with pytest.raises(FillIdFactoryError):
        provider(fill_ids=FixedFillIds(value)).execute(decision())  # type: ignore[arg-type]


def test_execution_factory_failure_prevents_fill_factory() -> None:
    calls: list[str] = []
    service = provider(
        execution_ids=FixedExecutionIds(RuntimeError("execution boom"), calls),
        fill_ids=FixedFillIds(calls=calls),
    )
    with pytest.raises(ExecutionIdFactoryError) as captured:
        service.execute(decision())  # type: ignore[arg-type]
    assert calls == ["execution"]
    assert isinstance(captured.value.__cause__, RuntimeError)


def test_duplicate_execution_and_fill_ids_are_rejected() -> None:
    with pytest.raises(ExecutionInvariantError, match="differ"):
        provider(fill_ids=FixedFillIds("execution-1")).execute(decision())  # type: ignore[arg-type]


def test_zero_fee_latency_and_global_context_independence() -> None:
    service = provider(config=PaperExecutionConfig(fee_rate=D(0), latency_ms=17))
    first = service.execute(decision())  # type: ignore[arg-type]
    with localcontext() as context:
        context.prec = 6
        second = service.execute(decision())  # type: ignore[arg-type]
    assert first.fill == second.fill
    assert first.fill is not None and first.fill.fee == 0
    assert first.completed_at_ms == NOW + 17

    custom = provider(config=PaperExecutionConfig(fee_rate=D("0.0005"))).execute(
        decision()  # type: ignore[arg-type]
    )
    assert custom.fill is not None and custom.fill.fee == D("2.00")

    rounded = provider(config=PaperExecutionConfig(fee_rate=D("0.000333"))).execute(
        decision()  # type: ignore[arg-type]
    )
    assert rounded.fill is not None and rounded.fill.fee == D("1.33")
    negative_zero = provider(config=PaperExecutionConfig(fee_rate=D("-0"))).execute(
        decision()  # type: ignore[arg-type]
    )
    assert negative_zero.fill is not None and negative_zero.fill.fee == D("0.00")


def test_cross_model_corruption_is_typed() -> None:
    approved = decision()
    order = approved.approved_order  # type: ignore[union-attr]
    audit = approved.sizing_audit  # type: ignore[union-attr]
    cases = [
        replace(approved, stream_key=MarketStreamKey("ETHUSDT", "1m")),
        replace(approved, sizing_audit=replace(audit, instrument_rules_version="other")),
        replace(approved, approved_order=replace(order, quantity=D("0.080001"))),
        replace(approved, approved_order=replace(order, reference_price=D("50000.001"))),
        replace(approved, approved_order=replace(order, gross_notional=D("1.00"))),
    ]
    for corrupted in cases:
        with pytest.raises(ExecutionInvariantError):
            provider().execute(corrupted)
    inconsistent_exposure = decision()
    object.__setattr__(inconsistent_exposure, "approved_target_exposure", D("0.3"))
    with pytest.raises(ExecutionInvariantError, match="audit"):
        provider().execute(inconsistent_exposure)  # type: ignore[arg-type]

    misaligned = decision()
    misaligned_order = replace(misaligned.approved_order, quantity=D("0.080001"))  # type: ignore[union-attr]
    misaligned_audit = replace(misaligned.sizing_audit, rounded_order_quantity=D("0.080001"))  # type: ignore[union-attr]
    with pytest.raises(ExecutionInvariantError, match="aligned"):
        provider().execute(
            replace(misaligned, approved_order=misaligned_order, sizing_audit=misaligned_audit)
        )

    wrong_gross = decision()
    wrong_order = replace(wrong_gross.approved_order, gross_notional=D("1.00"))  # type: ignore[union-attr]
    wrong_audit = replace(wrong_gross.sizing_audit, approved_order_notional=D("1.00"))  # type: ignore[union-attr]
    with pytest.raises(ExecutionInvariantError, match="gross"):
        provider().execute(
            replace(wrong_gross, approved_order=wrong_order, sizing_audit=wrong_audit)
        )


def test_fee_above_risk_envelope_and_invalid_input() -> None:
    with pytest.raises(ExecutionConfigurationError, match="envelope"):
        provider(config=PaperExecutionConfig(fee_rate=D("0.002"))).execute(decision())  # type: ignore[arg-type]
    with pytest.raises(UnsupportedRiskDecisionError):
        provider().execute("bad")  # type: ignore[arg-type]


def test_decimal_dependency_failure_is_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_product(left: Decimal, right: Decimal) -> Decimal:
        del left, right
        raise InvalidOperation("broken arithmetic")

    monkeypatch.setattr("app.execution.paper.precise_product", fail_product)
    with pytest.raises(ExecutionArithmeticError, match="arithmetic") as captured:
        provider().execute(decision())  # type: ignore[arg-type]
    assert isinstance(captured.value.__cause__, InvalidOperation)


def test_fee_envelope_exact_lower_and_smallest_breach() -> None:
    assert (
        provider(config=PaperExecutionConfig(fee_rate=D("0.001")))
        .execute(
            decision()  # type: ignore[arg-type]
        )
        .status
        is PaperExecutionStatus.FILLED
    )
    assert (
        provider(config=PaperExecutionConfig(fee_rate=D("0.000999")))
        .execute(
            decision()  # type: ignore[arg-type]
        )
        .status
        is PaperExecutionStatus.FILLED
    )
    calls: list[str] = []
    service = provider(
        config=PaperExecutionConfig(fee_rate=D("0.0010000000000000000001")),
        execution_ids=FixedExecutionIds(calls=calls),
        fill_ids=FixedFillIds(calls=calls),
    )
    with pytest.raises(ExecutionConfigurationError, match="envelope"):
        service.execute(decision())  # type: ignore[arg-type]
    assert calls == ["execution"]


def test_constructor_contracts() -> None:
    with pytest.raises(ExecutionIdFactoryError):
        create_execution_id(object())  # type: ignore[arg-type]
    with pytest.raises(FillIdFactoryError):
        create_fill_id(object())  # type: ignore[arg-type]
    with pytest.raises(ExecutionConfigurationError):
        PaperExecutionProvider("bad", rules(), FixedExecutionIds(), FixedFillIds())  # type: ignore[arg-type]
    with pytest.raises(ExecutionConfigurationError):
        PaperExecutionProvider(
            PaperExecutionConfig(),
            "bad",
            FixedExecutionIds(),
            FixedFillIds(),  # type: ignore[arg-type]
        )
    with pytest.raises(ExecutionConfigurationError):
        PaperExecutionProvider(
            PaperExecutionConfig(),
            rules(),
            object(),
            FixedFillIds(),  # type: ignore[arg-type]
        )
    with pytest.raises(ExecutionConfigurationError):
        PaperExecutionProvider(
            PaperExecutionConfig(),
            rules(),
            FixedExecutionIds(),
            object(),  # type: ignore[arg-type]
        )


def test_corrupted_executable_decision_fields_are_rejected() -> None:
    incomplete = decision()
    object.__setattr__(incomplete, "approved_order", None)
    with pytest.raises(ExecutionInvariantError, match="incomplete"):
        provider().execute(incomplete)  # type: ignore[arg-type]

    buy_dust = decision()
    object.__setattr__(buy_dust.approved_order, "dust_liquidation", True)  # type: ignore[union-attr]
    with pytest.raises(ExecutionInvariantError, match="dust"):
        provider().execute(buy_dust)  # type: ignore[arg-type]

    envelope = decision()
    object.__setattr__(envelope.approved_order, "required_quote", D("1"))  # type: ignore[union-attr]
    with pytest.raises(ExecutionInvariantError, match="envelope"):
        provider().execute(envelope)  # type: ignore[arg-type]

    sell_required = decision(
        action=OrderIntentAction.EXIT_LONG, exposure="0", base="0.08000", quote="6000"
    )
    object.__setattr__(sell_required.approved_order, "required_quote", D("1"))  # type: ignore[union-attr]
    with pytest.raises(ExecutionInvariantError, match="SELL"):
        provider().execute(sell_required)  # type: ignore[arg-type]

    rejected_with_order = decision(stale=True)
    object.__setattr__(rejected_with_order, "approved_order", decision().approved_order)  # type: ignore[union-attr]
    with pytest.raises(ExecutionInvariantError, match="rejected"):
        provider().execute(rejected_with_order)  # type: ignore[arg-type]

    unsupported = decision()
    object.__setattr__(unsupported, "outcome", "unknown")
    with pytest.raises(UnsupportedRiskDecisionError, match="outcome"):
        provider().execute(unsupported)  # type: ignore[arg-type]


def test_result_invariants_and_nested_metadata() -> None:
    result = provider().execute(decision())  # type: ignore[arg-type]
    with pytest.raises(ExecutionModelValidationError):
        replace(result, completed_at_ms=result.started_at_ms + 1)
    with pytest.raises(ExecutionModelValidationError):
        replace(result, fill=None)
    with pytest.raises(ExecutionModelValidationError):
        replace(result, execution_id="other")
    with pytest.raises(ExecutionModelValidationError):
        replace(result, completed_at_ms=result.completed_at_ms + 1, latency_ms=1)
    with pytest.raises(ExecutionModelValidationError):
        replace(result, correlation_id="other")
    with pytest.raises(ExecutionModelValidationError):
        replace(result, metadata={"nested": {"bad": True}})
    rejected = provider().execute(decision(stale=True))  # type: ignore[arg-type]
    with pytest.raises(ExecutionModelValidationError):
        replace(rejected, fill=result.fill)
    with pytest.raises(ExecutionModelValidationError):
        replace(rejected, risk_rejection_reasons=())
    with pytest.raises(ExecutionModelValidationError):
        replace(result, risk_rejection_reasons=("unexpected",))
    with pytest.raises(ExecutionModelValidationError):
        replace(rejected, risk_rejection_reasons=("same", "same"))
    with pytest.raises(ExecutionModelValidationError):
        replace(rejected, risk_rejection_reasons=(" ",))
    with pytest.raises(FrozenInstanceError):
        result.status = PaperExecutionStatus.NOT_EXECUTED  # type: ignore[misc]


@pytest.mark.parametrize(
    "changes",
    [
        {"reason_code": ""},
        {"status": "FILLED"},
        {"started_at_ms": True},
        {"completed_at_ms": -1},
        {"completed_at_ms": NOW - 1, "latency_ms": 0},
        {"correlation_id": " "},
        {"risk_decision_id": "other"},
        {"intent_id": "other"},
    ],
)
def test_result_rejects_invalid_cross_fields(changes: dict[str, object]) -> None:
    result = provider().execute(decision())  # type: ignore[arg-type]
    with pytest.raises(ExecutionModelValidationError):
        replace(result, **changes)


def test_provider_is_stateless_and_does_not_mutate_decision() -> None:
    approved = decision()
    service = provider()
    first = service.execute(approved)  # type: ignore[arg-type]
    second = service.execute(approved)  # type: ignore[arg-type]
    assert first.fill == second.fill
    assert approved == decision()

    sequenced = provider(
        execution_ids=SequentialExecutionIds("execution"),
        fill_ids=SequentialFillIds("fill"),
    )
    one = sequenced.execute(approved)  # type: ignore[arg-type]
    two = sequenced.execute(approved)  # type: ignore[arg-type]
    assert one.execution_id != two.execution_id
    assert one.fill is not None and two.fill is not None
    assert one.fill.fill_id != two.fill.fill_id
    assert replace(one.fill, fill_id=two.fill.fill_id, order_id=two.fill.order_id) == two.fill


@pytest.mark.parametrize("timestamp", [0, 10**100])
def test_decision_derived_timestamp_boundaries(timestamp: int) -> None:
    approved = replace(decision(), decided_at_ms=timestamp)
    result = provider(config=PaperExecutionConfig(latency_ms=25)).execute(approved)  # type: ignore[arg-type]
    assert result.started_at_ms == timestamp
    assert result.completed_at_ms == timestamp + 25
    assert result.fill is not None and result.fill.filled_at_ms == timestamp + 25


@pytest.mark.parametrize(
    "source_reason",
    ["stale_market", "insufficient_quote", "no_position", "below_minimum"],
)
def test_rejected_reason_semantics_are_preserved(source_reason: str) -> None:
    rejected = replace(decision(stale=True), rejection_reasons=(source_reason,))
    result = provider().execute(rejected)  # type: ignore[arg-type]
    assert result.status is PaperExecutionStatus.NOT_EXECUTED
    assert result.risk_rejection_reasons == (source_reason,)
    assert result.reason_code == "risk_rejected"


def test_fill_is_accepted_by_portfolio_buy_and_full_sell() -> None:
    portfolio = Portfolio(
        PortfolioConfig(
            portfolio_id="paper-1",
            stream_key=KEY,
            base_asset="BTC",
            quote_asset="USDT",
            initial_base_balance=D(0),
            initial_quote_balance=D("10000"),
            instrument_rules=rules(),
        )
    )
    buy = provider().execute(decision()).fill  # type: ignore[arg-type]
    assert buy is not None
    after_buy = portfolio.apply_fill(buy, D("50000.00"))
    assert after_buy.base_balance == D("0.08000")
    assert after_buy.quote_balance == D("5996.00")

    sell_decision = decision(
        price="50000.00",
        exposure="0",
        action=OrderIntentAction.EXIT_LONG,
        base="0.08000",
        quote="5996.00",
    )
    sell = provider(
        execution_ids=FixedExecutionIds("execution-2"), fill_ids=FixedFillIds("fill-2")
    ).execute(sell_decision)  # type: ignore[arg-type]
    assert sell.fill is not None
    after_sell = portfolio.apply_fill(sell.fill, D("50000.00"))
    assert after_sell.position is None
    assert after_sell.base_balance == 0
    assert after_sell.realised_pnl == D("-8.00")


def test_dust_liquidation_fill_is_explicitly_accepted_by_portfolio() -> None:
    portfolio = Portfolio(
        PortfolioConfig(
            portfolio_id="dust-paper",
            stream_key=KEY,
            base_asset="BTC",
            quote_asset="USDT",
            initial_base_balance=D(0),
            initial_quote_balance=D("100"),
            instrument_rules=rules(),
        )
    )
    buy_decision = decision(price="1000.00", exposure="0.1", quote="100")
    buy_fill = provider().execute(buy_decision).fill  # type: ignore[arg-type]
    assert buy_fill is not None
    opened = portfolio.apply_fill(buy_fill, D("1000.00"))
    assert opened.base_balance == D("0.01000")

    marked = portfolio.mark_to_market(D("100.00"), NOW)
    exit_decision = decision(
        price="100.00",
        exposure="0",
        action=OrderIntentAction.EXIT_LONG,
        base="0.01000",
        quote=str(marked.quote_balance),
    )
    assert exit_decision.outcome is RiskDecisionOutcome.MODIFIED  # type: ignore[union-attr]
    assert exit_decision.approved_order.dust_liquidation is True  # type: ignore[union-attr]
    execution = provider(
        execution_ids=FixedExecutionIds("execution-dust"),
        fill_ids=FixedFillIds("fill-dust"),
    ).execute(exit_decision)  # type: ignore[arg-type]
    assert execution.fill is not None
    assert execution.fill.metadata["dust_liquidation"] is True
    closed = portfolio.apply_fill(execution.fill, D("100.00"))
    assert closed.position is None
    assert closed.base_balance == 0
    assert closed.realised_pnl == D("-9.01")
    assert closed.total_fees == D("0.01")
