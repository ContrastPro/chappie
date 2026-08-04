from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel

from app.market.exceptions import MarketStreamKeyError


@dataclass(frozen=True, slots=True)
class MarketStreamKey:
    symbol: str
    interval: str

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol:
            raise MarketStreamKeyError("Market stream symbol must be a non-empty string")
        if self.symbol != self.symbol.strip().upper():
            raise MarketStreamKeyError("Market stream symbol must be normalized")
        if not self.symbol.isascii() or not self.symbol.isalnum():
            raise MarketStreamKeyError(
                "Market stream symbol must contain only ASCII letters and digits"
            )
        if not isinstance(self.interval, str) or not self.interval:
            raise MarketStreamKeyError(
                "Market stream interval must be a non-empty string"
            )
        if self.interval != self.interval.strip():
            raise MarketStreamKeyError("Market stream interval must be normalized")


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


@dataclass(frozen=True, slots=True)
class CandleSnapshot:
    open_time_ms: int
    close_time_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    @classmethod
    def from_candle(cls, candle: Candle) -> "CandleSnapshot":
        return cls(
            open_time_ms=candle.open_time_ms,
            close_time_ms=candle.close_time_ms,
            open=candle.open,
            high=candle.high,
            low=candle.low,
            close=candle.close,
            volume=candle.volume,
        )
