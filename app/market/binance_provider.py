from decimal import Decimal, InvalidOperation
from typing import Any, Self

import httpx

from app.market.exceptions import (
    MarketProviderConnectionError,
    MarketProviderHTTPError,
    MarketProviderResponseError,
    MarketProviderTimeoutError,
)
from app.market.intervals import BINANCE_SPOT_INTERVALS
from app.market.models import Candle, ServerTime, TickerPrice

MIN_CANDLE_LIMIT = 1
MAX_CANDLE_LIMIT = 1000


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

        if not isinstance(payload, dict):
            raise MarketProviderResponseError(
                "Binance returned an unexpected server time response format"
            )

        server_time = payload.get("serverTime")

        if not self._is_integer(server_time):
            raise MarketProviderResponseError(
                "Binance returned an invalid serverTime value"
            )

        return ServerTime(timestamp_ms=server_time)

    def get_ticker_price(self, symbol: str) -> TickerPrice:
        normalized_symbol = self._validate_symbol(symbol)

        payload = self._get_json(
            "/api/v3/ticker/price",
            params={"symbol": normalized_symbol},
        )

        if not isinstance(payload, dict):
            raise MarketProviderResponseError(
                "Binance returned an unexpected ticker response format"
            )

        response_symbol = payload.get("symbol")
        raw_price = payload.get("price")

        if not isinstance(response_symbol, str):
            raise MarketProviderResponseError(
                "Binance returned an invalid symbol"
            )

        return TickerPrice(
            symbol=response_symbol,
            price=self._parse_decimal(raw_price, "price"),
        )

    def get_candles(
        self,
        symbol: str,
        interval: str,
        limit: int,
    ) -> list[Candle]:
        normalized_symbol = self._validate_symbol(symbol)
        normalized_interval = self._validate_interval(interval)
        validated_limit = self._validate_limit(limit)

        payload = self._get_json(
            "/api/v3/klines",
            params={
                "symbol": normalized_symbol,
                "interval": normalized_interval,
                "limit": str(validated_limit),
            },
        )

        if not isinstance(payload, list):
            raise MarketProviderResponseError(
                "Binance returned an unexpected candles response format"
            )

        return [
            self._parse_candle(raw_candle, index)
            for index, raw_candle in enumerate(payload)
        ]

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
    ) -> Any:
        try:
            response = self._client.get(
                path,
                params=params,
            )
            response.raise_for_status()
        except httpx.TimeoutException as error:
            raise MarketProviderTimeoutError(
                f"Binance request timed out: {path}"
            ) from error
        except httpx.HTTPStatusError as error:
            raise MarketProviderHTTPError(
                status_code=error.response.status_code,
                retry_after_seconds=self._parse_retry_after(
                    error.response.headers.get("Retry-After")
                ),
            ) from error
        except httpx.RequestError as error:
            raise MarketProviderConnectionError(
                f"Could not connect to Binance: {error}"
            ) from error

        try:
            payload = response.json()
        except ValueError as error:
            raise MarketProviderResponseError(
                "Binance returned invalid JSON"
            ) from error

        return payload

    @staticmethod
    def _validate_symbol(symbol: str) -> str:
        if not isinstance(symbol, str):
            raise TypeError("Symbol must be a string")

        normalized_symbol = symbol.strip().upper()

        if not normalized_symbol:
            raise ValueError("Symbol must not be empty")

        if not normalized_symbol.isascii() or not normalized_symbol.isalnum():
            raise ValueError("Symbol must contain only ASCII letters and digits")

        return normalized_symbol

    @staticmethod
    def _validate_interval(interval: str) -> str:
        if not isinstance(interval, str):
            raise TypeError("Interval must be a string")

        normalized_interval = interval.strip()

        if normalized_interval not in BINANCE_SPOT_INTERVALS:
            raise ValueError(f"Unsupported Binance Spot interval: {interval!r}")

        return normalized_interval

    @staticmethod
    def _validate_limit(limit: int) -> int:
        if not BinanceMarketProvider._is_integer(limit):
            raise ValueError("Limit must be an integer")

        if not MIN_CANDLE_LIMIT <= limit <= MAX_CANDLE_LIMIT:
            raise ValueError(
                f"Limit must be between {MIN_CANDLE_LIMIT} and {MAX_CANDLE_LIMIT}"
            )

        return limit

    @staticmethod
    def _parse_candle(raw_candle: Any, index: int) -> Candle:
        if not isinstance(raw_candle, list) or len(raw_candle) < 7:
            raise MarketProviderResponseError(
                f"Binance returned an invalid candle at index {index}"
            )

        open_time_ms = raw_candle[0]
        close_time_ms = raw_candle[6]

        if not BinanceMarketProvider._is_integer(open_time_ms):
            raise MarketProviderResponseError(
                f"Binance returned an invalid open time for candle at index {index}"
            )

        if not BinanceMarketProvider._is_integer(close_time_ms):
            raise MarketProviderResponseError(
                f"Binance returned an invalid close time for candle at index {index}"
            )

        return Candle(
            open_time_ms=open_time_ms,
            close_time_ms=close_time_ms,
            open=BinanceMarketProvider._parse_decimal(raw_candle[1], "open", index),
            high=BinanceMarketProvider._parse_decimal(raw_candle[2], "high", index),
            low=BinanceMarketProvider._parse_decimal(raw_candle[3], "low", index),
            close=BinanceMarketProvider._parse_decimal(raw_candle[4], "close", index),
            volume=BinanceMarketProvider._parse_decimal(raw_candle[5], "volume", index),
        )

    @staticmethod
    def _parse_decimal(
        value: Any,
        field_name: str,
        candle_index: int | None = None,
    ) -> Decimal:
        location = "" if candle_index is None else f" for candle at index {candle_index}"

        if not isinstance(value, str):
            raise MarketProviderResponseError(
                f"Binance returned an invalid {field_name}{location}"
            )

        try:
            result = Decimal(value)
        except InvalidOperation as error:
            raise MarketProviderResponseError(
                f"Binance returned a non-numeric {field_name}{location}"
            ) from error

        if not result.is_finite():
            raise MarketProviderResponseError(
                f"Binance returned a non-finite {field_name}{location}"
            )

        return result

    @staticmethod
    def _is_integer(value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    @staticmethod
    def _parse_retry_after(value: str | None) -> int | None:
        if value is None:
            return None

        try:
            seconds = int(value)
        except ValueError:
            return None

        return seconds if seconds >= 0 else None
