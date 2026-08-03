from typing import Protocol

from app.market.models import Candle, ServerTime, TickerPrice


class MarketProvider(Protocol):
    def get_server_time(self) -> ServerTime:
        """Return server time."""

    def get_ticker_price(
        self,
        symbol: str,
    ) -> TickerPrice:
        """Return current ticker price."""

    def get_candles(
        self,
        symbol: str,
        interval: str,
        limit: int,
    ) -> list[Candle]:
        """Return historical candles."""

    def close(self) -> None:
        """Release resources."""