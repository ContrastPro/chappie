"""Causal point-in-time selection over immutable FRED/ALFRED V2 evidence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from .fred_alfred_v2 import FRED_ALFRED_GLOBAL_RISK_AUTHORITY_V2_IDENTITY, FRED_SERIES_V2
from .fred_alfred_v2_acquisition.contracts import FULL_VINTAGE_SERIES_V2
from .fred_alfred_v2_availability import (
    FRED_CAUSAL_AVAILABILITY_BINDINGS_V2,
    cboe_vix_source_session_close,
    count_us_federal_business_days,
)
from .fred_alfred_v2_semantic import (
    OBSERVED_VALUE,
    VALID_SOURCE_ABSENCE,
    FredSemanticEvidenceSetV2,
    FredVintageObservationV2,
)
from .nasdaq_calendar import nasdaq_regular_session_close

FRED_ALFRED_V2_PIT_MATERIALIZATION_IDENTITY = "chappie_fred_alfred_v2_pit_materialization/1.0.0"

KNOWN = "KNOWN"
NO_REFERENCE_OBSERVATION = "NO_REFERENCE_OBSERVATION"
PUBLICATION_NOT_YET_USABLE = "PUBLICATION_NOT_YET_USABLE"
NO_ELIGIBLE_VINTAGE = "NO_ELIGIBLE_VINTAGE"
STALE_SOURCE_RECORD = "STALE_SOURCE_RECORD"
INVALID_SOURCE_SESSION = "INVALID_SOURCE_SESSION"
MALFORMED_SEMANTIC_EVIDENCE = "MALFORMED_SEMANTIC_EVIDENCE"
UNAVAILABLE_STATES = (
    NO_REFERENCE_OBSERVATION,
    VALID_SOURCE_ABSENCE,
    PUBLICATION_NOT_YET_USABLE,
    NO_ELIGIBLE_VINTAGE,
    STALE_SOURCE_RECORD,
    INVALID_SOURCE_SESSION,
    MALFORMED_SEMANTIC_EVIDENCE,
)

FRESH = "FRESH"
STALE = "STALE"
DAILY_STALENESS_ANCHOR = "ECONOMIC_REFERENCE_DATE"
STLFSI4_STALENESS_ANCHOR = "WEEKLY_ECONOMIC_REFERENCE_DATE"

_BINDINGS = {item.series_id: item for item in FRED_CAUSAL_AVAILABILITY_BINDINGS_V2}


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _utc(timestamp: datetime) -> datetime:
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return timestamp.astimezone(UTC)


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time.min, UTC)


def _usable_at(observation: FredVintageObservationV2) -> datetime:
    released = _midnight(observation.realtime_start + timedelta(days=2))
    if observation.series_id == "NASDAQCOM":
        try:
            return max(released, nasdaq_regular_session_close(observation.reference_date))
        except ValueError as error:
            raise ValueError(INVALID_SOURCE_SESSION) from error
    if observation.series_id == "VIXCLS":
        try:
            return max(released, cboe_vix_source_session_close(observation.reference_date))
        except ValueError as error:
            raise ValueError(INVALID_SOURCE_SESSION) from error
    return released


def _staleness(observation: FredVintageObservationV2, timestamp: datetime) -> tuple[str, int]:
    end = timestamp.date()
    if end < observation.reference_date:
        return STALE, -1
    if observation.series_id == "STLFSI4":
        age = (end - observation.reference_date).days
        return (FRESH if age <= 9 else STALE), age
    age = count_us_federal_business_days(observation.reference_date, end)
    return (FRESH if age <= 3 else STALE), age


@dataclass(frozen=True, slots=True)
class FredKnownObservationV2:
    series_id: str
    reference_date: date
    status: str
    value: Decimal | None
    value_state: str | None
    realtime_start: date | None
    realtime_end: date | None
    effective_until_utc: datetime | None
    usable_at_utc: datetime | None
    staleness_state: str | None
    staleness_age: int | None
    selected_vintage_identity: str | None
    source_semantic_digest: str
    materialization_digest: str
    identity: str = FRED_ALFRED_V2_PIT_MATERIALIZATION_IDENTITY
    authority_identity: str = FRED_ALFRED_GLOBAL_RISK_AUTHORITY_V2_IDENTITY

    def __post_init__(self) -> None:
        if self.status not in (KNOWN, *UNAVAILABLE_STATES):
            raise ValueError("invalid PIT result status")
        if self.status == KNOWN and (
            self.value_state != OBSERVED_VALUE
            or self.value is None
            or self.usable_at_utc is None
            or self.staleness_state != FRESH
        ):
            raise ValueError("invalid known PIT result")
        if self.materialization_digest != _digest(self._payload()):
            raise ValueError("PIT materialization digest invalid")

    def _payload(self) -> dict[str, object]:
        binding = _BINDINGS[self.series_id]
        return {
            "identity": self.identity,
            "authority_identity": self.authority_identity,
            "series_id": self.series_id,
            "series_contract": {
                "pit_classification": binding.pit_classification,
                "availability_policy_identity": binding.availability_policy_identity,
                "staleness_policy": binding.staleness_policy,
                "staleness_calendar_identity": binding.staleness_calendar_identity,
            },
            "reference_date": self.reference_date.isoformat(),
            "status": self.status,
            "value": None if self.value is None else str(self.value),
            "value_state": self.value_state,
            "realtime_start": None
            if self.realtime_start is None
            else self.realtime_start.isoformat(),
            "realtime_end": None if self.realtime_end is None else self.realtime_end.isoformat(),
            "effective_until_utc": (
                None if self.effective_until_utc is None else self.effective_until_utc.isoformat()
            ),
            "usable_at_utc": None if self.usable_at_utc is None else self.usable_at_utc.isoformat(),
            "staleness_state": self.staleness_state,
            "staleness_age": self.staleness_age,
            "selected_vintage_identity": self.selected_vintage_identity,
            "source_semantic_digest": self.source_semantic_digest,
        }


def _result(
    evidence: FredSemanticEvidenceSetV2,
    reference_date: date,
    status: str,
    observation: FredVintageObservationV2 | None = None,
    *,
    usable_at: datetime | None = None,
    effective_until: datetime | None = None,
    staleness_state: str | None = None,
    staleness_age: int | None = None,
) -> FredKnownObservationV2:
    vintage_identity = None
    if observation is not None:
        vintage_identity = _digest(
            {
                "series_id": observation.series_id,
                "reference_date": observation.reference_date.isoformat(),
                "realtime_start": observation.realtime_start.isoformat(),
                "semantic_digest": observation.semantic_digest,
            }
        )
    values = {
        "series_id": evidence.series_id,
        "reference_date": reference_date,
        "status": status,
        "value": observation.value if status == KNOWN and observation is not None else None,
        "value_state": observation.value_state if observation is not None else None,
        "realtime_start": observation.realtime_start if observation is not None else None,
        "realtime_end": observation.realtime_end if observation is not None else None,
        "effective_until_utc": effective_until,
        "usable_at_utc": usable_at,
        "staleness_state": staleness_state,
        "staleness_age": staleness_age,
        "selected_vintage_identity": vintage_identity,
        "source_semantic_digest": evidence.semantic_digest,
    }
    provisional = object.__new__(FredKnownObservationV2)
    for key, value in values.items():
        object.__setattr__(provisional, key, value)
    object.__setattr__(provisional, "identity", FRED_ALFRED_V2_PIT_MATERIALIZATION_IDENTITY)
    object.__setattr__(
        provisional, "authority_identity", FRED_ALFRED_GLOBAL_RISK_AUTHORITY_V2_IDENTITY
    )
    return FredKnownObservationV2(**values, materialization_digest=_digest(provisional._payload()))


def value_known_at(
    semantic_evidence: FredSemanticEvidenceSetV2,
    series_id: str,
    reference_date: date,
    timestamp: datetime,
) -> FredKnownObservationV2:
    """Select exactly the semantic vintage causally usable at an aware timestamp."""
    at = _utc(timestamp)
    if series_id not in FRED_SERIES_V2:
        raise ValueError("series_id is outside the frozen FRED V2 catalogue")
    if not isinstance(semantic_evidence, FredSemanticEvidenceSetV2) or (
        semantic_evidence.series_id != series_id
    ):
        raise ValueError(MALFORMED_SEMANTIC_EVIDENCE)
    if reference_date > at.date():
        return _result(semantic_evidence, reference_date, NO_REFERENCE_OBSERVATION)
    candidates = tuple(
        item for item in semantic_evidence.observations if item.reference_date == reference_date
    )
    if not candidates:
        return _result(semantic_evidence, reference_date, NO_REFERENCE_OBSERVATION)
    try:
        scheduled = tuple((item, _usable_at(item)) for item in candidates)
    except ValueError as error:
        if str(error) == INVALID_SOURCE_SESSION:
            return _result(semantic_evidence, reference_date, INVALID_SOURCE_SESSION)
        return _result(semantic_evidence, reference_date, MALFORMED_SEMANTIC_EVIDENCE)
    eligible = tuple(pair for pair in scheduled if pair[1] <= at)
    if not eligible:
        status = (
            NO_ELIGIBLE_VINTAGE
            if series_id in FULL_VINTAGE_SERIES_V2
            else PUBLICATION_NOT_YET_USABLE
        )
        return _result(semantic_evidence, reference_date, status)
    observation, usable_at = max(
        eligible, key=lambda pair: (pair[0].realtime_start, pair[0].semantic_digest)
    )
    later = sorted(pair[1] for pair in scheduled if pair[1] > usable_at)
    effective_until = later[0] if later else None
    if observation.value_state not in (OBSERVED_VALUE, VALID_SOURCE_ABSENCE):
        return _result(
            semantic_evidence,
            reference_date,
            MALFORMED_SEMANTIC_EVIDENCE,
            observation,
            usable_at=usable_at,
            effective_until=effective_until,
        )
    if observation.value_state == VALID_SOURCE_ABSENCE:
        return _result(
            semantic_evidence,
            reference_date,
            VALID_SOURCE_ABSENCE,
            observation,
            usable_at=usable_at,
            effective_until=effective_until,
        )
    staleness_state, age = _staleness(observation, at)
    status = KNOWN if staleness_state == FRESH else STALE_SOURCE_RECORD
    result = _result(
        semantic_evidence,
        reference_date,
        status,
        observation,
        usable_at=usable_at,
        effective_until=effective_until,
        staleness_state=staleness_state,
        staleness_age=age,
    )
    if result.status == KNOWN and (result.usable_at_utc is None or result.usable_at_utc > at):
        raise AssertionError("causal PIT invariant violated")
    return result


def materialize_known_at(
    semantic_evidence: FredSemanticEvidenceSetV2,
    requests: tuple[tuple[date, datetime], ...],
) -> tuple[FredKnownObservationV2, ...]:
    """Materialize bounded caller-supplied reference-date/timestamp pairs in memory."""
    return tuple(
        value_known_at(semantic_evidence, semantic_evidence.series_id, reference, timestamp)
        for reference, timestamp in requests
    )
