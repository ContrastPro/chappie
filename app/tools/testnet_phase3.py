"""Dedicated one-shot M4.10 Phase 3 operator harness.

Importing this module performs no I/O. The production entrypoint remains separately authorized.
"""

from __future__ import annotations

import os
import sys
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from decimal import ROUND_CEILING, Decimal, localcontext
from enum import StrEnum
from pathlib import Path
from time import time_ns
from types import MappingProxyType
from typing import Protocol
from uuid import uuid4

import httpx

from app.core.logger import suppress_private_transport_logging
from app.exchange_execution import (
    DeterministicClientOrderIdFactory,
    DurableExchangeOrderLifecycle,
    ExchangeCommand,
    ExchangeOrderSide,
    ExecutionEnvironment,
    ExecutionSession,
    ExecutionSessionState,
    PendingExposure,
    Phase3BalanceScope,
    Phase3RiskEnvelope,
    ReviewedPhase3SmokeRequest,
    derive_phase3_economic_slot,
)
from app.exchange_execution.models import TransportOutcomeKind, require_account_fingerprint
from app.exchange_execution.ports import (
    AccountBalance,
    ExchangeInstrumentRules,
    SubmissionObservation,
)
from app.exchange_runtime.settlement import SQLiteExchangeSettlement
from app.exchange_runtime.submission import TestnetPreSubmitGate
from app.infrastructure.binance import (
    AccountTradeObservation,
    BinanceOrderRejectedError,
    BinanceSpotTestnetAdapter,
    BinanceSpotTestnetConfig,
    OpenOrderObservation,
)
from app.market.models import CandleSnapshot, MarketStreamKey
from app.persistence.canonical import canonical_sha256
from app.persistence.exchange import SQLiteExchangePersistence
from app.persistence.exchange_models import (
    ExchangeExecutionRoot,
    ExchangeReconciliationStatus,
    PersistentExchangeReconciliationRun,
)
from app.persistence.models import SQLitePersistenceConfig
from app.portfolio import InstrumentRules, Portfolio, PortfolioConfig
from app.risk import PortfolioRiskPolicy, RiskManager
from app.risk.exchange import (
    ExchangeReconciliationStatus as RiskReconciliationStatus,
)
from app.risk.exchange import (
    ExchangeRiskContext,
    ExchangeRiskOutcome,
    ExchangeRiskPolicy,
    ReconciledAccountState,
    evaluate_exchange_risk,
)
from app.risk.models import ApprovedOrderParameters, RiskOrderSide
from app.trading.models import MarketSnapshot, OrderIntent, OrderIntentAction, RiskDecisionOutcome

AUTHORIZATION_ENV = "M4_10_PHASE3_AUTHORIZED"
AUTHORIZATION_VALUE = "YES"
SPOT_TESTNET_HOST = "https://testnet.binance.vision"
DATABASE_PATH = Path("data/m4_10_testnet_smoke.sqlite3")
EVIDENCE_PATH = Path("logs/m4_10_phase3.log")
COLLISION_PATHS = (
    DATABASE_PATH,
    Path(f"{DATABASE_PATH}-wal"),
    Path(f"{DATABASE_PATH}-shm"),
    EVIDENCE_PATH,
)
STREAM = MarketStreamKey("BTCUSDT", "1m")
TARGET_NOTIONAL = Decimal(10)
MAX_NOTIONAL = Decimal(12)
MANAGED_ALLOCATION = Decimal(25)
MIN_RESERVE = Decimal(13)
MAX_FEE_RATE = Decimal("0.001")
MAX_SLIPPAGE_BPS = Decimal(10)
FRESHNESS_WINDOW_MS = 30_000
MAX_REQUESTS = 6
ALLOWED_ENDPOINTS = frozenset(
    {
        ("GET", "/api/v3/exchangeInfo"),
        ("GET", "/api/v3/ticker/price"),
        ("GET", "/api/v3/account"),
        ("GET", "/api/v3/openOrders"),
        ("GET", "/api/v3/myTrades"),
        ("POST", "/api/v3/order"),
    }
)


class Phase3OperatorError(RuntimeError):
    pass


class Phase3OperatorBlocked(Phase3OperatorError):
    pass


class Phase3TransportViolation(Phase3OperatorBlocked):
    pass


class Phase3Outcome(StrEnum):
    PASS = "PASS"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


class Phase3GuardTransport(httpx.BaseTransport):
    """Fixed-host whitelist with one POST and no retry/fallback path."""

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
            raise Phase3TransportViolation("Phase 3 host/port is not approved")
        method_path = (request.method, request.url.path)
        if method_path not in ALLOWED_ENDPOINTS or request.url.path == "/api/v3/order/test":
            raise Phase3TransportViolation("Phase 3 method or endpoint is not approved")
        if self.request_count >= self._max_requests:
            raise Phase3TransportViolation("Phase 3 request budget exhausted")
        if request.method == "POST":
            if self._post_count:
                raise Phase3TransportViolation("Phase 3 submission budget exhausted")
            self._post_count += 1
        self._counts[f"{request.method} {request.url.path}"] += 1
        response = self._transport.handle_request(request)
        if 300 <= response.status_code < 400:
            raise Phase3TransportViolation("Phase 3 redirects are forbidden")
        return response

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._transport.close()


class Phase3PreflightReader(Protocol):
    def read_instrument_rules(self, stream_key: MarketStreamKey) -> ExchangeInstrumentRules: ...
    def read_reference_price(self, stream_key: MarketStreamKey) -> Decimal: ...
    def read_balances(self, assets: tuple[str, ...]) -> tuple[AccountBalance, ...]: ...
    def read_open_order_observations(
        self, *, stream_key: MarketStreamKey
    ) -> tuple[OpenOrderObservation, ...]: ...
    def read_account_trade_observations(
        self, *, stream_key: MarketStreamKey, start_at_ms: int
    ) -> tuple[AccountTradeObservation, ...]: ...


class _Phase3RiskIdFactory:
    def new_risk_decision_id(self) -> str:
        return "phase3-risk"


class Phase3GatewayFacade:
    """Translate only explicit safe Binance rejection into lifecycle semantics."""

    def __init__(self, gateway: object) -> None:
        self._gateway = gateway

    def submit_market_order(self, request):
        try:
            return self._gateway.submit_market_order(request)
        except BinanceOrderRejectedError:
            return SubmissionObservation(
                TransportOutcomeKind.DETERMINISTIC_REJECTION, request.client_order_id
            )


@dataclass(frozen=True, slots=True)
class Phase3Preflight:
    observed_at_ms: int
    rules: ExchangeInstrumentRules
    reference_price: Decimal
    btc: AccountBalance
    usdt: AccountBalance
    quantity: Decimal
    notional: Decimal
    rules_fingerprint: str


@dataclass(frozen=True, slots=True)
class Phase3Result:
    outcome: Phase3Outcome
    run_id: str
    observed_at_ms: int
    account_fingerprint: str
    request_count: int
    endpoint_counts: Mapping[str, int]
    post_count: int
    authority_id: str | None = None
    durable_command_key: str | None = None
    client_order_id: str | None = None
    quantity: Decimal | None = None
    reference_price: Decimal | None = None
    reviewed_notional: Decimal | None = None
    rules_fingerprint: str | None = None
    durable_status: str | None = None
    reconciliation_required: bool = False
    stop_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "endpoint_counts", MappingProxyType(dict(self.endpoint_counts)))

    def render(self) -> str:
        values = (
            f"outcome={self.outcome.value}",
            f"run_id={self.run_id}",
            f"observed_at_ms={self.observed_at_ms}",
            f"host={SPOT_TESTNET_HOST}",
            f"account_fingerprint={self.account_fingerprint}",
            f"request_count={self.request_count}",
            f"post_count={self.post_count}",
            "endpoint_counts=" + ",".join(f"{k}:{v}" for k, v in self.endpoint_counts.items()),
            f"authority_id={self.authority_id}",
            f"durable_command_key={self.durable_command_key}",
            f"client_order_id={self.client_order_id}",
            "symbol=BTCUSDT",
            "side=BUY",
            f"quantity={self.quantity}",
            f"reference_price={self.reference_price}",
            f"reviewed_notional={self.reviewed_notional}",
            f"rules_fingerprint={self.rules_fingerprint}",
            "managed_allocation=25",
            "max_notional=12",
            "min_reserve=13",
            f"durable_status={self.durable_status}",
            f"reconciliation_required={self.reconciliation_required}",
            "stop_reasons=" + ",".join(self.stop_reasons),
        )
        return "\n".join(values) + "\n"


def validate_startup(
    environ: Mapping[str, str], *, database_path: Path, evidence_path: Path
) -> str:
    if environ.get(AUTHORIZATION_ENV) != AUTHORIZATION_VALUE:
        raise Phase3OperatorBlocked("Phase 3 authorization token is absent or invalid")
    paths = (
        database_path,
        Path(f"{database_path}-wal"),
        Path(f"{database_path}-shm"),
        evidence_path,
    )
    if any(path.exists() for path in paths):
        raise Phase3OperatorBlocked("Phase 3 artifact collision")
    fingerprint = environ.get("BINANCE_TESTNET_ACCOUNT_FINGERPRINT", "")
    try:
        require_account_fingerprint(fingerprint)
    except (TypeError, ValueError) as error:
        raise Phase3OperatorBlocked("Phase 3 account fingerprint is absent or invalid") from error
    if not environ.get("BINANCE_TESTNET_API_KEY") or not environ.get("BINANCE_TESTNET_SECRET_KEY"):
        raise Phase3OperatorBlocked("Phase 3 credentials are absent")
    return fingerprint


def calculate_quantity(rules: ExchangeInstrumentRules, price: Decimal) -> tuple[Decimal, Decimal]:
    if not isinstance(price, Decimal) or not price.is_finite() or price <= 0:
        raise Phase3OperatorBlocked("Phase 3 reference price is invalid")
    step = rules.quantity_step
    with localcontext() as context:
        context.prec = 80
        raw = max(TARGET_NOTIONAL / price, rules.min_quantity, rules.min_notional / price)
        quantity = (raw / step).to_integral_value(rounding=ROUND_CEILING) * step
        notional = quantity * price
        allowance = notional * MAX_FEE_RATE + notional * MAX_SLIPPAGE_BPS / Decimal(10_000)
    if quantity < rules.min_quantity or quantity % step != 0:
        raise Phase3OperatorBlocked("Phase 3 quantity violates exchange rules")
    if rules.max_quantity is not None and quantity > rules.max_quantity:
        raise Phase3OperatorBlocked("Phase 3 quantity exceeds exchange maxQty")
    if notional < rules.min_notional:
        raise Phase3OperatorBlocked("Phase 3 notional is below minNotional")
    if notional + allowance > MAX_NOTIONAL:
        raise Phase3OperatorBlocked("Phase 3 quantity exceeds reviewed ceiling")
    if MANAGED_ALLOCATION - notional - allowance < MIN_RESERVE:
        raise Phase3OperatorBlocked("Phase 3 retained reserve would be insufficient")
    return quantity, notional


def observe_preflight(reader: Phase3PreflightReader, *, now_ms: int) -> Phase3Preflight:
    rules = reader.read_instrument_rules(STREAM)
    price = reader.read_reference_price(STREAM)
    balances = {value.asset: value for value in reader.read_balances(("BTC", "USDT"))}
    if set(balances) != {"BTC", "USDT"}:
        raise Phase3OperatorBlocked("Phase 3 balance scope is incomplete")
    if reader.read_open_order_observations(stream_key=STREAM):
        raise Phase3OperatorBlocked("Phase 3 observed unexpected open orders")
    history = reader.read_account_trade_observations(stream_key=STREAM, start_at_ms=0)
    if any(
        value.client_order_id and value.client_order_id.startswith("ctt1t-") for value in history
    ):
        raise Phase3OperatorBlocked("Phase 3 observed managed-prefix trade history")
    if any(value.commission_asset not in {"BTC", "USDT"} for value in history):
        raise Phase3OperatorBlocked("Phase 3 observed unsupported commission history")
    if history:
        raise Phase3OperatorBlocked("Phase 3 observed unexpected trade history")
    if balances["USDT"].free < MANAGED_ALLOCATION:
        raise Phase3OperatorBlocked("Phase 3 exchange USDT coverage is insufficient")
    quantity, notional = calculate_quantity(rules, price)
    rules_fingerprint = canonical_sha256(
        {
            "symbol": STREAM.symbol,
            "step": rules.quantity_step,
            "min_quantity": rules.min_quantity,
            "min_notional": rules.min_notional,
            "version": rules.rules_version,
        },
        algorithm_version="phase3-rules/v1",
    )
    return Phase3Preflight(
        now_ms,
        rules,
        price,
        balances["BTC"],
        balances["USDT"],
        quantity,
        notional,
        rules_fingerprint,
    )


def _approved_risk(
    store: SQLiteExchangePersistence,
    managed: Portfolio,
    preflight: Phase3Preflight,
    *,
    root_id: str,
    session_id: str,
    now_ms: int,
) -> tuple[object, ExchangeRiskContext]:
    rules = InstrumentRules(
        STREAM,
        "BTC",
        "USDT",
        Decimal("0.01"),
        preflight.rules.quantity_step,
        preflight.rules.min_quantity,
        preflight.rules.min_notional,
        8,
        8,
        preflight.rules.rules_version,
    )
    financial_portfolio = replace(managed.snapshot, as_of_ms=now_ms)
    candle = CandleSnapshot(
        now_ms - 60_000,
        now_ms - 1,
        preflight.reference_price,
        preflight.reference_price,
        preflight.reference_price,
        preflight.reference_price,
        Decimal(0),
    )
    market = MarketSnapshot(
        "phase3-fresh-preflight",
        STREAM,
        now_ms,
        (candle,),
        candle,
        None,
        None,
        "phase3-risk-cycle",
        "phase3-market/v1",
        0,
        False,
    )
    intent = OrderIntent(
        "phase3-intent",
        "phase3-reviewed-signal",
        STREAM,
        now_ms,
        OrderIntentAction.ENTER_LONG,
        preflight.notional / MANAGED_ALLOCATION,
        "M4.10 reviewed one-shot BUY",
        "m4-10-phase3",
        "1",
        "phase3-risk-cycle",
    )
    decision = RiskManager(
        PortfolioRiskPolicy(
            MAX_NOTIONAL,
            MAX_NOTIONAL,
            policy_name="m4-10-phase3-reviewed-financial-risk",
            policy_version="1",
            min_cash_reserve=MIN_RESERVE,
            max_fee_rate=MAX_FEE_RATE,
            max_slippage_bps=MAX_SLIPPAGE_BPS,
        ),
        rules,
        _Phase3RiskIdFactory(),
    ).evaluate(intent, financial_portfolio, market)
    if (
        decision.outcome not in {RiskDecisionOutcome.APPROVED, RiskDecisionOutcome.MODIFIED}
        or not isinstance(decision.approved_order, ApprovedOrderParameters)
        or decision.approved_order.side is not RiskOrderSide.BUY
        or decision.approved_order.quantity != preflight.quantity
    ):
        raise Phase3OperatorBlocked("Phase 3 existing financial Risk blocked reviewed quantity")
    context = ExchangeRiskContext(
        ReconciledAccountState(
            store.load_bootstrap(root_id).root.account_fingerprint,
            STREAM,
            "BTC",
            "USDT",
            Decimal(0),
            Decimal(0),
            Decimal(25),
            Decimal(0),
            RiskReconciliationStatus.SAFE,
            1,
        ),
        PendingExposure(),
        store.load_session(session_id),
        None,
        0,
        0,
        0,
    )
    assessment = evaluate_exchange_risk(
        decision,
        managed.snapshot,
        context,
        ExchangeRiskPolicy(
            MAX_NOTIONAL, MAX_NOTIONAL, MAX_NOTIONAL, MAX_NOTIONAL, 1, 1, Decimal(1), MIN_RESERVE
        ),
    )
    if assessment.outcome is not ExchangeRiskOutcome.APPROVED:
        raise Phase3OperatorBlocked("Phase 3 existing exchange Risk blocked the command")
    return assessment, context


def run_phase3(
    reader: Phase3PreflightReader,
    gateway: object,
    guard: Phase3GuardTransport,
    *,
    account_fingerprint: str,
    database_path: Path,
    run_id: str,
    now_ms: Callable[[], int],
) -> Phase3Result:
    """Run the production-shaped one-shot graph; tests inject all transport."""

    suppress_private_transport_logging()
    preflight = observe_preflight(reader, now_ms=now_ms())
    if now_ms() - preflight.observed_at_ms > FRESHNESS_WINDOW_MS:
        raise Phase3OperatorBlocked("Phase 3 preflight is stale")
    root_id = canonical_sha256(
        {"account": account_fingerprint, "run": "m4.10-phase3"},
        algorithm_version="phase3-root/v1",
    )
    session_id = canonical_sha256(
        {"root": root_id, "session": "one-shot"}, algorithm_version="phase3-session/v1"
    )
    config_fingerprint = canonical_sha256(
        {
            "host": SPOT_TESTNET_HOST,
            "rules": preflight.rules_fingerprint,
            "fee_rate": MAX_FEE_RATE,
            "slippage_bps": MAX_SLIPPAGE_BPS,
        },
        algorithm_version="phase3-execution-config/v1",
    )
    store = SQLiteExchangePersistence(
        SQLitePersistenceConfig(database_path, expected_schema_version=5)
    )
    try:
        store.initialize_execution(
            ExchangeExecutionRoot(
                root_id, account_fingerprint, STREAM, config_fingerprint, now_ms()
            ),
            ExecutionSession(
                session_id, account_fingerprint, now_ms(), ExecutionSessionState.ACTIVE
            ),
            audit_identifier=run_id,
        )
        store.record_reconciliation_run(
            PersistentExchangeReconciliationRun(
                canonical_sha256({"run": run_id}, algorithm_version="phase3-reconciliation/v1"),
                root_id,
                session_id,
                account_fingerprint,
                0,
                1,
                ExchangeReconciliationStatus.SAFE,
                0,
                0,
                0,
                0,
                2,
                (),
                preflight.observed_at_ms,
                now_ms(),
                0,
                now_ms(),
                True,
                True,
                "BTCUSDT",
            )
        )
        managed = Portfolio(
            PortfolioConfig(
                "m4-10-phase3-managed",
                STREAM,
                "BTC",
                "USDT",
                Decimal(0),
                MANAGED_ALLOCATION,
                InstrumentRules(
                    STREAM,
                    "BTC",
                    "USDT",
                    Decimal("0.01"),
                    preflight.rules.quantity_step,
                    preflight.rules.min_quantity,
                    preflight.rules.min_notional,
                    8,
                    8,
                    preflight.rules.rules_version,
                ),
            )
        )
        assessment, context = _approved_risk(
            store, managed, preflight, root_id=root_id, session_id=session_id, now_ms=now_ms()
        )
        stable = canonical_sha256(
            {"root": root_id, "slot": "first-managed-btcusdt-buy"},
            algorithm_version="phase3-stable-decision/v1",
        )
        economic = derive_phase3_economic_slot(root_id, account_fingerprint)
        command_key = canonical_sha256(
            {
                "stable": stable,
                "quantity": preflight.quantity,
                "price": preflight.reference_price,
                "rules": preflight.rules_fingerprint,
                "config": config_fingerprint,
            },
            algorithm_version="phase3-command/v1",
        )
        client_id = DeterministicClientOrderIdFactory().create_client_order_id(
            environment=ExecutionEnvironment.TESTNET,
            venue="binance-spot",
            account_fingerprint=account_fingerprint,
            durable_command_key=command_key,
        )
        risk_fingerprint = canonical_sha256(
            {"assessment": assessment.outcome, "notional": preflight.notional},
            algorithm_version="phase3-risk-authorization/v1",
        )
        command = ExchangeCommand(
            command_key,
            stable,
            economic,
            STREAM,
            ExchangeOrderSide.BUY,
            preflight.quantity,
            preflight.reference_price,
            preflight.notional / MANAGED_ALLOCATION,
            preflight.notional,
            risk_fingerprint,
            preflight.rules_fingerprint,
            config_fingerprint,
            now_ms(),
            run_id,
        )
        envelope = Phase3RiskEnvelope(
            assumptions_fingerprint=canonical_sha256(
                {"fee": MAX_FEE_RATE, "slippage": MAX_SLIPPAGE_BPS},
                algorithm_version="phase3-assumptions/v1",
            )
        )
        authority_created_at = now_ms()
        reviewed = ReviewedPhase3SmokeRequest(
            canonical_sha256({"run": run_id}, algorithm_version="phase3-authority/v1"),
            run_id,
            root_id,
            session_id,
            account_fingerprint,
            0,
            1,
            economic,
            client_id,
            command,
            envelope,
            Phase3BalanceScope(
                account_fingerprint,
                preflight.btc.free,
                preflight.btc.locked,
                preflight.usdt.free,
                preflight.usdt.locked,
                MANAGED_ALLOCATION,
                preflight.observed_at_ms,
            ),
            managed.snapshot.portfolio_id,
            authority_created_at,
        )
        receipt = store.authorize_phase3_smoke_command(reviewed)
        if receipt.claim is None:
            raise Phase3OperatorBlocked("Phase 3 process claim is unavailable")
        managed.synchronize_recovered_state(
            replace(
                managed.snapshot,
                as_of_ms=authority_created_at,
                correlation_id=run_id,
            )
        )
        if now_ms() - preflight.observed_at_ms > FRESHNESS_WINDOW_MS:
            raise Phase3OperatorBlocked("Phase 3 preflight became stale after durable claim")
        lifecycle = DurableExchangeOrderLifecycle(
            store, Phase3GatewayFacade(gateway), timestamp_provider=now_ms
        )
        settlement = SQLiteExchangeSettlement(
            store,
            managed,
            execution_root_id=root_id,
            session_id=session_id,
            config_fingerprint=config_fingerprint,
            timestamp_provider=now_ms,
        )
        settlement.initialize()
        gate = TestnetPreSubmitGate(
            store,
            lifecycle,
            execution_root_id=root_id,
            max_authorization_age_ms=FRESHNESS_WINDOW_MS,
            settlement=settlement,
        )
        gate_result, durable = gate.authorize_and_submit(
            command_key,
            assessment=assessment,
            context=context,
            capability=None,
            expected_rules_fingerprint=preflight.rules_fingerprint,
            now_ms=now_ms(),
            phase3_claim=receipt.claim,
        )
        if not gate_result.authorized or durable is None:
            raise Phase3OperatorBlocked("Phase 3 final gate blocked submission")
        status = durable.order.status.value
        ambiguous = status == "RECONCILIATION_REQUIRED"
        outcome = Phase3Outcome.PASS if status == "ACKNOWLEDGED" else Phase3Outcome.BLOCKED
        return Phase3Result(
            outcome,
            run_id,
            preflight.observed_at_ms,
            account_fingerprint,
            guard.request_count,
            guard.endpoint_counts,
            guard.post_count,
            reviewed.authority_id,
            command_key,
            client_id,
            preflight.quantity,
            preflight.reference_price,
            preflight.notional,
            preflight.rules_fingerprint,
            status,
            ambiguous,
            () if outcome is Phase3Outcome.PASS else (status,),
        )
    finally:
        store.close()


class _ProductionReader:
    def __init__(self, adapter: BinanceSpotTestnetAdapter, client: httpx.Client) -> None:
        self._adapter = adapter
        self._client = client

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

    def read_reference_price(self, stream_key: MarketStreamKey) -> Decimal:
        response = self._client.get("/api/v3/ticker/price", params={"symbol": stream_key.symbol})
        response.raise_for_status()
        payload = response.json()
        value = Decimal(payload["price"])
        if payload.get("symbol") != stream_key.symbol or not value.is_finite() or value <= 0:
            raise Phase3OperatorBlocked("Phase 3 ticker response is invalid")
        return value


class _NoKnownIdentity:
    def by_client_order_id(self, client_order_id: str):
        return None

    def by_exchange_order_id(self, exchange_order_id: str):
        return None


def main() -> int:
    suppress_private_transport_logging()
    try:
        fingerprint = validate_startup(
            os.environ, database_path=DATABASE_PATH, evidence_path=EVIDENCE_PATH
        )
    except Phase3OperatorBlocked as error:
        print(type(error).__name__, file=sys.stderr)
        return 2
    guard = Phase3GuardTransport(httpx.HTTPTransport())
    client = httpx.Client(base_url=SPOT_TESTNET_HOST, transport=guard, follow_redirects=False)
    adapter = BinanceSpotTestnetAdapter(
        BinanceSpotTestnetConfig(
            os.environ["BINANCE_TESTNET_API_KEY"],
            os.environ["BINANCE_TESTNET_SECRET_KEY"],
            fingerprint,
        ),
        client=client,
        timestamp_provider=lambda: time_ns() // 1_000_000,
        identity_resolver=_NoKnownIdentity(),
    )
    try:
        result = run_phase3(
            _ProductionReader(adapter, client),
            adapter,
            guard,
            account_fingerprint=fingerprint,
            database_path=DATABASE_PATH,
            run_id=str(uuid4()),
            now_ms=lambda: time_ns() // 1_000_000,
        )
        EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
        EVIDENCE_PATH.write_text(result.render(), encoding="utf-8")
        print(result.render(), end="")
        return 0 if result.outcome is Phase3Outcome.PASS else 2
    except (KeyboardInterrupt, SystemExit):
        raise
    except Phase3OperatorBlocked as error:
        print(type(error).__name__, file=sys.stderr)
        return 2
    except Exception as error:  # noqa: BLE001 - emit taxonomy only
        print(type(error).__name__, file=sys.stderr)
        return 1
    finally:
        adapter.close()
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
