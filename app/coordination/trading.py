import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import replace

from app.coordination.exceptions import (
    TradingCoordinationCacheError,
    TradingCoordinationConfigurationError,
    TradingCoordinationIdFactoryError,
    TradingCoordinationInvariantError,
    TradingCoordinationPipelineError,
    TradingCoordinationSnapshotError,
    TradingCoordinationStateError,
)
from app.coordination.factories import CoordinationIdFactory
from app.coordination.models import (
    DecisionConsumerStatus,
    TradingDecisionCoordinationResult,
    TradingDecisionCoordinationStage,
    TradingDecisionCoordinatorLifecycle,
    TradingDecisionCoordinatorStatus,
)
from app.exchange_runtime.consumer import TestnetExecutionConsumer
from app.market.events import CandleClosed, MarketEventDispatcher, SubscriptionToken
from app.paper.models import PaperTradingEngineHealth
from app.shadow.consumer import ShadowDecisionConsumer, validate_event_cache_consistency
from app.shadow.exceptions import ShadowEventCacheMismatchError
from app.shadow.ports import MarketCacheReader, SnapshotIdFactory
from app.trading.exceptions import TradingDomainError
from app.trading.models import MarketSnapshot
from app.trading.pipeline import DecisionPipelineResult, PipelineOutcome, TradingDecisionPipeline
from app.trading.snapshot import MarketSnapshotFactory


class TradingDecisionCoordinator:
    """Synchronous owner of one shared decision evaluation per CandleClosed."""

    def __init__(
        self,
        cache_reader: MarketCacheReader,
        snapshot_factory: MarketSnapshotFactory,
        decision_pipeline: TradingDecisionPipeline,
        snapshot_id_factory: SnapshotIdFactory,
        coordination_id_factory: CoordinationIdFactory,
        *,
        shadow_consumer: ShadowDecisionConsumer | None = None,
        paper_engine: object | None = None,
        testnet_consumer: TestnetExecutionConsumer | None = None,
        history_limit: int = 1000,
        dedup_capacity: int = 10000,
        monotonic_clock: Callable[[], float] = time.monotonic,
        logger: logging.Logger | None = None,
    ) -> None:
        for name, value in (("history_limit", history_limit), ("dedup_capacity", dedup_capacity)):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise TradingCoordinationConfigurationError(f"{name} must be positive integer")
        if not isinstance(cache_reader, MarketCacheReader):
            raise TradingCoordinationConfigurationError("cache_reader contract is invalid")
        if not isinstance(snapshot_id_factory, SnapshotIdFactory):
            raise TradingCoordinationConfigurationError("snapshot_id_factory contract is invalid")
        if not isinstance(coordination_id_factory, CoordinationIdFactory):
            raise TradingCoordinationConfigurationError("coordination_id_factory is invalid")
        if not hasattr(snapshot_factory, "create") or not hasattr(decision_pipeline, "evaluate"):
            raise TradingCoordinationConfigurationError("snapshot/pipeline dependency is invalid")
        if shadow_consumer is None and paper_engine is None and testnet_consumer is None:
            raise TradingCoordinationConfigurationError("at least one consumer is required")
        if shadow_consumer is not None and not hasattr(shadow_consumer, "consume"):
            raise TradingCoordinationConfigurationError("shadow_consumer is invalid")
        if paper_engine is not None and (
            not hasattr(paper_engine, "process") or not hasattr(paper_engine, "status")
        ):
            raise TradingCoordinationConfigurationError("paper_engine is invalid")
        if testnet_consumer is not None and not isinstance(
            testnet_consumer, TestnetExecutionConsumer
        ):
            raise TradingCoordinationConfigurationError("testnet_consumer is invalid")
        if not callable(monotonic_clock):
            raise TradingCoordinationConfigurationError("monotonic_clock must be callable")
        self._cache = cache_reader
        self._snapshot_factory = snapshot_factory
        self._pipeline = decision_pipeline
        self._snapshot_ids = snapshot_id_factory
        self._coordination_ids = coordination_id_factory
        self._shadow = shadow_consumer
        self._paper = paper_engine
        self._testnet = testnet_consumer
        self._history_limit = history_limit
        self._dedup_capacity = dedup_capacity
        self._clock = monotonic_clock
        self._logger = logger or logging.getLogger("TradingDecisionCoordinator")
        self._dispatcher: MarketEventDispatcher | None = None
        self._subscription: SubscriptionToken | None = None
        self._completed: OrderedDict[str, TradingDecisionCoordinationResult] = OrderedDict()
        self._status = TradingDecisionCoordinatorStatus(
            lifecycle=TradingDecisionCoordinatorLifecycle.STOPPED
        )

    def start(self, dispatcher: MarketEventDispatcher) -> None:
        if self._status.lifecycle is TradingDecisionCoordinatorLifecycle.RUNNING:
            raise TradingCoordinationStateError("coordinator is already running")
        if not isinstance(dispatcher, MarketEventDispatcher):
            raise TradingCoordinationConfigurationError("dispatcher is invalid")
        try:
            token = dispatcher.subscribe(CandleClosed, self.handle_candle_closed)
        except Exception as error:
            raise TradingCoordinationStateError("coordinator subscription failed") from error
        self._dispatcher = dispatcher
        self._subscription = token
        self._status = replace(
            self._status,
            lifecycle=TradingDecisionCoordinatorLifecycle.RUNNING,
        )

    def stop(self) -> None:
        if self._status.lifecycle is TradingDecisionCoordinatorLifecycle.STOPPED:
            return
        dispatcher = self._dispatcher
        token = self._subscription
        if dispatcher is None or token is None:
            raise TradingCoordinationStateError("running coordinator has no subscription")
        try:
            removed = dispatcher.unsubscribe(token)
        except Exception as error:
            raise TradingCoordinationStateError("coordinator unsubscribe failed") from error
        if not removed:
            raise TradingCoordinationStateError("coordinator unsubscribe was not confirmed")
        self._dispatcher = None
        self._subscription = None
        self._status = replace(
            self._status,
            lifecycle=TradingDecisionCoordinatorLifecycle.STOPPED,
        )

    def handle_candle_closed(self, event: CandleClosed) -> None:
        try:
            result = self.process_event(event)
        except Exception as error:  # expected application failures must not stop ingestion
            self._logger.error(
                "Shared decision failed source_event_id=%s category=%s",
                getattr(event, "event_id", None),
                error.__class__.__name__,
            )
            logging.getLogger("TradingDecisionCoordinatorTraceback").exception(
                "Unexpected shared decision processing failure"
            )
            return
        self._logger.info(
            "Shared decision source_event_id=%s coordination_id=%s pipeline_run_id=%s "
            "direction=%s reason=%s intent_id=%s shadow=%s paper=%s paper_outcome=%s "
            "correlation=%s duration_ms=%s",
            result.source_event_id,
            result.coordination_id,
            result.decision_pipeline_run_id,
            result.signal_direction.value,
            result.signal_reason_code,
            result.intent_id,
            result.shadow_status.value,
            result.paper_status.value,
            result.paper_cycle_result.outcome.value if result.paper_cycle_result else None,
            result.correlation_cycle_id,
            result.total_duration_ms,
        )

    def process_event(self, event: CandleClosed) -> TradingDecisionCoordinationResult:
        if self._status.lifecycle is not TradingDecisionCoordinatorLifecycle.RUNNING:
            raise TradingCoordinationStateError("process_event requires RUNNING coordinator")
        if not isinstance(event, CandleClosed):
            raise TradingCoordinationInvariantError("event must be CandleClosed")
        duplicate = self._completed.get(event.event_id)
        if duplicate is not None:
            self._status = replace(
                self._status,
                ignored_duplicates=self._status.ignored_duplicates + 1,
            )
            return duplicate
        self._status = replace(
            self._status,
            handled_events=self._status.handled_events + 1,
            last_event_id=event.event_id,
            last_error_category=None,
            last_error_message=None,
        )
        total_started = self._clock()
        stages = [
            TradingDecisionCoordinationStage.EVENT_RECEIVED,
            TradingDecisionCoordinationStage.EVENT_VALIDATED,
        ]
        try:
            cache_started = self._clock()
            try:
                candles = self._cache.get_snapshot(event.stream_key, self._history_limit)
            except Exception as error:
                raise TradingCoordinationCacheError("market cache read failed") from error
            stages.append(TradingDecisionCoordinationStage.CACHE_READ)
            try:
                validate_event_cache_consistency(event, candles)
            except ShadowEventCacheMismatchError as error:
                raise TradingCoordinationCacheError(str(error)) from error
            coordination_id = self._new_id(
                self._coordination_ids.new_coordination_id,
                "coordination",
            )
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
            except Exception as error:
                raise TradingCoordinationSnapshotError("market snapshot build failed") from error
            snapshot_duration = _duration(self._clock, cache_started)
            stages.append(TradingDecisionCoordinationStage.SNAPSHOT_CREATED)
            decision_started = self._clock()
            try:
                decision = self._pipeline.evaluate(snapshot)
            except TradingDomainError as error:
                raise TradingCoordinationPipelineError(str(error)) from error
            except Exception as error:
                raise TradingCoordinationPipelineError("decision pipeline failed") from error
            self._validate_decision(event, snapshot, decision)
            decision_duration = _duration(self._clock, decision_started)
            stages.append(TradingDecisionCoordinationStage.DECISION_EVALUATED)
        except Exception as error:
            self._status = replace(
                self._status,
                failed_decisions=self._status.failed_decisions + 1,
                last_error_category=error.__class__.__name__,
                last_error_message=_safe_message(error),
            )
            raise

        self._record_success(decision)
        shadow_status = (
            DecisionConsumerStatus.NOT_ATTEMPTED
            if self._shadow is not None
            else DecisionConsumerStatus.DISABLED
        )
        shadow_record = None
        shadow_category = shadow_message = None
        shadow_duration = 0
        if self._shadow is not None:
            started = self._clock()
            try:
                shadow_record = self._shadow.consume(event, snapshot, decision, coordination_id)
            except Exception as error:
                shadow_status = DecisionConsumerStatus.FAILED
                shadow_category = error.__class__.__name__
                shadow_message = _safe_message(error)
                stages.append(TradingDecisionCoordinationStage.SHADOW_FAILED)
                self._logger.error("Shadow consumer failed source_event_id=%s", event.event_id)
                logging.getLogger("TradingDecisionCoordinatorTraceback").exception(
                    "Unexpected shadow consumer failure"
                )
            else:
                shadow_status = DecisionConsumerStatus.DELIVERED
                stages.append(TradingDecisionCoordinationStage.SHADOW_DELIVERED)
            shadow_duration = _duration(self._clock, started)

        paper_status = (
            DecisionConsumerStatus.NOT_ATTEMPTED
            if self._paper is not None
            else DecisionConsumerStatus.DISABLED
        )
        paper_result = None
        paper_category = paper_message = None
        paper_duration = 0
        if self._paper is not None:
            started = self._clock()
            if self._paper.status.health is PaperTradingEngineHealth.DEGRADED:
                paper_status = DecisionConsumerStatus.UNAVAILABLE
                paper_category = "PaperTradingEngineDegraded"
                paper_message = "paper engine is degraded"
                stages.append(TradingDecisionCoordinationStage.PAPER_UNAVAILABLE)
            else:
                try:
                    paper_result = self._paper.process(
                        decision_result=decision,
                        market_snapshot=snapshot,
                    )
                except Exception as error:
                    paper_status = DecisionConsumerStatus.FAILED
                    paper_category = error.__class__.__name__
                    paper_message = _safe_message(error)
                    stages.append(TradingDecisionCoordinationStage.PAPER_FAILED)
                    self._logger.error("Paper consumer failed source_event_id=%s", event.event_id)
                    logging.getLogger("TradingDecisionCoordinatorTraceback").exception(
                        "Unexpected paper consumer failure"
                    )
                else:
                    paper_status = DecisionConsumerStatus.DELIVERED
                    stages.append(TradingDecisionCoordinationStage.PAPER_DELIVERED)
            paper_duration = _duration(self._clock, started)
        if self._testnet is not None:
            try:
                self._testnet.consume(decision, snapshot)
            except Exception as error:  # noqa: BLE001 - isolate private execution consumer
                self._logger.error(
                    "Testnet consumer failed source_event_id=%s category=%s",
                    event.event_id,
                    error.__class__.__name__,
                )
        stages.append(TradingDecisionCoordinationStage.COMPLETED)
        result = TradingDecisionCoordinationResult(
            coordination_id=coordination_id,
            source_event_id=event.event_id,
            correlation_cycle_id=event.correlation_cycle_id,
            stream_key=event.stream_key,
            market_snapshot_id=snapshot.snapshot_id,
            decision_pipeline_run_id=decision.pipeline_run_id,
            signal_id=decision.signal.signal_id,
            signal_direction=decision.signal.direction,
            signal_reason_code=decision.signal.reason_code,
            intent_id=decision.order_intent.intent_id if decision.order_intent else None,
            market_snapshot=snapshot,
            decision_result=decision,
            shadow_enabled=self._shadow is not None,
            shadow_status=shadow_status,
            shadow_record=shadow_record,
            shadow_error_category=shadow_category,
            shadow_error_message=shadow_message,
            paper_enabled=self._paper is not None,
            paper_status=paper_status,
            paper_cycle_result=paper_result,
            paper_error_category=paper_category,
            paper_error_message=paper_message,
            completed_stages=tuple(stages),
            snapshot_duration_ms=snapshot_duration,
            decision_duration_ms=decision_duration,
            shadow_duration_ms=shadow_duration,
            paper_duration_ms=paper_duration,
            total_duration_ms=_duration(self._clock, total_started),
            metadata={"history_limit": self._history_limit},
        )
        self._remember(result)
        self._record_delivery(result)
        return result

    def _validate_decision(
        self,
        event: CandleClosed,
        snapshot: MarketSnapshot,
        result: DecisionPipelineResult,
    ) -> None:
        if not isinstance(result, DecisionPipelineResult):
            raise TradingCoordinationInvariantError("pipeline returned invalid result type")
        signal = result.signal
        intent = result.order_intent
        if (
            result.snapshot_id != snapshot.snapshot_id
            or result.stream_key != event.stream_key
            or result.correlation_id != event.correlation_cycle_id
            or signal.snapshot_id != snapshot.snapshot_id
            or signal.stream_key != event.stream_key
            or signal.correlation_id != event.correlation_cycle_id
        ):
            raise TradingCoordinationInvariantError("decision result correlation mismatch")
        if intent is not None and (
            intent.source_signal_id != signal.signal_id
            or intent.stream_key != event.stream_key
            or intent.correlation_id != event.correlation_cycle_id
        ):
            raise TradingCoordinationInvariantError("order intent correlation mismatch")

    def _new_id(self, factory: Callable[[], str], label: str) -> str:
        try:
            value = factory()
        except Exception as error:
            raise TradingCoordinationIdFactoryError(f"{label} ID factory failed") from error
        if not isinstance(value, str) or not value.strip():
            raise TradingCoordinationIdFactoryError(f"{label} ID factory returned invalid ID")
        return value

    def _record_success(self, result: DecisionPipelineResult) -> None:
        changes = {"successful_decisions": self._status.successful_decisions + 1}
        field = {
            PipelineOutcome.HOLD: "hold_decisions",
            PipelineOutcome.INTENT_CREATED: "intent_decisions",
            PipelineOutcome.PLANNER_DECLINED: "planner_declined_decisions",
        }[result.outcome]
        changes[field] = getattr(self._status, field) + 1
        self._status = replace(self._status, **changes)

    def _record_delivery(self, result: TradingDecisionCoordinationResult) -> None:
        updates = {
            "last_coordination_id": result.coordination_id,
            "last_snapshot_id": result.market_snapshot_id,
            "last_pipeline_run_id": result.decision_pipeline_run_id,
            "last_signal_direction": result.signal_direction,
            "last_intent_id": result.intent_id,
            "last_shadow_status": result.shadow_status,
            "last_paper_status": result.paper_status,
            "last_paper_outcome": (
                result.paper_cycle_result.outcome.value if result.paper_cycle_result else None
            ),
        }
        mapping = (
            (result.shadow_status, DecisionConsumerStatus.DELIVERED, "shadow_deliveries"),
            (result.shadow_status, DecisionConsumerStatus.FAILED, "shadow_failures"),
            (result.paper_status, DecisionConsumerStatus.DELIVERED, "paper_deliveries"),
            (result.paper_status, DecisionConsumerStatus.FAILED, "paper_failures"),
            (result.paper_status, DecisionConsumerStatus.UNAVAILABLE, "paper_unavailable"),
        )
        for actual, expected, field in mapping:
            if actual is expected:
                updates[field] = getattr(self._status, field) + 1
        self._status = replace(self._status, **updates)

    def _remember(self, result: TradingDecisionCoordinationResult) -> None:
        self._completed[result.source_event_id] = result
        if len(self._completed) > self._dedup_capacity:
            self._completed.popitem(last=False)

    @property
    def status(self) -> TradingDecisionCoordinatorStatus:
        return self._status


def _duration(clock: Callable[[], float], started: float) -> int:
    return max(0, round((clock() - started) * 1000))


def _safe_message(error: Exception) -> str:
    return (str(error) or error.__class__.__name__)[:500]
