from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType

import pytest

from app.research.capital_flow.fred_alfred_v2 import FRED_SERIES_V2
from app.research.capital_flow.fred_alfred_v2_acquisition.raw_evidence import build_raw_evidence
from app.research.capital_flow.fred_alfred_v2_acquisition.request_plan import (
    build_fred_alfred_v2_request_plan,
    build_full_vintage_observation_parts,
)
from app.research.capital_flow.fred_alfred_v2_acquisition.transport import ProviderPageV2
from app.research.capital_flow.fred_alfred_v2_pit import (
    FRED_ALFRED_V2_PIT_MATERIALIZATION_IDENTITY,
    FRESH,
    INVALID_SOURCE_SESSION,
    KNOWN,
    NO_ELIGIBLE_VINTAGE,
    NO_REFERENCE_OBSERVATION,
    PUBLICATION_NOT_YET_USABLE,
    STALE_SOURCE_RECORD,
    VALID_SOURCE_ABSENCE,
    FredKnownObservationV2,
    materialize_known_at,
    value_known_at,
)
from app.research.capital_flow.fred_alfred_v2_semantic import parse_semantic_evidence_set


def _raw(part, records: tuple[object, ...]):
    page = ProviderPageV2(
        part.identity,
        part.series_id,
        part.endpoint,
        len(records),
        0,
        part.pagination.limit,
        len(records),
        records,
        (("content_type", "application/json"),),
    )
    return build_raw_evidence(part, page)


def _release(
    series: str,
    *,
    reference: str = "2024-01-02",
    realtime_start: str = "2024-01-03",
    value: str = "12.50",
):
    part = build_fred_alfred_v2_request_plan(series).initial_parts[0]
    row = MappingProxyType(
        {
            "date": reference,
            "value": value,
            "realtime_start": realtime_start,
            "realtime_end": "9999-12-31",
        }
    )
    return parse_semantic_evidence_set(series, (_raw(part, (row,)),))


def _full(
    series: str,
    *,
    reference: str = "2024-01-02",
    vintages: tuple[tuple[str, str], ...] = (("20240103", "1"), ("20240105", "2")),
):
    plan = build_fred_alfred_v2_request_plan(series)
    enum = _raw(plan.initial_parts[0], (reference,))
    part = build_full_vintage_observation_parts(series, (reference,))[0]
    row = MappingProxyType(
        {"date": reference, **{f"{series}_{key}": value for key, value in vintages}}
    )
    return parse_semantic_evidence_set(series, (enum, _raw(part, (row,))))


def _at(day: int, hour: int = 0) -> datetime:
    return datetime(2024, 1, day, hour, tzinfo=UTC)


def test_identity_immutable_dto_utc_normalization_and_determinism() -> None:
    evidence = _release("DEXJPUS")
    first = value_known_at(
        evidence,
        "DEXJPUS",
        date(2024, 1, 2),
        datetime(2024, 1, 5, 2, tzinfo=timezone(timedelta(hours=2))),
    )
    second = value_known_at(evidence, "DEXJPUS", date(2024, 1, 2), _at(5))
    assert isinstance(first, FredKnownObservationV2)
    assert first.identity == FRED_ALFRED_V2_PIT_MATERIALIZATION_IDENTITY
    assert first == second
    assert first.usable_at_utc == _at(5)
    assert first.usable_at_utc.tzinfo is UTC
    with pytest.raises(FrozenInstanceError):
        first.status = "changed"  # type: ignore[misc]


def test_naive_timestamp_rejected_and_future_reference_never_leaks() -> None:
    evidence = _release("DGS2")
    with pytest.raises(ValueError, match="timezone-aware"):
        value_known_at(
            evidence,
            "DGS2",
            date(2024, 1, 2),
            datetime(2024, 1, 5),  # noqa: DTZ001
        )
    assert (
        value_known_at(evidence, "DGS2", date(2024, 1, 2), _at(1)).status
        == NO_REFERENCE_OBSERVATION
    )


@pytest.mark.parametrize("series", ("DEXJPUS", "DGS2", "DGS10"))
def test_release_series_before_equal_and_after_plus_two(series: str) -> None:
    evidence = _release(series)
    assert (
        value_known_at(evidence, series, date(2024, 1, 2), _at(4, 23)).status
        == PUBLICATION_NOT_YET_USABLE
    )
    equal = value_known_at(evidence, series, date(2024, 1, 2), _at(5))
    assert (equal.status, equal.usable_at_utc) == (KNOWN, _at(5))
    assert value_known_at(evidence, series, date(2024, 1, 2), _at(5, 1)).status == KNOWN


def test_friday_plus_two_is_sunday_even_when_monday_is_holiday() -> None:
    evidence = _release("DEXJPUS", reference="2024-01-05", realtime_start="2024-01-05")
    assert (
        value_known_at(evidence, "DEXJPUS", date(2024, 1, 5), _at(6, 23)).status
        == PUBLICATION_NOT_YET_USABLE
    )
    assert value_known_at(evidence, "DEXJPUS", date(2024, 1, 5), _at(7)).status == KNOWN


@pytest.mark.parametrize("series", ("DTWEXBGS", "STLFSI4"))
def test_full_vintage_revision_boundary_has_no_early_leakage(series: str) -> None:
    evidence = _full(series)
    reference = date(2024, 1, 2)
    assert value_known_at(evidence, series, reference, _at(4)).status == NO_ELIGIBLE_VINTAGE
    old = value_known_at(evidence, series, reference, _at(6, 23))
    new = value_known_at(evidence, series, reference, _at(7))
    assert (old.value, old.effective_until_utc) == (Decimal(1), _at(7))
    assert (new.value, new.realtime_start) == (Decimal(2), date(2024, 1, 5))
    assert old.selected_vintage_identity != new.selected_vintage_identity


def test_nasdaq_max_boundary_normal_early_close_and_invalid_session() -> None:
    normal = _release("NASDAQCOM", reference="2024-01-02", realtime_start="2024-01-02")
    assert (
        value_known_at(normal, "NASDAQCOM", date(2024, 1, 2), _at(3, 22)).status
        == PUBLICATION_NOT_YET_USABLE
    )
    assert value_known_at(normal, "NASDAQCOM", date(2024, 1, 2), _at(4)).status == KNOWN
    early = _release("NASDAQCOM", reference="2024-07-03", realtime_start="2024-07-01")
    result = value_known_at(
        early, "NASDAQCOM", date(2024, 7, 3), datetime(2024, 7, 3, 17, tzinfo=UTC)
    )
    assert (result.status, result.usable_at_utc) == (KNOWN, datetime(2024, 7, 3, 17, tzinfo=UTC))
    invalid = _release("NASDAQCOM", reference="2024-01-06", realtime_start="2024-01-04")
    assert (
        value_known_at(invalid, "NASDAQCOM", date(2024, 1, 6), _at(7)).status
        == INVALID_SOURCE_SESSION
    )


def test_vix_max_boundary_shortened_close_and_invalid_session() -> None:
    early = _release("VIXCLS", reference="2024-07-03", realtime_start="2024-07-01")
    result = value_known_at(
        early, "VIXCLS", date(2024, 7, 3), datetime(2024, 7, 3, 17, 15, tzinfo=UTC)
    )
    assert (result.status, result.usable_at_utc) == (
        KNOWN,
        datetime(2024, 7, 3, 17, 15, tzinfo=UTC),
    )
    invalid = _release("VIXCLS", reference="2024-01-06", realtime_start="2024-01-04")
    assert (
        value_known_at(invalid, "VIXCLS", date(2024, 1, 6), _at(7)).status == INVALID_SOURCE_SESSION
    )


def test_source_absence_is_explicit_and_never_numeric() -> None:
    evidence = _release("DGS10", value=".")
    result = value_known_at(evidence, "DGS10", date(2024, 1, 2), _at(5))
    assert (result.status, result.value, result.value_state) == (
        VALID_SOURCE_ABSENCE,
        None,
        VALID_SOURCE_ABSENCE,
    )


def test_daily_staleness_threshold_weekend_and_federal_holiday() -> None:
    evidence = _release("DGS2", reference="2024-07-03", realtime_start="2024-07-03")
    exact = value_known_at(evidence, "DGS2", date(2024, 7, 3), datetime(2024, 7, 9, tzinfo=UTC))
    stale = value_known_at(evidence, "DGS2", date(2024, 7, 3), datetime(2024, 7, 10, tzinfo=UTC))
    assert (exact.status, exact.staleness_state, exact.staleness_age) == (KNOWN, FRESH, 3)
    assert (stale.status, stale.staleness_age) == (STALE_SOURCE_RECORD, 4)


@pytest.mark.parametrize("days,status", ((8, KNOWN), (9, KNOWN), (10, STALE_SOURCE_RECORD)))
def test_stlfsi4_literal_calendar_staleness(days: int, status: str) -> None:
    evidence = _full("STLFSI4", vintages=(("20240103", "1"),))
    result = value_known_at(evidence, "STLFSI4", date(2024, 1, 2), _at(2) + timedelta(days=days))
    assert (result.status, result.staleness_age) == (status, days)


def test_all_seven_bindings_and_bounded_bulk_helper() -> None:
    assert FRED_SERIES_V2 == (
        "DEXJPUS",
        "NASDAQCOM",
        "DTWEXBGS",
        "DGS2",
        "DGS10",
        "STLFSI4",
        "VIXCLS",
    )
    evidence = _release("DEXJPUS")
    results = materialize_known_at(
        evidence, ((date(2024, 1, 2), _at(5)), (date(2024, 1, 2), _at(6)))
    )
    assert tuple(item.status for item in results) == (KNOWN, KNOWN)


def test_module_has_no_io_acquisition_publication_features_or_research() -> None:
    source = Path("app/research/capital_flow/fred_alfred_v2_pit.py").read_text(encoding="utf-8")
    assert all(
        token not in source
        for token in (
            "httpx",
            "requests.",
            "FRED_API_KEY",
            "manifest.json",
            "open(",
            "feature_",
            "BTC",
        )
    )
