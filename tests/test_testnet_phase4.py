import ast
import json
import shutil
import socket
import threading
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest

import app.tools.testnet_phase4 as phase4
import app.tools.testnet_phase4_remediate_zero_fee as remediation
from app.exchange_runtime.identity import SQLiteOrderIdentityResolver
from app.infrastructure.binance import BinanceSpotTestnetAdapter, BinanceSpotTestnetConfig
from app.persistence.exceptions import PersistenceIntegrityError
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


def phase4_response(
    request: httpx.Request,
    *,
    status="FILLED",
    fee_asset="USDT",
    quote_value: str | None = None,
    commission_value: str | None = None,
):
    if request.url.path == "/api/v3/exchangeInfo":
        return _phase3_response(request)
    if request.url.path == "/api/v3/order":
        query = parse_qs(request.url.query.decode())
        assert query["origClientOrderId"][0].startswith("ctt1t-")
        quantity = "0.00008" if status == "PARTIALLY_FILLED" else "0.00016"
        quote = quote_value or ("5" if status == "PARTIALLY_FILLED" else "10")
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
                    "quoteQty": quote_value or ("5" if partial else "10"),
                    "commission": commission_value or ("0.005" if partial else "0.01"),
                    "commissionAsset": fee_asset,
                    "time": 1020,
                    "isBuyer": True,
                    "isMaker": False,
                }
            ],
        )
    if request.url.path == "/api/v3/account":
        partial = status == "PARTIALLY_FILLED"
        fee = Decimal(commission_value or ("0.005" if partial else "0.01"))
        quantity = Decimal("0.00008") if partial else Decimal("0.00016")
        quote = Decimal(quote_value or ("5" if partial else "10"))
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
    store = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=6))
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


def test_zero_unsupported_fee_label_is_resolved_asset_neutrally(tmp_path) -> None:
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
    assert result.outcome is Phase4Outcome.PASS
    assert result.managed_btc_after == Decimal("0.00016")
    assert result.managed_usdt_after == 15
    trade = store._connection.execute(
        "SELECT commission,commission_asset FROM exchange_trades"
    ).fetchone()
    assert tuple(trade) == ("0", "BNB")
    assert (
        store._connection.execute(
            "SELECT count(*) FROM exchange_trade_settlements WHERE status='BLOCKED_UNSUPPORTED_FEE'"
        ).fetchone()[0]
        == 1
    )
    assert (
        store._connection.execute(
            "SELECT count(*) FROM exchange_trade_settlement_resolutions"
        ).fetchone()[0]
        == 1
    )
    adapter.close()
    client.close()
    store.close()


def test_production_style_existing_zero_btc_block_resolves_exactly(tmp_path) -> None:
    path = tmp_path / "production-style.sqlite3"
    phase3_database(path)
    handler = lambda request: phase4_response(
        request,
        fee_asset="BTC",
        quote_value="10.109264",
        commission_value="0",
    )
    store, guard, client, adapter = phase4_objects(path, handler)
    result = run_phase4(
        store,
        adapter,
        guard,
        expected_account="phase3-account",
        run_id="phase4-production-style",
        now_ms=iter(range(2000, 2100)).__next__,
    )
    assert result.outcome is Phase4Outcome.PASS
    assert result.managed_btc_after == Decimal("0.00016")
    assert result.managed_usdt_after == Decimal("14.890736")
    current = store._connection.execute(
        "SELECT portfolio_version,state_payload FROM exchange_portfolio_current"
    ).fetchone()
    payload = json.loads(current["state_payload"])
    assert current["portfolio_version"] == 1
    assert payload["total_fees"] == {"$decimal": "0"}
    assert payload["turnover"] == {"$decimal": "10.109264"}
    assert tuple(
        store._connection.execute(
            "SELECT commission,commission_asset FROM exchange_trades"
        ).fetchone()
    ) == ("0", "BTC")
    assert (
        store._connection.execute(
            "SELECT count(*) FROM exchange_trade_settlements WHERE status='BLOCKED_UNSUPPORTED_FEE'"
        ).fetchone()[0]
        == 1
    )
    assert (
        store._connection.execute(
            "SELECT count(*) FROM exchange_trade_settlement_resolutions"
        ).fetchone()[0]
        == 1
    )
    adapter.close()
    client.close()
    store.close()

    store, guard, client, adapter = phase4_objects(path, handler)
    repeated = run_phase4(
        store,
        adapter,
        guard,
        expected_account="phase3-account",
        run_id="phase4-production-style-repeat",
        now_ms=iter(range(3000, 3100)).__next__,
    )
    assert repeated.outcome is Phase4Outcome.PASS
    assert repeated.managed_btc_after == Decimal("0.00016")
    assert repeated.managed_usdt_after == Decimal("14.890736")
    assert (
        store._connection.execute(
            "SELECT count(*) FROM exchange_trade_settlement_resolutions"
        ).fetchone()[0]
        == 1
    )
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
    store = SQLiteExchangePersistence(SQLitePersistenceConfig(copy, expected_schema_version=6))
    recovered = recover_phase3(store, "phase3-account")
    assert recovered.client_order_id.startswith("ctt1t-")
    store.close()


def test_recovery_rejects_missing_or_wrong_lineage(tmp_path) -> None:
    path = tmp_path / "lineage.sqlite3"
    phase3_database(path)
    store = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=6))
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


def zero_fee_blocked_database(
    path: Path, *, commission: str = "0", commission_asset: str = "BTC"
) -> None:
    """Build the accepted durable shape without running schema-v6 resolution."""
    phase3_database(path)
    store = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=5))
    guard = Phase4ReadOnlyTransport(
        httpx.MockTransport(
            lambda request: phase4_response(
                request,
                fee_asset=commission_asset,
                quote_value="10.109264",
                commission_value=commission,
            )
        )
    )
    client = httpx.Client(base_url="https://testnet.binance.vision", transport=guard)
    adapter = BinanceSpotTestnetAdapter(
        BinanceSpotTestnetConfig("key", "secret", "phase3-account"),
        client=client,
        timestamp_provider=lambda: 2000,
        identity_resolver=SQLiteOrderIdentityResolver(store),
    )
    result = run_phase4(
        store,
        adapter,
        guard,
        expected_account="phase3-account",
        run_id="phase4-zero-fee-block",
        now_ms=iter(range(2000, 2100)).__next__,
    )
    assert result.outcome is Phase4Outcome.INCOMPLETE
    adapter.close()
    client.close()
    store.close()


def run_local_remediation(path: Path, *, failure_injector=None):
    store = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=6))
    try:
        result = remediation.run_remediation(
            store,
            run_id="offline-remediation",
            now_ms=iter(range(3000, 3200)).__next__,
            failure_injector=failure_injector,
        )
        return result
    finally:
        store.close()


def test_remediation_authorization_precedes_database_and_evidence(tmp_path) -> None:
    database = tmp_path / "smoke.sqlite3"
    evidence = tmp_path / "remediation.log"
    with pytest.raises(remediation.RemediationBlocked, match="authorization"):
        remediation.validate_startup({}, database=database, evidence=evidence)
    database.write_text("not opened", encoding="utf-8")
    evidence.write_text("preserved", encoding="utf-8")
    with pytest.raises(remediation.RemediationBlocked, match="evidence"):
        remediation.validate_startup(
            {remediation.AUTHORIZATION_ENV: "YES"}, database=database, evidence=evidence
        )
    assert remediation.AUTHORIZATION_ENV not in {
        "M4_10_PHASE3_AUTHORIZED",
        "M4_10_PHASE4_AUTHORIZED",
    }
    evidence.unlink()
    remediation.validate_startup(
        {remediation.AUTHORIZATION_ENV: "YES"}, database=database, evidence=evidence
    )
    database.unlink()
    with pytest.raises(remediation.RemediationBlocked, match="database"):
        remediation.validate_startup(
            {remediation.AUTHORIZATION_ENV: "YES"}, database=database, evidence=evidence
        )


def test_remediation_requires_schema_six(tmp_path) -> None:
    path = tmp_path / "schema-five.sqlite3"
    zero_fee_blocked_database(path)
    store = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=5))
    with pytest.raises(remediation.RemediationBlocked, match="schema version 6"):
        remediation.run_remediation(
            store,
            run_id="schema-five",
            now_ms=iter(range(3000, 3200)).__next__,
        )
    store.close()


def test_committed_v6_migration_alone_has_no_economic_effect_and_can_resume(tmp_path) -> None:
    path = tmp_path / "migration-only.sqlite3"
    zero_fee_blocked_database(path)
    migrated = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=6))
    assert (
        migrated._connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
        == 6
    )
    assert (
        migrated._connection.execute(
            "SELECT count(*) FROM exchange_trade_settlement_resolutions"
        ).fetchone()[0]
        == 0
    )
    current = migrated._connection.execute(
        "SELECT portfolio_version,state_payload FROM exchange_portfolio_current"
    ).fetchone()
    assert current["portfolio_version"] == 0
    payload = json.loads(current["state_payload"])
    assert payload["base_balance"] == {"$decimal": "0"}
    assert payload["quote_balance"] == {"$decimal": "25"}
    assert (
        migrated._connection.execute("SELECT status FROM exchange_trade_settlements").fetchone()[0]
        == "BLOCKED_UNSUPPORTED_FEE"
    )
    migrated.close()
    resumed = run_local_remediation(path)
    assert resumed.outcome is remediation.RemediationOutcome.PASS
    assert resumed.portfolio_version_after == 1


def test_zero_fee_remediation_is_exact_and_idempotent(tmp_path) -> None:
    path = tmp_path / "remediation.sqlite3"
    zero_fee_blocked_database(path)
    first = run_local_remediation(path)
    assert first.outcome is remediation.RemediationOutcome.PASS
    assert first.idempotent is False
    assert first.portfolio_version_before == 0
    assert first.portfolio_version_after == 1
    assert first.btc_after == Decimal("0.00016")
    assert first.usdt_after == Decimal("14.890736")
    assert first.turnover_after == Decimal("10.109264")
    second = run_local_remediation(path)
    assert second.outcome is remediation.RemediationOutcome.PASS
    assert second.idempotent is True
    assert second.portfolio_version_before == second.portfolio_version_after == 1
    assert second.resolution_id == first.resolution_id
    store = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=6))
    assert (
        store._connection.execute(
            "SELECT count(*) FROM exchange_trade_settlement_resolutions"
        ).fetchone()[0]
        == 1
    )
    assert tuple(
        store._connection.execute(
            "SELECT commission,commission_asset FROM exchange_trades"
        ).fetchone()
    ) == ("0", "BTC")
    assert (
        store._connection.execute("SELECT status FROM exchange_trade_settlements").fetchone()[0]
        == "BLOCKED_UNSUPPORTED_FEE"
    )
    store.close()


@pytest.mark.parametrize(
    "boundary",
    [
        "resolution_before_record",
        "resolution_after_record",
        "resolution_after_current",
        "resolution_after_turnover",
        "resolution_before_commit",
    ],
)
@pytest.mark.parametrize("crash", [RuntimeError, KeyboardInterrupt, SystemExit, GeneratorExit])
def test_remediation_precommit_crash_rolls_back_and_recovers_once(
    tmp_path, boundary, crash
) -> None:
    path = tmp_path / f"remediation-{boundary}-{crash.__name__}.sqlite3"
    zero_fee_blocked_database(path)

    def fail(point: str) -> None:
        if point == boundary:
            raise crash()

    with pytest.raises(crash):
        run_local_remediation(path, failure_injector=fail)
    recovered = run_local_remediation(path)
    assert recovered.outcome is remediation.RemediationOutcome.PASS
    assert recovered.idempotent is False


def test_remediation_lost_commit_ack_recovers_idempotently(tmp_path) -> None:
    path = tmp_path / "remediation-lost-ack.sqlite3"
    zero_fee_blocked_database(path)

    def fail(point: str) -> None:
        if point == "resolution_after_commit":
            raise RuntimeError("lost commit acknowledgement")

    with pytest.raises(RuntimeError, match="lost commit"):
        run_local_remediation(path, failure_injector=fail)
    recovered = run_local_remediation(path)
    assert recovered.idempotent is True
    assert recovered.portfolio_version_after == 1


def test_remediation_rejects_conflicting_already_resolved_truth(tmp_path) -> None:
    path = tmp_path / "resolved-conflict.sqlite3"
    zero_fee_blocked_database(path)
    run_local_remediation(path)
    store = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=6))
    store._connection.execute("UPDATE exchange_sessions SET executed_turnover='11'")
    store._connection.commit()
    with pytest.raises(remediation.RemediationBlocked, match="existing resolution"):
        remediation.run_remediation(
            store,
            run_id="resolved-conflict",
            now_ms=iter(range(3000, 3200)).__next__,
        )
    store.close()


def test_remediation_rejects_invalid_root_and_missing_current(tmp_path) -> None:
    for case in ("root", "current"):
        path = tmp_path / f"invalid-{case}.sqlite3"
        zero_fee_blocked_database(path)
        store = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=6))
        if case == "root":
            row = store._connection.execute(
                "SELECT portfolio_id,state_payload FROM exchange_portfolio_roots"
            ).fetchone()
            state = remediation.deserialize_portfolio_state(row["state_payload"])
            changed = replace(
                state,
                quote_balance=Decimal(24),
                initial_equity=Decimal(24),
                current_equity=Decimal(24),
                peak_equity=Decimal(24),
            )
            store._connection.execute(
                "UPDATE exchange_portfolio_roots SET state_payload=?,state_digest=? "
                "WHERE portfolio_id=?",
                (changed.canonical_payload, changed.digest, row["portfolio_id"]),
            )
            message = "managed Portfolio"
            error = PersistenceIntegrityError
        else:
            store._connection.execute("DELETE FROM exchange_portfolio_current")
            message = "current state is absent"
            error = remediation.RemediationBlocked
        store._connection.commit()
        try:
            with pytest.raises(error, match=message):
                remediation.run_remediation(
                    store,
                    run_id=f"invalid-{case}",
                    now_ms=iter(range(3000, 3200)).__next__,
                )
        finally:
            store.close()


def test_remediation_requires_single_root_and_exact_postcondition(tmp_path, monkeypatch) -> None:
    missing = tmp_path / "missing-root.sqlite3"
    zero_fee_blocked_database(missing)
    store = SQLiteExchangePersistence(SQLitePersistenceConfig(missing, expected_schema_version=6))
    store._connection.execute("PRAGMA foreign_keys=OFF")
    store._connection.execute("DELETE FROM exchange_portfolio_roots")
    store._connection.commit()
    with pytest.raises(remediation.RemediationBlocked, match="exactly one"):
        remediation._managed_portfolio(store)
    store.close()

    path = tmp_path / "invalid-postcondition.sqlite3"
    zero_fee_blocked_database(path)
    store = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=6))

    class IncompleteSettlement:
        def __init__(self, *args, **kwargs):
            pass

        def _coverage_is_complete(self, reconciliation):
            return True

        def initialize(self):
            pass

        def settle_pending(self):
            return SimpleNamespace(status=remediation.ExchangeSettlementStatus.PENDING)

    monkeypatch.setattr(remediation, "SQLiteExchangeSettlement", IncompleteSettlement)
    with pytest.raises(remediation.RemediationFailed, match="synchronized durable truth"):
        remediation.run_remediation(
            store,
            run_id="invalid-postcondition",
            now_ms=iter(range(3000, 3200)).__next__,
        )
    store.close()


@pytest.mark.parametrize(
    ("mutation", "error", "message"),
    [
        (
            "UPDATE exchange_reconciliation_current SET status='REQUIRED'",
            PersistenceIntegrityError,
            "reconciliation state digest mismatch",
        ),
        (
            "UPDATE exchange_sessions SET executed_turnover='1'",
            remediation.RemediationBlocked,
            "Portfolio/session",
        ),
    ],
)
def test_remediation_wrong_state_fails_closed(tmp_path, mutation, error, message) -> None:
    path = tmp_path / "wrong.sqlite3"
    zero_fee_blocked_database(path)
    store = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=6))
    store._connection.execute(mutation)
    store._connection.commit()
    with pytest.raises(error, match=message):
        remediation.run_remediation(
            store,
            run_id="wrong-state",
            now_ms=iter(range(3000, 3200)).__next__,
        )
    assert (
        store._connection.execute(
            "SELECT count(*) FROM exchange_trade_settlement_resolutions"
        ).fetchone()[0]
        == 0
    )
    store.close()


@pytest.mark.parametrize("asset", ["BTC", "BNB", "ETH"])
def test_positive_unsupported_fee_remediation_remains_blocked(tmp_path, asset) -> None:
    path = tmp_path / f"positive-{asset}.sqlite3"
    zero_fee_blocked_database(path, commission="0.00000001", commission_asset=asset)
    store = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=6))
    with pytest.raises(remediation.RemediationBlocked, match="trade/block lineage"):
        remediation.run_remediation(
            store,
            run_id="positive-fee",
            now_ms=iter(range(3000, 3200)).__next__,
        )
    assert (
        store._connection.execute(
            "SELECT count(*) FROM exchange_trade_settlement_resolutions"
        ).fetchone()[0]
        == 0
    )
    store.close()


@pytest.mark.parametrize(
    ("trigger", "table"),
    [
        ("exchange_trade_settlements_no_delete", "exchange_trade_settlements"),
        ("exchange_reconciliation_coverage_no_delete", "exchange_reconciliation_coverage"),
        ("exchange_smoke_authority_events_no_delete", "exchange_smoke_authority_events"),
    ],
)
def test_remediation_missing_durable_authority_fails_before_resolution(
    tmp_path, trigger, table
) -> None:
    path = tmp_path / f"missing-{table}.sqlite3"
    zero_fee_blocked_database(path)
    store = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=6))
    store._connection.execute(f"DROP TRIGGER {trigger}")
    store._connection.execute(f"DELETE FROM {table}")
    store._connection.commit()
    with pytest.raises((PersistenceIntegrityError, remediation.RemediationBlocked)):
        remediation.run_remediation(
            store,
            run_id="missing-authority",
            now_ms=iter(range(3000, 3200)).__next__,
        )
    assert (
        store._connection.execute(
            "SELECT count(*) FROM exchange_trade_settlement_resolutions"
        ).fetchone()[0]
        == 0
    )
    store.close()


def test_remediation_architecture_is_local_only() -> None:
    path = Path("app/tools/testnet_phase4_remediate_zero_fee.py")
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    }
    direct = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not imported & {
        "app.main",
        "app.infrastructure.binance",
        "app.exchange_runtime.submission",
        "app.exchange_runtime.consumer",
        "app.exchange_execution.phase3",
    }
    assert not direct & {"httpx", "socket"}
    for forbidden in (
        "BINANCE_TESTNET_API_KEY",
        "BINANCE_TESTNET_SECRET_KEY",
        "submit_market_order(",
        "POST",
        "cancel",
        "Phase3SmokeClaim",
    ):
        assert forbidden not in text


def test_remediation_main_evidence_is_exclusive_and_secret_free(tmp_path, monkeypatch) -> None:
    database = tmp_path / "smoke.sqlite3"
    evidence = tmp_path / "remediation.log"
    zero_fee_blocked_database(database)
    monkeypatch.setattr(remediation, "DATABASE_PATH", database)
    monkeypatch.setattr(remediation, "EVIDENCE_PATH", evidence)
    monkeypatch.setenv(remediation.AUTHORIZATION_ENV, "YES")
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "must-not-be-read")
    monkeypatch.setenv("BINANCE_TESTNET_SECRET_KEY", "must-not-be-read")
    assert remediation.main() == 0
    rendered = evidence.read_text(encoding="utf-8")
    assert "outcome=PASS" in rendered
    assert rendered.splitlines()[-1] == "outcome=PASS"
    assert "must-not-be-read" not in rendered
    preserved = rendered
    assert remediation.main() == 2
    assert evidence.read_text(encoding="utf-8") == preserved


def test_remediation_evidence_collision_after_commit_preserves_durable_truth(
    tmp_path, monkeypatch
) -> None:
    database = tmp_path / "smoke.sqlite3"
    evidence = tmp_path / "remediation.log"
    zero_fee_blocked_database(database)
    monkeypatch.setattr(remediation, "DATABASE_PATH", database)
    monkeypatch.setattr(remediation, "EVIDENCE_PATH", evidence)
    monkeypatch.setenv(remediation.AUTHORIZATION_ENV, "YES")
    original = remediation.run_remediation

    def collide(*args, **kwargs):
        result = original(*args, **kwargs)
        evidence.write_text("collision", encoding="utf-8")
        return result

    monkeypatch.setattr(remediation, "run_remediation", collide)
    assert remediation.main() == 1
    assert evidence.read_text(encoding="utf-8") == "collision"
    recovered = run_local_remediation(database)
    assert recovered.idempotent is True
    assert recovered.portfolio_version_after == 1


def test_short_evidence_write_cannot_leave_pass_marker(tmp_path, monkeypatch) -> None:
    database = tmp_path / "smoke.sqlite3"
    zero_fee_blocked_database(database)

    class ShortEvidence:
        content = ""
        parent = None

        def __init__(self):
            self.parent = self

        def exists(self):
            return False

        def mkdir(self, **kwargs):
            pass

        def open(self, *args, **kwargs):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def write(self, value):
            self.content = value[:-20]
            return len(self.content)

    evidence = ShortEvidence()
    monkeypatch.setattr(remediation, "DATABASE_PATH", database)
    monkeypatch.setattr(remediation, "EVIDENCE_PATH", evidence)
    monkeypatch.setenv(remediation.AUTHORIZATION_ENV, "YES")
    assert remediation.main() == 1
    assert "outcome=PASS" not in evidence.content
    recovered = run_local_remediation(database)
    assert recovered.idempotent is True


def test_two_independent_remediation_actors_apply_one_economic_transition(tmp_path) -> None:
    path = tmp_path / "concurrent-remediation.sqlite3"
    zero_fee_blocked_database(path)
    migrated = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=6))
    migrated.close()
    barrier = threading.Barrier(2)
    results = []

    def actor(index: int) -> None:
        store = None
        try:
            store = SQLiteExchangePersistence(
                SQLitePersistenceConfig(path, expected_schema_version=6)
            )
            barrier.wait(timeout=5)
            results.append(
                remediation.run_remediation(
                    store,
                    run_id=f"concurrent-{index}",
                    now_ms=iter(range(4000 + index * 100, 4100 + index * 100)).__next__,
                )
            )
        finally:
            if store is not None:
                store.close()

    threads = [threading.Thread(target=actor, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads)
    assert len(results) == 2
    assert sum(not result.idempotent for result in results) == 1
    store = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=6))
    assert (
        store._connection.execute(
            "SELECT count(*) FROM exchange_trade_settlement_resolutions"
        ).fetchone()[0]
        == 1
    )
    current = store._connection.execute(
        "SELECT portfolio_version,state_payload FROM exchange_portfolio_current"
    ).fetchone()
    assert current["portfolio_version"] == 1
    assert json.loads(current["state_payload"])["turnover"] == {"$decimal": "10.109264"}
    store.close()


def test_remediation_main_preserves_interrupt_and_classifies_local_block(
    tmp_path, monkeypatch
) -> None:
    database = tmp_path / "smoke.sqlite3"
    evidence = tmp_path / "remediation.log"
    zero_fee_blocked_database(database)
    monkeypatch.setattr(remediation, "DATABASE_PATH", database)
    monkeypatch.setattr(remediation, "EVIDENCE_PATH", evidence)
    monkeypatch.setenv(remediation.AUTHORIZATION_ENV, "YES")
    monkeypatch.setattr(
        remediation,
        "run_remediation",
        lambda *args, **kwargs: (_ for _ in ()).throw(remediation.RemediationBlocked("local")),
    )
    assert remediation.main() == 2
    monkeypatch.setattr(
        remediation,
        "run_remediation",
        lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    with pytest.raises(KeyboardInterrupt):
        remediation.main()
