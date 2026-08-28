"""Read-only semantic normalization of validated FRED/ALFRED V2 raw evidence."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from .fred_alfred_v2 import (
    FRED_ALFRED_GLOBAL_RISK_AUTHORITY_V2_IDENTITY,
    FRED_SERIES_V2,
    PIT_CLASSIFICATIONS_V2,
    PIT_FULL_VINTAGE_REQUIRED,
    PIT_RELEASE_VINTAGE_REQUIRED,
)
from .fred_alfred_v2_acquisition.contracts import (
    FULL_VINTAGE_SERIES_V2,
    RELEASE_VINTAGE_SERIES_V2,
)
from .fred_alfred_v2_acquisition.raw_evidence import (
    EVIDENCE_COMPLETE,
    RawEvidencePartV2,
    inspect_raw_evidence_set,
)
from .fred_alfred_v2_acquisition.request_plan import build_fred_alfred_v2_request_plan

FRED_ALFRED_V2_SEMANTIC_EVIDENCE_IDENTITY = "chappie_fred_alfred_v2_semantic_evidence/1.0.0"
SEMANTIC_EVIDENCE_COMPLETE = "SEMANTIC_EVIDENCE_COMPLETE"
OBSERVED_VALUE = "OBSERVED_VALUE"
VALID_SOURCE_ABSENCE = "VALID_SOURCE_ABSENCE"
ACQUISITION_GAP = "ACQUISITION_GAP"
MALFORMED_SOURCE_RECORD = "MALFORMED_SOURCE_RECORD"
VALUE_STATES_V2 = (OBSERVED_VALUE, VALID_SOURCE_ABSENCE, ACQUISITION_GAP, MALFORMED_SOURCE_RECORD)

_UNITS = {
    "DEXJPUS": "JPY_PER_USD",
    "NASDAQCOM": "NASDAQ_COMPOSITE_INDEX_LEVEL",
    "DTWEXBGS": "BROAD_TRADE_WEIGHTED_US_DOLLAR_INDEX",
    "DGS2": "PERCENT",
    "DGS10": "PERCENT",
    "STLFSI4": "INDEX_LEVEL",
    "VIXCLS": "VIX_INDEX_POINTS",
}


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _date(value: object, field: str) -> date:
    if not isinstance(value, str):
        raise TypeError(f"{MALFORMED_SOURCE_RECORD}: {field}")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{MALFORMED_SOURCE_RECORD}: {field}") from error


def _value(value: object) -> tuple[Decimal | None, str]:
    if value == ".":
        return None, VALID_SOURCE_ABSENCE
    if not isinstance(value, str) or not re.fullmatch(
        r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", value
    ):
        raise ValueError(f"{MALFORMED_SOURCE_RECORD}: value")
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{MALFORMED_SOURCE_RECORD}: value") from error
    if not parsed.is_finite():
        raise ValueError(f"{MALFORMED_SOURCE_RECORD}: value")
    return parsed, OBSERVED_VALUE


@dataclass(frozen=True, slots=True)
class FredVintageObservationV2:
    series_id: str
    reference_date: date
    value: Decimal | None
    value_state: str
    realtime_start: date
    realtime_end: date | None
    source_request_identity: str
    source_page_identity: str
    pit_classification: str
    unit: str
    semantic_digest: str
    schema_identity: str = FRED_ALFRED_V2_SEMANTIC_EVIDENCE_IDENTITY
    authority_identity: str = FRED_ALFRED_GLOBAL_RISK_AUTHORITY_V2_IDENTITY

    def __post_init__(self) -> None:
        if (
            self.series_id not in FRED_SERIES_V2
            or self.value_state not in VALUE_STATES_V2
            or self.pit_classification != dict(PIT_CLASSIFICATIONS_V2)[self.series_id]
            or self.unit != _UNITS[self.series_id]
            or (self.value is None) != (self.value_state == VALID_SOURCE_ABSENCE)
            or (self.realtime_end is not None and self.realtime_end < self.realtime_start)
        ):
            raise ValueError("semantic observation contract invalid")
        if self.semantic_digest != _digest(self._semantic_payload()):
            raise ValueError("semantic observation digest invalid")

    def _semantic_payload(self) -> dict[str, object]:
        return {
            "schema_identity": self.schema_identity,
            "authority_identity": self.authority_identity,
            "series_id": self.series_id,
            "reference_date": self.reference_date.isoformat(),
            "value": None if self.value is None else str(self.value),
            "value_state": self.value_state,
            "realtime_start": self.realtime_start.isoformat(),
            "realtime_end": None if self.realtime_end is None else self.realtime_end.isoformat(),
            "pit_classification": self.pit_classification,
            "unit": self.unit,
        }


def _observation(
    evidence: RawEvidencePartV2,
    reference: object,
    raw_value: object,
    realtime_start: object,
    realtime_end: object | None,
) -> FredVintageObservationV2:
    parsed_value, state = _value(raw_value)
    values = {
        "series_id": evidence.series_id,
        "reference_date": _date(reference, "date"),
        "value": parsed_value,
        "value_state": state,
        "realtime_start": _date(realtime_start, "realtime_start"),
        "realtime_end": None if realtime_end is None else _date(realtime_end, "realtime_end"),
        "source_request_identity": evidence.request_part_identity,
        "source_page_identity": evidence.provider_page_identity,
        "pit_classification": evidence.pit_strategy,
        "unit": _UNITS[evidence.series_id],
    }
    provisional = object.__new__(FredVintageObservationV2)
    for key, value in values.items():
        object.__setattr__(provisional, key, value)
    object.__setattr__(provisional, "schema_identity", FRED_ALFRED_V2_SEMANTIC_EVIDENCE_IDENTITY)
    object.__setattr__(
        provisional, "authority_identity", FRED_ALFRED_GLOBAL_RISK_AUTHORITY_V2_IDENTITY
    )
    return FredVintageObservationV2(
        **values, semantic_digest=_digest(provisional._semantic_payload())
    )


def parse_raw_evidence_part(evidence: RawEvidencePartV2) -> tuple[FredVintageObservationV2, ...]:
    """Parse a validated observations page; no cache, network, or PIT operations occur."""
    if evidence.series_id in RELEASE_VINTAGE_SERIES_V2:
        if (
            evidence.pit_strategy != PIT_RELEASE_VINTAGE_REQUIRED
            or evidence.request_kind != "INITIAL_RELEASE_OBSERVATIONS"
        ):
            raise ValueError("release-vintage raw evidence strategy mismatch")
        result = []
        for row in evidence.provider_records:
            if not isinstance(row, Mapping) or set(row) != {
                "date",
                "value",
                "realtime_start",
                "realtime_end",
            }:
                raise ValueError(f"{MALFORMED_SOURCE_RECORD}: release observation shape")
            result.append(
                _observation(
                    evidence, row["date"], row["value"], row["realtime_start"], row["realtime_end"]
                )
            )
        return tuple(result)
    if (
        evidence.series_id not in FULL_VINTAGE_SERIES_V2
        or evidence.pit_strategy != PIT_FULL_VINTAGE_REQUIRED
        or evidence.request_kind != "FULL_VINTAGE_OBSERVATIONS"
    ):
        raise ValueError("full-vintage raw evidence strategy mismatch")
    result = []
    prefix = f"{evidence.series_id}_"
    for row in evidence.provider_records:
        if not isinstance(row, Mapping) or "date" not in row:
            raise ValueError(f"{MALFORMED_SOURCE_RECORD}: full-vintage observation shape")
        columns = [key for key in row if key != "date"]
        if not columns or any(
            not isinstance(key, str) or not key.startswith(prefix) for key in columns
        ):
            raise ValueError(f"{MALFORMED_SOURCE_RECORD}: full-vintage column")
        for key in columns:
            realtime_start = f"{key[-8:-4]}-{key[-4:-2]}-{key[-2:]}"
            result.append(_observation(evidence, row["date"], row[key], realtime_start, None))
    return tuple(result)


@dataclass(frozen=True, slots=True)
class FredSemanticEvidenceSetV2:
    series_id: str
    observations: tuple[FredVintageObservationV2, ...]
    record_count: int
    missing_value_count: int
    distinct_reference_dates: int
    vintage_count: int
    revision_count: int
    reference_dates_with_revisions: int
    maximum_vintages_per_reference_date: int
    first_reference_date: date | None
    last_reference_date: date | None
    semantic_digest: str
    completeness_state: str = SEMANTIC_EVIDENCE_COMPLETE
    schema_identity: str = FRED_ALFRED_V2_SEMANTIC_EVIDENCE_IDENTITY

    def __post_init__(self) -> None:
        if (
            self.series_id not in FRED_SERIES_V2
            or self.completeness_state != SEMANTIC_EVIDENCE_COMPLETE
            or any(item.series_id != self.series_id for item in self.observations)
            or self.observations
            != tuple(
                sorted(
                    self.observations,
                    key=lambda item: (
                        item.reference_date,
                        item.realtime_start,
                        item.realtime_end or date.max,
                    ),
                )
            )
            or self.record_count != len(self.observations)
            or self.missing_value_count
            != sum(item.value_state == VALID_SOURCE_ABSENCE for item in self.observations)
            or self.semantic_digest != _digest(self._semantic_payload())
        ):
            raise ValueError("semantic evidence set contract invalid")

    def _semantic_payload(self) -> dict[str, object]:
        return {
            "identity": self.schema_identity,
            "authority": FRED_ALFRED_GLOBAL_RISK_AUTHORITY_V2_IDENTITY,
            "series_id": self.series_id,
            "pit_classification": dict(PIT_CLASSIFICATIONS_V2)[self.series_id],
            "unit": _UNITS[self.series_id],
            "observations": [item._semantic_payload() for item in self.observations],
        }


def parse_semantic_evidence_set(
    series_id: str, evidence_parts: tuple[RawEvidencePartV2, ...]
) -> FredSemanticEvidenceSetV2:
    """Normalize one complete validated raw evidence set into immutable semantic DTOs."""
    plan = build_fred_alfred_v2_request_plan(series_id)
    raw = inspect_raw_evidence_set(plan, evidence_parts)
    if raw.status != EVIDENCE_COMPLETE:
        raise ValueError("raw evidence is not complete")
    parsed = [
        item
        for part in evidence_parts
        if part.request_kind != "BOUNDED_VINTAGE_DATE_ENUMERATION"
        for item in parse_raw_evidence_part(part)
    ]
    by_identity: dict[tuple[date, date], FredVintageObservationV2] = {}
    for item in parsed:
        key = (item.reference_date, item.realtime_start)
        existing = by_identity.get(key)
        if existing is not None and existing.semantic_digest != item.semantic_digest:
            raise ValueError("conflicting same-vintage semantic observation")
        if existing is None or (item.source_request_identity, item.source_page_identity) < (
            existing.source_request_identity,
            existing.source_page_identity,
        ):
            by_identity[key] = item
    observations = tuple(
        sorted(
            by_identity.values(),
            key=lambda item: (
                item.reference_date,
                item.realtime_start,
                item.realtime_end or date.max,
            ),
        )
    )
    if series_id in RELEASE_VINTAGE_SERIES_V2 and len(
        {item.reference_date for item in observations}
    ) != len(observations):
        raise ValueError("release-vintage evidence contains multiple records per reference date")
    grouped: defaultdict[date, int] = defaultdict(int)
    for item in observations:
        grouped[item.reference_date] += 1
    dates = tuple(grouped)
    values = {
        "series_id": series_id,
        "observations": observations,
        "record_count": len(observations),
        "missing_value_count": sum(
            item.value_state == VALID_SOURCE_ABSENCE for item in observations
        ),
        "distinct_reference_dates": len(grouped),
        "vintage_count": len({item.realtime_start for item in observations}),
        "revision_count": sum(count - 1 for count in grouped.values()),
        "reference_dates_with_revisions": sum(count > 1 for count in grouped.values()),
        "maximum_vintages_per_reference_date": max(grouped.values(), default=0),
        "first_reference_date": min(dates, default=None),
        "last_reference_date": max(dates, default=None),
    }
    provisional = object.__new__(FredSemanticEvidenceSetV2)
    for key, value in values.items():
        object.__setattr__(provisional, key, value)
    object.__setattr__(provisional, "completeness_state", SEMANTIC_EVIDENCE_COMPLETE)
    object.__setattr__(provisional, "schema_identity", FRED_ALFRED_V2_SEMANTIC_EVIDENCE_IDENTITY)
    return FredSemanticEvidenceSetV2(
        **values,
        semantic_digest=_digest(provisional._semantic_payload()),
    )
