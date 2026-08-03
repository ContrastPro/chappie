from decimal import Decimal
from typing import Any

import httpx
import pytest

from app.market.binance_provider import BinanceMarketProvider
from app.market.exceptions import MarketProviderError, MarketProviderHTTPError
from app.market.models import Candle


def make_provider(payload: Any, status_code: int = 200) -> BinanceMarketProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload, request=request)

    provider = BinanceMarketProvider(base_url="https://test.binance.local")
    provider._client.close()
    provider._client = httpx.Client(
        base_url="https://test.binance.local",
        transport=httpx.MockTransport(handler),
    )
    return provider


def valid_kline() -> list[Any]:
    return [
        1_700_000_000_000,
        "50000.10",
        "50100.20",
        "49900.30",
        "50050.40",
        "12.50",
        1_700_000_059_999,
        "625000.00",
        42,
        "6.00",
        "300000.00",
        "0",
    ]


def test_get_candles_converts_binance_payload() -> None:
    provider = make_provider([valid_kline()])

    try:
        candles = provider.get_candles(" btcusdt ", "1m", 1)
    finally:
        provider.close()

    assert candles == [
        Candle(
            open_time_ms=1_700_000_000_000,
            close_time_ms=1_700_000_059_999,
            open=Decimal("50000.10"),
            high=Decimal("50100.20"),
            low=Decimal("49900.30"),
            close=Decimal("50050.40"),
            volume=Decimal("12.50"),
        )
    ]


@pytest.mark.parametrize("symbol", ["", "   ", "BTC/USDT", "BTC_USDT"])
def test_get_candles_rejects_invalid_symbol_value(symbol: Any) -> None:
    provider = make_provider([])

    try:
        with pytest.raises(ValueError, match="Symbol"):
            provider.get_candles(symbol, "1m", 1)
    finally:
        provider.close()


def test_get_candles_rejects_non_string_symbol() -> None:
    provider = make_provider([])

    try:
        with pytest.raises(TypeError, match="Symbol"):
            provider.get_candles(None, "1m", 1)  # type: ignore[arg-type]
    finally:
        provider.close()


@pytest.mark.parametrize("interval", ["", "60s", "1y", "1mo"])
def test_get_candles_rejects_invalid_interval_value(interval: Any) -> None:
    provider = make_provider([])

    try:
        with pytest.raises(ValueError, match="Interval|Unsupported"):
            provider.get_candles("BTCUSDT", interval, 1)
    finally:
        provider.close()


def test_get_candles_rejects_non_string_interval() -> None:
    provider = make_provider([])

    try:
        with pytest.raises(TypeError, match="Interval"):
            provider.get_candles("BTCUSDT", None, 1)  # type: ignore[arg-type]
    finally:
        provider.close()


@pytest.mark.parametrize("limit", [0, 1001, -1, 1.5, True, None])
def test_get_candles_rejects_invalid_limit(limit: Any) -> None:
    provider = make_provider([])

    try:
        with pytest.raises(ValueError, match="Limit"):
            provider.get_candles("BTCUSDT", "1m", limit)
    finally:
        provider.close()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        ["not-a-candle"],
        [[1_700_000_000_000, "1", "2"]],
        [[True, "1", "2", "0.5", "1.5", "10", 1_700_000_059_999]],
        [[1_700_000_000_000, "1", "2", "0.5", "1.5", "10", False]],
    ],
)
def test_get_candles_rejects_invalid_structure(payload: Any) -> None:
    provider = make_provider(payload)

    try:
        with pytest.raises(MarketProviderError, match="response format|invalid"):
            provider.get_candles("BTCUSDT", "1m", 1)
    finally:
        provider.close()


@pytest.mark.parametrize("value", [None, 1.0, "not-a-number", "NaN", "Infinity"])
def test_get_candles_rejects_invalid_decimal_values(value: Any) -> None:
    candle = valid_kline()
    candle[1] = value
    provider = make_provider([candle])

    try:
        with pytest.raises(MarketProviderError, match="open"):
            provider.get_candles("BTCUSDT", "1m", 1)
    finally:
        provider.close()


def test_get_candles_sends_normalized_parameters() -> None:
    captured_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_request
        captured_request = request
        return httpx.Response(200, json=[], request=request)

    provider = BinanceMarketProvider(base_url="https://test.binance.local")
    provider._client.close()
    provider._client = httpx.Client(
        base_url="https://test.binance.local",
        transport=httpx.MockTransport(handler),
    )

    try:
        assert provider.get_candles(" btcusdt ", "1m", 100) == []
    finally:
        provider.close()

    assert captured_request is not None
    assert captured_request.url.path == "/api/v3/klines"
    assert dict(captured_request.url.params) == {
        "symbol": "BTCUSDT",
        "interval": "1m",
        "limit": "100",
    }


def test_existing_dict_endpoint_rejects_list_payload() -> None:
    provider = make_provider([])

    try:
        with pytest.raises(MarketProviderError, match="ticker response format"):
            provider.get_ticker_price("BTCUSDT")
    finally:
        provider.close()


def test_http_error_exposes_typed_retry_metadata() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Retry-After": "120"},
            request=request,
        )

    provider = BinanceMarketProvider(base_url="https://test.binance.local")
    provider._client.close()
    provider._client = httpx.Client(
        base_url="https://test.binance.local",
        transport=httpx.MockTransport(handler),
    )

    try:
        with pytest.raises(MarketProviderHTTPError) as error_info:
            provider.get_candles("BTCUSDT", "1m", 2)
    finally:
        provider.close()

    assert error_info.value.status_code == 429
    assert error_info.value.retry_after_seconds == 120
    assert error_info.value.retryable is True


def test_fatal_http_error_is_typed_as_not_retryable() -> None:
    provider = make_provider({}, status_code=400)

    try:
        with pytest.raises(MarketProviderHTTPError) as error_info:
            provider.get_candles("BTCUSDT", "1m", 2)
    finally:
        provider.close()

    assert error_info.value.status_code == 400
    assert error_info.value.retry_after_seconds is None
    assert error_info.value.retryable is False
