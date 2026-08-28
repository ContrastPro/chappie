from __future__ import annotations

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
from app.research.capital_flow.fred_alfred_v2_semantic import (
    FRED_ALFRED_V2_SEMANTIC_EVIDENCE_IDENTITY,
    VALID_SOURCE_ABSENCE,
    parse_raw_evidence_part,
    parse_semantic_evidence_set,
)


def _part(series: str, index: int = 0):
    plan = build_fred_alfred_v2_request_plan(series)
    if plan.full_vintage_template is None:
        return plan.initial_parts[0]
    return build_full_vintage_observation_parts(series, ("2024-01-02",))[index]


def _evidence(series: str, records: tuple[object, ...], index: int = 0):
    part = _part(series, index)
    page = ProviderPageV2(
        part.identity,
        series,
        part.endpoint,
        len(records),
        0,
        part.pagination.limit,
        len(records),
        records,
        (("content_type", "application/json"),),
    )
    return build_raw_evidence(part, page)


def _enum_evidence(series: str, records: tuple[object, ...]):
    part = build_fred_alfred_v2_request_plan(series).initial_parts[0]
    page = ProviderPageV2(
        part.identity,
        series,
        part.endpoint,
        len(records),
        0,
        part.pagination.limit,
        len(records),
        records,
        (("content_type", "application/json"),),
    )
    return build_raw_evidence(part, page)


def _full_evidence(series: str, dates: tuple[str, ...], records: tuple[object, ...]):
    part = build_full_vintage_observation_parts(series, dates)[0]
    page = ProviderPageV2(
        part.identity,
        series,
        part.endpoint,
        len(records),
        0,
        part.pagination.limit,
        len(records),
        records,
        (("content_type", "application/json"),),
    )
    return build_raw_evidence(part, page)


def _release_row(value: str = "12.50", **extra: str) -> MappingProxyType:
    return MappingProxyType(
        {
            "date": "2024-01-02",
            "value": value,
            "realtime_start": "2024-01-03",
            "realtime_end": "9999-12-31",
            **extra,
        }
    )


def _full_row(value: str = "101.25") -> MappingProxyType:
    return MappingProxyType({"date": "2024-01-02", "DTWEXBGS_20240103": value})


def test_identity_and_immutable_decimal_observation() -> None:
    record = parse_raw_evidence_part(_evidence("DEXJPUS", (_release_row(),)))[0]
    assert record.schema_identity == FRED_ALFRED_V2_SEMANTIC_EVIDENCE_IDENTITY
    assert record.value == Decimal("12.50")
    with pytest.raises(AttributeError):
        record.value = Decimal(1)  # type: ignore[misc]


@pytest.mark.parametrize("value", ("12.50", "0", "-3.125", "1E-8", "+12.0", "0001.2300"))
def test_exact_decimal_values(value: str) -> None:
    assert parse_raw_evidence_part(_evidence("DGS2", (_release_row(value),)))[0].value == Decimal(
        value
    )


@pytest.mark.parametrize(
    "value", ("NaN", "Infinity", "-Infinity", "+Infinity", "sNaN", "1_0", "not-a-number", "")
)
def test_invalid_decimal_values_fail_closed(value: str) -> None:
    with pytest.raises(ValueError, match="MALFORMED_SOURCE_RECORD"):
        parse_raw_evidence_part(_evidence("DGS2", (_release_row(value),)))


def test_dot_is_explicit_source_absence_never_zero() -> None:
    record = parse_raw_evidence_part(_evidence("DGS2", (_release_row("."),)))[0]
    assert (record.value, record.value_state) == (None, VALID_SOURCE_ABSENCE)


@pytest.mark.parametrize(
    "field,value",
    (
        ("date", "bad"),
        ("realtime_start", "bad"),
        ("realtime_end", "bad"),
        ("date", "2024-13-01"),
        ("realtime_start", "2024-01-01T00:00"),
    ),
)
def test_bad_dates_fail_closed(field: str, value: str) -> None:
    row = dict(_release_row())
    row[field] = value
    with pytest.raises(ValueError, match="MALFORMED_SOURCE_RECORD"):
        parse_raw_evidence_part(_evidence("NASDAQCOM", (MappingProxyType(row),)))


def test_full_vintage_columnar_rows_preserve_revisions() -> None:
    enum = _enum_evidence("DTWEXBGS", ("2024-01-02",))
    observations = _evidence("DTWEXBGS", (_full_row(),))
    result = parse_semantic_evidence_set("DTWEXBGS", (enum, observations))
    assert result.record_count == result.distinct_reference_dates == 1
    assert result.observations[0].realtime_start.isoformat() == "2024-01-03"
    assert result.observations[0].realtime_end is None


def test_full_vintage_multiple_columns_are_revisions_not_flattened() -> None:
    enum = _enum_evidence("DTWEXBGS", ("2024-01-02", "2024-01-04"))
    row = MappingProxyType(
        {"date": "2024-01-02", "DTWEXBGS_20240102": "1", "DTWEXBGS_20240104": "2"}
    )
    observations = _full_evidence("DTWEXBGS", ("2024-01-02", "2024-01-04"), (row,))
    result = parse_semantic_evidence_set("DTWEXBGS", (enum, observations))
    assert (
        result.record_count,
        result.revision_count,
        result.maximum_vintages_per_reference_date,
    ) == (2, 1, 2)


def test_full_vintage_dot_is_source_absence_not_zero() -> None:
    record = parse_raw_evidence_part(_evidence("DTWEXBGS", (_full_row("."),)))[0]
    assert (record.value, record.value_state) == (None, VALID_SOURCE_ABSENCE)


def test_identical_semantics_have_equal_digests_but_conflicts_differ() -> None:
    evidence = _evidence("DEXJPUS", (_release_row(),))
    first = parse_raw_evidence_part(evidence)[0]
    assert first.semantic_digest == parse_raw_evidence_part(evidence)[0].semantic_digest
    conflict = _evidence("DEXJPUS", (_release_row("99"),))
    assert first.semantic_digest != parse_raw_evidence_part(conflict)[0].semantic_digest


def test_release_group_rejects_full_strategy_and_full_group_rejects_release_strategy() -> None:
    with pytest.raises(ValueError, match="MALFORMED_SOURCE_RECORD"):
        parse_raw_evidence_part(_evidence("DEXJPUS", (_full_row(),)))
    with pytest.raises(ValueError, match="full-vintage"):
        parse_raw_evidence_part(_evidence("DTWEXBGS", (_release_row(),)))


def test_ordering_and_digest_are_page_layout_invariant() -> None:
    dates = ("2024-01-02", "2024-01-04")
    enum = _enum_evidence("DTWEXBGS", dates)
    first = _evidence(
        "DTWEXBGS",
        (
            MappingProxyType(
                {"date": "2024-01-02", "DTWEXBGS_20240104": "2", "DTWEXBGS_20240102": "1"}
            ),
        ),
    )
    second = _evidence(
        "DTWEXBGS",
        (
            MappingProxyType(
                {"date": "2024-01-02", "DTWEXBGS_20240102": "1", "DTWEXBGS_20240104": "2"}
            ),
        ),
    )
    left = parse_semantic_evidence_set(
        "DTWEXBGS", (enum, _full_evidence("DTWEXBGS", dates, first.provider_records))
    )
    right = parse_semantic_evidence_set(
        "DTWEXBGS", (enum, _full_evidence("DTWEXBGS", dates, second.provider_records))
    )
    assert left.semantic_digest == right.semantic_digest
    assert [item.realtime_start for item in left.observations] == sorted(
        item.realtime_start for item in left.observations
    )


@pytest.mark.parametrize(
    "series,unit",
    (
        ("DEXJPUS", "JPY_PER_USD"),
        ("NASDAQCOM", "NASDAQ_COMPOSITE_INDEX_LEVEL"),
        ("DGS2", "PERCENT"),
        ("DGS10", "PERCENT"),
        ("VIXCLS", "VIX_INDEX_POINTS"),
    ),
)
def test_release_series_units_are_preserved(series: str, unit: str) -> None:
    assert parse_raw_evidence_part(_evidence(series, (_release_row(),)))[0].unit == unit


@pytest.mark.parametrize("series", FRED_SERIES_V2)
def test_parser_source_has_no_http_or_downstream_logic(series: str) -> None:
    source = Path("app/research/capital_flow/fred_alfred_v2_semantic.py").read_text(
        encoding="utf-8"
    )
    assert series in FRED_SERIES_V2
    assert all(
        token not in source
        for token in ("httpx", "FRED_API_KEY", "usable_at", "manifest.json", "feature_", "Research")
    )
