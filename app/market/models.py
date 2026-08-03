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