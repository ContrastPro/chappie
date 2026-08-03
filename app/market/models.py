from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel


class ServerTime(BaseModel):
    timestamp_ms: int

    @property
    def datetime_utc(self) -> datetime:
        return datetime.fromtimestamp(
            self.timestamp_ms / 1000,
            tz=UTC,
        )


class TickerPrice(BaseModel):
    symbol: str
    price: Decimal

class Candle(BaseModel):
    open_time_ms: int
    close_time_ms: int

    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    @property
    def open_time_utc(self) -> datetime:
        return datetime.fromtimestamp(
            self.open_time_ms / 1000,
            tz=UTC,
        )

    @property
    def close_time_utc(self) -> datetime:
        return datetime.fromtimestamp(
            self.close_time_ms / 1000,
            tz=UTC,
        )