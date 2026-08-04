from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from types import MappingProxyType

from app.market.models import MarketStreamKey
from app.shadow.exceptions import InvalidShadowDecisionRecordError
from app.trading.models import (
    ImmutableMapping,
    OrderIntentAction,
    SignalDirection,
    freeze_mapping,
)
from app.trading.pipeline import PipelineOutcome


class ShadowDecisionStatus(Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ShadowObservationStatus(Enum):
    OBSERVED = "observed"
    OBSERVATION_FAILED = "observation_failed"
    NOT_ATTEMPTED = "not_attempted"


class ShadowFailureCategory(Enum):
    CACHE_READ = "cache_read"
    EVENT_CACHE_MISMATCH = "event_cache_mismatch"
    SNAPSHOT_BUILD = "snapshot_build"
    PIPELINE_EVALUATION = "pipeline_evaluation"
    OBSERVER_FAILURE = "observer_failure"
    INTERNAL_ERROR = "internal_error"


class ShadowLifecycle(Enum):
    STOPPED = "stopped"
    RUNNING = "running"


@dataclass(frozen=True, slots=True)
class ShadowDecisionRecord:
    shadow_run_id: str
    source_event_id: str
    correlation_cycle_id: str | None
    stream_key: MarketStreamKey
    evaluated_at_ms: int
    snapshot_id: str | None
    pipeline_run_id: str | None
    signal_id: str | None
    signal_direction: SignalDirection | None
    signal_reason_code: str | None
    intent_id: str | None
    intent_action: OrderIntentAction | None
    target_exposure: Decimal | None
    pipeline_outcome: PipelineOutcome | None
    strategy_name: str | None
    strategy_version: str | None
    planner_name: str | None
    planner_version: str | None
    decision_status: ShadowDecisionStatus
    observation_status: ShadowObservationStatus
    failure_category: ShadowFailureCategory | None = None
    failure_message: str | None = None
    evaluation_duration_ms: Decimal | int | None = None
    observation_duration_ms: Decimal | int | None = None
    metadata: ImmutableMapping = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        for name in ("shadow_run_id", "source_event_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise InvalidShadowDecisionRecordError(f"{name} must be a non-empty string")
        if self.correlation_cycle_id is not None and (
            not isinstance(self.correlation_cycle_id, str) or not self.correlation_cycle_id.strip()
        ):
            raise InvalidShadowDecisionRecordError("correlation_cycle_id must be non-empty")
        if not isinstance(self.stream_key, MarketStreamKey):
            raise InvalidShadowDecisionRecordError("stream_key must be a MarketStreamKey")
        if (
            not isinstance(self.evaluated_at_ms, int)
            or isinstance(self.evaluated_at_ms, bool)
            or self.evaluated_at_ms < 0
        ):
            raise InvalidShadowDecisionRecordError("evaluated_at_ms must be non-negative")
        for name in ("evaluation_duration_ms", "observation_duration_ms"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, Decimal))
                or value < 0
                or (isinstance(value, Decimal) and not value.is_finite())
            ):
                raise InvalidShadowDecisionRecordError(f"{name} must be finite and non-negative")
        if self.target_exposure is not None and (
            not isinstance(self.target_exposure, Decimal) or not self.target_exposure.is_finite()
        ):
            raise InvalidShadowDecisionRecordError("target_exposure must be a finite Decimal")
        if self.decision_status is ShadowDecisionStatus.SUCCEEDED:
            required = (
                self.pipeline_run_id,
                self.signal_id,
                self.signal_direction,
                self.signal_reason_code,
                self.pipeline_outcome,
                self.strategy_name,
                self.strategy_version,
                self.planner_name,
                self.planner_version,
            )
            if any(
                value is None or (isinstance(value, str) and not value.strip())
                for value in required
            ):
                raise InvalidShadowDecisionRecordError(
                    "successful decision requires pipeline fields"
                )
            if self.pipeline_outcome is PipelineOutcome.INTENT_CREATED:
                if any(
                    value is None
                    for value in (self.intent_id, self.intent_action, self.target_exposure)
                ):
                    raise InvalidShadowDecisionRecordError("INTENT_CREATED requires intent fields")
            elif any(
                value is not None
                for value in (self.intent_id, self.intent_action, self.target_exposure)
            ):
                raise InvalidShadowDecisionRecordError(
                    "Only INTENT_CREATED may contain intent fields"
                )
            if self.failure_category not in (None, ShadowFailureCategory.OBSERVER_FAILURE):
                raise InvalidShadowDecisionRecordError("successful decision has invalid failure")
            if self.failure_category is None and self.failure_message is not None:
                raise InvalidShadowDecisionRecordError("failure message requires category")
        elif self.decision_status is ShadowDecisionStatus.FAILED:
            if (
                self.failure_category is None
                or not isinstance(self.failure_message, str)
                or not self.failure_message.strip()
            ):
                raise InvalidShadowDecisionRecordError("failed decision requires failure details")
        else:
            raise InvalidShadowDecisionRecordError("invalid decision status")
        try:
            object.__setattr__(self, "metadata", freeze_mapping(self.metadata))
        except (TypeError, ValueError) as error:
            raise InvalidShadowDecisionRecordError("metadata must be deeply immutable") from error


@dataclass(frozen=True, slots=True)
class ShadowDecisionEngineStatus:
    lifecycle: ShadowLifecycle
    subscribed: bool
    handled_events: int
    successful_decisions: int
    hold_decisions: int
    intent_decisions: int
    declined_decisions: int
    failed_decisions: int
    observation_failures: int
    ignored_duplicates: int
    last_event_id: str | None
    last_snapshot_id: str | None
    last_shadow_run_id: str | None
    last_pipeline_run_id: str | None
    last_signal_direction: SignalDirection | None
    last_error_category: ShadowFailureCategory | None
    last_error_message: str | None
