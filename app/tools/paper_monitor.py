"""Read-only terminal dashboard for the active local Paper runtime."""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.config import get_settings

NA = "N/A"


@dataclass(frozen=True, slots=True)
class PaperMonitorSnapshot:
    database_path: Path
    database_status: str
    schema_version: int | None
    portfolio: dict[str, Any] | None
    fills: tuple[dict[str, Any], ...]
    status: dict[str, Any] | None
    status_error: str | None


def _decimal(payload: dict[str, Any], name: str) -> str:
    value = payload.get(name)
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("$decimal"), str):
        return value["$decimal"]
    return NA


def _read_status(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Tolerate a status file being atomically replaced while it is read."""
    for _ in range(2):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None, None
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            return value, None
        return None, "invalid status JSON"
    return None, "status temporarily unavailable"


def read_paper_monitor_snapshot(
    database_path: Path, status_path: Path, *, fill_limit: int = 10
) -> PaperMonitorSnapshot:
    """Prefer the active process's atomic snapshot; enrich stopped runtimes from SQLite."""
    if fill_limit <= 0:
        raise ValueError("fill_limit must be positive")
    status, status_error = _read_status(status_path)
    if status is not None and status.get("process_state") == "READY":
        portfolio = status.get("portfolio")
        return PaperMonitorSnapshot(
            database_path,
            "owned by active runtime / live snapshot mode",
            status.get("schema_version") if isinstance(status.get("schema_version"), int) else None,
            portfolio if isinstance(portfolio, dict) else None,
            (),
            status,
            status_error,
        )
    if not database_path.is_file():
        return PaperMonitorSnapshot(
            database_path, "no database", None, None, (), status, status_error
        )
    try:
        connection = sqlite3.connect(f"file:{database_path.resolve().as_posix()}?mode=ro", uri=True)
        try:
            row = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
            schema_version = row[0] if row is not None else None
            row = connection.execute(
                "SELECT state_payload FROM portfolio_current ORDER BY portfolio_version DESC LIMIT 1"
            ).fetchone()
            portfolio = json.loads(row[0]) if row is not None else None
            fills = tuple(
                {
                    "fill_id": record[0],
                    "order_id": record[1],
                    "side": record[2],
                    "quantity": record[3],
                    "price": record[4],
                    "gross_notional": record[5],
                    "fee": record[6],
                    "filled_at_ms": record[7],
                }
                for record in connection.execute(
                    "SELECT fill_id,order_id,side,quantity,price,gross_notional,fee,filled_at_ms "
                    "FROM fills ORDER BY filled_at_ms DESC LIMIT ?",
                    (fill_limit,),
                )
            )
        finally:
            connection.close()
    except (sqlite3.Error, OSError, json.JSONDecodeError) as error:
        return PaperMonitorSnapshot(
            database_path,
            f"unavailable: {type(error).__name__}",
            None,
            None,
            (),
            status,
            status_error,
        )
    return PaperMonitorSnapshot(
        database_path, "available", schema_version, portfolio, fills, status, status_error
    )


def _utc_time(value: object) -> str:
    if not isinstance(value, int):
        return NA
    return datetime.fromtimestamp(value / 1000, UTC).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def _line(label: str, value: object) -> str:
    return f"  {label:<31} {value if value is not None else NA}"


def render_dashboard(snapshot: PaperMonitorSnapshot, *, now: datetime | None = None) -> str:
    now = now or datetime.now().astimezone()
    status = snapshot.status or {}
    portfolio = snapshot.portfolio or {}
    raw_position = portfolio.get("position")
    position = raw_position if isinstance(raw_position, dict) else {}
    market = status.get("market") if isinstance(status.get("market"), dict) else {}
    strategy = status.get("strategy") if isinstance(status.get("strategy"), dict) else {}
    trading = status.get("trading") if isinstance(status.get("trading"), dict) else {}
    lines = ["Chappie Paper Monitor", _line("Local time", now.strftime("%Y-%m-%d %H:%M:%S %Z"))]
    lines.extend(
        [
            _line("Process state", status.get("process_state", "stopped")),
            _line("Mode", status.get("mode")),
            _line("SAFE_STOP reason", status.get("safe_stop_reason")),
            "",
            "PORTFOLIO",
            _line(
                "Equity",
                _decimal(portfolio, "equity")
                if "equity" in portfolio
                else _decimal(portfolio, "current_equity"),
            ),
            _line("Quote balance / USDT", _decimal(portfolio, "quote_balance")),
            _line("Base balance / BTC", _decimal(portfolio, "base_balance")),
            _line(
                "Position quantity",
                _decimal(portfolio, "position_quantity")
                if "position_quantity" in portfolio
                else _decimal(position, "quantity"),
            ),
            _line(
                "Average entry price",
                _decimal(portfolio, "average_entry_price")
                if "average_entry_price" in portfolio
                else _decimal(position, "average_entry_price"),
            ),
            _line(
                "Cost basis",
                _decimal(portfolio, "cost_basis")
                if "cost_basis" in portfolio
                else _decimal(position, "cost_basis"),
            ),
            _line("Realized PnL", _decimal(portfolio, "realised_pnl")),
            _line("Unrealized PnL", _decimal(portfolio, "unrealised_pnl")),
            _line(
                "Fees",
                _decimal(portfolio, "fees")
                if "fees" in portfolio
                else _decimal(portfolio, "total_fees"),
            ),
            _line("Turnover", _decimal(portfolio, "turnover")),
            _line("Drawdown", _decimal(portfolio, "drawdown")),
            _line("Trade count", portfolio.get("trade_count")),
            _line(
                "Portfolio version", portfolio.get("portfolio_version", portfolio.get("version"))
            ),
            "",
            "MARKET",
            _line("Symbol", market.get("symbol", portfolio.get("symbol"))),
            _line("Interval", market.get("interval", portfolio.get("interval"))),
            _line("Current price", market.get("latest_price")),
            _line("Latest closed price", market.get("latest_closed_price")),
            _line("Cached candles", market.get("cached_candle_count")),
            _line(
                "Last successful polling cycle",
                _utc_time(
                    market.get(
                        "last_successful_market_cycle_time_ms",
                        status.get("last_successful_market_cycle_time_ms"),
                    )
                ),
            ),
            _line("Polling state", market.get("polling_state", status.get("market_polling_state"))),
            "",
            "STRATEGY",
            _line("Last signal", strategy.get("last_signal")),
            _line("Reason", strategy.get("reason")),
            _line("Last intent", strategy.get("last_intent")),
            _line("Last decision", _utc_time(strategy.get("last_decision_time_ms"))),
            "",
            "TRADES",
        ]
    )
    if not snapshot.fills:
        lines.append("  no trades")
    else:
        lines.append(
            "  time                 side  quantity       price          gross          fee       fill/order"
        )
        for fill in snapshot.fills:
            lines.append(
                f"  {_utc_time(fill['filled_at_ms'])[:19]:<20} {fill['side']:<5} "
                f"{fill['quantity']:<14} {fill['price']:<14} {fill['gross_notional']:<14} "
                f"{fill['fee']:<9} {fill['fill_id'][:8]}/{fill['order_id'][:8]}"
            )
    if trading.get("last_fill_id") or trading.get("last_outcome"):
        lines.append(
            _line("Last trade", trading.get("last_fill_id") or trading.get("last_outcome"))
        )
    lines.extend(
        [
            "",
            "SYSTEM",
            _line("DB path", snapshot.database_path),
            _line("DB status", snapshot.database_status),
            _line("DB schema", snapshot.schema_version),
            _line("Persistence/recovery", status.get("reconciliation_status")),
            _line("Submission enabled", status.get("submission_enabled")),
            _line("Status freshness", status.get("updated_at_utc")),
        ]
    )
    if snapshot.status_error:
        lines.append(_line("Status read", snapshot.status_error))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Chappie Paper runtime monitor")
    parser.add_argument("--interval", type=float, default=5.0, help="Refresh interval in seconds")
    parser.add_argument("--once", action="store_true", help="Render one snapshot and exit")
    args = parser.parse_args(argv)
    if args.interval <= 0:
        parser.error("--interval must be positive")
    settings = get_settings()
    try:
        while True:
            snapshot = read_paper_monitor_snapshot(
                settings.sqlite_path, settings.server_status_file
            )
            print("\x1b[2J\x1b[H" if not args.once else "", end="")
            print(render_dashboard(snapshot))
            if args.once:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
