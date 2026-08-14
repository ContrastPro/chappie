import ast
import json
import socket
import sqlite3
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import app.tools.testnet_phase3 as phase3
from app.exchange_execution import ExchangeOrderStatus
from app.infrastructure.binance import (
    BinanceRateLimitError,
    BinanceSpotTestnetAdapter,
    BinanceSpotTestnetConfig,
)
from app.market.models import MarketStreamKey
from app.persistence.exchange import SQLiteExchangePersistence
from app.persistence.models import SQLitePersistenceConfig
from app.tools.testnet_phase3 import (
    AUTHORIZATION_ENV,
    Phase3GuardTransport,
    Phase3OperatorBlocked,
    Phase3Outcome,
    Phase3Result,
    Phase3TransportViolation,
    _NoKnownIdentity,
    _ProductionReader,
    calculate_quantity,
    observe_preflight,
    run_phase3,
    validate_startup,
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*args, **kwargs):
        raise AssertionError("Phase 3 test attempted real network or DNS")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)


def environment(**changes: str) -> dict[str, str]:
    return {
        AUTHORIZATION_ENV: "YES",
        "BINANCE_TESTNET_ACCOUNT_FINGERPRINT": "phase3-account",
        "BINANCE_TESTNET_API_KEY": "test-key",
        "BINANCE_TESTNET_SECRET_KEY": "test-secret",
        **changes,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {AUTHORIZATION_ENV: "NO"},
        {"M4_10_PHASE1_AUTHORIZED": "YES"},
        {"M4_10_PHASE2_AUTHORIZED": "YES"},
    ],
)
def test_authorization_is_exact_and_precedes_artifacts(tmp_path, changes) -> None:
    with pytest.raises(Phase3OperatorBlocked, match="authorization"):
        validate_startup(changes, database_path=tmp_path / "db", evidence_path=tmp_path / "log")


@pytest.mark.parametrize(
    "missing",
    [
        "BINANCE_TESTNET_ACCOUNT_FINGERPRINT",
        "BINANCE_TESTNET_API_KEY",
        "BINANCE_TESTNET_SECRET_KEY",
    ],
)
def test_configuration_must_be_complete(tmp_path, missing) -> None:
    values = environment()
    values.pop(missing)
    with pytest.raises(Phase3OperatorBlocked):
        validate_startup(values, database_path=tmp_path / "db", evidence_path=tmp_path / "log")


def test_valid_startup_returns_fingerprint(tmp_path) -> None:
    assert (
        validate_startup(
            environment(), database_path=tmp_path / "db", evidence_path=tmp_path / "log"
        )
        == "phase3-account"
    )


def test_authorization_and_collision_do_not_probe_secret_keys(tmp_path) -> None:
    class GuardedEnvironment(dict):
        def get(self, key, default=None):
            if key in {"BINANCE_TESTNET_API_KEY", "BINANCE_TESTNET_SECRET_KEY"}:
                raise AssertionError("secret presence was probed")
            return super().get(key, default)

    missing_auth = GuardedEnvironment()
    with pytest.raises(Phase3OperatorBlocked, match="authorization"):
        validate_startup(
            missing_auth,
            database_path=tmp_path / "missing-db",
            evidence_path=tmp_path / "missing-log",
        )
    collision = tmp_path / "collision.sqlite3"
    collision.write_text("keep", encoding="utf-8")
    authorized = GuardedEnvironment(
        {
            AUTHORIZATION_ENV: "YES",
            "BINANCE_TESTNET_ACCOUNT_FINGERPRINT": "phase3-account",
        }
    )
    with pytest.raises(Phase3OperatorBlocked, match="collision"):
        validate_startup(
            authorized,
            database_path=collision,
            evidence_path=tmp_path / "collision-log",
        )


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm"])
def test_database_collision_blocks_without_deletion(tmp_path, suffix) -> None:
    database = tmp_path / "smoke.sqlite3"
    collision = database if not suffix else tmp_path / f"smoke.sqlite3{suffix}"
    collision.write_text("keep", encoding="utf-8")
    with pytest.raises(Phase3OperatorBlocked, match="collision"):
        validate_startup(environment(), database_path=database, evidence_path=tmp_path / "evidence")
    assert collision.read_text(encoding="utf-8") == "keep"


def test_evidence_collision_blocks(tmp_path) -> None:
    evidence = tmp_path / "evidence"
    evidence.write_text("keep", encoding="utf-8")
    with pytest.raises(Phase3OperatorBlocked, match="collision"):
        validate_startup(environment(), database_path=tmp_path / "db", evidence_path=evidence)


def test_guard_fixed_host_endpoint_redirect_and_single_post() -> None:
    underlying = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    guard = Phase3GuardTransport(underlying)
    assert (
        guard.handle_request(
            httpx.Request("GET", "https://testnet.binance.vision/api/v3/account")
        ).status_code
        == 200
    )
    assert (
        guard.handle_request(
            httpx.Request("POST", "https://testnet.binance.vision/api/v3/order")
        ).status_code
        == 200
    )
    with pytest.raises(Phase3TransportViolation, match="submission budget"):
        guard.handle_request(httpx.Request("POST", "https://testnet.binance.vision/api/v3/order"))
    for url in (
        "https://api.binance.com/api/v3/account",
        "https://testnet.binance.vision:8443/api/v3/account",
        "https://testnet.binance.vision/api/v3/order/test",
        "https://testnet.binance.vision/api/v3/order",
    ):
        with pytest.raises(Phase3TransportViolation):
            guard.handle_request(httpx.Request("GET", url))
    redirect = Phase3GuardTransport(
        httpx.MockTransport(lambda request: httpx.Response(302, headers={"location": "https://x"}))
    )
    with pytest.raises(Phase3TransportViolation, match="redirect"):
        redirect.handle_request(
            httpx.Request("GET", "https://testnet.binance.vision/api/v3/account")
        )
    limited = Phase3GuardTransport(underlying, max_requests=0)
    with pytest.raises(Phase3TransportViolation, match="budget"):
        limited.handle_request(
            httpx.Request("GET", "https://testnet.binance.vision/api/v3/account")
        )
    guard.close()
    guard.close()


def test_guard_blocks_seventh_request_and_every_unapproved_write() -> None:
    calls = 0

    def respond(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    guard = Phase3GuardTransport(httpx.MockTransport(respond))
    for _ in range(6):
        guard.handle_request(httpx.Request("GET", "https://testnet.binance.vision/api/v3/account"))
    with pytest.raises(Phase3TransportViolation, match="budget"):
        guard.handle_request(httpx.Request("GET", "https://testnet.binance.vision/api/v3/account"))
    assert calls == 6
    for method in ("DELETE", "PUT", "PATCH"):
        with pytest.raises(Phase3TransportViolation):
            Phase3GuardTransport(httpx.MockTransport(respond)).handle_request(
                httpx.Request(method, "https://testnet.binance.vision/api/v3/order")
            )
    assert calls == 6


def test_phase3_tool_has_no_continuous_or_raw_runtime_imports() -> None:
    path = Path("app/tools/testnet_phase3.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    forbidden = {
        "app.main",
        "app.exchange_runtime.consumer",
        "app.strategies",
        "app.market.polling",
        "app.application",
    }
    assert not imports & forbidden


def test_quantity_uses_fresh_decimal_rules() -> None:
    from app.exchange_execution.ports import ExchangeInstrumentRules
    from app.market.models import MarketStreamKey

    rules = ExchangeInstrumentRules(
        MarketStreamKey("BTCUSDT", "1m"),
        Decimal("0.00001"),
        Decimal("0.00001"),
        Decimal(5),
        "rules-v1",
        Decimal(1000),
    )
    quantity, notional = calculate_quantity(rules, Decimal("63849.22"))
    assert quantity == Decimal("0.00016")
    assert Decimal(10) <= notional < Decimal(12)
    with pytest.raises(Phase3OperatorBlocked):
        calculate_quantity(rules, Decimal("NaN"))
    too_coarse = ExchangeInstrumentRules(
        MarketStreamKey("BTCUSDT", "1m"),
        Decimal(1),
        Decimal(1),
        Decimal(5),
        "rules-v2",
        Decimal(1000),
    )
    with pytest.raises(Phase3OperatorBlocked, match="ceiling"):
        calculate_quantity(too_coarse, Decimal(100))


class _Reader:
    def __init__(self) -> None:
        from app.exchange_execution.ports import AccountBalance, ExchangeInstrumentRules

        self.rules = ExchangeInstrumentRules(
            MarketStreamKey("BTCUSDT", "1m"),
            Decimal("0.00001"),
            Decimal("0.00001"),
            Decimal(5),
            "rules",
            Decimal(1000),
        )
        self.price = Decimal("63849.22")
        self.balances = (
            AccountBalance("BTC", Decimal(1), Decimal(0)),
            AccountBalance("USDT", Decimal(25), Decimal(0)),
        )
        self.open_orders = ()
        self.trades = ()

    def read_instrument_rules(self, stream_key):
        return self.rules

    def read_reference_price(self, stream_key):
        return self.price

    def read_balances(self, assets):
        return self.balances

    def read_open_order_observations(self, *, stream_key):
        return self.open_orders

    def read_account_trade_observations(self, *, stream_key, start_at_ms):
        return self.trades


def test_preflight_fails_closed_for_account_anomalies() -> None:
    from app.exchange_execution.ports import AccountBalance
    from app.infrastructure.binance import AccountTradeObservation

    reader = _Reader()
    assert observe_preflight(reader, now_ms=1000).quantity == Decimal("0.00016")
    cases = (
        {"balances": reader.balances[:1]},
        {"open_orders": (object(),)},
        {"trades": (AccountTradeObservation("1", "ctt1t-known", "USDT"),)},
        {"trades": (AccountTradeObservation("1", "external", "BNB"),)},
        {"trades": (AccountTradeObservation("1", "external", "USDT"),)},
        {
            "balances": (
                reader.balances[0],
                AccountBalance("USDT", Decimal(24), Decimal(0)),
            )
        },
    )
    for changes in cases:
        candidate = _Reader()
        for name, value in changes.items():
            setattr(candidate, name, value)
        with pytest.raises(Phase3OperatorBlocked):
            observe_preflight(candidate, now_ms=1000)


def test_stale_preflight_blocks_before_database_or_submission(tmp_path) -> None:
    guard = Phase3GuardTransport(httpx.MockTransport(_responses))
    client = httpx.Client(base_url="https://testnet.binance.vision", transport=guard)
    adapter = BinanceSpotTestnetAdapter(
        BinanceSpotTestnetConfig("key", "secret", "phase3-account"),
        client=client,
        timestamp_provider=lambda: 1000,
        identity_resolver=_NoKnownIdentity(),
    )
    ticks = iter((1000, 32_000))
    database = tmp_path / "stale.sqlite3"
    with pytest.raises(Phase3OperatorBlocked, match="stale"):
        run_phase3(
            _ProductionReader(adapter, client),
            adapter,
            guard,
            account_fingerprint="phase3-account",
            database_path=database,
            run_id="offline-stale",
            now_ms=lambda: next(ticks),
        )
    assert not database.exists()
    assert guard.post_count == 0
    adapter.close()
    client.close()


@pytest.mark.parametrize(("age", "authorized"), [(30_000, True), (30_001, False)])
def test_operator_freshness_exact_boundary(tmp_path, age, authorized) -> None:
    ticker_reads = 0

    def count_ticker(request):
        nonlocal ticker_reads
        if request.url.path == "/api/v3/ticker/price":
            ticker_reads += 1
        return _responses(request)

    guard = Phase3GuardTransport(httpx.MockTransport(count_ticker))
    client = httpx.Client(base_url="https://testnet.binance.vision", transport=guard)
    adapter = BinanceSpotTestnetAdapter(
        BinanceSpotTestnetConfig("key", "secret", "phase3-account"),
        client=client,
        timestamp_provider=lambda: 1000,
        identity_resolver=_NoKnownIdentity(),
    )
    calls = 0

    def clock():
        nonlocal calls
        calls += 1
        return 1000 if calls == 1 else 1000 + age

    database = tmp_path / f"fresh-{age}.sqlite3"
    if authorized:
        result = run_phase3(
            _ProductionReader(adapter, client),
            adapter,
            guard,
            account_fingerprint="phase3-account",
            database_path=database,
            run_id=f"fresh-{age}",
            now_ms=clock,
        )
        assert result.outcome is Phase3Outcome.PASS
        assert guard.post_count == 1
    else:
        with pytest.raises(Phase3OperatorBlocked, match="stale"):
            run_phase3(
                _ProductionReader(adapter, client),
                adapter,
                guard,
                account_fingerprint="phase3-account",
                database_path=database,
                run_id=f"fresh-{age}",
                now_ms=clock,
            )
        assert guard.post_count == 0
        assert not database.exists()
    assert ticker_reads == 1
    adapter.close()
    client.close()


@pytest.mark.parametrize("risk_layer", ["financial", "exchange"])
def test_existing_risk_layers_fail_closed_before_authority(
    tmp_path, monkeypatch, risk_layer
) -> None:
    if risk_layer == "financial":

        class BlockedRisk:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def evaluate(self, *args, **kwargs):
                return SimpleNamespace(
                    outcome=phase3.RiskDecisionOutcome.REJECTED,
                    approved_order=None,
                )

        monkeypatch.setattr(phase3, "RiskManager", BlockedRisk)
    else:
        monkeypatch.setattr(
            phase3,
            "evaluate_exchange_risk",
            lambda *args, **kwargs: SimpleNamespace(outcome=phase3.ExchangeRiskOutcome.BLOCKED),
        )
    guard = Phase3GuardTransport(httpx.MockTransport(_responses))
    client = httpx.Client(base_url="https://testnet.binance.vision", transport=guard)
    adapter = BinanceSpotTestnetAdapter(
        BinanceSpotTestnetConfig("key", "secret", "phase3-account"),
        client=client,
        timestamp_provider=lambda: 1000,
        identity_resolver=_NoKnownIdentity(),
    )
    ticks = iter(range(1000, 1100))
    with pytest.raises(Phase3OperatorBlocked, match="Risk"):
        run_phase3(
            _ProductionReader(adapter, client),
            adapter,
            guard,
            account_fingerprint="phase3-account",
            database_path=tmp_path / f"{risk_layer}.sqlite3",
            run_id=f"offline-{risk_layer}",
            now_ms=lambda: next(ticks),
        )
    assert guard.post_count == 0
    adapter.close()
    client.close()


def _responses(request: httpx.Request, *, post_mode: str = "ack") -> httpx.Response:
    path = request.url.path
    if path == "/api/v3/exchangeInfo":
        return httpx.Response(
            200,
            json={
                "symbols": [
                    {
                        "symbol": "BTCUSDT",
                        "filters": [
                            {
                                "filterType": "MARKET_LOT_SIZE",
                                "minQty": "0.00001",
                                "stepSize": "0.00001",
                                "maxQty": "1000",
                            },
                            {
                                "filterType": "MIN_NOTIONAL",
                                "minNotional": "5",
                                "applyToMarket": True,
                            },
                        ],
                    }
                ]
            },
        )
    if path == "/api/v3/ticker/price":
        return httpx.Response(200, json={"symbol": "BTCUSDT", "price": "63849.22"})
    if path == "/api/v3/account":
        return httpx.Response(
            200,
            json={
                "balances": [
                    {"asset": "BTC", "free": "1", "locked": "0"},
                    {"asset": "USDT", "free": "10000", "locked": "0"},
                ]
            },
        )
    if path in {"/api/v3/openOrders", "/api/v3/myTrades"}:
        return httpx.Response(200, json=[])
    if path == "/api/v3/order":
        assert request.method == "POST"
        body = request.content.decode()
        assert "side=BUY" in body and "type=MARKET" in body
        assert "newOrderRespType=ACK" in body and "newClientOrderId=ctt1t-" in body
        if post_mode == "timeout":
            raise httpx.ReadTimeout("injected", request=request)
        if post_mode == "reject":
            return httpx.Response(400, json={"code": -1013, "msg": "filtered"})
        if post_mode == "malformed":
            return httpx.Response(200, json={"symbol": "BTCUSDT"})
        return httpx.Response(
            200,
            json={
                "symbol": "BTCUSDT",
                "orderId": 123,
                "clientOrderId": next(
                    part.split("=", 1)[1]
                    for part in body.split("&")
                    if part.startswith("newClientOrderId=")
                ),
                "transactTime": 2000,
            },
        )
    raise AssertionError(path)


def test_offline_full_graph_exactly_one_post_ack(tmp_path) -> None:
    guard = Phase3GuardTransport(httpx.MockTransport(_responses))
    client = httpx.Client(base_url="https://testnet.binance.vision", transport=guard)
    adapter = BinanceSpotTestnetAdapter(
        BinanceSpotTestnetConfig("key", "secret", "phase3-account"),
        client=client,
        timestamp_provider=lambda: 1000,
        identity_resolver=_NoKnownIdentity(),
    )
    ticks = iter(range(1000, 1100))
    result = run_phase3(
        _ProductionReader(adapter, client),
        adapter,
        guard,
        account_fingerprint="phase3-account",
        database_path=tmp_path / "phase3.sqlite3",
        run_id="offline-run",
        now_ms=lambda: next(ticks),
    )
    assert result.outcome is Phase3Outcome.PASS
    assert result.post_count == 1
    assert result.durable_status == ExchangeOrderStatus.ACKNOWLEDGED.value
    assert result.quantity == Decimal("0.00016")
    rendered = result.render()
    assert "test-key" not in rendered
    assert "test-secret" not in rendered
    assert "signature=" not in rendered
    adapter.close()
    client.close()


def test_submitting_and_one_attempt_are_committed_before_mock_post(tmp_path) -> None:
    database = tmp_path / "commit-before-post.sqlite3"
    observed = False

    def inspect_then_ack(request: httpx.Request) -> httpx.Response:
        nonlocal observed
        if request.method == "POST":
            with closing(sqlite3.connect(database)) as connection:
                order = connection.execute(
                    "SELECT status,approved_notional FROM exchange_orders"
                ).fetchone()
                authority = connection.execute(
                    "SELECT status FROM exchange_smoke_authorities"
                ).fetchone()
                attempts = connection.execute(
                    "SELECT count(*) FROM exchange_submission_attempts"
                ).fetchone()[0]
                session = connection.execute(
                    "SELECT submission_count,submitted_notional FROM exchange_sessions"
                ).fetchone()
            assert order == ("SUBMITTING", "10.2158752")
            assert authority == ("CONSUMED",)
            assert attempts == 1
            assert session == (1, "10.2158752")
            observed = True
        return _responses(request)

    guard = Phase3GuardTransport(httpx.MockTransport(inspect_then_ack))
    client = httpx.Client(base_url="https://testnet.binance.vision", transport=guard)
    adapter = BinanceSpotTestnetAdapter(
        BinanceSpotTestnetConfig("key", "secret", "phase3-account"),
        client=client,
        timestamp_provider=lambda: 1000,
        identity_resolver=_NoKnownIdentity(),
    )
    ticks = iter(range(1000, 1100))
    result = run_phase3(
        _ProductionReader(adapter, client),
        adapter,
        guard,
        account_fingerprint="phase3-account",
        database_path=database,
        run_id="offline-ordering",
        now_ms=lambda: next(ticks),
    )
    assert observed and result.outcome is Phase3Outcome.PASS
    with closing(sqlite3.connect(database)) as connection:
        payload = json.loads(
            connection.execute("SELECT state_payload FROM exchange_portfolio_current").fetchone()[0]
        )
        assert payload["base_balance"] == {"$decimal": "0"}
        assert payload["quote_balance"] == {"$decimal": "25"}
        assert connection.execute("SELECT count(*) FROM exchange_trades").fetchone()[0] == 0
    adapter.close()
    client.close()


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt, SystemExit, GeneratorExit])
def test_crash_after_submitting_commit_never_reaches_transport(
    tmp_path, monkeypatch, failure
) -> None:
    database = tmp_path / f"after-commit-{failure.__name__}.sqlite3"
    posts = 0

    def transport(request):
        nonlocal posts
        if request.method == "POST":
            posts += 1
        return _responses(request)

    real_store = SQLiteExchangePersistence

    def store(config):
        def fail(boundary):
            if boundary == "attempt_after_commit":
                raise failure("after durable SUBMITTING")

        return real_store(config, failure_injector=fail)

    monkeypatch.setattr(phase3, "SQLiteExchangePersistence", store)
    guard = Phase3GuardTransport(httpx.MockTransport(transport))
    client = httpx.Client(base_url="https://testnet.binance.vision", transport=guard)
    adapter = BinanceSpotTestnetAdapter(
        BinanceSpotTestnetConfig("key", "secret", "phase3-account"),
        client=client,
        timestamp_provider=lambda: 1000,
        identity_resolver=_NoKnownIdentity(),
    )
    ticks = iter(range(1000, 1100))
    with pytest.raises(failure):
        run_phase3(
            _ProductionReader(adapter, client),
            adapter,
            guard,
            account_fingerprint="phase3-account",
            database_path=database,
            run_id=f"offline-{failure.__name__}",
            now_ms=lambda: next(ticks),
        )
    assert posts == 0
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT status FROM exchange_orders").fetchone() == (
            "SUBMITTING",
        )
        assert connection.execute("SELECT submission_count FROM exchange_sessions").fetchone() == (
            1,
        )
    fresh = real_store(SQLitePersistenceConfig(database, expected_schema_version=5))
    recovery = fresh.recover_local()
    assert [item.order.status.value for item in recovery.reconciliation_required] == ["SUBMITTING"]
    fresh.close()
    adapter.close()
    client.close()


@pytest.mark.parametrize(
    ("mode", "status"),
    [
        ("reject", "REJECTED"),
        ("timeout", "RECONCILIATION_REQUIRED"),
        ("malformed", "RECONCILIATION_REQUIRED"),
    ],
)
def test_rejection_and_ambiguity_never_retry(tmp_path, mode: str, status: str) -> None:
    guard = Phase3GuardTransport(
        httpx.MockTransport(lambda request: _responses(request, post_mode=mode))
    )
    client = httpx.Client(base_url="https://testnet.binance.vision", transport=guard)
    adapter = BinanceSpotTestnetAdapter(
        BinanceSpotTestnetConfig("key", "secret", "phase3-account"),
        client=client,
        timestamp_provider=lambda: 1000,
        identity_resolver=_NoKnownIdentity(),
    )
    ticks = iter(range(1000, 1100))
    result = run_phase3(
        _ProductionReader(adapter, client),
        adapter,
        guard,
        account_fingerprint="phase3-account",
        database_path=tmp_path / f"{mode}.sqlite3",
        run_id=f"offline-{mode}",
        now_ms=lambda: next(ticks),
    )
    assert result.outcome is Phase3Outcome.BLOCKED
    assert result.durable_status == status
    assert result.post_count == 1
    adapter.close()
    client.close()


@pytest.mark.parametrize("failure", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_during_gateway_baseexception_keeps_one_identity_and_submitting(tmp_path, failure) -> None:
    database = tmp_path / f"gateway-{failure.__name__}.sqlite3"
    posts = 0

    def fail_during_gateway(request):
        nonlocal posts
        if request.method == "POST":
            posts += 1
            raise failure("transmission may have occurred")
        return _responses(request)

    guard = Phase3GuardTransport(httpx.MockTransport(fail_during_gateway))
    client = httpx.Client(base_url="https://testnet.binance.vision", transport=guard)
    adapter = BinanceSpotTestnetAdapter(
        BinanceSpotTestnetConfig("key", "secret", "phase3-account"),
        client=client,
        timestamp_provider=lambda: 1000,
        identity_resolver=_NoKnownIdentity(),
    )
    ticks = iter(range(1000, 1100))
    with pytest.raises(failure):
        run_phase3(
            _ProductionReader(adapter, client),
            adapter,
            guard,
            account_fingerprint="phase3-account",
            database_path=database,
            run_id=f"gateway-{failure.__name__}",
            now_ms=lambda: next(ticks),
        )
    assert posts == 1
    with closing(sqlite3.connect(database)) as connection:
        order = connection.execute("SELECT status,client_order_id FROM exchange_orders").fetchone()
        assert order[0] == "SUBMITTING"
        assert order[1].startswith("ctt1t-")
        assert connection.execute(
            "SELECT count(*) FROM exchange_submission_attempts"
        ).fetchone() == (1,)
    adapter.close()
    client.close()


def test_ambiguous_status_persistence_failure_retains_submitting(tmp_path, monkeypatch) -> None:
    database = tmp_path / "ambiguous-persistence.sqlite3"
    posts = 0

    def timeout(request):
        nonlocal posts
        if request.method == "POST":
            posts += 1
            raise httpx.ReadTimeout("lost ACK", request=request)
        return _responses(request)

    real_store = SQLiteExchangePersistence

    def store(config):
        def fail(boundary):
            if boundary == "status_before_commit":
                raise RuntimeError("status persistence interrupted")

        return real_store(config, failure_injector=fail)

    monkeypatch.setattr(phase3, "SQLiteExchangePersistence", store)
    guard = Phase3GuardTransport(httpx.MockTransport(timeout))
    client = httpx.Client(base_url="https://testnet.binance.vision", transport=guard)
    adapter = BinanceSpotTestnetAdapter(
        BinanceSpotTestnetConfig("key", "secret", "phase3-account"),
        client=client,
        timestamp_provider=lambda: 1000,
        identity_resolver=_NoKnownIdentity(),
    )
    ticks = iter(range(1000, 1100))
    with pytest.raises(RuntimeError, match="persistence"):
        run_phase3(
            _ProductionReader(adapter, client),
            adapter,
            guard,
            account_fingerprint="phase3-account",
            database_path=database,
            run_id="ambiguous-persistence",
            now_ms=lambda: next(ticks),
        )
    assert posts == 1
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT status FROM exchange_orders").fetchone() == (
            "SUBMITTING",
        )
        assert connection.execute(
            "SELECT count(*) FROM exchange_submission_attempts"
        ).fetchone() == (1,)
    adapter.close()
    client.close()


@pytest.mark.parametrize("mode", ["418", "429", "500", "protocol"])
def test_transport_failures_never_retry_or_leak_into_durable_identity(tmp_path, mode) -> None:
    database = tmp_path / f"transport-{mode}.sqlite3"
    posts = 0

    def fail(request):
        nonlocal posts
        if request.method != "POST":
            return _responses(request)
        posts += 1
        if mode == "protocol":
            raise httpx.RemoteProtocolError("reset", request=request)
        status = int(mode)
        headers = {"Retry-After": "1"} if status in {418, 429} else {}
        return httpx.Response(status, headers=headers, json={"code": -1, "msg": "safe"})

    guard = Phase3GuardTransport(httpx.MockTransport(fail))
    client = httpx.Client(base_url="https://testnet.binance.vision", transport=guard)
    adapter = BinanceSpotTestnetAdapter(
        BinanceSpotTestnetConfig("key", "secret", "phase3-account"),
        client=client,
        timestamp_provider=lambda: 1000,
        identity_resolver=_NoKnownIdentity(),
    )
    ticks = iter(range(1000, 1100))
    if mode in {"418", "429"}:
        with pytest.raises(BinanceRateLimitError):
            run_phase3(
                _ProductionReader(adapter, client),
                adapter,
                guard,
                account_fingerprint="phase3-account",
                database_path=database,
                run_id=f"transport-{mode}",
                now_ms=lambda: next(ticks),
            )
    else:
        result = run_phase3(
            _ProductionReader(adapter, client),
            adapter,
            guard,
            account_fingerprint="phase3-account",
            database_path=database,
            run_id=f"transport-{mode}",
            now_ms=lambda: next(ticks),
        )
        assert result.outcome is Phase3Outcome.BLOCKED
        assert result.durable_status == "RECONCILIATION_REQUIRED"
    assert posts == 1
    with closing(sqlite3.connect(database)) as connection:
        expected_status = "SUBMITTING" if mode in {"418", "429"} else "RECONCILIATION_REQUIRED"
        assert connection.execute("SELECT status FROM exchange_orders").fetchone() == (
            expected_status,
        )
    adapter.close()
    client.close()


class _Closeable:
    def __init__(self, *args, **kwargs) -> None:
        self.closed = False
        self.close_count = 0

    def close(self) -> None:
        self.close_count += 1
        self.closed = True


@pytest.mark.parametrize(
    ("result_outcome", "expected"),
    [(Phase3Outcome.PASS, 0), (Phase3Outcome.BLOCKED, 2)],
)
def test_main_writes_sanitized_evidence_offline(
    tmp_path, monkeypatch, result_outcome, expected
) -> None:
    evidence = tmp_path / "phase3.log"
    client = _Closeable()
    adapter = _Closeable()
    monkeypatch.setattr(phase3, "EVIDENCE_PATH", evidence)
    monkeypatch.setattr(phase3, "validate_startup", lambda *args, **kwargs: "phase3-account")
    monkeypatch.setattr(phase3.httpx, "HTTPTransport", _Closeable)
    monkeypatch.setattr(phase3.httpx, "Client", lambda *args, **kwargs: client)
    monkeypatch.setattr(phase3, "BinanceSpotTestnetAdapter", lambda *args, **kwargs: adapter)
    monkeypatch.setattr(
        phase3,
        "run_phase3",
        lambda *args, **kwargs: Phase3Result(
            result_outcome,
            "run",
            1,
            "phase3-account",
            6,
            {"POST /api/v3/order": 1},
            1,
        ),
    )
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "secret-key")
    monkeypatch.setenv("BINANCE_TESTNET_SECRET_KEY", "secret-value")
    assert phase3.main() == expected
    assert "secret" not in evidence.read_text(encoding="utf-8")
    assert client.closed and adapter.closed
    assert client.close_count == adapter.close_count == 1


@pytest.mark.parametrize(
    ("error", "expected"),
    [(Phase3OperatorBlocked("blocked"), 2), (ValueError("failed"), 1)],
)
def test_main_maps_errors_to_exit_codes_offline(monkeypatch, error, expected) -> None:
    client = _Closeable()
    adapter = _Closeable()
    monkeypatch.setattr(phase3, "validate_startup", lambda *args, **kwargs: "phase3-account")
    monkeypatch.setattr(phase3.httpx, "HTTPTransport", _Closeable)
    monkeypatch.setattr(phase3.httpx, "Client", lambda *args, **kwargs: client)
    monkeypatch.setattr(phase3, "BinanceSpotTestnetAdapter", lambda *args, **kwargs: adapter)
    monkeypatch.setattr(phase3, "run_phase3", lambda *args, **kwargs: (_ for _ in ()).throw(error))
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "key")
    monkeypatch.setenv("BINANCE_TESTNET_SECRET_KEY", "secret")
    assert phase3.main() == expected
    assert client.closed and adapter.closed
    assert client.close_count == adapter.close_count == 1


def test_evidence_write_failure_is_failed_once_without_rerun(tmp_path, monkeypatch) -> None:
    evidence = tmp_path / "phase3.log"
    client = _Closeable()
    adapter = _Closeable()
    calls = 0

    def completed(*args, **kwargs):
        nonlocal calls
        calls += 1
        return Phase3Result(
            Phase3Outcome.PASS,
            "run",
            1,
            "phase3-account",
            6,
            {"POST /api/v3/order": 1},
            1,
        )

    original_write = Path.write_text

    def fail_write(path, *args, **kwargs):
        if path == evidence:
            raise OSError("evidence sink unavailable")
        return original_write(path, *args, **kwargs)

    monkeypatch.setattr(phase3, "EVIDENCE_PATH", evidence)
    monkeypatch.setattr(phase3, "validate_startup", lambda *args, **kwargs: "phase3-account")
    monkeypatch.setattr(phase3.httpx, "HTTPTransport", _Closeable)
    monkeypatch.setattr(phase3.httpx, "Client", lambda *args, **kwargs: client)
    monkeypatch.setattr(phase3, "BinanceSpotTestnetAdapter", lambda *args, **kwargs: adapter)
    monkeypatch.setattr(phase3, "run_phase3", completed)
    monkeypatch.setattr(Path, "write_text", fail_write)
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "key")
    monkeypatch.setenv("BINANCE_TESTNET_SECRET_KEY", "secret")
    assert phase3.main() == 1
    assert calls == 1
    assert not evidence.exists()
    assert client.close_count == adapter.close_count == 1


def test_main_stops_before_transport_when_startup_is_blocked(monkeypatch) -> None:
    monkeypatch.setattr(
        phase3,
        "validate_startup",
        lambda *args, **kwargs: (_ for _ in ()).throw(Phase3OperatorBlocked("blocked")),
    )
    monkeypatch.setattr(
        phase3.httpx,
        "HTTPTransport",
        lambda: (_ for _ in ()).throw(AssertionError("transport constructed")),
    )
    assert phase3.main() == 2


def test_production_reader_and_identity_are_fail_closed() -> None:
    class Adapter:
        def read_instrument_rules(self, stream_key):
            return "rules"

        def read_balances(self, assets):
            return assets

        def read_open_order_observations(self, *, stream_key):
            return (stream_key,)

        def read_account_trade_observations(self, *, stream_key, start_at_ms):
            return (stream_key, start_at_ms)

    class Client:
        def __init__(self) -> None:
            self.payload = {"symbol": "BTCUSDT", "price": "63849.22"}

        def get(self, path, *, params):
            return httpx.Response(200, json=self.payload, request=httpx.Request("GET", "https://x"))

    adapter = Adapter()
    client = Client()
    reader = _ProductionReader(adapter, client)  # type: ignore[arg-type]
    key = MarketStreamKey("BTCUSDT", "1m")
    assert reader.read_instrument_rules(key) == "rules"
    assert reader.read_balances(("BTC",)) == ("BTC",)
    assert reader.read_open_order_observations(stream_key=key) == (key,)
    assert reader.read_account_trade_observations(stream_key=key, start_at_ms=1) == (key, 1)
    assert reader.read_reference_price(key) == Decimal("63849.22")
    client.payload = {"symbol": "ETHUSDT", "price": "1"}
    with pytest.raises(Phase3OperatorBlocked, match="ticker"):
        reader.read_reference_price(key)
    identity = _NoKnownIdentity()
    assert identity.by_client_order_id("x") is None
    assert identity.by_exchange_order_id("x") is None


def test_main_preserves_operator_interrupt(monkeypatch) -> None:
    client = _Closeable()
    adapter = _Closeable()
    monkeypatch.setattr(phase3, "validate_startup", lambda *args, **kwargs: "phase3-account")
    monkeypatch.setattr(phase3.httpx, "HTTPTransport", _Closeable)
    monkeypatch.setattr(phase3.httpx, "Client", lambda *args, **kwargs: client)
    monkeypatch.setattr(phase3, "BinanceSpotTestnetAdapter", lambda *args, **kwargs: adapter)
    monkeypatch.setattr(
        phase3,
        "run_phase3",
        lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "key")
    monkeypatch.setenv("BINANCE_TESTNET_SECRET_KEY", "secret")
    with pytest.raises(KeyboardInterrupt):
        phase3.main()
    assert client.closed and adapter.closed
    assert client.close_count == adapter.close_count == 1
