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
    ) -> None:
        self._provider = provider
        self._cache = cache
        self._stream_key = stream_key
        self._policy = policy or PollingPolicy()
        self._clock = clock or SystemClock()
        self._sleeper = sleeper or StopAwareSleeper()
        self._logger = logger or logging.getLogger(self.__class__.__name__)
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
            return self._stopped_result(bootstrap=self._is_bootstrap())

        result = self._execute_cycle(bootstrap=self._is_bootstrap())
        self._record_result(result)
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

    def _execute_cycle(self, *, bootstrap: bool) -> PollCycleResult:
        limit = (
            min(self._cache.capacity, self._policy.max_bootstrap_limit)
            if bootstrap
            else self._policy.regular_limit
        )
        fetch = self._fetch_candles(limit)
        if fetch.stopped:
            return self._stopped_result(bootstrap=bootstrap, attempts=fetch.attempts)
        if fetch.error is not None:
            return self._failed_result(
                category=PollErrorCategory.PROVIDER,
                attempts=fetch.attempts,
                bootstrap=bootstrap,
                fatal=not fetch.error.retryable,
            )
        if not fetch.candles:
            return self._failed_result(
                category=PollErrorCategory.EMPTY_RESPONSE,
                attempts=fetch.attempts,
                bootstrap=bootstrap,
            )

        observed_at_ms = self._clock.now_ms()
        try:
            self._validate_batch(fetch.candles, observed_at_ms)
        except CandleSequenceValidationError:
            return self._recover_gap(fetch, bootstrap)
        except (TypeError, ValueError):
            return self._failed_result(
                category=PollErrorCategory.VALIDATION,
                attempts=fetch.attempts,
                fetched=len(fetch.candles),
                bootstrap=bootstrap,
                fatal=True,
            )

        try:
            cache_result = self._cache.upsert_candles(
                self._stream_key,
                fetch.candles,
                observed_at_ms,
            )
        except MarketCacheBatchError as error:
            if self._has_cause(error, MarketCacheGapError):
                return self._recover_gap(fetch, bootstrap)
            return self._failed_result(
                category=PollErrorCategory.CACHE,
                attempts=fetch.attempts,
                fetched=len(fetch.candles),
                bootstrap=bootstrap,
                fatal=True,
            )
        except MarketCacheError:
            return self._failed_result(
                category=PollErrorCategory.CACHE,
                attempts=fetch.attempts,
                fetched=len(fetch.candles),
                bootstrap=bootstrap,
                fatal=True,
            )

        return self._successful_result(
            cache_result,
            fetched=len(fetch.candles),
            attempts=fetch.attempts,
            bootstrap=bootstrap,
            recovery=False,
        )

    def _recover_gap(
        self,
        original_fetch: _FetchResult,
        bootstrap: bool,
    ) -> PollCycleResult:
        if bootstrap:
            return self._failed_result(
                category=PollErrorCategory.GAP_RECOVERY,
                attempts=original_fetch.attempts,
                fetched=len(original_fetch.candles),
                bootstrap=True,
                recovery=True,
            )

        self._logger.warning("Polling gap recovery started")
        recovery_fetch = self._fetch_candles(
            min(self._cache.capacity, self._policy.max_bootstrap_limit)
        )
        total_attempts = original_fetch.attempts + recovery_fetch.attempts
        if recovery_fetch.stopped:
            return self._stopped_result(
                bootstrap=False,
                attempts=total_attempts,
                recovery=True,
            )
        if recovery_fetch.error is not None or not recovery_fetch.candles:
            return self._failed_result(
                category=PollErrorCategory.GAP_RECOVERY,
                attempts=total_attempts,
                fetched=len(recovery_fetch.candles),
                bootstrap=False,
                recovery=True,
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
            return self._failed_result(
                category=PollErrorCategory.GAP_RECOVERY,
                attempts=total_attempts,
                fetched=len(recovery_fetch.candles),
                bootstrap=False,
                recovery=True,
            )

        self._logger.info("Polling gap recovery succeeded")
        return self._successful_result(
            cache_result,
            fetched=len(recovery_fetch.candles),
            attempts=total_attempts,
            bootstrap=False,
            recovery=True,
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

    @staticmethod
    def _failed_result(
        *,
        category: PollErrorCategory,
        attempts: int,
        bootstrap: bool,
        fetched: int = 0,
        recovery: bool = False,
        fatal: bool = False,
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
        )

    @staticmethod
    def _stopped_result(
        *,
        bootstrap: bool,
        attempts: int = 0,
        recovery: bool = False,
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
        )

    @staticmethod
    def _successful_result(
        cache_result: BatchUpsertResult,
        *,
        fetched: int,
        attempts: int,
        bootstrap: bool,
        recovery: bool,
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
