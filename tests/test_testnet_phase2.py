import ast
import logging
import socket
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.infrastructure.binance import (
    BinanceSpotTestnetAdapter,
    BinanceSpotTestnetConfig,
    sign_hmac_sha256,
)
from app.tools import testnet_phase2
from app.tools.testnet_phase2 import (
    MAX_REQUESTS,
    QUANTITY,
    REFERENCE_PRICE,
    Phase2GuardTransport,
    Phase2OrderTestFacade,
    Phase2Outcome,
    Phase2TransportViolation,
    Phase2ValidationError,
    phase2_client_order_id,
    run_phase2,
    validate_reviewed_inputs,
)

D = Decimal
API_KEY = "PHASE2_FAKE_KEY_NEVER_LEAK"
SECRET = "PHASE2_FAKE_SECRET_NEVER_LEAK"
FINGERPRINT = "gleb-testnet-01"


@pytest.fixture(autouse=True)
def forbid_real_network(monkeypatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: pytest.fail("DNS attempted"))
    monkeypatch.setattr(
        socket, "create_connection", lambda *args, **kwargs: pytest.fail("socket attempted")
    )


class NoIdentities:
    def by_client_order_id(self, client_order_id: str):
        return None

    def by_exchange_order_id(self, exchange_order_id: str):
        return None


def make_harness(handler):
    inner = httpx.MockTransport(handler)
    guard = Phase2GuardTransport(inner)
    client = httpx.Client(transport=guard)
    adapter = BinanceSpotTestnetAdapter(
        BinanceSpotTestnetConfig(API_KEY, SECRET, FINGERPRINT),
        client=client,
        timestamp_provider=lambda: 1_700_000_000_000,
        identity_resolver=NoIdentities(),
    )
    return guard, client, adapter, Phase2OrderTestFacade(adapter)


def test_full_phase2_success_is_exactly_one_signed_order_test(monkeypatch, caplog) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    guard, client, adapter, facade = make_harness(handler)
    try:
        with caplog.at_level(logging.DEBUG):
            result = run_phase2(
                facade,
                guard,
                account_fingerprint=FINGERPRINT,
                run_id="phase2-run",
                now_ms=1_700_000_000_001,
            )
    finally:
        adapter.close()
        client.close()

    assert result.outcome is Phase2Outcome.PASS
    assert result.validated
    assert result.request_count == 1
    assert result.endpoint_counts == {"POST /api/v3/order/test": 1}
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url == "https://testnet.binance.vision/api/v3/order/test"
    assert request.headers["X-MBX-APIKEY"] == API_KEY
    unsigned, signature = request.content.decode().rsplit("&signature=", 1)
    client_id = phase2_client_order_id(FINGERPRINT)
    assert unsigned == (
        "symbol=BTCUSDT&side=BUY&type=MARKET&quantity=0.00016000&"
        f"newClientOrderId={client_id}&newOrderRespType=ACK&"
        "timestamp=1700000000000&recvWindow=5000"
    )
    assert signature == sign_hmac_sha256(unsigned, SECRET)
    assert len(client_id) == 32 and client_id.startswith("ctt1t-")
    rendered = result.render() + caplog.text
    for forbidden in (API_KEY, SECRET, signature, unsigned, "signature=", "X-MBX-APIKEY"):
        assert forbidden not in rendered


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("GET", "https://testnet.binance.vision/api/v3/order/test"),
        ("POST", "https://testnet.binance.vision/api/v3/order"),
        ("DELETE", "https://testnet.binance.vision/api/v3/order"),
        ("POST", "https://api.binance.com/api/v3/order/test"),
        ("POST", "https://testnet.binance.vision:8443/api/v3/order/test"),
    ],
)
def test_guard_rejects_every_other_method_path_or_authority_before_transport(
    method: str, url: str
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    client = httpx.Client(transport=Phase2GuardTransport(httpx.MockTransport(handler)))
    try:
        with pytest.raises(Phase2TransportViolation):
            client.request(method, url)
    finally:
        client.close()
    assert calls == 0


def test_second_request_is_rejected_before_transport() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    client = httpx.Client(transport=Phase2GuardTransport(httpx.MockTransport(handler)))
    try:
        client.post("https://testnet.binance.vision/api/v3/order/test")
        with pytest.raises(Phase2TransportViolation, match="budget"):
            client.post("https://testnet.binance.vision/api/v3/order/test")
    finally:
        client.close()
    assert MAX_REQUESTS == 1
    assert calls == 1


@pytest.mark.parametrize(
    ("quantity", "price", "message"),
    [
        (0.00016, REFERENCE_PRICE, "Decimals"),
        (D("0"), REFERENCE_PRICE, "Decimals"),
        (D("NaN"), REFERENCE_PRICE, "Decimals"),
        (QUANTITY, 63849.22, "Decimals"),
        (D("0.000005"), REFERENCE_PRICE, "minQty"),
        (D("0.000015"), REFERENCE_PRICE, "stepSize"),
        (D("0.00001000"), D("100"), "margin"),
    ],
)
def test_invalid_quantity_or_reference_fails_before_transport(quantity, price, message) -> None:
    with pytest.raises(Phase2ValidationError, match=message):
        validate_reviewed_inputs(quantity=quantity, reference_price=price)  # type: ignore[arg-type]


def test_reviewed_quantity_is_aligned_and_has_two_x_minimum_margin() -> None:
    validate_reviewed_inputs(quantity=QUANTITY, reference_price=REFERENCE_PRICE)
    assert QUANTITY % D("0.00001000") == 0
    assert QUANTITY * REFERENCE_PRICE == D("10.2158752000000000")
    assert QUANTITY * REFERENCE_PRICE >= D("10.00000000")


def test_client_order_id_is_deterministic_and_non_durable() -> None:
    first = phase2_client_order_id(FINGERPRINT)
    assert first == phase2_client_order_id(FINGERPRINT)
    assert first != phase2_client_order_id("another-testnet-account")


def test_facade_exposes_only_order_test() -> None:
    public = {name for name in dir(Phase2OrderTestFacade) if not name.startswith("_")}
    assert public == {"test_market_order"}


def test_phase2_module_has_no_submission_runtime_or_persistence_dependencies() -> None:
    path = Path(testnet_phase2.__file__)
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden = {
        "DurableExchangeOrderLifecycle",
        "TestnetPreSubmitGate",
        "SubmissionCapability",
        "submit_market_order",
        "submit_authorized",
        "begin_submission",
        "TestnetExecutionConsumer",
        "TradingDecisionCoordinator",
        "TradingDecisionPipeline",
        "SQLiteExchangePersistence",
        "Portfolio",
    }
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    attrs = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert not forbidden & (names | attrs)
    for forbidden_module in (
        "app.main",
        "app.runtime.application",
        "app.exchange_runtime",
        "app.persistence",
        "app.portfolio",
    ):
        assert forbidden_module not in source


@pytest.mark.parametrize(
    "environment",
    [
        {},
        {"M4_10_PHASE1_AUTHORIZED": "YES"},
        {"M4_10_PHASE2_AUTHORIZED": "NO"},
    ],
)
def test_cli_requires_exact_phase2_authorization_before_transport(
    monkeypatch, tmp_path, environment
) -> None:
    monkeypatch.chdir(tmp_path)
    for name in ("M4_10_PHASE1_AUTHORIZED", "M4_10_PHASE2_AUTHORIZED"):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(httpx, "HTTPTransport", lambda: pytest.fail("transport constructed"))
    assert testnet_phase2.main() == 2


def test_cli_refuses_existing_evidence_before_credentials_or_transport(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.chdir(tmp_path)
    evidence = tmp_path / "logs/m4_10_phase2.log"
    evidence.parent.mkdir()
    evidence.write_text("old", encoding="utf-8")
    monkeypatch.setenv("M4_10_PHASE2_AUTHORIZED", "YES")
    monkeypatch.setattr(httpx, "HTTPTransport", lambda: pytest.fail("transport constructed"))
    assert testnet_phase2.main() == 2


@pytest.mark.parametrize(
    ("response", "expected_retry"),
    [
        (httpx.Response(500, text="raw-sensitive"), None),
        (httpx.Response(400, json={"code": -1021, "msg": "raw-sensitive"}), None),
        (httpx.Response(429, headers={"Retry-After": "7"}), 7),
        (httpx.Response(200, content=b"raw-sensitive"), None),
    ],
)
def test_failures_are_sanitized_and_never_retry(response, expected_retry) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return response

    guard, client, adapter, facade = make_harness(handler)
    try:
        result = run_phase2(facade, guard, account_fingerprint=FINGERPRINT, run_id="run", now_ms=1)
    finally:
        adapter.close()
        client.close()
    assert result.outcome is Phase2Outcome.FAILED
    assert not result.validated
    assert result.retry_after_seconds == expected_retry
    assert calls == 1
    assert "raw-sensitive" not in result.render()
    assert "RECONCILIATION_REQUIRED" not in result.render()


def test_keyboard_interrupt_propagates_without_retry() -> None:
    class Interrupted:
        def test_market_order(self, **kwargs):
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_phase2(
            Phase2OrderTestFacade(Interrupted()),  # type: ignore[arg-type]
            type("Guard", (), {"request_count": 0, "endpoint_counts": {}})(),  # type: ignore[arg-type]
            account_fingerprint=FINGERPRINT,
            run_id="run",
            now_ms=1,
        )


def test_cli_mock_success_writes_sanitized_evidence_and_closes_once(monkeypatch, tmp_path) -> None:
    close_count = 0

    class TrackingTransport(httpx.MockTransport):
        def close(self) -> None:
            nonlocal close_count
            close_count += 1
            super().close()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("M4_10_PHASE2_AUTHORIZED", "YES")
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", API_KEY)
    monkeypatch.setenv("BINANCE_TESTNET_SECRET_KEY", SECRET)
    monkeypatch.setenv("BINANCE_TESTNET_ACCOUNT_FINGERPRINT", FINGERPRINT)
    monkeypatch.setattr(
        httpx,
        "HTTPTransport",
        lambda: TrackingTransport(lambda request: httpx.Response(200, json={})),
    )

    assert testnet_phase2.main() == 0
    evidence = (tmp_path / "logs/m4_10_phase2.log").read_text(encoding="utf-8")
    assert "outcome=PASS" in evidence
    assert "request_count=1" in evidence
    assert "validated=True" in evidence
    assert "financial_semantics=none" in evidence
    for forbidden in (API_KEY, SECRET, "signature=", "X-MBX-APIKEY"):
        assert forbidden not in evidence
    assert close_count == 1


def test_cli_interrupt_closes_owned_transport_once(monkeypatch, tmp_path) -> None:
    close_count = 0

    class TrackingTransport(httpx.MockTransport):
        def close(self) -> None:
            nonlocal close_count
            close_count += 1
            super().close()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("M4_10_PHASE2_AUTHORIZED", "YES")
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", API_KEY)
    monkeypatch.setenv("BINANCE_TESTNET_SECRET_KEY", SECRET)
    monkeypatch.setenv("BINANCE_TESTNET_ACCOUNT_FINGERPRINT", FINGERPRINT)
    monkeypatch.setattr(
        httpx,
        "HTTPTransport",
        lambda: TrackingTransport(lambda request: pytest.fail("transport reached")),
    )
    monkeypatch.setattr(
        testnet_phase2,
        "run_phase2",
        lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt),
    )

    with pytest.raises(KeyboardInterrupt):
        testnet_phase2.main()
    assert close_count == 1
    assert not (tmp_path / "logs/m4_10_phase2.log").exists()
