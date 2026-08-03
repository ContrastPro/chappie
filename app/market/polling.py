import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum
from typing import Protocol

from app.market.cache import (
    BatchUpsertResult,
    CandleUpsertResult,
    InMemoryMarketCache,
    MarketStreamKey,
)
from app.market.events import (
    CorrelationIdFactory,
    EventIdFactory,
    MarketEvent,
    MarketEventDispatcher,
    MarketEventProjector,
    MarketEventSource,
    MarketGapDetected,
    MarketGapPhase,
    MarketGapReason,
    MarketStreamFailed,
    MarketStreamFailureReason,
    UUIDCorrelationIdFactory,
    UUIDEventIdFactory,
)
from app.market.exceptions import (
    CandleSequenceValidationError,
    MarketCacheBatchError,
    MarketCacheError,
    MarketCacheGapError,
    MarketProviderError,
    MarketProviderHTTPError,
    PollingEngineConfigurationError,
    PollingEngineStateError,
)
from app.market.intervals import next_binance_spot_open_time_ms
from app.market.models import Candle
from app.market.provider import MarketProvider
from app.market.validation import validate_candle_sequence


class Clock(Protocol):
    def now_ms(self) -> int:
        """Return wall-clock time in milliseconds."""

    def monotonic(self) -> float:
        """Return monotonic time in seconds."""


class Sleeper(Protocol):
    def wait(
        self,
        seconds: float,
        stop_requested: Callable[[], bool],
    ) -> bool:
        """Wait, returning false when interrupted by a stop request."""


class SystemClock:
    def now_ms(self) -> int:
        return time.time_ns() // 1_000_000

    def monotonic(self) -> float:
        return time.monotonic()


class StopAwareSleeper:
    def __init__(self, check_interval_seconds: float = 0.1) -> None:
        if check_interval_seconds <= 0:
            raise PollingEngineConfigurationError(
                "Sleeper check interval must be positive"
            )
        self._check_interval_seconds = check_interval_seconds

    def wait(
        self,
        seconds: float,
        stop_requested: Callable[[], bool],
    ) -> bool:
        deadline = time.monotonic() + max(seconds, 0.0)
        while not stop_requested():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            time.sleep(min(remaining, self._check_interval_seconds))
        return False


class PollingLifecycleState(Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    BACKING_OFF = "backing_off"
    STOPPING = "stopping"
    FAILED = "failed"


class PollCycleOutcome(Enum):
    SUCCESS = "success"
    FAILED = "failed"
    STOPPED = "stopped"


class PollErrorCategory(Enum):
    PROVIDER = "provider"
    EMPTY_RESPONSE = "empty_response"
    VALIDATION = "validation"
    CACHE = "cache"
    GAP_RECOVERY = "gap_recovery"
    EVENT_PROJECTION = "event_projection"
    EVENT_DISPATCH = "event_dispatch"


class PollDeliveryOutcome(Enum):
    SUCCESS = "success"
    DELIVERY_DEGRADED = "delivery_degraded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class PollingPolicy:
    cadence_seconds: float = 10.0
    max_provider_attempts: int = 3
    provider_retry_delays: tuple[float, ...] = (1.0, 2.0)
    cycle_backoff_delays: tuple[float, ...] = (5.0, 10.0, 20.0, 30.0)
    max_retry_after_seconds: float = 30.0
    max_bootstrap_limit: int = 1000
    regular_limit: int = 2
    max_failed_cycles: int = 3

    def __post_init__(self) -> None:
        if self.cadence_seconds <= 0:
            raise PollingEngineConfigurationError("Polling cadence must be positive")
        if self.max_provider_attempts <= 0:
            raise PollingEngineConfigurationError("Provider attempts must be positive")
        if len(self.provider_retry_delays) < self.max_provider_attempts - 1:
            raise PollingEngineConfigurationError(
                "Provider retry delays must cover all retries"
            )
        if any(delay < 0 for delay in self.provider_retry_delays):
            raise PollingEngineConfigurationError(
                "Provider retry delays must not be negative"
            )
        if not self.cycle_backoff_delays or any(
            delay <= 0 for delay in self.cycle_backoff_delays
        ):
            raise PollingEngineConfigurationError(
                "Cycle backoff delays must be positive"
            )
        if self.max_retry_after_seconds <= 0:
            raise PollingEngineConfigurationError(
                "Maximum Retry-After must be positive"
            )
        if self.max_bootstrap_limit <= 0:
            raise PollingEngineConfigurationError(
                "Maximum bootstrap limit must be positive"
            )
        if self.regular_limit != 2:
            raise PollingEngineConfigurationError("Regular polling limit must be 2")
        if self.max_failed_cycles <= 0:
            raise PollingEngineConfigurationError(
                "Maximum failed cycles must be positive"
            )


@dataclass(frozen=True, slots=True)
class PollCycleResult:
    outcome: PollCycleOutcome
    fetched_candle_count: int
    inserted: int
    updated: int
    unchanged: int
    evicted: int
    attempts: int
    bootstrap: bool
    recovery: bool
    error_category: PollErrorCategory | None
    fatal: bool
    correlation_cycle_id: str
    published_event_count: int
    delivery_failure_count: int
    delivery_outcome: PollDeliveryOutcome
    cache_committed: bool


@dataclass(frozen=True, slots=True)
class PollingStatus:
    lifecycle_state: PollingLifecycleState
    stop_requested: bool
    consecutive_failures: int
    completed_cycles: int
    successful_cycles: int
    failed_cycles: int
    last_success_time_ms: int | None
    last_error_category: PollErrorCategory | None
    stream_key: MarketStreamKey


@dataclass(frozen=True, slots=True)
class _FetchResult:
    candles: tuple[Candle, ...]
    attempts: int
    error: MarketProviderError | None = None
    stopped: bool = False


@dataclass(frozen=True, slots=True)
class _EventPublicationResult:
    published_count: int = 0
    handler_failure_count: int = 0
    infrastructure_error: Exception | None = None


class SynchronousRestPollingEngine:
    """Blocking, single-writer ingestion engine that does not own its provider."""

    def __init__(
        self,
        provider: MarketProvider,
        cache: InMemoryMarketCache,
        stream_key: MarketStreamKey,
        *,
        policy: PollingPolicy | None = None,
        clock: Clock | None = None,
        sleeper: Sleeper | None = None,
        logger: logging.Logger | None = None,
        dispatcher: MarketEventDispatcher | None = None,
        projector: MarketEventProjector | None = None,
        event_id_factory: EventIdFactory | None = None,
        correlation_id_factory: CorrelationIdFactory | None = None,
    ) -> None:
        if (dispatcher is None) != (projector is None):
            raise PollingEngineConfigurationError(
                "Dispatcher and projector must be configured together"
            )
        self._provider = provider
        self._cache = cache
        self._stream_key = stream_key
        self._policy = policy or PollingPolicy()
        self._clock = clock or SystemClock()
        self._sleeper = sleeper or StopAwareSleeper()
        self._logger = logger or logging.getLogger(self.__class__.__name__)
        self._dispatcher = dispatcher
        self._projector = projector
        self._event_id_factory = event_id_factory or UUIDEventIdFactory()
        self._correlation_id_factory = (
            correlation_id_factory or UUIDCorrelationIdFactory()
        )
        self._status = PollingStatus(
            lifecycle_state=PollingLifecycleState.STOPPED,
            stop_requested=False,
            consecutive_failures=0,
            completed_cycles=0,
            successful_cycles=0,
            failed_cycles=0,
            last_success_time_ms=None,
            last_error_category=None,
            stream_key=stream_key,
        )

    @property
    def status(self) -> PollingStatus:
        return self._status

    def request_stop(self) -> None:
        self._status = replace(self._status, stop_requested=True)

    def run_once(self) -> PollCycleResult:
        if self._status.lifecycle_state not in {
            PollingLifecycleState.STOPPED,
            PollingLifecycleState.RUNNING,
            PollingLifecycleState.STARTING,
            PollingLifecycleState.BACKING_OFF,
        }:
            raise PollingEngineStateError(
                f"Cannot poll while engine is {self._status.lifecycle_state.value}"
            )
        if self._status.stop_requested:
            return self._stopped_result(
                bootstrap=self._is_bootstrap(),
                correlation_cycle_id=self._correlation_id_factory.next_id(),
            )

        correlation_cycle_id = self._correlation_id_factory.next_id()
        result = self._execute_cycle(
            bootstrap=self._is_bootstrap(),
            correlation_cycle_id=correlation_cycle_id,
        )
        self._record_result(result)
        if (
            result.outcome is PollCycleOutcome.FAILED
            and self._status.lifecycle_state is PollingLifecycleState.FAILED
        ):
            result = self._attach_terminal_failure_event(result)
        return result

    def run_forever(self) -> None:
        try:
            self._run_forever()
        except PollingEngineStateError:
            raise
        except Exception:
            self._status = replace(
                self._status,
                lifecycle_state=PollingLifecycleState.FAILED,
            )
            self._logger.exception("Polling engine stopped after an unexpected error")
            raise

    def _run_forever(self) -> None:
        if self._status.lifecycle_state is not PollingLifecycleState.STOPPED:
            raise PollingEngineStateError("Polling engine must be stopped before run_forever")

        self._status = replace(
            self._status,
            lifecycle_state=PollingLifecycleState.STARTING,
            stop_requested=False,
        )
        self._logger.info("Polling engine starting")
        next_deadline: float | None = None
        poll_immediately = True

        while True:
            if self._status.stop_requested:
                self._finish_stop()
                return

            if not poll_immediately:
                if next_deadline is None:
                    next_deadline = self._clock.monotonic() + self._policy.cadence_seconds
                wait_seconds = max(0.0, next_deadline - self._clock.monotonic())
                if not self._sleeper.wait(wait_seconds, self._is_stop_requested):
                    self._finish_stop()
                    return

            result = self.run_once()
            if result.outcome is PollCycleOutcome.STOPPED:
                self._finish_stop()
                return

            if result.outcome is PollCycleOutcome.SUCCESS:
                self._status = replace(
                    self._status,
                    lifecycle_state=PollingLifecycleState.RUNNING,
                )
                self._logger.info(
                    "Polling cycle succeeded: fetched=%s inserted=%s updated=%s",
                    result.fetched_candle_count,
                    result.inserted,
                    result.updated,
                )
                now = self._clock.monotonic()
                if next_deadline is None or poll_immediately:
                    next_deadline = now + self._policy.cadence_seconds
                else:
                    next_deadline += self._policy.cadence_seconds
                    while next_deadline <= now:
                        next_deadline += self._policy.cadence_seconds
                poll_immediately = False
                continue

            if self._is_fatal_result(result) or (
                self._status.consecutive_failures >= self._policy.max_failed_cycles
            ):
                self._status = replace(
                    self._status,
                    lifecycle_state=PollingLifecycleState.FAILED,
                )
                self._logger.error(
                    "Polling engine failed: category=%s",
                    result.error_category.value if result.error_category else "unknown",
                )
                return

            self._status = replace(
                self._status,
                lifecycle_state=PollingLifecycleState.BACKING_OFF,
            )
            backoff = self._cycle_backoff_seconds()
            self._logger.warning("Polling cycle backing off for %s seconds", backoff)
            if not self._sleeper.wait(backoff, self._is_stop_requested):
                self._finish_stop()
                return
            self._status = replace(
                self._status,
                lifecycle_state=(
                    PollingLifecycleState.STARTING
                    if self._is_bootstrap()
                    else PollingLifecycleState.RUNNING
                ),
            )
            next_deadline = None
            poll_immediately = True

    def _execute_cycle(
        self,
        *,
        bootstrap: bool,
        correlation_cycle_id: str,
    ) -> PollCycleResult:
        limit = (
            min(self._cache.capacity, self._policy.max_bootstrap_limit)
            if bootstrap
            else self._policy.regular_limit
        )
        fetch = self._fetch_candles(limit)
        if fetch.stopped:
            return self._stopped_result(
                bootstrap=bootstrap,
                attempts=fetch.attempts,
                correlation_cycle_id=correlation_cycle_id,
            )
        if fetch.error is not None:
            return self._failed_result(
                category=PollErrorCategory.PROVIDER,
                attempts=fetch.attempts,
                bootstrap=bootstrap,
                fatal=not fetch.error.retryable,
                correlation_cycle_id=correlation_cycle_id,
            )
        if not fetch.candles:
            return self._failed_result(
                category=PollErrorCategory.EMPTY_RESPONSE,
                attempts=fetch.attempts,
                bootstrap=bootstrap,
                correlation_cycle_id=correlation_cycle_id,
            )

        observed_at_ms = self._clock.now_ms()
        try:
            self._validate_batch(fetch.candles, observed_at_ms)
        except CandleSequenceValidationError:
            return self._recover_gap(fetch, bootstrap, correlation_cycle_id)
        except (TypeError, ValueError):
            return self._failed_result(
                category=PollErrorCategory.VALIDATION,
                attempts=fetch.attempts,
                fetched=len(fetch.candles),
                bootstrap=bootstrap,
                fatal=True,
                correlation_cycle_id=correlation_cycle_id,
            )

        try:
            cache_result = self._cache.upsert_candles(
                self._stream_key,
                fetch.candles,
                observed_at_ms,
            )
        except MarketCacheBatchError as error:
            if self._has_cause(error, MarketCacheGapError):
                return self._recover_gap(fetch, bootstrap, correlation_cycle_id)
            return self._failed_result(
                category=PollErrorCategory.CACHE,
                attempts=fetch.attempts,
                fetched=len(fetch.candles),
                bootstrap=bootstrap,
                fatal=True,
                correlation_cycle_id=correlation_cycle_id,
            )
        except MarketCacheError:
            return self._failed_result(
                category=PollErrorCategory.CACHE,
                attempts=fetch.attempts,
                fetched=len(fetch.candles),
                bootstrap=bootstrap,
                fatal=True,
                correlation_cycle_id=correlation_cycle_id,
            )

        result = self._successful_result(
            cache_result,
            fetched=len(fetch.candles),
            attempts=fetch.attempts,
            bootstrap=bootstrap,
            recovery=False,
            correlation_cycle_id=correlation_cycle_id,
        )
        return self._attach_cache_events(
            result,
            cache_result,
            observed_at_ms=observed_at_ms,
        )

    def _recover_gap(
        self,
        original_fetch: _FetchResult,
        bootstrap: bool,
        correlation_cycle_id: str,
    ) -> PollCycleResult:
        diagnostic = self._publish_gap_event(
            correlation_cycle_id,
            original_fetch,
            phase=MarketGapPhase.REGULAR,
            reason=MarketGapReason.INTERVAL_GAP,
        )
        if diagnostic.infrastructure_error is not None:
            return self._failed_result(
                category=PollErrorCategory.EVENT_DISPATCH,
                attempts=original_fetch.attempts,
                fetched=len(original_fetch.candles),
                bootstrap=bootstrap,
                fatal=True,
                correlation_cycle_id=correlation_cycle_id,
            )
        if bootstrap:
            result = self._failed_result(
                category=PollErrorCategory.GAP_RECOVERY,
                attempts=original_fetch.attempts,
                fetched=len(original_fetch.candles),
                bootstrap=True,
                recovery=True,
                correlation_cycle_id=correlation_cycle_id,
            )
            return self._merge_delivery(
                result,
                diagnostic.published_count,
                diagnostic.handler_failure_count,
            )

        self._logger.warning("Polling gap recovery started")
        recovery_fetch = self._fetch_candles(
            min(self._cache.capacity, self._policy.max_bootstrap_limit)
        )
        total_attempts = original_fetch.attempts + recovery_fetch.attempts
        if recovery_fetch.stopped:
            result = self._stopped_result(
                bootstrap=False,
                attempts=total_attempts,
                recovery=True,
                correlation_cycle_id=correlation_cycle_id,
            )
            return self._merge_delivery(
                result,
                diagnostic.published_count,
                diagnostic.handler_failure_count,
            )
        if recovery_fetch.error is not None or not recovery_fetch.candles:
            result = self._failed_result(
                category=PollErrorCategory.GAP_RECOVERY,
                attempts=total_attempts,
                fetched=len(recovery_fetch.candles),
                bootstrap=False,
                recovery=True,
                correlation_cycle_id=correlation_cycle_id,
            )
            return self._merge_delivery(
                result,
                diagnostic.published_count,
                diagnostic.handler_failure_count,
            )

        observed_at_ms = self._clock.now_ms()
        try:
            self._validate_batch(recovery_fetch.candles, observed_at_ms)
            recovery_batch = self._recovery_suffix(recovery_fetch.candles)
            cache_result = self._cache.upsert_candles(
                self._stream_key,
                recovery_batch,
                observed_at_ms,
            )
        except (TypeError, ValueError):
            recovery_diagnostic = self._publish_gap_event(
                correlation_cycle_id,
                recovery_fetch,
                phase=MarketGapPhase.RECOVERY,
                reason=MarketGapReason.RECOVERY_DISCONNECTED,
            )
            result = self._failed_result(
                category=PollErrorCategory.GAP_RECOVERY,
                attempts=total_attempts,
                fetched=len(recovery_fetch.candles),
                bootstrap=False,
                recovery=True,
                correlation_cycle_id=correlation_cycle_id,
            )
            return self._merge_delivery(
                result,
                diagnostic.published_count + recovery_diagnostic.published_count,
                diagnostic.handler_failure_count
                + recovery_diagnostic.handler_failure_count
                + (recovery_diagnostic.infrastructure_error is not None),
            )

        self._logger.info("Polling gap recovery succeeded")
        result = self._successful_result(
            cache_result,
            fetched=len(recovery_fetch.candles),
            attempts=total_attempts,
            bootstrap=False,
            recovery=True,
            correlation_cycle_id=correlation_cycle_id,
        )
        result = self._attach_cache_events(
            result,
            cache_result,
            observed_at_ms=observed_at_ms,
        )
        return self._merge_delivery(
            result,
            diagnostic.published_count,
            diagnostic.handler_failure_count,
        )

    def _fetch_candles(self, limit: int) -> _FetchResult:
        attempts = 0
        while attempts < self._policy.max_provider_attempts:
            if self._status.stop_requested:
                return _FetchResult(candles=(), attempts=attempts, stopped=True)
            attempts += 1
            try:
                candles = self._provider.get_candles(
                    self._stream_key.symbol,
                    self._stream_key.interval,
                    limit,
                )
                return _FetchResult(candles=tuple(candles), attempts=attempts)
            except MarketProviderError as error:
                if not error.retryable or attempts >= self._policy.max_provider_attempts:
                    return _FetchResult(candles=(), attempts=attempts, error=error)
                delay = self._provider_retry_seconds(error, attempts)
                self._logger.warning(
                    "Market provider retry: attempt=%s delay=%s",
                    attempts,
                    delay,
                )
                if not self._sleeper.wait(delay, self._is_stop_requested):
                    return _FetchResult(candles=(), attempts=attempts, stopped=True)
        raise AssertionError("Provider attempt loop ended unexpectedly")

    def _validate_batch(self, candles: tuple[Candle, ...], observed_at_ms: int) -> None:
        validate_candle_sequence(
            list(candles),
            self._stream_key.interval,
            allow_incomplete_last=True,
            current_time_ms=observed_at_ms,
        )

    def _recovery_suffix(self, candles: tuple[Candle, ...]) -> tuple[Candle, ...]:
        latest = self._cache.get_latest_candle(self._stream_key)
        if latest is None:
            return candles
        for index, candle in enumerate(candles):
            if candle.open_time_ms == latest.open_time_ms:
                return candles[index:]
        raise MarketCacheGapError(
            "Recovery history does not connect to the latest cached candle"
        )

    def _record_result(self, result: PollCycleResult) -> None:
        completed = self._status.completed_cycles + 1
        if result.outcome is PollCycleOutcome.SUCCESS:
            self._status = replace(
                self._status,
                completed_cycles=completed,
                successful_cycles=self._status.successful_cycles + 1,
                consecutive_failures=0,
                last_success_time_ms=self._clock.now_ms(),
                last_error_category=None,
            )
        elif result.outcome is PollCycleOutcome.FAILED:
            consecutive = self._status.consecutive_failures + 1
            state = self._status.lifecycle_state
            if self._is_fatal_result(result) or consecutive >= self._policy.max_failed_cycles:
                state = PollingLifecycleState.FAILED
            self._status = replace(
                self._status,
                lifecycle_state=state,
                completed_cycles=completed,
                failed_cycles=self._status.failed_cycles + 1,
                consecutive_failures=consecutive,
                last_error_category=result.error_category,
            )

    def _attach_cache_events(
        self,
        result: PollCycleResult,
        cache_result: BatchUpsertResult,
        *,
        observed_at_ms: int,
    ) -> PollCycleResult:
        if self._projector is None:
            return result

        try:
            events = self._projector.project(
                self._stream_key,
                cache_result.changes,
                occurred_at_ms=observed_at_ms,
                correlation_cycle_id=result.correlation_cycle_id,
                source=MarketEventSource.REST_POLLING,
                bootstrap=result.bootstrap,
                recovery=result.recovery,
            )
        except Exception:
            self._logger.exception("Market event projection failed after cache commit")
            return self._post_commit_event_failure(
                result,
                PollErrorCategory.EVENT_PROJECTION,
            )

        publication = self._publish_events(events)
        if publication.infrastructure_error is not None:
            self._logger.error(
                "Market event dispatch failed after cache commit: %r",
                publication.infrastructure_error,
            )
            failed = self._merge_delivery(
                result,
                publication.published_count,
                publication.handler_failure_count + 1,
            )
            return self._post_commit_event_failure(
                failed,
                PollErrorCategory.EVENT_DISPATCH,
            )
        return self._merge_delivery(
            result,
            publication.published_count,
            publication.handler_failure_count,
        )

    def _attach_terminal_failure_event(
        self,
        result: PollCycleResult,
    ) -> PollCycleResult:
        if self._dispatcher is None:
            return result

        category = result.error_category
        reason_by_category = {
            PollErrorCategory.PROVIDER: MarketStreamFailureReason.PROVIDER,
            PollErrorCategory.EMPTY_RESPONSE: MarketStreamFailureReason.EMPTY_RESPONSE,
            PollErrorCategory.VALIDATION: MarketStreamFailureReason.VALIDATION,
            PollErrorCategory.CACHE: MarketStreamFailureReason.CACHE_INTEGRITY,
            PollErrorCategory.GAP_RECOVERY: MarketStreamFailureReason.GAP_RECOVERY,
            PollErrorCategory.EVENT_PROJECTION: MarketStreamFailureReason.DELIVERY,
            PollErrorCategory.EVENT_DISPATCH: MarketStreamFailureReason.DELIVERY,
        }
        reason = reason_by_category.get(
            category,
            MarketStreamFailureReason.PROVIDER,
        )
        try:
            event = MarketStreamFailed(
                event_id=self._event_id_factory.next_id(),
                occurred_at_ms=self._clock.now_ms(),
                stream_key=self._stream_key,
                correlation_cycle_id=result.correlation_cycle_id,
                source=MarketEventSource.REST_POLLING,
                error_category=category.value if category is not None else "unknown",
                consecutive_failures=self._status.consecutive_failures,
                terminal=True,
                reason=reason,
            )
            publication = self._publish_events((event,))
        except Exception:
            self._logger.exception("Terminal diagnostic event creation failed")
            return self._merge_delivery(result, 0, 1)
        return self._merge_delivery(
            result,
            publication.published_count,
            publication.handler_failure_count
            + (publication.infrastructure_error is not None),
        )

    def _publish_gap_event(
        self,
        correlation_cycle_id: str,
        fetch: _FetchResult,
        *,
        phase: MarketGapPhase,
        reason: MarketGapReason,
    ) -> _EventPublicationResult:
        if self._dispatcher is None:
            return _EventPublicationResult()

        latest = self._cache.get_latest_candle(self._stream_key)
        received_open_time_ms = (
            fetch.candles[0].open_time_ms if fetch.candles else None
        )
        try:
            event = MarketGapDetected(
                event_id=self._event_id_factory.next_id(),
                occurred_at_ms=self._clock.now_ms(),
                stream_key=self._stream_key,
                correlation_cycle_id=correlation_cycle_id,
                source=MarketEventSource.REST_POLLING,
                expected_open_time_ms=(
                    next_binance_spot_open_time_ms(
                        latest.open_time_ms,
                        self._stream_key.interval,
                    )
                    if latest is not None
                    else None
                ),
                received_open_time_ms=received_open_time_ms,
                last_confirmed_open_time_ms=(
                    latest.open_time_ms if latest is not None else None
                ),
                phase=phase,
                reason=reason,
            )
        except Exception as error:  # noqa: BLE001 - reported as event infrastructure
            return _EventPublicationResult(infrastructure_error=error)
        return self._publish_events((event,))

    def _publish_events(
        self,
        events: tuple[MarketEvent, ...],
    ) -> _EventPublicationResult:
        if self._dispatcher is None:
            return _EventPublicationResult()

        failures = 0
        published = 0
        for event in events:
            try:
                delivery = self._dispatcher.publish(event)
            except Exception as error:  # noqa: BLE001 - infrastructure boundary
                return _EventPublicationResult(
                    published_count=published,
                    handler_failure_count=failures,
                    infrastructure_error=error,
                )
            failures += delivery.failure_count
            published += 1
        return _EventPublicationResult(
            published_count=published,
            handler_failure_count=failures,
        )

    @staticmethod
    def _post_commit_event_failure(
        result: PollCycleResult,
        category: PollErrorCategory,
    ) -> PollCycleResult:
        return replace(
            result,
            outcome=PollCycleOutcome.FAILED,
            error_category=category,
            fatal=True,
            delivery_outcome=PollDeliveryOutcome.FAILED,
            cache_committed=True,
        )

    @staticmethod
    def _merge_delivery(
        result: PollCycleResult,
        published_event_count: int,
        delivery_failure_count: int,
    ) -> PollCycleResult:
        total_failures = result.delivery_failure_count + delivery_failure_count
        delivery_outcome = result.delivery_outcome
        if result.outcome is PollCycleOutcome.SUCCESS and total_failures:
            delivery_outcome = PollDeliveryOutcome.DELIVERY_DEGRADED
        return replace(
            result,
            published_event_count=(
                result.published_event_count + published_event_count
            ),
            delivery_failure_count=total_failures,
            delivery_outcome=delivery_outcome,
        )

    @staticmethod
    def _failed_result(
        *,
        category: PollErrorCategory,
        attempts: int,
        bootstrap: bool,
        fetched: int = 0,
        recovery: bool = False,
        fatal: bool = False,
        correlation_cycle_id: str,
        cache_committed: bool = False,
    ) -> PollCycleResult:
        return PollCycleResult(
            outcome=PollCycleOutcome.FAILED,
            fetched_candle_count=fetched,
            inserted=0,
            updated=0,
            unchanged=0,
            evicted=0,
            attempts=attempts,
            bootstrap=bootstrap,
            recovery=recovery,
            error_category=category,
            fatal=fatal,
            correlation_cycle_id=correlation_cycle_id,
            published_event_count=0,
            delivery_failure_count=0,
            delivery_outcome=PollDeliveryOutcome.FAILED,
            cache_committed=cache_committed,
        )

    @staticmethod
    def _stopped_result(
        *,
        bootstrap: bool,
        attempts: int = 0,
        recovery: bool = False,
        correlation_cycle_id: str,
    ) -> PollCycleResult:
        return PollCycleResult(
            outcome=PollCycleOutcome.STOPPED,
            fetched_candle_count=0,
            inserted=0,
            updated=0,
            unchanged=0,
            evicted=0,
            attempts=attempts,
            bootstrap=bootstrap,
            recovery=recovery,
            error_category=None,
            fatal=False,
            correlation_cycle_id=correlation_cycle_id,
            published_event_count=0,
            delivery_failure_count=0,
            delivery_outcome=PollDeliveryOutcome.SUCCESS,
            cache_committed=False,
        )

    @staticmethod
    def _successful_result(
        cache_result: BatchUpsertResult,
        *,
        fetched: int,
        attempts: int,
        bootstrap: bool,
        recovery: bool,
        correlation_cycle_id: str,
    ) -> PollCycleResult:
        return PollCycleResult(
            outcome=PollCycleOutcome.SUCCESS,
            fetched_candle_count=fetched,
            inserted=cache_result.results.count(CandleUpsertResult.INSERTED),
            updated=cache_result.results.count(CandleUpsertResult.UPDATED),
            unchanged=cache_result.results.count(CandleUpsertResult.UNCHANGED),
            evicted=cache_result.evicted_count,
            attempts=attempts,
            bootstrap=bootstrap,
            recovery=recovery,
            error_category=None,
            fatal=False,
            correlation_cycle_id=correlation_cycle_id,
            published_event_count=0,
            delivery_failure_count=0,
            delivery_outcome=PollDeliveryOutcome.SUCCESS,
            cache_committed=True,
        )

    def _provider_retry_seconds(
        self,
        error: MarketProviderError,
        completed_attempts: int,
    ) -> float:
        if (
            isinstance(error, MarketProviderHTTPError)
            and error.status_code == 429
            and error.retry_after_seconds is not None
        ):
            return min(
                float(error.retry_after_seconds),
                self._policy.max_retry_after_seconds,
            )
        return self._policy.provider_retry_delays[completed_attempts - 1]

    def _cycle_backoff_seconds(self) -> float:
        index = min(
            self._status.consecutive_failures - 1,
            len(self._policy.cycle_backoff_delays) - 1,
        )
        return self._policy.cycle_backoff_delays[index]

    def _is_bootstrap(self) -> bool:
        return self._cache.get_latest_candle(self._stream_key) is None

    def _is_stop_requested(self) -> bool:
        return self._status.stop_requested

    def _finish_stop(self) -> None:
        self._status = replace(
            self._status,
            lifecycle_state=PollingLifecycleState.STOPPING,
        )
        self._logger.info("Polling engine stopping gracefully")
        self._status = replace(
            self._status,
            lifecycle_state=PollingLifecycleState.STOPPED,
        )

    @staticmethod
    def _has_cause(error: BaseException, error_type: type[BaseException]) -> bool:
        current: BaseException | None = error
        while current is not None:
            if isinstance(current, error_type):
                return True
            current = current.__cause__
        return False

    @staticmethod
    def _is_fatal_result(result: PollCycleResult) -> bool:
        return result.fatal
