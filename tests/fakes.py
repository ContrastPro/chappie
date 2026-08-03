from decimal import Decimal

from app.market.models import Candle, ServerTime, TickerPrice


class FakeMarketProvider:
    def get_server_time(self) -> ServerTime:
        return ServerTime(timestamp_ms=1_700_000_000_000)

    def get_ticker_price(self, symbol: str) -> TickerPrice:
        return TickerPrice(
            symbol=symbol,
            price=Decimal("50000.00"),
        )

    def get_candles(
        self,
        symbol: str,
        interval: str,
        limit: int,
    ) -> list[Candle]:
        del symbol, interval
        return [
            Candle(
                open_time_ms=1_700_000_000_000 + index * 60_000,
                close_time_ms=1_700_000_059_999 + index * 60_000,
                open=Decimal("50000.00"),
                high=Decimal("50100.00"),
                low=Decimal("49900.00"),
                close=Decimal("50050.00"),
                volume=Decimal("10.00"),
            )
            for index in range(limit)
        ]

    def close(self) -> None:
        pass
