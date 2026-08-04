import logging
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from app.market.cache import InMemoryMarketCache, MarketStreamKey
from app.market.events import (
    CandleClosed,
    CandleInserted,
    EventDeliveryResult,
    MarketEvent,
    MarketEventDispatcher,
    MarketEventProjector,
    MarketGapDetected,
    MarketGapPhase,
    MarketStreamFailed,
)
from app.market.exceptions import (
    MarketProviderHTTPError,
    MarketProviderTimeoutError,
    PollingEngineConfigurationError,
    PollingEngineStateError,
)
from app.market.models import Candle, ServerTime, TickerPrice
from app.market.polling import (
    PollCycleOutcome,
    PollDeliveryOutcome,
    PollErrorCategory,
    PollingLifecycleState,
    PollingPolicy,
    StopAwareSleeper,
    SynchronousRestPollingEngine,
)

OPEN_TIME_MS = 1_700_000_000_000
INTERVAL_MS = 60_000
KEY = MarketStreamKey("BTCUSDT", "1m")


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


class FakeClock:
    def __init__(self, wall_time_ms: int, monotonic_seconds: float = 0.0) -> None:
        self.wall_time_ms = wall_time_ms
        self.monotonic_seconds = monotonic_seconds

    def now_ms(self) -> int:
        return self.wall_time_ms

    def monotonic(self) -> float:
        return self.monotonic_seconds

    def advance(self, seconds: float) -> None:
        self.monotonic_seconds += seconds
        self.wall_time_ms += int(seconds * 1000)


class FakeSleeper:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.waits: list[float] = []
        self.on_wait: Callable[[float, int], None] | None = None

    def wait(
        self,
        seconds: float,
        stop_requested: Callable[[], bool],
    ) -> bool:
        self.waits.append(seconds)
        if self.on_wait is not None:
            self.on_wait(seconds, len(self.waits))
        if stop_requested():
            return False
        self.clock.advance(seconds)
        return True


class ScriptedMarketProvider:
    def __init__(
        self,
        responses: list[list[Candle] | Exception],
        on_call: Callable[[int], None] | None = None,
        server_time_ms: int = OPEN_TIME_MS,
        server_time_responses: list[ServerTime | Exception] | None = None,
    ) -> None:
        self.responses = list(responses)
        self.on_call = on_call
        self.calls: list[tuple[str, str, int]] = []
        self.close_calls = 0
        self.server_time_ms = server_time_ms
        self.server_time_responses = list(server_time_responses or [])
        self.server_time_calls = 0
        self.request_order: list[str] = []

    def get_server_time(self) -> ServerTime:
        self.server_time_calls += 1
        self.request_order.append("server_time")
        if self.server_time_responses:
            response = self.server_time_responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return ServerTime(timestamp_ms=self.server_time_ms)

    def get_ticker_price(self, symbol: str) -> TickerPrice:
        return TickerPrice(symbol=symbol, price=Decimal(100))

    def get_candles(self, symbol: str, interval: str, limit: int) -> list[Candle]:
        self.request_order.append("candles")
        self.calls.append((symbol, interval, limit))
        if self.on_call is not None:
            self.on_call(len(self.calls))
        if not self.responses:
            raise AssertionError("No scripted provider response remains")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return [candle.model_copy(deep=True) for candle in response]

    def close(self) -> None:
        self.close_calls += 1


class SequentialIdFactory:
    def __init__(self, prefix: str) -> None:
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


class OwnershipTrackingDispatcher(MarketEventDispatcher):
    def __init__(self) -> None:
        super().__init__()
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class FailingInfrastructureDispatcher(MarketEventDispatcher):
    def __init__(self, fail_on_publish: int) -> None:
        super().__init__()
        self.fail_on_publish = fail_on_publish
        self.publish_calls = 0

    def publish(self, event: MarketEvent) -> EventDeliveryResult:
        self.publish_calls += 1
        if self.publish_calls == self.fail_on_publish:
            raise RuntimeError("dispatcher infrastructure failed")
        return super().publish(event)


def make_engine(
    responses: list[list[Candle] | Exception],
    *,
    cache: InMemoryMarketCache | None = None,
    clock: FakeClock | None = None,
    sleeper: FakeSleeper | None = None,
    policy: PollingPolicy | None = None,
    on_provider_call: Callable[[int], None] | None = None,
    dispatcher: MarketEventDispatcher | None = None,
    projector: MarketEventProjector | None = None,
    event_id_factory: SequentialIdFactory | None = None,
    correlation_id_factory: SequentialIdFactory | None = None,
    server_time_responses: list[ServerTime | Exception] | None = None,
) -> tuple[
    SynchronousRestPollingEngine,
    ScriptedMarketProvider,
    InMemoryMarketCache,
    FakeClock,
    FakeSleeper,
]:
    market_cache = cache or InMemoryMarketCache()
    fake_clock = clock or FakeClock(OPEN_TIME_MS + 20 * INTERVAL_MS)
    fake_sleeper = sleeper or FakeSleeper(fake_clock)
    provider = ScriptedMarketProvider(
        responses,
        on_provider_call,
        server_time_ms=fake_clock.wall_time_ms,
        server_time_responses=server_time_responses,
    )
    engine = SynchronousRestPollingEngine(
        provider,
        market_cache,
        KEY,
        policy=policy,
        clock=fake_clock,
        sleeper=fake_sleeper,
        dispatcher=dispatcher,
        projector=projector,
        event_id_factory=event_id_factory,
        correlation_id_factory=correlation_id_factory,
    )
    return engine, provider, market_cache, fake_clock, fake_sleeper


def seed_cache(cache: InMemoryMarketCache, candles: list[Candle], observed_at_ms: int) -> None:
    cache.upsert_candles(KEY, candles, observed_at_ms)


def test_successful_bootstrap() -> None:
    candles = [make_candle(0), make_candle(1)]
    engine, provider, cache, _, _ = make_engine([candles])

    result = engine.run_once()

    assert result.outcome is PollCycleOutcome.SUCCESS
    assert result.bootstrap is True
    assert result.fetched_candle_count == 2
    assert result.inserted == 2
    assert provider.calls == [("BTCUSDT", "1m", 1000)]
    assert cache.get_snapshot(KEY) == tuple(candles)


def test_successful_bootstrap_run_logs_one_event_summary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine, _, _, _, _ = make_engine([[make_candle(0), make_candle(1)]])

    with caplog.at_level(logging.INFO, logger="SynchronousRestPollingEngine"):
        engine.run_cycles(1)

    summaries = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("Bootstrap events published:")
    ]
    assert summaries == ["Bootstrap events published: inserted=2 updated=0 closed=0 evicted=0"]


def test_single_first_candle_in_empty_cache_is_not_a_gap() -> None:
    candle = make_candle(0)
    engine, provider, cache, _, _ = make_engine([[candle]])

    result = engine.run_once()

    assert result.outcome is PollCycleOutcome.SUCCESS
    assert result.inserted == 1
    assert result.recovery is False
    assert len(provider.calls) == 1
    assert cache.get_snapshot(KEY) == (candle,)


def test_one_thousand_consecutive_candles_pass_bootstrap() -> None:
    candles = [make_candle(index) for index in range(1000)]
    clock = FakeClock(candles[-1].close_time_ms + 1)
    engine, provider, cache, _, _ = make_engine([candles], clock=clock)

    result = engine.run_once()

    assert result.outcome is PollCycleOutcome.SUCCESS
    assert result.inserted == 1000
    assert result.recovery is False
    assert len(provider.calls) == 1
    assert len(cache.get_snapshot(KEY)) == 1000


def test_bootstrap_uses_exchange_time_when_local_clock_is_one_hour_behind() -> None:
    candles = [make_candle(index) for index in range(1000)]
    exchange_time_ms = candles[-1].close_time_ms + 1
    local_clock = FakeClock(exchange_time_ms - 60 * INTERVAL_MS)
    engine, provider, cache, _, _ = make_engine(
        [candles],
        clock=local_clock,
        server_time_responses=[ServerTime(timestamp_ms=exchange_time_ms)],
    )

    result = engine.run_once()

    assert result.outcome is PollCycleOutcome.SUCCESS
    assert result.exchange_time_ms == exchange_time_ms
    assert result.clock_skew_ms == -60 * INTERVAL_MS
    assert len(cache.get_snapshot(KEY)) == 1000
    assert provider.request_order[:2] == ["server_time", "candles"]


def test_local_clock_ahead_does_not_close_last_open_candle() -> None:
    first = make_candle(0)
    last = make_candle(1)
    exchange_time_ms = last.close_time_ms
    local_clock = FakeClock(exchange_time_ms + 60 * INTERVAL_MS)
    engine, _, cache, _, _ = make_engine(
        [[first, last]],
        clock=local_clock,
        server_time_responses=[ServerTime(timestamp_ms=exchange_time_ms)],
    )

    result = engine.run_once()

    assert result.outcome is PollCycleOutcome.SUCCESS
    assert result.clock_skew_ms == 60 * INTERVAL_MS
    assert cache.get_latest_closed_candle(KEY, exchange_time_ms) == first
    assert cache.get_latest_closed_candle(KEY, local_clock.wall_time_ms) == last


def test_two_incomplete_candles_are_rejected_by_exchange_time(
    caplog: pytest.LogCaptureFixture,
) -> None:
    candles = [make_candle(0), make_candle(1), make_candle(2)]
    exchange_time_ms = candles[1].open_time_ms
    local_clock = FakeClock(candles[-1].close_time_ms + 60 * INTERVAL_MS)
    engine, provider, cache, _, _ = make_engine(
        [candles],
        clock=local_clock,
        server_time_responses=[ServerTime(timestamp_ms=exchange_time_ms)],
    )

    with caplog.at_level(logging.ERROR):
        result = engine.run_once()

    assert result.error_category is PollErrorCategory.BOOTSTRAP_VALIDATION
    assert result.fatal is True
    assert len(provider.calls) == 1
    assert cache.get_snapshot(KEY) == ()
    assert "invalid_index=1" in caplog.text
    assert "incomplete_candle_count=2" in caplog.text
    assert f"observed_at_ms={exchange_time_ms}" in caplog.text


def test_internal_bootstrap_gap_is_fatal_validation_without_recovery() -> None:
    candles = [make_candle(0), make_candle(2)]
    dispatcher = MarketEventDispatcher()
    received: list[MarketEvent] = []
    dispatcher.subscribe(MarketEvent, received.append)
    projector = MarketEventProjector(SequentialIdFactory("event"))
    engine, provider, cache, _, _ = make_engine(
        [candles],
        dispatcher=dispatcher,
        projector=projector,
        event_id_factory=SequentialIdFactory("diagnostic"),
    )

    result = engine.run_once()

    assert result.outcome is PollCycleOutcome.FAILED
    assert result.error_category is PollErrorCategory.BOOTSTRAP_VALIDATION
    assert result.fatal is True
    assert result.bootstrap is True
    assert result.recovery is False
    assert len(provider.calls) == 1
    assert cache.get_snapshot(KEY) == ()
    gap = next(event for event in received if isinstance(event, MarketGapDetected))
    assert gap.phase is MarketGapPhase.BOOTSTRAP
    assert gap.previous_index == 0
    assert gap.current_index == 1
    assert gap.previous_open_time_ms == candles[0].open_time_ms
    assert gap.previous_close_time_ms == candles[0].close_time_ms
    assert gap.expected_open_time_ms == make_candle(1).open_time_ms
    assert gap.received_open_time_ms == candles[1].open_time_ms
    assert gap.actual_difference_ms == 2 * INTERVAL_MS


def test_bootstrap_limit_does_not_exceed_provider_maximum() -> None:
    cache = InMemoryMarketCache(capacity=2000)
    engine, provider, _, _, _ = make_engine([[make_candle(0)]], cache=cache)

    engine.run_once()

    assert provider.calls == [("BTCUSDT", "1m", 1000)]


def test_successful_regular_cycle_requests_two_candles() -> None:
    first = make_candle(0)
    second = make_candle(1)
    cache = InMemoryMarketCache()
    seed_cache(cache, [first], OPEN_TIME_MS + 10 * INTERVAL_MS)
    engine, provider, _, _, _ = make_engine([[first, second]], cache=cache)

    result = engine.run_once()

    assert result.outcome is PollCycleOutcome.SUCCESS
    assert result.bootstrap is False
    assert result.unchanged == 1
    assert result.inserted == 1
    assert provider.calls[0][2] == 2


def test_regular_limit_above_two_ignores_history_before_cached_latest() -> None:
    first = make_candle(0)
    latest = make_candle(1)
    next_candle = make_candle(2)
    cache = InMemoryMarketCache()
    seed_cache(cache, [first, latest], OPEN_TIME_MS + 10 * INTERVAL_MS)
    engine, provider, _, _, _ = make_engine(
        [[first, latest, next_candle]],
        cache=cache,
        policy=PollingPolicy(regular_limit=3),
    )

    result = engine.run_once()

    assert result.outcome is PollCycleOutcome.SUCCESS
    assert result.unchanged == 1
    assert result.inserted == 1
    assert provider.calls[0][2] == 3
    assert cache.get_snapshot(KEY) == (first, latest, next_candle)


def test_regular_cycle_updates_open_candle() -> None:
    current = make_candle(0)
    updated = make_candle(0, high="115", low="85", close="108", volume="12")
    clock = FakeClock(current.close_time_ms)
    cache = InMemoryMarketCache()
    seed_cache(cache, [current], current.close_time_ms)
    engine, _, _, _, _ = make_engine([[updated]], cache=cache, clock=clock)

    result = engine.run_once()

    assert result.updated == 1
    assert cache.get_latest_candle(KEY) == updated


def test_regular_cycle_accepts_exact_duplicate() -> None:
    candle = make_candle(0)
    cache = InMemoryMarketCache()
    seed_cache(cache, [candle], candle.close_time_ms + 1)
    engine, _, _, _, _ = make_engine([[candle]], cache=cache)

    result = engine.run_once()

    assert result.unchanged == 1
    assert result.inserted == result.updated == 0


def test_monotonic_scheduling_does_not_accumulate_drift() -> None:
    candles = [make_candle(0), make_candle(1)]
    clock = FakeClock(OPEN_TIME_MS + 20 * INTERVAL_MS)

    def advance_regular_request(call_number: int) -> None:
        if call_number == 2:
            clock.advance(3.0)

    engine, _, _, _, sleeper = make_engine(
        [[candles[0]], candles],
        clock=clock,
        on_provider_call=advance_regular_request,
    )

    def stop_on_second_wait(_seconds: float, wait_number: int) -> None:
        if wait_number == 2:
            engine.request_stop()

    sleeper.on_wait = stop_on_second_wait
    engine.run_forever()

    assert sleeper.waits == [10.0, 7.0]
    assert engine.status.lifecycle_state is PollingLifecycleState.STOPPED


def test_request_stop_interrupts_cadence_sleep() -> None:
    engine, provider, _, _, sleeper = make_engine([[make_candle(0)]])
    sleeper.on_wait = lambda _seconds, _number: engine.request_stop()

    engine.run_forever()

    assert len(provider.calls) == 1
    assert sleeper.waits == [10.0]
    assert engine.status.lifecycle_state is PollingLifecycleState.STOPPED


def test_request_stop_interrupts_cycle_backoff() -> None:
    failures = [MarketProviderTimeoutError("timeout") for _ in range(3)]
    engine, _, _, _, sleeper = make_engine(failures)

    def stop_on_cycle_backoff(seconds: float, _number: int) -> None:
        if seconds == 5.0:
            engine.request_stop()

    sleeper.on_wait = stop_on_cycle_backoff
    engine.run_forever()

    assert sleeper.waits == [1.0, 2.0, 5.0]
    assert engine.status.lifecycle_state is PollingLifecycleState.STOPPED


def test_transient_provider_error_retries_then_succeeds() -> None:
    engine, _, _, _, sleeper = make_engine(
        [MarketProviderTimeoutError("timeout"), [make_candle(0)]]
    )

    result = engine.run_once()

    assert result.outcome is PollCycleOutcome.SUCCESS
    assert result.attempts == 2
    assert sleeper.waits == [1.0]


def test_provider_attempts_exhausted() -> None:
    failures = [MarketProviderTimeoutError("timeout") for _ in range(3)]
    engine, provider, cache, _, sleeper = make_engine(failures)

    result = engine.run_once()

    assert result.outcome is PollCycleOutcome.FAILED
    assert result.error_category is PollErrorCategory.PROVIDER
    assert result.attempts == 3
    assert len(provider.calls) == 3
    assert sleeper.waits == [1.0, 2.0]
    assert cache.get_snapshot(KEY) == ()


def test_server_time_timeout_is_provider_failure_without_wall_clock_fallback() -> None:
    timeout = MarketProviderTimeoutError("server time timeout")
    engine, provider, cache, _, sleeper = make_engine(
        [[make_candle(0)]],
        server_time_responses=[timeout, timeout, timeout],
    )

    result = engine.run_once()

    assert result.outcome is PollCycleOutcome.FAILED
    assert result.error_category is PollErrorCategory.PROVIDER
    assert result.exchange_time_ms is None
    assert result.clock_skew_ms is None
    assert provider.server_time_calls == 3
    assert provider.calls == []
    assert provider.request_order == ["server_time"] * 3
    assert sleeper.waits == [1.0, 2.0]
    assert cache.get_snapshot(KEY) == ()


@pytest.mark.parametrize(
    ("skew_ms", "expects_warning"),
    [(6_000, True), (4_999, False)],
)
def test_clock_skew_warning_threshold(
    skew_ms: int,
    expects_warning: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    exchange_time_ms = OPEN_TIME_MS + 20 * INTERVAL_MS
    clock = FakeClock(exchange_time_ms + skew_ms)
    engine, _, _, _, _ = make_engine(
        [[make_candle(0)]],
        clock=clock,
        policy=PollingPolicy(clock_skew_warning_seconds=5),
        server_time_responses=[ServerTime(timestamp_ms=exchange_time_ms)],
    )

    with caplog.at_level(logging.WARNING):
        result = engine.run_once()

    assert result.outcome is PollCycleOutcome.SUCCESS
    assert ("Market clock skew exceeds threshold" in caplog.text) is expects_warning


def test_retry_after_is_capped_at_thirty_seconds() -> None:
    engine, _, _, _, sleeper = make_engine(
        [MarketProviderHTTPError(429, retry_after_seconds=120), [make_candle(0)]]
    )

    result = engine.run_once()

    assert result.outcome is PollCycleOutcome.SUCCESS
    assert sleeper.waits == [30.0]


def test_fatal_http_4xx_fails_without_retry() -> None:
    engine, provider, _, _, sleeper = make_engine([MarketProviderHTTPError(400)])

    result = engine.run_once()

    assert result.outcome is PollCycleOutcome.FAILED
    assert result.fatal is True
    assert result.attempts == 1
    assert len(provider.calls) == 1
    assert sleeper.waits == []
    assert engine.status.lifecycle_state is PollingLifecycleState.FAILED


def test_empty_response_fails_without_changing_cache() -> None:
    engine, _, cache, _, _ = make_engine([[]])

    result = engine.run_once()

    assert result.error_category is PollErrorCategory.EMPTY_RESPONSE
    assert cache.get_snapshot(KEY) == ()


def test_failure_counter_resets_after_success() -> None:
    engine, _, _, _, _ = make_engine([[], [make_candle(0)]])

    engine.run_once()
    success = engine.run_once()

    assert success.outcome is PollCycleOutcome.SUCCESS
    assert engine.status.consecutive_failures == 0
    assert engine.status.failed_cycles == 1
    assert engine.status.successful_cycles == 1


def test_engine_transitions_to_failed_after_three_failed_cycles() -> None:
    engine, _, _, _, _ = make_engine([[], [], []])

    engine.run_once()
    engine.run_once()
    engine.run_once()

    assert engine.status.lifecycle_state is PollingLifecycleState.FAILED
    assert engine.status.consecutive_failures == 3
    assert engine.status.failed_cycles == 3


def test_run_forever_uses_cycle_backoff_before_failure_threshold() -> None:
    engine, _, _, _, sleeper = make_engine([[], [], []])

    engine.run_forever()

    assert sleeper.waits == [5.0, 10.0]
    assert engine.status.lifecycle_state is PollingLifecycleState.FAILED
    assert engine.status.completed_cycles == 3


def test_run_cycles_stops_after_exact_successful_cycle_count() -> None:
    responses = [
        [make_candle(0)],
        [make_candle(0), make_candle(1)],
        [make_candle(1), make_candle(2)],
    ]
    engine, provider, _, _, sleeper = make_engine(responses)

    engine.run_cycles(3)

    assert engine.status.lifecycle_state is PollingLifecycleState.STOPPED
    assert engine.status.completed_cycles == 3
    assert engine.status.successful_cycles == 3
    assert [call[2] for call in provider.calls] == [1000, 2, 2]
    assert sleeper.waits == [10.0, 10.0]


@pytest.mark.parametrize("count", [0, -1, True, 1.5])
def test_run_cycles_rejects_invalid_count(count: object) -> None:
    engine, _, _, _, _ = make_engine([[make_candle(0)]])

    with pytest.raises(PollingEngineConfigurationError):
        engine.run_cycles(count)  # type: ignore[arg-type]


def test_keyboard_interrupt_during_provider_request_finishes_stopped() -> None:
    def interrupt(call_number: int) -> None:
        raise KeyboardInterrupt

    engine, provider, _, _, _ = make_engine(
        [[make_candle(0)]],
        on_provider_call=interrupt,
    )

    with pytest.raises(KeyboardInterrupt):
        engine.run_forever()

    assert len(provider.calls) == 1
    assert engine.status.lifecycle_state is PollingLifecycleState.STOPPED


@pytest.mark.parametrize("phase", ["cadence", "provider_retry", "backoff"])
def test_keyboard_interrupt_during_wait_finishes_stopped(phase: str) -> None:
    clock = FakeClock(OPEN_TIME_MS + 20 * INTERVAL_MS)
    sleeper = FakeSleeper(clock)

    def interrupt(seconds: float, wait_number: int) -> None:
        raise KeyboardInterrupt

    sleeper.on_wait = interrupt
    if phase == "cadence":
        responses: list[list[Candle] | Exception] = [[make_candle(0)]]
    elif phase == "provider_retry":
        responses = [MarketProviderTimeoutError("temporary timeout")]
    else:
        responses = [[]]
    engine, _, _, _, _ = make_engine(
        responses,
        clock=clock,
        sleeper=sleeper,
    )

    with pytest.raises(KeyboardInterrupt):
        engine.run_forever()

    assert len(sleeper.waits) == 1
    assert engine.status.lifecycle_state is PollingLifecycleState.STOPPED


def test_gap_recovery_succeeds_atomically() -> None:
    cached = make_candle(0)
    cache = InMemoryMarketCache()
    seed_cache(cache, [cached], OPEN_TIME_MS + 10 * INTERVAL_MS)
    regular_with_gap = [make_candle(2), make_candle(3)]
    recovery = [make_candle(index) for index in range(4)]
    engine, provider, _, _, _ = make_engine(
        [regular_with_gap, recovery],
        cache=cache,
    )

    result = engine.run_once()

    assert result.outcome is PollCycleOutcome.SUCCESS
    assert result.recovery is True
    assert result.attempts == 2
    assert provider.calls == [
        ("BTCUSDT", "1m", 2),
        ("BTCUSDT", "1m", 1000),
    ]
    assert cache.get_snapshot(KEY) == tuple(recovery)


def test_cache_to_batch_gap_runs_one_recovery_with_exact_diagnostic() -> None:
    cached = make_candle(0)
    cache = InMemoryMarketCache()
    seed_cache(cache, [cached], OPEN_TIME_MS + 10 * INTERVAL_MS)
    regular_with_gap = [make_candle(2), make_candle(3)]
    recovery = [make_candle(index) for index in range(4)]
    dispatcher = MarketEventDispatcher()
    received: list[MarketEvent] = []
    dispatcher.subscribe(MarketEvent, received.append)
    projector = MarketEventProjector(SequentialIdFactory("event"))
    engine, provider, _, _, _ = make_engine(
        [regular_with_gap, recovery],
        cache=cache,
        dispatcher=dispatcher,
        projector=projector,
        event_id_factory=SequentialIdFactory("diagnostic"),
    )

    result = engine.run_once()

    assert result.outcome is PollCycleOutcome.SUCCESS
    assert result.recovery is True
    assert len(provider.calls) == 2
    assert provider.server_time_calls == 1
    assert provider.request_order == ["server_time", "candles", "candles"]
    gaps = [event for event in received if isinstance(event, MarketGapDetected)]
    assert len(gaps) == 1
    assert gaps[0].previous_index == -1
    assert gaps[0].current_index == 0
    assert gaps[0].previous_open_time_ms == cached.open_time_ms
    assert gaps[0].previous_close_time_ms == cached.close_time_ms
    assert gaps[0].expected_open_time_ms == make_candle(1).open_time_ms
    assert gaps[0].received_open_time_ms == make_candle(2).open_time_ms


def test_candle_closed_is_determined_by_exchange_time() -> None:
    candle = make_candle(0)
    cache = InMemoryMarketCache()
    seed_cache(cache, [candle], candle.close_time_ms)
    dispatcher = MarketEventDispatcher()
    received: list[MarketEvent] = []
    dispatcher.subscribe(MarketEvent, received.append)
    projector = MarketEventProjector(SequentialIdFactory("event"))
    local_clock = FakeClock(candle.open_time_ms)
    exchange_time_ms = candle.close_time_ms + 1
    engine, _, _, _, _ = make_engine(
        [[candle]],
        cache=cache,
        clock=local_clock,
        dispatcher=dispatcher,
        projector=projector,
        server_time_responses=[ServerTime(timestamp_ms=exchange_time_ms)],
    )

    result = engine.run_once()

    closed = [event for event in received if isinstance(event, CandleClosed)]
    assert result.outcome is PollCycleOutcome.SUCCESS
    assert len(closed) == 1
    assert closed[0].occurred_at_ms == exchange_time_ms
    assert closed[0].candle.open_time_ms == candle.open_time_ms


def test_gap_recovery_failure_leaves_cache_unchanged() -> None:
    cached = make_candle(0)
    cache = InMemoryMarketCache()
    seed_cache(cache, [cached], OPEN_TIME_MS + 10 * INTERVAL_MS)
    engine, _, _, _, _ = make_engine(
        [[make_candle(2), make_candle(3)], [make_candle(1), make_candle(2)]],
        cache=cache,
    )

    result = engine.run_once()

    assert result.outcome is PollCycleOutcome.FAILED
    assert result.error_category is PollErrorCategory.GAP_RECOVERY
    assert cache.get_snapshot(KEY) == (cached,)


def test_validation_error_leaves_cache_unchanged() -> None:
    cached = make_candle(0)
    invalid = make_candle(1, high="0")
    cache = InMemoryMarketCache()
    seed_cache(cache, [cached], OPEN_TIME_MS + 10 * INTERVAL_MS)
    engine, _, _, _, _ = make_engine([[invalid]], cache=cache)

    result = engine.run_once()

    assert result.error_category is PollErrorCategory.VALIDATION
    assert cache.get_snapshot(KEY) == (cached,)


def test_cache_conflict_leaves_cache_unchanged() -> None:
    cached = make_candle(0)
    conflicting = make_candle(0, close="106")
    cache = InMemoryMarketCache()
    seed_cache(cache, [cached], cached.close_time_ms + 1)
    engine, _, _, _, _ = make_engine([[conflicting]], cache=cache)

    result = engine.run_once()

    assert result.error_category is PollErrorCategory.CACHE
    assert cache.get_snapshot(KEY) == (cached,)


def test_provider_is_not_closed_by_engine() -> None:
    engine, provider, _, _, sleeper = make_engine([[make_candle(0)]])
    sleeper.on_wait = lambda _seconds, _number: engine.request_stop()

    engine.run_forever()

    assert provider.close_calls == 0


def test_run_once_does_not_enter_persistent_running_state() -> None:
    engine, _, _, _, _ = make_engine([[make_candle(0)]])

    engine.run_once()

    assert engine.status.lifecycle_state is PollingLifecycleState.STOPPED


def test_status_and_result_are_immutable() -> None:
    engine, _, _, _, _ = make_engine([[make_candle(0)]])
    result = engine.run_once()
    status = engine.status

    with pytest.raises(FrozenInstanceError):
        result.attempts = 99  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        status.completed_cycles = 99  # type: ignore[misc]


def test_fatal_http_error_immediately_fails_run_forever() -> None:
    engine, provider, _, _, sleeper = make_engine([MarketProviderHTTPError(404)])

    engine.run_forever()

    assert engine.status.lifecycle_state is PollingLifecycleState.FAILED
    assert engine.status.failed_cycles == 1
    assert len(provider.calls) == 1
    assert sleeper.waits == []


def test_fatal_validation_error_skips_cycle_backoff() -> None:
    invalid = make_candle(0, high="0")
    engine, _, cache, _, sleeper = make_engine([[invalid]])

    engine.run_forever()

    assert engine.status.lifecycle_state is PollingLifecycleState.FAILED
    assert engine.status.failed_cycles == 1
    assert cache.get_snapshot(KEY) == ()
    assert sleeper.waits == []


def test_fatal_cache_conflict_skips_cycle_backoff() -> None:
    cached = make_candle(0)
    conflicting = make_candle(0, close="106")
    cache = InMemoryMarketCache()
    seed_cache(cache, [cached], cached.close_time_ms + 1)
    engine, _, _, _, sleeper = make_engine([[conflicting]], cache=cache)

    engine.run_forever()

    assert engine.status.lifecycle_state is PollingLifecycleState.FAILED
    assert engine.status.failed_cycles == 1
    assert cache.get_snapshot(KEY) == (cached,)
    assert sleeper.waits == []


def test_retryable_provider_failure_uses_attempts_then_cycle_backoff() -> None:
    failures = [MarketProviderTimeoutError("timeout") for _ in range(3)]
    engine, provider, _, _, sleeper = make_engine(failures)

    def stop_during_cycle_backoff(seconds: float, _number: int) -> None:
        if seconds == 5.0:
            assert engine.status.lifecycle_state is PollingLifecycleState.BACKING_OFF
            engine.request_stop()

    sleeper.on_wait = stop_during_cycle_backoff
    engine.run_forever()

    assert len(provider.calls) == 3
    assert sleeper.waits == [1.0, 2.0, 5.0]
    assert engine.status.lifecycle_state is PollingLifecycleState.STOPPED


def test_stop_during_provider_retry_prevents_additional_requests() -> None:
    engine, provider, _, _, sleeper = make_engine([MarketProviderTimeoutError("timeout")])
    sleeper.on_wait = lambda _seconds, _number: engine.request_stop()

    engine.run_forever()

    assert len(provider.calls) == 1
    assert sleeper.waits == [1.0]
    assert engine.status.lifecycle_state is PollingLifecycleState.STOPPED


def test_running_backoff_running_state_path() -> None:
    states_on_provider_call: list[PollingLifecycleState] = []
    failures = [MarketProviderTimeoutError("timeout") for _ in range(3)]
    responses: list[list[Candle] | Exception] = [
        [make_candle(0)],
        *failures,
        [make_candle(0), make_candle(1)],
    ]
    engine, provider, _, _, sleeper = make_engine(responses)

    def capture_provider_state(_call_number: int) -> None:
        states_on_provider_call.append(engine.status.lifecycle_state)

    provider.on_call = capture_provider_state

    def stop_after_recovery(seconds: float, wait_number: int) -> None:
        if wait_number == 5 and seconds == 10.0:
            engine.request_stop()

    sleeper.on_wait = stop_after_recovery
    engine.run_forever()

    assert states_on_provider_call[0] is PollingLifecycleState.STARTING
    assert PollingLifecycleState.RUNNING in states_on_provider_call[1:]
    assert 5.0 in sleeper.waits
    assert engine.status.lifecycle_state is PollingLifecycleState.STOPPED


def test_failed_engine_cannot_be_restarted_without_reset() -> None:
    engine, _, _, _, _ = make_engine([MarketProviderHTTPError(400)])
    engine.run_forever()

    with pytest.raises(PollingEngineStateError):
        engine.run_forever()

    assert engine.status.lifecycle_state is PollingLifecycleState.FAILED


def test_nested_run_forever_while_running_is_rejected() -> None:
    engine, _, _, _, sleeper = make_engine([[make_candle(0)]])
    nested_call_rejected = False

    def reject_nested_call(_seconds: float, _number: int) -> None:
        nonlocal nested_call_rejected
        assert engine.status.lifecycle_state is PollingLifecycleState.RUNNING
        with pytest.raises(PollingEngineStateError):
            engine.run_forever()
        nested_call_rejected = True
        engine.request_stop()

    sleeper.on_wait = reject_nested_call
    engine.run_forever()

    assert nested_call_rejected is True
    assert engine.status.lifecycle_state is PollingLifecycleState.STOPPED


def test_request_stop_is_idempotent() -> None:
    engine, _, _, _, _ = make_engine([[make_candle(0)]])

    engine.request_stop()
    first_status = engine.status
    engine.request_stop()

    assert engine.status == first_status


def test_unexpected_exception_transitions_to_failed() -> None:
    engine, _, _, _, _ = make_engine([RuntimeError("unexpected")])

    with pytest.raises(RuntimeError, match="unexpected"):
        engine.run_forever()

    assert engine.status.lifecycle_state is PollingLifecycleState.FAILED


def test_fatal_error_transitions_running_engine_to_failed() -> None:
    invalid = make_candle(1, high="0")
    engine, _, _, _, sleeper = make_engine([[make_candle(0)], [invalid]])

    engine.run_forever()

    assert sleeper.waits == [10.0]
    assert engine.status.lifecycle_state is PollingLifecycleState.FAILED
    assert engine.status.successful_cycles == 1
    assert engine.status.failed_cycles == 1


def test_exception_during_backoff_transitions_to_failed() -> None:
    failures = [MarketProviderTimeoutError("timeout") for _ in range(3)]
    engine, _, _, _, sleeper = make_engine(failures)

    def fail_during_backoff(seconds: float, _number: int) -> None:
        if seconds == 5.0:
            assert engine.status.lifecycle_state is PollingLifecycleState.BACKING_OFF
            raise RuntimeError("backoff failure")

    sleeper.on_wait = fail_during_backoff

    with pytest.raises(RuntimeError, match="backoff failure"):
        engine.run_forever()

    assert engine.status.lifecycle_state is PollingLifecycleState.FAILED


def test_successful_recovery_preserves_existing_history() -> None:
    history = [make_candle(0), make_candle(1)]
    cache = InMemoryMarketCache()
    seed_cache(cache, history, OPEN_TIME_MS + 10 * INTERVAL_MS)
    recovery = [make_candle(index) for index in range(5)]
    engine, _, _, _, _ = make_engine(
        [[make_candle(3), make_candle(4)], recovery],
        cache=cache,
    )

    result = engine.run_once()

    assert result.recovery is True
    assert cache.get_snapshot(KEY) == tuple(recovery)
    assert cache.get_snapshot(KEY)[:2] == tuple(history)


def test_recovery_cannot_replace_conflicting_closed_candle() -> None:
    cached = make_candle(0)
    cache = InMemoryMarketCache()
    seed_cache(cache, [cached], OPEN_TIME_MS + 10 * INTERVAL_MS)
    conflicting = make_candle(0, close="106")
    recovery = [conflicting, make_candle(1), make_candle(2)]
    engine, provider, _, _, _ = make_engine(
        [[make_candle(2), make_candle(3)], recovery],
        cache=cache,
    )

    result = engine.run_once()

    assert result.error_category is PollErrorCategory.GAP_RECOVERY
    assert result.recovery is True
    assert len(provider.calls) == 2
    assert cache.get_snapshot(KEY) == (cached,)


def test_failed_recovery_runs_only_once_per_cycle() -> None:
    cached = make_candle(0)
    cache = InMemoryMarketCache()
    seed_cache(cache, [cached], OPEN_TIME_MS + 10 * INTERVAL_MS)
    engine, provider, _, _, _ = make_engine(
        [[make_candle(2), make_candle(3)], [make_candle(1), make_candle(2)]],
        cache=cache,
    )

    result = engine.run_once()

    assert result.error_category is PollErrorCategory.GAP_RECOVERY
    assert len(provider.calls) == 2
    assert cache.get_snapshot(KEY) == (cached,)


def test_failed_bootstrap_remains_bootstrap_on_next_cycle() -> None:
    engine, provider, _, _, _ = make_engine([[], [make_candle(0)]])

    failed = engine.run_once()
    succeeded = engine.run_once()

    assert failed.bootstrap is True
    assert failed.recovery is False
    assert succeeded.bootstrap is True
    assert succeeded.recovery is False
    assert [call[2] for call in provider.calls] == [1000, 1000]


def test_cycle_result_distinguishes_bootstrap_regular_and_recovery() -> None:
    cache = InMemoryMarketCache()
    engine, provider, _, _, _ = make_engine(
        [
            [make_candle(0)],
            [make_candle(0), make_candle(1)],
            [make_candle(3), make_candle(4)],
            [make_candle(index) for index in range(5)],
        ],
        cache=cache,
    )

    bootstrap = engine.run_once()
    regular = engine.run_once()
    recovery = engine.run_once()

    assert (bootstrap.bootstrap, bootstrap.recovery) == (True, False)
    assert (regular.bootstrap, regular.recovery) == (False, False)
    assert (recovery.bootstrap, recovery.recovery) == (False, True)
    assert len(provider.calls) == 4


def test_successful_cycle_publishes_confirmed_cache_events() -> None:
    dispatcher = MarketEventDispatcher()
    received: list[MarketEvent] = []
    dispatcher.subscribe(MarketEvent, received.append)
    projector = MarketEventProjector(SequentialIdFactory("event"))
    correlations = SequentialIdFactory("cycle")
    candles = [make_candle(0), make_candle(1)]
    engine, _, _, _, _ = make_engine(
        [candles],
        dispatcher=dispatcher,
        projector=projector,
        correlation_id_factory=correlations,
    )

    result = engine.run_once()

    assert [type(event) for event in received] == [CandleInserted, CandleInserted]
    assert result.published_event_count == 2
    assert result.delivery_failure_count == 0
    assert result.delivery_outcome is PollDeliveryOutcome.SUCCESS
    assert result.correlation_cycle_id == "cycle-1"
    assert {event.correlation_cycle_id for event in received} == {"cycle-1"}


def test_handler_failure_degrades_delivery_without_failing_ingestion() -> None:
    dispatcher = MarketEventDispatcher()

    def fail(event: MarketEvent) -> None:
        raise RuntimeError("consumer unavailable")

    dispatcher.subscribe(CandleInserted, fail)
    projector = MarketEventProjector(SequentialIdFactory("event"))
    engine, provider, cache, _, sleeper = make_engine(
        [[make_candle(0)]],
        dispatcher=dispatcher,
        projector=projector,
    )

    result = engine.run_once()

    assert result.outcome is PollCycleOutcome.SUCCESS
    assert result.delivery_outcome is PollDeliveryOutcome.DELIVERY_DEGRADED
    assert result.delivery_failure_count == 1
    assert engine.status.consecutive_failures == 0
    assert engine.status.failed_cycles == 0
    assert len(provider.calls) == 1
    assert sleeper.waits == []
    assert result.recovery is False
    assert cache.get_latest_candle(KEY) == make_candle(0)


def test_validation_failure_publishes_no_cache_change_event() -> None:
    dispatcher = MarketEventDispatcher()
    received: list[MarketEvent] = []
    dispatcher.subscribe(MarketEvent, received.append)
    projector = MarketEventProjector(SequentialIdFactory("event"))
    invalid = make_candle(0, high="0")
    engine, _, cache, _, _ = make_engine(
        [[invalid]],
        dispatcher=dispatcher,
        projector=projector,
        event_id_factory=SequentialIdFactory("diagnostic"),
    )

    result = engine.run_once()

    assert result.outcome is PollCycleOutcome.FAILED
    assert all(not isinstance(event, CandleInserted) for event in received)
    assert [type(event) for event in received] == [MarketStreamFailed]
    assert cache.get_snapshot(KEY) == ()


def test_cache_failure_publishes_no_cache_change_event() -> None:
    cached = make_candle(0)
    conflicting = make_candle(0, close="106")
    cache = InMemoryMarketCache()
    seed_cache(cache, [cached], cached.close_time_ms + 1)
    dispatcher = MarketEventDispatcher()
    received: list[MarketEvent] = []
    dispatcher.subscribe(MarketEvent, received.append)
    projector = MarketEventProjector(SequentialIdFactory("event"))
    engine, _, _, _, _ = make_engine(
        [[conflicting]],
        cache=cache,
        dispatcher=dispatcher,
        projector=projector,
        event_id_factory=SequentialIdFactory("diagnostic"),
    )

    result = engine.run_once()

    assert result.outcome is PollCycleOutcome.FAILED
    assert result.error_category is PollErrorCategory.CACHE
    assert [type(event) for event in received] == [MarketStreamFailed]
    assert cache.get_snapshot(KEY) == (cached,)


def test_gap_diagnostic_precedes_recovery_cache_events() -> None:
    cached = make_candle(0)
    cache = InMemoryMarketCache()
    seed_cache(cache, [cached], OPEN_TIME_MS + 10 * INTERVAL_MS)
    dispatcher = MarketEventDispatcher()
    received: list[MarketEvent] = []
    dispatcher.subscribe(MarketEvent, received.append)
    projector = MarketEventProjector(SequentialIdFactory("event"))
    engine, _, _, _, _ = make_engine(
        [
            [make_candle(2), make_candle(3)],
            [make_candle(index) for index in range(4)],
        ],
        cache=cache,
        dispatcher=dispatcher,
        projector=projector,
        event_id_factory=SequentialIdFactory("diagnostic"),
        correlation_id_factory=SequentialIdFactory("cycle"),
    )

    result = engine.run_once()

    assert result.outcome is PollCycleOutcome.SUCCESS
    assert result.recovery is True
    assert isinstance(received[0], MarketGapDetected)
    assert received[0].expected_open_time_ms == make_candle(1).open_time_ms
    assert received[0].received_open_time_ms == make_candle(2).open_time_ms
    assert any(isinstance(event, CandleInserted) for event in received[1:])
    assert {event.correlation_cycle_id for event in received} == {"cycle-1"}
    assert result.published_event_count == len(received)


def test_terminal_provider_failure_publishes_stream_failed() -> None:
    dispatcher = MarketEventDispatcher()
    received: list[MarketEvent] = []
    dispatcher.subscribe(MarketEvent, received.append)
    projector = MarketEventProjector(SequentialIdFactory("event"))
    error = MarketProviderHTTPError(400)
    engine, _, _, _, _ = make_engine(
        [error],
        dispatcher=dispatcher,
        projector=projector,
        event_id_factory=SequentialIdFactory("diagnostic"),
    )

    result = engine.run_once()

    assert result.outcome is PollCycleOutcome.FAILED
    assert result.fatal is True
    assert len(received) == 1
    assert isinstance(received[0], MarketStreamFailed)
    assert received[0].terminal is True
    assert result.published_event_count == 1

    with pytest.raises(PollingEngineStateError):
        engine.run_once()
    assert len(received) == 1


def test_terminal_diagnostic_handler_failure_preserves_ingestion_error() -> None:
    dispatcher = MarketEventDispatcher()

    def fail_diagnostic(event: MarketEvent) -> None:
        raise RuntimeError("diagnostic consumer failed")

    dispatcher.subscribe(MarketStreamFailed, fail_diagnostic)
    projector = MarketEventProjector(SequentialIdFactory("event"))
    engine, provider, _, _, sleeper = make_engine(
        [MarketProviderHTTPError(400)],
        dispatcher=dispatcher,
        projector=projector,
        event_id_factory=SequentialIdFactory("diagnostic"),
    )

    result = engine.run_once()

    assert result.outcome is PollCycleOutcome.FAILED
    assert result.error_category is PollErrorCategory.PROVIDER
    assert result.delivery_failure_count == 1
    assert result.delivery_outcome is PollDeliveryOutcome.FAILED
    assert len(provider.calls) == 1
    assert sleeper.waits == []


def test_different_polling_cycles_receive_different_correlation_ids() -> None:
    dispatcher = MarketEventDispatcher()
    received: list[MarketEvent] = []
    dispatcher.subscribe(MarketEvent, received.append)
    projector = MarketEventProjector(SequentialIdFactory("event"))
    engine, _, _, _, _ = make_engine(
        [[make_candle(0)], [make_candle(0), make_candle(1)]],
        dispatcher=dispatcher,
        projector=projector,
        correlation_id_factory=SequentialIdFactory("cycle"),
    )

    first = engine.run_once()
    second = engine.run_once()

    assert first.correlation_cycle_id == "cycle-1"
    assert second.correlation_cycle_id == "cycle-2"
    assert {event.correlation_cycle_id for event in received} == {
        "cycle-1",
        "cycle-2",
    }


def test_degraded_delivery_does_not_republish_successful_handler_event() -> None:
    dispatcher = MarketEventDispatcher()
    failing_calls: list[str] = []
    successful_calls: list[str] = []

    def failing(event: MarketEvent) -> None:
        failing_calls.append(event.event_id)
        raise RuntimeError("consumer unavailable")

    dispatcher.subscribe(CandleInserted, failing)
    dispatcher.subscribe(
        CandleInserted,
        lambda event: successful_calls.append(event.event_id),
    )
    projector = MarketEventProjector(SequentialIdFactory("event"))
    candle = make_candle(0)
    engine, provider, _, _, sleeper = make_engine(
        [[candle], [candle]],
        dispatcher=dispatcher,
        projector=projector,
    )

    degraded = engine.run_once()
    unchanged = engine.run_once()

    assert degraded.delivery_outcome is PollDeliveryOutcome.DELIVERY_DEGRADED
    assert unchanged.outcome is PollCycleOutcome.SUCCESS
    assert failing_calls == ["event-1"]
    assert successful_calls == ["event-1"]
    assert len(provider.calls) == 2
    assert sleeper.waits == []


def test_diagnostic_handler_failure_does_not_mask_successful_recovery() -> None:
    cached = make_candle(0)
    cache = InMemoryMarketCache()
    seed_cache(cache, [cached], OPEN_TIME_MS + 10 * INTERVAL_MS)
    dispatcher = MarketEventDispatcher()
    received: list[MarketEvent] = []
    dispatcher.subscribe(MarketEvent, received.append)

    def fail_gap(event: MarketEvent) -> None:
        raise RuntimeError("diagnostic consumer failed")

    dispatcher.subscribe(MarketGapDetected, fail_gap)
    projector = MarketEventProjector(SequentialIdFactory("event"))
    engine, provider, _, _, sleeper = make_engine(
        [
            [make_candle(2), make_candle(3)],
            [make_candle(index) for index in range(4)],
        ],
        cache=cache,
        dispatcher=dispatcher,
        projector=projector,
    )

    result = engine.run_once()

    assert result.outcome is PollCycleOutcome.SUCCESS
    assert result.delivery_outcome is PollDeliveryOutcome.DELIVERY_DEGRADED
    assert result.delivery_failure_count == 1
    assert all(not isinstance(event, MarketStreamFailed) for event in received)
    assert len(provider.calls) == 2
    assert sleeper.waits == []


def test_projector_exception_is_failed_post_commit_cycle() -> None:
    dispatcher = MarketEventDispatcher()
    received: list[MarketEvent] = []
    dispatcher.subscribe(MarketEvent, received.append)
    projector = MarketEventProjector(FailOnceIdFactory(fail_on=2))
    candles = [make_candle(0), make_candle(1)]
    engine, _, cache, _, _ = make_engine(
        [candles],
        dispatcher=dispatcher,
        projector=projector,
        event_id_factory=SequentialIdFactory("diagnostic"),
    )

    result = engine.run_once()

    assert result.outcome is PollCycleOutcome.FAILED
    assert result.error_category is PollErrorCategory.EVENT_PROJECTION
    assert result.cache_committed is True
    assert result.fatal is True
    assert engine.status.lifecycle_state is PollingLifecycleState.FAILED
    assert cache.get_snapshot(KEY) == tuple(candles)
    assert [type(event) for event in received] == [MarketStreamFailed]


def test_dispatcher_infrastructure_exception_is_explicit_post_commit_failure() -> None:
    dispatcher = FailingInfrastructureDispatcher(fail_on_publish=2)
    received: list[MarketEvent] = []
    dispatcher.subscribe(MarketEvent, received.append)
    projector = MarketEventProjector(SequentialIdFactory("event"))
    candles = [make_candle(0), make_candle(1)]
    engine, _, cache, _, _ = make_engine(
        [candles],
        dispatcher=dispatcher,
        projector=projector,
        event_id_factory=SequentialIdFactory("diagnostic"),
    )

    result = engine.run_once()

    assert result.outcome is PollCycleOutcome.FAILED
    assert result.error_category is PollErrorCategory.EVENT_DISPATCH
    assert result.delivery_outcome is PollDeliveryOutcome.FAILED
    assert result.cache_committed is True
    assert result.published_event_count == 2
    assert result.delivery_failure_count == 1
    assert engine.status.lifecycle_state is PollingLifecycleState.FAILED
    assert cache.get_snapshot(KEY) == tuple(candles)
    assert [type(event) for event in received] == [CandleInserted, MarketStreamFailed]


def test_engine_does_not_close_externally_owned_dispatcher() -> None:
    dispatcher = OwnershipTrackingDispatcher()
    projector = MarketEventProjector(SequentialIdFactory("event"))
    engine, _, _, _, _ = make_engine(
        [[make_candle(0)]],
        dispatcher=dispatcher,
        projector=projector,
    )

    engine.run_once()

    assert dispatcher.close_calls == 0


def test_production_sleeper_checks_stop_between_short_sleep_segments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_time = 0.0
    stopped = False
    sleep_calls: list[float] = []

    def fake_monotonic() -> float:
        return current_time

    def fake_sleep(seconds: float) -> None:
        nonlocal current_time, stopped
        sleep_calls.append(seconds)
        current_time += seconds
        stopped = True

    monkeypatch.setattr("app.market.polling.time.monotonic", fake_monotonic)
    monkeypatch.setattr("app.market.polling.time.sleep", fake_sleep)
    sleeper = StopAwareSleeper(check_interval_seconds=0.1)

    completed = sleeper.wait(10.0, lambda: stopped)

    assert completed is False
    assert sleep_calls == [0.1]
