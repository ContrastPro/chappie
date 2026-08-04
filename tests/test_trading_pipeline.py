import ast
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
from pathlib import Path
from typing import ClassVar

import pytest

from app.market.models import Candle, MarketStreamKey
from app.strategies.ema_crossover import EmaCrossoverConfig, EmaCrossoverStrategy
from app.trading.exceptions import (
    IntentFactoryError,
    InvalidSignalError,
    PipelineInvariantError,
    PipelineRunIdFactoryError,
    PlannerExecutionError,
    StrategyExecutionError,
)
from app.trading.intent import (
    IntentPlanOutcome,
    IntentPlanResult,
    LongOnlyIntentPlanner,
    LongOnlyIntentPlannerConfig,
)
from app.trading.models import (
    MarketSnapshot,
    OrderIntent,
    OrderIntentAction,
    Signal,
    SignalDirection,
)
from app.trading.pipeline import (
    DecisionPipelineResult,
    PipelineOutcome,
    PipelineRunIdFactory,
    TradingDecisionPipeline,
    UUIDPipelineRunIdFactory,
)
from app.trading.snapshot import MarketSnapshotFactory
from app.trading.strategy import SignalIdFactory, Strategy, UUIDSignalIdFactory

KEY = MarketStreamKey("BTCUSDT", "1m")
NOW_MS = 1_700_000_000_000


class SequenceFactory:
    def __init__(self, prefix: str, values: list[str | BaseException] | None = None) -> None:
        self.prefix = prefix
        self.values = list(values or [])
        self.calls = 0

    def _next(self) -> str:
        self.calls += 1
        if self.values:
            value = self.values.pop(0)
            if isinstance(value, BaseException):
                raise value
            return value
        return f"{self.prefix}-{self.calls}"

    def next_id(self) -> str:
        return self._next()

    def new_intent_id(self) -> str:
        return self._next()

    def new_pipeline_run_id(self) -> str:
        return self._next()


def snapshot(
    *,
    key: MarketStreamKey = KEY,
    snapshot_id: str = "snapshot-1",
    as_of_ms: int = NOW_MS,
) -> MarketSnapshot:
    return MarketSnapshotFactory(120_000).create(
        key,
        (),
        as_of_ms,
        snapshot_id,
        correlation_cycle_id="cycle-1",
    )


def signal_for(
    source: MarketSnapshot,
    direction: SignalDirection,
    **overrides: object,
) -> Signal:
    reasons = {
        SignalDirection.BUY: "bullish_crossover",
        SignalDirection.SELL: "bearish_crossover",
        SignalDirection.HOLD: "no_crossover",
    }
    values = {
        "signal_id": "signal-1",
        "snapshot_id": source.snapshot_id,
        "stream_key": source.stream_key,
        "created_at_ms": source.as_of_ms,
        "effective_at_ms": source.as_of_ms,
        "direction": direction,
        "reason_code": reasons[direction],
        "explanation": "test",
        "strategy_name": "test_strategy",
        "strategy_version": "1",
        "strategy_parameters": {},
        "correlation_id": source.correlation_cycle_id,
        **overrides,
    }
    return Signal(**values)  # type: ignore[arg-type]


class FakeStrategy:
    name = "test_strategy"
    version = "1"
    required_history = 0
    configuration: ClassVar[dict[str, object]] = {}

    def __init__(self, result: Signal | BaseException) -> None:
        self.result = result
        self.calls = 0

    def generate_signal(self, source: MarketSnapshot) -> Signal:
        del source
        self.calls += 1
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class FakePlanner:
    name = "fake_planner"
    version = "1"
    configuration: ClassVar[dict[str, object]] = {
        "long_target_exposure": Decimal(1)
    }

    def __init__(self, result: IntentPlanResult | BaseException) -> None:
        self.result = result
        self.calls = 0

    def plan(self, source: Signal) -> IntentPlanResult:
        del source
        self.calls += 1
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def intent_for(source: Signal, **overrides: object) -> OrderIntent:
    action = (
        OrderIntentAction.ENTER_LONG
        if source.direction is SignalDirection.BUY
        else OrderIntentAction.EXIT_LONG
    )
    exposure = Decimal(1) if action is OrderIntentAction.ENTER_LONG else Decimal(0)
    values = {
        "intent_id": "intent-1",
        "source_signal_id": source.signal_id,
        "stream_key": source.stream_key,
        "created_at_ms": source.effective_at_ms,
        "action": action,
        "target_exposure": exposure,
        "reason": "test plan",
        "strategy_name": source.strategy_name,
        "strategy_version": source.strategy_version,
        "correlation_id": source.correlation_id,
        **overrides,
    }
    return OrderIntent(**values)  # type: ignore[arg-type]


def plan_for(source: Signal, **overrides: object) -> IntentPlanResult:
    if source.direction is SignalDirection.HOLD:
        outcome = IntentPlanOutcome.NO_INTENT_FOR_HOLD
        intent = None
    else:
        outcome = IntentPlanOutcome.CREATED
        intent = intent_for(source)
    values = {
        "outcome": outcome,
        "signal_id": source.signal_id,
        "intent": intent,
        "reason_code": "planned",
        "planner_name": "fake_planner",
        "planner_version": "1",
        "correlation_id": source.correlation_id,
        **overrides,
    }
    return IntentPlanResult(**values)  # type: ignore[arg-type]


def pipeline(
    source_signal: Signal,
    plan: IntentPlanResult | None = None,
    *,
    run_ids: SequenceFactory | None = None,
) -> tuple[TradingDecisionPipeline, FakeStrategy, FakePlanner]:
    strategy = FakeStrategy(source_signal)
    planner = FakePlanner(plan or plan_for(source_signal))
    subject = TradingDecisionPipeline(
        strategy,
        planner,
        run_ids or SequenceFactory("run"),
    )
    return subject, strategy, planner


@pytest.mark.parametrize(
    ("direction", "expected"),
    [
        (SignalDirection.HOLD, PipelineOutcome.HOLD),
        (SignalDirection.BUY, PipelineOutcome.INTENT_CREATED),
        (SignalDirection.SELL, PipelineOutcome.INTENT_CREATED),
    ],
)
def test_normal_pipeline_paths(
    direction: SignalDirection,
    expected: PipelineOutcome,
) -> None:
    source = snapshot()
    source_signal = signal_for(source, direction)
    subject, strategy, planner = pipeline(source_signal)

    result = subject.evaluate(source)

    assert result.outcome is expected
    assert result.signal == source_signal
    assert result.order_intent == result.intent_plan.intent
    assert result.evaluated_at_ms == source.as_of_ms
    assert result.correlation_id == "cycle-1"
    assert strategy.calls == planner.calls == 1
    expected_stages = (
        "snapshot_validated",
        "strategy_evaluated",
        "intent_planned",
    )
    if expected is PipelineOutcome.INTENT_CREATED:
        expected_stages += ("intent_created",)
    assert result.completed_stages == expected_stages


def test_planner_declined_is_normal_result() -> None:
    source = snapshot()
    source_signal = signal_for(source, SignalDirection.BUY)
    declined = plan_for(
        source_signal,
        outcome=IntentPlanOutcome.DECLINED,
        intent=None,
        reason_code="planner_policy_declined",
    )
    subject, _, _ = pipeline(source_signal, declined)

    result = subject.evaluate(source)

    assert result.outcome is PipelineOutcome.PLANNER_DECLINED
    assert result.order_intent is None
    assert result.reason_code == "planner_policy_declined"


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"snapshot_id": "wrong"}, "snapshot_id"),
        ({"stream_key": MarketStreamKey("ETHUSDT", "1m")}, "stream_key"),
        ({"strategy_name": "wrong"}, "strategy_name"),
        ({"strategy_version": "wrong"}, "strategy_version"),
        ({"created_at_ms": NOW_MS + 1, "effective_at_ms": NOW_MS + 1}, "timestamps"),
        ({"correlation_id": "wrong"}, "correlation"),
    ],
)
def test_pipeline_rejects_invalid_snapshot_signal_relationship(
    override: dict[str, object],
    message: str,
) -> None:
    source = snapshot()
    bad_signal = signal_for(source, SignalDirection.BUY, **override)
    subject, _, planner = pipeline(bad_signal)

    with pytest.raises(PipelineInvariantError, match=message):
        subject.evaluate(source)
    assert planner.calls == 0


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"signal_id": "wrong"}, "signal_id"),
        ({"planner_name": "wrong"}, "planner_name"),
        ({"planner_version": "wrong"}, "planner_version"),
        ({"correlation_id": "wrong"}, "correlation"),
    ],
)
def test_pipeline_rejects_invalid_signal_plan_relationship(
    override: dict[str, object],
    message: str,
) -> None:
    source = snapshot()
    source_signal = signal_for(source, SignalDirection.BUY)
    corrupted = plan_for(source_signal)
    for field, value in override.items():
        object.__setattr__(corrupted, field, value)
    subject, _, _ = pipeline(source_signal, corrupted)

    with pytest.raises(PipelineInvariantError, match=message):
        subject.evaluate(source)


@pytest.mark.parametrize(
    ("direction", "intent_override", "message"),
    [
        (SignalDirection.BUY, {"source_signal_id": "wrong"}, "source_signal_id"),
        (
            SignalDirection.BUY,
            {"stream_key": MarketStreamKey("ETHUSDT", "1m")},
            "stream_key",
        ),
        (SignalDirection.BUY, {"created_at_ms": NOW_MS + 1}, "timestamp"),
        (SignalDirection.BUY, {"strategy_name": "wrong"}, "strategy identity"),
        (SignalDirection.BUY, {"correlation_id": "wrong"}, "correlation"),
        (
            SignalDirection.BUY,
            {"action": OrderIntentAction.EXIT_LONG, "target_exposure": Decimal(0)},
            "ENTER_LONG",
        ),
        (
            SignalDirection.SELL,
            {"action": OrderIntentAction.ENTER_LONG, "target_exposure": Decimal(1)},
            "EXIT_LONG",
        ),
        (SignalDirection.BUY, {"target_exposure": Decimal("0.5")}, "target exposure"),
    ],
)
def test_pipeline_rejects_invalid_signal_intent_relationship(
    direction: SignalDirection,
    intent_override: dict[str, object],
    message: str,
) -> None:
    source = snapshot()
    source_signal = signal_for(source, direction)
    bad_intent = intent_for(source_signal, **intent_override)
    bad_plan = plan_for(source_signal)
    object.__setattr__(bad_plan, "intent", bad_intent)
    subject, _, _ = pipeline(source_signal, bad_plan)

    with pytest.raises(PipelineInvariantError, match=message):
        subject.evaluate(source)


def test_pipeline_rejects_created_without_intent_and_hold_with_intent() -> None:
    source = snapshot()
    buy = signal_for(source, SignalDirection.BUY)
    missing = plan_for(buy)
    object.__setattr__(missing, "intent", None)
    subject, _, _ = pipeline(buy, missing)
    with pytest.raises(PipelineInvariantError):
        subject.evaluate(source)


def test_pipeline_rejects_buy_with_hold_outcome_and_sell_nonzero_exposure() -> None:
    source = snapshot()
    buy = signal_for(source, SignalDirection.BUY)
    invalid_plan = plan_for(buy)
    object.__setattr__(invalid_plan, "outcome", IntentPlanOutcome.NO_INTENT_FOR_HOLD)
    object.__setattr__(invalid_plan, "intent", None)
    subject, _, _ = pipeline(buy, invalid_plan)
    with pytest.raises(PipelineInvariantError):
        subject.evaluate(source)

    sell = signal_for(source, SignalDirection.SELL)
    bad_intent = intent_for(sell)
    object.__setattr__(bad_intent, "target_exposure", Decimal("0.1"))
    bad_plan = plan_for(sell)
    object.__setattr__(bad_plan, "intent", bad_intent)
    subject, _, _ = pipeline(sell, bad_plan)
    with pytest.raises(PipelineInvariantError, match="zero exposure"):
        subject.evaluate(source)

    hold = signal_for(source, SignalDirection.HOLD)
    invalid = plan_for(hold)
    object.__setattr__(invalid, "outcome", IntentPlanOutcome.CREATED)
    object.__setattr__(invalid, "intent", intent_for(signal_for(source, SignalDirection.BUY)))
    subject, _, _ = pipeline(hold, invalid)
    with pytest.raises(PipelineInvariantError, match="HOLD"):
        subject.evaluate(source)


def test_pipeline_result_and_nested_metadata_are_immutable() -> None:
    source = snapshot()
    source_signal = signal_for(source, SignalDirection.BUY)
    subject, _, _ = pipeline(source_signal)

    result = subject.evaluate(source)

    with pytest.raises(FrozenInstanceError):
        result.outcome = PipelineOutcome.HOLD  # type: ignore[misc]
    with pytest.raises(TypeError):
        result.metadata["new"] = "value"  # type: ignore[index]
    with pytest.raises(TypeError):
        result.intent_plan.metadata["new"] = "value"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        result.signal.direction = SignalDirection.HOLD  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.order_intent.action = OrderIntentAction.EXIT_LONG  # type: ignore[union-attr,misc]


def test_pipeline_result_defensively_freezes_metadata_and_stages() -> None:
    source = snapshot()
    source_signal = signal_for(source, SignalDirection.HOLD)
    plan = plan_for(source_signal)
    metadata = {"nested": ["value"]}
    stages = ("snapshot_validated", "strategy_evaluated", "intent_planned")
    result = DecisionPipelineResult(
        pipeline_run_id="run-1",
        snapshot_id=source.snapshot_id,
        stream_key=source.stream_key,
        evaluated_at_ms=source.as_of_ms,
        strategy_name=source_signal.strategy_name,
        strategy_version=source_signal.strategy_version,
        signal=source_signal,
        intent_plan=plan,
        order_intent=None,
        outcome=PipelineOutcome.HOLD,
        reason_code=source_signal.reason_code,
        correlation_id=source_signal.correlation_id,
        completed_stages=stages,
        metadata=metadata,
    )
    metadata["nested"].append("mutated")

    assert result.metadata["nested"] == ("value",)
    assert isinstance(result.completed_stages, tuple)
    with pytest.raises(PipelineInvariantError):
        replace(result, metadata={"nested": {"mutable": True}})  # type: ignore[dict-item]


def test_pipeline_id_failure_prevents_strategy_call_and_preserves_cause() -> None:
    source = snapshot()
    source_signal = signal_for(source, SignalDirection.BUY)
    run_ids = SequenceFactory("run", [RuntimeError("run id failed")])
    subject, strategy, planner = pipeline(source_signal, run_ids=run_ids)

    with pytest.raises(PipelineRunIdFactoryError) as caught:
        subject.evaluate(source)

    assert isinstance(caught.value.__cause__, RuntimeError)
    assert strategy.calls == planner.calls == 0


@pytest.mark.parametrize("value", ["", "   ", 42])
def test_pipeline_run_factory_rejects_invalid_output(value: object) -> None:
    source = snapshot()
    source_signal = signal_for(source, SignalDirection.HOLD)
    run_ids = SequenceFactory("run", [value])  # type: ignore[list-item]
    subject, strategy, planner = pipeline(source_signal, run_ids=run_ids)

    with pytest.raises(PipelineRunIdFactoryError):
        subject.evaluate(source)

    assert strategy.calls == planner.calls == 0


def test_strategy_exception_is_wrapped_and_prevents_planner() -> None:
    source = snapshot()
    strategy = FakeStrategy(RuntimeError("strategy failed"))
    source_signal = signal_for(source, SignalDirection.HOLD)
    planner = FakePlanner(plan_for(source_signal))
    subject = TradingDecisionPipeline(strategy, planner, SequenceFactory("run"))

    with pytest.raises(StrategyExecutionError) as caught:
        subject.evaluate(source)

    assert isinstance(caught.value.__cause__, RuntimeError)
    assert planner.calls == 0


def test_planner_exception_is_wrapped_without_partial_result() -> None:
    source = snapshot()
    source_signal = signal_for(source, SignalDirection.BUY)
    strategy = FakeStrategy(source_signal)
    planner = FakePlanner(RuntimeError("planner failed"))
    subject = TradingDecisionPipeline(strategy, planner, SequenceFactory("run"))

    with pytest.raises(PlannerExecutionError) as caught:
        subject.evaluate(source)

    assert isinstance(caught.value.__cause__, RuntimeError)
    assert strategy.calls == planner.calls == 1


def test_domain_errors_keep_type_and_pipeline_is_reusable_after_failure() -> None:
    source = snapshot()
    good_signal = signal_for(source, SignalDirection.HOLD)
    strategy = FakeStrategy(InvalidSignalError("domain failure"))
    planner = FakePlanner(plan_for(good_signal))
    subject = TradingDecisionPipeline(strategy, planner, SequenceFactory("run"))

    with pytest.raises(InvalidSignalError):
        subject.evaluate(source)
    strategy.result = good_signal

    assert subject.evaluate(source).outcome is PipelineOutcome.HOLD
    assert strategy.calls == 2
    assert planner.calls == 1


def test_planner_factory_error_keeps_type_and_pipeline_is_reusable() -> None:
    source = snapshot()
    source_signal = signal_for(source, SignalDirection.BUY)
    strategy = FakeStrategy(source_signal)
    ids = SequenceFactory("intent", [RuntimeError("failed"), "intent-2"])
    planner = LongOnlyIntentPlanner(LongOnlyIntentPlannerConfig(), ids)
    subject = TradingDecisionPipeline(strategy, planner, SequenceFactory("run"))

    with pytest.raises(IntentFactoryError) as caught:
        subject.evaluate(source)
    result = subject.evaluate(source)

    assert isinstance(caught.value.__cause__, RuntimeError)
    assert result.order_intent is not None
    assert result.order_intent.intent_id == "intent-2"


def test_production_factories_and_dependencies_satisfy_protocols() -> None:
    source = snapshot()
    strategy = FakeStrategy(signal_for(source, SignalDirection.HOLD))

    assert isinstance(strategy, Strategy)
    assert isinstance(UUIDSignalIdFactory(), SignalIdFactory)
    assert isinstance(UUIDPipelineRunIdFactory(), PipelineRunIdFactory)


def test_decision_modules_have_no_forbidden_infrastructure_dependencies() -> None:
    forbidden = (
        "app.agent",
        "app.database",
        "app.execution",
        "app.main",
        "app.market.cache",
        "app.market.events",
        "app.market.polling",
        "app.risk",
        "httpx",
    )
    for module_path in (
        Path("app/trading/intent.py"),
        Path("app/trading/pipeline.py"),
    ):
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        imports = [
            dependency
            for node in ast.walk(tree)
            for dependency in (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
        ]
        assert not any(
            dependency.startswith(prefix)
            for dependency in imports
            for prefix in forbidden
        )


def test_repeated_calls_are_stateless_and_work_for_different_streams() -> None:
    first_snapshot = snapshot()
    source_signal = signal_for(first_snapshot, SignalDirection.BUY)
    planner_ids = SequenceFactory("intent")
    long_only = LongOnlyIntentPlanner(LongOnlyIntentPlannerConfig(), planner_ids)
    strategy = FakeStrategy(source_signal)
    subject = TradingDecisionPipeline(strategy, long_only, SequenceFactory("run"))

    first = subject.evaluate(first_snapshot)
    second = subject.evaluate(first_snapshot)

    assert first.pipeline_run_id != second.pipeline_run_id
    assert first.order_intent.intent_id != second.order_intent.intent_id  # type: ignore[union-attr]
    assert first.order_intent.action == second.order_intent.action  # type: ignore[union-attr]
    assert first.order_intent.target_exposure == second.order_intent.target_exposure  # type: ignore[union-attr]

    other_snapshot = snapshot(
        key=MarketStreamKey("ETHUSDT", "1m"),
        snapshot_id="snapshot-eth",
    )
    strategy.result = signal_for(other_snapshot, SignalDirection.BUY)
    other = subject.evaluate(other_snapshot)
    assert other.stream_key == other_snapshot.stream_key


def test_equivalent_dependencies_produce_equivalent_semantics() -> None:
    source = snapshot()

    def evaluate() -> DecisionPipelineResult:
        source_signal = signal_for(source, SignalDirection.BUY)
        return TradingDecisionPipeline(
            FakeStrategy(source_signal),
            LongOnlyIntentPlanner(
                LongOnlyIntentPlannerConfig(long_target_exposure=Decimal("0.3")),
                SequenceFactory("intent", ["fixed-intent"]),
            ),
            SequenceFactory("run", ["fixed-run"]),
        ).evaluate(source)

    first = evaluate()
    second = evaluate()

    assert first == second
    assert first.order_intent is first.intent_plan.intent


@pytest.mark.parametrize("field", ["created_at_ms", "effective_at_ms"])
def test_pipeline_rejects_each_corrupted_signal_timestamp(field: str) -> None:
    source = snapshot()
    corrupted = signal_for(source, SignalDirection.BUY)
    object.__setattr__(corrupted, field, source.as_of_ms + 1)
    subject, _, planner = pipeline(corrupted)

    with pytest.raises(PipelineInvariantError, match="timestamps"):
        subject.evaluate(source)
    assert planner.calls == 0


def test_result_rejects_distinct_but_equal_intent_and_corrupted_identity() -> None:
    source = snapshot()
    source_signal = signal_for(source, SignalDirection.BUY)
    plan = plan_for(source_signal)
    assert plan.intent is not None
    equal_intent = replace(plan.intent)
    common = {
        "pipeline_run_id": "run-1",
        "snapshot_id": source.snapshot_id,
        "stream_key": source.stream_key,
        "evaluated_at_ms": source.as_of_ms,
        "strategy_name": source_signal.strategy_name,
        "strategy_version": source_signal.strategy_version,
        "signal": source_signal,
        "intent_plan": plan,
        "order_intent": plan.intent,
        "outcome": PipelineOutcome.INTENT_CREATED,
        "reason_code": plan.reason_code,
        "correlation_id": source_signal.correlation_id,
        "completed_stages": (
            "snapshot_validated",
            "strategy_evaluated",
            "intent_planned",
            "intent_created",
        ),
    }
    with pytest.raises(PipelineInvariantError, match="same object"):
        DecisionPipelineResult(**{**common, "order_intent": equal_intent})
    with pytest.raises(PipelineInvariantError, match="strategy identity"):
        DecisionPipelineResult(**{**common, "strategy_name": "wrong"})
    with pytest.raises(PipelineInvariantError, match="correlation"):
        DecisionPipelineResult(**{**common, "correlation_id": "wrong"})


def market_snapshot(closes: list[int], *, stale: bool = False, include_open: bool = False) -> MarketSnapshot:
    values: list[Candle] = []
    for index, close_value in enumerate(closes + ([999] if include_open else [])):
        open_ms = NOW_MS + index * 60_000
        close = Decimal(close_value)
        values.append(
            Candle(
                open_time_ms=open_ms,
                close_time_ms=open_ms + 59_999,
                open=close,
                high=close + 1,
                low=max(Decimal("0.1"), close - 1),
                close=close,
                volume=Decimal(10),
            )
        )
    as_of_ms = values[-1].close_time_ms + (2 if stale else 1)
    if include_open:
        as_of_ms = values[-1].close_time_ms
    return MarketSnapshotFactory(1 if stale else 120_000).create(
        KEY,
        values,
        as_of_ms,
        "market-snapshot",
        correlation_cycle_id="cycle-1",
    )


@pytest.mark.parametrize(
    ("closes", "stale", "outcome", "direction", "action", "exposure"),
    [
        ([3, 2, 1, 4], False, PipelineOutcome.INTENT_CREATED, SignalDirection.BUY, OrderIntentAction.ENTER_LONG, Decimal("0.4")),
        ([1, 2, 3, 0], False, PipelineOutcome.INTENT_CREATED, SignalDirection.SELL, OrderIntentAction.EXIT_LONG, Decimal(0)),
        ([3, 2, 1, 4, 5], False, PipelineOutcome.HOLD, SignalDirection.HOLD, None, None),
        ([3, 2, 1, 4], True, PipelineOutcome.HOLD, SignalDirection.HOLD, None, None),
    ],
)
def test_ema_strategy_integration(
    closes: list[int],
    stale: bool,
    outcome: PipelineOutcome,
    direction: SignalDirection,
    action: OrderIntentAction | None,
    exposure: Decimal | None,
) -> None:
    source = market_snapshot(closes, stale=stale)
    ema = EmaCrossoverStrategy(
        EmaCrossoverConfig(fast_window=2, slow_window=3),
        SequenceFactory("signal"),
    )
    long_only = LongOnlyIntentPlanner(
        LongOnlyIntentPlannerConfig(long_target_exposure=Decimal("0.4")),
        SequenceFactory("intent"),
    )
    subject = TradingDecisionPipeline(ema, long_only, SequenceFactory("run"))

    result = subject.evaluate(source)

    assert result.outcome is outcome
    assert result.signal.direction is direction
    if action is None:
        assert result.order_intent is None
    else:
        assert result.order_intent is not None
        assert result.order_intent.action is action
        assert result.order_intent.target_exposure == exposure


def test_open_candle_does_not_change_integrated_decision() -> None:
    closed = market_snapshot([3, 2, 1, 4])
    with_open = market_snapshot([3, 2, 1, 4], include_open=True)

    def evaluate(source: MarketSnapshot) -> DecisionPipelineResult:
        return TradingDecisionPipeline(
            EmaCrossoverStrategy(
                EmaCrossoverConfig(fast_window=2, slow_window=3),
                SequenceFactory("signal"),
            ),
            LongOnlyIntentPlanner(
                LongOnlyIntentPlannerConfig(),
                SequenceFactory("intent"),
            ),
            SequenceFactory("run"),
        ).evaluate(source)

    first = evaluate(closed)
    second = evaluate(with_open)

    assert first.signal.direction == second.signal.direction
    assert first.order_intent.action == second.order_intent.action  # type: ignore[union-attr]
