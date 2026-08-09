import logging
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal

import pytest

from app.execution.models import PaperExecutionConfig, PaperExecutionResult, PaperExecutionStatus
from app.execution.paper import PaperExecutionProvider
from app.market.models import CandleSnapshot, MarketStreamKey
from app.paper.engine import PaperTradingEngine
from app.paper.exceptions import (
    PaperTradingConfigurationError,
    PaperTradingCycleIdFactoryError,
    PaperTradingExecutionError,
    PaperTradingInvariantError,
    PaperTradingPortfolioError,
    PaperTradingRiskError,
    PaperTradingStateError,
)
from app.paper.factories import (
    PaperTradingCycleIdFactory,
    UUIDPaperTradingCycleIdFactory,
    create_cycle_id,
)
from app.paper.models import (
    PaperTradingCycleOutcome,
    PaperTradingEngineActivity,
    PaperTradingEngineConfig,
    PaperTradingEngineHealth,
    PaperTradingFailureCategory,
    PaperTradingObservationStatus,
    PaperTradingRecord,
    PaperTradingStage,
)
from app.paper.observer import LoggingPaperTradingObserver, PaperTradingObserver
from app.persistence.models import InitialPortfolioRecord, SQLitePersistenceConfig
from app.persistence.runtime import (
    DurablePaperCycleStore,
    DurablePaperRuntimeConfig,
    snapshot_to_state,
    state_to_snapshot,
)
from app.persistence.sqlite import SQLiteTradingPersistence
from app.portfolio.models import InstrumentRules, PortfolioConfig
from app.portfolio.portfolio import Portfolio
from app.risk.manager import RiskManager
from app.risk.models import PortfolioRiskPolicy
from app.trading.intent import IntentPlanOutcome, IntentPlanResult
from app.trading.models import (
    MarketSnapshot,
    OrderIntent,
    OrderIntentAction,
    Signal,
    SignalDirection,
)
from app.trading.pipeline import DecisionPipelineResult, PipelineOutcome

D = Decimal
KEY = MarketStreamKey("BTCUSDT", "1m")


class Ids:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self.calls = 0

    def _next(self) -> str:
        self.calls += 1
        return f"{self.prefix}-{self.calls}"

    def new_cycle_id(self) -> str:
        return self._next()

    def new_risk_decision_id(self) -> str:
        return self._next()

    def new_execution_id(self) -> str:
        return self._next()

    def new_fill_id(self) -> str:
        return self._next()


class Observer:
    def __init__(self, error: BaseException | None = None) -> None:
        self.records = []
        self.error = error

    def on_cycle(self, record: object) -> None:
        self.records.append(record)
        if self.error is not None:
            raise self.error


class BrokenRisk(RiskManager):
    def evaluate(self, *args: object, **kwargs: object) -> object:
        raise RuntimeError("risk unavailable")


class BrokenExecution:
    def __init__(self, instrument_rules: InstrumentRules) -> None:
        self.instrument_rules = instrument_rules

    def execute(self, decision: object) -> object:
        raise RuntimeError("execution unavailable")


class NotExecuted:
    def __init__(self, instrument_rules: InstrumentRules) -> None:
        self.instrument_rules = instrument_rules

    def execute(self, risk: object) -> PaperExecutionResult:
        return PaperExecutionResult(
            execution_id="execution-none",
            risk_decision_id=risk.decision_id,  # type: ignore[attr-defined]
            intent_id=risk.intent_id,  # type: ignore[attr-defined]
            status=PaperExecutionStatus.NOT_EXECUTED,
            fill=None,
            started_at_ms=risk.decided_at_ms,  # type: ignore[attr-defined]
            completed_at_ms=risk.decided_at_ms,  # type: ignore[attr-defined]
            latency_ms=0,
            provider_name="fake",
            provider_version="1",
            reason_code="not_executed",
            risk_rejection_reasons=("provider_declined",),
            correlation_id=risk.correlation_id,  # type: ignore[attr-defined]
        )


class BrokenPortfolio(Portfolio):
    def apply_fill(self, fill: object, mark_price: Decimal) -> object:
        raise RuntimeError("apply unavailable")


class BrokenMarkPortfolio(Portfolio):
    def mark_to_market(self, *args: object, **kwargs: object) -> object:
        raise RuntimeError("mark unavailable")


class CorruptMarkPortfolio(Portfolio):
    def mark_to_market(self, *args: object, **kwargs: object) -> object:
        result = super().mark_to_market(*args, **kwargs)  # type: ignore[arg-type]
        return replace(result, portfolio_id="wrong")


class CorruptApplyPortfolio(Portfolio):
    def apply_fill(self, *args: object, **kwargs: object) -> object:
        result = super().apply_fill(*args, **kwargs)  # type: ignore[arg-type]
        return replace(result, version=result.version + 1)


class BrokenCommittedSyncPortfolio(Portfolio):
    def apply_committed_transition(self, transition: object) -> object:
        raise RuntimeError("committed sync unavailable")


class CommitFailingPersistence(SQLiteTradingPersistence):
    def begin_cycle(self):  # type: ignore[no-untyped-def]
        transaction = super().begin_cycle()

        def fail_commit() -> None:
            transaction.rollback()
            raise RuntimeError("injected commit failure")

        transaction.commit = fail_commit  # type: ignore[method-assign]
        return transaction


def rules() -> InstrumentRules:
    return InstrumentRules(
        stream_key=KEY,
        base_asset="BTC",
        quote_asset="USDT",
        price_tick=D("0.01"),
        quantity_step=D("0.00001"),
        min_quantity=D("0.00001"),
        min_notional=D("10"),
        base_asset_precision=5,
        quote_asset_precision=2,
        rules_version="btc-usdt/1",
    )


def portfolio(kind: type[Portfolio] = Portfolio) -> Portfolio:
    return kind(portfolio_config())


def portfolio_config() -> PortfolioConfig:
    return PortfolioConfig(
        portfolio_id="portfolio-1",
        stream_key=KEY,
        base_asset="BTC",
        quote_asset="USDT",
        initial_base_balance=D(0),
        initial_quote_balance=D("10000"),
        instrument_rules=rules(),
    )


def market(run: int = 1, price: str = "50000.00") -> MarketSnapshot:
    as_of = run * 60_000
    candle = CandleSnapshot(
        open_time_ms=as_of - 60_000,
        close_time_ms=as_of - 1,
        open=D(price),
        high=D(price),
        low=D(price),
        close=D(price),
        volume=D(1),
    )
    return MarketSnapshot(
        snapshot_id=f"snapshot-{run}",
        stream_key=KEY,
        as_of_ms=as_of,
        closed_candles=(candle,),
        latest_closed_candle=candle,
        latest_open_candle=None,
        source_event_id=f"event-{run}",
        correlation_cycle_id=f"correlation-{run}",
        data_version="1",
        staleness_ms=0,
        is_stale=False,
    )


def market_at_1000(price: str = "50000.00") -> MarketSnapshot:
    candle = CandleSnapshot(
        open_time_ms=0,
        close_time_ms=999,
        open=D(price),
        high=D(price),
        low=D(price),
        close=D(price),
        volume=D(1),
    )
    return MarketSnapshot(
        snapshot_id="snapshot-1000",
        stream_key=KEY,
        as_of_ms=1000,
        closed_candles=(candle,),
        latest_closed_candle=candle,
        latest_open_candle=None,
        source_event_id="event-1000",
        correlation_cycle_id="correlation-1000",
        data_version="1",
        staleness_ms=0,
        is_stale=False,
    )


def decision(
    snapshot: MarketSnapshot,
    outcome: PipelineOutcome = PipelineOutcome.HOLD,
    *,
    action: OrderIntentAction = OrderIntentAction.ENTER_LONG,
    exposure: str = "0.4",
) -> DecisionPipelineResult:
    direction = {
        PipelineOutcome.HOLD: SignalDirection.HOLD,
        PipelineOutcome.INTENT_CREATED: (
            SignalDirection.BUY if action is OrderIntentAction.ENTER_LONG else SignalDirection.SELL
        ),
        PipelineOutcome.PLANNER_DECLINED: SignalDirection.BUY,
    }[outcome]
    signal_reason = {
        SignalDirection.BUY: "bullish_crossover",
        SignalDirection.SELL: "bearish_crossover",
        SignalDirection.HOLD: "insufficient_history",
    }[direction]
    signal = Signal(
        signal_id=f"signal-{snapshot.snapshot_id}",
        snapshot_id=snapshot.snapshot_id,
        stream_key=KEY,
        created_at_ms=snapshot.as_of_ms,
        effective_at_ms=snapshot.as_of_ms,
        direction=direction,
        reason_code=signal_reason,
        explanation="test",
        strategy_name="ema",
        strategy_version="1",
        strategy_parameters={},
        correlation_id=snapshot.correlation_cycle_id,
    )
    intent = None
    plan_outcome = IntentPlanOutcome.NO_INTENT_FOR_HOLD
    if outcome is PipelineOutcome.INTENT_CREATED:
        intent = OrderIntent(
            intent_id=f"intent-{snapshot.snapshot_id}",
            source_signal_id=signal.signal_id,
            stream_key=KEY,
            created_at_ms=snapshot.as_of_ms,
            action=action,
            target_exposure=D(exposure),
            reason="intent_reason",
            strategy_name="ema",
            strategy_version="1",
            correlation_id=snapshot.correlation_cycle_id,
        )
        plan_outcome = IntentPlanOutcome.CREATED
    elif outcome is PipelineOutcome.PLANNER_DECLINED:
        plan_outcome = IntentPlanOutcome.DECLINED
    plan = IntentPlanResult(
        outcome=plan_outcome,
        signal_id=signal.signal_id,
        intent=intent,
        reason_code="plan_reason",
        planner_name="planner",
        planner_version="1",
        correlation_id=snapshot.correlation_cycle_id,
    )
    stages = ("snapshot_validated", "strategy_evaluated", "intent_planned")
    if intent is not None:
        stages = (*stages, "intent_created")
    return DecisionPipelineResult(
        pipeline_run_id=f"pipeline-{snapshot.snapshot_id}",
        snapshot_id=snapshot.snapshot_id,
        stream_key=KEY,
        evaluated_at_ms=snapshot.as_of_ms,
        strategy_name="ema",
        strategy_version="1",
        signal=signal,
        intent_plan=plan,
        order_intent=intent,
        outcome=outcome,
        reason_code="pipeline_reason",
        correlation_id=snapshot.correlation_cycle_id,
        completed_stages=stages,
    )


def engine(
    *,
    account: Portfolio | None = None,
    observer: Observer | None = None,
    risk: RiskManager | None = None,
    execution: object | None = None,
    config: PaperTradingEngineConfig | None = None,
    cycle_ids: Ids | None = None,
    durable_store: DurablePaperCycleStore | None = None,
) -> PaperTradingEngine:
    account = account or portfolio()
    risk_ids = Ids("risk")
    risk = risk or RiskManager(
        PortfolioRiskPolicy(max_order_notional=D("10000"), max_position_notional=D("10000")),
        rules(),
        risk_ids,
    )
    execution = execution or PaperExecutionProvider(
        PaperExecutionConfig(), rules(), Ids("execution"), Ids("fill")
    )
    return PaperTradingEngine(
        config or PaperTradingEngineConfig(),
        account,
        risk,
        execution,  # type: ignore[arg-type]
        cycle_ids or Ids("cycle"),
        observer or Observer(),
        monotonic_clock=lambda: 1.0,
        durable_store=durable_store,
    )


def durable_store(
    adapter: SQLiteTradingPersistence, checkpoint_interval: int = 100
) -> DurablePaperCycleStore:
    return DurablePaperCycleStore(
        adapter,
        DurablePaperRuntimeConfig(
            exchange="binance",
            strategy_config_fingerprint="a" * 64,
            planner_name="planner",
            planner_version="1",
            planner_config_fingerprint="b" * 64,
            snapshot_policy_fingerprint="c" * 64,
            risk_policy_fingerprint="d" * 64,
            instrument_rules_fingerprint="e" * 64,
            execution_config_fingerprint="f" * 64,
            checkpoint_interval_transitions=checkpoint_interval,
        ),
    )


def test_config_protocols_and_initial_status() -> None:
    config = PaperTradingEngineConfig()
    service = engine(config=config)
    assert service.status.activity is PaperTradingEngineActivity.IDLE
    assert service.status.health is PaperTradingEngineHealth.READY
    assert service.last_result is None
    assert isinstance(Ids("cycle"), PaperTradingCycleIdFactory)
    assert isinstance(Observer(), PaperTradingObserver)
    assert UUIDPaperTradingCycleIdFactory().new_cycle_id().strip()
    with pytest.raises(FrozenInstanceError):
        config.dedup_capacity = 1  # type: ignore[misc]


@pytest.mark.parametrize(
    "config",
    [
        PaperTradingEngineConfig,
        lambda: PaperTradingEngineConfig(engine_name=""),
        lambda: PaperTradingEngineConfig(engine_version=" "),
        lambda: PaperTradingEngineConfig(dedup_capacity=0),
        lambda: PaperTradingEngineConfig(dedup_capacity=True),
        lambda: PaperTradingEngineConfig(observe_no_intent=1),
    ],
)
def test_invalid_config(config: object) -> None:
    if config is PaperTradingEngineConfig:
        return
    with pytest.raises(PaperTradingConfigurationError):
        config()  # type: ignore[operator]


def test_hold_marks_observes_and_deduplicates() -> None:
    account = portfolio()
    observer = Observer()
    ids = Ids("cycle")
    service = engine(account=account, observer=observer, cycle_ids=ids)
    snapshot = market()
    source = decision(snapshot)
    result = service.process(source, snapshot)
    assert result.outcome is PaperTradingCycleOutcome.NO_INTENT
    assert result.portfolio_before.as_of_ms == snapshot.as_of_ms
    assert result.portfolio_after == result.portfolio_before
    assert result.observation_status is PaperTradingObservationStatus.OBSERVED
    assert len(observer.records) == 1
    assert service.process(source, snapshot) is result
    assert ids.calls == 1
    assert len(observer.records) == 1
    assert service.status.ignored_duplicates == 1


def test_hold_observation_can_be_disabled() -> None:
    observer = Observer()
    service = engine(
        observer=observer,
        config=PaperTradingEngineConfig(observe_no_intent=False),
    )
    snapshot = market()
    result = service.process(decision(snapshot), snapshot)
    assert result.observation_status is PaperTradingObservationStatus.NOT_ATTEMPTED
    assert observer.records == []


def test_planner_decline_is_separate_outcome() -> None:
    snapshot = market()
    service = engine()
    result = service.process(decision(snapshot, PipelineOutcome.PLANNER_DECLINED), snapshot)
    assert result.outcome is PaperTradingCycleOutcome.PLANNER_DECLINED
    assert service.status.planner_declined_cycles == 1


def test_buy_hold_sell_full_offline_flow() -> None:
    account = portfolio()
    observer = Observer()
    service = engine(account=account, observer=observer)
    buy_market = market(1, "50000.00")
    buy = service.process(decision(buy_market, PipelineOutcome.INTENT_CREATED), buy_market)
    assert buy.outcome is PaperTradingCycleOutcome.EXECUTED
    assert buy.portfolio_after.position is not None
    hold_market = market(2, "55000.00")
    hold = service.process(decision(hold_market), hold_market)
    assert hold.portfolio_after.unrealised_pnl > buy.portfolio_after.unrealised_pnl
    sell_market = market(3, "60000.00")
    sell = service.process(
        decision(
            sell_market,
            PipelineOutcome.INTENT_CREATED,
            action=OrderIntentAction.EXIT_LONG,
            exposure="0",
        ),
        sell_market,
    )
    assert sell.outcome is PaperTradingCycleOutcome.EXECUTED
    assert sell.portfolio_after.position is None
    assert sell.portfolio_after.realised_pnl > 0
    assert sell.portfolio_after.total_fees > 0
    assert len(observer.records) == 3


def test_manual_buy_cycle_has_exact_decimal_economics_and_id_chain() -> None:
    account = portfolio()
    observer = Observer()
    service = engine(account=account, observer=observer)
    snapshot = market_at_1000()
    result = service.process(decision(snapshot, PipelineOutcome.INTENT_CREATED), snapshot)
    fill = result.execution_result.fill  # type: ignore[union-attr]
    assert result.outcome is PaperTradingCycleOutcome.EXECUTED
    assert result.portfolio_version_at_start == 0
    assert result.portfolio_version_before == 1
    assert result.portfolio_version_after == 2
    assert fill is not None
    assert fill.quantity == D("0.08")
    assert fill.gross_notional == D("4000.00")
    assert fill.fee == D("4.00")
    assert result.portfolio_after.quote_balance == D("5996.00")
    assert result.portfolio_after.base_balance == D("0.08")
    assert result.portfolio_after.position is not None
    assert result.portfolio_after.position.cost_basis == D("4004.00")
    assert result.decision_pipeline_run_id == result.decision_result.pipeline_run_id
    assert result.intent_id == result.risk_decision.intent_id  # type: ignore[union-attr]
    assert result.risk_decision_id == fill.risk_decision_id
    assert result.execution_id == fill.order_id
    assert result.fill_id == fill.fill_id
    assert len(observer.records) == 1
    assert service.process(result.decision_result, snapshot) is result
    assert result.portfolio_after.applied_fill_count == 1


def test_full_real_component_scenario_has_exact_manual_accounting() -> None:
    account = portfolio()
    observer = Observer()
    ids = Ids("cycle")
    service = engine(account=account, observer=observer, cycle_ids=ids)

    first_market = market(1, "50000.00")
    hold = service.process(decision(first_market), first_market)
    assert hold.portfolio_version_at_start == 0
    assert hold.portfolio_version_before == 1
    assert hold.portfolio_after.current_equity == D("10000")

    buy_market = market(2, "50000.00")
    buy_decision = decision(buy_market, PipelineOutcome.INTENT_CREATED)
    buy = service.process(buy_decision, buy_market)
    assert buy.portfolio_after.quote_balance == D("5996.00")
    assert buy.portfolio_after.base_balance == D("0.08")
    assert buy.portfolio_after.position is not None
    assert buy.portfolio_after.position.cost_basis == D("4004.00")
    assert buy.portfolio_after.current_equity == D("9996.0000")

    high_market = market(3, "55000.00")
    high = service.process(decision(high_market), high_market)
    assert high.portfolio_after.unrealised_pnl == D("396.0000")
    assert high.portfolio_after.current_equity == D("10396.0000")
    assert high.portfolio_after.drawdown == D("0.0000")

    low_market = market(4, "45000.00")
    low = service.process(decision(low_market), low_market)
    assert low.portfolio_after.unrealised_pnl == D("-404.0000")
    assert low.portfolio_after.current_equity == D("9596.0000")
    assert low.portfolio_after.drawdown == D("800.0000")
    assert low.portfolio_after.max_drawdown == D("800.0000")
    version_after_low = account.snapshot.version
    assert service.process(high.decision_result, high_market) is high
    assert account.snapshot.version == version_after_low

    sell_market = market(5, "60000.00")
    sell_decision = decision(
        sell_market,
        PipelineOutcome.INTENT_CREATED,
        action=OrderIntentAction.EXIT_LONG,
        exposure="0",
    )
    sell = service.process(sell_decision, sell_market)
    assert sell.portfolio_after.base_balance == D("0.00")
    assert sell.portfolio_after.quote_balance == D("10791.20")
    assert sell.portfolio_after.position is None
    assert sell.portfolio_after.realised_pnl == D("791.20")
    assert sell.portfolio_after.total_fees == D("8.80")
    assert sell.portfolio_after.current_equity == D("10791.20")
    assert sell.portfolio_after.max_drawdown == D("800.0000")
    assert sell.portfolio_version_at_start == 5
    assert sell.portfolio_version_before == 6
    assert sell.portfolio_version_after == 7
    sell_record = observer.records[-1]
    assert sell_record.side == "SELL"
    assert sell_record.quantity == D("0.08")
    assert sell_record.gross_notional == D("4800.00")
    assert sell_record.fee == D("4.80")
    assert service.process(sell_decision, sell_market) is sell
    assert account.snapshot.applied_fill_count == 2
    assert len(observer.records) == 5
    assert ids.calls == 5


def test_risk_rejection_does_not_execute() -> None:
    snapshot = replace(market(), is_stale=True, staleness_ms=10)
    service = engine()
    result = service.process(decision(snapshot, PipelineOutcome.INTENT_CREATED), snapshot)
    assert result.outcome is PaperTradingCycleOutcome.RISK_REJECTED
    assert result.execution_result is None
    assert service.status.risk_rejected_cycles == 1


def test_all_risk_rejection_reasons_reach_compact_record_and_dedup() -> None:
    class MultiReasonRisk(RiskManager):
        def evaluate(self, *args: object, **kwargs: object) -> object:
            result = super().evaluate(*args, **kwargs)  # type: ignore[arg-type]
            return replace(result, rejection_reasons=("stale_market", "policy_block"))

    risk = MultiReasonRisk(
        PortfolioRiskPolicy(max_order_notional=D("10000"), max_position_notional=D("10000")),
        rules(),
        Ids("risk"),
    )
    observer = Observer()
    service = engine(risk=risk, observer=observer)
    snapshot = replace(market(), is_stale=True, staleness_ms=1)
    source = decision(snapshot, PipelineOutcome.INTENT_CREATED)
    result = service.process(source, snapshot)
    assert observer.records[0].risk_rejection_reasons == (
        "stale_market",
        "policy_block",
    )
    assert service.process(source, snapshot) is result
    assert len(observer.records) == 1


def test_observer_exception_is_isolated_and_cached() -> None:
    observer = Observer(RuntimeError("observer boom"))
    service = engine(observer=observer)
    snapshot = market()
    source = decision(snapshot)
    result = service.process(source, snapshot)
    assert result.outcome is PaperTradingCycleOutcome.NO_INTENT
    assert result.observation_status is PaperTradingObservationStatus.OBSERVATION_FAILED
    assert result.failure_category is PaperTradingFailureCategory.OBSERVER
    assert service.status.health is PaperTradingEngineHealth.READY
    assert service.process(source, snapshot) is result
    assert len(observer.records) == 1


def test_observer_failure_after_committed_buy_is_safe_and_next_cycle_succeeds() -> None:
    account = portfolio()
    observer = Observer(RuntimeError("observer boom"))
    service = engine(account=account, observer=observer)
    buy_market = market(1)
    source = decision(buy_market, PipelineOutcome.INTENT_CREATED)
    result = service.process(source, buy_market)
    committed = account.snapshot
    assert result.outcome is PaperTradingCycleOutcome.EXECUTED
    assert result.observation_status is PaperTradingObservationStatus.OBSERVATION_FAILED
    assert result.failure_category is PaperTradingFailureCategory.OBSERVER
    assert service.status.observation_failures == 1
    assert service.status.failed_cycles == 0
    assert service.status.health is PaperTradingEngineHealth.READY
    assert service.process(source, buy_market) is result
    assert account.snapshot == committed
    assert len(observer.records) == 1
    observer.error = None
    hold_market = market(2, "51000.00")
    next_result = service.process(decision(hold_market), hold_market)
    assert next_result.outcome is PaperTradingCycleOutcome.NO_INTENT
    assert next_result.observation_status is PaperTradingObservationStatus.OBSERVED
    assert service.status.last_error_category is None


def test_observer_base_exception_is_not_swallowed() -> None:
    service = engine(observer=Observer(KeyboardInterrupt()))
    snapshot = market()
    with pytest.raises(KeyboardInterrupt):
        service.process(decision(snapshot), snapshot)
    assert service.status.activity is PaperTradingEngineActivity.IDLE
    assert service.status.health is PaperTradingEngineHealth.DEGRADED
    assert service.status.failed_cycles == 1
    assert service.last_result is None


@pytest.mark.parametrize("failure", [KeyboardInterrupt(), SystemExit(), GeneratorExit()])
def test_dependency_base_exception_before_trade_commit_is_not_swallowed(
    failure: BaseException,
) -> None:
    class InterruptingRisk(RiskManager):
        def evaluate(self, *args: object, **kwargs: object) -> object:
            raise failure

    risk = InterruptingRisk(
        PortfolioRiskPolicy(max_order_notional=D("10000"), max_position_notional=D("10000")),
        rules(),
        Ids("risk"),
    )
    service = engine(risk=risk)
    snapshot = market()
    with pytest.raises(type(failure)):
        service.process(decision(snapshot, PipelineOutcome.INTENT_CREATED), snapshot)
    assert service.status.activity is PaperTradingEngineActivity.IDLE
    assert service.status.health is PaperTradingEngineHealth.READY
    assert service.last_result is None


def test_risk_exception_is_wrapped_and_engine_reusable() -> None:
    broken = BrokenRisk(
        PortfolioRiskPolicy(max_order_notional=D("10000"), max_position_notional=D("10000")),
        rules(),
        Ids("risk"),
    )
    service = engine(risk=broken)
    snapshot = market()
    with pytest.raises(PaperTradingRiskError) as captured:
        service.process(decision(snapshot, PipelineOutcome.INTENT_CREATED), snapshot)
    assert isinstance(captured.value.__cause__, RuntimeError)
    assert service.status.health is PaperTradingEngineHealth.READY
    assert service.status.activity is PaperTradingEngineActivity.IDLE


def test_execution_exception_is_wrapped() -> None:
    service = engine(execution=BrokenExecution(rules()))
    snapshot = market()
    with pytest.raises(PaperTradingExecutionError) as captured:
        service.process(decision(snapshot, PipelineOutcome.INTENT_CREATED), snapshot)
    assert isinstance(captured.value.__cause__, RuntimeError)
    assert service.status.health is PaperTradingEngineHealth.READY


def test_apply_failure_degrades_and_blocks_future_calls() -> None:
    service = engine(account=portfolio(BrokenPortfolio))
    snapshot = market()
    with pytest.raises(PaperTradingPortfolioError) as captured:
        service.process(decision(snapshot, PipelineOutcome.INTENT_CREATED), snapshot)
    assert isinstance(captured.value.__cause__, RuntimeError)
    assert service.status.health is PaperTradingEngineHealth.DEGRADED
    assert service.status.activity is PaperTradingEngineActivity.IDLE
    assert service.status.failed_cycles == 1
    assert service.status.last_error_category is PaperTradingFailureCategory.PORTFOLIO_APPLY
    assert service.status.last_error_message
    assert service.last_result is None
    with pytest.raises(PaperTradingStateError):
        service.process(decision(snapshot), snapshot)
    assert service.status.failed_cycles == 1


def test_input_mismatch_prevents_mutation() -> None:
    account = portfolio()
    service = engine(account=account)
    before = account.snapshot
    snapshot = market()
    with pytest.raises(Exception, match="do not match"):
        service.process(decision(snapshot), market(2))
    assert account.snapshot == before


def test_fifo_eviction_and_duplicate_does_not_reorder() -> None:
    ids = Ids("cycle")
    service = engine(config=PaperTradingEngineConfig(dedup_capacity=2), cycle_ids=ids)
    first_market = market(1)
    first = decision(first_market)
    service.process(first, first_market)
    second_market = market(2)
    second = decision(second_market)
    service.process(second, second_market)
    service.process(first, first_market)
    third_market = market(3)
    service.process(decision(third_market), third_market)
    with pytest.raises(PaperTradingPortfolioError):
        service.process(first, first_market)
    assert ids.calls == 4


def test_capacity_one_reprocesses_evicted_key_with_new_id_and_observation() -> None:
    ids = Ids("cycle")
    observer = Observer()
    service = engine(
        config=PaperTradingEngineConfig(dedup_capacity=1),
        cycle_ids=ids,
        observer=observer,
    )
    first_market = market(1)
    first = decision(first_market)
    first_result = service.process(first, first_market)
    second_market = replace(
        first_market,
        snapshot_id="snapshot-second",
        source_event_id="event-second",
        correlation_cycle_id="correlation-second",
    )
    service.process(decision(second_market), second_market)
    repeated = service.process(first, first_market)
    assert ids.calls == 3
    assert len(observer.records) == 3
    assert first_result.cycle_id == "cycle-1"
    assert repeated.cycle_id == "cycle-3"


def test_dependencies_are_not_closed() -> None:
    account = portfolio()
    service = engine(account=account)
    assert not hasattr(service, "close")
    assert not hasattr(service, "start")
    assert not hasattr(service, "stop")


def test_not_executed_does_not_apply_fill() -> None:
    account = portfolio()
    service = engine(account=account, execution=NotExecuted(rules()))
    snapshot = market()
    result = service.process(decision(snapshot, PipelineOutcome.INTENT_CREATED), snapshot)
    assert result.outcome is PaperTradingCycleOutcome.NOT_EXECUTED
    assert result.execution_result is not None
    assert result.fill_id is None
    assert result.portfolio_after.applied_fill_count == 0
    assert result.completed_stages[-1] is PaperTradingStage.NO_FILL


def test_reentrant_process_is_rejected_without_breaking_outer_cycle() -> None:
    snapshot = market()
    source = decision(snapshot)

    class ReentrantObserver:
        nested_error = None

        def on_cycle(self, record: object) -> None:
            try:
                service.process(source, snapshot)
            except PaperTradingStateError as error:
                self.nested_error = error

    observer = ReentrantObserver()
    service = engine(observer=observer)  # type: ignore[arg-type]
    result = service.process(source, snapshot)
    assert result.observation_status is PaperTradingObservationStatus.OBSERVED
    assert isinstance(observer.nested_error, PaperTradingStateError)
    assert service.status.activity is PaperTradingEngineActivity.IDLE


@pytest.mark.parametrize("value", ["", " ", 1])
def test_invalid_cycle_factory_output(value: object) -> None:
    class Factory:
        def new_cycle_id(self) -> object:
            return value

    with pytest.raises(PaperTradingCycleIdFactoryError):
        create_cycle_id(Factory())  # type: ignore[arg-type]


def test_cycle_factory_rejects_non_protocol_object() -> None:
    with pytest.raises(PaperTradingCycleIdFactoryError, match="protocol"):
        create_cycle_id(object())  # type: ignore[arg-type]


def test_cycle_factory_exception_preserves_cause() -> None:
    class Factory:
        def new_cycle_id(self) -> str:
            raise RuntimeError("factory boom")

    with pytest.raises(PaperTradingCycleIdFactoryError) as captured:
        create_cycle_id(Factory())
    assert isinstance(captured.value.__cause__, RuntimeError)


def test_engine_factory_failure_consumes_no_dependencies_and_next_cycle_succeeds() -> None:
    class FailOnce:
        calls = 0

        def new_cycle_id(self) -> str:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("factory boom")
            return "cycle-ok"

    factory = FailOnce()
    observer = Observer()
    service = engine(cycle_ids=factory, observer=observer)  # type: ignore[arg-type]
    snapshot = market()
    source = decision(snapshot)
    with pytest.raises(PaperTradingCycleIdFactoryError) as captured:
        service.process(source, snapshot)
    assert isinstance(captured.value.__cause__, RuntimeError)
    assert service.status.activity is PaperTradingEngineActivity.IDLE
    assert service.status.health is PaperTradingEngineHealth.READY
    assert service.status.handled_decisions == 1
    assert observer.records == []
    result = service.process(source, snapshot)
    assert result.cycle_id == "cycle-ok"
    assert service.status.handled_decisions == 2


def test_result_models_are_immutable_and_validate_stages() -> None:
    snapshot = market()
    result = engine().process(decision(snapshot), snapshot)
    with pytest.raises(FrozenInstanceError):
        result.outcome = PaperTradingCycleOutcome.EXECUTED  # type: ignore[misc]
    with pytest.raises(TypeError):
        result.metadata["x"] = 1  # type: ignore[index]
    with pytest.raises(PaperTradingInvariantError, match="duplicates"):
        replace(result, completed_stages=(*result.completed_stages, result.completed_stages[-1]))
    with pytest.raises(PaperTradingInvariantError, match="match outcome"):
        replace(result, completed_stages=result.completed_stages[:-1])
    with pytest.raises(PaperTradingInvariantError, match="trade data"):
        replace(result, intent_id="intent")


def test_result_models_reject_cross_outcome_corruption() -> None:
    hold_market = market()
    hold = engine().process(decision(hold_market), hold_market)
    for changes in (
        {"portfolio_before": object()},
        {"portfolio_version_at_start": 99},
        {"decision_result": object()},
        {
            "failure_category": PaperTradingFailureCategory.EXECUTION,
            "failure_message": "wrong",
        },
        {
            "failure_category": PaperTradingFailureCategory.OBSERVER,
            "failure_message": "observer",
        },
    ):
        with pytest.raises(PaperTradingInvariantError):
            replace(hold, **changes)

    rejected_market = replace(market(2), is_stale=True, staleness_ms=1)
    rejected = engine().process(
        decision(rejected_market, PipelineOutcome.INTENT_CREATED), rejected_market
    )
    with pytest.raises(PaperTradingInvariantError, match="risk-rejected"):
        replace(rejected, risk_decision_id="wrong")

    no_fill_market = market(3)
    no_fill = engine(execution=NotExecuted(rules())).process(
        decision(no_fill_market, PipelineOutcome.INTENT_CREATED), no_fill_market
    )
    with pytest.raises(PaperTradingInvariantError, match="not-executed"):
        replace(no_fill, execution_id="wrong")

    executed_market = market(4)
    executed = engine().process(
        decision(executed_market, PipelineOutcome.INTENT_CREATED), executed_market
    )
    with pytest.raises(PaperTradingInvariantError, match="executed"):
        replace(executed, fill_id="wrong")


def test_record_is_compact_immutable_and_validated() -> None:
    observer = Observer()
    snapshot = market()
    engine(observer=observer).process(decision(snapshot), snapshot)
    record = observer.records[0]
    assert isinstance(record, PaperTradingRecord)
    assert not hasattr(record, "portfolio_after")
    assert not hasattr(record, "market_snapshot")
    with pytest.raises(TypeError):
        record.metadata["x"] = 1  # type: ignore[index]
    with pytest.raises(PaperTradingInvariantError):
        replace(record, equity=D("NaN"))


def test_logging_observer_emits_compact_decimal_strings(caplog: pytest.LogCaptureFixture) -> None:
    observer = Observer()
    snapshot = market()
    engine(observer=observer).process(decision(snapshot), snapshot)
    record = observer.records[0]
    logger = logging.getLogger("paper-test")
    with caplog.at_level(logging.INFO, logger="paper-test"):
        LoggingPaperTradingObserver(logger).on_cycle(record)
    assert "cycle_id=" in caplog.text
    assert "equity=10000" in caplog.text
    assert "closed_candles" not in caplog.text


def test_constructor_rejects_invalid_dependencies() -> None:
    account = portfolio()
    valid_risk = RiskManager(
        PortfolioRiskPolicy(max_order_notional=D("10000"), max_position_notional=D("10000")),
        rules(),
        Ids("risk"),
    )
    valid_execution = PaperExecutionProvider(
        PaperExecutionConfig(), rules(), Ids("execution"), Ids("fill")
    )
    values = [
        (object(), account, valid_risk, valid_execution, Ids("cycle"), Observer(), lambda: 0),
        (
            PaperTradingEngineConfig(),
            object(),
            valid_risk,
            valid_execution,
            Ids("cycle"),
            Observer(),
            lambda: 0,
        ),
        (
            PaperTradingEngineConfig(),
            account,
            object(),
            valid_execution,
            Ids("cycle"),
            Observer(),
            lambda: 0,
        ),
        (
            PaperTradingEngineConfig(),
            account,
            valid_risk,
            object(),
            Ids("cycle"),
            Observer(),
            lambda: 0,
        ),
        (
            PaperTradingEngineConfig(),
            account,
            valid_risk,
            valid_execution,
            object(),
            Observer(),
            lambda: 0,
        ),
        (
            PaperTradingEngineConfig(),
            account,
            valid_risk,
            valid_execution,
            Ids("cycle"),
            object(),
            lambda: 0,
        ),
        (
            PaperTradingEngineConfig(),
            account,
            valid_risk,
            valid_execution,
            Ids("cycle"),
            Observer(),
            object(),
        ),
    ]
    for args in values:
        with pytest.raises(PaperTradingConfigurationError):
            PaperTradingEngine(*args)  # type: ignore[arg-type]


def test_constructor_rejects_stream_and_rules_mismatches() -> None:
    btc_rules = rules()
    account = portfolio()
    risk = RiskManager(
        PortfolioRiskPolicy(max_order_notional=D("10000"), max_position_notional=D("10000")),
        btc_rules,
        Ids("risk"),
    )

    def execution_with(custom: InstrumentRules) -> PaperExecutionProvider:
        return PaperExecutionProvider(PaperExecutionConfig(), custom, Ids("execution"), Ids("fill"))

    eth_rules = replace(btc_rules, stream_key=MarketStreamKey("ETHUSDT", "1m"))
    with pytest.raises(PaperTradingConfigurationError, match="streams"):
        PaperTradingEngine(
            PaperTradingEngineConfig(),
            account,
            risk,
            execution_with(eth_rules),
            Ids("cycle"),
            Observer(),
        )
    with pytest.raises(PaperTradingConfigurationError, match="versions"):
        PaperTradingEngine(
            PaperTradingEngineConfig(),
            account,
            risk,
            execution_with(replace(btc_rules, rules_version="other")),
            Ids("cycle"),
            Observer(),
        )
    eth_account = Portfolio(
        PortfolioConfig(
            portfolio_id="eth",
            stream_key=eth_rules.stream_key,
            base_asset="BTC",
            quote_asset="USDT",
            initial_base_balance=D(0),
            initial_quote_balance=D("10000"),
            instrument_rules=eth_rules,
        )
    )
    with pytest.raises(PaperTradingConfigurationError, match="portfolio stream"):
        PaperTradingEngine(
            PaperTradingEngineConfig(),
            eth_account,
            risk,
            execution_with(btc_rules),
            Ids("cycle"),
            Observer(),
        )


@pytest.mark.parametrize(
    "case",
    [
        "decision_type",
        "market_type",
        "timestamp",
        "correlation",
        "signal_snapshot",
        "signal_time",
        "intent_signal",
        "missing_intent",
    ],
)
def test_input_cross_model_corruption_is_typed_before_portfolio_mutation(case: str) -> None:
    snapshot = market()
    source = decision(snapshot, PipelineOutcome.INTENT_CREATED)
    decision_input: object = source
    market_input: object = snapshot
    if case == "decision_type":
        decision_input = object()
    elif case == "market_type":
        market_input = object()
    elif case == "timestamp":
        object.__setattr__(source, "evaluated_at_ms", snapshot.as_of_ms + 1)
    elif case == "correlation":
        object.__setattr__(source, "correlation_id", "wrong")
    elif case == "signal_snapshot":
        object.__setattr__(source.signal, "snapshot_id", "wrong")
    elif case == "signal_time":
        object.__setattr__(source.signal, "effective_at_ms", snapshot.as_of_ms + 1)
    elif case == "intent_signal":
        object.__setattr__(source.order_intent, "source_signal_id", "wrong")
    else:
        object.__setattr__(source, "order_intent", None)
    account = portfolio()
    before = account.snapshot
    service = engine(account=account)
    with pytest.raises(PaperTradingInvariantError):
        service.process(decision_input, market_input)  # type: ignore[arg-type]
    assert account.snapshot == before


def test_empty_closed_history_and_mark_failures_are_typed() -> None:
    empty = MarketSnapshot(
        snapshot_id="empty",
        stream_key=KEY,
        as_of_ms=1000,
        closed_candles=(),
        latest_closed_candle=None,
        latest_open_candle=None,
        source_event_id="event-empty",
        correlation_cycle_id="correlation-empty",
        data_version="1",
        staleness_ms=0,
        is_stale=False,
    )
    service = engine()
    with pytest.raises(PaperTradingInvariantError, match="closed candle"):
        service.process(decision(empty), empty)

    snapshot = market()
    broken = engine(account=portfolio(BrokenMarkPortfolio))
    with pytest.raises(PaperTradingPortfolioError) as captured:
        broken.process(decision(snapshot), snapshot)
    assert isinstance(captured.value.__cause__, RuntimeError)

    corrupt = engine(account=portfolio(CorruptMarkPortfolio))
    with pytest.raises(PaperTradingPortfolioError, match="valuation result"):
        corrupt.process(decision(snapshot), snapshot)


def test_corrupt_execution_and_applied_portfolio_are_typed() -> None:
    class CorruptExecution(PaperExecutionProvider):
        def execute(self, risk: object) -> object:
            result = super().execute(risk)  # type: ignore[arg-type]
            object.__setattr__(result, "risk_decision_id", "wrong")
            return result

    execution = CorruptExecution(PaperExecutionConfig(), rules(), Ids("execution"), Ids("fill"))
    snapshot = market()
    service = engine(execution=execution)
    with pytest.raises(PaperTradingExecutionError, match="invalid"):
        service.process(decision(snapshot, PipelineOutcome.INTENT_CREATED), snapshot)
    assert service.status.health is PaperTradingEngineHealth.READY

    corrupt_portfolio = engine(account=portfolio(CorruptApplyPortfolio))
    with pytest.raises(PaperTradingPortfolioError):
        corrupt_portfolio.process(decision(snapshot, PipelineOutcome.INTENT_CREATED), snapshot)
    assert corrupt_portfolio.status.health is PaperTradingEngineHealth.DEGRADED


def test_durable_full_restart_reuses_terminal_sell_without_second_fill(tmp_path) -> None:
    database = tmp_path / "paper.sqlite3"
    account_a = portfolio()
    adapter_a = SQLiteTradingPersistence(SQLitePersistenceConfig(database))
    adapter_a.initialize_portfolio(
        InitialPortfolioRecord(snapshot_to_state(account_a.snapshot), "9" * 64, 0)
    )
    execution_ids = Ids("execution")
    fill_ids = Ids("fill")
    execution = PaperExecutionProvider(PaperExecutionConfig(), rules(), execution_ids, fill_ids)
    runtime_a = engine(
        account=account_a,
        execution=execution,
        durable_store=durable_store(adapter_a),
    )
    buy_market = market(1, "50000.00")
    buy = runtime_a.process(decision(buy_market, PipelineOutcome.INTENT_CREATED), buy_market)
    hold_market = market(2, "55000.00")
    hold = runtime_a.process(decision(hold_market), hold_market)
    sell_market = market(3, "60000.00")
    sell_decision = decision(
        sell_market,
        PipelineOutcome.INTENT_CREATED,
        action=OrderIntentAction.EXIT_LONG,
        exposure="0",
    )
    sell = runtime_a.process(sell_decision, sell_market)
    assert buy.durable_committed is hold.durable_committed is sell.durable_committed is True
    assert sell.portfolio_after.position is None
    committed = sell.portfolio_after
    adapter_a.close()

    adapter_b = SQLiteTradingPersistence(SQLitePersistenceConfig(database))
    recovery = adapter_b.recover("portfolio-1", expected_config_fingerprint="9" * 64)
    assert recovery.portfolio_state is not None
    account_b = Portfolio.restore(portfolio_config(), state_to_snapshot(recovery.portfolio_state))
    runtime_b = engine(
        account=account_b,
        execution=execution,
        durable_store=durable_store(adapter_b),
    )
    replay = runtime_b.process(sell_decision, sell_market)
    assert replay.outcome is PaperTradingCycleOutcome.DURABLE_REPLAY
    assert replay.recovered_existing is True
    assert replay.portfolio_after == committed
    assert execution_ids.calls == 2
    assert fill_ids.calls == 2
    assert len(adapter_b.load_transitions("portfolio-1")) == 5
    new_market = market(4, "61000.00")
    continued = runtime_b.process(decision(new_market), new_market)
    assert continued.durable_committed is True
    assert continued.portfolio_after.version == committed.version + 1
    adapter_b.close()


def test_durable_hold_persists_valuation_and_restores_exactly(tmp_path) -> None:
    database = tmp_path / "hold.sqlite3"
    account = portfolio()
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(database))
    adapter.initialize_portfolio(
        InitialPortfolioRecord(snapshot_to_state(account.snapshot), "9" * 64, 0)
    )
    runtime = engine(account=account, durable_store=durable_store(adapter))
    result = runtime.process(decision(market()), market())
    assert result.outcome is PaperTradingCycleOutcome.NO_INTENT
    assert result.durable_committed is True
    adapter.close()

    reopened = SQLiteTradingPersistence(SQLitePersistenceConfig(database))
    recovered = reopened.recover("portfolio-1", expected_config_fingerprint="9" * 64)
    assert recovered.portfolio_state is not None
    assert state_to_snapshot(recovered.portfolio_state) == result.portfolio_after
    reopened.close()


def test_durable_decision_slot_correction_fails_closed_without_mutation(tmp_path) -> None:
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(tmp_path / "slot.sqlite3"))
    account = portfolio()
    adapter.initialize_portfolio(
        InitialPortfolioRecord(snapshot_to_state(account.snapshot), "9" * 64, 0)
    )
    runtime = engine(account=account, durable_store=durable_store(adapter))
    original_market = market()
    runtime.process(decision(original_market), original_market)
    before = account.snapshot
    corrected_candle = replace(original_market.closed_candles[0], close=D("49999"))
    corrected = replace(
        original_market,
        closed_candles=(corrected_candle,),
        latest_closed_candle=corrected_candle,
    )
    corrected_decision = replace(decision(corrected), pipeline_run_id="pipeline-corrected")
    with pytest.raises(PaperTradingPortfolioError, match="durable"):
        runtime.process(corrected_decision, corrected)
    assert account.snapshot == before
    assert runtime.status.health is PaperTradingEngineHealth.DEGRADED
    adapter.close()


def test_durable_commit_failure_leaves_memory_and_database_uncommitted(tmp_path) -> None:
    database = tmp_path / "commit-failure.sqlite3"
    account = portfolio()
    adapter = CommitFailingPersistence(SQLitePersistenceConfig(database))
    adapter.initialize_portfolio(
        InitialPortfolioRecord(snapshot_to_state(account.snapshot), "9" * 64, 0)
    )
    runtime = engine(account=account, durable_store=durable_store(adapter))
    before = account.snapshot
    snapshot = market_at_1000()
    source = decision(snapshot, PipelineOutcome.INTENT_CREATED)
    with pytest.raises(PaperTradingPortfolioError, match="commit"):
        runtime.process(source, snapshot)
    assert account.snapshot == before
    assert (
        adapter.load_terminal_decision(
            durable_store(adapter).identity(source, snapshot).stable_decision_key
        )
        is None
    )
    adapter.close()

    reopened = SQLiteTradingPersistence(SQLitePersistenceConfig(database))
    recovered = reopened.recover("portfolio-1", expected_config_fingerprint="9" * 64)
    assert recovered.portfolio_state == snapshot_to_state(before)
    reopened.close()


def test_post_commit_sync_failure_requires_restart_and_never_reexecutes(tmp_path) -> None:
    database = tmp_path / "post-commit.sqlite3"
    account = BrokenCommittedSyncPortfolio(portfolio_config())
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(database))
    adapter.initialize_portfolio(
        InitialPortfolioRecord(snapshot_to_state(account.snapshot), "9" * 64, 0)
    )
    execution_ids = Ids("execution")
    fill_ids = Ids("fill")
    execution = PaperExecutionProvider(PaperExecutionConfig(), rules(), execution_ids, fill_ids)
    runtime = engine(
        account=account,
        execution=execution,
        durable_store=durable_store(adapter),
    )
    before = account.snapshot
    snapshot = market_at_1000()
    source = decision(snapshot, PipelineOutcome.INTENT_CREATED)
    with pytest.raises(PaperTradingPortfolioError, match="synchronization"):
        runtime.process(source, snapshot)
    assert account.snapshot == before
    assert runtime.status.health is PaperTradingEngineHealth.DEGRADED
    recovered = adapter.recover("portfolio-1", expected_config_fingerprint="9" * 64)
    assert recovered.portfolio_state is not None
    assert recovered.portfolio_state.version == 2
    adapter.close()

    reopened = SQLiteTradingPersistence(SQLitePersistenceConfig(database))
    recovered = reopened.recover("portfolio-1", expected_config_fingerprint="9" * 64)
    restored = Portfolio.restore(
        portfolio_config(),
        state_to_snapshot(recovered.portfolio_state),  # type: ignore[arg-type]
    )
    runtime_b = engine(
        account=restored,
        execution=execution,
        durable_store=durable_store(reopened),
    )
    replay = runtime_b.process(source, snapshot)
    assert replay.outcome is PaperTradingCycleOutcome.DURABLE_REPLAY
    assert execution_ids.calls == 1
    assert fill_ids.calls == 1
    reopened.close()


def test_observer_failure_after_durable_commit_does_not_allow_retrade(tmp_path) -> None:
    database = tmp_path / "observer.sqlite3"
    account = portfolio()
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(database))
    adapter.initialize_portfolio(
        InitialPortfolioRecord(snapshot_to_state(account.snapshot), "9" * 64, 0)
    )
    snapshot = market_at_1000()
    source = decision(snapshot, PipelineOutcome.INTENT_CREATED)
    result = engine(
        account=account,
        observer=Observer(RuntimeError("observer down")),
        durable_store=durable_store(adapter),
    ).process(source, snapshot)
    assert result.durable_committed is True
    assert result.observation_status is PaperTradingObservationStatus.OBSERVATION_FAILED
    assert adapter.load_terminal_decision(result.stable_decision_key) is not None  # type: ignore[arg-type]
    adapter.close()


def test_durable_store_guards_identity_and_checkpoint_failures(tmp_path) -> None:
    config_value = DurablePaperRuntimeConfig(
        exchange="binance",
        strategy_config_fingerprint="a" * 64,
        planner_name="planner",
        planner_version="1",
        planner_config_fingerprint="b" * 64,
        snapshot_policy_fingerprint="c" * 64,
        risk_policy_fingerprint="d" * 64,
        instrument_rules_fingerprint="e" * 64,
        execution_config_fingerprint="f" * 64,
    )
    with pytest.raises(TypeError, match="persistence"):
        DurablePaperCycleStore(object(), config_value)  # type: ignore[arg-type]
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(tmp_path / "guards.sqlite3"))
    with pytest.raises(TypeError, match="config"):
        DurablePaperCycleStore(adapter, object())  # type: ignore[arg-type]
    store = durable_store(adapter, checkpoint_interval=1)
    empty_market = replace(
        market(),
        closed_candles=(),
        latest_closed_candle=None,
    )
    with pytest.raises(ValueError, match="closed candle"):
        store.identity(decision(market()), empty_market)
    assert store.maybe_checkpoint(portfolio().snapshot, 0) is False
    adapter.close()
    valued = portfolio().preview_mark_to_market(D("100"), 1).after
    assert store.maybe_checkpoint(valued, 1) is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cycle_id", ""),
        ("portfolio_version_at_start", -1),
        ("portfolio_id", "other"),
        ("portfolio_version_after", 99),
        ("decision_pipeline_run_id", "other"),
        ("market_snapshot_id", "other"),
        ("signal_id", "other"),
        ("outcome", "executed"),
        ("observation_status", "observed"),
        ("completed_stages", ()),
        ("completed_stages", ("bad",)),
        ("failure_message", "orphan"),
        ("metadata", {"nested": object()}),
    ],
)
def test_cycle_result_rejects_invalid_boundaries(field: str, value: object) -> None:
    snapshot = market()
    result = engine().process(decision(snapshot), snapshot)
    with pytest.raises(PaperTradingInvariantError):
        replace(result, **{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("signal_direction", "buy"),
        ("cycle_outcome", "executed"),
        ("observation_status", "observed"),
        ("metadata", {"nested": object()}),
    ],
)
def test_record_rejects_invalid_boundaries(field: str, value: object) -> None:
    observer = Observer()
    snapshot = market()
    engine(observer=observer).process(decision(snapshot), snapshot)
    with pytest.raises(PaperTradingInvariantError):
        replace(observer.records[0], **{field: value})


def test_clock_failure_and_invalid_value_do_not_change_business_result() -> None:
    class Clock:
        calls = 0

        def __call__(self) -> object:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("clock")
            return "bad"

    account = portfolio()
    risk = RiskManager(
        PortfolioRiskPolicy(max_order_notional=D("10000"), max_position_notional=D("10000")),
        rules(),
        Ids("risk"),
    )
    execution = PaperExecutionProvider(
        PaperExecutionConfig(), rules(), Ids("execution"), Ids("fill")
    )
    service = PaperTradingEngine(
        PaperTradingEngineConfig(),
        account,
        risk,
        execution,
        Ids("cycle"),
        Observer(),
        Clock(),  # type: ignore[arg-type]
    )
    snapshot = market()
    result = service.process(decision(snapshot), snapshot)
    assert result.total_duration_ms == 0
    assert result.portfolio_duration_ms == 0


def test_deterministic_duration_metrics_cover_every_component() -> None:
    class Clock:
        value = -1

        def __call__(self) -> float:
            self.value += 1
            return float(self.value)

    account = portfolio()
    risk = RiskManager(
        PortfolioRiskPolicy(max_order_notional=D("10000"), max_position_notional=D("10000")),
        rules(),
        Ids("risk"),
    )
    execution = PaperExecutionProvider(
        PaperExecutionConfig(), rules(), Ids("execution"), Ids("fill")
    )
    service = PaperTradingEngine(
        PaperTradingEngineConfig(),
        account,
        risk,
        execution,
        Ids("cycle"),
        Observer(),
        Clock(),
    )
    snapshot = market()
    result = service.process(decision(snapshot, PipelineOutcome.INTENT_CREATED), snapshot)
    assert result.risk_duration_ms == 1000
    assert result.execution_duration_ms == 1000
    assert result.portfolio_duration_ms == 2000
    assert result.observation_duration_ms == 1000
    assert result.total_duration_ms == 12000


def test_corrupt_risk_result_is_typed_and_not_executed() -> None:
    class CorruptRisk(RiskManager):
        def evaluate(self, *args: object, **kwargs: object) -> object:
            result = super().evaluate(*args, **kwargs)  # type: ignore[arg-type]
            object.__setattr__(result, "market_snapshot_id", "wrong")
            return result

    risk = CorruptRisk(
        PortfolioRiskPolicy(max_order_notional=D("10000"), max_position_notional=D("10000")),
        rules(),
        Ids("risk"),
    )
    service = engine(risk=risk)
    snapshot = market()
    with pytest.raises(PaperTradingRiskError):
        service.process(decision(snapshot, PipelineOutcome.INTENT_CREATED), snapshot)
    assert service.status.last_error_category is PaperTradingFailureCategory.RISK_EVALUATION


def test_logging_observer_warning_branch(caplog: pytest.LogCaptureFixture) -> None:
    observer = Observer()
    snapshot = market()
    engine(observer=observer).process(decision(snapshot), snapshot)
    failed = replace(
        observer.records[0],
        observation_status=PaperTradingObservationStatus.OBSERVATION_FAILED,
        failure_category=PaperTradingFailureCategory.OBSERVER,
    )
    with caplog.at_level(logging.WARNING, logger="paper-warning"):
        LoggingPaperTradingObserver(logging.getLogger("paper-warning")).on_cycle(failed)
    assert "Paper cycle" in caplog.text


def test_observer_exception_logs_traceback_safely(caplog: pytest.LogCaptureFixture) -> None:
    service = engine(observer=Observer(RuntimeError("observer boom")))
    snapshot = market()
    with caplog.at_level(logging.ERROR, logger="PaperTrading"):
        result = service.process(decision(snapshot), snapshot)
    assert result.observation_status is PaperTradingObservationStatus.OBSERVATION_FAILED
    assert "Paper observer failed" in caplog.text
    assert "RuntimeError: observer boom" in caplog.text
    assert "closed_candles" not in caplog.text
