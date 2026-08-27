"""Point-in-time FRED/ALFRED authority contracts; no acquisition without FRED_API_KEY."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal

from .free_v1 import FRED_SERIES_V1

FRED_ALFRED_GLOBAL_RISK_AUTHORITY_IDENTITY = "chappie_fred_alfred_global_risk_authority/1.0.0"
FRED_SOURCE_START = "2023-09-01"
FRED_SOURCE_END = "2026-05-31"
FRED_API_KEY_REQUIRED = "FRED_API_KEY_REQUIRED"
VALID_SOURCE_ABSENCE = "VALID_SOURCE_ABSENCE"
MALFORMED_SOURCE_RECORD = "MALFORMED_SOURCE_RECORD"
ACQUISITION_GAP = "ACQUISITION_GAP"
SERIES_SEMANTICS_V1 = {
    "DEXJPUS": "JPY_PER_USD",
    "GOLDAMGBD228NLBM": "LBMA_AM_USD_FIX",
    "NASDAQCOM": "NASDAQ_COMPOSITE_COMPLETED_XNYS_SESSION",
    "DTWEXBGS": "BROAD_TRADE_WEIGHTED_USD_INDEX",
    "DGS2": "PERCENT_YIELD_2_YEAR",
    "DGS10": "PERCENT_YIELD_10_YEAR",
    "STLFSI4": "WEEKLY_UNTRANSFORMED_LEVEL",
}


@dataclass(frozen=True, slots=True)
class VintageObservationV1:
    series_id: str
    reference_date: str
    value: Decimal | None
    realtime_start: datetime
    realtime_end: datetime | None
    usable_at: datetime

    def __post_init__(self) -> None:
        if (
            self.series_id not in FRED_SERIES_V1
            or self.realtime_start.tzinfo is None
            or self.usable_at.tzinfo is None
        ):
            raise ValueError("invalid FRED/ALFRED vintage record")
        if self.realtime_start.astimezone(UTC) > self.usable_at.astimezone(UTC):
            raise ValueError("INVALID_VINTAGE")
        if self.realtime_end is not None and self.realtime_end.astimezone(
            UTC
        ) <= self.realtime_start.astimezone(UTC):
            raise ValueError("INVALID_VINTAGE")


def fred_api_key() -> str:
    """Read the credential only from the approved local environment boundary."""
    value = os.environ.get("FRED_API_KEY")
    if not value:
        raise RuntimeError(FRED_API_KEY_REQUIRED)
    return value


def canonical_series_digest(records: tuple[VintageObservationV1, ...]) -> str:
    identities: dict[tuple[str, str, datetime], Decimal | None] = {}
    for record in records:
        identity = (record.series_id, record.reference_date, record.realtime_start.astimezone(UTC))
        previous = identities.setdefault(identity, record.value)
        if previous != record.value:
            raise ValueError("conflicting duplicate vintage")
    payload = [
        {
            **asdict(record),
            "value": None if record.value is None else str(record.value),
            "realtime_start": record.realtime_start.astimezone(UTC).isoformat(),
            "realtime_end": None
            if record.realtime_end is None
            else record.realtime_end.astimezone(UTC).isoformat(),
            "usable_at": record.usable_at.astimezone(UTC).isoformat(),
        }
        for record in records
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def latest_usable_vintage(
    records: tuple[VintageObservationV1, ...], *, reference_date: str, target_time: datetime
) -> VintageObservationV1 | None:
    target = target_time.astimezone(UTC)
    candidates = [
        record
        for record in records
        if record.reference_date == reference_date
        and record.usable_at.astimezone(UTC) <= target
        and (record.realtime_end is None or target < record.realtime_end.astimezone(UTC))
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda record: record.realtime_start.astimezone(UTC))
