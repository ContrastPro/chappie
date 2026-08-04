import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.main as main_module
from app.core.config import Settings
from app.core.logger import configure_logging, shutdown_logging

RUN_ID = "test-run-0001"


class FixedRunIdFactory:
    def create(self) -> str:
        return RUN_ID


@pytest.fixture(autouse=True)
def clean_root_logging() -> None:
    shutdown_logging()
    yield
    shutdown_logging()


def configure_in(tmp_path: Path, *, run_id: str = RUN_ID) -> tuple[Path, Path]:
    log_directory = tmp_path / "nested" / "logs"
    run_log_file = tmp_path / "current" / "latest_run.log"
    configure_logging("INFO", log_directory, run_log_file, run_id)
    return log_directory / "chappie.log", run_log_file


def flush_handlers() -> None:
    for handler in logging.getLogger().handlers:
        handler.flush()


def test_logging_creates_directories_and_writes_utf8_run_log(tmp_path: Path) -> None:
    history_file, run_file = configure_in(tmp_path)

    logging.getLogger("test").info("Безопасный запуск")
    flush_handlers()

    assert history_file.is_file()
    assert run_file.is_file()
    assert "Безопасный запуск" in run_file.read_text(encoding="utf-8")
    assert f"run_id={RUN_ID}" in run_file.read_text(encoding="utf-8")
    assert all(
        f"run_id={RUN_ID}" in line for line in run_file.read_text(encoding="utf-8").splitlines()
    )


def test_reconfigure_overwrites_run_log_but_appends_history(tmp_path: Path) -> None:
    history_file, run_file = configure_in(tmp_path, run_id="first-run")
    logging.getLogger("test").info("first message")
    flush_handlers()

    configure_in(tmp_path, run_id="second-run")
    logging.getLogger("test").info("second message")
    flush_handlers()

    current = run_file.read_text(encoding="utf-8")
    history = history_file.read_text(encoding="utf-8")
    assert "first message" not in current
    assert "second message" in current
    assert history.count("first message") == 1
    assert history.count("second message") == 1
    assert len(logging.getLogger().handlers) == 3


def test_console_filters_verbose_events_but_keeps_warnings(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, run_file = configure_in(tmp_path)

    logging.getLogger("MarketEvents").info("CandleInserted open_time_ms=1")
    logging.getLogger("httpx").info("HTTP Request: GET safe-public-url")
    logging.getLogger("MarketEvents").warning("MarketGapDetected")
    logging.getLogger("MarketEvents").error("MarketStreamFailed")
    logging.getLogger("PollingEngine").info("Polling cycle completed")
    flush_handlers()

    console = capsys.readouterr().err
    current = run_file.read_text(encoding="utf-8")
    assert "CandleInserted" not in console
    assert "HTTP Request" not in console
    assert "MarketGapDetected" in console
    assert "MarketStreamFailed" in console
    assert "Polling cycle completed" in console
    assert "CandleInserted" in current
    assert "HTTP Request" in current


def test_main_writes_complete_header_footer_and_flushes_normal_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,
        log_directory=tmp_path / "logs",
        run_log_file=tmp_path / "run" / "latest.log",
        smoke_run_cycles=3,
    )
    application = SimpleNamespace(
        engine=SimpleNamespace(
            status=SimpleNamespace(lifecycle_state=SimpleNamespace(value="stopped"))
        )
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module, "build_application", lambda value: application)

    def complete_run(app: object, logger: logging.Logger) -> int:
        del app
        logger.info("Stage 1 status lifecycle=stopped completed=3")
        return 0

    monkeypatch.setattr(main_module, "run_application", complete_run)

    assert main_module.main(FixedRunIdFactory()) == 0

    current = settings.run_log_file.read_text(encoding="utf-8")
    assert "Run started:" in current
    assert "application=Chappie Trader" in current
    assert "smoke_run_cycles=3" in current
    assert f"run_id={RUN_ID}" in current
    assert "Stage 1 status lifecycle=stopped completed=3" in current
    assert "Run finished:" in current
    assert "exit_code=0 lifecycle=stopped" in current


def test_main_records_fatal_composition_traceback_and_footer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,
        log_directory=tmp_path / "logs",
        run_log_file=tmp_path / "latest.log",
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)

    def fail_build(value: Settings) -> None:
        del value
        raise RuntimeError("unexpected composition failure")

    monkeypatch.setattr(main_module, "build_application", fail_build)

    assert main_module.main(FixedRunIdFactory()) == 1

    current = settings.run_log_file.read_text(encoding="utf-8")
    assert "Traceback" in current
    assert "RuntimeError: unexpected composition failure" in current
    assert "exit_code=1 lifecycle=COMPOSITION_FAILED" in current
