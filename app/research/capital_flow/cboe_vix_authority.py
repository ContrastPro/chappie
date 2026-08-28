"""Pure bounded parser contracts for the official Cboe VIX daily CSV."""

from __future__ import annotations

import csv
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from exchange_calendars.errors import NotSessionError

from .calendar import completed_session_available_at, regular_session_close

CBOE_VIX_AUTHORITY_IDENTITY = "chappie_cboe_vix_historical_authority/1.0.0"
CBOE_PROVIDER_IDENTITY = "CBOE"
CBOE_VIX_SOURCE_IDENTITY = "OFFICIAL_CBOE_VIX_HISTORICAL_DAILY_CSV"
CBOE_VIX_HEADER = ("DATE", "OPEN", "HIGH", "LOW", "CLOSE")
CBOE_VIX_START = date(2023, 9, 1)
CBOE_VIX_END = date(2026, 5, 31)
CBOE_VIX_BLIND_START = date(2026, 9, 1)
CBOE_VIX_USABLE_POLICY = "ACTUAL_XNYS_CLOSE_PLUS_FIXED_60_MINUTES"


@dataclass(frozen=True, slots=True)
class CboeVixSourceRowV1:
    reference_date: date
    close: Decimal
    session_close_utc: datetime
    usable_at_utc: datetime


@dataclass(frozen=True, slots=True)
class CboeVixQualityEvidenceV1:
    row_count: int
    expected_session_count: int
    missing_expected_sessions: tuple[date, ...]
    unexpected_sessions: tuple[date, ...]
    identical_duplicate_count: int
    conflicting_duplicate_count: int
    malformed_count: int
    first_session: date | None
    last_session: date | None
    cutoff_crossing_date: date | None
    rows_seen_after_cutoff: int


@dataclass(frozen=True, slots=True)
class CboeVixBoundedResultV1:
    authority_identity: str
    provider_identity: str
    authorized_start: date
    authorized_end: date
    rows: tuple[CboeVixSourceRowV1, ...]
    quality: CboeVixQualityEvidenceV1


def _source_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%m/%d/%Y").replace(tzinfo=UTC).date()
    except ValueError as error:
        raise ValueError("malformed Cboe VIX date") from error


def _close(value: str) -> Decimal:
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("malformed Cboe VIX close") from error
    if not result.is_finite() or result <= 0:
        raise ValueError("malformed Cboe VIX close")
    return result


def _expected_sessions(start: date, end: date) -> tuple[date, ...]:
    result: list[date] = []
    cursor = start
    while cursor <= end:
        try:
            regular_session_close(cursor)
        except NotSessionError:
            cursor += timedelta(days=1)
            continue
        else:
            result.append(cursor)
        cursor += timedelta(days=1)
    return tuple(result)


def parse_bounded_cboe_vix_csv(
    lines: Iterable[str],
    *,
    coverage_start: date = CBOE_VIX_START,
    coverage_end: date = CBOE_VIX_END,
) -> CboeVixBoundedResultV1:
    reader = csv.DictReader(lines)
    if tuple(reader.fieldnames or ()) != CBOE_VIX_HEADER:
        raise ValueError("unexpected Cboe VIX CSV schema")
    retained: dict[date, CboeVixSourceRowV1] = {}
    previous: date | None = None
    identical = conflicts = malformed = 0
    cutoff: date | None = None
    for raw in reader:
        try:
            reference = _source_date(raw["DATE"])
        except (KeyError, ValueError):
            malformed += 1
            raise
        if previous is not None and reference < previous:
            raise ValueError("CBOE_VIX_SOURCE_ORDER_INVALID")
        previous = reference
        if reference > coverage_end:
            cutoff = reference
            break
        if reference < coverage_start:
            continue
        try:
            session_close = regular_session_close(reference).astimezone(UTC)
            close = _close(raw["CLOSE"])
        except Exception as error:
            malformed += 1
            raise ValueError("unexpected or malformed Cboe VIX session") from error
        row = CboeVixSourceRowV1(
            reference, close, session_close, completed_session_available_at(reference)
        )
        existing = retained.get(reference)
        if existing is not None:
            if existing == row:
                identical += 1
                continue
            conflicts += 1
            raise ValueError("conflicting Cboe VIX duplicate")
        retained[reference] = row
    expected = _expected_sessions(coverage_start, coverage_end)
    actual = tuple(sorted(retained))
    missing = tuple(item for item in expected if item not in retained)
    unexpected = tuple(item for item in actual if item not in set(expected))
    quality = CboeVixQualityEvidenceV1(
        len(actual),
        len(expected),
        missing,
        unexpected,
        identical,
        conflicts,
        malformed,
        actual[0] if actual else None,
        actual[-1] if actual else None,
        cutoff,
        1 if cutoff else 0,
    )
    return CboeVixBoundedResultV1(
        CBOE_VIX_AUTHORITY_IDENTITY,
        CBOE_PROVIDER_IDENTITY,
        coverage_start,
        coverage_end,
        tuple(retained[item] for item in actual),
        quality,
    )
