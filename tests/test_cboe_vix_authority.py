from datetime import UTC, date, datetime

import pytest

from app.research.capital_flow.cboe_vix_authority import (
    CBOE_PROVIDER_IDENTITY,
    CBOE_VIX_AUTHORITY_IDENTITY,
    CBOE_VIX_END,
    CBOE_VIX_START,
    parse_bounded_cboe_vix_csv,
)

HEADER = "DATE,OPEN,HIGH,LOW,CLOSE\n"


def test_identity_bounds_decimal_and_normal_usable_time() -> None:
    result = parse_bounded_cboe_vix_csv(
        [HEADER, "06/03/2025,18,19,17,18.50\n", "06/04/2025,BOOM,BOOM,BOOM,BOOM\n"],
        coverage_start=date(2025, 6, 3),
        coverage_end=date(2025, 6, 3),
    )
    assert (result.authority_identity, result.provider_identity) == (
        CBOE_VIX_AUTHORITY_IDENTITY,
        CBOE_PROVIDER_IDENTITY,
    )
    assert (CBOE_VIX_START.isoformat(), CBOE_VIX_END.isoformat()) == ("2023-09-01", "2026-05-31")
    assert str(result.rows[0].close) == "18.50"
    assert result.rows[0].usable_at_utc == datetime(2025, 6, 3, 21, tzinfo=UTC)
    assert result.quality.cutoff_crossing_date == date(2025, 6, 4)


@pytest.mark.parametrize(
    "value", ["bad", "NaN", "+Infinity", "Infinity", "-Infinity", "", "0", "-1"]
)
def test_invalid_close_rejects(value: str) -> None:
    with pytest.raises(ValueError, match="session"):
        parse_bounded_cboe_vix_csv(
            [HEADER, f"06/03/2025,1,1,1,{value}\n"],
            coverage_start=date(2025, 6, 3),
            coverage_end=date(2025, 6, 3),
        )


def test_weekend_holiday_and_conflicting_duplicate_reject() -> None:
    for row in ("06/07/2025,1,1,1,18\n", "07/04/2025,1,1,1,18\n"):
        with pytest.raises(ValueError, match="session"):
            parse_bounded_cboe_vix_csv(
                [HEADER, row], coverage_start=_date(row), coverage_end=_date(row)
            )
    with pytest.raises(ValueError, match="conflicting"):
        parse_bounded_cboe_vix_csv(
            [HEADER, "06/03/2025,1,1,1,18\n", "06/03/2025,1,1,1,19\n"],
            coverage_start=date(2025, 6, 3),
            coverage_end=date(2025, 6, 3),
        )


def _date(row: str) -> date:
    return datetime.strptime(row[:10], "%m/%d/%Y").replace(tzinfo=UTC).date()


def test_early_close_and_duplicate_deduplication() -> None:
    row = "07/03/2025,1,1,1,18\n"
    result = parse_bounded_cboe_vix_csv(
        [HEADER, row, row], coverage_start=date(2025, 7, 3), coverage_end=date(2025, 7, 3)
    )
    assert result.rows[0].usable_at_utc == datetime(2025, 7, 3, 18, tzinfo=UTC)
    assert result.quality.identical_duplicate_count == 1


def test_decreasing_source_dates_fail_closed_without_sorting() -> None:
    with pytest.raises(ValueError, match="CBOE_VIX_SOURCE_ORDER_INVALID"):
        parse_bounded_cboe_vix_csv(
            [HEADER, "06/04/2025,1,1,1,18\n", "06/03/2025,1,1,1,19\n"],
            coverage_start=date(2025, 6, 3),
            coverage_end=date(2025, 6, 4),
        )


def test_repeated_parse_is_deterministic() -> None:
    lines = [HEADER, "06/03/2025,1,1,1,18\n", "06/04/2025,1,1,1,19\n"]
    first = parse_bounded_cboe_vix_csv(
        lines, coverage_start=date(2025, 6, 3), coverage_end=date(2025, 6, 4)
    )
    second = parse_bounded_cboe_vix_csv(
        lines, coverage_start=date(2025, 6, 3), coverage_end=date(2025, 6, 4)
    )
    assert first == second


def test_missing_session_and_cutoff_does_not_parse_or_request_another_row() -> None:
    class Guarded:
        def __iter__(self):
            yield HEADER
            yield "06/03/2025,1,1,1,18\n"
            yield "06/05/2025,SECRET,SECRET,SECRET,NOT_A_NUMBER\n"
            raise AssertionError("second post-cutoff row requested")

    result = parse_bounded_cboe_vix_csv(
        Guarded(), coverage_start=date(2025, 6, 3), coverage_end=date(2025, 6, 4)
    )
    assert result.quality.cutoff_crossing_date == date(2025, 6, 5)
    assert result.quality.rows_seen_after_cutoff == 1
    assert result.quality.missing_expected_sessions == (date(2025, 6, 4),)
