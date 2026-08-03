from typing import Protocol

from app.market.models import ServerTime, TickerPrice


class MarketProvider(Protocol):
    def get_server_time(self) -> ServerTime:
        """Возвращает серверное время источника рынка."""

    def get_ticker_price(self, symbol: str) -> TickerPrice:
        """Возвращает текущую цену торгового инструмента."""

    def close(self) -> None:
        """Освобождает ресурсы провайдера."""