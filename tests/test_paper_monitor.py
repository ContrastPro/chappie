"""Focused read-only Paper monitor tests."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.tools.paper_monitor import (
    _format_btc,
    _format_decimal,
    _format_duration,
    _trade_analytics,
    main,
    read_paper_monitor_snapshot,
    render_dashboard,
)


def _database(path: Path, *, with_fill: bool = False) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE schema_migrations (version INTEGER);
            CREATE TABLE portfolio_current (portfolio_version INTEGER, state_payload TEXT);
            CREATE TABLE fills (
                fill_id TEXT, order_id TEXT, side TEXT, quantity TEXT, price TEXT,
                gross_notional TEXT, fee TEXT, filled_at_ms INTEGER
            );
            INSERT INTO schema_migrations VALUES (1);
            """
        )
        payload = {
            "portfolio_id": "paper-btcusdt",
            "symbol": "BTCUSDT",
            "interval": "1m",
            "quote_balance": {"$decimal": "9000"},
            "base_balance": {"$decimal": "0.01"},
            "current_equity": {"$decimal": "10000"},
            "realised_pnl": {"$decimal": "1"},
            "unrealised_pnl": {"$decimal": "2"},
            "total_fees": {"$decimal": "3"},
            "turnover": {"$decimal": "1000"},
            "drawdown": {"$decimal": "4"},
            "trade_count": 1,
            "version": 7,
            "position": {
                "quantity": {"$decimal": "0.01"},
                "average_entry_price": {"$decimal": "100000"},
                "cost_basis": {"$decimal": "1001"},
            },
        }
        connection.execute("INSERT INTO portfolio_current VALUES (?, ?)", (7, json.dumps(payload)))
        if with_fill:
            connection.execute(
                "INSERT INTO fills VALUES (?,?,?,?,?,?,?,?)",
                (
                    "fill-identifier",
                    "order-identifier",
                    "BUY",
                    "0.01",
                    "100000",
                    "1000",
                    "1",
                    1_000,
                ),
            )
        connection.commit()
    finally:
        connection.close()


def _status(path: Path, **values: object) -> None:
    payload = {
        "process_state": "READY",
        "mode": "paper",
        "market_polling_state": "running",
        "last_successful_market_cycle_time_ms": 1_000,
        "safe_stop_reason": None,
        "schema_version": 1,
        "submission_enabled": False,
        "updated_at_utc": "2026-08-15T00:00:00+00:00",
    }
    payload.update(values)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_empty_fresh_database_and_missing_status_are_observable(tmp_path: Path) -> None:
    database = tmp_path / "paper.sqlite3"
    _database(database)

    snapshot = read_paper_monitor_snapshot(database, tmp_path / "missing.json")

    assert snapshot.database_status == "available"
    assert snapshot.fills == ()
    assert snapshot.status is None
    assert "no trades" in render_dashboard(snapshot)


def test_active_portfolio_and_trade_are_rendered(tmp_path: Path) -> None:
    database = tmp_path / "paper.sqlite3"
    status = tmp_path / "status.json"
    _database(database, with_fill=True)
    _status(status, process_state="STOPPED")

    rendered = render_dashboard(
        read_paper_monitor_snapshot(database, status),
        now=datetime(2026, 8, 15, tzinfo=UTC),
    )

    assert "Equity                          10,000.00" in rendered
    assert "BUY" in rendered
    assert "fill-ide/order-id" in rendered
    assert "Polling state                   running" in rendered


def test_active_runtime_prefers_live_status_when_database_is_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "paper.sqlite3"
    status = tmp_path / "status.json"
    _status(
        status,
        portfolio={
            "equity": "10000",
            "quote_balance": "9000",
            "quote_asset": "USDT",
            "base_balance": "0.01",
            "base_asset": "BTC",
            "fees": "2",
            "turnover": "1000",
            "trade_count": 1,
            "portfolio_version": 7,
        },
        market={
            "symbol": "BTCUSDT",
            "interval": "1m",
            "latest_price": "100000",
            "polling_state": "RUNNING",
        },
        strategy={"last_signal": "hold", "reason": "NO_CROSSOVER"},
    )
    database.touch()

    def locked(*_args: object, **_kwargs: object) -> sqlite3.Connection:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(sqlite3, "connect", locked)
    snapshot = read_paper_monitor_snapshot(database, status)
    rendered = render_dashboard(snapshot)

    assert snapshot.database_status == "owned by active runtime / live snapshot mode"
    assert "10,000.00" in rendered and "BTCUSDT" in rendered and "NO_CROSSOVER" in rendered


def test_compact_decimal_btc_and_duration_formatting_are_terminal_safe() -> None:
    assert _format_decimal("1234.567") == "1,234.57"
    assert _format_decimal("12.3456", suffix="%") == "12.35%"
    assert _format_btc("0.00016000") == "0.00016"
    assert _format_duration(65_000) == "01:05"
    assert _format_duration(3_661_000) == "01:01:01"


def test_completed_round_trip_analytics_only_uses_matched_paper_fills() -> None:
    fills = (
        {
            "fill_id": "buy",
            "order_id": "buy-order",
            "side": "BUY",
            "quantity": "1",
            "price": "100",
            "gross_notional": "100",
            "fee": "1",
            "filled_at_ms": 1_000,
        },
        {
            "fill_id": "sell",
            "order_id": "sell-order",
            "side": "SELL",
            "quantity": "1",
            "price": "110",
            "gross_notional": "110",
            "fee": "1",
            "filled_at_ms": 61_000,
        },
    )

    analytics = _trade_analytics(
        fills, {"equity": "1000", "initial_equity": "1000", "fees": "2", "turnover": "210"}
    )

    assert analytics["buy_count"] == analytics["sell_count"] == analytics["completed"] == 1
    assert analytics["wins"] == 1 and analytics["losses"] == 0
    assert analytics["net_realised"] == 8
    assert analytics["average_hold_ms"] == 60_000
    assert analytics["fee_drag"] == Decimal("0.2")


def test_open_position_is_not_reported_as_a_completed_round_trip() -> None:
    analytics = _trade_analytics(
        (
            {
                "fill_id": "buy",
                "order_id": "buy-order",
                "side": "BUY",
                "quantity": "1",
                "price": "100",
                "gross_notional": "100",
                "fee": "1",
                "filled_at_ms": 1_000,
            },
        ),
        {"equity": "1000"},
    )

    assert analytics["completed"] == 0
    assert analytics["average_net"] is None


def test_live_snapshot_uses_bounded_sanitized_recent_trades(tmp_path: Path) -> None:
    status = tmp_path / "status.json"
    _status(
        status,
        trading={
            "recent_trades": [
                {
                    "fill_id": "fill-12345678",
                    "order_id": "order-12345678",
                    "side": "SELL",
                    "quantity": "0.00016",
                    "price": "63000",
                    "gross_notional": "10.08",
                    "fee": "0.01",
                    "filled_at_ms": 1_000,
                }
            ]
        },
    )

    snapshot = read_paper_monitor_snapshot(tmp_path / "missing.sqlite3", status)

    assert len(snapshot.fills) == 1
    assert "SELL" in render_dashboard(snapshot)


def test_stopped_runtime_returns_unavailable_when_database_is_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "paper.sqlite3"
    database.touch()
    status = tmp_path / "status.json"
    _status(status, process_state="STOPPED")
    monkeypatch.setattr(
        sqlite3,
        "connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("database is locked")
        ),
    )

    snapshot = read_paper_monitor_snapshot(database, status)

    assert snapshot.database_status == "unavailable: OperationalError"


def test_safe_stop_and_absent_database_do_not_crash(tmp_path: Path) -> None:
    status = tmp_path / "status.json"
    _status(status, process_state="SAFE_STOP", safe_stop_reason="operator stop")

    rendered = render_dashboard(read_paper_monitor_snapshot(tmp_path / "missing.sqlite3", status))

    assert "SAFE_STOP" in rendered
    assert "operator stop" in rendered
    assert "no database" in rendered


def test_stale_status_is_visible_without_terminal_specific_features(tmp_path: Path) -> None:
    status = tmp_path / "status.json"
    _status(status, updated_at_utc="2026-08-15T00:00:00+00:00")

    rendered = render_dashboard(
        read_paper_monitor_snapshot(tmp_path / "missing.sqlite3", status),
        now=datetime(2026, 8, 15, 0, 3, tzinfo=UTC),
    )

    assert "STATUS STALE (03:00)" in rendered


def test_monitor_opens_database_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = tmp_path / "paper.sqlite3"
    _database(database)
    original_connect = sqlite3.connect
    calls: list[tuple[str, bool]] = []

    def connect(path: str, *args: object, **kwargs: object) -> sqlite3.Connection:
        calls.append((path, bool(kwargs.get("uri"))))
        return original_connect(path, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    read_paper_monitor_snapshot(database, tmp_path / "missing.json")

    assert calls and "mode=ro" in calls[0][0] and calls[0][1]


def test_once_and_interval_validation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "app.tools.paper_monitor.get_settings",
        lambda: type(
            "Settings",
            (),
            {"sqlite_path": Path("missing.sqlite3"), "server_status_file": Path("missing.json")},
        )(),
    )

    assert main(["--once"]) == 0
    assert "Chappie Paper Monitor" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["--interval", "0"])
