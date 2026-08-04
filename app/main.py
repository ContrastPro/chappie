import logging
from datetime import UTC, datetime
from typing import Protocol

from pydantic import ValidationError

from app.application import ChappieTraderApplication, build_application, run_application
from app.core.config import get_settings
from app.core.logger import UUIDRunIdFactory, configure_logging, shutdown_logging


class RunIdFactory(Protocol):
    def create(self) -> str:
        """Return the identifier for one application invocation."""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def main(run_id_factory: RunIdFactory | None = None) -> int:
    try:
        settings = get_settings()
    except ValidationError:
        logging.getLogger(__name__).exception("Invalid configuration")
        return 2

    run_id = (run_id_factory or UUIDRunIdFactory()).create()
    try:
        configure_logging(
            log_level=settings.log_level,
            log_directory=settings.log_directory,
            run_log_file=settings.run_log_file,
            run_id=run_id,
        )
    except Exception:
        logging.getLogger(__name__).exception("Could not configure logging")
        return 2

    logger = logging.getLogger(__name__)
    application: ChappieTraderApplication | None = None
    exit_code = 1
    lifecycle = "NOT_STARTED"
    try:
        logger.info(
            "Run started: application=%s version=%s started_at_utc=%s "
            "environment=%s trading_mode=%s exchange=%s symbol=%s interval=%s "
            "smoke_run_cycles=%s run_id=%s",
            settings.app_name,
            settings.app_version,
            _utc_now(),
            settings.environment,
            settings.trading_mode,
            settings.exchange_name,
            settings.default_symbol,
            settings.default_interval,
            settings.smoke_run_cycles,
            run_id,
        )
        try:
            application = build_application(settings)
        except Exception:
            lifecycle = "COMPOSITION_FAILED"
            logger.exception("Application composition failed")
        else:
            exit_code = run_application(application, logger)
            lifecycle = application.engine.status.lifecycle_state.value
        return exit_code
    finally:
        logger.info(
            "Run finished: run_id=%s finished_at_utc=%s exit_code=%s lifecycle=%s",
            run_id,
            _utc_now(),
            exit_code,
            lifecycle,
        )
        shutdown_logging()


if __name__ == "__main__":
    raise SystemExit(main())
