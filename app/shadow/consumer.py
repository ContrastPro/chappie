import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from app.market.events import CandleClosed
from app.market.models import CandleSnapshot
from app.shadow.exceptions import ShadowEventCacheMismatchError, ShadowFactoryError
from app.shadow.models import (
    ShadowDecisionRecord,
    ShadowDecisionStatus,
    ShadowFailureCategory,
    ShadowObservationStatus,
)
from app.shadow.observer import ShadowDecisionObserver
from app.shadow.ports import ShadowRunIdFactory
from app.trading.models import MarketSnapshot
from app.trading.pipeline import DecisionPipelineResult


def validate_event_cache_consistency(
    event: CandleClosed,
    candles: tuple[object, ...],
) -> None:
    """Validate a confirmed closure against one committed cache snapshot."""

    if event.occurred_at_ms <= event.candle.close_time_ms:
        raise ShadowEventCacheMismatchError("CandleClosed occurred at or before close boundary")
    if not candles:
        raise ShadowEventCacheMismatchError("Market cache snapshot is empty")
    found = next(
        (item for item in candles if item.open_time_ms == event.candle.open_time_ms),
        None,
    )
    if found is None:
        raise ShadowEventCacheMismatchError("Event candle is absent from configured history")
    cached = CandleSnapshot.from_candle(found)
    if cached != event.candle:
        raise ShadowEventCacheMismatchError("Event candle does not match cache snapshot")
    if event.occurred_at_ms <= cached.close_time_ms:
        raise ShadowEventCacheMismatchError("Cached event candle is not closed")


@dataclass(frozen=True, slots=True)
class ShadowDecisionConsumerStatus:
    handled_decisions: int
    successful_records: int
    observation_failures: int
    last_record: ShadowDecisionRecord | None
    last_error: str | None


class ShadowDecisionConsumer:
    """Observe a precomputed shared decision without cache or pipeline access."""

    def __init__(
        self,
        run_id_factory: ShadowRunIdFactory,
        observer: ShadowDecisionObserver,
        monotonic_clock: Callable[[], float] = time.monotonic,
        logger: logging.Logger | None = None,
    ) -> None:
        if not isinstance(run_id_factory, ShadowRunIdFactory):
            raise TypeError("run_id_factory does not satisfy its contract")
        if not isinstance(observer, ShadowDecisionObserver):
            raise TypeError("observer does not satisfy its contract")
        if not callable(monotonic_clock):
            raise TypeError("monotonic_clock must be callable")
        self._ids = run_id_factory
        self._observer = observer
        self._clock = monotonic_clock
        self._logger = logger or logging.getLogger("ShadowDecisions")
        self._handled = 0
        self._successful = 0
        self._observation_failures = 0
        self._last_record: ShadowDecisionRecord | None = None
        self._last_error: str | None = None

    def consume(
        self,
        event: CandleClosed,
        snapshot: MarketSnapshot,
        decision_result: DecisionPipelineResult,
        coordination_id: str,
    ) -> ShadowDecisionRecord:
        if not isinstance(event, CandleClosed):
            raise TypeError("event must be CandleClosed")
        if not isinstance(snapshot, MarketSnapshot):
            raise TypeError("snapshot must be MarketSnapshot")
        if not isinstance(decision_result, DecisionPipelineResult):
            raise TypeError("decision_result must be DecisionPipelineResult")
        if not isinstance(coordination_id, str) or not coordination_id.strip():
            raise ValueError("coordination_id must be non-empty")
        if (
            snapshot.source_event_id != event.event_id
            or snapshot.snapshot_id != decision_result.snapshot_id
        ):
            raise ValueError("shared decision correlation mismatch")
        run_id = self._new_id()
        intent = decision_result.order_intent
        record = ShadowDecisionRecord(
            shadow_run_id=run_id,
            source_event_id=event.event_id,
            correlation_cycle_id=event.correlation_cycle_id,
            stream_key=event.stream_key,
            evaluated_at_ms=event.occurred_at_ms,
            snapshot_id=snapshot.snapshot_id,
            pipeline_run_id=decision_result.pipeline_run_id,
            signal_id=decision_result.signal.signal_id,
            signal_direction=decision_result.signal.direction,
            signal_reason_code=decision_result.signal.reason_code,
            intent_id=intent.intent_id if intent else None,
            intent_action=intent.action if intent else None,
            target_exposure=intent.target_exposure if intent else None,
            pipeline_outcome=decision_result.outcome,
            strategy_name=decision_result.strategy_name,
            strategy_version=decision_result.strategy_version,
            planner_name=decision_result.intent_plan.planner_name,
            planner_version=decision_result.intent_plan.planner_version,
            decision_status=ShadowDecisionStatus.SUCCEEDED,
            observation_status=ShadowObservationStatus.OBSERVED,
            metadata={
                "pipeline_reason_code": decision_result.reason_code,
                "coordination_id": coordination_id,
            },
        )
        started = self._clock()
        try:
            self._observer.on_decision(record)
        except Exception as error:  # observer degradation is part of shadow semantics
            self._observation_failures += 1
            self._last_error = _safe_message(error)
            self._logger.error("Shadow decision observer failed")
            logging.getLogger("ShadowDecisionTraceback").exception(
                "Unexpected shared shadow observer failure"
            )
            record = replace(
                record,
                observation_status=ShadowObservationStatus.OBSERVATION_FAILED,
                failure_category=ShadowFailureCategory.OBSERVER_FAILURE,
                failure_message=self._last_error,
                observation_duration_ms=_duration(self._clock, started),
            )
        else:
            record = replace(
                record,
                observation_duration_ms=_duration(self._clock, started),
            )
            self._last_error = None
        self._handled += 1
        self._successful += 1
        self._last_record = record
        return record

    def _new_id(self) -> str:
        try:
            value = self._ids.new_shadow_run_id()
        except Exception as error:
            raise ShadowFactoryError("shadow run ID factory failed") from error
        if not isinstance(value, str) or not value.strip():
            raise ShadowFactoryError("shadow run ID factory returned an invalid ID")
        return value

    @property
    def status(self) -> ShadowDecisionConsumerStatus:
        return ShadowDecisionConsumerStatus(
            handled_decisions=self._handled,
            successful_records=self._successful,
            observation_failures=self._observation_failures,
            last_record=self._last_record,
            last_error=self._last_error,
        )


def _duration(clock: Callable[[], float], started: float) -> int:
    return max(0, round((clock() - started) * 1000))


def _safe_message(error: Exception) -> str:
    return (str(error) or error.__class__.__name__)[:500]
