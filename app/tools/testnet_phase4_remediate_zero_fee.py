"""Offline one-shot M4.10 zero-commission settlement remediation.

Importing this module performs no I/O. The operator has no network or credential dependency.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from time import time_ns
from uuid import uuid4

from app.exchange_execution import ExchangeOrderSide, ExchangeOrderStatus
from app.exchange_runtime import ExchangeSettlementStatus, SQLiteExchangeSettlement
from app.persistence.canonical import deserialize_decimal
from app.persistence.exchange import SQLiteExchangePersistence
from app.persistence.exchange_models import ExchangeReconciliationStatus
from app.persistence.models import SQLitePersistenceConfig
from app.persistence.runtime import state_to_snapshot
from app.persistence.sqlite import deserialize_portfolio_state
from app.portfolio import InstrumentRules, Portfolio, PortfolioConfig

AUTHORIZATION_ENV = "M4_10_PHASE4_REMEDIATION_AUTHORIZED"
AUTHORIZATION_VALUE = "YES"
DATABASE_PATH = Path("data/m4_10_testnet_smoke.sqlite3")
EVIDENCE_PATH = Path("logs/m4_10_phase4_remediation.log")
POLICY_ID = "zero-commission-asset-neutral/v1"
EXPECTED_BASE = Decimal("0.00016")
EXPECTED_QUOTE = Decimal("10.109264")
EXPECTED_INITIAL_BTC = Decimal(0)
EXPECTED_INITIAL_USDT = Decimal(25)
EXPECTED_FINAL_USDT = EXPECTED_INITIAL_USDT - EXPECTED_QUOTE


class RemediationError(RuntimeError):
    """Base local remediation error."""


class RemediationBlocked(RemediationError):
    """Durable preconditions or explicit operator authorization are absent."""


class RemediationFailed(RemediationError):
    """The local resolution completed without the required durable result."""


class RemediationOutcome(StrEnum):
    PASS = "PASS"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class RemediationResult:
    outcome: RemediationOutcome
    run_id: str
    observed_at_ms: int
    authority_id: str
    command_key: str
    client_order_id: str
    trade_id: str
    resolution_id: str
    portfolio_version_before: int
    portfolio_version_after: int
    btc_before: Decimal
    btc_after: Decimal
    usdt_before: Decimal
    usdt_after: Decimal
    turnover_before: Decimal
    turnover_after: Decimal
    settlement_status: ExchangeSettlementStatus
    idempotent: bool

    def render(self) -> str:
        fields = (
            ("run_id", self.run_id),
            ("observed_at_ms", self.observed_at_ms),
            ("authority_id", self.authority_id),
            ("durable_command_key", self.command_key),
            ("client_order_id", self.client_order_id),
            ("exchange_trade_id", self.trade_id),
            ("policy_id", POLICY_ID),
            ("resolution_id", self.resolution_id),
            ("portfolio_version_before", self.portfolio_version_before),
            ("portfolio_version_after", self.portfolio_version_after),
            ("managed_btc_before", self.btc_before),
            ("managed_btc_after", self.btc_after),
            ("managed_usdt_before", self.usdt_before),
            ("managed_usdt_after", self.usdt_after),
            ("turnover_before", self.turnover_before),
            ("turnover_after", self.turnover_after),
            ("global_settlement_status", self.settlement_status.value),
            ("idempotent", str(self.idempotent).lower()),
            ("outcome", self.outcome.value),
        )
        return "".join(f"{key}={value}\n" for key, value in fields)


def validate_startup(environ: Mapping[str, str], *, database: Path, evidence: Path) -> None:
    """Validate authorization and collision guards before writable DB acquisition."""
    if environ.get(AUTHORIZATION_ENV) != AUTHORIZATION_VALUE:
        raise RemediationBlocked("remediation authorization is absent or invalid")
    if evidence.exists():
        raise RemediationBlocked("remediation evidence already exists")
    if not database.is_file():
        raise RemediationBlocked("Phase 3 smoke database is absent")


def _managed_portfolio(store: SQLiteExchangePersistence) -> tuple[Portfolio, object]:
    roots = store._connection.execute("SELECT * FROM exchange_portfolio_roots").fetchall()
    if len(roots) != 1:
        raise RemediationBlocked("remediation requires exactly one managed Portfolio root")
    root = roots[0]
    state = deserialize_portfolio_state(root["state_payload"])
    if (
        state.version != 0
        or state.base_asset != "BTC"
        or state.quote_asset != "USDT"
        or state.base_balance != EXPECTED_INITIAL_BTC
        or state.quote_balance != EXPECTED_INITIAL_USDT
        or state.turnover != 0
        or state.total_fees != 0
        or state.trade_count != 0
        or state.applied_fill_count != 0
    ):
        raise RemediationBlocked("managed Portfolio root does not match the accepted initial state")
    rules = InstrumentRules(
        state.stream_key,
        state.base_asset,
        state.quote_asset,
        Decimal("0.01"),
        Decimal("0.00001"),
        Decimal("0.00001"),
        Decimal(5),
        8,
        8,
        "m4-10-remediation-local/v1",
    )
    portfolio = Portfolio(
        PortfolioConfig(
            state.portfolio_id,
            state.stream_key,
            state.base_asset,
            state.quote_asset,
            state.base_balance,
            state.quote_balance,
            rules,
        )
    )
    if portfolio.snapshot != state_to_snapshot(state):
        portfolio.synchronize_recovered_state(state_to_snapshot(state))
    return portfolio, root


def _validated_identity(store: SQLiteExchangePersistence):
    store.recover_local()
    authorities = store._connection.execute("SELECT * FROM exchange_smoke_authorities").fetchall()
    orders = store.list_orders()
    trades = store._connection.execute("SELECT * FROM exchange_trades").fetchall()
    blocks = store._connection.execute(
        "SELECT * FROM exchange_trade_settlements WHERE status='BLOCKED_UNSUPPORTED_FEE'"
    ).fetchall()
    other_settlements = store._connection.execute(
        "SELECT count(*) FROM exchange_trade_settlements WHERE status!='BLOCKED_UNSUPPORTED_FEE'"
    ).fetchone()[0]
    if (
        len(authorities) != 1
        or len(orders) != 1
        or len(trades) != 1
        or len(blocks) != 1
        or other_settlements != 0
    ):
        raise RemediationBlocked("durable smoke cardinality does not match the accepted state")
    authority, durable, trade, blocked = authorities[0], orders[0], trades[0], blocks[0]
    order = durable.order
    commission = deserialize_decimal(trade["commission"])
    if (
        authority["status"] != "CONSUMED"
        or authority["consumed_durable_command_key"] != order.durable_command_key
        or authority["execution_root_id"] != durable.execution_root_id
        or authority["session_id"] != durable.session_id
        or authority["account_fingerprint"] != order.account_fingerprint
        or order.side is not ExchangeOrderSide.BUY
        or order.status is not ExchangeOrderStatus.FILLED
        or order.exchange_order_id is None
        or trade["durable_command_key"] != order.durable_command_key
        or trade["exchange_order_id"] != order.exchange_order_id
        or deserialize_decimal(trade["quantity"]) != EXPECTED_BASE
        or deserialize_decimal(trade["quote_quantity"]) != EXPECTED_QUOTE
        or not commission.is_zero()
        or trade["commission_asset"] != "BTC"
        or blocked["exchange_trade_id"] != trade["exchange_trade_id"]
        or blocked["durable_command_key"] != order.durable_command_key
        or blocked["portfolio_version_before"] != 0
        or blocked["portfolio_version_after"] != 0
    ):
        raise RemediationBlocked(
            "durable trade/block lineage does not match accepted production truth"
        )
    return authority, durable, trade, blocked


def run_remediation(
    store: SQLiteExchangePersistence,
    *,
    run_id: str,
    now_ms: Callable[[], int],
    failure_injector: Callable[[str], None] | None = None,
) -> RemediationResult:
    """Resolve the exact known durable zero-fee block with no exchange interaction."""
    if store._trading.config.expected_schema_version != 6:
        raise RemediationBlocked("remediation requires schema version 6")
    authority, durable, trade, blocked = _validated_identity(store)
    order = durable.order
    portfolio, root = _managed_portfolio(store)
    current = store._connection.execute(
        "SELECT * FROM exchange_portfolio_current WHERE portfolio_id=?",
        (root["portfolio_id"],),
    ).fetchone()
    if current is None:
        raise RemediationBlocked("managed Portfolio current state is absent")
    before_state = deserialize_portfolio_state(current["state_payload"])
    session_before = store.load_session(durable.session_id)
    resolution_rows = store._connection.execute(
        "SELECT * FROM exchange_trade_settlement_resolutions"
    ).fetchall()
    already_resolved = len(resolution_rows) == 1
    if len(resolution_rows) > 1:
        raise RemediationBlocked("multiple zero-fee resolutions conflict")
    if already_resolved:
        if (
            before_state.version != 1
            or session_before.cumulative_executed_turnover != EXPECTED_QUOTE
        ):
            raise RemediationBlocked("existing resolution does not match durable Portfolio truth")
    elif (
        before_state.version != 0
        or before_state.base_balance != EXPECTED_INITIAL_BTC
        or before_state.quote_balance != EXPECTED_INITIAL_USDT
        or before_state.turnover != 0
        or session_before.cumulative_executed_turnover != 0
    ):
        raise RemediationBlocked("current Portfolio/session is not eligible for remediation")
    reconciliation = store.read_reconciliation_state(durable.execution_root_id)
    settlement = SQLiteExchangeSettlement(
        store,
        portfolio,
        execution_root_id=durable.execution_root_id,
        session_id=durable.session_id,
        config_fingerprint=root["config_fingerprint"],
        timestamp_provider=now_ms,
        failure_injector=failure_injector,
    )
    if (
        reconciliation.status is not ExchangeReconciliationStatus.SAFE
        or reconciliation.latest_reconciliation_id is None
        or not settlement._coverage_is_complete(reconciliation)
    ):
        raise RemediationBlocked("SAFE complete reconciliation authority is absent")
    settlement.initialize()
    result = settlement.settle_pending()
    after = portfolio.snapshot
    session_after = store.load_session(durable.session_id)
    resolutions = store._connection.execute(
        "SELECT * FROM exchange_trade_settlement_resolutions"
    ).fetchall()
    persisted_trade = store._connection.execute(
        "SELECT commission,commission_asset FROM exchange_trades WHERE exchange_trade_id=?",
        (trade["exchange_trade_id"],),
    ).fetchone()
    historical_block = store._connection.execute(
        "SELECT status FROM exchange_trade_settlements WHERE settlement_id=?",
        (blocked["settlement_id"],),
    ).fetchone()
    if (
        result.status is not ExchangeSettlementStatus.SYNCHRONIZED
        or len(resolutions) != 1
        or after.version != 1
        or after.base_balance != EXPECTED_BASE
        or after.quote_balance != EXPECTED_FINAL_USDT
        or after.total_fees != 0
        or after.turnover != EXPECTED_QUOTE
        or session_after.cumulative_executed_turnover != EXPECTED_QUOTE
        or tuple(persisted_trade or ()) != ("0", "BTC")
        or tuple(historical_block or ()) != ("BLOCKED_UNSUPPORTED_FEE",)
    ):
        raise RemediationFailed(
            "local remediation did not produce exact synchronized durable truth"
        )
    return RemediationResult(
        RemediationOutcome.PASS,
        run_id,
        now_ms(),
        authority["authority_id"],
        order.durable_command_key,
        order.client_order_id,
        trade["exchange_trade_id"],
        resolutions[0]["resolution_id"],
        before_state.version,
        after.version,
        before_state.base_balance,
        after.base_balance,
        before_state.quote_balance,
        after.quote_balance,
        session_before.cumulative_executed_turnover,
        session_after.cumulative_executed_turnover,
        result.status,
        already_resolved or result.resolved_count == 0,
    )


def main() -> int:
    try:
        validate_startup(os.environ, database=DATABASE_PATH, evidence=EVIDENCE_PATH)
    except RemediationBlocked as error:
        print(type(error).__name__, file=sys.stderr)
        return 2
    try:
        store = SQLiteExchangePersistence(
            SQLitePersistenceConfig(DATABASE_PATH, expected_schema_version=6)
        )
        try:
            result = run_remediation(
                store,
                run_id=str(uuid4()),
                now_ms=lambda: time_ns() // 1_000_000,
            )
        finally:
            store.close()
        EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with EVIDENCE_PATH.open("x", encoding="utf-8") as evidence:
            rendered = result.render()
            if evidence.write(rendered) != len(rendered):
                raise OSError("remediation evidence write was incomplete")
        print(result.render(), end="")
        return 0
    except (KeyboardInterrupt, SystemExit):
        raise
    except RemediationBlocked as error:
        print(type(error).__name__, file=sys.stderr)
        return 2
    except Exception as error:  # noqa: BLE001
        print(type(error).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
