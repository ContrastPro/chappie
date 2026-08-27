from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.research.capital_flow.fred_alfred_authority import (
    FRED_ALFRED_GLOBAL_RISK_AUTHORITY_IDENTITY,
    FRED_API_KEY_REQUIRED,
    FRED_SOURCE_END,
    FRED_SOURCE_START,
    SERIES_SEMANTICS_V1,
    VALID_SOURCE_ABSENCE,
    VintageObservationV1,
    canonical_series_digest,
    fred_api_key,
    latest_usable_vintage,
)


def test_exact_series_and_semantics_are_frozen() -> None:
    assert FRED_ALFRED_GLOBAL_RISK_AUTHORITY_IDENTITY.endswith("/1.0.0")
    assert (FRED_SOURCE_START, FRED_SOURCE_END) == ("2023-09-01", "2026-05-31")
    assert len(SERIES_SEMANTICS_V1) == 7
    assert SERIES_SEMANTICS_V1["DEXJPUS"] == "JPY_PER_USD"
    assert SERIES_SEMANTICS_V1["STLFSI4"] == "WEEKLY_UNTRANSFORMED_LEVEL"
    assert FRED_API_KEY_REQUIRED == "FRED_API_KEY_REQUIRED"


def test_vintages_preserve_point_in_time_and_fail_closed() -> None:
    first = VintageObservationV1(
        "DGS2",
        "2024-01-02",
        Decimal("4.25"),
        datetime(2024, 1, 3, tzinfo=UTC),
        datetime(2024, 2, 1, tzinfo=UTC),
        datetime(2024, 1, 3, tzinfo=UTC),
    )
    revised = VintageObservationV1(
        "DGS2",
        "2024-01-02",
        Decimal("4.30"),
        datetime(2024, 2, 1, tzinfo=UTC),
        None,
        datetime(2024, 2, 1, tzinfo=UTC),
    )
    records = (first, revised)
    assert (
        latest_usable_vintage(
            records, reference_date="2024-01-02", target_time=datetime(2024, 1, 4, tzinfo=UTC)
        )
        == first
    )
    assert (
        latest_usable_vintage(
            records, reference_date="2024-01-02", target_time=datetime(2024, 2, 2, tzinfo=UTC)
        )
        == revised
    )
    assert canonical_series_digest(records) == canonical_series_digest(records)
    with pytest.raises(ValueError, match="INVALID_VINTAGE"):
        VintageObservationV1(
            "DGS2",
            "2024-01-02",
            None,
            datetime(2024, 2, 1, tzinfo=UTC),
            datetime(2024, 1, 1, tzinfo=UTC),
            datetime(2024, 2, 1, tzinfo=UTC),
        )


def test_missing_credential_and_conflicting_duplicate_vintage_fail_closed(monkeypatch) -> None:
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="FRED_API_KEY_REQUIRED"):
        fred_api_key()
    assert VALID_SOURCE_ABSENCE == "VALID_SOURCE_ABSENCE"
    first = VintageObservationV1(
        "DGS2",
        "2024-01-02",
        Decimal("4.25"),
        datetime(2024, 1, 3, tzinfo=UTC),
        None,
        datetime(2024, 1, 3, tzinfo=UTC),
    )
    conflict = VintageObservationV1(
        "DGS2",
        "2024-01-02",
        Decimal("4.30"),
        datetime(2024, 1, 3, tzinfo=UTC),
        None,
        datetime(2024, 1, 3, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="conflicting"):
        canonical_series_digest((first, conflict))
