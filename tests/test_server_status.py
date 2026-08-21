"""Focused MVP server-bootstrap status contract tests."""

from __future__ import annotations

import json
import logging
import os
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.foundation.config import Settings
from app.market.models import CandleSnapshot
from app.market.polling import PollingLifecycleState
from app.runtime.application import StageOneStatusReport, run_application
from app.runtime.status import (
    AtomicServerStatusPublisher,
    ServerProcessState,
    ServerStatus,
    ServerStatusWriteError,
    status_for_application,
)


def _report(**changes: object) -> StageOneStatusReport:
    values: dict[str, object] = {
        "lifecycle_state": PollingLifecycleState.STOPPED,
        "completed_cycles": 0,
        "successful_cycles": 0,
        "failed_cycles": 0,
        "consecutive_failures": 0,
        "last_success_time_ms": None,
        "last_error_category": None,
        "cached_candle_count": 0,
        "latest_candle": None,
        "latest_closed_candle": None,
        "published_event_count": 0,
        "delivery_failure_count": 0,
        "last_exchange_time_ms": None,
        "last_clock_skew_ms": None,
    }
    values.update(changes)
    return StageOneStatusReport(**values)


def test_default_testnet_database_is_dedicated_to_active_mvp() -> None:
    settings = Settings(_env_file=None)

    assert settings.active_testnet_sqlite_path == Path("data/testnet_mvp.sqlite3")
    assert settings.active_testnet_sqlite_path != Path("data/m4_10_testnet_smoke.sqlite3")
    assert settings.sqlite_path == Path("data/chappie-paper.sqlite3")


def test_explicit_sqlite_path_remains_the_operator_testnet_override(tmp_path: Path) -> None:
    path = tmp_path / "testnet.sqlite3"
    settings = Settings(_env_file=None, sqlite_path=path)

    assert settings.active_testnet_sqlite_path == path


def test_historical_m410_database_is_rejected_for_active_testnet() -> None:
    with pytest.raises(ValueError, match="historical M4.10"):
        Settings(
            _env_file=None,
            trading_mode="testnet",
            testnet_execution_enabled=True,
            persistence_enabled=True,
            sqlite_path=Path("data/m4_10_testnet_smoke.sqlite3"),
            binance_testnet_api_key="test-key",
            binance_testnet_secret_key="test-secret",
            binance_testnet_account_fingerprint="test-account",
        )


def test_status_is_sanitized_and_atomically_published(tmp_path: Path) -> None:
    path = tmp_path / "status.json"
    publisher = AtomicServerStatusPublisher(path)
    status = ServerStatus(
        mode="testnet",
        process_state=ServerProcessState.SAFE_STOP,
        schema_version=4,
        market_polling_state="STOPPED",
        last_successful_market_cycle_time_ms=123,
        reconciliation_status="SAFE",
        reconciliation_time_ms=124,
        settlement_status="SYNCHRONIZED",
        submission_enabled=False,
        safe_stop_reason="TESTNET_STARTUP_BLOCKED:KILL_SWITCH_ACTIVE",
        updated_at_utc="2026-08-15T00:00:00+00:00",
    )

    publisher.publish(status)

    assert json.loads(path.read_text()) == {
        "last_successful_market_cycle_time_ms": 123,
        "market_polling_state": "STOPPED",
        "mode": "testnet",
        "process_state": "SAFE_STOP",
        "reconciliation_status": "SAFE",
        "reconciliation_time_ms": 124,
        "safe_stop_reason": "TESTNET_STARTUP_BLOCKED:KILL_SWITCH_ACTIVE",
        "schema_version": 4,
        "settlement_status": "SYNCHRONIZED",
        "portfolio": None,
        "market": None,
        "strategy": None,
        "trading": None,
        "submission_enabled": False,
        "updated_at_utc": "2026-08-15T00:00:00+00:00",
    }
    assert not list(tmp_path.glob(".status.json.*"))


def test_status_write_failure_is_explicit_and_leaves_no_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status = ServerStatus(
        mode="paper",
        process_state=ServerProcessState.READY,
        schema_version=None,
        market_polling_state="STARTING",
        last_successful_market_cycle_time_ms=None,
        reconciliation_status=None,
        reconciliation_time_ms=None,
        settlement_status=None,
        submission_enabled=False,
        safe_stop_reason=None,
        updated_at_utc="2026-08-15T00:00:00+00:00",
    )
    publisher = AtomicServerStatusPublisher(tmp_path / "status.json")
    monkeypatch.setattr(os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("blocked")))
    with pytest.raises(ServerStatusWriteError):
        publisher.publish(status)

    assert not publisher.path.exists()


def test_status_for_blocked_testnet_disables_submission() -> None:
    application = SimpleNamespace(
        startup_safe_stop_reason=None,
        settings=SimpleNamespace(trading_mode="testnet"),
        status_report=lambda: _report(
            persistence_schema_version=4,
            testnet_submission_capability_present=True,
            testnet_reconciliation_status="SAFE",
            testnet_settlement_status="SYNCHRONIZED",
        ),
    )

    status = status_for_application(
        application,
        ServerProcessState.SAFE_STOP,
        safe_stop_reason="STARTUP_CHECK_FAILED",
    )

    assert not status.submission_enabled
    assert status.safe_stop_reason == "STARTUP_CHECK_FAILED"


def test_ready_status_contains_sanitized_live_paper_observability() -> None:
    application = SimpleNamespace(
        startup_safe_stop_reason=None,
        settings=SimpleNamespace(
            trading_mode="paper", default_symbol="BTCUSDT", default_interval="1m"
        ),
        status_report=lambda: _report(
            lifecycle_state=PollingLifecycleState.RUNNING,
            cached_candle_count=42,
            successful_cycles=9,
            failed_cycles=1,
            last_success_time_ms=10_000,
            latest_candle=CandleSnapshot(
                1, 2, Decimal(100), Decimal(102), Decimal(99), Decimal(101), Decimal(3)
            ),
            latest_closed_candle=CandleSnapshot(
                0, 1, Decimal(98), Decimal(100), Decimal(97), Decimal(99), Decimal(4)
            ),
            paper_enabled=True,
            paper_quote_asset="USDT",
            paper_quote_balance=Decimal(9000),
            paper_base_asset="BTC",
            paper_base_balance=Decimal("0.01"),
            paper_equity=Decimal(10010),
            paper_total_fees=Decimal(2),
            paper_turnover=Decimal(1000),
            paper_trade_count=1,
            paper_portfolio_version=7,
            shadow_last_signal_direction="hold",
            shadow_last_intent_id="intent-1",
        ),
    )

    status = status_for_application(application, ServerProcessState.READY)
    encoded = json.dumps(
        status.__dict__
        if hasattr(status, "__dict__")
        else {name: getattr(status, name) for name in status.__dataclass_fields__}
    )

    assert status.portfolio is not None
    assert status.portfolio["equity"] == "10010"
    assert status.market == {
        "symbol": "BTCUSDT",
        "interval": "1m",
        "latest_price": "101",
        "latest_open_price": "100",
        "latest_closed_price": "99",
        "cached_candle_count": 42,
        "polling_state": "running",
        "last_successful_market_cycle_time_ms": 10_000,
        "successful_cycles": 9,
        "failed_cycles": 1,
    }
    assert status.strategy is not None and status.strategy["last_signal"] == "hold"
    assert "secret" not in encoded.lower() and "api_key" not in encoded.lower()


def test_startup_safe_stop_does_not_start_market_polling_or_submission() -> None:
    published: list[ServerStatus] = []

    class Agent:
        started = False

        def start(self) -> None:
            self.started = True

        def stop(self) -> None:
            return None

    agent = Agent()
    application = SimpleNamespace(
        startup_safe_stop_reason="TESTNET_STARTUP_BLOCKED:KILL_SWITCH_ACTIVE",
        agent=agent,
        engine=SimpleNamespace(
            status=SimpleNamespace(lifecycle_state=PollingLifecycleState.STOPPED)
        ),
        request_shutdown=lambda: None,
        close=lambda: None,
        settings=SimpleNamespace(trading_mode="testnet"),
        status_report=lambda: _report(
            persistence_schema_version=4,
            testnet_reconciliation_status="SAFE",
            testnet_settlement_status="SYNCHRONIZED",
        ),
    )
    publisher = SimpleNamespace(publish=published.append)

    assert (
        run_application(application, logging.getLogger(__name__), status_publisher=publisher) == 1
    )
    assert not agent.started
    assert [value.process_state for value in published] == [
        ServerProcessState.SAFE_STOP,
        ServerProcessState.STOPPED,
    ]
    assert not published[0].submission_enabled
    assert published[1].safe_stop_reason == "TESTNET_STARTUP_BLOCKED:KILL_SWITCH_ACTIVE"


def test_running_polling_cycle_publishes_current_operational_status() -> None:
    published: list[ServerStatus] = []

    class Engine:
        def __init__(self) -> None:
            self.status = SimpleNamespace(
                lifecycle_state=PollingLifecycleState.RUNNING,
                failed_cycles=0,
            )
            self.observer = None

        def set_cycle_observer(self, observer) -> None:
            self.observer = observer

    engine = Engine()
    application = SimpleNamespace(
        startup_safe_stop_reason=None,
        engine=engine,
        agent=SimpleNamespace(start=lambda: engine.observer()),
        request_shutdown=lambda: None,
        close=lambda: None,
        settings=SimpleNamespace(trading_mode="paper"),
        status_report=lambda: _report(
            lifecycle_state=PollingLifecycleState.RUNNING,
            last_success_time_ms=456,
        ),
    )

    publisher = SimpleNamespace(publish=published.append)

    assert (
        run_application(application, logging.getLogger(__name__), status_publisher=publisher) == 0
    )
    assert published[1].process_state is ServerProcessState.READY
    assert published[1].market_polling_state == "running"
    assert published[1].last_successful_market_cycle_time_ms == 456
