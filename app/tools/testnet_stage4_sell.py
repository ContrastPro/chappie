"""Dedicated one-shot Stage 4 managed-balance SELL operator.

Importing this module performs no I/O. Production execution is separately and
explicitly authorized; tests inject the reader, gateway, clock, and paths.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, localcontext
from enum import StrEnum
from pathlib import Path
from time import time_ns
from types import MappingProxyType
from typing import Protocol
from uuid import uuid4

import httpx

from app.core.logger import suppress_private_transport_logging
from app.exchange_execution import (
    DurableExchangeOrderLifecycle,
    ExchangeCommand,
    ExchangeOrderSide,
    PendingExposure,
    ReviewedStage4ManagedSellRequest,
    Stage4ManagedSellRiskEnvelope,
    derive_stage4_sell_economic_slot,
)
from app.exchange_execution.models import TransportOutcomeKind, require_account_fingerprint
from app.exchange_execution.ports import (
    AccountBalance,
    ExchangeInstrumentRules,
    SubmissionObservation,
)
from app.exchange_execution.stage4_sell_recovery import Stage4SellRecoveryReason
from app.exchange_runtime.submission import TestnetPreSubmitGate
from app.infrastructure.binance import (
    AccountTradeObservation,
    BinanceOrderRejectedError,
    BinanceSpotTestnetAdapter,
    BinanceSpotTestnetConfig,
    OpenOrderObservation,
    OrderIdentity,
)
from app.market.models import CandleSnapshot, MarketStreamKey
from app.persistence.canonical import canonical_sha256, deserialize_decimal
from app.persistence.exchange import SQLiteExchangePersistence
from app.persistence.models import SQLitePersistenceConfig
from app.persistence.runtime import state_to_snapshot
from app.persistence.sqlite import deserialize_portfolio_state
from app.portfolio import InstrumentRules, Portfolio, PortfolioConfig
from app.risk import PortfolioRiskPolicy, RiskManager
from app.risk.exchange import (
    ExchangeReconciliationStatus,
    ExchangeRiskContext,
    ExchangeRiskOutcome,
    ExchangeRiskPolicy,
    ReconciledAccountState,
    evaluate_exchange_risk,
)
from app.risk.models import ApprovedOrderParameters, RiskOrderSide
from app.trading.models import MarketSnapshot, OrderIntent, OrderIntentAction, RiskDecisionOutcome

AUTHORIZATION_ENV = "STAGE4_MANAGED_SELL_AUTHORIZED"
AUTHORIZATION_VALUE = "YES"
HOST = "https://testnet.binance.vision"
DATABASE_PATH = Path("data/m4_10_testnet_smoke.sqlite3")
EVIDENCE_PATH = Path("logs/stage4_managed_sell.log")
STREAM = MarketStreamKey("BTCUSDT", "1m")
FRESHNESS_MS = 30_000
MAX_REQUESTS = 11
ALLOWED = frozenset(
    {
        ("GET", "/api/v3/exchangeInfo"),
        ("GET", "/api/v3/ticker/price"),
        ("GET", "/api/v3/account"),
        ("GET", "/api/v3/openOrders"),
        ("GET", "/api/v3/myTrades"),
        ("POST", "/api/v3/order"),
    }
)


class Stage4SellError(RuntimeError):
    pass


class Stage4SellBlocked(Stage4SellError):
    def __init__(self, message: str, *, reason_code: str = "UNCLASSIFIED_BLOCK") -> None:
        super().__init__(message)
        self.reason_code = reason_code


class Stage4SellTransportViolation(Stage4SellBlocked):
    pass


class Stage4SellOutcome(StrEnum):
    PASS = "PASS"
    BLOCKED = "BLOCKED"


class Stage4SellGuardTransport(httpx.BaseTransport):
    """Fixed Testnet allowlist with two GET snapshots and at most one POST."""

    def __init__(self, transport: httpx.BaseTransport, *, max_requests: int = MAX_REQUESTS) -> None:
        self._transport = transport
        self._max_requests = max_requests
        self._counts: Counter[str] = Counter()
        self._post_count = 0
        self._closed = False

    @property
    def request_count(self) -> int:
        return sum(self._counts.values())

    @property
    def post_count(self) -> int:
        return self._post_count

    @property
    def endpoint_counts(self) -> Mapping[str, int]:
        return MappingProxyType(dict(sorted(self._counts.items())))

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if (
            request.url.scheme != "https"
            or request.url.host != "testnet.binance.vision"
            or request.url.port not in {None, 443}
        ):
            raise Stage4SellTransportViolation("Stage 4 SELL host/port is not approved")
        endpoint = (request.method, request.url.path)
        if endpoint not in ALLOWED or request.url.path == "/api/v3/order/test":
            raise Stage4SellTransportViolation("Stage 4 SELL method/path is not approved")
        if self.request_count >= self._max_requests:
            raise Stage4SellTransportViolation("Stage 4 SELL request budget exhausted")
        if request.method == "POST":
            if self._post_count:
                raise Stage4SellTransportViolation("Stage 4 SELL POST budget exhausted")
            self._post_count += 1
        self._counts[f"{request.method} {request.url.path}"] += 1
        response = self._transport.handle_request(request)
        if 300 <= response.status_code < 400:
            raise Stage4SellTransportViolation("Stage 4 SELL redirects are forbidden")
        return response

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._transport.close()


class Stage4SellReader(Protocol):
    def read_instrument_rules(self, stream_key: MarketStreamKey) -> ExchangeInstrumentRules: ...
    def read_reference_price(self, stream_key: MarketStreamKey) -> Decimal: ...
    def read_balances(self, assets: tuple[str, ...]) -> tuple[AccountBalance, ...]: ...
    def read_open_order_observations(
        self, *, stream_key: MarketStreamKey
    ) -> tuple[OpenOrderObservation, ...]: ...
    def read_account_trade_observations(
        self, *, stream_key: MarketStreamKey, start_at_ms: int
    ) -> tuple[AccountTradeObservation, ...]: ...


@dataclass(frozen=True, slots=True)
class SellSnapshot:
    observed_at_ms: int
    rules: ExchangeInstrumentRules
    reference_price: Decimal
    btc: AccountBalance
    usdt: AccountBalance
    open_orders_digest: str
    trades_digest: str
    managed_trade_identities: tuple[tuple[str, str], ...]

    @property
    def digest(self) -> str:
        return canonical_sha256(
            {
                "rules": self.rules.rules_version,
                "price": self.reference_price,
                "btc": (self.btc.free, self.btc.locked),
                "usdt": (self.usdt.free, self.usdt.locked),
                "open_orders": self.open_orders_digest,
                "trades": self.trades_digest,
                "managed_trades": self.managed_trade_identities,
            },
            algorithm_version="stage4-sell-snapshot/v1",
        )


@dataclass(frozen=True, slots=True)
class Stage4SellResult:
    outcome: Stage4SellOutcome
    run_id: str
    account_fingerprint: str
    authority_id: str
    durable_command_key: str
    client_order_id: str
    quantity: Decimal
    reference_price: Decimal
    reviewed_notional: Decimal
    rules_fingerprint: str
    portfolio_version: int
    session_before: int
    session_after: int
    durable_status: str
    reconciliation_required: bool
    request_count: int
    post_count: int
    endpoint_counts: Mapping[str, int]

    def render(self) -> str:
        fields = (
            ("run_id", self.run_id),
            ("host", HOST),
            ("account_fingerprint", self.account_fingerprint),
            ("authority_id", self.authority_id),
            ("durable_command_key", self.durable_command_key),
            ("client_order_id", self.client_order_id),
            ("symbol", "BTCUSDT"),
            ("side", "SELL"),
            ("quantity", self.quantity),
            ("reference_price", self.reference_price),
            ("reviewed_notional", self.reviewed_notional),
            ("rules_fingerprint", self.rules_fingerprint),
            ("portfolio_predecessor", self.portfolio_version),
            ("session_count_before", self.session_before),
            ("session_count_after", self.session_after),
            ("durable_status", self.durable_status),
            ("reconciliation_required", str(self.reconciliation_required).lower()),
            ("request_count", self.request_count),
            ("post_count", self.post_count),
            ("endpoint_counts", ",".join(f"{k}:{v}" for k, v in self.endpoint_counts.items())),
            ("outcome", self.outcome.value),
        )
        return "".join(f"{key}={value}\n" for key, value in fields)


def validate_startup(environ: Mapping[str, str], *, database: Path, evidence: Path) -> str:
    if environ.get(AUTHORIZATION_ENV) != AUTHORIZATION_VALUE:
        raise Stage4SellBlocked("Stage 4 SELL authorization is absent or invalid")
    if evidence.exists():
        raise Stage4SellBlocked("Stage 4 SELL evidence collision")
    if (
        not database.is_file()
        or Path(f"{database}-wal").exists()
        or Path(f"{database}-shm").exists()
    ):
        raise Stage4SellBlocked("Stage 4 SELL database is absent or has sidecars")
    fingerprint = environ.get("BINANCE_TESTNET_ACCOUNT_FINGERPRINT", "")
    try:
        require_account_fingerprint(fingerprint)
    except (TypeError, ValueError) as error:
        raise Stage4SellBlocked("Stage 4 SELL account fingerprint is invalid") from error
    if not environ.get("BINANCE_TESTNET_API_KEY") or not environ.get("BINANCE_TESTNET_SECRET_KEY"):
        raise Stage4SellBlocked("Stage 4 SELL credentials are absent")
    return fingerprint


def _observation_digest(values: tuple[object, ...], version: str) -> str:
    return canonical_sha256([repr(value) for value in values], algorithm_version=version)


def observe_snapshot(reader: Stage4SellReader, *, now_ms: int) -> SellSnapshot:
    rules = reader.read_instrument_rules(STREAM)
    price = reader.read_reference_price(STREAM)
    balances = {value.asset: value for value in reader.read_balances(("BTC", "USDT"))}
    if set(balances) != {"BTC", "USDT"} or not price.is_finite() or price <= 0:
        raise Stage4SellBlocked("Stage 4 SELL market/account snapshot is invalid")
    orders = reader.read_open_order_observations(stream_key=STREAM)
    if any(value.client_order_id.startswith("ctt1t-") for value in orders):
        raise Stage4SellBlocked("Stage 4 SELL observed managed-prefix open orders")
    trades = reader.read_account_trade_observations(stream_key=STREAM, start_at_ms=0)
    managed = tuple(
        sorted(
            (value.exchange_order_id, value.client_order_id)
            for value in trades
            if value.client_order_id is not None and value.client_order_id.startswith("ctt1t-")
        )
    )
    return SellSnapshot(
        now_ms,
        rules,
        price,
        balances["BTC"],
        balances["USDT"],
        _observation_digest(tuple(orders), "stage4-sell-open-orders/v1"),
        _observation_digest(tuple(trades), "stage4-sell-trades/v1"),
        managed,
    )


def _managed_portfolio(
    store: SQLiteExchangePersistence, rules: ExchangeInstrumentRules
) -> tuple[Portfolio, object, object]:
    roots = store._connection.execute("SELECT * FROM exchange_portfolio_roots").fetchall()
    currents = store._connection.execute("SELECT * FROM exchange_portfolio_current").fetchall()
    if len(roots) != 1 or len(currents) != 1:
        raise Stage4SellBlocked("Stage 4 SELL requires exactly one managed Portfolio")
    initial = deserialize_portfolio_state(roots[0]["state_payload"])
    state = deserialize_portfolio_state(currents[0]["state_payload"])
    instrument = InstrumentRules(
        state.stream_key,
        state.base_asset,
        state.quote_asset,
        Decimal("0.01"),
        rules.quantity_step,
        rules.min_quantity,
        rules.min_notional,
        8,
        8,
        rules.rules_version,
    )
    portfolio = Portfolio(
        PortfolioConfig(
            state.portfolio_id,
            state.stream_key,
            state.base_asset,
            state.quote_asset,
            initial.base_balance,
            initial.quote_balance,
            instrument,
        )
    )
    portfolio.synchronize_recovered_state(state_to_snapshot(state))
    return portfolio, roots[0], currents[0]


class _RiskIds:
    def new_risk_decision_id(self) -> str:
        return "stage4-sell-risk"


def _risk(
    store: SQLiteExchangePersistence,
    portfolio: Portfolio,
    snapshot: SellSnapshot,
    *,
    session_id: str,
    account_fingerprint: str,
    now_ms: int,
):
    marked = portfolio.preview_mark_to_market(
        snapshot.reference_price, max(now_ms, portfolio.snapshot.as_of_ms), "stage4-sell-risk"
    ).after
    candle = CandleSnapshot(
        now_ms - 60_000,
        now_ms - 1,
        snapshot.reference_price,
        snapshot.reference_price,
        snapshot.reference_price,
        snapshot.reference_price,
        Decimal(0),
    )
    market = MarketSnapshot(
        "stage4-sell-fresh",
        STREAM,
        now_ms,
        (candle,),
        candle,
        None,
        None,
        "stage4-sell-cycle",
        "stage4-sell-market/v1",
        0,
        False,
    )
    intent = OrderIntent(
        "stage4-sell-intent",
        "stage4-sell-reviewed",
        STREAM,
        now_ms,
        OrderIntentAction.EXIT_LONG,
        Decimal(0),
        "ADR-066 full managed exit",
        "stage4-managed-sell",
        "1",
        "stage4-sell-cycle",
    )
    manager = RiskManager(
        PortfolioRiskPolicy(
            Decimal(1000000),
            Decimal(1000000),
            policy_name="stage4-managed-sell",
            policy_version="1",
        ),
        InstrumentRules(
            STREAM,
            "BTC",
            "USDT",
            Decimal("0.01"),
            snapshot.rules.quantity_step,
            snapshot.rules.min_quantity,
            snapshot.rules.min_notional,
            8,
            8,
            snapshot.rules.rules_version,
        ),
        _RiskIds(),
    )
    decision = manager.evaluate(intent, marked, market)
    approved = decision.approved_order
    if (
        decision.outcome not in {RiskDecisionOutcome.APPROVED, RiskDecisionOutcome.MODIFIED}
        or not isinstance(approved, ApprovedOrderParameters)
        or approved.side is not RiskOrderSide.SELL
        or approved.quantity != marked.base_balance
    ):
        raise Stage4SellBlocked("financial Risk did not approve the exact managed SELL")
    session = store.load_session(session_id)
    reconciliation = store.read_reconciliation_state(
        store._connection.execute(
            "SELECT execution_root_id FROM exchange_execution_roots"
        ).fetchone()[0]
    )
    context = ExchangeRiskContext(
        ReconciledAccountState(
            account_fingerprint,
            STREAM,
            "BTC",
            "USDT",
            marked.base_balance,
            Decimal(0),
            marked.quote_balance,
            Decimal(0),
            ExchangeReconciliationStatus.SAFE,
            reconciliation.generation,
        ),
        PendingExposure(),
        session,
        None,
        0,
        0,
        0,
    )
    assessment = evaluate_exchange_risk(
        decision,
        marked,
        context,
        ExchangeRiskPolicy(
            Decimal(1000000),
            Decimal(1000000),
            Decimal(1000000),
            Decimal(1000000),
            2,
            1,
            Decimal(1),
            max_order_quantity=snapshot.rules.max_quantity,
        ),
    )
    if assessment.outcome is not ExchangeRiskOutcome.APPROVED:
        raise Stage4SellBlocked("exchange Risk did not approve the exact managed SELL")
    return assessment, context, marked


class _GatewayFacade:
    def __init__(self, gateway: object) -> None:
        self._gateway = gateway

    def submit_market_order(self, request):
        try:
            return self._gateway.submit_market_order(request)
        except BinanceOrderRejectedError:
            return SubmissionObservation(
                TransportOutcomeKind.DETERMINISTIC_REJECTION, request.client_order_id
            )


def run_stage4_sell(
    reader: Stage4SellReader,
    gateway: object,
    guard: Stage4SellGuardTransport,
    *,
    account_fingerprint: str,
    database_path: Path,
    run_id: str,
    now_ms: Callable[[], int],
) -> Stage4SellResult:
    suppress_private_transport_logging()
    first = observe_snapshot(reader, now_ms=now_ms())
    store = SQLiteExchangePersistence(
        SQLitePersistenceConfig(database_path, expected_schema_version=8)
    )
    try:
        store.recover_local()
        root = store._connection.execute("SELECT * FROM exchange_execution_roots").fetchone()
        session = store._connection.execute("SELECT * FROM exchange_sessions").fetchone()
        if (
            root is None
            or session is None
            or root["account_fingerprint"] != account_fingerprint
            or session["submission_count"] != 1
        ):
            raise Stage4SellBlocked("Stage 4 SELL durable predecessor is invalid")
        portfolio, _, current = _managed_portfolio(store, first.rules)
        quantity = portfolio.snapshot.base_balance
        if (
            quantity <= 0
            or first.btc.free + first.btc.locked < quantity
            or first.usdt.free + first.usdt.locked < portfolio.snapshot.quote_balance
        ):
            raise Stage4SellBlocked("Stage 4 SELL managed balances are not covered")
        with localcontext() as context:
            context.prec = 80
            notional = quantity * first.reference_price
        assessment, risk_context, _marked = _risk(
            store,
            portfolio,
            first,
            session_id=session["session_id"],
            account_fingerprint=account_fingerprint,
            now_ms=now_ms(),
        )
        source = store._connection.execute(
            "SELECT o.durable_command_key,t.exchange_trade_id FROM exchange_orders o JOIN exchange_trades t ON t.durable_command_key=o.durable_command_key WHERE o.side='BUY' AND o.status='FILLED'"
        ).fetchall()
        if len(source) != 1:
            raise Stage4SellBlocked("Stage 4 SELL source BUY lineage is invalid")
        source_identity = store._connection.execute(
            "SELECT exchange_order_id,client_order_id FROM exchange_orders "
            "WHERE durable_command_key=? AND side='BUY' AND status='FILLED'",
            (source[0]["durable_command_key"],),
        ).fetchone()
        if source_identity is None or first.managed_trade_identities != (tuple(source_identity),):
            raise Stage4SellBlocked("Stage 4 SELL exchange managed history conflicts")
        settlement_values = {
            "portfolio_version": current["portfolio_version"],
            "portfolio_digest": current["state_digest"],
            "reconciliation_generation": risk_context.account.reconciliation_generation,
            "pending": 0,
            "blocked": 0,
            "status": "SYNCHRONIZED",
        }
        settlement_digest = canonical_sha256(
            settlement_values, algorithm_version="stage4-sell-settlement-state/v1"
        )
        slot = derive_stage4_sell_economic_slot(
            root["execution_root_id"],
            account_fingerprint,
            current["portfolio_id"],
            current["portfolio_version"],
            current["state_digest"],
            source[0]["exchange_trade_id"],
        )
        risk_fingerprint = canonical_sha256(
            {
                "decision": assessment.risk_decision_id,
                "quantity": quantity,
                "notional": notional,
                "portfolio": current["state_digest"],
            },
            algorithm_version="stage4-sell-risk/v1",
        )
        ownership_digest = canonical_sha256(
            {
                "account": account_fingerprint,
                "exchange_btc": first.btc.free + first.btc.locked,
                "managed_btc": quantity,
                "portfolio": current["state_digest"],
            },
            algorithm_version="stage4-sell-ownership/v1",
        )
        stable = canonical_sha256(
            {"slot": slot, "policy": "stage4-managed-full-exit/v1"},
            algorithm_version="stage4-sell-stable/v1",
        )
        command_key = canonical_sha256(
            {
                "stable": stable,
                "quantity": quantity,
                "price": first.reference_price,
                "rules": first.rules.rules_version,
                "config": root["configuration_fingerprint"],
            },
            algorithm_version="stage4-sell-command/v1",
        )
        command = ExchangeCommand(
            command_key,
            stable,
            slot,
            STREAM,
            ExchangeOrderSide.SELL,
            quantity,
            first.reference_price,
            Decimal(0),
            notional,
            risk_fingerprint,
            first.rules.rules_version,
            root["configuration_fingerprint"],
            now_ms(),
            run_id,
        )
        request = ReviewedStage4ManagedSellRequest(
            canonical_sha256(
                {"run": run_id, "slot": slot}, algorithm_version="stage4-sell-authority/v1"
            ),
            run_id,
            root["execution_root_id"],
            session["session_id"],
            account_fingerprint,
            current["portfolio_id"],
            current["portfolio_version"],
            current["state_digest"],
            quantity,
            1,
            2,
            0,
            risk_context.account.reconciliation_generation,
            settlement_digest,
            source[0]["durable_command_key"],
            source[0]["exchange_trade_id"],
            slot,
            first.rules,
            risk_fingerprint,
            root["configuration_fingerprint"],
            ownership_digest,
            command,
            Stage4ManagedSellRiskEnvelope(
                quantity,
                notional,
                deserialize_decimal(session["submitted_notional"]),
                deserialize_decimal(session["executed_turnover"]),
            ),
            now_ms(),
        )
        receipt = store.authorize_stage4_managed_sell_command(request)
        if receipt.claim is None:
            reasons = (Stage4SellRecoveryReason.CLAIM_FAILURE.value,)
            store.record_stage4_sell_recovery_required(
                command_key,
                reasons=reasons,
                snapshot_stage="FINAL_GATE",
                snapshot_digest=None,
                occurred_at_ms=now_ms(),
            )
            raise Stage4SellBlocked(
                "Stage 4 SELL live claim is unavailable", reason_code=reasons[0]
            )
        try:
            second = observe_snapshot(reader, now_ms=now_ms())
        except Stage4SellBlocked as error:
            reasons = (Stage4SellRecoveryReason.SNAPSHOT_OBSERVATION_FAILED.value,)
            store.record_stage4_sell_recovery_required(
                command_key,
                reasons=reasons,
                snapshot_stage="SNAPSHOT_B",
                snapshot_digest=None,
                occurred_at_ms=now_ms(),
            )
            raise Stage4SellBlocked(str(error), reason_code=reasons[0]) from error
        portfolio2, _, current2 = _managed_portfolio(store, second.rules)
        changed: set[str] = set()
        if first.rules.rules_version != second.rules.rules_version:
            changed.add(Stage4SellRecoveryReason.SNAPSHOT_RULE_CHANGED.value)
        if first.reference_price != second.reference_price:
            changed.add(Stage4SellRecoveryReason.SNAPSHOT_PRICE_CHANGED.value)
        if (first.btc, first.usdt) != (second.btc, second.usdt):
            changed.add(Stage4SellRecoveryReason.SNAPSHOT_BALANCE_CHANGED.value)
        if (
            first.open_orders_digest != second.open_orders_digest
            or first.trades_digest != second.trades_digest
            or first.managed_trade_identities != second.managed_trade_identities
        ):
            changed.add(Stage4SellRecoveryReason.SNAPSHOT_HISTORY_CHANGED.value)
        if (
            current2["state_digest"] != current["state_digest"]
            or portfolio2.snapshot.base_balance != quantity
        ):
            changed.add(Stage4SellRecoveryReason.PORTFOLIO_CHANGED.value)
        if now_ms() - second.observed_at_ms > FRESHNESS_MS:
            changed.add(Stage4SellRecoveryReason.SNAPSHOT_OBSERVATION_FAILED.value)
        if changed:
            reasons = tuple(sorted(changed))
            store.record_stage4_sell_recovery_required(
                command_key,
                reasons=reasons,
                snapshot_stage="SNAPSHOT_B",
                snapshot_digest=second.digest,
                occurred_at_ms=now_ms(),
            )
            raise Stage4SellBlocked("Stage 4 SELL second snapshot changed", reason_code=reasons[0])
        lifecycle = DurableExchangeOrderLifecycle(
            store, _GatewayFacade(gateway), timestamp_provider=now_ms
        )
        gate = TestnetPreSubmitGate(
            store,
            lifecycle,
            execution_root_id=root["execution_root_id"],
            max_authorization_age_ms=FRESHNESS_MS,
        )
        gate_result, durable = gate.authorize_and_submit(
            command_key,
            assessment=assessment,
            context=risk_context,
            capability=None,
            expected_rules_fingerprint=first.rules.rules_version,
            now_ms=now_ms(),
            stage4_sell_claim=receipt.claim,
        )
        if not gate_result.authorized or durable is None:
            mapped: set[str] = {Stage4SellRecoveryReason.FINAL_GATE_REJECTED.value}
            for reason in gate_result.reasons:
                if reason.value == "RULES_MISMATCH":
                    mapped.add(Stage4SellRecoveryReason.SNAPSHOT_RULE_CHANGED.value)
                elif reason.value == "RISK_CONTEXT_STALE":
                    mapped.add(Stage4SellRecoveryReason.SESSION_CHANGED.value)
                elif reason.value == "RECONCILIATION_UNSAFE":
                    mapped.add(Stage4SellRecoveryReason.RECONCILIATION_CHANGED.value)
                elif reason.value == "CONTROL_UNSAFE":
                    mapped.add(Stage4SellRecoveryReason.CONTROL_CHANGED.value)
                elif reason.value in {"PORTFOLIO_RECOVERY_UNSAFE", "SETTLEMENT_UNSAFE"}:
                    mapped.add(Stage4SellRecoveryReason.PORTFOLIO_CHANGED.value)
            reasons = tuple(sorted(mapped))
            store.record_stage4_sell_recovery_required(
                command_key,
                reasons=reasons,
                snapshot_stage="FINAL_GATE",
                snapshot_digest=second.digest,
                occurred_at_ms=now_ms(),
            )
            raise Stage4SellBlocked(
                "Stage 4 SELL final gate blocked submission", reason_code=reasons[0]
            )
        status = durable.order.status.value
        outcome = Stage4SellOutcome.PASS if status == "ACKNOWLEDGED" else Stage4SellOutcome.BLOCKED
        return Stage4SellResult(
            outcome,
            run_id,
            account_fingerprint,
            request.authority_id,
            command_key,
            receipt.client_order_id,
            quantity,
            first.reference_price,
            notional,
            first.rules.rules_version,
            current["portfolio_version"],
            1,
            store.load_session(session["session_id"]).submission_count,
            status,
            status == "RECONCILIATION_REQUIRED",
            guard.request_count,
            guard.post_count,
            guard.endpoint_counts,
        )
    finally:
        store.close()


class _ProductionReader:
    def __init__(self, adapter: BinanceSpotTestnetAdapter, client: httpx.Client) -> None:
        self._adapter, self._client = adapter, client

    def read_instrument_rules(self, stream_key):
        return self._adapter.read_instrument_rules(stream_key)

    def read_balances(self, assets):
        return self._adapter.read_balances(assets)

    def read_open_order_observations(self, *, stream_key):
        return self._adapter.read_open_order_observations(stream_key=stream_key)

    def read_account_trade_observations(self, *, stream_key, start_at_ms):
        return self._adapter.read_account_trade_observations(
            stream_key=stream_key, start_at_ms=start_at_ms
        )

    def read_reference_price(self, stream_key):
        response = self._client.get("/api/v3/ticker/price", params={"symbol": stream_key.symbol})
        response.raise_for_status()
        payload = response.json()
        value = Decimal(payload["price"])
        if payload.get("symbol") != stream_key.symbol or not value.is_finite() or value <= 0:
            raise Stage4SellBlocked("Stage 4 SELL ticker is invalid")
        return value


class _NoKnownIdentity:
    def by_client_order_id(self, client_order_id: str):
        return None

    def by_exchange_order_id(self, exchange_order_id: str):
        return None


class _DurableIdentityResolver:
    """Read only the fixed durable order identity needed to normalize account trades."""

    def __init__(self, database: Path) -> None:
        self._database = database

    def _find(self, column: str, value: str) -> OrderIdentity | None:
        if column not in {"client_order_id", "exchange_order_id"}:
            raise Stage4SellBlocked("Stage 4 SELL identity lookup is invalid")
        connection = sqlite3.connect(f"file:{self._database}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                f"SELECT durable_command_key,client_order_id,side,original_quantity,"
                f"created_at_ms FROM exchange_orders WHERE {column}=?",
                (value,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return OrderIdentity(
            row["durable_command_key"],
            row["client_order_id"],
            ExchangeOrderSide(row["side"]),
            deserialize_decimal(row["original_quantity"]),
            row["created_at_ms"],
        )

    def by_client_order_id(self, client_order_id: str):
        return self._find("client_order_id", client_order_id)

    def by_exchange_order_id(self, exchange_order_id: str):
        return self._find("exchange_order_id", exchange_order_id)


def _publish_evidence(path: Path, payload: str) -> None:
    if not payload.endswith("outcome=PASS\n"):
        raise Stage4SellError("Stage 4 SELL PASS marker must be last")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            written = stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
            if written != len(payload):
                raise Stage4SellError("Stage 4 SELL evidence short write")
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    suppress_private_transport_logging()
    try:
        fingerprint = validate_startup(os.environ, database=DATABASE_PATH, evidence=EVIDENCE_PATH)
    except Stage4SellBlocked as error:
        print(f"{type(error).__name__}:{error.reason_code}", file=sys.stderr)
        return 2
    guard = Stage4SellGuardTransport(httpx.HTTPTransport(retries=0))
    try:
        client = httpx.Client(base_url=HOST, transport=guard, follow_redirects=False)
    except (KeyboardInterrupt, SystemExit):
        guard.close()
        raise
    except Exception as error:  # noqa: BLE001 - construction failure taxonomy only
        guard.close()
        print(type(error).__name__, file=sys.stderr)
        return 1
    try:
        adapter = BinanceSpotTestnetAdapter(
            BinanceSpotTestnetConfig(
                os.environ["BINANCE_TESTNET_API_KEY"],
                os.environ["BINANCE_TESTNET_SECRET_KEY"],
                fingerprint,
            ),
            client=client,
            timestamp_provider=lambda: time_ns() // 1_000_000,
            identity_resolver=_DurableIdentityResolver(DATABASE_PATH),
        )
    except (KeyboardInterrupt, SystemExit):
        client.close()
        raise
    except Exception as error:  # noqa: BLE001 - construction failure taxonomy only
        client.close()
        print(type(error).__name__, file=sys.stderr)
        return 1
    try:
        try:
            result = run_stage4_sell(
                _ProductionReader(adapter, client),
                adapter,
                guard,
                account_fingerprint=fingerprint,
                database_path=DATABASE_PATH,
                run_id=str(uuid4()),
                now_ms=lambda: time_ns() // 1_000_000,
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Stage4SellBlocked as error:
            print(f"{type(error).__name__}:{error.reason_code}", file=sys.stderr)
            return 2
        except Exception as error:  # noqa: BLE001
            print(type(error).__name__, file=sys.stderr)
            return 1
    finally:
        adapter.close()
        client.close()
    try:
        rendered = result.render()
        _publish_evidence(EVIDENCE_PATH, rendered)
        print(rendered, end="")
        return 0 if result.outcome is Stage4SellOutcome.PASS else 2
    except Exception as error:  # noqa: BLE001 - sanitized evidence failure
        print(type(error).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
