"""One-shot M4.10 Phase 4 recovery and settlement operator.

Importing this module performs no I/O. Production execution requires separate authorization.
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from time import time_ns
from types import MappingProxyType
from uuid import uuid4

import httpx

from app.core.logger import suppress_private_transport_logging
from app.exchange_execution import ExchangeOrderSide, ExchangeOrderStatus
from app.exchange_execution.models import QueryResultKind
from app.exchange_runtime.identity import SQLiteOrderIdentityResolver
from app.exchange_runtime.settlement import SQLiteExchangeSettlement
from app.infrastructure.binance import BinanceSpotTestnetAdapter, BinanceSpotTestnetConfig
from app.persistence.canonical import canonical_sha256, deserialize_decimal
from app.persistence.exchange import SQLiteExchangePersistence
from app.persistence.exchange_models import (
    ExchangeReconciliationStatus,
    PersistentExchangeReconciliationRun,
)
from app.persistence.models import SQLitePersistenceConfig
from app.portfolio import InstrumentRules, Portfolio, PortfolioConfig

AUTHORIZATION_ENV = "M4_10_PHASE4_AUTHORIZED"
AUTHORIZATION_VALUE = "YES"
DATABASE_PATH = Path("data/m4_10_testnet_smoke.sqlite3")
EVIDENCE_PATH = Path("logs/m4_10_phase4.log")
HOST = "https://testnet.binance.vision"
MAX_REQUESTS = 4
ALLOWED = frozenset(
    {
        ("GET", "/api/v3/order"),
        ("GET", "/api/v3/myTrades"),
        ("GET", "/api/v3/account"),
        ("GET", "/api/v3/exchangeInfo"),
    }
)


class Phase4Error(RuntimeError):
    pass


class Phase4Blocked(Phase4Error):
    pass


class Phase4TransportViolation(Phase4Blocked):
    pass


class Phase4Outcome(StrEnum):
    PASS = "PASS"
    INCOMPLETE = "INCOMPLETE"
    BLOCKED = "BLOCKED"


class Phase4ReadOnlyTransport(httpx.BaseTransport):
    def __init__(self, transport: httpx.BaseTransport) -> None:
        self._transport = transport
        self._counts: Counter[str] = Counter()
        self._closed = False

    @property
    def request_count(self) -> int:
        return sum(self._counts.values())

    @property
    def endpoint_counts(self) -> Mapping[str, int]:
        return MappingProxyType(dict(sorted(self._counts.items())))

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if (
            request.url.scheme != "https"
            or request.url.host != "testnet.binance.vision"
            or request.url.port not in {None, 443}
            or (request.method, request.url.path) not in ALLOWED
        ):
            raise Phase4TransportViolation("Phase 4 transport is read-only and fixed-host")
        if self.request_count >= MAX_REQUESTS:
            raise Phase4TransportViolation("Phase 4 request budget exhausted")
        self._counts[f"{request.method} {request.url.path}"] += 1
        response = self._transport.handle_request(request)
        if 300 <= response.status_code < 400:
            raise Phase4TransportViolation("Phase 4 redirects are forbidden")
        return response

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._transport.close()


@dataclass(frozen=True, slots=True)
class RecoveredPhase3:
    authority_id: str
    root_id: str
    session_id: str
    account_fingerprint: str
    command_key: str
    client_order_id: str
    exchange_order_id: str
    quantity: Decimal
    config_fingerprint: str
    rules_fingerprint: str
    observed_at_ms: int
    external_btc: Decimal
    external_usdt: Decimal
    external_btc_locked: Decimal
    external_usdt_locked: Decimal


@dataclass(frozen=True, slots=True)
class Phase4Result:
    outcome: Phase4Outcome
    run_id: str
    observed_at_ms: int
    recovered: RecoveredPhase3
    request_count: int
    endpoint_counts: Mapping[str, int]
    remote_status: str
    authoritative_trade_count: int
    newly_settled_trade_count: int
    duplicate_trade_count: int
    managed_btc_before: Decimal
    managed_btc_after: Decimal
    managed_usdt_before: Decimal
    managed_usdt_after: Decimal
    reconciliation_status: str

    def render(self) -> str:
        fields = {
            "outcome": self.outcome,
            "run_id": self.run_id,
            "observed_at_ms": self.observed_at_ms,
            "host": HOST,
            "account_fingerprint": self.recovered.account_fingerprint,
            "authority_id": self.recovered.authority_id,
            "durable_command_key": self.recovered.command_key,
            "client_order_id": self.recovered.client_order_id,
            "request_count": self.request_count,
            "endpoint_counts": ",".join(
                f"{key}:{value}" for key, value in self.endpoint_counts.items()
            ),
            "authoritative_order_status": self.remote_status,
            "authoritative_trade_count": self.authoritative_trade_count,
            "newly_settled_trade_count": self.newly_settled_trade_count,
            "duplicate_trade_count": self.duplicate_trade_count,
            "managed_btc_before": self.managed_btc_before,
            "managed_btc_after": self.managed_btc_after,
            "managed_usdt_before": self.managed_usdt_before,
            "managed_usdt_after": self.managed_usdt_after,
            "reconciliation_status": self.reconciliation_status,
        }
        return "".join(f"{key}={value}\n" for key, value in fields.items())


def validate_startup(environ: Mapping[str, str], *, database: Path, evidence: Path) -> str:
    if environ.get(AUTHORIZATION_ENV) != AUTHORIZATION_VALUE:
        raise Phase4Blocked("Phase 4 authorization is absent or invalid")
    if not database.is_file():
        raise Phase4Blocked("Phase 3 smoke database is absent")
    if evidence.exists():
        raise Phase4Blocked("Phase 4 evidence already exists")
    fingerprint = environ.get("BINANCE_TESTNET_ACCOUNT_FINGERPRINT", "")
    if not fingerprint.strip():
        raise Phase4Blocked("Phase 4 account fingerprint is absent")
    if not environ.get("BINANCE_TESTNET_API_KEY") or not environ.get("BINANCE_TESTNET_SECRET_KEY"):
        raise Phase4Blocked("Phase 4 credentials are absent")
    return fingerprint


def recover_phase3(store: SQLiteExchangePersistence, expected_account: str) -> RecoveredPhase3:
    # This validates the complete authority/event/command/Portfolio linkage and all
    # payload digests before any recovered field is trusted by the operator.
    store.recover_local()
    rows = store._connection.execute("SELECT * FROM exchange_smoke_authorities").fetchall()
    orders = store.list_orders()
    if len(rows) != 1 or len(orders) != 1:
        raise Phase4Blocked("Phase 4 requires exactly one authority and order")
    row, durable = rows[0], orders[0]
    order = durable.order
    scope = json.loads(row["balance_scope_payload"])
    if (
        row["status"] != "CONSUMED"
        or row["consumed_durable_command_key"] != order.durable_command_key
        or row["account_fingerprint"] != expected_account
        or order.account_fingerprint != expected_account
        or order.side is not ExchangeOrderSide.BUY
        or order.exchange_order_id is None
        or order.status is ExchangeOrderStatus.PREPARED
    ):
        raise Phase4Blocked("Phase 3 durable identity is inconsistent")
    return RecoveredPhase3(
        row["authority_id"],
        row["execution_root_id"],
        row["session_id"],
        expected_account,
        order.durable_command_key,
        order.client_order_id,
        order.exchange_order_id,
        order.original_base_quantity,
        row["config_fingerprint"],
        row["rules_fingerprint"],
        scope["observed_at_ms"],
        deserialize_decimal(scope["btc_free"]["$decimal"]),
        deserialize_decimal(scope["usdt_free"]["$decimal"]),
        deserialize_decimal(scope["btc_locked"]["$decimal"]),
        deserialize_decimal(scope["usdt_locked"]["$decimal"]),
    )


def _rules_fingerprint(rules) -> str:
    return canonical_sha256(
        {
            "symbol": rules.stream_key.symbol,
            "step": rules.quantity_step,
            "min_quantity": rules.min_quantity,
            "min_notional": rules.min_notional,
            "version": rules.rules_version,
        },
        algorithm_version="phase3-rules/v1",
    )


def run_phase4(
    store: SQLiteExchangePersistence,
    adapter: BinanceSpotTestnetAdapter,
    guard: Phase4ReadOnlyTransport,
    *,
    expected_account: str,
    run_id: str,
    now_ms: Callable[[], int],
) -> Phase4Result:
    suppress_private_transport_logging()
    recovered = recover_phase3(store, expected_account)
    durable = store.load_order(recovered.command_key)
    before_count = store.trade_count(recovered.command_key)
    portfolio_before = store._connection.execute(
        "SELECT state_payload FROM exchange_portfolio_current"
    ).fetchone()
    if portfolio_before is None:
        raise Phase4Blocked("managed Portfolio is missing")
    before_payload = json.loads(portfolio_before[0])
    rules = adapter.read_instrument_rules(durable.order.stream_key)
    if _rules_fingerprint(rules) != recovered.rules_fingerprint:
        raise Phase4Blocked("instrument rules drifted from Phase 3")
    query = adapter.query_order(
        stream_key=durable.order.stream_key, client_order_id=recovered.client_order_id
    )
    if query.kind is not QueryResultKind.FOUND or query.order is None:
        raise Phase4Blocked("authoritative order is unavailable")
    remote = query.order
    if (
        remote.durable_command_key != recovered.command_key
        or remote.client_order_id != recovered.client_order_id
        or remote.exchange_order_id != recovered.exchange_order_id
        or remote.original_base_quantity != recovered.quantity
        or remote.side is not ExchangeOrderSide.BUY
    ):
        raise Phase4Blocked("authoritative order identity conflicts")
    trades = adapter.list_order_trades(
        stream_key=remote.stream_key, exchange_order_id=recovered.exchange_order_id
    )
    balances = {value.asset: value for value in adapter.read_balances(("BTC", "USDT"))}
    quote_quantity = sum((trade.quote_quantity for trade in trades), Decimal(0))
    base_bought = sum((trade.quantity for trade in trades), Decimal(0))
    quote_fee = sum(
        (trade.commission for trade in trades if trade.commission_asset == "USDT"), Decimal(0)
    )
    base_fee = sum(
        (trade.commission for trade in trades if trade.commission_asset == "BTC"), Decimal(0)
    )
    if (
        balances["BTC"].free != recovered.external_btc + base_bought - base_fee
        or balances["BTC"].locked != recovered.external_btc_locked
        or balances["USDT"].free != recovered.external_usdt - quote_quantity - quote_fee
        or balances["USDT"].locked != recovered.external_usdt_locked
    ):
        raise Phase4Blocked("exchange balances conflict with authoritative managed effects")
    started = now_ms()
    current = store.load_order(recovered.command_key)
    store.record_status(
        recovered.command_key,
        status=remote.status,
        occurred_at_ms=now_ms(),
        reason_code="phase4_authoritative_order",
        exchange_order_id=remote.exchange_order_id,
        acknowledged_at_ms=remote.acknowledged_at_ms,
        cumulative_executed_base_quantity=remote.cumulative_executed_base_quantity,
        cumulative_quote_quantity=remote.cumulative_quote_quantity,
        exchange_timestamp_ms=remote.latest_exchange_update_ms,
    )
    store.persist_trades(recovered.command_key, trades)
    completeness = store.trade_completeness(recovered.command_key)
    persisted = store.load_order(recovered.command_key).order
    persisted_quote = sum(
        (
            deserialize_decimal(row[0])
            for row in store._connection.execute(
                "SELECT quote_quantity FROM exchange_trades WHERE durable_command_key=?",
                (recovered.command_key,),
            ).fetchall()
        ),
        Decimal(0),
    )
    safe = (
        completeness.value == "COMPLETE" and persisted_quote == persisted.cumulative_quote_quantity
    )
    state = store.read_reconciliation_state(recovered.root_id)
    reconciliation_id = canonical_sha256(
        {"run": run_id, "root": recovered.root_id}, algorithm_version="phase4-reconciliation/v1"
    )
    completed = now_ms()
    outcome = ExchangeReconciliationStatus.SAFE if safe else ExchangeReconciliationStatus.REQUIRED
    store.record_reconciliation_run(
        PersistentExchangeReconciliationRun(
            reconciliation_id,
            recovered.root_id,
            recovered.session_id,
            recovered.account_fingerprint,
            state.generation,
            state.generation + safe,
            outcome,
            1,
            int(current.order != remote),
            store.trade_count(recovered.command_key) - before_count,
            0,
            2,
            () if safe else ("TRADE_INCOMPLETE",),
            started,
            completed,
            0,
            completed,
            safe,
            safe,
            "BTCUSDT",
        )
    )
    config = PortfolioConfig(
        "m4-10-phase3-managed",
        durable.order.stream_key,
        "BTC",
        "USDT",
        Decimal(0),
        Decimal(25),
        InstrumentRules(
            durable.order.stream_key,
            "BTC",
            "USDT",
            Decimal("0.01"),
            rules.quantity_step,
            rules.min_quantity,
            rules.min_notional,
            8,
            8,
            rules.rules_version,
        ),
    )
    portfolio = Portfolio(config)
    settlement = SQLiteExchangeSettlement(
        store,
        portfolio,
        execution_root_id=recovered.root_id,
        session_id=recovered.session_id,
        config_fingerprint=recovered.config_fingerprint,
        timestamp_provider=now_ms,
    )
    settlement_result = settlement.settle_pending()
    after = portfolio.snapshot
    final = store.load_order(recovered.command_key).order
    finished = final.status.is_exchange_terminal and safe and settlement_result.blocked_count == 0
    result_outcome = Phase4Outcome.PASS if finished else Phase4Outcome.INCOMPLETE
    return Phase4Result(
        result_outcome,
        run_id,
        completed,
        recovered,
        guard.request_count,
        guard.endpoint_counts,
        remote.status.value,
        len(trades),
        settlement_result.settled_count,
        len(trades) - (store.trade_count(recovered.command_key) - before_count),
        deserialize_decimal(before_payload["base_balance"]["$decimal"]),
        after.base_balance,
        deserialize_decimal(before_payload["quote_balance"]["$decimal"]),
        after.quote_balance,
        outcome.value,
    )


def main() -> int:
    suppress_private_transport_logging()
    try:
        account = validate_startup(os.environ, database=DATABASE_PATH, evidence=EVIDENCE_PATH)
    except Phase4Blocked as error:
        print(type(error).__name__, file=sys.stderr)
        return 2
    try:
        with ExitStack() as resources:
            store = SQLiteExchangePersistence(
                SQLitePersistenceConfig(DATABASE_PATH, expected_schema_version=5)
            )
            resources.callback(store.close)
            guard = Phase4ReadOnlyTransport(httpx.HTTPTransport())
            client = httpx.Client(base_url=HOST, transport=guard, follow_redirects=False)
            resources.callback(client.close)
            adapter = BinanceSpotTestnetAdapter(
                BinanceSpotTestnetConfig(
                    os.environ["BINANCE_TESTNET_API_KEY"],
                    os.environ["BINANCE_TESTNET_SECRET_KEY"],
                    account,
                ),
                client=client,
                timestamp_provider=lambda: time_ns() // 1_000_000,
                identity_resolver=SQLiteOrderIdentityResolver(store),
            )
            resources.callback(adapter.close)
            result = run_phase4(
                store,
                adapter,
                guard,
                expected_account=account,
                run_id=str(uuid4()),
                now_ms=lambda: time_ns() // 1_000_000,
            )
            EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
            with EVIDENCE_PATH.open("x", encoding="utf-8") as evidence:
                evidence.write(result.render())
            print(result.render(), end="")
            return 0 if result.outcome is Phase4Outcome.PASS else 2
    except (KeyboardInterrupt, SystemExit):
        raise
    except Phase4Blocked as error:
        print(type(error).__name__, file=sys.stderr)
        return 2
    except Exception as error:  # noqa: BLE001
        print(type(error).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
