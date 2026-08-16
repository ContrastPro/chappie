"""Small, atomic server-status surface for the active MVP process."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.application import ChappieTraderApplication


class ServerProcessState(StrEnum):
    STARTING = "STARTING"
    READY = "READY"
    SAFE_STOP = "SAFE_STOP"
    STOPPED = "STOPPED"


class ServerStatusWriteError(RuntimeError):
    """The operator-visible status could not be safely published."""


@dataclass(frozen=True, slots=True)
class ServerStatus:
    mode: str
    process_state: ServerProcessState
    schema_version: int | None
    market_polling_state: str
    last_successful_market_cycle_time_ms: int | None
    reconciliation_status: str | None
    reconciliation_time_ms: int | None
    settlement_status: str | None
    submission_enabled: bool
    safe_stop_reason: str | None
    updated_at_utc: str
    portfolio: dict[str, object] | None = None
    market: dict[str, object] | None = None
    strategy: dict[str, object] | None = None
    trading: dict[str, object] | None = None


class AtomicServerStatusPublisher:
    """Publish sanitized JSON with replace-based atomic visibility."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def publish(self, status: ServerStatus) -> None:
        temporary: Path | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            encoded = (
                json.dumps(asdict(status), sort_keys=True, separators=(",", ":")) + "\n"
            ).encode("utf-8")
            with NamedTemporaryFile(
                mode="wb", dir=self._path.parent, prefix=f".{self._path.name}.", delete=False
            ) as handle:
                temporary = Path(handle.name)
                written = handle.write(encoded)
                if written != len(encoded):
                    raise ServerStatusWriteError("status write was incomplete")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
        except ServerStatusWriteError:
            raise
        except OSError as error:
            raise ServerStatusWriteError("server status publication failed") from error
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass


def status_for_application(
    application: ChappieTraderApplication,
    state: ServerProcessState,
    *,
    safe_stop_reason: str | None = None,
) -> ServerStatus:
    report = application.status_report()
    reason = (
        safe_stop_reason or report.testnet_safe_stop_reason or application.startup_safe_stop_reason
    )
    if state is ServerProcessState.SAFE_STOP and reason is None:
        reason = "STARTUP_SAFETY_CHECK_FAILED"

    def decimal_text(value: Decimal | None) -> str | None:
        return str(value) if value is not None else None

    latest = report.latest_candle
    latest_closed = report.latest_closed_candle
    portfolio = None
    if report.paper_enabled:
        portfolio = {
            "quote_asset": report.paper_quote_asset,
            "quote_balance": decimal_text(report.paper_quote_balance),
            "base_asset": report.paper_base_asset,
            "base_balance": decimal_text(report.paper_base_balance),
            "equity": decimal_text(report.paper_equity),
            "position_quantity": decimal_text(report.paper_position_quantity),
            "average_entry_price": decimal_text(report.paper_average_entry_price),
            "cost_basis": decimal_text(report.paper_cost_basis),
            "realised_pnl": decimal_text(report.paper_realised_pnl),
            "unrealised_pnl": decimal_text(report.paper_unrealised_pnl),
            "fees": decimal_text(report.paper_total_fees),
            "turnover": decimal_text(report.paper_turnover),
            "drawdown": decimal_text(report.paper_drawdown),
            "trade_count": report.paper_trade_count,
            "portfolio_version": report.paper_portfolio_version,
        }
    return ServerStatus(
        mode=application.settings.trading_mode,
        process_state=state,
        schema_version=report.persistence_schema_version,
        market_polling_state=report.lifecycle_state.value,
        last_successful_market_cycle_time_ms=report.last_success_time_ms,
        reconciliation_status=report.testnet_reconciliation_status,
        reconciliation_time_ms=report.last_exchange_time_ms,
        settlement_status=report.testnet_settlement_status,
        submission_enabled=(
            state is ServerProcessState.READY and report.testnet_submission_capability_present
        ),
        safe_stop_reason=reason,
        updated_at_utc=datetime.now(UTC).isoformat(),
        portfolio=portfolio,
        market={
            "symbol": report.paper_symbol or getattr(application.settings, "default_symbol", None),
            "interval": report.paper_interval
            or getattr(application.settings, "default_interval", None),
            "latest_price": decimal_text(latest.close) if latest is not None else None,
            "latest_open_price": decimal_text(latest.open) if latest is not None else None,
            "latest_closed_price": (
                decimal_text(latest_closed.close) if latest_closed is not None else None
            ),
            "cached_candle_count": report.cached_candle_count,
            "polling_state": report.lifecycle_state.value,
            "last_successful_market_cycle_time_ms": report.last_success_time_ms,
            "successful_cycles": report.successful_cycles,
            "failed_cycles": report.failed_cycles,
        },
        strategy={
            "last_signal": report.shadow_last_signal_direction,
            "reason": report.shadow_last_error_category,
            "last_intent": report.shadow_last_intent_id,
            "last_decision_time_ms": report.last_success_time_ms,
        },
        trading={
            "last_outcome": report.paper_last_outcome,
            "last_fill_id": report.paper_last_fill_id,
        },
    )
