import ast
import logging
import socket
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.exchange_execution.ports import (
    AccountBalance,
    ExchangeInstrumentRules,
)
from app.infrastructure.binance import (
    AccountTradeObservation,
    BinanceSpotTestnetAdapter,
    BinanceSpotTestnetConfig,
    OpenOrderObservation,
)
from app.market.models import MarketStreamKey
from app.tools import testnet_phase1
from app.tools.testnet_phase1 import (
    MAX_REQUESTS,
    Phase1GuardTransport,
    Phase1Outcome,
    Phase1ReadFacade,
    Phase1TransportViolation,
    run_phase1,
)

D = Decimal
KEY = MarketStreamKey("BTCUSDT", "1m")
API_KEY = "PHASE1_FAKE_KEY_NEVER_LEAK"
SECRET = "PHASE1_FAKE_SECRET_NEVER_LEAK"
FINGERPRINT = "dedicated-testnet-account"
CLIENT_ID = "ctt1t-abcdefghijklmnopqrstuvwxyz"


class NoIdentities:
    def by_client_order_id(self, client_order_id: str):
        return None

    def by_exchange_order_id(self, exchange_order_id: str):
        return None


def exchange_info() -> dict[str, object]:
    return {
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                    {
                        "filterType": "MARKET_LOT_SIZE",
                        "stepSize": "0.00001",
                        "minQty": "0.00001",
                        "maxQty": "1000",
                    },
                    {"filterType": "MIN_NOTIONAL", "minNotional": "5", "applyToMarket": True},
                ],
            }
        ]
    }


def make_harness(handler):
    inner = httpx.MockTransport(handler)
    guard = Phase1GuardTransport(inner)
    client = httpx.Client(transport=guard)
    adapter = BinanceSpotTestnetAdapter(
        BinanceSpotTestnetConfig(API_KEY, SECRET, FINGERPRINT),
        client=client,
        timestamp_provider=lambda: 1_700_000_000_000,
        identity_resolver=NoIdentities(),
    )
    return guard, client, adapter, Phase1ReadFacade(adapter)


def test_full_phase1_success_is_exactly_four_gets_and_sanitized(caplog, monkeypatch) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payloads = {
            "/api/v3/exchangeInfo": exchange_info(),
            "/api/v3/account": {
                "balances": [
                    {"asset": "BTC", "free": "0", "locked": "0"},
                    {"asset": "USDT", "free": "10000", "locked": "0"},
                ]
            },
            "/api/v3/openOrders": [],
            "/api/v3/myTrades": [],
        }
        return httpx.Response(200, json=payloads[request.url.path])

    guard, client, adapter, facade = make_harness(handler)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: pytest.fail("DNS attempted"))
    monkeypatch.setattr(
        socket, "create_connection", lambda *args, **kwargs: pytest.fail("socket attempted")
    )
    try:
        with caplog.at_level(logging.DEBUG):
            result = run_phase1(
                facade,
                guard,
                account_fingerprint=FINGERPRINT,
                run_id="phase1-run",
                now_ms=1_700_000_000_001,
            )
    finally:
        adapter.close()
        client.close()
    assert result.outcome is Phase1Outcome.PASS
    assert result.request_count == 4
    assert [request.method for request in requests] == ["GET"] * 4
    assert [request.url.path for request in requests] == [
        "/api/v3/exchangeInfo",
        "/api/v3/account",
        "/api/v3/openOrders",
        "/api/v3/myTrades",
    ]
    rendered = result.render() + caplog.text
    assert API_KEY not in rendered
    assert SECRET not in rendered
    assert "signature=" not in rendered


class FakeReader:
    def __init__(self, *, open_orders=(), trades=()):
        self.open_orders = open_orders
        self.trades = trades

    def read_instrument_rules(self, stream_key):
        return ExchangeInstrumentRules(KEY, D("0.00001"), D("0.00001"), D("5"), "rules", D("1000"))

    def read_balances(self, assets):
        return (AccountBalance("BTC", D("0"), D("0")), AccountBalance("USDT", D("10"), D("0")))

    def read_open_order_observations(self, *, stream_key):
        return self.open_orders

    def read_account_trade_observations(self, *, stream_key, start_at_ms):
        return self.trades


class CountingGuard:
    def __init__(self) -> None:
        self.request_count = 4
        self.endpoint_counts = {"GET fake": 4}


def trade(*, client_id=CLIENT_ID, commission_asset="USDT") -> AccountTradeObservation:
    return AccountTradeObservation("1", client_id, commission_asset)


def run_fake(reader: FakeReader):
    return run_phase1(
        Phase1ReadFacade(reader),
        CountingGuard(),  # type: ignore[arg-type]
        account_fingerprint=FINGERPRINT,
        run_id="run",
        now_ms=1000,
    )


def test_open_order_blocks_and_stops_before_trade_history() -> None:
    reader = FakeReader(open_orders=(OpenOrderObservation("external-order"),))
    result = run_fake(reader)
    assert result.outcome is Phase1Outcome.BLOCKED
    assert result.stop_reasons == ("unexpected_open_order",)
    assert result.unmanaged_open_order_count == 1


def test_unknown_open_order_is_counted_without_durable_identity() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payloads = {
            "/api/v3/exchangeInfo": exchange_info(),
            "/api/v3/account": {
                "balances": [
                    {"asset": "BTC", "free": "0", "locked": "0"},
                    {"asset": "USDT", "free": "100", "locked": "0"},
                ]
            },
            "/api/v3/openOrders": [{"symbol": "BTCUSDT", "clientOrderId": "external-order"}],
        }
        return httpx.Response(200, json=payloads[request.url.path])

    guard, client, adapter, facade = make_harness(handler)
    try:
        result = run_phase1(
            facade, guard, account_fingerprint=FINGERPRINT, run_id="run", now_ms=1000
        )
    finally:
        adapter.close()
        client.close()
    assert result.outcome is Phase1Outcome.BLOCKED
    assert result.request_count == 3
    assert result.open_order_count == 1
    assert result.unmanaged_open_order_count == 1


def test_managed_trade_blocks_without_mutation() -> None:
    result = run_fake(FakeReader(trades=(trade(),)))
    assert result.outcome is Phase1Outcome.BLOCKED
    assert result.managed_trade_count == 1


def test_unsupported_commission_blocks_without_conversion() -> None:
    result = run_fake(FakeReader(trades=(trade(client_id="external", commission_asset="BNB"),)))
    assert result.outcome is Phase1Outcome.BLOCKED
    assert result.unsupported_commission_count == 1


def test_unknown_account_trade_is_counted_and_blocks_without_durable_identity() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payloads = {
            "/api/v3/exchangeInfo": exchange_info(),
            "/api/v3/account": {
                "balances": [
                    {"asset": "BTC", "free": "0", "locked": "0"},
                    {"asset": "USDT", "free": "100", "locked": "0"},
                ]
            },
            "/api/v3/openOrders": [],
            "/api/v3/myTrades": [{"symbol": "BTCUSDT", "orderId": 987, "commissionAsset": "USDT"}],
        }
        return httpx.Response(200, json=payloads[request.url.path])

    guard, client, adapter, facade = make_harness(handler)
    try:
        result = run_phase1(
            facade, guard, account_fingerprint=FINGERPRINT, run_id="run", now_ms=1000
        )
    finally:
        adapter.close()
        client.close()
    assert result.outcome is Phase1Outcome.BLOCKED
    assert result.recent_trade_count == 1
    assert result.managed_trade_count == 0
    assert result.stop_reasons == ("unexpected_trade_history",)


@pytest.mark.parametrize("status", [400, 418, 429])
def test_auth_timestamp_and_rate_limit_fail_once_without_retry(status: int) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if request.url.path == "/api/v3/exchangeInfo":
            return httpx.Response(200, json=exchange_info())
        return httpx.Response(
            status,
            json={"code": -1021, "msg": "sensitive"},
            headers={"Retry-After": "7"} if status in {418, 429} else None,
        )

    guard, client, adapter, facade = make_harness(handler)
    try:
        result = run_phase1(facade, guard, account_fingerprint=FINGERPRINT, run_id="run", now_ms=1)
    finally:
        adapter.close()
        client.close()
    assert result.outcome is Phase1Outcome.FAILED
    assert calls == 2
    assert "sensitive" not in result.render()
    assert result.retry_after_seconds == (7 if status in {418, 429} else None)


def test_budget_rejects_sixth_request_before_inner_transport() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    guard = Phase1GuardTransport(httpx.MockTransport(handler))
    client = httpx.Client(transport=guard)
    try:
        for _ in range(MAX_REQUESTS):
            client.get("https://testnet.binance.vision/api/v3/account")
        with pytest.raises(Phase1TransportViolation, match="budget"):
            client.get("https://testnet.binance.vision/api/v3/account")
    finally:
        client.close()
    assert calls == MAX_REQUESTS


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_guard_rejects_forbidden_methods_before_transport(method: str) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    client = httpx.Client(transport=Phase1GuardTransport(httpx.MockTransport(handler)))
    try:
        with pytest.raises(Phase1TransportViolation, match="not approved"):
            client.request(method, "https://testnet.binance.vision/api/v3/order/test")
    finally:
        client.close()
    assert calls == 0


@pytest.mark.parametrize(
    "url",
    [
        "http://testnet.binance.vision/api/v3/account",
        "https://testnet.binance.vision:8443/api/v3/account",
        "https://api.binance.com/api/v3/account",
        "https://testnet.binance.vision/api/v3/time",
    ],
)
def test_guard_rejects_wrong_authority_or_unapproved_get_before_transport(url: str) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    client = httpx.Client(transport=Phase1GuardTransport(httpx.MockTransport(handler)))
    try:
        with pytest.raises(Phase1TransportViolation):
            client.get(url)
    finally:
        client.close()
    assert calls == 0


def test_facade_has_no_submit_order_test_cancel_or_query_order_api() -> None:
    public = {name for name in dir(Phase1ReadFacade) if not name.startswith("_")}
    assert public == {
        "read_account_trade_observations",
        "read_balances",
        "read_instrument_rules",
        "read_open_order_observations",
    }


def test_phase1_module_has_no_submission_or_trader_imports_or_calls() -> None:
    path = Path(testnet_phase1.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    forbidden = {
        "TradingDecisionCoordinator",
        "TradingDecisionPipeline",
        "TestnetExecutionConsumer",
        "TestnetExecutionApplicationService",
        "TestnetPreSubmitGate",
        "SubmissionCapability",
        "DurableExchangeOrderLifecycle",
        "submit_market_order",
        "test_market_order",
        "begin_submission",
        "submit_authorized",
    }
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    attrs = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert not forbidden & (names | attrs)
    source = path.read_text(encoding="utf-8")
    assert "app.main" not in source
    assert "app.runtime.application" not in source
    assert "app.exchange_runtime" not in source
    assert "app.persistence" not in source


def test_cli_requires_exact_authorization_before_transport(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("M4_10_PHASE1_AUTHORIZED", raising=False)
    monkeypatch.setattr(httpx, "HTTPTransport", lambda: pytest.fail("transport constructed"))
    assert testnet_phase1.main() == 2
    assert "authorization" in capsys.readouterr().err


def test_cli_rejects_wrong_authorization_before_transport(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("M4_10_PHASE1_AUTHORIZED", "PHASE2")
    monkeypatch.setattr(httpx, "HTTPTransport", lambda: pytest.fail("transport constructed"))
    assert testnet_phase1.main() == 2


def test_cli_refuses_existing_evidence_before_credentials_or_transport(
    monkeypatch, tmp_path, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    evidence = tmp_path / "logs/m4_10_phase1_retry1.log"
    evidence.parent.mkdir()
    evidence.write_text("old", encoding="utf-8")
    monkeypatch.setenv("M4_10_PHASE1_AUTHORIZED", "YES")
    monkeypatch.setattr(httpx, "HTTPTransport", lambda: pytest.fail("transport constructed"))
    assert testnet_phase1.main() == 2
    assert "already exists" in capsys.readouterr().err


def test_keyboard_interrupt_propagates_without_retry() -> None:
    class Interrupted(FakeReader):
        def read_instrument_rules(self, stream_key):
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_fake(Interrupted())


def test_cli_mock_success_writes_sanitized_evidence_and_closes_once(monkeypatch, tmp_path) -> None:
    close_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        payloads = {
            "/api/v3/exchangeInfo": exchange_info(),
            "/api/v3/account": {
                "balances": [
                    {"asset": "BTC", "free": "0", "locked": "0"},
                    {"asset": "USDT", "free": "100", "locked": "0"},
                ]
            },
            "/api/v3/openOrders": [],
            "/api/v3/myTrades": [],
        }
        return httpx.Response(200, json=payloads[request.url.path])

    class TrackingMockTransport(httpx.MockTransport):
        def close(self) -> None:
            nonlocal close_count
            close_count += 1
            super().close()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("M4_10_PHASE1_AUTHORIZED", "YES")
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", API_KEY)
    monkeypatch.setenv("BINANCE_TESTNET_SECRET_KEY", SECRET)
    monkeypatch.setenv("BINANCE_TESTNET_ACCOUNT_FINGERPRINT", FINGERPRINT)
    monkeypatch.setattr(httpx, "HTTPTransport", lambda: TrackingMockTransport(handler))

    assert testnet_phase1.main() == 0
    evidence = (tmp_path / "logs/m4_10_phase1_retry1.log").read_text(encoding="utf-8")
    assert "outcome=PASS" in evidence
    assert "request_count=4" in evidence
    assert "rules_quantity_step=0.00001" in evidence
    assert API_KEY not in evidence
    assert SECRET not in evidence
    assert "signature=" not in evidence
    assert close_count == 1
