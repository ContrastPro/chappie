from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

from app.market.cache import MarketStreamKey
from app.paper.models import PaperTradingCycleResult
from app.shadow.models import ShadowDecisionRecord
from app.trading.models import ImmutableMapping, MarketSnapshot, SignalDirection, freeze_mapping
from app.trading.pipeline import DecisionPipelineResult


class TradingDecisionCoordinatorLifecycle(StrEnum):
    STOPPED = "STOPPED"
    RUNNING = "RUNNING"


class DecisionConsumerStatus(StrEnum):
    DISABLED = "DISABLED"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"
    UNAVAILABLE = "UNAVAILABLE"
    NOT_ATTEMPTED = "NOT_ATTEMPTED"


class TradingDecisionCoordinationStage(StrEnum):
    EVENT_RECEIVED = "EVENT_RECEIVED"
    EVENT_VALIDATED = "EVENT_VALIDATED"
    CACHE_READ = "CACHE_READ"
    SNAPSHOT_CREATED = "SNAPSHOT_CREATED"
    DECISION_EVALUATED = "DECISION_EVALUATED"
    SHADOW_DELIVERED = "SHADOW_DELIVERED"
    SHADOW_FAILED = "SHADOW_FAILED"
    PAPER_DELIVERED = "PAPER_DELIVERED"
    PAPER_FAILED = "PAPER_FAILED"
    PAPER_UNAVAILABLE = "PAPER_UNAVAILABLE"
    COMPLETED = "COMPLETED"


_ORDER = {stage: index for index, stage in enumerate(TradingDecisionCoordinationStage)}


@dataclass(frozen=True, slots=True)
class TradingDecisionCoordinationResult:
    coordination_id: str
    source_event_id: str
    correlation_cycle_id: str
    stream_key: MarketStreamKey
    market_snapshot_id: str
    decision_pipeline_run_id: str
    signal_id: str
    signal_direction: SignalDirection
    signal_reason_code: str
    intent_id: str | None
    market_snapshot: MarketSnapshot
    decision_result: DecisionPipelineResult
    shadow_enabled: bool
    shadow_status: DecisionConsumerStatus
    shadow_record: ShadowDecisionRecord | None
    shadow_error_category: str | None
    shadow_error_message: str | None
    paper_enabled: bool
    paper_status: DecisionConsumerStatus
    paper_cycle_result: PaperTradingCycleResult | None
    paper_error_category: str | None
    paper_error_message: str | None
    completed_stages: tuple[TradingDecisionCoordinationStage, ...]
    snapshot_duration_ms: int
    decision_duration_ms: int
    shadow_duration_ms: int
    paper_duration_ms: int
    total_duration_ms: int
    metadata: ImmutableMapping = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        for name in (
            "coordination_id",
            "source_event_id",
            "correlation_cycle_id",
            "market_snapshot_id",
            "decision_pipeline_run_id",
            "signal_id",
            "signal_reason_code",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.stream_key, MarketStreamKey):
            raise TypeError("stream_key must be a MarketStreamKey")
        if (
            self.market_snapshot.snapshot_id != self.market_snapshot_id
            or self.market_snapshot.stream_key != self.stream_key
            or self.market_snapshot.source_event_id != self.source_event_id
            or self.market_snapshot.correlation_cycle_id != self.correlation_cycle_id
        ):
            raise ValueError("market snapshot identity mismatch")
        result = self.decision_result
        if result.pipeline_run_id != self.decision_pipeline_run_id:
            raise ValueError("pipeline run ID mismatch")
        if result.snapshot_id != self.market_snapshot_id or result.stream_key != self.stream_key:
            raise ValueError("decision result does not match market snapshot")
        if (
            result.signal.signal_id != self.signal_id
            or result.signal.direction is not self.signal_direction
        ):
            raise ValueError("signal identity fields mismatch")
        if result.signal.reason_code != self.signal_reason_code:
            raise ValueError("signal reason mismatch")
        actual_intent_id = result.order_intent.intent_id if result.order_intent else None
        if actual_intent_id != self.intent_id:
            raise ValueError("intent ID mismatch")
        self._validate_consumer("shadow")
        self._validate_consumer("paper")
        if len(set(self.completed_stages)) != len(self.completed_stages):
            raise ValueError("completed_stages must be unique")
        if tuple(sorted(self.completed_stages, key=_ORDER.get)) != self.completed_stages:
            raise ValueError("completed_stages are out of order")
        if (
            not self.completed_stages
            or self.completed_stages[-1] is not TradingDecisionCoordinationStage.COMPLETED
        ):
            raise ValueError("completed result requires COMPLETED stage")
        expected_stages = [
            TradingDecisionCoordinationStage.EVENT_RECEIVED,
            TradingDecisionCoordinationStage.EVENT_VALIDATED,
            TradingDecisionCoordinationStage.CACHE_READ,
            TradingDecisionCoordinationStage.SNAPSHOT_CREATED,
            TradingDecisionCoordinationStage.DECISION_EVALUATED,
        ]
        expected_stages.extend(self._consumer_stages("shadow"))
        expected_stages.extend(self._consumer_stages("paper"))
        expected_stages.append(TradingDecisionCoordinationStage.COMPLETED)
        if self.completed_stages != tuple(expected_stages):
            raise ValueError("completed_stages do not match consumer statuses")
        for name in (
            "snapshot_duration_ms",
            "decision_duration_ms",
            "shadow_duration_ms",
            "paper_duration_ms",
            "total_duration_ms",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))

    def _consumer_stages(self, prefix: str) -> tuple[TradingDecisionCoordinationStage, ...]:
        status = getattr(self, f"{prefix}_status")
        mapping = {
            ("shadow", DecisionConsumerStatus.DELIVERED): (
                TradingDecisionCoordinationStage.SHADOW_DELIVERED,
            ),
            ("shadow", DecisionConsumerStatus.FAILED): (
                TradingDecisionCoordinationStage.SHADOW_FAILED,
            ),
            ("paper", DecisionConsumerStatus.DELIVERED): (
                TradingDecisionCoordinationStage.PAPER_DELIVERED,
            ),
            ("paper", DecisionConsumerStatus.FAILED): (
                TradingDecisionCoordinationStage.PAPER_FAILED,
            ),
            ("paper", DecisionConsumerStatus.UNAVAILABLE): (
                TradingDecisionCoordinationStage.PAPER_UNAVAILABLE,
            ),
        }
        return mapping.get((prefix, status), ())

    def _validate_consumer(self, prefix: str) -> None:
        enabled = getattr(self, f"{prefix}_enabled")
        status = getattr(self, f"{prefix}_status")
        result = getattr(self, "shadow_record" if prefix == "shadow" else "paper_cycle_result")
        category = getattr(self, f"{prefix}_error_category")
        message = getattr(self, f"{prefix}_error_message")
        if not enabled:
            if (
                status is not DecisionConsumerStatus.DISABLED
                or result is not None
                or category
                or message
            ):
                raise ValueError(f"disabled {prefix} consumer has delivery data")
            return
        if status is DecisionConsumerStatus.DELIVERED:
            if result is None or category or message:
                raise ValueError(f"delivered {prefix} consumer requires only its result")
            self._validate_delivered_result(prefix, result)
        elif status is DecisionConsumerStatus.FAILED:
            if result is not None or not category or not message:
                raise ValueError(f"failed {prefix} consumer requires error details")
        elif status is DecisionConsumerStatus.UNAVAILABLE:
            if prefix != "paper" or result is not None:
                raise ValueError("only paper may be unavailable without a result")
        else:
            raise ValueError(f"invalid terminal {prefix} consumer status")

    def _validate_delivered_result(self, prefix: str, result: object) -> None:
        if prefix == "shadow":
            expected = {
                "source_event_id": self.source_event_id,
                "correlation_cycle_id": self.correlation_cycle_id,
                "stream_key": self.stream_key,
                "snapshot_id": self.market_snapshot_id,
                "pipeline_run_id": self.decision_pipeline_run_id,
                "signal_id": self.signal_id,
                "intent_id": self.intent_id,
            }
        else:
            expected = {
                "decision_result": self.decision_result,
                "market_snapshot_id": self.market_snapshot_id,
                "decision_pipeline_run_id": self.decision_pipeline_run_id,
                "signal_id": self.signal_id,
                "intent_id": self.intent_id,
                "correlation_id": self.correlation_cycle_id,
            }
        if any(getattr(result, name, object()) != value for name, value in expected.items()):
            raise ValueError(f"delivered {prefix} result identity mismatch")


@dataclass(frozen=True, slots=True)
class TradingDecisionCoordinatorStatus:
    lifecycle: TradingDecisionCoordinatorLifecycle
    handled_events: int = 0
    successful_decisions: int = 0
    hold_decisions: int = 0
    intent_decisions: int = 0
    planner_declined_decisions: int = 0
    failed_decisions: int = 0
    shadow_deliveries: int = 0
    shadow_failures: int = 0
    paper_deliveries: int = 0
    paper_failures: int = 0
    paper_unavailable: int = 0
    ignored_duplicates: int = 0
    last_event_id: str | None = None
    last_coordination_id: str | None = None
    last_snapshot_id: str | None = None
    last_pipeline_run_id: str | None = None
    last_signal_direction: SignalDirection | None = None
    last_intent_id: str | None = None
    last_shadow_status: DecisionConsumerStatus | None = None
    last_paper_status: DecisionConsumerStatus | None = None
    last_paper_outcome: str | None = None
    last_error_category: str | None = None
    last_error_message: str | None = None
