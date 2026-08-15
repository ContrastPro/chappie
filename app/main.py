import logging
import signal
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from inspect import signature
from typing import Protocol

from pydantic import ValidationError

from app.application import ChappieTraderApplication, build_application, run_application
from app.core.config import get_settings
from app.core.logger import UUIDRunIdFactory, configure_logging, shutdown_logging
from app.server_status import (
    AtomicServerStatusPublisher,
    ServerProcessState,
    ServerStatus,
)


class RunIdFactory(Protocol):
    def create(self) -> str:
        """Return the identifier for one application invocation."""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _install_shutdown_handlers(
    application: ChappieTraderApplication, logger: logging.Logger
) -> Callable[[], None]:
    """Request orderly shutdown on terminal signals from the main thread."""
    if threading.current_thread() is not threading.main_thread():
        return lambda: None
    previous = {value: signal.getsignal(value) for value in (signal.SIGINT, signal.SIGTERM)}

    def request_shutdown(signum: int, _frame: object) -> None:
        logger.info("Shutdown signal received signal=%s", signum)
        application.request_shutdown()

    for value in previous:
        signal.signal(value, request_shutdown)

    def restore() -> None:
        for value, handler in previous.items():
            signal.signal(value, handler)

    return restore


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
    status_publisher = AtomicServerStatusPublisher(settings.server_status_file)
    application: ChappieTraderApplication | None = None
    exit_code = 1
    lifecycle = "NOT_STARTED"
    try:
        try:
            status_publisher.publish(
                ServerStatus(
                    mode=settings.trading_mode,
                    process_state=ServerProcessState.STARTING,
                    schema_version=None,
                    market_polling_state="STARTING",
                    last_successful_market_cycle_time_ms=None,
                    reconciliation_status=None,
                    reconciliation_time_ms=None,
                    settlement_status=None,
                    submission_enabled=False,
                    safe_stop_reason=None,
                    updated_at_utc=_utc_now(),
                )
            )
        except Exception:
            logger.exception("Could not publish server STARTING status")
            return 2
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
        logger.info(
            "Shadow decisions enabled=%s strategy=ema_crossover/1.0.0 "
            "ema_fast=%s ema_slow=%s planner=long_only_target_exposure/1.0.0 "
            "long_target_exposure=%s history_limit=%s dedup_capacity=%s",
            settings.shadow_decisions_enabled,
            settings.ema_fast_window,
            settings.ema_slow_window,
            settings.long_target_exposure,
            settings.shadow_history_limit,
            settings.shadow_dedup_capacity,
        )
        try:
            application = build_application(settings)
        except Exception:
            lifecycle = "COMPOSITION_FAILED"
            logger.exception("Application composition failed")
            try:
                status_publisher.publish(
                    # No application graph exists yet; retain only sanitized process facts.
                    ServerStatus(
                        mode=settings.trading_mode,
                        process_state=ServerProcessState.SAFE_STOP,
                        schema_version=None,
                        market_polling_state="STOPPED",
                        last_successful_market_cycle_time_ms=None,
                        reconciliation_status=None,
                        reconciliation_time_ms=None,
                        settlement_status=None,
                        submission_enabled=False,
                        safe_stop_reason="STARTUP_COMPOSITION_FAILED",
                        updated_at_utc=_utc_now(),
                    )
                )
            except Exception:
                logger.exception("Could not publish startup SAFE_STOP status")
        else:
            restore_handlers = _install_shutdown_handlers(application, logger)
            try:
                if "status_publisher" in signature(run_application).parameters:
                    exit_code = run_application(
                        application, logger, status_publisher=status_publisher
                    )
                else:  # Compatibility with deliberately minimal injected test runners.
                    exit_code = run_application(application, logger)
                lifecycle = application.engine.status.lifecycle_state.value
            finally:
                restore_handlers()
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
