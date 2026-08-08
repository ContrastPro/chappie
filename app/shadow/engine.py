import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import replace

from app.market.events import CandleClosed, MarketEventDispatcher, SubscriptionToken
from app.shadow.consumer import validate_event_cache_consistency
from app.shadow.exceptions import (
    InvalidShadowConfigurationError,
    ShadowCacheReadError,
    ShadowEngineStateError,
    ShadowEventCacheMismatchError,
    ShadowFactoryError,
    ShadowPipelineEvaluationError,
    ShadowSnapshotBuildError,
)
from app.shadow.models import (
    ShadowDecisionEngineStatus,
    ShadowDecisionRecord,
    ShadowDecisionStatus,
    ShadowFailureCategory,
    ShadowLifecycle,
    ShadowObservationStatus,
)
from app.shadow.observer import ShadowDecisionObserver
from app.shadow.ports import MarketCacheReader, ShadowRunIdFactory, SnapshotIdFactory
from app.trading.exceptions import InvalidMarketSnapshotError, TradingDomainError
from app.trading.pipeline import PipelineOutcome, TradingDecisionPipeline
from app.trading.snapshot import MarketSnapshotFactory


class ShadowTradingDecisionEngine:
    """Single-threaded CandleClosed application handler; never executes orders."""

    def __init__(
        self,
        cache_reader: MarketCacheReader,
        snapshot_factory: MarketSnapshotFactory,
        decision_pipeline: TradingDecisionPipeline,
        snapshot_id_factory: SnapshotIdFactory,
        shadow_run_id_factory: ShadowRunIdFactory,
        observer: ShadowDecisionObserver,
        history_limit: int,
        dedup_capacity: int,
        monotonic_clock: Callable[[], float] = time.monotonic,
        logger: logging.Logger | None = None,
    ) -> None:
        for name, value in (("history_limit", history_limit), ("dedup_capacity", dedup_capacity)):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise InvalidShadowConfigurationError(f"{name} must be a positive integer")
        contracts = (
            (cache_reader, MarketCacheReader, "cache_reader"),
            (snapshot_id_factory, SnapshotIdFactory, "snapshot_id_factory"),
            (shadow_run_id_factory, ShadowRunIdFactory, "shadow_run_id_factory"),
            (observer, ShadowDecisionObserver, "observer"),
        )
        for dependency, contract, name in contracts:
            if not isinstance(dependency, contract):
                raise InvalidShadowConfigurationError(f"{name} does not satisfy its contract")
        if not hasattr(snapshot_factory, "create"):
            raise InvalidShadowConfigurationError("snapshot_factory must provide create")
        if not hasattr(decision_pipeline, "evaluate"):
            raise InvalidShadowConfigurationError("decision_pipeline must provide evaluate")
        if not callable(monotonic_clock):
            raise InvalidShadowConfigurationError("monotonic_clock must be callable")
        self._cache = cache_reader
        self._snapshot_factory = snapshot_factory
        self._pipeline = decision_pipeline
        self._snapshot_ids = snapshot_id_factory
        self._run_ids = shadow_run_id_factory
        self._observer = observer
        self._history_limit = history_limit
        self._dedup_capacity = dedup_capacity
        self._clock = monotonic_clock
        self._logger = logger or logging.getLogger("ShadowDecisionEngine")
        self._lifecycle = ShadowLifecycle.STOPPED
        self._dispatcher: MarketEventDispatcher | None = None
        self._subscription: SubscriptionToken | None = None
        self._seen_order: deque[str] = deque()
        self._seen: set[str] = set()
        self._last_record: ShadowDecisionRecord | None = None
        self._counts = {
            name: 0
            for name in (
                "handled",
                "successful",
                "hold",
                "intent",
                "declined",
                "failed",
                "observation_failures",
                "duplicates",
            )
        }

    def start(self, dispatcher: MarketEventDispatcher) -> None:
        if self._lifecycle is not ShadowLifecycle.STOPPED:
            raise ShadowEngineStateError("Shadow engine is already running")
        if not isinstance(dispatcher, MarketEventDispatcher):
            raise InvalidShadowConfigurationError("dispatcher is invalid")
        try:
            token = dispatcher.subscribe(CandleClosed, self.handle_candle_closed)
        except Exception as error:
            raise ShadowEngineStateError("Could not subscribe shadow engine") from error
        self._dispatcher = dispatcher
        self._subscription = token
        self._lifecycle = ShadowLifecycle.RUNNING

    def stop(self) -> None:
        if self._lifecycle is ShadowLifecycle.STOPPED:
            return
        assert self._dispatcher is not None and self._subscription is not None
        try:
            removed = self._dispatcher.unsubscribe(self._subscription)
        except Exception as error:
            raise ShadowEngineStateError("Could not unsubscribe shadow engine") from error
        if not removed:
            raise ShadowEngineStateError("Shadow subscription was not found")
        self._dispatcher = None
        self._subscription = None
        self._lifecycle = ShadowLifecycle.STOPPED

    def handle_candle_closed(self, event: CandleClosed) -> None:
        if self._lifecycle is not ShadowLifecycle.RUNNING:
            raise ShadowEngineStateError("Shadow engine is not running")
        if not isinstance(event, CandleClosed):
            raise ShadowEventCacheMismatchError("Only CandleClosed is supported")
        event_id = event.event_id
        if event_id in self._seen:
            self._counts["duplicates"] += 1
            return
        process_started = False
        try:
            process_started = True
            self._process(event)
        finally:
            if process_started:
                self._remember(event_id)

    def _process(self, event: CandleClosed) -> None:
        run_id: str | None = None
        snapshot_id: str | None = None
        started = self._clock()
        try:
            run_id = self._new_id(self._run_ids.new_shadow_run_id, "shadow run")
            try:
                candles = self._cache.get_snapshot(event.stream_key, self._history_limit)
            except Exception as error:
                raise ShadowCacheReadError("Market cache read failed") from error
            self._validate_consistency(event, candles)
            snapshot_id = self._new_id(self._snapshot_ids.new_snapshot_id, "snapshot")
            try:
                snapshot = self._snapshot_factory.create(
                    event.stream_key,
                    candles,
                    event.occurred_at_ms,
                    snapshot_id,
                    event.event_id,
                    event.correlation_cycle_id,
                )
            except InvalidMarketSnapshotError as error:
                raise ShadowSnapshotBuildError(str(error)) from error
            try:
                result = self._pipeline.evaluate(snapshot)
            except TradingDomainError as error:
                raise ShadowPipelineEvaluationError(str(error)) from error
            duration = self._duration(started)
            record = self._success_record(event, run_id, snapshot_id, result, duration)
            self._observe(record)
        except BaseException as error:
            if not isinstance(error, Exception):
                raise
            category = self._category(error)
            if category is ShadowFailureCategory.INTERNAL_ERROR:
                self._logger.error("Shadow decision failed category=%s", category.value)
                logging.getLogger("ShadowDecisionTraceback").exception(
                    "Unexpected shadow decision failure category=%s", category.value
                )
            else:
                self._logger.warning("Shadow decision failed category=%s", category.value)
            if run_id is None:
                try:
                    run_id = self._new_id(self._run_ids.new_shadow_run_id, "shadow run")
                except Exception:  # noqa: BLE001 - diagnostic fallback must be total
                    run_id = f"failed-{event.event_id}"
            record = ShadowDecisionRecord(
                shadow_run_id=run_id,
                source_event_id=event.event_id,
                correlation_cycle_id=event.correlation_cycle_id,
                stream_key=event.stream_key,
                evaluated_at_ms=event.occurred_at_ms,
                snapshot_id=snapshot_id,
                pipeline_run_id=None,
                signal_id=None,
                signal_direction=None,
                signal_reason_code=None,
                intent_id=None,
                intent_action=None,
                target_exposure=None,
                pipeline_outcome=None,
                strategy_name=None,
                strategy_version=None,
                planner_name=None,
                planner_version=None,
                decision_status=ShadowDecisionStatus.FAILED,
                observation_status=ShadowObservationStatus.NOT_ATTEMPTED,
                failure_category=category,
                failure_message=str(error) or error.__class__.__name__,
                evaluation_duration_ms=self._duration(started),
            )
            self._finalize(record)
            try:
                self._observe_failed(record)
            except Exception:
                self._logger.error("Failed shadow decision could not be observed")
                logging.getLogger("ShadowDecisionTraceback").exception(
                    "Unexpected failed-decision observer failure"
                )

    def _observe(self, record: ShadowDecisionRecord) -> None:
        observed = replace(record, observation_status=ShadowObservationStatus.OBSERVED)
        started = self._clock()
        try:
            self._observer.on_decision(observed)
        except BaseException as error:
            if not isinstance(error, Exception):
                raise
            failed = replace(
                record,
                observation_status=ShadowObservationStatus.OBSERVATION_FAILED,
                failure_category=ShadowFailureCategory.OBSERVER_FAILURE,
                failure_message=str(error) or error.__class__.__name__,
                observation_duration_ms=self._duration(started),
            )
            self._counts["observation_failures"] += 1
            self._logger.error("Shadow decision observer failed")
            logging.getLogger("ShadowDecisionTraceback").exception(
                "Unexpected shadow decision observer failure"
            )
            self._finalize(failed)
            return
        self._finalize(replace(observed, observation_duration_ms=self._duration(started)))

    def _observe_failed(self, record: ShadowDecisionRecord) -> None:
        started = self._clock()
        observed = replace(record, observation_status=ShadowObservationStatus.OBSERVED)
        self._observer.on_decision(observed)
        self._last_record = replace(observed, observation_duration_ms=self._duration(started))

    def _finalize(self, record: ShadowDecisionRecord) -> None:
        self._last_record = record
        self._counts["handled"] += 1
        if record.decision_status is ShadowDecisionStatus.FAILED:
            self._counts["failed"] += 1
            return
        self._counts["successful"] += 1
        key = {
            PipelineOutcome.HOLD: "hold",
            PipelineOutcome.INTENT_CREATED: "intent",
            PipelineOutcome.PLANNER_DECLINED: "declined",
        }[record.pipeline_outcome]
        self._counts[key] += 1

    @staticmethod
    def _validate_consistency(event: CandleClosed, candles: tuple[object, ...]) -> None:
        validate_event_cache_consistency(event, candles)

    @staticmethod
    def _new_id(factory: Callable[[], str], label: str) -> str:
        try:
            value = factory()
        except Exception as error:
            raise ShadowFactoryError(f"{label} ID factory failed") from error
        if not isinstance(value, str) or not value.strip():
            raise ShadowFactoryError(f"{label} ID factory returned an invalid ID")
        return value

    def _duration(self, started: float) -> int:
        return max(0, round((self._clock() - started) * 1000))

    @staticmethod
    def _category(error: Exception) -> ShadowFailureCategory:
        mapping = (
            (ShadowCacheReadError, ShadowFailureCategory.CACHE_READ),
            (ShadowEventCacheMismatchError, ShadowFailureCategory.EVENT_CACHE_MISMATCH),
            (ShadowSnapshotBuildError, ShadowFailureCategory.SNAPSHOT_BUILD),
            (ShadowPipelineEvaluationError, ShadowFailureCategory.PIPELINE_EVALUATION),
        )
        return next(
            (category for kind, category in mapping if isinstance(error, kind)),
            ShadowFailureCategory.INTERNAL_ERROR,
        )

    def _remember(self, event_id: str) -> None:
        if event_id in self._seen:
            return
        self._seen.add(event_id)
        self._seen_order.append(event_id)
        if len(self._seen_order) > self._dedup_capacity:
            self._seen.remove(self._seen_order.popleft())

    @staticmethod
    def _success_record(event, run_id, snapshot_id, result, duration):
        intent = result.order_intent
        return ShadowDecisionRecord(
            shadow_run_id=run_id,
            source_event_id=event.event_id,
            correlation_cycle_id=event.correlation_cycle_id,
            stream_key=event.stream_key,
            evaluated_at_ms=event.occurred_at_ms,
            snapshot_id=snapshot_id,
            pipeline_run_id=result.pipeline_run_id,
            signal_id=result.signal.signal_id,
            signal_direction=result.signal.direction,
            signal_reason_code=result.signal.reason_code,
            intent_id=intent.intent_id if intent else None,
            intent_action=intent.action if intent else None,
            target_exposure=intent.target_exposure if intent else None,
            pipeline_outcome=result.outcome,
            strategy_name=result.strategy_name,
            strategy_version=result.strategy_version,
            planner_name=result.intent_plan.planner_name,
            planner_version=result.intent_plan.planner_version,
            decision_status=ShadowDecisionStatus.SUCCEEDED,
            observation_status=ShadowObservationStatus.NOT_ATTEMPTED,
            evaluation_duration_ms=duration,
            metadata={"pipeline_reason_code": result.reason_code},
        )

    @property
    def last_record(self) -> ShadowDecisionRecord | None:
        return self._last_record

    @property
    def status(self) -> ShadowDecisionEngineStatus:
        record = self._last_record
        return ShadowDecisionEngineStatus(
            lifecycle=self._lifecycle,
            subscribed=self._subscription is not None,
            handled_events=self._counts["handled"],
            successful_decisions=self._counts["successful"],
            hold_decisions=self._counts["hold"],
            intent_decisions=self._counts["intent"],
            declined_decisions=self._counts["declined"],
            failed_decisions=self._counts["failed"],
            observation_failures=self._counts["observation_failures"],
            ignored_duplicates=self._counts["duplicates"],
            last_event_id=record.source_event_id if record else None,
            last_snapshot_id=record.snapshot_id if record else None,
            last_shadow_run_id=record.shadow_run_id if record else None,
            last_pipeline_run_id=record.pipeline_run_id if record else None,
            last_signal_direction=record.signal_direction if record else None,
            last_error_category=record.failure_category if record else None,
            last_error_message=record.failure_message if record else None,
        )
