from decimal import Decimal, InvalidOperation
from typing import Any, Self

import httpx

from app.market.exceptions import MarketProviderError
from app.market.models import ServerTime, TickerPrice


class BinanceMarketProvider:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout_seconds,
            headers={
                "User-Agent": "chappie-trader/0.1.0",
            },
        )

    def get_server_time(self) -> ServerTime:
        payload = self._get_json("/api/v3/time")

        server_time = payload.get("serverTime")

        if not isinstance(server_time, int):
            raise MarketProviderError(
                "Binance returned an invalid serverTime value"
            )

        return ServerTime(timestamp_ms=server_time)

    def get_ticker_price(self, symbol: str) -> TickerPrice:
        normalized_symbol = symbol.strip().upper()

        if not normalized_symbol:
            raise ValueError("Symbol must not be empty")

        payload = self._get_json(
            "/api/v3/ticker/price",
            params={"symbol": normalized_symbol},
        )

        response_symbol = payload.get("symbol")
        raw_price = payload.get("price")

        if not isinstance(response_symbol, str):
            raise MarketProviderError(
                "Binance returned an invalid symbol"
            )

        if not isinstance(raw_price, str):
            raise MarketProviderError(
                "Binance returned an invalid price"
            )

        try:
            price = Decimal(raw_price)
        except InvalidOperation as error:
            raise MarketProviderError(
                "Binance returned a non-numeric price"
            ) from error

        return TickerPrice(
            symbol=response_symbol,
            price=price,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        self.close()

    def _get_json(
        self,
        path: str,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            response = self._client.get(
                path,
                params=params,
            )
            response.raise_for_status()
        except httpx.TimeoutException as error:
            raise MarketProviderError(
                f"Binance request timed out: {path}"
            ) from error
        except httpx.HTTPStatusError as error:
            raise MarketProviderError(
                "Binance returned HTTP "
                f"{error.response.status_code}: "
                f"{error.response.text}"
            ) from error
        except httpx.RequestError as error:
            raise MarketProviderError(
                f"Could not connect to Binance: {error}"
            ) from error

        try:
            payload = response.json()
        except ValueError as error:
            raise MarketProviderError(
                "Binance returned invalid JSON"
            ) from error

        if not isinstance(payload, dict):
            raise MarketProviderError(
                "Binance returned an unexpected response format"
            )

        return payload