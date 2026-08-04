from dataclasses import FrozenInstanceError
from decimal import Decimal
from uuid import UUID

import pytest

from app.market.cache import InMemoryMarketCache, MarketStreamKey
from app.market.events import (
    CandleClosed,
    CandleClosedReason,
    CandleInserted,
    CandleInsertedReason,
    MarketEventDispatcher,
    MarketEventSource,
)
from app.market.models import Candle, CandleSnapshot
from app.shadow import (
    MarketCacheReader,
    ShadowDecisionStatus,
    ShadowLifecycle,
    ShadowObservationStatus,
    ShadowTradingDecisionEngine,
    UUIDShadowRunIdFactory,
    UUIDSnapshotIdFactory,
)
from app.shadow.exceptions import ShadowEngineStateError
from app.strategies.ema_crossover import EmaCrossoverConfig, EmaCrossoverStrategy
from app.trading import (
    LongOnlyIntentPlanner,
    LongOnlyIntentPlannerConfig,
    MarketSnapshotFactory,
    PipelineOutcome,
    TradingDecisionPipeline,
)
from app.trading.exceptions import InvalidMarketSnapshotError, PipelineInvariantError

KEY = MarketStreamKey("BTCUSDT", "1m")


class Ids:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self.count = 0

    def _next(self) -> str:
        self.count += 1
        return f"{self.prefix}-{self.count}"

    next_id = _next
    new_intent_id = _next
    new_pipeline_run_id = _next
    new_snapshot_id = _next
    new_shadow_run_id = _next


class Observer:
    def __init__(self) -> None:
        self.records = []
        self.error: BaseException | None = None

    def on_decision(self, record):
        self.records.append(record)
        if self.error is not None:
            raise self.error


def candle(index: int, close: str = "100") -> Candle:
    opened = index * 60_000
    return Candle(
        open_time_ms=opened,
        close_time_ms=opened + 59_999,
        open=Decimal(100),
        high=Decimal(110),
        low=Decimal(90),
        close=Decimal(close),
        volume=Decimal(10),
    )


def event(item: Candle, event_id: str = "event-1") -> CandleClosed:
    return CandleClosed(
        event_id=event_id,
        occurred_at_ms=item.close_time_ms + 1,
        stream_key=KEY,
        correlation_cycle_id="cycle-1",
        source=MarketEventSource.REST_POLLING,
        candle=CandleSnapshot.from_candle(item),
        reason=CandleClosedReason.TIME_CONFIRMED,
    )


def build(*, capacity: int = 10):
    cache = InMemoryMarketCache(100)
    item = candle(1)
    cache.upsert_candle(KEY, item, item.close_time_ms + 1)
    ids = Ids("id")
    pipeline = TradingDecisionPipeline(
        EmaCrossoverStrategy(EmaCrossoverConfig(), ids),
        LongOnlyIntentPlanner(LongOnlyIntentPlannerConfig(), ids),
        ids,
    )
    observer = Observer()
    engine = ShadowTradingDecisionEngine(
        cache,
        MarketSnapshotFactory(120_000),
        pipeline,
        ids,
        ids,
        observer,
        history_limit=100,
        dedup_capacity=capacity,
        monotonic_clock=lambda: 1.0,
    )
    return engine, cache, observer, item, ids


def test_protocol_and_uuid_factories() -> None:
    cache = InMemoryMarketCache()
    assert isinstance(cache, MarketCacheReader)
    assert UUID(UUIDSnapshotIdFactory().new_snapshot_id())
    assert UUID(UUIDShadowRunIdFactory().new_shadow_run_id())


def test_lifecycle_success_hold_and_immutable_status() -> None:
    engine, _, observer, item, _ = build()
    dispatcher = MarketEventDispatcher()
    assert engine.status.lifecycle is ShadowLifecycle.STOPPED
    engine.start(dispatcher)
    result = dispatcher.publish(event(item))
    assert not result.degraded
    assert len(observer.records) == 1
    record = observer.records[0]
    assert record.decision_status is ShadowDecisionStatus.SUCCEEDED
    assert record.observation_status is ShadowObservationStatus.OBSERVED
    assert record.pipeline_outcome is PipelineOutcome.HOLD
    assert engine.status.hold_decisions == 1
    with pytest.raises(FrozenInstanceError):
        engine.status.handled_events = 99
    engine.stop()
    assert engine.status.lifecycle is ShadowLifecycle.STOPPED
    engine.stop()


def test_duplicate_is_ignored_before_dependencies_and_survives_restart() -> None:
    engine, _, observer, item, ids = build()
    dispatcher = MarketEventDispatcher()
    engine.start(dispatcher)
    dispatcher.publish(event(item))
    calls = ids.count
    dispatcher.publish(event(item))
    assert ids.count == calls
    assert len(observer.records) == 1
    assert engine.status.ignored_duplicates == 1
    engine.stop()
    engine.start(dispatcher)
    dispatcher.publish(event(item))
    assert engine.status.ignored_duplicates == 2


def test_dedup_fifo_eviction_allows_old_event_again() -> None:
    engine, _, observer, item, _ = build(capacity=2)
    dispatcher = MarketEventDispatcher()
    engine.start(dispatcher)
    for identity in ("one", "two", "three", "one"):
        dispatcher.publish(event(item, identity))
    assert len(observer.records) == 4


def test_observer_exception_is_isolated_and_record_retained() -> None:
    engine, _, observer, item, _ = build()
    dispatcher = MarketEventDispatcher()
    engine.start(dispatcher)
    observer.error = RuntimeError("sink unavailable")
    result = dispatcher.publish(event(item))
    assert not result.degraded
    assert engine.last_record is not None
    assert engine.last_record.decision_status is ShadowDecisionStatus.SUCCEEDED
    assert engine.last_record.observation_status is ShadowObservationStatus.OBSERVATION_FAILED
    assert engine.status.observation_failures == 1
    assert engine.status.successful_decisions == 1
    dispatcher.publish(event(item))
    assert len(observer.records) == 1
    assert engine.status.ignored_duplicates == 1


@pytest.mark.parametrize("error", [KeyboardInterrupt(), SystemExit(2)])
def test_base_exception_is_not_swallowed_but_event_is_deduplicated(error) -> None:
    engine, _, observer, item, _ = build()
    dispatcher = MarketEventDispatcher()
    engine.start(dispatcher)
    observer.error = error
    with pytest.raises(type(error)):
        dispatcher.publish(event(item))
    observer.error = None
    dispatcher.publish(event(item))
    assert not observer.records[1:]
    assert engine.status.ignored_duplicates == 1


def test_cache_mismatch_isolated_and_next_event_succeeds() -> None:
    engine, cache, observer, item, _ = build()
    dispatcher = MarketEventDispatcher()
    engine.start(dispatcher)
    missing = candle(2)
    assert not dispatcher.publish(event(missing, "missing")).degraded
    assert engine.status.failed_decisions == 1
    assert observer.records[-1].decision_status is ShadowDecisionStatus.FAILED
    dispatcher.publish(event(missing, "missing"))
    assert engine.status.failed_decisions == 1
    assert engine.status.ignored_duplicates == 1
    assert not dispatcher.publish(event(item, "valid")).degraded
    assert engine.status.successful_decisions == 1
    assert len(cache.get_snapshot(KEY)) == 1


def test_start_stop_state_guards() -> None:
    engine, _, _, item, _ = build()
    with pytest.raises(ShadowEngineStateError):
        engine.handle_candle_closed(event(item))
    dispatcher = MarketEventDispatcher()
    engine.start(dispatcher)
    with pytest.raises(ShadowEngineStateError):
        engine.start(dispatcher)
    engine.stop()
    with pytest.raises(ShadowEngineStateError):
        engine.handle_candle_closed(event(item))


def test_subscription_ignores_non_closed_market_events() -> None:
    engine, _, observer, item, ids = build()
    dispatcher = MarketEventDispatcher()
    engine.start(dispatcher)
    inserted = CandleInserted(
        event_id="inserted",
        occurred_at_ms=item.close_time_ms + 1,
        stream_key=KEY,
        correlation_cycle_id="cycle",
        source=MarketEventSource.REST_POLLING,
        candle=CandleSnapshot.from_candle(item),
        reason=CandleInsertedReason.NEW_CANDLE,
    )
    before = ids.count
    result = dispatcher.publish(inserted)
    assert not result.degraded
    assert result.delivered_count == 0
    assert ids.count == before
    assert observer.records == []
    assert engine.status.handled_events == 0


@pytest.mark.parametrize(
    ("dependency_name", "method_name", "failure"),
    [
        ("_cache", "get_snapshot", RuntimeError("cache unavailable")),
        ("_snapshot_factory", "create", InvalidMarketSnapshotError("bad snapshot")),
        ("_pipeline", "evaluate", PipelineInvariantError("bad pipeline")),
    ],
)
def test_dependency_failure_isolated_duplicate_ignored_and_next_event_succeeds(
    monkeypatch, dependency_name: str, method_name: str, failure: Exception
) -> None:
    engine, _, observer, item, _ = build()
    dispatcher = MarketEventDispatcher()
    later_handler_events = []
    engine.start(dispatcher)
    dispatcher.subscribe(CandleClosed, later_handler_events.append)
    dependency = getattr(engine, dependency_name)
    original = getattr(dependency, method_name)
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise failure
        return original(*args, **kwargs)

    monkeypatch.setattr(dependency, method_name, fail_once)
    first = event(item, "failed-event")
    assert not dispatcher.publish(first).degraded
    assert later_handler_events == [first]
    assert engine.status.failed_decisions == 1
    dispatcher.publish(first)
    assert calls == 1
    assert engine.status.ignored_duplicates == 1
    second = event(item, "next-event")
    assert not dispatcher.publish(second).degraded
    assert calls == 2
    assert engine.status.successful_decisions == 1
    assert observer.records[-1].decision_status is ShadowDecisionStatus.SUCCEEDED


def test_event_snapshot_allows_next_open_candle_and_preserves_audit_chain() -> None:
    cache = InMemoryMarketCache(10)
    closes = (Decimal(3), Decimal(2), Decimal(1), Decimal(4), Decimal(999))
    items = [
        Candle(
            open_time_ms=index * 60_000,
            close_time_ms=index * 60_000 + 59_999,
            open=close,
            high=close + 1,
            low=max(Decimal("0.1"), close - 1),
            close=close,
            volume=Decimal(10),
        )
        for index, close in enumerate(closes)
    ]
    occurred_at_ms = items[3].close_time_ms + 1
    cache.upsert_candles(KEY, items, occurred_at_ms)
    ids = Ids("audit")
    observer = Observer()
    pipeline = TradingDecisionPipeline(
        EmaCrossoverStrategy(EmaCrossoverConfig(2, 3), ids),
        LongOnlyIntentPlanner(LongOnlyIntentPlannerConfig(Decimal("0.4")), ids),
        ids,
    )
    engine = ShadowTradingDecisionEngine(
        cache,
        MarketSnapshotFactory(120_000),
        pipeline,
        ids,
        ids,
        observer,
        history_limit=5,
        dedup_capacity=10,
        monotonic_clock=lambda: 1.0,
    )
    dispatcher = MarketEventDispatcher()
    engine.start(dispatcher)
    closed = CandleClosed(
        event_id="closed-3",
        occurred_at_ms=occurred_at_ms,
        stream_key=KEY,
        correlation_cycle_id="cycle-audit",
        source=MarketEventSource.REST_POLLING,
        candle=CandleSnapshot.from_candle(items[3]),
        reason=CandleClosedReason.TIME_CONFIRMED,
    )
    dispatcher.publish(closed)

    record = observer.records[0]
    assert record.source_event_id == "closed-3"
    assert record.correlation_cycle_id == "cycle-audit"
    assert record.snapshot_id and record.shadow_run_id and record.pipeline_run_id
    assert record.signal_id and record.intent_id
    assert record.signal_direction.value == "buy"
    assert record.intent_action.value == "enter_long"
    assert record.target_exposure == Decimal("0.4")
