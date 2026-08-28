from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from app.research.capital_flow.fred_alfred_v2 import (
    NASDAQ_CALENDAR_IDENTITY_V2,
    NASDAQ_CALENDAR_IMPLEMENTATION_STATUS,
    NASDAQ_SESSION_SEMANTIC_V2,
)
from app.research.capital_flow.nasdaq_calendar import (
    NASDAQ_CALENDAR_AUTHORITY_IDENTITY,
    NASDAQ_EARLY_CLOSES,
    NASDAQ_MARKET_IDENTITY,
    NASDAQ_OFFICIAL_SCHEDULE_SOURCES,
    NASDAQ_SUPPORTED_END,
    NASDAQ_SUPPORTED_START,
    NASDAQ_TIMEZONE,
    is_nasdaq_regular_session,
    nasdaq_previous_completed_session,
    nasdaq_regular_session_close,
    nasdaq_regular_session_open,
    nasdaq_session_for_timestamp,
)
from app.research.capital_flow.nasdaq_calendar import (
    NASDAQ_CALENDAR_IMPLEMENTATION_STATUS as ADAPTER_STATUS,
)


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def test_identity_is_nasdaq_specific_bound_and_not_xnys() -> None:
    assert NASDAQ_CALENDAR_AUTHORITY_IDENTITY == NASDAQ_CALENDAR_IDENTITY_V2
    assert NASDAQ_MARKET_IDENTITY == "THE_NASDAQ_STOCK_MARKET"
    assert "XNYS" not in NASDAQ_CALENDAR_AUTHORITY_IDENTITY
    assert "XNYS" not in NASDAQ_SESSION_SEMANTIC_V2
    assert NASDAQ_TIMEZONE == "America/New_York"
    assert ADAPTER_STATUS == NASDAQ_CALENDAR_IMPLEMENTATION_STATUS == "IMPLEMENTED"
    assert NASDAQ_SUPPORTED_START == date(2023, 9, 1)
    assert NASDAQ_SUPPORTED_END == date(2026, 5, 31)
    assert all("nasdaqtrader.com" in source.lower() for source in NASDAQ_OFFICIAL_SCHEDULE_SOURCES)


def test_normal_winter_and_summer_sessions_use_iana_dst_and_utc_outputs() -> None:
    winter = date(2025, 1, 10)
    summer = date(2025, 6, 3)
    assert nasdaq_regular_session_open(winter) == utc(2025, 1, 10, 14, 30)
    assert nasdaq_regular_session_close(winter) == utc(2025, 1, 10, 21)
    assert nasdaq_regular_session_open(summer) == utc(2025, 6, 3, 13, 30)
    assert nasdaq_regular_session_close(summer) == utc(2025, 6, 3, 20)
    assert nasdaq_regular_session_open(winter).tzinfo is UTC
    assert nasdaq_regular_session_close(summer).tzinfo is UTC


def test_weekends_official_holidays_and_special_closure_are_not_sessions() -> None:
    assert not is_nasdaq_regular_session(date(2025, 6, 7))
    assert not is_nasdaq_regular_session(date(2024, 7, 4))
    assert not is_nasdaq_regular_session(date(2025, 1, 9))
    with pytest.raises(ValueError, match="not a Nasdaq"):
        nasdaq_regular_session_open(date(2025, 1, 9))


def test_actual_early_close_is_returned_and_is_the_completion_boundary() -> None:
    early = date(2024, 11, 29)
    assert NASDAQ_EARLY_CLOSES[early].isoformat() == "13:00:00"
    assert nasdaq_regular_session_close(early) == utc(2024, 11, 29, 18)
    assert nasdaq_previous_completed_session(utc(2024, 11, 29, 17, 59)) == utc(2024, 11, 27)
    assert nasdaq_previous_completed_session(utc(2024, 11, 29, 18)) == utc(2024, 11, 29)


def test_session_for_timestamp_has_exact_regular_session_boundaries() -> None:
    session = utc(2025, 6, 3)
    assert nasdaq_session_for_timestamp(utc(2025, 6, 3, 13, 29)) is None
    assert nasdaq_session_for_timestamp(utc(2025, 6, 3, 13, 30)) == session
    assert nasdaq_session_for_timestamp(utc(2025, 6, 3, 16)) == session
    assert nasdaq_session_for_timestamp(utc(2025, 6, 3, 20)) == session
    assert nasdaq_session_for_timestamp(utc(2025, 6, 3, 20, 1)) is None


def test_previous_completed_session_handles_weekdays_weekends_and_holidays() -> None:
    assert nasdaq_previous_completed_session(utc(2025, 6, 3, 19)) == utc(2025, 6, 2)
    assert nasdaq_previous_completed_session(utc(2025, 6, 3, 20)) == utc(2025, 6, 3)
    assert nasdaq_previous_completed_session(utc(2025, 6, 7, 15)) == utc(2025, 6, 6)
    assert nasdaq_previous_completed_session(utc(2024, 7, 5, 12)) == utc(2024, 7, 3)


def test_naive_timestamps_and_dates_outside_frozen_range_fail_closed() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        nasdaq_session_for_timestamp(datetime(2025, 6, 3, 13, 30))  # noqa: DTZ001
    with pytest.raises(ValueError, match="outside frozen"):
        is_nasdaq_regular_session(date(2023, 8, 31))


def test_adapter_has_no_fred_http_feature_or_research_implementation() -> None:
    source = Path("app/research/capital_flow/nasdaq_calendar.py").read_text(encoding="utf-8")
    forbidden = ("httpx", "requests", "FRED_API_KEY", "realtime_start", "feature value")
    assert all(token not in source for token in forbidden)
