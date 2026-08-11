import copy
from dataclasses import FrozenInstanceError
from decimal import Decimal
from urllib.parse import parse_qs

import httpx
import pytest

from app.exchange_execution.models import (
    ExchangeOrderSide,
    ExchangeOrderStatus,
    QueryResultKind,
    TransportFailureCategory,
    TransportOutcomeKind,
)
from app.exchange_execution.ports import MarketOrderSubmission
from app.infrastructure.binance import (
    BinanceAuthenticationError,
    BinanceConfigurationError,
    BinanceNormalizationError,
    BinanceOrderRejectedError,
    BinanceRateLimitError,
    BinanceResponseError,
    BinanceSpotTestnetAdapter,
    BinanceSpotTestnetConfig,
    BinanceTransportError,
    BinanceUnsupportedContractError,
    OrderIdentity,
    encode_parameters,
    sign_hmac_sha256,
)
from app.market.models import MarketStreamKey

D = Decimal
KEY = MarketStreamKey("BTCUSDT", "1m")
CLIENT_ID = "ctt1t-abcdefghijklmnopqrstuvwxyz"
COMMAND_KEY = "a" * 64
API_KEY = "TEST_API_KEY_DO_NOT_LEAK_123"
SECRET = "TEST_SECRET_DO_NOT_LEAK_456"


class Resolver:
    def __init__(self, identity: OrderIdentity | None = None, exchange_order_id: str = "123"):
        self.identity = identity
        self.exchange_order_id = exchange_order_id

    def by_client_order_id(self, client_order_id: str) -> OrderIdentity | None:
        return (
            self.identity
            if self.identity and client_order_id == self.identity.client_order_id
            else None
        )

    def by_exchange_order_id(self, exchange_order_id: str) -> OrderIdentity | None:
        return self.identity if exchange_order_id == self.exchange_order_id else None


def identity(side: ExchangeOrderSide = ExchangeOrderSide.BUY, quantity: str = "0.10000"):
    return OrderIdentity(COMMAND_KEY, CLIENT_ID, side, D(quantity), 1000)


def config(**overrides: object) -> BinanceSpotTestnetConfig:
    values: dict[str, object] = {
        "api_key": API_KEY,
        "api_secret": SECRET,
        "account_fingerprint": "testnet-account-1",
    }
    return BinanceSpotTestnetConfig(**{**values, **overrides})  # type: ignore[arg-type]


def adapter(handler, *, resolver: Resolver | None = None, config_value=None):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    value = BinanceSpotTestnetAdapter(
        config_value or config(),
        client=client,
        timestamp_provider=lambda: 1_700_000_000_000,
        identity_resolver=resolver or Resolver(identity()),
    )
    return value, client


def submission(side: ExchangeOrderSide = ExchangeOrderSide.BUY) -> MarketOrderSubmission:
    return MarketOrderSubmission(
        COMMAND_KEY,
        CLIENT_ID,
        "testnet-account-1",
        KEY,
        side,
        D("0.10000"),
    )


def ack() -> dict[str, object]:
    return {
        "symbol": "BTCUSDT",
        "orderId": 123,
        "orderListId": -1,
        "clientOrderId": CLIENT_ID,
        "transactTime": 1_700_000_000_001,
    }


def order_payload(status: str = "NEW", *, side: str = "BUY") -> dict[str, object]:
    executed = "0.00000"
    quote = "0.00000"
    if status == "PARTIALLY_FILLED":
        executed, quote = "0.04000", "4000.00"
    elif status == "FILLED":
        executed, quote = "0.10000", "10000.00"
    return {
        "symbol": "BTCUSDT",
        "orderId": 123,
        "clientOrderId": CLIENT_ID,
        "price": "0.00000",
        "origQty": "0.10000",
        "executedQty": executed,
        "cummulativeQuoteQty": quote,
        "status": status,
        "type": "MARKET",
        "side": side,
        "updateTime": 1_700_000_000_002,
    }


def test_official_hmac_fixture_and_parameter_encoding_are_exact() -> None:
    payload = (
        "symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1"
        "&recvWindow=5000&timestamp=1499827319559"
    )
    secret = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
    assert sign_hmac_sha256(payload, secret) == (
        "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71"
    )
    assert (
        encode_parameters((("quantity", D("0.10000")), ("timestamp", 1), ("flag", False)))
        == "quantity=0.10000&timestamp=1&flag=false"
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"base_url": "https://api.binance.com"},
        {"base_url": "http://testnet.binance.vision"},
        {"base_url": "https://demo-api.binance.com"},
        {"api_key": ""},
        {"api_secret": ""},
        {"recv_window_ms": 0},
        {"recv_window_ms": 60001},
        {"request_timeout_seconds": D("NaN")},
    ],
)
def test_config_rejects_unsafe_values(overrides: dict[str, object]) -> None:
    with pytest.raises(BinanceConfigurationError):
        config(**overrides)


def test_config_and_errors_never_render_secrets(caplog: pytest.LogCaptureFixture) -> None:
    value = config()
    rendered = repr(value)
    assert API_KEY not in rendered and SECRET not in rendered
    error = BinanceAuthenticationError(code=-1022, status_code=400)
    assert API_KEY not in str(error) and SECRET not in str(error)
    assert not caplog.records


@pytest.mark.parametrize("side", [ExchangeOrderSide.BUY, ExchangeOrderSide.SELL])
def test_submit_market_ack_has_exact_shape_and_never_retries(side: ExchangeOrderSide) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=ack())

    value, client = adapter(handler)
    try:
        result = value.submit_market_order(submission(side))
    finally:
        value.close()
        client.close()
    assert result.transport_outcome is TransportOutcomeKind.RESPONSE_RECEIVED
    assert result.exchange_status is ExchangeOrderStatus.ACKNOWLEDGED
    assert result.exchange_order_id == "123"
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url.host == "testnet.binance.vision"
    assert request.url.path == "/api/v3/order"
    assert request.headers["X-MBX-APIKEY"] == API_KEY
    body = parse_qs(request.content.decode(), keep_blank_values=True)
    unsigned, signature = request.content.decode().rsplit("&signature=", 1)
    assert unsigned == (
        f"symbol=BTCUSDT&side={side.value}&type=MARKET&quantity=0.10000&"
        f"newClientOrderId={CLIENT_ID}&newOrderRespType=ACK&"
        "timestamp=1700000000000&recvWindow=5000"
    )
    if side is ExchangeOrderSide.BUY:
        assert signature == "37c95ac12607584b625005266aafec9ed81932c07a8fb95e6b4d70f0f0a91140"
    assert body["symbol"] == ["BTCUSDT"]
    assert body["side"] == [side.value]
    assert body["type"] == ["MARKET"]
    assert body["quantity"] == ["0.10000"]
    assert body["newClientOrderId"] == [CLIENT_ID]
    assert body["newOrderRespType"] == ["ACK"]
    assert body["timestamp"] == ["1700000000000"]
    assert body["recvWindow"] == ["5000"]
    assert len(body["signature"][0]) == 64
    assert "quoteOrderQty" not in body
    assert SECRET not in request.content.decode()


@pytest.mark.parametrize(
    ("failure", "category"),
    [
        (httpx.ReadTimeout("timeout"), TransportFailureCategory.TIMEOUT),
        (httpx.ConnectError("reset"), TransportFailureCategory.UNKNOWN_TRANSMISSION),
        (
            httpx.Response(500, json={"code": -1000, "msg": "internal"}),
            TransportFailureCategory.HTTP_5XX,
        ),
        (
            httpx.Response(502, json={"code": -1000, "msg": "internal"}),
            TransportFailureCategory.HTTP_5XX,
        ),
        (
            httpx.Response(503, json={"code": -1000, "msg": "internal"}),
            TransportFailureCategory.HTTP_5XX,
        ),
        (
            httpx.Response(504, json={"code": -1007, "msg": "status unknown"}),
            TransportFailureCategory.HTTP_5XX,
        ),
    ],
)
def test_submit_ambiguous_failures_are_never_retried(failure, category) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if isinstance(failure, Exception):
            failure.request = request  # type: ignore[attr-defined]
            raise failure
        return failure

    value, client = adapter(handler)
    try:
        result = value.submit_market_order(submission())
    finally:
        client.close()
    assert calls == 1
    assert result.transport_outcome is TransportOutcomeKind.MAY_HAVE_BEEN_TRANSMITTED
    assert result.failure_category is category


def test_submit_minus_1007_and_malformed_ack_are_ambiguous() -> None:
    responses = iter(
        [
            httpx.Response(400, json={"code": -1007, "msg": "unknown"}),
            httpx.Response(200, content=b"not-json"),
        ]
    )
    value, client = adapter(lambda request: next(responses))
    try:
        timeout = value.submit_market_order(submission())
        malformed = value.submit_market_order(submission())
    finally:
        client.close()
    assert timeout.failure_category is TransportFailureCategory.TIMEOUT
    assert malformed.failure_category is TransportFailureCategory.RESPONSE_LOST


@pytest.mark.parametrize(
    "failure_type",
    [
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.ReadTimeout,
        httpx.WriteTimeout,
        httpx.ReadError,
        httpx.WriteError,
        httpx.RemoteProtocolError,
    ],
)
def test_submit_httpx_transport_taxonomy_is_conservatively_ambiguous(failure_type) -> None:
    calls = 0

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        raise failure_type("failure", request=request)

    value, client = adapter(handler)
    try:
        result = value.submit_market_order(submission())
    finally:
        client.close()
    assert calls == 1
    assert result.transport_outcome is TransportOutcomeKind.MAY_HAVE_BEEN_TRANSMITTED


@pytest.mark.parametrize("code", [-1013, -1121, -2010])
def test_submit_explicit_rejections_are_typed(code: int) -> None:
    value, client = adapter(lambda request: httpx.Response(400, json={"code": code, "msg": "safe"}))
    try:
        with pytest.raises(BinanceOrderRejectedError) as captured:
            value.submit_market_order(submission())
    finally:
        client.close()
    assert captured.value.code == code
    assert captured.value.safe_message == "safe"


@pytest.mark.parametrize("code", [-1022, -2014, -2015, -1021])
def test_auth_signature_and_timestamp_errors_fail_closed(code: int) -> None:
    value, client = adapter(lambda request: httpx.Response(400, json={"code": code, "msg": SECRET}))
    try:
        with pytest.raises(BinanceAuthenticationError) as captured:
            value.submit_market_order(submission())
    finally:
        client.close()
    assert SECRET not in str(captured.value)


@pytest.mark.parametrize("status", [418, 429])
def test_rate_limit_is_typed_and_does_not_sleep(status: int) -> None:
    value, client = adapter(
        lambda request: httpx.Response(
            status, headers={"Retry-After": "17"}, json={"code": -1003, "msg": "limit"}
        )
    )
    try:
        with pytest.raises(BinanceRateLimitError) as captured:
            value.submit_market_order(submission())
    finally:
        client.close()
    assert captured.value.status_code == status
    assert captured.value.retry_after_seconds == 17


def test_rate_limit_status_is_typed_even_with_malformed_body() -> None:
    value, client = adapter(
        lambda request: httpx.Response(429, headers={"Retry-After": "bad"}, content=b"bad")
    )
    try:
        with pytest.raises(BinanceRateLimitError) as captured:
            value.submit_market_order(submission())
    finally:
        client.close()
    assert captured.value.retry_after_seconds is None


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("NEW", ExchangeOrderStatus.ACKNOWLEDGED),
        ("PARTIALLY_FILLED", ExchangeOrderStatus.PARTIALLY_FILLED),
        ("FILLED", ExchangeOrderStatus.FILLED),
        ("CANCELED", ExchangeOrderStatus.CANCELED),
        ("REJECTED", ExchangeOrderStatus.REJECTED),
        ("EXPIRED", ExchangeOrderStatus.EXPIRED),
        ("EXPIRED_IN_MATCH", ExchangeOrderStatus.EXPIRED),
    ],
)
def test_query_order_normalizes_authoritative_states(status: str, expected) -> None:
    value, client = adapter(lambda request: httpx.Response(200, json=order_payload(status)))
    try:
        result = value.query_order(stream_key=KEY, client_order_id=CLIENT_ID)
    finally:
        client.close()
    assert result.kind is QueryResultKind.FOUND
    assert result.order is not None
    assert result.order.status is expected
    assert result.order.durable_command_key == COMMAND_KEY


def test_query_not_found_is_only_not_found_without_resubmit_claim() -> None:
    value, client = adapter(
        lambda request: httpx.Response(400, json={"code": -2013, "msg": "Order does not exist"})
    )
    try:
        result = value.query_order(stream_key=KEY, client_order_id=CLIENT_ID)
    finally:
        client.close()
    assert result == type(result)(QueryResultKind.NOT_FOUND)
    assert not hasattr(result, "safe_to_resubmit")


def test_unknown_order_status_and_missing_identity_fail_closed() -> None:
    value, client = adapter(lambda request: httpx.Response(200, json=order_payload("FUTURE")))
    try:
        with pytest.raises(BinanceUnsupportedContractError):
            value.query_order(stream_key=KEY, client_order_id=CLIENT_ID)
    finally:
        client.close()


def test_ack_symbol_or_client_mismatch_is_ambiguous_not_rejected() -> None:
    payloads = iter([{**ack(), "symbol": "ETHUSDT"}, {**ack(), "clientOrderId": "wrong"}])
    calls = 0

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=next(payloads))

    value, client = adapter(handler)
    try:
        results = (value.submit_market_order(submission()), value.submit_market_order(submission()))
    finally:
        client.close()
    assert calls == 2
    assert all(item.failure_category is TransportFailureCategory.RESPONSE_LOST for item in results)
    value, client = adapter(
        lambda request: httpx.Response(200, json=order_payload()), resolver=Resolver()
    )
    try:
        with pytest.raises(BinanceUnsupportedContractError):
            value.query_order(stream_key=KEY, client_order_id=CLIENT_ID)
    finally:
        client.close()


def trade_payload(*, buyer: bool, commission_asset: str, trade_id: int) -> dict[str, object]:
    return {
        "symbol": "BTCUSDT",
        "id": trade_id,
        "orderId": 123,
        "price": "100000.00",
        "qty": "0.04000",
        "quoteQty": "4000.00000",
        "commission": "0.00004",
        "commissionAsset": commission_asset,
        "time": 1_700_000_000_003,
        "isBuyer": buyer,
        "isMaker": False,
    }


@pytest.mark.parametrize(
    ("side", "buyer"), [(ExchangeOrderSide.BUY, True), (ExchangeOrderSide.SELL, False)]
)
def test_my_trades_uses_is_buyer_and_preserves_all_commission_assets(side, buyer) -> None:
    payload = [
        trade_payload(buyer=buyer, commission_asset="USDT", trade_id=1),
        trade_payload(buyer=buyer, commission_asset="BTC", trade_id=2),
        trade_payload(buyer=buyer, commission_asset="BNB", trade_id=3),
    ]
    value, client = adapter(
        lambda request: httpx.Response(200, json=payload), resolver=Resolver(identity(side))
    )
    try:
        trades = value.list_order_trades(stream_key=KEY, exchange_order_id="123")
    finally:
        client.close()
    assert [trade.side for trade in trades] == [side] * 3
    assert [trade.commission_asset for trade in trades] == ["USDT", "BTC", "BNB"]
    assert [trade.identity.exchange_trade_id for trade in trades] == ["1", "2", "3"]


def test_my_trades_rejects_cross_order_fact_and_preserves_large_ids() -> None:
    huge = 9_007_199_254_740_993
    payload = trade_payload(buyer=True, commission_asset="BNB", trade_id=huge)
    payload["orderId"] = huge
    value, client = adapter(
        lambda request: httpx.Response(200, json=[payload]),
        resolver=Resolver(identity(), exchange_order_id=str(huge)),
    )
    try:
        trades = value.list_order_trades(stream_key=KEY, exchange_order_id=str(huge))
    finally:
        client.close()
    assert trades[0].identity.exchange_order_id == str(huge)
    assert trades[0].identity.exchange_trade_id == str(huge)
    payload["orderId"] = huge + 1
    value, client = adapter(
        lambda request: httpx.Response(200, json=[payload]),
        resolver=Resolver(identity(), exchange_order_id=str(huge)),
    )
    try:
        with pytest.raises(BinanceNormalizationError, match="order ID mismatch"):
            value.list_order_trades(stream_key=KEY, exchange_order_id=str(huge))
    finally:
        client.close()


def test_balances_are_filtered_ordered_and_decimal_only() -> None:
    payload = {
        "balances": [
            {"asset": "USDT", "free": "1000.00", "locked": "2.00"},
            {"asset": "BTC", "free": "0.10", "locked": "0.01"},
            {"asset": "BNB", "free": "3", "locked": "0"},
        ]
    }
    value, client = adapter(lambda request: httpx.Response(200, json=payload))
    try:
        balances = value.read_balances(("BTC", "USDT"))
    finally:
        client.close()
    assert [item.asset for item in balances] == ["BTC", "USDT"]
    assert balances[0].free == D("0.10")
    assert isinstance(balances[1].locked, Decimal)


def test_open_orders_normalize_without_cancel_behavior() -> None:
    value, client = adapter(lambda request: httpx.Response(200, json=[order_payload()]))
    try:
        orders = value.list_open_orders(stream_key=KEY)
    finally:
        client.close()
    assert len(orders) == 1
    assert orders[0].status is ExchangeOrderStatus.ACKNOWLEDGED


def test_exchange_info_normalizes_market_filters_and_version() -> None:
    payload = {
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "filters": [
                    {"filterType": "LOT_SIZE", "minQty": "0.00001", "stepSize": "0.00001"},
                    {
                        "filterType": "MARKET_LOT_SIZE",
                        "minQty": "0.00010",
                        "stepSize": "0.00010",
                    },
                    {"filterType": "MIN_NOTIONAL", "minNotional": "5.00", "applyToMarket": True},
                ],
            }
        ]
    }
    value, client = adapter(lambda request: httpx.Response(200, json=payload))
    try:
        rules = value.read_instrument_rules(KEY)
    finally:
        client.close()
    assert rules.quantity_step == D("0.00010")
    assert rules.min_quantity == D("0.00010")
    assert rules.min_notional == D("5.00")
    assert len(rules.rules_version) == 64


def test_read_timeout_is_typed_and_has_no_retry() -> None:
    calls = 0

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timeout", request=request)

    value, client = adapter(handler)
    try:
        with pytest.raises(BinanceTransportError):
            value.read_balances(("BTC",))
    finally:
        client.close()
    assert calls == 1


def test_read_transport_error_drops_credential_bearing_cause() -> None:
    def handler(request: httpx.Request):
        raise httpx.ReadError("request failed", request=request)

    value, client = adapter(handler)
    try:
        with pytest.raises(BinanceTransportError) as captured:
            value.read_balances(("BTC",))
    finally:
        client.close()
    assert captured.value.__cause__ is None
    assert API_KEY not in str(captured.value) and SECRET not in str(captured.value)


def test_private_redirect_is_not_followed_or_forwarded() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://evil.example/steal"})

    value, client = adapter(handler)
    try:
        result = value.submit_market_order(submission())
    finally:
        client.close()
    assert len(requests) == 1
    assert requests[0].url.host == "testnet.binance.vision"
    assert result.failure_category is TransportFailureCategory.UNKNOWN_TRANSMISSION


def test_ambiguous_submit_does_not_disable_read_reconciliation() -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request):
        requests.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(503, json={"code": -1000, "msg": "unknown"})
        return httpx.Response(200, json=order_payload())

    value, client = adapter(handler)
    try:
        submit_result = value.submit_market_order(submission())
        query_result = value.query_order(stream_key=KEY, client_order_id=CLIENT_ID)
    finally:
        client.close()
    assert submit_result.transport_outcome is TransportOutcomeKind.MAY_HAVE_BEEN_TRANSMITTED
    assert query_result.kind is QueryResultKind.FOUND
    assert requests == [("POST", "/api/v3/order"), ("GET", "/api/v3/order")]


def test_read_operations_use_only_expected_get_endpoints() -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request):
        requests.append((request.method, request.url.path))
        payloads: dict[str, object] = {
            "/api/v3/order": order_payload(),
            "/api/v3/myTrades": [],
            "/api/v3/openOrders": [],
            "/api/v3/account": {"balances": [{"asset": "BTC", "free": "0", "locked": "0"}]},
            "/api/v3/exchangeInfo": {
                "symbols": [
                    {
                        "symbol": "BTCUSDT",
                        "filters": [
                            {
                                "filterType": "MARKET_LOT_SIZE",
                                "minQty": "0.0001",
                                "stepSize": "0.0001",
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
        }
        return httpx.Response(200, json=payloads[request.url.path])

    value, client = adapter(handler)
    try:
        value.query_order(stream_key=KEY, client_order_id=CLIENT_ID)
        value.list_order_trades(stream_key=KEY, exchange_order_id="123")
        value.list_open_orders(stream_key=KEY)
        value.read_balances(("BTC",))
        value.read_instrument_rules(KEY)
    finally:
        client.close()
    assert requests == [
        ("GET", "/api/v3/order"),
        ("GET", "/api/v3/myTrades"),
        ("GET", "/api/v3/openOrders"),
        ("GET", "/api/v3/account"),
        ("GET", "/api/v3/exchangeInfo"),
    ]


def test_adapter_close_is_idempotent_and_does_not_close_injected_client() -> None:
    value, client = adapter(lambda request: httpx.Response(200, json=ack()))
    value.close()
    value.close()
    assert not client.is_closed
    with pytest.raises(BinanceConfigurationError, match="closed"):
        value.submit_market_order(submission())
    client.close()


@pytest.mark.parametrize(
    "payload",
    [
        {"balances": [{"asset": "BTC", "free": 0.1, "locked": "0"}]},
        {"balances": [{"asset": "BTC", "free": "NaN", "locked": "0"}]},
        {"balances": [{"asset": "BTC", "free": "0.1", "locked": "-1"}]},
    ],
)
def test_financial_float_nonfinite_and_negative_payloads_are_rejected(payload) -> None:
    value, client = adapter(lambda request: httpx.Response(200, json=payload))
    try:
        with pytest.raises(BinanceNormalizationError):
            value.read_balances(("BTC",))
    finally:
        client.close()


@pytest.mark.parametrize("zero", ["-0", "-0.00000000"])
def test_negative_zero_balances_normalize_unsigned(zero: str) -> None:
    value, client = adapter(
        lambda request: httpx.Response(
            200, json={"balances": [{"asset": "BTC", "free": zero, "locked": zero}]}
        )
    )
    try:
        balance = value.read_balances(("BTC",))[0]
    finally:
        client.close()
    assert not balance.free.is_signed()
    assert not balance.locked.is_signed()


def test_signing_validation_and_models_are_immutable() -> None:
    with pytest.raises(BinanceConfigurationError):
        sign_hmac_sha256("payload", "")
    with pytest.raises(BinanceConfigurationError):
        encode_parameters((("value", D("Infinity")),))
    with pytest.raises(FrozenInstanceError):
        copy.copy(config()).api_key = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("side", "BUY"),
        ("original_base_quantity", D(0)),
        ("original_base_quantity", D("NaN")),
        ("created_at_ms", -1),
    ],
)
def test_order_identity_rejects_corruption(field: str, value: object) -> None:
    from dataclasses import replace

    with pytest.raises((BinanceConfigurationError, ValueError)):
        replace(identity(), **{field: value})


def test_adapter_constructor_timestamp_context_and_account_validation() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    try:
        for arguments in (
            {
                "config": object(),
                "client": client,
                "timestamp_provider": lambda: 1,
                "identity_resolver": Resolver(),
            },
            {
                "config": config(),
                "client": object(),
                "timestamp_provider": lambda: 1,
                "identity_resolver": Resolver(),
            },
            {
                "config": config(),
                "client": client,
                "timestamp_provider": 1,
                "identity_resolver": Resolver(),
            },
            {
                "config": config(),
                "client": client,
                "timestamp_provider": lambda: 1,
                "identity_resolver": object(),
            },
        ):
            with pytest.raises(BinanceConfigurationError):
                BinanceSpotTestnetAdapter(**arguments)  # type: ignore[arg-type]
        value = BinanceSpotTestnetAdapter(
            config(), client=client, timestamp_provider=lambda: -1, identity_resolver=Resolver()
        )
        with pytest.raises(BinanceConfigurationError, match="timestamp"):
            value.submit_market_order(submission())
        with value:
            pass
        assert not client.is_closed
    finally:
        client.close()


def test_timestamp_and_identity_resolver_fail_before_network_and_preserve_cause() -> None:
    calls = 0

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=order_payload())

    class BrokenResolver(Resolver):
        def by_client_order_id(self, client_order_id: str):
            raise LookupError("resolver detail")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        value = BinanceSpotTestnetAdapter(
            config(),
            client=client,
            timestamp_provider=lambda: 1,
            identity_resolver=BrokenResolver(),
        )
        with pytest.raises(BinanceConfigurationError) as captured:
            value.query_order(stream_key=KEY, client_order_id=CLIENT_ID)
        assert isinstance(captured.value.__cause__, LookupError)
        failing_time = BinanceSpotTestnetAdapter(
            config(),
            client=client,
            timestamp_provider=lambda: (_ for _ in ()).throw(RuntimeError("clock")),
            identity_resolver=Resolver(identity()),
        )
        with pytest.raises(BinanceConfigurationError) as captured_time:
            failing_time.submit_market_order(submission())
        assert isinstance(captured_time.value.__cause__, RuntimeError)
    finally:
        client.close()
    assert calls == 0


def test_submit_input_account_and_unclassified_http_failure_fail_closed() -> None:
    value, client = adapter(lambda request: httpx.Response(400, content=b"bad request"))
    try:
        with pytest.raises(BinanceConfigurationError):
            value.submit_market_order(object())  # type: ignore[arg-type]
        mismatched = copy.copy(submission())
        object.__setattr__(mismatched, "account_fingerprint", "other")
        with pytest.raises(BinanceConfigurationError, match="lineage"):
            value.submit_market_order(mismatched)
        result = value.submit_market_order(submission())
        assert result.failure_category is TransportFailureCategory.UNKNOWN_TRANSMISSION
    finally:
        client.close()


@pytest.mark.parametrize(
    "mutation",
    [
        {"clientOrderId": "ctt1t-zzzzzzzzzzzzzzzzzzzzzzzzzz"},
        {"symbol": "ETHUSDT"},
        {"side": "SELL"},
        {"origQty": "0.2"},
        {"side": "FUTURE"},
    ],
)
def test_order_identity_payload_mismatches_fail_closed(mutation: dict[str, object]) -> None:
    payload = {**order_payload(), **mutation}
    value, client = adapter(lambda request: httpx.Response(200, json=payload))
    try:
        with pytest.raises((BinanceNormalizationError, BinanceUnsupportedContractError)):
            value.query_order(stream_key=KEY, client_order_id=CLIENT_ID)
    finally:
        client.close()


def test_trade_validation_and_missing_identity_fail_closed() -> None:
    value, client = adapter(lambda request: httpx.Response(200, json=[]), resolver=Resolver())
    try:
        with pytest.raises(BinanceUnsupportedContractError):
            value.list_order_trades(stream_key=KEY, exchange_order_id="123")
        with pytest.raises(BinanceConfigurationError):
            value.list_order_trades(stream_key=KEY, exchange_order_id="")
    finally:
        client.close()
    for mutation in ({"symbol": "ETHUSDT"}, {"isBuyer": False}, {"isBuyer": "true"}):
        payload = [{**trade_payload(buyer=True, commission_asset="BNB", trade_id=1), **mutation}]
        value, client = adapter(lambda request, p=payload: httpx.Response(200, json=p))
        try:
            with pytest.raises(BinanceNormalizationError):
                value.list_order_trades(stream_key=KEY, exchange_order_id="123")
        finally:
            client.close()


def test_open_order_and_balance_shape_fail_closed() -> None:
    for payload, operation in (
        ([1], lambda value: value.list_open_orders(stream_key=KEY)),
        ([order_payload()], lambda value: value.list_open_orders(stream_key=KEY)),
        ({"balances": "bad"}, lambda value: value.read_balances(("BTC",))),
        ({"balances": [1]}, lambda value: value.read_balances(("BTC",))),
        ({"balances": []}, lambda value: value.read_balances(("BTC",))),
    ):
        resolver = Resolver() if payload == [order_payload()] else Resolver(identity())
        value, client = adapter(
            lambda request, p=payload: httpx.Response(200, json=p), resolver=resolver
        )
        try:
            with pytest.raises((BinanceNormalizationError, BinanceUnsupportedContractError)):
                operation(value)
        finally:
            client.close()
    value, client = adapter(lambda request: httpx.Response(200, json={"balances": []}))
    try:
        with pytest.raises(BinanceConfigurationError):
            value.read_balances(["BTC"])  # type: ignore[arg-type]
        with pytest.raises(BinanceConfigurationError):
            value.read_balances(("BTC", "BTC"))
    finally:
        client.close()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"symbols": []},
        {"symbols": [{"symbol": "ETHUSDT", "filters": []}]},
        {"symbols": [{"symbol": "BTCUSDT", "filters": "bad"}]},
        {"symbols": [{"symbol": "BTCUSDT", "filters": []}]},
        {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "filters": [{"filterType": "LOT_SIZE", "minQty": "0.1", "stepSize": "0.1"}],
                }
            ]
        },
        {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "filters": [
                        {"filterType": "LOT_SIZE", "minQty": "0.1", "stepSize": "0.1"},
                        {
                            "filterType": "MIN_NOTIONAL",
                            "minNotional": "5",
                            "applyToMarket": False,
                        },
                    ],
                }
            ]
        },
    ],
)
def test_instrument_rule_contract_gaps_fail_closed(payload: object) -> None:
    value, client = adapter(lambda request: httpx.Response(200, json=payload))
    try:
        with pytest.raises((BinanceNormalizationError, BinanceUnsupportedContractError)):
            value.read_instrument_rules(KEY)
    finally:
        client.close()


def test_unknown_and_richer_market_filters_fail_closed_and_disabled_market_lot_falls_back() -> None:
    lot = {"filterType": "LOT_SIZE", "minQty": "0.00001", "stepSize": "0.00001"}
    minimum = {"filterType": "MIN_NOTIONAL", "minNotional": "5", "applyToMarket": True}
    filter_sets = (
        [lot, minimum, {"filterType": "FUTURE_CRITICAL_FILTER", "value": "1"}],
        [
            lot,
            {
                "filterType": "NOTIONAL",
                "minNotional": "5",
                "maxNotional": "1000",
                "applyMinToMarket": True,
                "applyMaxToMarket": True,
            },
        ],
    )
    for filters in filter_sets:
        payload = {"symbols": [{"symbol": "BTCUSDT", "filters": filters}]}
        value, client = adapter(lambda request, p=payload: httpx.Response(200, json=p))
        try:
            with pytest.raises(BinanceUnsupportedContractError):
                value.read_instrument_rules(KEY)
        finally:
            client.close()
    payload = {
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "filters": [
                    lot,
                    minimum,
                    {"filterType": "MARKET_LOT_SIZE", "minQty": "0", "stepSize": "0"},
                ],
            }
        ]
    }
    value, client = adapter(lambda request: httpx.Response(200, json=payload))
    try:
        rules = value.read_instrument_rules(KEY)
    finally:
        client.close()
    assert rules.quantity_step == D("0.00001")


def test_response_helper_validation_and_safe_retry_after() -> None:
    response = httpx.Response(200, content=b"not-json")
    with pytest.raises(BinanceNormalizationError):
        BinanceSpotTestnetAdapter._json_object(response)
    with pytest.raises(BinanceNormalizationError):
        BinanceSpotTestnetAdapter._json_list(response)
    with pytest.raises(BinanceNormalizationError):
        BinanceSpotTestnetAdapter._json_object(httpx.Response(200, json=[]))
    with pytest.raises(BinanceNormalizationError):
        BinanceSpotTestnetAdapter._json_list(httpx.Response(200, json={}))
    with pytest.raises(BinanceNormalizationError):
        BinanceSpotTestnetAdapter._error_payload(
            httpx.Response(400, json={"code": "bad", "msg": 1})
        )
    assert BinanceSpotTestnetAdapter._retry_after(None) is None
    assert BinanceSpotTestnetAdapter._retry_after("bad") is None
    assert BinanceSpotTestnetAdapter._retry_after("-1") is None


def test_signing_rejects_invalid_parameter_types_and_names() -> None:
    with pytest.raises(BinanceConfigurationError):
        encode_parameters((("", "value"),))
    with pytest.raises(BinanceConfigurationError):
        encode_parameters((("value", object()),))  # type: ignore[arg-type]
    with pytest.raises(BinanceConfigurationError):
        sign_hmac_sha256(1, "secret")  # type: ignore[arg-type]
    with pytest.raises(BinanceConfigurationError, match="duplicate"):
        encode_parameters((("symbol", "BTCUSDT"), ("symbol", "ETHUSDT")))


def test_internal_request_boundary_rejects_reserved_or_missing_owned_fields() -> None:
    value, client = adapter(lambda request: httpx.Response(200, json={}))
    try:
        with pytest.raises(BinanceConfigurationError, match="signature"):
            value._send("GET", "/api/v3/order", [("signature", "caller")], signed=True)
        with pytest.raises(BinanceConfigurationError, match="timing"):
            value._send("GET", "/api/v3/order", [("timestamp", 1)], signed=True)
    finally:
        client.close()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(429, headers={"Retry-After": "1"}),
        httpx.Response(400, json={"code": -1022, "msg": "bad signature"}),
        httpx.Response(400, content=b"malformed"),
    ],
)
def test_read_error_matrix_is_typed(response: httpx.Response) -> None:
    value, client = adapter(lambda request: response)
    try:
        with pytest.raises(
            (BinanceRateLimitError, BinanceAuthenticationError, BinanceResponseError)
        ):
            value.read_balances(("BTC",))
    finally:
        client.close()


def test_trade_resolver_failure_is_typed_before_http() -> None:
    calls = 0

    class BrokenResolver(Resolver):
        def by_exchange_order_id(self, exchange_order_id: str):
            raise LookupError("resolver detail")

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=[])

    value, client = adapter(handler, resolver=BrokenResolver())
    try:
        with pytest.raises(BinanceConfigurationError) as captured:
            value.list_order_trades(stream_key=KEY, exchange_order_id="123")
    finally:
        client.close()
    assert isinstance(captured.value.__cause__, LookupError)
    assert calls == 0


@pytest.mark.parametrize(
    "filters",
    [
        [{"filterType": "MARKET_LOT_SIZE", "minQty": "0.1", "stepSize": "0.1"}],
        [
            {"filterType": "MARKET_LOT_SIZE", "minQty": "0.1", "stepSize": "0.1"},
            {
                "filterType": "MIN_NOTIONAL",
                "minNotional": "5",
                "applyToMarket": False,
            },
        ],
        [
            {"filterType": "MARKET_LOT_SIZE", "minQty": "0.1", "stepSize": "0.1"},
            {
                "filterType": "NOTIONAL",
                "minNotional": "5",
                "maxNotional": "100",
                "applyMinToMarket": True,
                "applyMaxToMarket": True,
            },
        ],
    ],
)
def test_unrepresentable_market_filter_semantics_fail_closed(filters: list[dict]) -> None:
    payload = {"symbols": [{"symbol": "BTCUSDT", "filters": filters}]}
    value, client = adapter(lambda request: httpx.Response(200, json=payload))
    try:
        with pytest.raises(BinanceUnsupportedContractError):
            value.read_instrument_rules(KEY)
    finally:
        client.close()


def test_malformed_trade_and_primitive_fields_fail_closed() -> None:
    value, client = adapter(lambda request: httpx.Response(200, json=[1]))
    try:
        with pytest.raises(BinanceNormalizationError, match="trade entry"):
            value.list_order_trades(stream_key=KEY, exchange_order_id="123")
    finally:
        client.close()
    for mutation in (
        {"clientOrderId": ""},
        {"orderId": "123"},
        {"origQty": "not-a-decimal"},
    ):
        payload = {**order_payload(), **mutation}
        value, client = adapter(lambda request, p=payload: httpx.Response(200, json=p))
        try:
            with pytest.raises(BinanceNormalizationError):
                value.query_order(stream_key=KEY, client_order_id=CLIENT_ID)
        finally:
            client.close()
