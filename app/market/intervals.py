from types import MappingProxyType
from typing import Final

from app.market.exceptions import UnsupportedMarketIntervalError

SECOND_MS: Final = 1_000
MINUTE_MS: Final = 60 * SECOND_MS
HOUR_MS: Final = 60 * MINUTE_MS
DAY_MS: Final = 24 * HOUR_MS
WEEK_MS: Final = 7 * DAY_MS

BINANCE_SPOT_FIXED_INTERVAL_MS = MappingProxyType(
    {
        "1s": SECOND_MS,
        "1m": MINUTE_MS,
        "3m": 3 * MINUTE_MS,
        "5m": 5 * MINUTE_MS,
        "15m": 15 * MINUTE_MS,
        "30m": 30 * MINUTE_MS,
        "1h": HOUR_MS,
        "2h": 2 * HOUR_MS,
        "4h": 4 * HOUR_MS,
        "6h": 6 * HOUR_MS,
        "8h": 8 * HOUR_MS,
        "12h": 12 * HOUR_MS,
        "1d": DAY_MS,
        "3d": 3 * DAY_MS,
        "1w": WEEK_MS,
    }
)
BINANCE_SPOT_CALENDAR_INTERVALS = frozenset({"1M"})
BINANCE_SPOT_INTERVALS = frozenset(
    (*BINANCE_SPOT_FIXED_INTERVAL_MS, *BINANCE_SPOT_CALENDAR_INTERVALS)
)


def fixed_binance_spot_interval_ms(interval: str) -> int:
    """Return milliseconds for a supported fixed-duration Binance Spot interval."""

    if not isinstance(interval, str):
        raise TypeError("Interval must be a string")

    try:
        return BINANCE_SPOT_FIXED_INTERVAL_MS[interval]
    except KeyError as error:
        raise UnsupportedMarketIntervalError(
            f"Unsupported fixed-duration Binance Spot interval: {interval!r}"
        ) from error
