"""Bounded Nasdaq regular-session authority sourced from official Nasdaq schedules."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from types import MappingProxyType
from zoneinfo import ZoneInfo

NASDAQ_CALENDAR_AUTHORITY_IDENTITY = "NASDAQ_EQUITY_REGULAR_SESSION_CALENDAR/1.0.0"
NASDAQ_MARKET_IDENTITY = "THE_NASDAQ_STOCK_MARKET"
NASDAQ_TIMEZONE = "America/New_York"
NASDAQ_REGULAR_OPEN = time(9, 30)
NASDAQ_REGULAR_CLOSE = time(16, 0)
NASDAQ_SUPPORTED_START = date(2023, 9, 1)
NASDAQ_SUPPORTED_END = date(2026, 5, 31)
NASDAQ_CALENDAR_IMPLEMENTATION_STATUS = "IMPLEMENTED"

NASDAQ_OFFICIAL_SCHEDULE_SOURCES = (
    "https://www.nasdaqtrader.com/content/technicalsupport/2023tradingcalendar.pdf",
    "https://www.nasdaqtrader.com/content/technicalsupport/2024tradingcalendar.pdf",
    "https://www.nasdaqtrader.com/content/technicalsupport/2025tradingcalendar.pdf",
    "https://www.nasdaqtrader.com/Trader.aspx?id=Calendar",
    "https://www.nasdaqtrader.com/TraderNews.aspx?id=ETA2024-87",
)

# Frozen official full-day closures inside the supported range. The 2025-01-09
# national day of mourning is sourced from the Nasdaq Equity Trader Alert above.
_NASDAQ_HOLIDAYS = frozenset(
    {
        date(2023, 9, 4),
        date(2023, 11, 23),
        date(2023, 12, 25),
        date(2024, 1, 1),
        date(2024, 1, 15),
        date(2024, 2, 19),
        date(2024, 3, 29),
        date(2024, 5, 27),
        date(2024, 6, 19),
        date(2024, 7, 4),
        date(2024, 9, 2),
        date(2024, 11, 28),
        date(2024, 12, 25),
        date(2025, 1, 1),
        date(2025, 1, 9),
        date(2025, 1, 20),
        date(2025, 2, 17),
        date(2025, 4, 18),
        date(2025, 5, 26),
        date(2025, 6, 19),
        date(2025, 7, 4),
        date(2025, 9, 1),
        date(2025, 11, 27),
        date(2025, 12, 25),
        date(2026, 1, 1),
        date(2026, 1, 19),
        date(2026, 2, 16),
        date(2026, 4, 3),
        date(2026, 5, 25),
    }
)

# Each date carries its actual officially scheduled close. This is deliberately
# not a rule asserting that every possible Nasdaq early close is 13:00 ET.
NASDAQ_EARLY_CLOSES = MappingProxyType(
    {
        date(2023, 11, 24): time(13, 0),
        date(2024, 7, 3): time(13, 0),
        date(2024, 11, 29): time(13, 0),
        date(2024, 12, 24): time(13, 0),
        date(2025, 7, 3): time(13, 0),
        date(2025, 11, 28): time(13, 0),
        date(2025, 12, 24): time(13, 0),
    }
)

_NEW_YORK = ZoneInfo(NASDAQ_TIMEZONE)


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _bounded(value: date) -> date:
    if not NASDAQ_SUPPORTED_START <= value <= NASDAQ_SUPPORTED_END:
        raise ValueError("Nasdaq session date outside frozen authority range")
    return value


def _local_datetime(session_date: date, local_time: time) -> datetime:
    return datetime.combine(session_date, local_time, tzinfo=_NEW_YORK).astimezone(UTC)


def _session_label(session_date: date) -> datetime:
    return datetime.combine(session_date, time.min, tzinfo=UTC)


def is_nasdaq_regular_session(session_date: date) -> bool:
    """Return whether a bounded date is an official Nasdaq regular session."""
    session_date = _bounded(session_date)
    return session_date.weekday() < 5 and session_date not in _NASDAQ_HOLIDAYS


def nasdaq_regular_session_open(session_date: date) -> datetime:
    """Return the Nasdaq regular-session open as a timezone-aware UTC value."""
    if not is_nasdaq_regular_session(session_date):
        raise ValueError("date is not a Nasdaq regular session")
    return _local_datetime(session_date, NASDAQ_REGULAR_OPEN)


def nasdaq_regular_session_close(session_date: date) -> datetime:
    """Return the actual scheduled Nasdaq close as a timezone-aware UTC value."""
    if not is_nasdaq_regular_session(session_date):
        raise ValueError("date is not a Nasdaq regular session")
    return _local_datetime(
        session_date, NASDAQ_EARLY_CLOSES.get(session_date, NASDAQ_REGULAR_CLOSE)
    )


def nasdaq_session_for_timestamp(timestamp: datetime) -> datetime | None:
    """Return the Nasdaq session label only while its regular session is open."""
    timestamp = _aware_utc(timestamp)
    local_date = _bounded(timestamp.astimezone(_NEW_YORK).date())
    if not is_nasdaq_regular_session(local_date):
        return None
    if (
        nasdaq_regular_session_open(local_date)
        <= timestamp
        <= nasdaq_regular_session_close(local_date)
    ):
        return _session_label(local_date)
    return None


def nasdaq_previous_completed_session(timestamp: datetime) -> datetime:
    """Return the latest Nasdaq session whose actual scheduled close is at or before T."""
    timestamp = _aware_utc(timestamp)
    candidate = _bounded(timestamp.astimezone(_NEW_YORK).date())
    while candidate >= NASDAQ_SUPPORTED_START:
        if is_nasdaq_regular_session(candidate) and timestamp >= nasdaq_regular_session_close(
            candidate
        ):
            return _session_label(candidate)
        candidate -= timedelta(days=1)
    raise ValueError("no completed Nasdaq session inside frozen authority range")
