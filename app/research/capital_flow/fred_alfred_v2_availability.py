"""Frozen causal-availability contracts and bounded calendars for FRED/ALFRED V2."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .fred_alfred_v2 import (
    CONSERVATIVE_RELEASE_USABLE_AT_POLICY,
    DAILY_EXTERNAL_STALENESS_V2,
    FRED_ALFRED_GLOBAL_RISK_AUTHORITY_V2_IDENTITY,
    FRED_SERIES_V2,
    NASDAQ_CALENDAR_IDENTITY_V2,
    PIT_CLASSIFICATIONS_V2,
    STLFSI4_STALENESS_V2,
)

FRED_ALFRED_RELEASE_VINTAGE_CAUSAL_AVAILABILITY_IDENTITY = (
    "FRED_ALFRED_RELEASE_VINTAGE_CAUSAL_AVAILABILITY/1.0.0"
)
FRED_ALFRED_FULL_VINTAGE_CAUSAL_AVAILABILITY_IDENTITY = (
    "FRED_ALFRED_FULL_VINTAGE_CAUSAL_AVAILABILITY/1.0.0"
)
FRED_ALFRED_STLFSI4_CAUSAL_AVAILABILITY_IDENTITY = "FRED_ALFRED_STLFSI4_CAUSAL_AVAILABILITY/1.0.0"
FRED_ALFRED_NASDAQCOM_CAUSAL_AVAILABILITY_IDENTITY = (
    "FRED_ALFRED_NASDAQCOM_CAUSAL_AVAILABILITY/1.0.0"
)
FRED_ALFRED_VIXCLS_CAUSAL_AVAILABILITY_IDENTITY = "FRED_ALFRED_VIXCLS_CAUSAL_AVAILABILITY/1.0.0"
CBOE_VIX_DAILY_CLOSE_SESSION_CALENDAR_IDENTITY = "CBOE_VIX_DAILY_CLOSE_SESSION_CALENDAR/1.0.0"
US_FEDERAL_BUSINESS_DAY_CALENDAR_IDENTITY = "US_FEDERAL_BUSINESS_DAY_CALENDAR/1.0.0"

AVAILABILITY_CALENDAR_DAYS = "START_OF_UTC_DAY(REALTIME_START_DATE_PLUS_2_CALENDAR_DAYS)"
FULL_VINTAGE_AVAILABILITY = (
    "START_OF_UTC_DAY(THAT_VINTAGE_REALTIME_START_DATE_PLUS_2_CALENDAR_DAYS)"
)
VIXCLS_USABLE_AT_POLICY = (
    "MAX(OFFICIAL_CBOE_VIX_SOURCE_SESSION_CLOSE_UTC,"
    "START_OF_UTC_DAY(REALTIME_START_DATE_PLUS_2_CALENDAR_DAYS))"
)
DAILY_FEDERAL_STALENESS = "<=3_US_FEDERAL_BUSINESS_DAYS"
STALENESS_CALENDAR_DAYS = "CALENDAR_DAYS"

CALENDAR_START = date(2023, 9, 1)
CALENDAR_END = date(2026, 5, 31)
CBOE_VIX_MARKET_SOURCE_IDENTITY = "CBOE_VIX_SPOT_INDEX_DAILY_CLOSE_SOURCE_SESSION"
CBOE_VIX_TIMEZONE = "America/New_York"
CBOE_VIX_NORMAL_CLOSE = time(16, 15)
CBOE_VIX_SPECIAL_CLOSE = time(13, 15)

# Official Cboe U.S. Options annual calendars and special notices, bounded to V2.
CBOE_VIX_FULL_DAY_CLOSURES = frozenset(
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
CBOE_VIX_SPECIAL_CLOSES = {
    date(2023, 11, 24): CBOE_VIX_SPECIAL_CLOSE,
    date(2024, 7, 3): CBOE_VIX_SPECIAL_CLOSE,
    date(2024, 11, 29): CBOE_VIX_SPECIAL_CLOSE,
    date(2024, 12, 24): CBOE_VIX_SPECIAL_CLOSE,
    date(2025, 7, 3): CBOE_VIX_SPECIAL_CLOSE,
    date(2025, 11, 28): CBOE_VIX_SPECIAL_CLOSE,
    date(2025, 12, 24): CBOE_VIX_SPECIAL_CLOSE,
}

# Nationwide observed holidays under 5 U.S.C. §6103; no regional/ad-hoc closures.
US_FEDERAL_OBSERVED_HOLIDAYS = frozenset(
    {
        date(2023, 9, 4),
        date(2023, 10, 9),
        date(2023, 11, 10),
        date(2023, 11, 23),
        date(2023, 12, 25),
        date(2024, 1, 1),
        date(2024, 1, 15),
        date(2024, 2, 19),
        date(2024, 5, 27),
        date(2024, 6, 19),
        date(2024, 7, 4),
        date(2024, 9, 2),
        date(2024, 10, 14),
        date(2024, 11, 11),
        date(2024, 11, 28),
        date(2024, 12, 25),
        date(2025, 1, 1),
        date(2025, 1, 20),
        date(2025, 2, 17),
        date(2025, 5, 26),
        date(2025, 6, 19),
        date(2025, 7, 4),
        date(2025, 9, 1),
        date(2025, 10, 13),
        date(2025, 11, 11),
        date(2025, 11, 27),
        date(2025, 12, 25),
        date(2026, 1, 1),
        date(2026, 1, 19),
        date(2026, 2, 16),
        date(2026, 5, 25),
    }
)
US_FEDERAL_REGIONAL_HOLIDAYS_EXCLUDED = frozenset({date(2025, 1, 20)})


@dataclass(frozen=True, slots=True)
class BoundedCalendarAuthorityV1:
    identity: str
    market_or_jurisdiction: str
    start: date
    end: date
    timezone: str
    source: str


CBOE_VIX_CALENDAR_AUTHORITY = BoundedCalendarAuthorityV1(
    CBOE_VIX_DAILY_CLOSE_SESSION_CALENDAR_IDENTITY,
    CBOE_VIX_MARKET_SOURCE_IDENTITY,
    CALENDAR_START,
    CALENDAR_END,
    CBOE_VIX_TIMEZONE,
    "OFFICIAL_CBOE_VIX_METHODOLOGY_US_OPTIONS_HOURS_HOLIDAY_NOTICES",
)
US_FEDERAL_BUSINESS_DAY_AUTHORITY = BoundedCalendarAuthorityV1(
    US_FEDERAL_BUSINESS_DAY_CALENDAR_IDENTITY,
    "UNITED_STATES_NATIONWIDE_FEDERAL_BUSINESS_DAY",
    CALENDAR_START,
    CALENDAR_END,
    "UTC",
    "OFFICIAL_OPM_5_USC_6103_OBSERVED_HOLIDAY_SCHEDULES",
)


def _bounded(day: date) -> None:
    if not CALENDAR_START <= day <= CALENDAR_END:
        raise ValueError("date outside bounded causal calendar")


def is_cboe_vix_source_session(day: date) -> bool:
    """Return whether the bounded date has an official Cboe VIX source session."""
    _bounded(day)
    return day.weekday() < 5 and day not in CBOE_VIX_FULL_DAY_CLOSURES


def cboe_vix_source_session_close(day: date) -> datetime:
    """Return the official bounded VIX source-session close as a UTC datetime."""
    if not is_cboe_vix_source_session(day):
        raise ValueError("date is not a Cboe VIX source session")
    local_close = CBOE_VIX_SPECIAL_CLOSES.get(day, CBOE_VIX_NORMAL_CLOSE)
    return datetime.combine(day, local_close, ZoneInfo(CBOE_VIX_TIMEZONE)).astimezone(UTC)


def previous_completed_cboe_vix_source_session(timestamp: datetime) -> date:
    """Return the latest bounded VIX source session completed by an aware timestamp."""
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    current = min(timestamp.astimezone(UTC).date(), CALENDAR_END)
    _bounded(current)
    while current >= CALENDAR_START:
        if is_cboe_vix_source_session(current) and cboe_vix_source_session_close(
            current
        ) <= timestamp.astimezone(UTC):
            return current
        current -= timedelta(days=1)
    raise ValueError("no completed Cboe VIX source session in bounded calendar")


def is_us_federal_business_day(day: date) -> bool:
    """Use Monday-Friday excluding frozen nationwide observed federal holidays."""
    _bounded(day)
    return day.weekday() < 5 and day not in US_FEDERAL_OBSERVED_HOLIDAYS


def count_us_federal_business_days(start_exclusive: date, end_inclusive: date) -> int:
    """Count federal business days in the explicit interval (start, end]."""
    _bounded(start_exclusive)
    _bounded(end_inclusive)
    if end_inclusive < start_exclusive:
        raise ValueError("federal business-day interval is reversed")
    current = start_exclusive + timedelta(days=1)
    count = 0
    while current <= end_inclusive:
        count += int(is_us_federal_business_day(current))
        current += timedelta(days=1)
    return count


@dataclass(frozen=True, slots=True)
class FredCausalAvailabilityBindingV2:
    series_id: str
    pit_classification: str
    availability_policy_identity: str
    availability_policy: str
    availability_calendar_identity: str | None
    staleness_policy: str
    staleness_calendar_identity: str | None
    authority_identity: str = FRED_ALFRED_GLOBAL_RISK_AUTHORITY_V2_IDENTITY


FRED_CAUSAL_AVAILABILITY_BINDINGS_V2 = (
    FredCausalAvailabilityBindingV2(
        "DEXJPUS",
        dict(PIT_CLASSIFICATIONS_V2)["DEXJPUS"],
        FRED_ALFRED_RELEASE_VINTAGE_CAUSAL_AVAILABILITY_IDENTITY,
        AVAILABILITY_CALENDAR_DAYS,
        None,
        DAILY_FEDERAL_STALENESS,
        US_FEDERAL_BUSINESS_DAY_CALENDAR_IDENTITY,
    ),
    FredCausalAvailabilityBindingV2(
        "NASDAQCOM",
        dict(PIT_CLASSIFICATIONS_V2)["NASDAQCOM"],
        FRED_ALFRED_NASDAQCOM_CAUSAL_AVAILABILITY_IDENTITY,
        CONSERVATIVE_RELEASE_USABLE_AT_POLICY,
        NASDAQ_CALENDAR_IDENTITY_V2,
        DAILY_FEDERAL_STALENESS,
        US_FEDERAL_BUSINESS_DAY_CALENDAR_IDENTITY,
    ),
    FredCausalAvailabilityBindingV2(
        "DTWEXBGS",
        dict(PIT_CLASSIFICATIONS_V2)["DTWEXBGS"],
        FRED_ALFRED_FULL_VINTAGE_CAUSAL_AVAILABILITY_IDENTITY,
        FULL_VINTAGE_AVAILABILITY,
        None,
        DAILY_FEDERAL_STALENESS,
        US_FEDERAL_BUSINESS_DAY_CALENDAR_IDENTITY,
    ),
    FredCausalAvailabilityBindingV2(
        "DGS2",
        dict(PIT_CLASSIFICATIONS_V2)["DGS2"],
        FRED_ALFRED_RELEASE_VINTAGE_CAUSAL_AVAILABILITY_IDENTITY,
        AVAILABILITY_CALENDAR_DAYS,
        None,
        DAILY_FEDERAL_STALENESS,
        US_FEDERAL_BUSINESS_DAY_CALENDAR_IDENTITY,
    ),
    FredCausalAvailabilityBindingV2(
        "DGS10",
        dict(PIT_CLASSIFICATIONS_V2)["DGS10"],
        FRED_ALFRED_RELEASE_VINTAGE_CAUSAL_AVAILABILITY_IDENTITY,
        AVAILABILITY_CALENDAR_DAYS,
        None,
        DAILY_FEDERAL_STALENESS,
        US_FEDERAL_BUSINESS_DAY_CALENDAR_IDENTITY,
    ),
    FredCausalAvailabilityBindingV2(
        "STLFSI4",
        dict(PIT_CLASSIFICATIONS_V2)["STLFSI4"],
        FRED_ALFRED_STLFSI4_CAUSAL_AVAILABILITY_IDENTITY,
        FULL_VINTAGE_AVAILABILITY,
        None,
        STLFSI4_STALENESS_V2,
        STALENESS_CALENDAR_DAYS,
    ),
    FredCausalAvailabilityBindingV2(
        "VIXCLS",
        dict(PIT_CLASSIFICATIONS_V2)["VIXCLS"],
        FRED_ALFRED_VIXCLS_CAUSAL_AVAILABILITY_IDENTITY,
        VIXCLS_USABLE_AT_POLICY,
        CBOE_VIX_DAILY_CLOSE_SESSION_CALENDAR_IDENTITY,
        DAILY_FEDERAL_STALENESS,
        US_FEDERAL_BUSINESS_DAY_CALENDAR_IDENTITY,
    ),
)

if tuple(binding.series_id for binding in FRED_CAUSAL_AVAILABILITY_BINDINGS_V2) != FRED_SERIES_V2:
    raise RuntimeError("causal availability bindings changed frozen series order")
if DAILY_EXTERNAL_STALENESS_V2 != "<=3_US_BUSINESS_DAYS":
    raise RuntimeError("released daily staleness contract changed unexpectedly")
