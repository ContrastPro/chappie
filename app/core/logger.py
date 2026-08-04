import logging
from pathlib import Path
from uuid import uuid4

LOG_FORMAT = (
    "%(asctime)s | %(levelname)s | %(name)s | "
    "run_id=%(run_id)s | %(message)s"
)


class UUIDRunIdFactory:
    """Create a unique identifier for one application invocation."""

    def create(self) -> str:
        return str(uuid4())


class _RunIdFilter(logging.Filter):
    def __init__(self, run_id: str) -> None:
        super().__init__()
        self._run_id = run_id

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = self._run_id
        return True


class _ConsoleFilter(logging.Filter):
    """Keep operational INFO output while retaining every warning and error."""

    _verbose_info_loggers = ("MarketEvents", "httpx", "httpcore")

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        return not record.name.startswith(self._verbose_info_loggers)


def configure_logging(
    log_level: str,
    log_directory: Path,
    run_log_file: Path,
    run_id: str,
) -> None:
    """Configure concise console, append history and overwrite per-run logs."""

    log_directory.mkdir(parents=True, exist_ok=True)
    run_log_file.parent.mkdir(parents=True, exist_ok=True)

    level = getattr(logging, log_level.upper(), logging.INFO)
    formatter = logging.Formatter(LOG_FORMAT)
    run_id_filter = _RunIdFilter(run_id)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(run_id_filter)
    console_handler.addFilter(_ConsoleFilter())

    history_handler = logging.FileHandler(
        log_directory / "chappie.log",
        mode="a",
        encoding="utf-8",
    )
    history_handler.setLevel(level)
    history_handler.setFormatter(formatter)
    history_handler.addFilter(run_id_filter)

    run_handler = logging.FileHandler(
        run_log_file,
        mode="w",
        encoding="utf-8",
    )
    run_handler.setLevel(level)
    run_handler.setFormatter(formatter)
    run_handler.addFilter(run_id_filter)

    logging.basicConfig(
        level=level,
        handlers=[console_handler, history_handler, run_handler],
        force=True,
    )


def shutdown_logging() -> None:
    """Flush, close and detach application-owned root handlers."""

    root_logger = logging.getLogger()
    for handler in root_logger.handlers[:]:
        try:
            try:
                handler.flush()
            except (OSError, ValueError):
                # A test capture stream may already be closed by its owner.
                pass
        finally:
            handler.close()
            root_logger.removeHandler(handler)
