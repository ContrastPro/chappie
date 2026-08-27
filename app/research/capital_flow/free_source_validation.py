"""Synthetic validation contracts for Free V1 authorities; no acquisition or values."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .calendar import NASDAQ_COMPLETION_USABLE_RULE
from .free_v1 import FRED_SERIES_V1

SOURCE_RECORD_MISSING = "SOURCE_RECORD_MISSING"
STALE_SOURCE_RECORD = "STALE_SOURCE_RECORD"
USABLE_AT_IN_FUTURE = "USABLE_AT_IN_FUTURE"
SESSION_NOT_COMPLETED = "SESSION_NOT_COMPLETED"
PUBLICATION_NOT_YET_USABLE = "PUBLICATION_NOT_YET_USABLE"
INVALID_VINTAGE = "INVALID_VINTAGE"
INCOMPLETE_COUPLING_WINDOW = "INCOMPLETE_COUPLING_WINDOW"

FRED_ALFRED_CONTRACTS_V1 = tuple(
    (series, "daily_or_weekly_reference_date", "ALFRED_REALTIME_INTERVAL_REQUIRED")
    for series in FRED_SERIES_V1
)
CBOE_VIX_CONTRACT_V1 = (
    "CBOE_DAILY_VIX_CLOSE_PRIMARY",
    "FRED_VIXCLS_VERIFICATION_ONLY",
    "XNYS_SESSION_DATE_INDEX_POINTS",
    "ACTUAL_XNYS_CLOSE_PLUS_FIXED_60_MINUTES",
    "<=3_US_business_days",
)
DEFILLAMA_CONTRACT_V1 = ((1, "USDT"), (2, "USDC"), (5, "DAI"))
BINANCE_CONTRACT_V1 = (
    "BTCUSDT_PLUS_FROZEN_18_BREADTH_SYMBOLS",
    "UTC_DAILY_BOUNDARY",
    "PRE_2025_MILLISECONDS_FROM_2025_MICROSECONDS_CANONICAL_UTC_MILLISECONDS",
)
BTC_DAILY_ALIGNMENT_RULE = (
    "PAIR_EXTERNAL_REFERENCE_DATE_WITH_BTC_UTC_DAILY_RETURN_ENDING_ON_SAME_DATE"
)


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be UTC")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class DailySourceFixtureV1:
    authority_id: str
    reference_date: str
    event_time: datetime
    realtime_start: datetime
    realtime_end: datetime | None
    published_at: datetime
    usable_at: datetime
    staleness_limit: timedelta

    def __post_init__(self) -> None:
        for name in ("event_time", "realtime_start", "published_at", "usable_at"):
            _utc(getattr(self, name), name)
        if self.realtime_end is not None:
            _utc(self.realtime_end, "realtime_end")
        if self.realtime_start > self.usable_at or self.published_at > self.usable_at:
            raise ValueError(INVALID_VINTAGE)

    def unavailable_reason_at(self, target_time: datetime) -> str | None:
        target_time = _utc(target_time, "target_time")
        if self.realtime_end is not None and target_time >= self.realtime_end:
            return INVALID_VINTAGE
        if target_time < self.usable_at:
            return PUBLICATION_NOT_YET_USABLE
        if target_time - self.event_time > self.staleness_limit:
            return STALE_SOURCE_RECORD
        return None


@dataclass(frozen=True, slots=True)
class CouplingObservationFixtureV1:
    btc_present: bool
    external_present: bool
    completed: bool
    usable_at: datetime
    event_time: datetime
    stale: bool = False


def validate_coupling_observation_window(
    points: tuple[CouplingObservationFixtureV1, ...], *, target_time: datetime, required_count: int
) -> str | None:
    target_time = _utc(target_time, "target_time")
    if len(points) != required_count:
        return INCOMPLETE_COUPLING_WINDOW
    for point in points:
        if not point.btc_present or not point.external_present:
            return SOURCE_RECORD_MISSING
        if not point.completed:
            return SESSION_NOT_COMPLETED
        if point.usable_at > target_time or point.event_time > target_time:
            return USABLE_AT_IN_FUTURE
        if point.stale:
            return STALE_SOURCE_RECORD
    return None


def nasdaq_usable_boundary_rule() -> str:
    return NASDAQ_COMPLETION_USABLE_RULE
