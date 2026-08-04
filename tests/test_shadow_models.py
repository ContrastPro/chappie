from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from app.market.cache import MarketStreamKey
from app.shadow.exceptions import InvalidShadowDecisionRecordError
from app.shadow.models import (
    ShadowDecisionRecord,
    ShadowDecisionStatus,
    ShadowFailureCategory,
    ShadowObservationStatus,
)
from app.trading.models import SignalDirection
from app.trading.pipeline import PipelineOutcome


def successful(**changes):
    values = {
        "shadow_run_id": "run",
        "source_event_id": "event",
        "correlation_cycle_id": "cycle",
        "stream_key": MarketStreamKey("BTCUSDT", "1m"),
        "evaluated_at_ms": 10,
        "snapshot_id": "snapshot",
        "pipeline_run_id": "pipeline",
        "signal_id": "signal",
        "signal_direction": SignalDirection.HOLD,
        "signal_reason_code": "no_crossover",
        "intent_id": None,
        "intent_action": None,
        "target_exposure": None,
        "pipeline_outcome": PipelineOutcome.HOLD,
        "strategy_name": "ema",
        "strategy_version": "1",
        "planner_name": "planner",
        "planner_version": "1",
        "decision_status": ShadowDecisionStatus.SUCCEEDED,
        "observation_status": ShadowObservationStatus.OBSERVED,
        "evaluation_duration_ms": 0,
        "metadata": {"nested": ("safe",)},
    }
    values.update(changes)
    return ShadowDecisionRecord(**values)


def test_success_record_is_deeply_immutable() -> None:
    record = successful()
    with pytest.raises(TypeError):
        record.metadata["x"] = 1
    with pytest.raises(FrozenInstanceError):
        record.snapshot_id = "other"


@pytest.mark.parametrize(
    "changes",
    [
        {"pipeline_run_id": None},
        {"failure_category": ShadowFailureCategory.CACHE_READ},
        {"evaluation_duration_ms": -1},
        {"target_exposure": Decimal("NaN")},
    ],
)
def test_invalid_success_record(changes) -> None:
    with pytest.raises(InvalidShadowDecisionRecordError):
        successful(**changes)


def test_failed_record_requires_details() -> None:
    with pytest.raises(InvalidShadowDecisionRecordError):
        successful(decision_status=ShadowDecisionStatus.FAILED, pipeline_run_id=None)
