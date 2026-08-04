import logging
from typing import Protocol, runtime_checkable

from app.shadow.models import ShadowDecisionRecord, ShadowDecisionStatus


@runtime_checkable
class ShadowDecisionObserver(Protocol):
    def on_decision(self, record: ShadowDecisionRecord) -> None: ...


class LoggingShadowDecisionObserver:
    """Log one compact record without market history, payloads or secrets."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger("ShadowDecisions")

    def on_decision(self, record: ShadowDecisionRecord) -> None:
        message = (
            "Shadow decision shadow_run_id=%s source_event_id=%s correlation_cycle_id=%s "
            "symbol=%s interval=%s snapshot_id=%s pipeline_run_id=%s direction=%s "
            "signal_reason=%s intent_action=%s target_exposure=%s strategy=%s/%s "
            "planner=%s/%s outcome=%s decision_status=%s observation_status=%s "
            "failure_category=%s evaluation_duration_ms=%s"
        )
        args = (
            record.shadow_run_id,
            record.source_event_id,
            record.correlation_cycle_id,
            record.stream_key.symbol,
            record.stream_key.interval,
            record.snapshot_id,
            record.pipeline_run_id,
            record.signal_direction.value if record.signal_direction else None,
            record.signal_reason_code,
            record.intent_action.value if record.intent_action else None,
            record.target_exposure,
            record.strategy_name,
            record.strategy_version,
            record.planner_name,
            record.planner_version,
            record.pipeline_outcome.value if record.pipeline_outcome else None,
            record.decision_status.value,
            record.observation_status.value,
            record.failure_category.value if record.failure_category else None,
            record.evaluation_duration_ms,
        )
        if record.decision_status is ShadowDecisionStatus.FAILED:
            self._logger.warning(message, *args)
        else:
            self._logger.info(message, *args)
