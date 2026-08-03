from collections.abc import Callable

import httpx
import pytest

from app.market.binance_provider import BinanceMarketProvider
from app.market.models import Candle, ServerTime, TickerPrice
from app.market.provider import MarketProvider
from tests.fakes import FakeMarketProvider


def make_binance_provider() -> BinanceMarketProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/time":
            payload: object = {"serverTime": 1_700_000_000_000}
        elif request.url.path == "/api/v3/ticker/price":
            payload = {"symbol": "BTCUSDT", "price": "50000.00"}
        else:
            payload = [
                [
                    1_700_000_000_000,
                    "50000.00",
                    "50100.00",
                    "49900.00",
                    "50050.00",
                    "10.00",
                    1_700_000_059_999,
                ]
            ]

        return httpx.Response(200, json=payload, request=request)

    provider = BinanceMarketProvider(base_url="https://test.binance.local")
    provider._client.close()
    provider._client = httpx.Client(
        base_url="https://test.binance.local",
        transport=httpx.MockTransport(handler),
    )
    return provider


@pytest.fixture(params=[FakeMarketProvider, make_binance_provider])
def market_provider(request: pytest.FixtureRequest) -> MarketProvider:
    factory: Callable[[], MarketProvider] = request.param
    provider = factory()

    try:
        yield provider
    finally:
        provider.close()


def test_market_provider_server_time_contract(market_provider: MarketProvider) -> None:
    assert isinstance(market_provider.get_server_time(), ServerTime)


def test_market_provider_ticker_contract(market_provider: MarketProvider) -> None:
    ticker = market_provider.get_ticker_price("BTCUSDT")

    assert isinstance(ticker, TickerPrice)
    assert ticker.symbol == "BTCUSDT"


def test_market_provider_candles_contract(market_provider: MarketProvider) -> None:
    candles = market_provider.get_candles("BTCUSDT", "1m", 1)

    assert len(candles) == 1
    assert all(isinstance(candle, Candle) for candle in candles)
