import logging

from app.shadow.models import ShadowDecisionStatus
from app.shadow.observer import LoggingShadowDecisionObserver
from tests.test_shadow_models import successful


def test_logging_observer_writes_compact_success_without_metadata_or_history(caplog) -> None:
    record = successful(metadata={"secret": "must-not-appear"})
    with caplog.at_level(logging.INFO, logger="shadow-test"):
        LoggingShadowDecisionObserver(logging.getLogger("shadow-test")).on_decision(record)
    message = caplog.messages[-1]
    assert "source_event_id=event" in message
    assert "decision_status=succeeded" in message
    assert "must-not-appear" not in message
    assert "closed_candles" not in message


def test_logging_observer_uses_warning_for_failed_decision(caplog) -> None:
    from app.shadow.models import ShadowFailureCategory, ShadowObservationStatus

    record = successful(
        decision_status=ShadowDecisionStatus.FAILED,
        pipeline_run_id=None,
        signal_id=None,
        signal_direction=None,
        signal_reason_code=None,
        pipeline_outcome=None,
        strategy_name=None,
        strategy_version=None,
        planner_name=None,
        planner_version=None,
        observation_status=ShadowObservationStatus.OBSERVED,
        failure_category=ShadowFailureCategory.CACHE_READ,
        failure_message="cache unavailable",
    )
    with caplog.at_level(logging.WARNING, logger="shadow-test"):
        LoggingShadowDecisionObserver(logging.getLogger("shadow-test")).on_decision(record)
    assert caplog.records[-1].levelno == logging.WARNING
