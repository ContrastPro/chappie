"""Read-only terminal dashboard for the active local Paper runtime."""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
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
    analytics_fills: tuple[dict[str, Any], ...]
    performance_complete: bool
    status: dict[str, Any] | None
    status_error: str | None


def _decimal(payload: dict[str, Any], name: str) -> str:
    value = payload.get(name)
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("$decimal"), str):
        return value["$decimal"]
    return NA


def _as_decimal(value: object) -> Decimal | None:
    if isinstance(value, dict):
        value = value.get("$decimal")
    if not isinstance(value, str | int | float | Decimal):
        return None
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return decimal if decimal.is_finite() else None


def _format_decimal(value: object, *, places: int = 2, suffix: str = "") -> str:
    decimal = _as_decimal(value)
    if decimal is None:
        return NA
    quantized = decimal.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return f"{quantized:,.{places}f}{suffix}"


def _format_btc(value: object) -> str:
    decimal = _as_decimal(value)
    if decimal is None:
        return NA
    text = f"{decimal.quantize(Decimal('0.00000001'), rounding=ROUND_HALF_UP):f}".rstrip(
        "0"
    ).rstrip(".")
    return text or "0"


def _format_duration(milliseconds: object) -> str:
    if isinstance(milliseconds, bool) or not isinstance(milliseconds, int) or milliseconds < 0:
        return NA
    seconds = milliseconds // 1000
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


def _portfolio_value(portfolio: dict[str, Any], name: str) -> object:
    return portfolio.get(name, portfolio.get({"equity": "current_equity"}.get(name, name)))


def _trade_analytics(
    fills: tuple[dict[str, Any], ...], portfolio: dict[str, Any]
) -> dict[str, object]:
    """Calculate conservative FIFO Paper analytics from complete persisted fills only."""
    lots: list[dict[str, Decimal | int]] = []
    completed: list[tuple[Decimal, int]] = []
    buy_count = sell_count = 0
    for fill in sorted(fills, key=lambda item: int(item.get("filled_at_ms") or 0)):
        quantity = _as_decimal(fill.get("quantity"))
        price = _as_decimal(fill.get("price"))
        fee = _as_decimal(fill.get("fee"))
        timestamp = fill.get("filled_at_ms")
        if quantity is None or price is None or fee is None or not isinstance(timestamp, int):
            continue
        if fill.get("side") == "BUY":
            buy_count += 1
            lots.append(
                {"quantity": quantity, "unit_cost": price + fee / quantity, "time": timestamp}
            )
        elif fill.get("side") == "SELL":
            sell_count += 1
            remaining = quantity
            fee_per_unit = fee / quantity
            while remaining > 0 and lots:
                lot = lots[0]
                lot_quantity = Decimal(str(lot["quantity"]))
                matched = min(remaining, lot_quantity)
                pnl = matched * (price - fee_per_unit - Decimal(str(lot["unit_cost"])))
                completed.append((pnl, max(0, timestamp - int(lot["time"]))))
                remaining -= matched
                if matched == lot_quantity:
                    lots.pop(0)
                else:
                    lot["quantity"] = lot_quantity - matched
    results = [value for value, _ in completed]
    gross = sum((value for value in results if value > 0), Decimal(0))
    net = sum(results, Decimal(0))
    equity = _as_decimal(_portfolio_value(portfolio, "equity"))
    initial = _as_decimal(portfolio.get("initial_equity"))
    fees = _as_decimal(portfolio.get("fees", portfolio.get("total_fees"))) or Decimal(0)
    turnover = _as_decimal(portfolio.get("turnover"))
    return {
        "buy_count": buy_count,
        "sell_count": sell_count,
        "completed": len(completed),
        "wins": sum(1 for value in results if value > 0),
        "losses": sum(1 for value in results if value < 0),
        "gross_realised": gross,
        "net_realised": net,
        "average_net": net / len(completed) if completed else None,
        "best": max(results) if results else None,
        "worst": min(results) if results else None,
        "average_hold_ms": sum(value for _, value in completed) // len(completed)
        if completed
        else None,
        "win_rate": (Decimal(sum(1 for value in results if value > 0)) / len(completed) * 100)
        if completed
        else None,
        "fee_drag": (fees / (initial or equity) * 100) if (initial or equity) else None,
        "turnover_ratio": (turnover / equity * 100) if turnover is not None and equity else None,
    }


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
        trading = status.get("trading")
        recent = trading.get("recent_trades") if isinstance(trading, dict) else None
        fills = (
            tuple(item for item in recent if isinstance(item, dict))
            if isinstance(recent, list)
            else ()
        )
        return PaperMonitorSnapshot(
            database_path,
            "owned by active runtime / live snapshot mode",
            status.get("schema_version") if isinstance(status.get("schema_version"), int) else None,
            portfolio if isinstance(portfolio, dict) else None,
            fills[:fill_limit],
            (),
            False,
            status,
            status_error,
        )
    if not database_path.is_file():
        return PaperMonitorSnapshot(
            database_path, "no database", None, None, (), (), False, status, status_error
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
            analytics_fills = tuple(
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
                    "FROM fills ORDER BY filled_at_ms ASC",
                )
            )
            fills = tuple(reversed(analytics_fills[-fill_limit:]))
        finally:
            connection.close()
    except (sqlite3.Error, OSError, json.JSONDecodeError) as error:
        return PaperMonitorSnapshot(
            database_path,
            f"unavailable: {type(error).__name__}",
            None,
            None,
            (),
            (),
            False,
            status,
            status_error,
        )
    return PaperMonitorSnapshot(
        database_path,
        "available",
        schema_version,
        portfolio,
        fills,
        analytics_fills,
        True,
        status,
        status_error,
    )


def _utc_time(value: object) -> str:
    if not isinstance(value, int):
        return NA
    return datetime.fromtimestamp(value / 1000, UTC).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def _status_freshness(value: object, now: datetime) -> str:
    if not isinstance(value, str):
        return "STATUS UNAVAILABLE"
    try:
        updated = datetime.fromisoformat(value)
    except ValueError:
        return "STATUS INVALID"
    if updated.tzinfo is None:
        return "STATUS INVALID"
    seconds = max(0, int((now.astimezone(UTC) - updated.astimezone(UTC)).total_seconds()))
    return (
        f"STATUS STALE ({_format_duration(seconds * 1000)})"
        if seconds > 120
        else f"fresh ({_format_duration(seconds * 1000)})"
    )


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
    analytics = (
        _trade_analytics(snapshot.analytics_fills, portfolio)
        if snapshot.performance_complete
        else {}
    )
    equity = _portfolio_value(portfolio, "equity")
    position_quantity = (
        portfolio.get("position_quantity")
        if "position_quantity" in portfolio
        else _decimal(position, "quantity")
    )
    current_price = market.get("latest_price")
    quantity_decimal, price_decimal = _as_decimal(position_quantity), _as_decimal(current_price)
    position_value = (
        quantity_decimal * price_decimal
        if quantity_decimal is not None and price_decimal is not None
        else None
    )
    realised = _decimal(portfolio, "realised_pnl")
    unrealised = _decimal(portfolio, "unrealised_pnl")
    total_pnl = (
        _as_decimal(realised) + _as_decimal(unrealised)
        if _as_decimal(realised) is not None and _as_decimal(unrealised) is not None
        else None
    )
    initial_equity = _decimal(portfolio, "initial_equity")
    total_return = (
        (total_pnl / _as_decimal(initial_equity) * 100)
        if total_pnl is not None and _as_decimal(initial_equity)
        else None
    )
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
                _format_decimal(equity),
            ),
            _line("Quote balance / USDT", _format_decimal(_decimal(portfolio, "quote_balance"))),
            _line("Base balance / BTC", _format_btc(_decimal(portfolio, "base_balance"))),
            _line("Position quantity", _format_btc(position_quantity)),
            _line("Position market value", _format_decimal(position_value)),
            _line(
                "Average entry price",
                _format_decimal(
                    _decimal(portfolio, "average_entry_price")
                    if "average_entry_price" in portfolio
                    else _decimal(position, "average_entry_price")
                ),
            ),
            _line(
                "Cost basis",
                _format_decimal(
                    _decimal(portfolio, "cost_basis")
                    if "cost_basis" in portfolio
                    else _decimal(position, "cost_basis")
                ),
            ),
            _line("Realized PnL", _format_decimal(realised)),
            _line("Unrealized PnL", _format_decimal(unrealised)),
            _line("Total PnL", _format_decimal(total_pnl)),
            _line("Total return", _format_decimal(total_return, suffix="%")),
            _line(
                "Fees",
                _format_decimal(
                    _decimal(portfolio, "fees")
                    if "fees" in portfolio
                    else _decimal(portfolio, "total_fees")
                ),
            ),
            _line("Turnover", _format_decimal(_decimal(portfolio, "turnover"))),
            _line("Drawdown", _format_decimal(_decimal(portfolio, "drawdown"))),
            _line("Trade count", portfolio.get("trade_count")),
            _line(
                "Portfolio version", portfolio.get("portfolio_version", portfolio.get("version"))
            ),
            "",
            "MARKET",
            _line("Symbol", market.get("symbol", portfolio.get("symbol"))),
            _line("Interval", market.get("interval", portfolio.get("interval"))),
            _line("Current price", _format_decimal(current_price)),
            _line("Latest closed price", _format_decimal(market.get("latest_closed_price"))),
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
            _line("Active strategy", strategy.get("name")),
            _line("Parameters", strategy.get("parameters")),
            _line("Indicators", strategy.get("indicators")),
            _line("Last signal", strategy.get("last_signal")),
            _line("Reason", strategy.get("reason")),
            _line("Fast EMA", _format_decimal(strategy.get("fast_ema"))),
            _line("Slow EMA", _format_decimal(strategy.get("slow_ema"))),
            _line("EMA spread", _format_decimal(strategy.get("ema_spread"))),
            _line("Exposure", strategy.get("exposure", "LONG" if quantity_decimal else "FLAT")),
            _line("Last BUY", _utc_time(strategy.get("last_buy_time_ms"))),
            _line("Last SELL", _utc_time(strategy.get("last_sell_time_ms"))),
            _line("Last intent", strategy.get("last_intent")),
            _line("Last decision", _utc_time(strategy.get("last_decision_time_ms"))),
            "",
            (
                "PERFORMANCE (complete persisted fills only)"
                if snapshot.performance_complete
                else "PERFORMANCE (unavailable while runtime owns SQLite)"
            ),
            _line(
                "BUY / SELL fills",
                f"{analytics.get('buy_count', NA)} / {analytics.get('sell_count', NA)}",
            ),
            _line("Completed round trips", analytics.get("completed", NA)),
            _line("Wins / losses", f"{analytics.get('wins', NA)} / {analytics.get('losses', NA)}"),
            _line("Win rate", _format_decimal(analytics.get("win_rate"), suffix="%")),
            _line("Gross realized PnL", _format_decimal(analytics.get("gross_realised"))),
            _line("Net realized PnL", _format_decimal(analytics.get("net_realised"))),
            _line("Average net per round trip", _format_decimal(analytics.get("average_net"))),
            _line(
                "Best / worst round trip",
                f"{_format_decimal(analytics.get('best'))} / {_format_decimal(analytics.get('worst'))}",
            ),
            _line("Average holding duration", _format_duration(analytics.get("average_hold_ms"))),
            _line("Fee drag", _format_decimal(analytics.get("fee_drag"), suffix="%")),
            _line(
                "Turnover / equity", _format_decimal(analytics.get("turnover_ratio"), suffix="%")
            ),
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
                f"  {_utc_time(fill.get('filled_at_ms'))[:19]:<20} {fill.get('side', NA)!s:<5} "
                f"{_format_btc(fill.get('quantity')):<14} {_format_decimal(fill.get('price')):<14} "
                f"{_format_decimal(fill.get('gross_notional')):<14} {_format_decimal(fill.get('fee')):<9} "
                f"{str(fill.get('fill_id', NA))[:8]}/{str(fill.get('order_id', NA))[:8]}"
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
            _line("Status freshness", _status_freshness(status.get("updated_at_utc"), now)),
        ]
    )
    if snapshot.status_error:
        lines.append(_line("Status read", snapshot.status_error))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Chappie Paper runtime monitor")
    parser.add_argument("--interval", type=float, default=5.0, help="Refresh interval in seconds")
    parser.add_argument("--once", action="store_true", help="Render one snapshot and exit")
    parser.add_argument(
        "--trades", type=int, default=10, help="Recent trades to display (default: 10)"
    )
    args = parser.parse_args(argv)
    if args.interval <= 0:
        parser.error("--interval must be positive")
    if args.trades <= 0:
        parser.error("--trades must be positive")
    settings = get_settings()
    try:
        while True:
            snapshot = read_paper_monitor_snapshot(
                settings.sqlite_path, settings.server_status_file, fill_limit=args.trades
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
