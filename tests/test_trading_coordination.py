from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.coordination import (
    DecisionConsumerStatus,
    TradingCoordinationCacheError,
    TradingCoordinationConfigurationError,
    TradingCoordinationIdFactoryError,
    TradingCoordinationInvariantError,
    TradingCoordinationPipelineError,
    TradingCoordinationSnapshotError,
    TradingCoordinationStateError,
    TradingDecisionCoordinationStage,
    TradingDecisionCoordinator,
    TradingDecisionCoordinatorLifecycle,
)
from app.coordination.factories import UUIDCoordinationIdFactory
from app.exchange_runtime import TestnetExecutionConsumer as ExecutionConsumer
from app.market.cache import MarketStreamKey
from app.market.events import (
    CandleClosed,
    CandleClosedReason,
    MarketEventDispatcher,
    MarketEventSource,
)
from app.market.models import Candle, CandleSnapshot
from app.paper.models import PaperTradingEngineHealth
from app.shadow import ShadowDecisionConsumer
from app.shadow.consumer import validate_event_cache_consistency
from app.shadow.exceptions import ShadowEventCacheMismatchError, ShadowFactoryError
from app.shadow.models import ShadowObservationStatus
from app.strategies.ema_crossover import EmaCrossoverConfig, EmaCrossoverStrategy
from app.trading import LongOnlyIntentPlanner, LongOnlyIntentPlannerConfig
from app.trading.exceptions import TradingDomainError
from app.trading.models import SignalDirection
from app.trading.pipeline import TradingDecisionPipeline
from app.trading.snapshot import MarketSnapshotFactory

STREAM = MarketStreamKey("BTCUSDT", "1m")
OPEN = 1_700_000_000_000


class FixedId:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self.calls = 0

    def _next(self) -> str:
        self.calls += 1
        return f"{self.prefix}-{self.calls}"

    def next_id(self) -> str:
        return self._next()

    def new_intent_id(self) -> str:
        return self._next()

    def new_pipeline_run_id(self) -> str:
        return self._next()

    def new_snapshot_id(self) -> str:
        return self._next()

    def new_coordination_id(self) -> str:
        return self._next()

    def new_shadow_run_id(self) -> str:
        return self._next()


class SequenceClock:
    def __init__(self, values: tuple[float, ...]) -> None:
        self.values = iter(values)

    def __call__(self) -> float:
        return next(self.values)


class CacheSpy:
    def __init__(self, candles: tuple[Candle, ...], error: Exception | None = None) -> None:
        self.candles = candles
        self.error = error
        self.calls = 0

    def get_snapshot(self, key, limit=None):
        assert key == STREAM
        assert limit == 1000
        self.calls += 1
        if self.error:
            raise self.error
        return self.candles


class SnapshotFactorySpy:
    def __init__(self, error: Exception | None = None) -> None:
        self.real = MarketSnapshotFactory(120_000)
        self.error = error
        self.calls = 0

    def create(self, *args):
        self.calls += 1
        if self.error:
            raise self.error
        return self.real.create(*args)


class PipelineSpy:
    def __init__(self, error: Exception | None = None, replacement=None) -> None:
        strategy = EmaCrossoverStrategy(EmaCrossoverConfig(2, 3), FixedId("signal"))
        planner = LongOnlyIntentPlanner(
            LongOnlyIntentPlannerConfig(Decimal("0.5")), FixedId("intent")
        )
        self.real = TradingDecisionPipeline(strategy, planner, FixedId("pipeline"))
        self.calls = 0
        self.error = error
        self.replacement = replacement

    def evaluate(self, snapshot):
        self.calls += 1
        if self.error:
            raise self.error
        if self.replacement is not None:
            return self.replacement
        return self.real.evaluate(snapshot)


class ShadowSpy:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.calls = []

    def consume(self, event, snapshot, decision_result, coordination_id):
        self.calls.append((event, snapshot, decision_result, coordination_id))
        if self.error is not None:
            raise self.error
        intent = decision_result.order_intent
        return SimpleNamespace(
            source_event_id=event.event_id,
            correlation_cycle_id=event.correlation_cycle_id,
            stream_key=event.stream_key,
            snapshot_id=snapshot.snapshot_id,
            pipeline_run_id=decision_result.pipeline_run_id,
            signal_id=decision_result.signal.signal_id,
            intent_id=intent.intent_id if intent else None,
        )


class ObserverSpy:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.records = []

    def on_decision(self, record):
        self.records.append(record)
        if self.error is not None:
            raise self.error


class PaperSpy:
    def __init__(
        self,
        *,
        health: PaperTradingEngineHealth = PaperTradingEngineHealth.READY,
        error: BaseException | None = None,
    ) -> None:
        self._health = health
        self.error = error
        self.calls = []

    @property
    def status(self):
        return SimpleNamespace(health=self._health)

    def process(self, *, decision_result, market_snapshot):
        self.calls.append((market_snapshot, decision_result))
        if self.error is not None:
            raise self.error
        intent = decision_result.order_intent
        return SimpleNamespace(
            outcome=SimpleNamespace(value="NO_INTENT"),
            decision_result=decision_result,
            market_snapshot_id=market_snapshot.snapshot_id,
            decision_pipeline_run_id=decision_result.pipeline_run_id,
            signal_id=decision_result.signal.signal_id,
            intent_id=intent.intent_id if intent else None,
            correlation_id=decision_result.correlation_id,
        )


def candle(offset: int = 0) -> Candle:
    opened = OPEN + offset * 60_000
    return Candle(
        open_time_ms=opened,
        close_time_ms=opened + 59_999,
        open=Decimal(100),
        high=Decimal(110),
        low=Decimal(90),
        close=Decimal(105),
        volume=Decimal(10),
    )


def candles_with_closes(closes: list[int]) -> tuple[Candle, ...]:
    return tuple(
        candle(index).model_copy(update={"close": Decimal(close)})
        for index, close in enumerate(closes)
    )


def event(event_id: str = "event-1", offset: int = 0) -> CandleClosed:
    item = candle(offset)
    return CandleClosed(
        event_id=event_id,
        occurred_at_ms=item.close_time_ms + 1,
        stream_key=STREAM,
        correlation_cycle_id="cycle-1",
        source=MarketEventSource.REPLAY,
        candle=CandleSnapshot.from_candle(item),
        reason=CandleClosedReason.TIME_CONFIRMED,
    )


def coordinator(*, shadow=None, paper=None, testnet=None, candles=None, capacity=10):
    cache = CacheSpy(candles or (candle(),))
    snapshots = SnapshotFactorySpy()
    pipeline = PipelineSpy()
    instance = TradingDecisionCoordinator(
        cache,
        snapshots,
        pipeline,
        FixedId("snapshot"),
        FixedId("coordination"),
        shadow_consumer=shadow,
        paper_engine=paper,
        testnet_consumer=testnet,
        dedup_capacity=capacity,
    )
    return instance, cache, snapshots, pipeline


def test_lifecycle_start_stop_and_process_requires_running() -> None:
    instance, *_ = coordinator(shadow=ShadowSpy())
    dispatcher = MarketEventDispatcher()
    with pytest.raises(TradingCoordinationStateError, match="RUNNING"):
        instance.process_event(event())
    instance.start(dispatcher)
    with pytest.raises(TradingCoordinationStateError, match="already"):
        instance.start(dispatcher)
    instance.stop()
    instance.stop()


def test_unsubscribe_failure_keeps_running_and_token(monkeypatch) -> None:
    instance, *_ = coordinator(shadow=ShadowSpy())
    dispatcher = MarketEventDispatcher()
    instance.start(dispatcher)
    monkeypatch.setattr(dispatcher, "unsubscribe", lambda token: False)
    with pytest.raises(TradingCoordinationStateError, match="not confirmed"):
        instance.stop()
    assert instance.status.lifecycle.value == "RUNNING"


def test_shared_identity_pipeline_once_and_duplicate_same_result() -> None:
    shadow = ShadowSpy()
    paper = PaperSpy()
    instance, cache, snapshots, pipeline = coordinator(shadow=shadow, paper=paper)
    instance.start(MarketEventDispatcher())

    first = instance.process_event(event())
    duplicate = instance.process_event(event())

    assert duplicate is first
    assert cache.calls == snapshots.calls == pipeline.calls == 1
    assert shadow.calls[0][1] is paper.calls[0][0]
    assert shadow.calls[0][2] is paper.calls[0][1]
    assert shadow.calls[0][2].signal is paper.calls[0][1].signal
    assert first.shadow_status is first.paper_status is DecisionConsumerStatus.DELIVERED
    assert instance.status.ignored_duplicates == 1


def test_shadow_and_testnet_receive_same_shared_objects_without_reevaluation() -> None:
    shadow = ShadowSpy()
    calls = []
    testnet = ExecutionConsumer(lambda decision, snapshot: calls.append((decision, snapshot)))
    items = candles_with_closes([3, 2, 1, 4])
    source_event = replace(
        event(offset=len(items) - 1), candle=CandleSnapshot.from_candle(items[-1])
    )
    instance, _, _, pipeline = coordinator(shadow=shadow, testnet=testnet, candles=items)
    instance.start(MarketEventDispatcher())
    result = instance.process_event(source_event)
    assert pipeline.calls == 1
    assert shadow.calls[0][1] is calls[0][1] is result.market_snapshot
    assert shadow.calls[0][2] is calls[0][0] is result.decision_result
    assert testnet.status.executed == 1


def test_testnet_execution_failure_enters_safe_stop_and_never_retries() -> None:
    calls = []

    def fail_once(decision, snapshot) -> None:
        calls.append((decision, snapshot))
        raise RuntimeError("ambiguous execution result")

    testnet = ExecutionConsumer(fail_once)
    items = candles_with_closes([3, 2, 1, 4])
    first_event = replace(
        event("testnet-failure", offset=len(items) - 1),
        candle=CandleSnapshot.from_candle(items[-1]),
    )
    instance, _, _, pipeline = coordinator(testnet=testnet, candles=items)
    dispatcher = MarketEventDispatcher()
    instance.start(dispatcher)

    delivery = dispatcher.publish(first_event)

    assert delivery.delivered_count == 1
    assert len(calls) == 1
    assert instance.status.lifecycle is TradingDecisionCoordinatorLifecycle.SAFE_STOP
    assert instance.status.testnet_safe_stop_reason == "RuntimeError"
    assert instance.status.last_error_message == "Testnet execution failure; restart required"

    second_event = replace(first_event, event_id="testnet-after-safe-stop")
    dispatcher.publish(second_event)

    assert len(calls) == 1
    assert pipeline.calls == 1
    with pytest.raises(TradingCoordinationStateError, match="requires RUNNING"):
        instance.process_event(second_event)
    instance.stop()
    with pytest.raises(TradingCoordinationStateError, match="fresh process"):
        instance.start(dispatcher)


def test_testnet_hold_path_does_not_enter_safe_stop_or_execute() -> None:
    calls = []
    testnet = ExecutionConsumer(lambda decision, snapshot: calls.append((decision, snapshot)))
    items = candles_with_closes([3, 2, 1, 3])
    source_event = replace(
        event("testnet-hold", offset=len(items) - 1),
        candle=CandleSnapshot.from_candle(items[-1]),
    )
    instance, *_ = coordinator(testnet=testnet, candles=items)
    instance.start(MarketEventDispatcher())

    result = instance.process_event(source_event)

    assert result.decision_result.outcome.value == "hold"
    assert calls == []
    assert instance.status.lifecycle is TradingDecisionCoordinatorLifecycle.RUNNING


def test_paper_delivery_remains_available_without_testnet_safe_stop() -> None:
    paper = PaperSpy()
    instance, *_ = coordinator(paper=paper)
    instance.start(MarketEventDispatcher())

    result = instance.process_event(event("paper-only"))

    assert result.paper_status is DecisionConsumerStatus.DELIVERED
    assert len(paper.calls) == 1
    assert instance.status.lifecycle is TradingDecisionCoordinatorLifecycle.RUNNING


@pytest.mark.parametrize(
    ("closes", "expected_direction"),
    (([3, 2, 1, 4], "BUY"), ([1, 2, 3, 0], "SELL"), ([3, 2, 1, 3], "HOLD")),
)
def test_shared_identity_is_exact_for_buy_sell_and_hold(closes, expected_direction) -> None:
    items = candles_with_closes(closes)
    source_event = event(offset=len(items) - 1)
    source_event = replace(source_event, candle=CandleSnapshot.from_candle(items[-1]))
    shadow = ShadowSpy()
    paper = PaperSpy()
    instance, cache, snapshots, pipeline = coordinator(
        shadow=shadow,
        paper=paper,
        candles=items,
    )
    instance.start(MarketEventDispatcher())

    result = instance.process_event(source_event)

    shadow_event, shadow_snapshot, shadow_decision, _ = shadow.calls[0]
    paper_snapshot, paper_decision = paper.calls[0]
    assert shadow_event is source_event
    assert shadow_snapshot is paper_snapshot is result.market_snapshot
    assert shadow_decision is paper_decision is result.decision_result
    assert shadow_decision.signal is paper_decision.signal
    assert shadow_decision.order_intent is paper_decision.order_intent
    assert result.signal_direction.name == expected_direction
    assert cache.calls == snapshots.calls == pipeline.calls == 1


def test_shadow_exception_does_not_block_paper_and_result_is_terminal() -> None:
    shadow = ShadowSpy(ValueError("shadow failed"))
    paper = PaperSpy()
    instance, _, _, pipeline = coordinator(shadow=shadow, paper=paper)
    instance.start(MarketEventDispatcher())
    result = instance.process_event(event())

    assert result.shadow_status is DecisionConsumerStatus.FAILED
    assert result.paper_status is DecisionConsumerStatus.DELIVERED
    assert instance.status.successful_decisions == 1
    assert instance.status.failed_decisions == 0
    assert instance.process_event(event()) is result
    assert pipeline.calls == len(shadow.calls) == len(paper.calls) == 1


def test_paper_exception_preserves_shadow_and_no_duplicate_retry() -> None:
    shadow = ShadowSpy()
    paper = PaperSpy(error=ValueError("paper failed"))
    instance, *_ = coordinator(shadow=shadow, paper=paper)
    instance.start(MarketEventDispatcher())
    result = instance.process_event(event())

    assert result.shadow_status is DecisionConsumerStatus.DELIVERED
    assert result.paper_status is DecisionConsumerStatus.FAILED
    assert instance.process_event(event()) is result
    assert len(shadow.calls) == len(paper.calls) == 1


def test_degraded_paper_is_unavailable_while_shadow_continues() -> None:
    shadow = ShadowSpy()
    paper = PaperSpy(health=PaperTradingEngineHealth.DEGRADED)
    instance, *_ = coordinator(shadow=shadow, paper=paper)
    instance.start(MarketEventDispatcher())
    result = instance.process_event(event())

    assert result.shadow_status is DecisionConsumerStatus.DELIVERED
    assert result.paper_status is DecisionConsumerStatus.UNAVAILABLE
    assert paper.calls == []
    assert instance.status.paper_unavailable == 1
    assert instance.status.lifecycle.value == "RUNNING"


@pytest.mark.parametrize("failure", (KeyboardInterrupt(), SystemExit(), GeneratorExit()))
def test_shadow_base_exception_propagates_and_is_not_cached(failure) -> None:
    shadow = ShadowSpy(failure)
    paper = PaperSpy()
    instance, *_ = coordinator(shadow=shadow, paper=paper)
    instance.start(MarketEventDispatcher())
    with pytest.raises(type(failure)):
        instance.process_event(event())
    assert paper.calls == []
    assert instance.status.successful_decisions == 1
    assert instance.status.lifecycle.value == "RUNNING"
    with pytest.raises(type(failure)):
        instance.process_event(event())
    assert len(shadow.calls) == 2


@pytest.mark.parametrize("failure", (KeyboardInterrupt(), SystemExit(), GeneratorExit()))
def test_paper_base_exception_propagates_without_terminal_result(failure) -> None:
    shadow = ShadowSpy()
    paper = PaperSpy(error=failure)
    instance, *_ = coordinator(shadow=shadow, paper=paper)
    instance.start(MarketEventDispatcher())
    with pytest.raises(type(failure)):
        instance.process_event(event())
    assert len(shadow.calls) == len(paper.calls) == 1
    assert instance.status.lifecycle.value == "RUNNING"
    with pytest.raises(type(failure)):
        instance.process_event(event())
    assert len(shadow.calls) == len(paper.calls) == 2


def test_fifo_eviction_is_deterministic_and_duplicate_does_not_reorder() -> None:
    shadow = ShadowSpy()
    instance, *_ = coordinator(shadow=shadow, capacity=2)
    instance.start(MarketEventDispatcher())
    first = instance.process_event(event("one"))
    second = instance.process_event(event("two"))
    assert instance.process_event(event("one")) is first
    instance.process_event(event("three"))
    assert instance.process_event(event("two")) is second
    replayed_first = instance.process_event(event("one"))
    assert replayed_first is not first


def test_real_shadow_consumer_observes_precomputed_shared_decision() -> None:
    producer, *_ = coordinator(shadow=ShadowSpy())
    producer.start(MarketEventDispatcher())
    shared = producer.process_event(event())
    observer = ObserverSpy()
    consumer = ShadowDecisionConsumer(FixedId("shadow"), observer)

    record = consumer.consume(
        event(),
        shared.market_snapshot,
        shared.decision_result,
        shared.coordination_id,
    )

    assert len(observer.records) == 1
    assert observer.records[0].shadow_run_id == record.shadow_run_id
    assert observer.records[0].pipeline_run_id == record.pipeline_run_id
    assert record.pipeline_run_id == shared.decision_pipeline_run_id
    assert record.observation_status is ShadowObservationStatus.OBSERVED
    assert consumer.status.successful_records == 1


def test_shadow_consumer_isolates_observer_exception_but_not_base_exception() -> None:
    producer, *_ = coordinator(shadow=ShadowSpy())
    producer.start(MarketEventDispatcher())
    shared = producer.process_event(event())
    failing = ShadowDecisionConsumer(FixedId("shadow"), ObserverSpy(ValueError("observer")))
    record = failing.consume(
        event(), shared.market_snapshot, shared.decision_result, shared.coordination_id
    )
    assert record.observation_status is ShadowObservationStatus.OBSERVATION_FAILED
    assert failing.status.observation_failures == 1

    interrupted = ShadowDecisionConsumer(FixedId("shadow"), ObserverSpy(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        interrupted.consume(
            event(), shared.market_snapshot, shared.decision_result, shared.coordination_id
        )


@pytest.mark.parametrize(
    ("keyword", "value"),
    (("history_limit", 0), ("dedup_capacity", True)),
)
def test_coordinator_rejects_invalid_limits(keyword, value) -> None:
    cache = CacheSpy((candle(),))
    kwargs = {keyword: value}
    with pytest.raises(TradingCoordinationConfigurationError):
        TradingDecisionCoordinator(
            cache,
            SnapshotFactorySpy(),
            PipelineSpy(),
            FixedId("snapshot"),
            FixedId("coordination"),
            shadow_consumer=ShadowSpy(),
            **kwargs,
        )


def test_coordinator_rejects_missing_or_invalid_dependencies() -> None:
    valid = (
        CacheSpy((candle(),)),
        SnapshotFactorySpy(),
        PipelineSpy(),
        FixedId("snapshot"),
        FixedId("coordination"),
    )
    for index in range(5):
        args = list(valid)
        args[index] = object()
        with pytest.raises(TradingCoordinationConfigurationError):
            TradingDecisionCoordinator(*args, shadow_consumer=ShadowSpy())
    with pytest.raises(TradingCoordinationConfigurationError, match="consumer"):
        TradingDecisionCoordinator(*valid)
    with pytest.raises(TradingCoordinationConfigurationError, match="shadow"):
        TradingDecisionCoordinator(*valid, shadow_consumer=object())
    with pytest.raises(TradingCoordinationConfigurationError, match="paper"):
        TradingDecisionCoordinator(*valid, paper_engine=object())
    with pytest.raises(TradingCoordinationConfigurationError, match="monotonic"):
        TradingDecisionCoordinator(*valid, shadow_consumer=ShadowSpy(), monotonic_clock=None)


def test_start_and_stop_failures_preserve_lifecycle_and_cause(monkeypatch) -> None:
    instance, *_ = coordinator(shadow=ShadowSpy())
    with pytest.raises(TradingCoordinationConfigurationError, match="dispatcher"):
        instance.start(object())
    dispatcher = MarketEventDispatcher()
    monkeypatch.setattr(
        dispatcher, "subscribe", lambda *args: (_ for _ in ()).throw(RuntimeError())
    )
    with pytest.raises(TradingCoordinationStateError) as captured:
        instance.start(dispatcher)
    assert isinstance(captured.value.__cause__, RuntimeError)
    assert instance.status.lifecycle.value == "STOPPED"

    monkeypatch.undo()
    instance.start(dispatcher)
    original = dispatcher.unsubscribe
    monkeypatch.setattr(
        dispatcher,
        "unsubscribe",
        lambda token: (_ for _ in ()).throw(RuntimeError("unsubscribe")),
    )
    with pytest.raises(TradingCoordinationStateError) as captured:
        instance.stop()
    assert isinstance(captured.value.__cause__, RuntimeError)
    assert instance.status.lifecycle.value == "RUNNING"
    monkeypatch.setattr(dispatcher, "unsubscribe", original)
    instance.stop()


def test_predecision_failures_are_typed_not_cached_and_increment_failed() -> None:
    cases = (
        (
            CacheSpy((candle(),), RuntimeError("cache")),
            SnapshotFactorySpy(),
            PipelineSpy(),
            TradingCoordinationCacheError,
        ),
        (CacheSpy(()), SnapshotFactorySpy(), PipelineSpy(), TradingCoordinationCacheError),
        (
            CacheSpy((candle(),)),
            SnapshotFactorySpy(RuntimeError("snapshot")),
            PipelineSpy(),
            TradingCoordinationSnapshotError,
        ),
        (
            CacheSpy((candle(),)),
            SnapshotFactorySpy(),
            PipelineSpy(RuntimeError("pipeline")),
            TradingCoordinationPipelineError,
        ),
        (
            CacheSpy((candle(),)),
            SnapshotFactorySpy(),
            PipelineSpy(replacement=object()),
            TradingCoordinationInvariantError,
        ),
    )
    for cache, snapshots, pipeline, expected in cases:
        instance = TradingDecisionCoordinator(
            cache,
            snapshots,
            pipeline,
            FixedId("snapshot"),
            FixedId("coordination"),
            shadow_consumer=ShadowSpy(),
        )
        instance.start(MarketEventDispatcher())
        with pytest.raises(expected):
            instance.process_event(event())
        with pytest.raises(expected):
            instance.process_event(event())
        assert instance.status.failed_decisions == 2


@pytest.mark.parametrize("method", ("new_snapshot_id", "new_coordination_id"))
@pytest.mark.parametrize("mode", ("empty", "raise"))
def test_id_factory_failures_are_typed_and_preserve_cause(method, mode) -> None:
    class BrokenId(FixedId):
        def _next(self):
            if mode == "raise":
                raise RuntimeError("factory")
            return " "

    snapshot_ids = BrokenId("snapshot") if method == "new_snapshot_id" else FixedId("snapshot")
    coordination_ids = (
        BrokenId("coordination") if method == "new_coordination_id" else FixedId("coordination")
    )
    instance = TradingDecisionCoordinator(
        CacheSpy((candle(),)),
        SnapshotFactorySpy(),
        PipelineSpy(),
        snapshot_ids,
        coordination_ids,
        shadow_consumer=ShadowSpy(),
    )
    instance.start(MarketEventDispatcher())
    with pytest.raises(TradingCoordinationIdFactoryError) as captured:
        instance.process_event(event())
    assert (captured.value.__cause__ is not None) is (mode == "raise")


def test_runtime_handler_isolates_expected_failure_and_dispatcher_continues() -> None:
    instance = TradingDecisionCoordinator(
        CacheSpy((), RuntimeError("cache")),
        SnapshotFactorySpy(),
        PipelineSpy(),
        FixedId("snapshot"),
        FixedId("coordination"),
        shadow_consumer=ShadowSpy(),
    )
    dispatcher = MarketEventDispatcher()
    instance.start(dispatcher)
    delivery = dispatcher.publish(event())
    assert delivery.delivered_count == 1
    assert delivery.failure_count == 0
    assert instance.status.failed_decisions == 1


def test_runtime_handler_success_and_domain_pipeline_error_paths() -> None:
    successful, *_ = coordinator(shadow=ShadowSpy())
    dispatcher = MarketEventDispatcher()
    successful.start(dispatcher)
    delivery = dispatcher.publish(event())
    assert delivery.delivered_count == 1
    assert successful.status.successful_decisions == 1

    failed = TradingDecisionCoordinator(
        CacheSpy((candle(),)),
        SnapshotFactorySpy(),
        PipelineSpy(TradingDomainError("domain failure")),
        FixedId("snapshot"),
        FixedId("coordination"),
        shadow_consumer=ShadowSpy(),
    )
    failed.start(MarketEventDispatcher())
    with pytest.raises(TradingCoordinationPipelineError, match="domain failure"):
        failed.process_event(event())


def test_both_consumer_exceptions_are_terminal_delivery_degradation() -> None:
    shadow = ShadowSpy(ValueError("shadow"))
    paper = PaperSpy(error=ValueError("paper"))
    instance, *_ = coordinator(shadow=shadow, paper=paper)
    instance.start(MarketEventDispatcher())
    result = instance.process_event(event())
    assert result.shadow_status is result.paper_status is DecisionConsumerStatus.FAILED
    assert instance.status.successful_decisions == 1
    assert instance.status.shadow_failures == instance.status.paper_failures == 1
    assert instance.process_event(event()) is result


def test_result_model_rejects_identity_and_consumer_corruption() -> None:
    instance, *_ = coordinator(shadow=ShadowSpy(), paper=PaperSpy())
    instance.start(MarketEventDispatcher())
    result = instance.process_event(event())
    corruptions = (
        {"coordination_id": " "},
        {"stream_key": object()},
        {"market_snapshot_id": "other"},
        {"market_snapshot": replace(result.market_snapshot, source_event_id="other")},
        {
            "stream_key": MarketStreamKey("ETHUSDT", "1m"),
            "market_snapshot": replace(
                result.market_snapshot,
                stream_key=MarketStreamKey("ETHUSDT", "1m"),
            ),
        },
        {"decision_pipeline_run_id": "other"},
        {"signal_id": "other"},
        {"signal_direction": SignalDirection.SELL},
        {"signal_reason_code": "other"},
        {"intent_id": "unexpected"},
        {"shadow_enabled": False},
        {"shadow_record": None},
        {"paper_enabled": False},
        {"paper_cycle_result": None},
        {
            "shadow_status": DecisionConsumerStatus.FAILED,
            "shadow_record": None,
        },
        {"shadow_status": DecisionConsumerStatus.UNAVAILABLE, "shadow_record": None},
        {"shadow_status": DecisionConsumerStatus.NOT_ATTEMPTED, "shadow_record": None},
        {"paper_status": DecisionConsumerStatus.UNAVAILABLE},
        {
            "shadow_record": SimpleNamespace(
                **{**vars(result.shadow_record), "source_event_id": "other"}
            )
        },
        {
            "paper_cycle_result": SimpleNamespace(
                **{
                    **vars(result.paper_cycle_result),
                    "market_snapshot_id": "other",
                }
            )
        },
        {
            "completed_stages": tuple(
                TradingDecisionCoordinationStage.SHADOW_FAILED
                if stage is TradingDecisionCoordinationStage.SHADOW_DELIVERED
                else stage
                for stage in result.completed_stages
            )
        },
        {"completed_stages": result.completed_stages[1:]},
        {"completed_stages": result.completed_stages[:-1]},
        {
            "completed_stages": (
                result.completed_stages[1],
                result.completed_stages[0],
                *result.completed_stages[2:],
            )
        },
        {"completed_stages": result.completed_stages + (result.completed_stages[-1],)},
        {"total_duration_ms": -1},
    )
    for changes in corruptions:
        with pytest.raises((TypeError, ValueError)):
            replace(result, **changes)
    with pytest.raises(TypeError):
        result.metadata["new"] = "value"


@pytest.mark.parametrize(
    ("shadow", "paper", "expected"),
    (
        (
            ShadowSpy(),
            None,
            (
                "EVENT_RECEIVED",
                "EVENT_VALIDATED",
                "CACHE_READ",
                "SNAPSHOT_CREATED",
                "DECISION_EVALUATED",
                "SHADOW_DELIVERED",
                "COMPLETED",
            ),
        ),
        (
            None,
            PaperSpy(),
            (
                "EVENT_RECEIVED",
                "EVENT_VALIDATED",
                "CACHE_READ",
                "SNAPSHOT_CREATED",
                "DECISION_EVALUATED",
                "PAPER_DELIVERED",
                "COMPLETED",
            ),
        ),
        (
            ShadowSpy(ValueError("shadow")),
            PaperSpy(),
            (
                "EVENT_RECEIVED",
                "EVENT_VALIDATED",
                "CACHE_READ",
                "SNAPSHOT_CREATED",
                "DECISION_EVALUATED",
                "SHADOW_FAILED",
                "PAPER_DELIVERED",
                "COMPLETED",
            ),
        ),
        (
            ShadowSpy(),
            PaperSpy(error=ValueError("paper")),
            (
                "EVENT_RECEIVED",
                "EVENT_VALIDATED",
                "CACHE_READ",
                "SNAPSHOT_CREATED",
                "DECISION_EVALUATED",
                "SHADOW_DELIVERED",
                "PAPER_FAILED",
                "COMPLETED",
            ),
        ),
        (
            ShadowSpy(),
            PaperSpy(health=PaperTradingEngineHealth.DEGRADED),
            (
                "EVENT_RECEIVED",
                "EVENT_VALIDATED",
                "CACHE_READ",
                "SNAPSHOT_CREATED",
                "DECISION_EVALUATED",
                "SHADOW_DELIVERED",
                "PAPER_UNAVAILABLE",
                "COMPLETED",
            ),
        ),
    ),
)
def test_completed_stage_sequences_are_exact(shadow, paper, expected) -> None:
    instance, *_ = coordinator(shadow=shadow, paper=paper)
    instance.start(MarketEventDispatcher())
    result = instance.process_event(event())
    assert tuple(stage.value for stage in result.completed_stages) == expected


def test_durations_use_injected_monotonic_clock_and_clamp_anomalies() -> None:
    instance = TradingDecisionCoordinator(
        CacheSpy((candle(),)),
        SnapshotFactorySpy(),
        PipelineSpy(),
        FixedId("snapshot"),
        FixedId("coordination"),
        shadow_consumer=ShadowSpy(),
        paper_engine=PaperSpy(),
        monotonic_clock=SequenceClock(tuple(float(value) for value in range(10))),
    )
    instance.start(MarketEventDispatcher())
    result = instance.process_event(event())
    assert (
        result.snapshot_duration_ms,
        result.decision_duration_ms,
        result.shadow_duration_ms,
        result.paper_duration_ms,
        result.total_duration_ms,
    ) == (1000, 1000, 1000, 1000, 9000)

    anomalous = TradingDecisionCoordinator(
        CacheSpy((candle(),)),
        SnapshotFactorySpy(),
        PipelineSpy(),
        FixedId("snapshot"),
        FixedId("coordination"),
        shadow_consumer=ShadowSpy(),
        monotonic_clock=SequenceClock(tuple(float(value) for value in range(10, 2, -1))),
    )
    anomalous.start(MarketEventDispatcher())
    result = anomalous.process_event(event("clock-anomaly"))
    assert result.snapshot_duration_ms == 0
    assert result.decision_duration_ms == 0
    assert result.shadow_duration_ms == 0
    assert result.total_duration_ms == 0


def test_event_cache_consistency_rejects_each_mismatch() -> None:
    valid_event = event()
    with pytest.raises(ShadowEventCacheMismatchError, match="boundary"):
        validate_event_cache_consistency(
            replace(valid_event, occurred_at_ms=valid_event.candle.close_time_ms),
            (candle(),),
        )
    with pytest.raises(ShadowEventCacheMismatchError, match="empty"):
        validate_event_cache_consistency(valid_event, ())
    with pytest.raises(ShadowEventCacheMismatchError, match="absent"):
        validate_event_cache_consistency(valid_event, (candle(1),))
    changed = candle().model_copy(update={"close": Decimal(106)})
    with pytest.raises(ShadowEventCacheMismatchError, match="does not match"):
        validate_event_cache_consistency(valid_event, (changed,))


def test_shadow_consumer_validates_inputs_and_id_factory() -> None:
    producer, *_ = coordinator(shadow=ShadowSpy())
    producer.start(MarketEventDispatcher())
    shared = producer.process_event(event())
    consumer = ShadowDecisionConsumer(FixedId("shadow"), ObserverSpy())
    for args in (
        (object(), shared.market_snapshot, shared.decision_result, shared.coordination_id),
        (event(), object(), shared.decision_result, shared.coordination_id),
        (event(), shared.market_snapshot, object(), shared.coordination_id),
        (event(), shared.market_snapshot, shared.decision_result, " "),
    ):
        with pytest.raises((TypeError, ValueError)):
            consumer.consume(*args)

    class InvalidShadowId(FixedId):
        def new_shadow_run_id(self):
            return ""

    invalid = ShadowDecisionConsumer(InvalidShadowId("shadow"), ObserverSpy())
    with pytest.raises(ShadowFactoryError):
        invalid.consume(
            event(), shared.market_snapshot, shared.decision_result, shared.coordination_id
        )

    class RaisingShadowId(FixedId):
        def new_shadow_run_id(self):
            raise RuntimeError("factory")

    raising = ShadowDecisionConsumer(RaisingShadowId("shadow"), ObserverSpy())
    with pytest.raises(ShadowFactoryError) as captured:
        raising.consume(
            event(), shared.market_snapshot, shared.decision_result, shared.coordination_id
        )
    assert isinstance(captured.value.__cause__, RuntimeError)


def test_shadow_consumer_constructor_and_uuid_factory_contracts() -> None:
    with pytest.raises(TypeError, match="run_id"):
        ShadowDecisionConsumer(object(), ObserverSpy())
    with pytest.raises(TypeError, match="observer"):
        ShadowDecisionConsumer(FixedId("shadow"), object())
    with pytest.raises(TypeError, match="monotonic"):
        ShadowDecisionConsumer(FixedId("shadow"), ObserverSpy(), None)
    assert UUIDCoordinationIdFactory().new_coordination_id().strip()
