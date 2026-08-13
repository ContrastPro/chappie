import ast
import shutil
import socket
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

import app.tools.testnet_phase4 as phase4
from app.exchange_runtime.identity import SQLiteOrderIdentityResolver
from app.infrastructure.binance import BinanceSpotTestnetAdapter, BinanceSpotTestnetConfig
from app.persistence.exchange import SQLiteExchangePersistence
from app.persistence.models import SQLitePersistenceConfig
from app.tools.testnet_phase3 import (
    Phase3GuardTransport,
    _NoKnownIdentity,
    _ProductionReader,
    run_phase3,
)
from app.tools.testnet_phase4 import (
    AUTHORIZATION_ENV,
    Phase4Blocked,
    Phase4Outcome,
    Phase4ReadOnlyTransport,
    Phase4Result,
    Phase4TransportViolation,
    RecoveredPhase3,
    recover_phase3,
    run_phase4,
    validate_startup,
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Phase 4 test attempted network or DNS")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)


def _phase3_response(request: httpx.Request) -> httpx.Response:
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
        return httpx.Response(200, json={"symbol": "BTCUSDT", "price": "62500"})
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
    if path == "/api/v3/order" and request.method == "POST":
        body = parse_qs(request.content.decode())
        return httpx.Response(
            200,
            json={
                "symbol": "BTCUSDT",
                "orderId": 123,
                "clientOrderId": body["newClientOrderId"][0],
                "transactTime": 1010,
            },
        )
    raise AssertionError(path)


def phase3_database(path: Path) -> None:
    guard = Phase3GuardTransport(httpx.MockTransport(_phase3_response))
    client = httpx.Client(base_url="https://testnet.binance.vision", transport=guard)
    adapter = BinanceSpotTestnetAdapter(
        BinanceSpotTestnetConfig("key", "secret", "phase3-account"),
        client=client,
        timestamp_provider=lambda: 1000,
        identity_resolver=_NoKnownIdentity(),
    )
    ticks = iter(range(1000, 1100))
    run_phase3(
        _ProductionReader(adapter, client),
        adapter,
        guard,
        account_fingerprint="phase3-account",
        database_path=path,
        run_id="phase3-fixture",
        now_ms=lambda: next(ticks),
    )
    adapter.close()
    client.close()


def phase4_response(request: httpx.Request, *, status="FILLED", fee_asset="USDT"):
    if request.url.path == "/api/v3/exchangeInfo":
        return _phase3_response(request)
    if request.url.path == "/api/v3/order":
        query = parse_qs(request.url.query.decode())
        assert query["origClientOrderId"][0].startswith("ctt1t-")
        quantity = "0.00008" if status == "PARTIALLY_FILLED" else "0.00016"
        quote = "5" if status == "PARTIALLY_FILLED" else "10"
        return httpx.Response(
            200,
            json={
                "symbol": "BTCUSDT",
                "orderId": 123,
                "clientOrderId": query["origClientOrderId"][0],
                "price": "0",
                "origQty": "0.00016",
                "executedQty": quantity,
                "cummulativeQuoteQty": quote,
                "status": status,
                "side": "BUY",
                "type": "MARKET",
                "time": 1010,
                "updateTime": 1020,
            },
        )
    if request.url.path == "/api/v3/myTrades":
        partial = status == "PARTIALLY_FILLED"
        return httpx.Response(
            200,
            json=[
                {
                    "symbol": "BTCUSDT",
                    "id": 1,
                    "orderId": 123,
                    "price": "62500",
                    "qty": "0.00008" if partial else "0.00016",
                    "quoteQty": "5" if partial else "10",
                    "commission": "0.005" if partial else "0.01",
                    "commissionAsset": fee_asset,
                    "time": 1020,
                    "isBuyer": True,
                    "isMaker": False,
                }
            ],
        )
    if request.url.path == "/api/v3/account":
        partial = status == "PARTIALLY_FILLED"
        fee = Decimal("0.005") if partial else Decimal("0.01")
        quantity = Decimal("0.00008") if partial else Decimal("0.00016")
        quote = Decimal(5) if partial else Decimal(10)
        btc = Decimal(1) + quantity - (fee if fee_asset == "BTC" else Decimal(0))
        usdt = Decimal(10000) - quote - (fee if fee_asset == "USDT" else Decimal(0))
        return httpx.Response(
            200,
            json={
                "balances": [
                    {
                        "asset": "BTC",
                        "free": str(btc),
                        "locked": "0",
                    },
                    {
                        "asset": "USDT",
                        "free": str(usdt),
                        "locked": "0",
                    },
                ]
            },
        )
    raise AssertionError(request.url.path)


def phase4_objects(path: Path, handler):
    store = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=5))
    guard = Phase4ReadOnlyTransport(httpx.MockTransport(handler))
    client = httpx.Client(base_url="https://testnet.binance.vision", transport=guard)
    adapter = BinanceSpotTestnetAdapter(
        BinanceSpotTestnetConfig("key", "secret", "phase3-account"),
        client=client,
        timestamp_provider=lambda: 2000,
        identity_resolver=SQLiteOrderIdentityResolver(store),
    )
    return store, guard, client, adapter


def test_authorization_collision_and_database_ordering(tmp_path) -> None:
    database = tmp_path / "db.sqlite3"
    evidence = tmp_path / "phase4.log"
    with pytest.raises(Phase4Blocked, match="authorization"):
        validate_startup({}, database=database, evidence=evidence)
    database.write_text("x", encoding="utf-8")
    evidence.write_text("x", encoding="utf-8")
    with pytest.raises(Phase4Blocked, match="evidence"):
        validate_startup({AUTHORIZATION_ENV: "YES"}, database=database, evidence=evidence)
    assert AUTHORIZATION_ENV != "M4_10_PHASE3_AUTHORIZED"
    evidence.unlink()
    values = {AUTHORIZATION_ENV: "YES"}
    with pytest.raises(Phase4Blocked, match="fingerprint"):
        validate_startup(values, database=database, evidence=evidence)
    values["BINANCE_TESTNET_ACCOUNT_FINGERPRINT"] = "phase3-account"
    with pytest.raises(Phase4Blocked, match="credentials"):
        validate_startup(values, database=database, evidence=evidence)
    values.update({"BINANCE_TESTNET_API_KEY": "key", "BINANCE_TESTNET_SECRET_KEY": "secret"})
    assert validate_startup(values, database=database, evidence=evidence) == "phase3-account"
    with pytest.raises(Phase4Blocked, match="database"):
        validate_startup(values, database=tmp_path / "absent", evidence=evidence)


def test_read_only_allowlist_and_budget() -> None:
    calls = 0

    def response(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    guard = Phase4ReadOnlyTransport(httpx.MockTransport(response))
    for path in ("order", "myTrades", "account", "exchangeInfo"):
        guard.handle_request(httpx.Request("GET", f"https://testnet.binance.vision/api/v3/{path}"))
    with pytest.raises(Phase4TransportViolation):
        guard.handle_request(httpx.Request("GET", "https://testnet.binance.vision/api/v3/account"))
    for method in ("POST", "DELETE", "PUT", "PATCH"):
        with pytest.raises(Phase4TransportViolation):
            Phase4ReadOnlyTransport(httpx.MockTransport(response)).handle_request(
                httpx.Request(method, "https://testnet.binance.vision/api/v3/order")
            )
    assert calls == 4
    redirect = Phase4ReadOnlyTransport(
        httpx.MockTransport(lambda request: httpx.Response(302, headers={"location": "https://x"}))
    )
    with pytest.raises(Phase4TransportViolation, match="redirect"):
        redirect.handle_request(
            httpx.Request("GET", "https://testnet.binance.vision/api/v3/account")
        )
    guard.close()
    guard.close()


def test_full_filled_settlement_is_exact_once_across_restart(tmp_path) -> None:
    path = tmp_path / "phase4.sqlite3"
    phase3_database(path)
    store, guard, client, adapter = phase4_objects(path, lambda request: phase4_response(request))
    ticks = iter(range(2000, 2100))
    first = run_phase4(
        store,
        adapter,
        guard,
        expected_account="phase3-account",
        run_id="phase4-first",
        now_ms=lambda: next(ticks),
    )
    assert first.outcome is Phase4Outcome.PASS
    assert first.managed_btc_after == Decimal("0.00016")
    assert first.managed_usdt_after == Decimal("14.99")
    assert first.newly_settled_trade_count == 1
    adapter.close()
    client.close()
    store.close()

    store, guard, client, adapter = phase4_objects(path, lambda request: phase4_response(request))
    ticks = iter(range(3000, 3100))
    second = run_phase4(
        store,
        adapter,
        guard,
        expected_account="phase3-account",
        run_id="phase4-restart",
        now_ms=lambda: next(ticks),
    )
    assert second.managed_btc_after == first.managed_btc_after
    assert second.managed_usdt_after == first.managed_usdt_after
    assert second.newly_settled_trade_count == 0
    adapter.close()
    client.close()
    store.close()


def test_partial_is_settled_but_incomplete(tmp_path) -> None:
    path = tmp_path / "partial.sqlite3"
    phase3_database(path)
    store, guard, client, adapter = phase4_objects(
        path, lambda request: phase4_response(request, status="PARTIALLY_FILLED")
    )
    ticks = iter(range(2000, 2100))
    result = run_phase4(
        store,
        adapter,
        guard,
        expected_account="phase3-account",
        run_id="phase4-partial",
        now_ms=lambda: next(ticks),
    )
    assert result.outcome is Phase4Outcome.INCOMPLETE
    assert result.managed_btc_after == Decimal("0.00008")
    assert result.managed_usdt_after == Decimal("19.995")
    adapter.close()
    client.close()
    store.close()


@pytest.mark.parametrize("fee_asset", ["BTC", "BNB"])
def test_unsupported_fee_persists_truth_but_blocks_settlement(tmp_path, fee_asset) -> None:
    path = tmp_path / f"fee-{fee_asset}.sqlite3"
    phase3_database(path)
    store, guard, client, adapter = phase4_objects(
        path, lambda request: phase4_response(request, fee_asset=fee_asset)
    )
    ticks = iter(range(2000, 2100))
    result = run_phase4(
        store,
        adapter,
        guard,
        expected_account="phase3-account",
        run_id="phase4-fee",
        now_ms=lambda: next(ticks),
    )
    assert result.outcome is Phase4Outcome.INCOMPLETE
    assert store.trade_count(result.recovered.command_key) == 1
    assert result.managed_btc_after == Decimal(0)
    assert result.managed_usdt_after == Decimal(25)
    adapter.close()
    client.close()
    store.close()


def test_balance_drift_and_identity_conflict_fail_closed(tmp_path) -> None:
    path = tmp_path / "conflict.sqlite3"
    phase3_database(path)

    def conflict(request):
        response = phase4_response(request)
        if request.url.path == "/api/v3/account":
            return httpx.Response(
                200,
                json={
                    "balances": [
                        {"asset": "BTC", "free": "1.5", "locked": "0"},
                        {"asset": "USDT", "free": "9989.99", "locked": "0"},
                    ]
                },
            )
        return response

    store, guard, client, adapter = phase4_objects(path, conflict)
    with pytest.raises(Phase4Blocked, match="balances"):
        run_phase4(
            store,
            adapter,
            guard,
            expected_account="phase3-account",
            run_id="phase4-conflict",
            now_ms=iter(range(2000, 2100)).__next__,
        )
    adapter.close()
    client.close()
    store.close()


@pytest.mark.parametrize(
    ("asset", "delta"),
    [("BTC", "0.1"), ("BTC", "-0.00001"), ("USDT", "1"), ("USDT", "-1")],
)
def test_unexplained_account_balance_delta_fails_closed(tmp_path, asset, delta) -> None:
    path = tmp_path / f"balance-{asset}-{delta}.sqlite3"
    phase3_database(path)

    def response(request):
        original = phase4_response(request)
        if request.url.path != "/api/v3/account":
            return original
        payload = original.json()
        for balance in payload["balances"]:
            if balance["asset"] == asset:
                balance["free"] = str(Decimal(balance["free"]) + Decimal(delta))
        return httpx.Response(200, json=payload)

    store, guard, client, adapter = phase4_objects(path, response)
    with pytest.raises(Phase4Blocked, match="balances"):
        run_phase4(
            store,
            adapter,
            guard,
            expected_account="phase3-account",
            run_id="phase4-balance-conflict",
            now_ms=iter(range(2000, 2100)).__next__,
        )
    adapter.close()
    client.close()
    store.close()


def test_locked_balance_change_fails_closed(tmp_path) -> None:
    path = tmp_path / "locked.sqlite3"
    phase3_database(path)

    def response(request):
        original = phase4_response(request)
        if request.url.path != "/api/v3/account":
            return original
        payload = original.json()
        payload["balances"][0]["locked"] = "0.00001"
        payload["balances"][0]["free"] = "1.00015"
        return httpx.Response(200, json=payload)

    store, guard, client, adapter = phase4_objects(path, response)
    with pytest.raises(Phase4Blocked, match="balances"):
        run_phase4(
            store,
            adapter,
            guard,
            expected_account="phase3-account",
            run_id="phase4-locked-conflict",
            now_ms=iter(range(2000, 2100)).__next__,
        )
    adapter.close()
    client.close()
    store.close()


def test_quote_trade_completeness_is_required_for_pass(tmp_path) -> None:
    path = tmp_path / "quote-incomplete.sqlite3"
    phase3_database(path)

    def response(request):
        original = phase4_response(request)
        if request.url.path != "/api/v3/order":
            return original
        payload = original.json()
        payload["cummulativeQuoteQty"] = "10.01"
        return httpx.Response(200, json=payload)

    store, guard, client, adapter = phase4_objects(path, response)
    result = run_phase4(
        store,
        adapter,
        guard,
        expected_account="phase3-account",
        run_id="phase4-quote-incomplete",
        now_ms=iter(range(2000, 2100)).__next__,
    )
    assert result.outcome is Phase4Outcome.INCOMPLETE
    assert result.reconciliation_status == "REQUIRED"
    assert result.managed_btc_after == 0
    adapter.close()
    client.close()
    store.close()


def test_zero_unsupported_fee_label_remains_blocked(tmp_path) -> None:
    path = tmp_path / "zero-bnb-fee.sqlite3"
    phase3_database(path)

    def response(request):
        original = phase4_response(request, fee_asset="BNB")
        if request.url.path != "/api/v3/myTrades":
            return original
        payload = original.json()
        payload[0]["commission"] = "0"
        return httpx.Response(200, json=payload)

    store, guard, client, adapter = phase4_objects(path, response)
    result = run_phase4(
        store,
        adapter,
        guard,
        expected_account="phase3-account",
        run_id="phase4-zero-bnb",
        now_ms=iter(range(2000, 2100)).__next__,
    )
    assert result.outcome is Phase4Outcome.INCOMPLETE
    assert result.managed_btc_after == 0
    assert result.managed_usdt_after == 25
    adapter.close()
    client.close()
    store.close()


def test_architecture_has_no_submission_or_continuous_runtime() -> None:
    path = Path("app/tools/testnet_phase4.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not imports & {
        "app.main",
        "app.exchange_execution.durable",
        "app.exchange_runtime.submission",
        "app.exchange_runtime.consumer",
        "app.strategies",
    }
    text = path.read_text(encoding="utf-8")
    assert "submit_market_order(" not in text
    assert "POST" not in text
    assert "while " not in text
    assert "sleep(" not in text
    assert "cancel" not in text.lower()


def test_production_artifacts_are_never_test_targets(tmp_path) -> None:
    source = tmp_path / "source.sqlite3"
    phase3_database(source)
    copy = tmp_path / "copy.sqlite3"
    shutil.copyfile(source, copy)
    store = SQLiteExchangePersistence(SQLitePersistenceConfig(copy, expected_schema_version=5))
    recovered = recover_phase3(store, "phase3-account")
    assert recovered.client_order_id.startswith("ctt1t-")
    store.close()


def test_recovery_rejects_missing_or_wrong_lineage(tmp_path) -> None:
    path = tmp_path / "lineage.sqlite3"
    phase3_database(path)
    store = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=5))
    with pytest.raises(Phase4Blocked, match="identity"):
        recover_phase3(store, "wrong-account")
    store._connection.execute("DROP TRIGGER exchange_smoke_authority_events_no_delete")
    store._connection.execute("DELETE FROM exchange_smoke_authority_events")
    store._connection.execute("DROP TRIGGER exchange_smoke_authorities_no_delete")
    store._connection.execute("DELETE FROM exchange_smoke_authorities")
    store._connection.commit()
    with pytest.raises(Phase4Blocked, match="exactly one"):
        recover_phase3(store, "phase3-account")
    store.close()


class _Closeable:
    def __init__(self, *args, **kwargs) -> None:
        self.close_count = 0

    def close(self) -> None:
        self.close_count += 1


def _result(outcome=Phase4Outcome.PASS):
    recovered = RecoveredPhase3(
        "a" * 64,
        "root",
        "session",
        "phase3-account",
        "b" * 64,
        "ctt1t-abcdefghijklmnopqrstuvwxyz",
        "123",
        Decimal("0.00016"),
        "c" * 64,
        "d" * 64,
        1,
        Decimal(1),
        Decimal(10000),
        Decimal(0),
        Decimal(0),
    )
    return Phase4Result(
        outcome,
        "run",
        2,
        recovered,
        4,
        {"GET /api/v3/order": 1},
        "FILLED",
        1,
        1,
        0,
        Decimal(0),
        Decimal("0.00016"),
        Decimal(25),
        Decimal("14.99"),
        "SAFE",
    )


@pytest.mark.parametrize(
    ("outcome", "expected"), [(Phase4Outcome.PASS, 0), (Phase4Outcome.INCOMPLETE, 2)]
)
def test_main_offline_success_and_incomplete(tmp_path, monkeypatch, outcome, expected) -> None:
    evidence = tmp_path / "phase4.log"
    store = _Closeable()
    client = _Closeable()
    adapter = _Closeable()
    monkeypatch.setattr(phase4, "EVIDENCE_PATH", evidence)
    monkeypatch.setattr(phase4, "validate_startup", lambda *args, **kwargs: "phase3-account")
    monkeypatch.setattr(phase4, "SQLiteExchangePersistence", lambda *args, **kwargs: store)
    monkeypatch.setattr(phase4.httpx, "HTTPTransport", _Closeable)
    monkeypatch.setattr(phase4.httpx, "Client", lambda *args, **kwargs: client)
    monkeypatch.setattr(phase4, "BinanceSpotTestnetAdapter", lambda *args, **kwargs: adapter)
    monkeypatch.setattr(phase4, "SQLiteOrderIdentityResolver", lambda value: object())
    monkeypatch.setattr(phase4, "run_phase4", lambda *args, **kwargs: _result(outcome))
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "key")
    monkeypatch.setenv("BINANCE_TESTNET_SECRET_KEY", "secret")
    assert phase4.main() == expected
    assert "secret" not in evidence.read_text(encoding="utf-8")
    assert store.close_count == client.close_count == adapter.close_count == 1


@pytest.mark.parametrize(
    ("error", "expected"), [(Phase4Blocked("blocked"), 2), (ValueError("failed"), 1)]
)
def test_main_offline_error_taxonomy(monkeypatch, error, expected) -> None:
    store = _Closeable()
    client = _Closeable()
    adapter = _Closeable()
    monkeypatch.setattr(phase4, "validate_startup", lambda *args, **kwargs: "phase3-account")
    monkeypatch.setattr(phase4, "SQLiteExchangePersistence", lambda *args, **kwargs: store)
    monkeypatch.setattr(phase4.httpx, "HTTPTransport", _Closeable)
    monkeypatch.setattr(phase4.httpx, "Client", lambda *args, **kwargs: client)
    monkeypatch.setattr(phase4, "BinanceSpotTestnetAdapter", lambda *args, **kwargs: adapter)
    monkeypatch.setattr(phase4, "SQLiteOrderIdentityResolver", lambda value: object())
    monkeypatch.setattr(phase4, "run_phase4", lambda *args, **kwargs: (_ for _ in ()).throw(error))
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "key")
    monkeypatch.setenv("BINANCE_TESTNET_SECRET_KEY", "secret")
    assert phase4.main() == expected
    assert store.close_count == client.close_count == adapter.close_count == 1


def test_main_authorization_failure_constructs_no_resources(monkeypatch) -> None:
    monkeypatch.setattr(
        phase4,
        "validate_startup",
        lambda *args, **kwargs: (_ for _ in ()).throw(Phase4Blocked("blocked")),
    )
    monkeypatch.setattr(
        phase4,
        "SQLiteExchangePersistence",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("DB opened")),
    )
    assert phase4.main() == 2


def test_main_preserves_interrupt(monkeypatch) -> None:
    store = _Closeable()
    client = _Closeable()
    adapter = _Closeable()
    monkeypatch.setattr(phase4, "validate_startup", lambda *args, **kwargs: "phase3-account")
    monkeypatch.setattr(phase4, "SQLiteExchangePersistence", lambda *args, **kwargs: store)
    monkeypatch.setattr(phase4.httpx, "HTTPTransport", _Closeable)
    monkeypatch.setattr(phase4.httpx, "Client", lambda *args, **kwargs: client)
    monkeypatch.setattr(phase4, "BinanceSpotTestnetAdapter", lambda *args, **kwargs: adapter)
    monkeypatch.setattr(phase4, "SQLiteOrderIdentityResolver", lambda value: object())
    monkeypatch.setattr(
        phase4,
        "run_phase4",
        lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "key")
    monkeypatch.setenv("BINANCE_TESTNET_SECRET_KEY", "secret")
    with pytest.raises(KeyboardInterrupt):
        phase4.main()
    assert store.close_count == client.close_count == adapter.close_count == 1


def test_main_evidence_collision_race_never_overwrites(tmp_path, monkeypatch) -> None:
    evidence = tmp_path / "phase4.log"
    evidence.write_text("preserved", encoding="utf-8")
    store = _Closeable()
    client = _Closeable()
    adapter = _Closeable()
    monkeypatch.setattr(phase4, "EVIDENCE_PATH", evidence)
    monkeypatch.setattr(phase4, "validate_startup", lambda *args, **kwargs: "phase3-account")
    monkeypatch.setattr(phase4, "SQLiteExchangePersistence", lambda *args, **kwargs: store)
    monkeypatch.setattr(phase4.httpx, "HTTPTransport", _Closeable)
    monkeypatch.setattr(phase4.httpx, "Client", lambda *args, **kwargs: client)
    monkeypatch.setattr(phase4, "BinanceSpotTestnetAdapter", lambda *args, **kwargs: adapter)
    monkeypatch.setattr(phase4, "SQLiteOrderIdentityResolver", lambda value: object())
    monkeypatch.setattr(phase4, "run_phase4", lambda *args, **kwargs: _result())
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "key")
    monkeypatch.setenv("BINANCE_TESTNET_SECRET_KEY", "secret")
    assert phase4.main() == 1
    assert evidence.read_text(encoding="utf-8") == "preserved"
    assert store.close_count == client.close_count == adapter.close_count == 1


def test_main_closes_store_when_client_construction_fails(monkeypatch) -> None:
    store = _Closeable()
    monkeypatch.setattr(phase4, "validate_startup", lambda *args, **kwargs: "phase3-account")
    monkeypatch.setattr(phase4, "SQLiteExchangePersistence", lambda *args, **kwargs: store)
    monkeypatch.setattr(phase4.httpx, "HTTPTransport", _Closeable)
    monkeypatch.setattr(
        phase4.httpx,
        "Client",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("client failed")),
    )
    assert phase4.main() == 1
    assert store.close_count == 1
