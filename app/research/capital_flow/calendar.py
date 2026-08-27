"""XNYS calendar authority adapter for offline Capital Flow research contracts."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import exchange_calendars as xcals

XNYS_CALENDAR_ID = "XNYS"
XNYS_TIMEZONE = "America/New_York"
NASDAQ_COMPLETION_PUBLICATION_LAG = timedelta(hours=1)
NASDAQ_COMPLETION_USABLE_RULE = "ACTUAL_XNYS_CLOSE_PLUS_FIXED_60_MINUTES"

_CALENDAR = xcals.get_calendar(XNYS_CALENDAR_ID)


def _utc(value: datetime, name: str = "timestamp") -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _session_date(value: datetime | date) -> date:
    if isinstance(value, datetime):
        return _utc(value, "session").date()
    return value


def _session_label(value: datetime | date) -> datetime:
    label = _CALENDAR.date_to_session(_session_date(value), direction="none")
    return label.to_pydatetime().replace(tzinfo=UTC)


def session_for_timestamp(timestamp: datetime) -> datetime | None:
    """Return the UTC XNYS session label if ``timestamp`` is in regular trading."""
    timestamp = _utc(timestamp)
    session = _CALENDAR.minute_to_session(timestamp, direction="previous")
    session_date = session.date()
    opened = regular_session_open(session_date)
    closed = regular_session_close(session_date)
    return _session_label(session_date) if opened <= timestamp <= closed else None


def regular_session_open(session: datetime | date) -> datetime:
    """Return the actual XNYS regular-session open as an aware UTC datetime."""
    return _CALENDAR.session_open(_session_date(session)).to_pydatetime().astimezone(UTC)


def regular_session_close(session: datetime | date) -> datetime:
    """Return the actual XNYS regular-session close, including early closes, in UTC."""
    return _CALENDAR.session_close(_session_date(session)).to_pydatetime().astimezone(UTC)


def is_regular_session(timestamp: datetime) -> bool:
    return session_for_timestamp(timestamp) is not None


def completed_session_available_at(session: datetime | date) -> datetime:
    """Freeze the conservative NASDAQCOM usable boundary for a completed session."""
    return regular_session_close(session) + NASDAQ_COMPLETION_PUBLICATION_LAG


def previous_completed_session(timestamp: datetime) -> datetime:
    """Return the latest session eligible under the frozen publication boundary."""
    timestamp = _utc(timestamp)
    candidate = _CALENDAR.minute_to_session(timestamp, direction="previous").date()
    if timestamp >= completed_session_available_at(candidate):
        return _session_label(candidate)
    return _CALENDAR.previous_session(candidate).to_pydatetime().replace(tzinfo=UTC)
