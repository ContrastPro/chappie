import logging
from typing import Protocol, runtime_checkable

from app.paper.models import PaperTradingObservationStatus, PaperTradingRecord


@runtime_checkable
class PaperTradingObserver(Protocol):
    def on_cycle(self, record: PaperTradingRecord) -> None: ...


class LoggingPaperTradingObserver:
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger("PaperTrading")

    def on_cycle(self, record: PaperTradingRecord) -> None:
        message = (
            "Paper cycle cycle_id=%s pipeline_run_id=%s snapshot_id=%s outcome=%s "
            "signal=%s intent=%s risk=%s execution=%s fill=%s quantity=%s price=%s "
            "gross=%s fee=%s equity=%s drawdown=%s observation=%s correlation=%s"
        )
        values = (
            record.cycle_id,
            record.decision_pipeline_run_id,
            record.market_snapshot_id,
            record.cycle_outcome.value,
            record.signal_direction.value,
            record.intent_id,
            record.risk_outcome.value if record.risk_outcome else None,
            record.execution_status,
            record.fill_id,
            str(record.quantity) if record.quantity is not None else None,
            str(record.fill_price) if record.fill_price is not None else None,
            str(record.gross_notional) if record.gross_notional is not None else None,
            str(record.fee) if record.fee is not None else None,
            str(record.equity),
            str(record.drawdown),
            record.observation_status.value,
            record.correlation_id,
        )
        if record.observation_status is PaperTradingObservationStatus.OBSERVATION_FAILED:
            self._logger.warning(message, *values)
        else:
            self._logger.info(message, *values)
