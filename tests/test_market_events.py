from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from app.market.cache import InMemoryMarketCache, MarketStreamKey
from app.market.events import (
    CandleClosed,
    CandleClosedReason,
    CandleEvicted,
    CandleInserted,
    CandleUpdated,
    MarketEvent,
    MarketEventDispatcher,
    MarketEventProjector,
    MarketEventSource,
)
from app.market.exceptions import RecursiveEventPublishError
from app.market.models import Candle

OPEN_TIME_MS = 1_700_000_000_000
INTERVAL_MS = 60_000
KEY = MarketStreamKey("BTCUSDT", "1m")
ETH_KEY = MarketStreamKey("ETHUSDT", "1m")


def make_candle(
    index: int,
    *,
    high: str = "110",
    low: str = "90",
    close: str = "105",
    volume: str = "10",
) -> Candle:
    open_time_ms = OPEN_TIME_MS + index * INTERVAL_MS
    return Candle(
        open_time_ms=open_time_ms,
        close_time_ms=open_time_ms + INTERVAL_MS - 1,
        open=Decimal(100),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(volume),
    )


class SequentialIdFactory:
    def __init__(self, prefix: str = "event") -> None:
        self.prefix = prefix
        self.value = 0

    def next_id(self) -> str:
        self.value += 1
        return f"{self.prefix}-{self.value}"


class FailOnceIdFactory(SequentialIdFactory):
    def __init__(self, fail_on: int) -> None:
        super().__init__("event")
        self.fail_on = fail_on

    def next_id(self) -> str:
        self.value += 1
        if self.value == self.fail_on:
            raise RuntimeError("id allocation failed")
        return f"{self.prefix}-{self.value}"


def project(
    projector: MarketEventProjector,
    key: MarketStreamKey,
    changes: tuple,
    *,
    observed_at_ms: int,
    bootstrap: bool = False,
    recovery: bool = False,
) -> tuple[MarketEvent, ...]:
    return projector.project(
        key,
        changes,
        occurred_at_ms=observed_at_ms,
        correlation_cycle_id="cycle-1",
        source=MarketEventSource.REST_POLLING,
        bootstrap=bootstrap,
        recovery=recovery,
    )


def make_inserted_event(event_id: str = "event-1") -> CandleInserted:
    cache = InMemoryMarketCache()
    candle = make_candle(0)
    result = cache.upsert_candles(KEY, [candle], candle.close_time_ms)
    projector = MarketEventProjector(SequentialIdFactory())
    event = project(
        projector,
        KEY,
        result.changes,
        observed_at_ms=candle.close_time_ms,
    )[0]
    assert isinstance(event, CandleInserted)
    if event.event_id == event_id:
        return event
    raise AssertionError("Unexpected deterministic event id")


def test_dispatcher_delivers_in_subscription_order_and_to_base_type() -> None:
    dispatcher = MarketEventDispatcher()
    received: list[str] = []
    dispatcher.subscribe(MarketEvent, lambda event: received.append("base"))
    dispatcher.subscribe(CandleInserted, lambda event: received.append("first"))
    dispatcher.subscribe(CandleInserted, lambda event: received.append("second"))

    result = dispatcher.publish(make_inserted_event())

    assert received == ["base", "first", "second"]
    assert result.delivered_count == 3
    assert result.failure_count == 0
    assert result.degraded is False


def test_dispatcher_unsubscribe_and_subscription_snapshot_during_publish() -> None:
    dispatcher = MarketEventDispatcher()
    received: list[str] = []
    second_token = None

    def first(event: MarketEvent) -> None:
        received.append("first")
        assert second_token is not None
        dispatcher.unsubscribe(second_token)
        dispatcher.subscribe(CandleInserted, lambda item: received.append("late"))

    dispatcher.subscribe(CandleInserted, first)
    second_token = dispatcher.subscribe(
        CandleInserted,
        lambda event: received.append("second"),
    )

    dispatcher.publish(make_inserted_event())
    dispatcher.publish(make_inserted_event())

    assert received == ["first", "second", "first", "late"]
    assert dispatcher.unsubscribe(second_token) is False


def test_dispatcher_collects_handler_failures_and_continues() -> None:
    dispatcher = MarketEventDispatcher()
    received: list[str] = []

    def failing(event: MarketEvent) -> None:
        raise RuntimeError("consumer failed")

    dispatcher.subscribe(CandleInserted, failing)
    dispatcher.subscribe(CandleInserted, lambda event: received.append("healthy"))

    result = dispatcher.publish(make_inserted_event())

    assert received == ["healthy"]
    assert result.delivered_count == 1
    assert result.failure_count == 1
    assert isinstance(result.failures[0].error, RuntimeError)
    assert result.degraded is True


def test_recursive_publish_is_reported_as_typed_delivery_failure() -> None:
    dispatcher = MarketEventDispatcher()
    event = make_inserted_event()
    dispatcher.subscribe(CandleInserted, lambda received: dispatcher.publish(received))

    result = dispatcher.publish(event)

    assert result.failure_count == 1
    assert isinstance(result.failures[0].error, RecursiveEventPublishError)


def test_dispatcher_recovers_active_publish_state_after_handler_exception() -> None:
    dispatcher = MarketEventDispatcher()
    calls = 0

    def fail_once(event: MarketEvent) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("first delivery failed")

    dispatcher.subscribe(CandleInserted, fail_once)

    failed = dispatcher.publish(make_inserted_event())
    succeeded = dispatcher.publish(make_inserted_event())

    assert failed.failure_count == 1
    assert succeeded.delivered_count == 1
    assert succeeded.failure_count == 0


def test_event_payload_is_immutable_and_detached_from_cache() -> None:
    cache = InMemoryMarketCache()
    candle = make_candle(0)
    result = cache.upsert_candles(KEY, [candle], candle.close_time_ms)
    events = project(
        MarketEventProjector(SequentialIdFactory()),
        KEY,
        result.changes,
        observed_at_ms=candle.close_time_ms,
    )
    inserted = events[0]
    assert isinstance(inserted, CandleInserted)

    with pytest.raises(FrozenInstanceError):
        inserted.candle.close = Decimal(1)  # type: ignore[misc]

    candle.close = Decimal(1)
    assert inserted.candle.close == Decimal(105)
    assert cache.get_latest_candle(KEY).close == Decimal(105)  # type: ignore[union-attr]


def test_projector_emits_insert_update_and_eviction_details() -> None:
    cache = InMemoryMarketCache(capacity=1)
    projector = MarketEventProjector(SequentialIdFactory())
    first = make_candle(0)
    initial = cache.upsert_candles(KEY, [first], first.close_time_ms)
    inserted = project(
        projector,
        KEY,
        initial.changes,
        observed_at_ms=first.close_time_ms,
    )
    updated_candle = make_candle(0, high="115", low="85", close="108", volume="12")
    updated_result = cache.upsert_candles(
        KEY,
        [updated_candle],
        first.close_time_ms,
    )
    updated = project(
        projector,
        KEY,
        updated_result.changes,
        observed_at_ms=first.close_time_ms,
    )
    second = make_candle(1)
    next_result = cache.upsert_candles(KEY, [second], second.close_time_ms)
    next_events = project(
        projector,
        KEY,
        next_result.changes,
        observed_at_ms=second.close_time_ms,
    )

    assert [type(event) for event in inserted] == [CandleInserted]
    assert [type(event) for event in updated] == [CandleUpdated]
    assert updated[0].previous.close == Decimal(105)  # type: ignore[union-attr]
    assert updated[0].current.close == Decimal(108)  # type: ignore[union-attr]
    assert [type(event) for event in next_events] == [CandleClosed, CandleInserted, CandleEvicted]
    assert next_events[2].candle.open_time_ms == first.open_time_ms  # type: ignore[union-attr]


def test_exact_duplicate_can_close_once_after_time_crosses_boundary() -> None:
    cache = InMemoryMarketCache()
    projector = MarketEventProjector(SequentialIdFactory())
    candle = make_candle(0)
    first = cache.upsert_candles(KEY, [candle], candle.close_time_ms)
    project(
        projector,
        KEY,
        first.changes,
        observed_at_ms=candle.close_time_ms,
    )

    duplicate = cache.upsert_candles(KEY, [candle], candle.close_time_ms + 1)
    first_close = project(
        projector,
        KEY,
        duplicate.changes,
        observed_at_ms=candle.close_time_ms + 1,
    )
    duplicate_again = cache.upsert_candles(KEY, [candle], candle.close_time_ms + 2)
    second_close = project(
        projector,
        KEY,
        duplicate_again.changes,
        observed_at_ms=candle.close_time_ms + 2,
    )

    assert len(first_close) == 1
    assert isinstance(first_close[0], CandleClosed)
    assert first_close[0].reason is CandleClosedReason.TIME_CONFIRMED
    assert second_close == ()


def test_exact_duplicate_before_close_has_no_mutation_event() -> None:
    cache = InMemoryMarketCache()
    projector = MarketEventProjector(SequentialIdFactory())
    candle = make_candle(0)
    inserted = cache.upsert_candles(KEY, [candle], candle.close_time_ms)
    project(
        projector,
        KEY,
        inserted.changes,
        observed_at_ms=candle.close_time_ms,
    )
    duplicate = cache.upsert_candles(KEY, [candle], candle.close_time_ms)

    events = project(
        projector,
        KEY,
        duplicate.changes,
        observed_at_ms=candle.close_time_ms,
    )

    assert events == ()


def test_next_candle_closes_previous_before_insert() -> None:
    cache = InMemoryMarketCache()
    projector = MarketEventProjector(SequentialIdFactory())
    first = make_candle(0)
    initial = cache.upsert_candles(KEY, [first], first.close_time_ms)
    project(projector, KEY, initial.changes, observed_at_ms=first.close_time_ms)
    second = make_candle(1)
    result = cache.upsert_candles(KEY, [second], second.open_time_ms)

    events = project(
        projector,
        KEY,
        result.changes,
        observed_at_ms=second.open_time_ms,
    )

    assert [type(event) for event in events] == [CandleClosed, CandleInserted]
    assert events[0].reason is CandleClosedReason.NEXT_CANDLE_OBSERVED  # type: ignore[union-attr]


def test_bootstrap_suppresses_historical_closes_but_tracks_latest_open() -> None:
    cache = InMemoryMarketCache()
    projector = MarketEventProjector(SequentialIdFactory())
    first = make_candle(0)
    latest = make_candle(1)
    bootstrap = cache.upsert_candles(KEY, [first, latest], latest.close_time_ms)

    initial_events = project(
        projector,
        KEY,
        bootstrap.changes,
        observed_at_ms=latest.close_time_ms,
        bootstrap=True,
    )
    duplicate = cache.upsert_candles(KEY, [latest], latest.close_time_ms + 1)
    later_events = project(
        projector,
        KEY,
        duplicate.changes,
        observed_at_ms=latest.close_time_ms + 1,
    )

    assert all(not isinstance(event, CandleClosed) for event in initial_events)
    assert len(later_events) == 1
    assert isinstance(later_events[0], CandleClosed)
    assert later_events[0].candle.open_time_ms == latest.open_time_ms


def test_closure_watermarks_are_independent_per_stream() -> None:
    projector = MarketEventProjector(SequentialIdFactory())
    candle = make_candle(0)
    closed_events: list[MarketEvent] = []

    for key in (KEY, ETH_KEY):
        cache = InMemoryMarketCache()
        inserted = cache.upsert_candles(key, [candle], candle.close_time_ms)
        project(projector, key, inserted.changes, observed_at_ms=candle.close_time_ms)
        duplicate = cache.upsert_candles(key, [candle], candle.close_time_ms + 1)
        closed_events.extend(
            project(
                projector,
                key,
                duplicate.changes,
                observed_at_ms=candle.close_time_ms + 1,
            )
        )

    assert [event.stream_key for event in closed_events] == [KEY, ETH_KEY]


def test_event_ids_are_unique_and_deterministic() -> None:
    cache = InMemoryMarketCache()
    candles = [make_candle(0), make_candle(1)]
    result = cache.upsert_candles(KEY, candles, candles[-1].close_time_ms)

    events = project(
        MarketEventProjector(SequentialIdFactory()),
        KEY,
        result.changes,
        observed_at_ms=candles[-1].close_time_ms,
        bootstrap=True,
    )

    assert [event.event_id for event in events] == ["event-1", "event-2"]
    assert len({event.event_id for event in events}) == len(events)


def test_factory_failure_rolls_back_projector_state_and_returns_no_events() -> None:
    cache = InMemoryMarketCache()
    candles = [make_candle(0), make_candle(1)]
    changes = cache.upsert_candles(
        KEY,
        candles,
        candles[-1].close_time_ms,
    ).changes
    projector = MarketEventProjector(FailOnceIdFactory(fail_on=2))

    with pytest.raises(RuntimeError, match="id allocation failed"):
        project(
            projector,
            KEY,
            changes,
            observed_at_ms=candles[-1].close_time_ms,
            bootstrap=True,
        )

    events = project(
        projector,
        KEY,
        changes,
        observed_at_ms=candles[-1].close_time_ms,
        bootstrap=True,
    )
    assert [type(event) for event in events] == [CandleInserted, CandleInserted]


def test_final_update_projects_updated_closed_then_next_inserted() -> None:
    cache = InMemoryMarketCache(capacity=2)
    original = make_candle(0)
    finalized = make_candle(0, high="112", low="89", close="108", volume="12")
    next_open = make_candle(1, close="101", volume="1")
    projector = MarketEventProjector(SequentialIdFactory())
    bootstrap = cache.upsert_candles(KEY, [original], original.close_time_ms)
    project(
        projector,
        KEY,
        bootstrap.changes,
        observed_at_ms=original.close_time_ms,
        bootstrap=True,
    )

    committed = cache.upsert_candles(KEY, [finalized, next_open], next_open.open_time_ms)
    events = project(
        projector,
        KEY,
        committed.changes,
        observed_at_ms=next_open.open_time_ms,
    )

    assert [type(event) for event in events] == [
        CandleUpdated,
        CandleClosed,
        CandleInserted,
    ]
    assert events[0].current == events[1].candle  # type: ignore[union-attr]
    assert events[1].candle.close == Decimal(108)  # type: ignore[union-attr]
    assert [event.event_id for event in events] == ["event-2", "event-3", "event-4"]


def test_exact_duplicate_crossing_boundary_projects_closure_only_once() -> None:
    cache = InMemoryMarketCache()
    candle = make_candle(0)
    projector = MarketEventProjector(SequentialIdFactory())
    bootstrap = cache.upsert_candles(KEY, [candle], candle.close_time_ms)
    project(
        projector,
        KEY,
        bootstrap.changes,
        observed_at_ms=candle.close_time_ms,
        bootstrap=True,
    )

    confirmed = cache.upsert_candles(KEY, [candle], candle.close_time_ms + 1)
    first = project(
        projector,
        KEY,
        confirmed.changes,
        observed_at_ms=candle.close_time_ms + 1,
    )
    repeated = cache.upsert_candles(KEY, [candle], candle.close_time_ms + 1)
    second = project(
        projector,
        KEY,
        repeated.changes,
        observed_at_ms=candle.close_time_ms + 1,
    )

    assert [type(event) for event in first] == [CandleClosed]
    assert second == ()
