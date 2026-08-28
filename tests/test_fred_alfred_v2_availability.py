from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from app.research.capital_flow.fred_alfred_v2 import (
    CONSERVATIVE_RELEASE_USABLE_AT_POLICY,
    FRED_ALFRED_GLOBAL_RISK_AUTHORITY_V2_IDENTITY,
    FRED_SERIES_V2,
    NASDAQ_CALENDAR_IDENTITY_V2,
    PIT_CLASSIFICATIONS_V2,
)
from app.research.capital_flow.fred_alfred_v2_availability import (
    AVAILABILITY_CALENDAR_DAYS,
    CALENDAR_END,
    CALENDAR_START,
    CBOE_VIX_CALENDAR_AUTHORITY,
    CBOE_VIX_DAILY_CLOSE_SESSION_CALENDAR_IDENTITY,
    CBOE_VIX_MARKET_SOURCE_IDENTITY,
    CBOE_VIX_TIMEZONE,
    DAILY_FEDERAL_STALENESS,
    FRED_ALFRED_FULL_VINTAGE_CAUSAL_AVAILABILITY_IDENTITY,
    FRED_ALFRED_RELEASE_VINTAGE_CAUSAL_AVAILABILITY_IDENTITY,
    FRED_ALFRED_STLFSI4_CAUSAL_AVAILABILITY_IDENTITY,
    FRED_CAUSAL_AVAILABILITY_BINDINGS_V2,
    FULL_VINTAGE_AVAILABILITY,
    US_FEDERAL_BUSINESS_DAY_AUTHORITY,
    US_FEDERAL_BUSINESS_DAY_CALENDAR_IDENTITY,
    US_FEDERAL_REGIONAL_HOLIDAYS_EXCLUDED,
    VIXCLS_USABLE_AT_POLICY,
    cboe_vix_source_session_close,
    count_us_federal_business_days,
    is_cboe_vix_source_session,
    is_us_federal_business_day,
    previous_completed_cboe_vix_source_session,
)

BINDINGS = {binding.series_id: binding for binding in FRED_CAUSAL_AVAILABILITY_BINDINGS_V2}


def test_authority_identity_and_contract_completion_are_immutable() -> None:
    assert {item.authority_identity for item in BINDINGS.values()} == {
        FRED_ALFRED_GLOBAL_RISK_AUTHORITY_V2_IDENTITY
    }
    with pytest.raises(FrozenInstanceError):
        BINDINGS["DEXJPUS"].series_id = "OTHER"  # type: ignore[misc]


def test_shared_release_policy_identity_and_exact_series() -> None:
    selected = tuple(
        item.series_id
        for item in FRED_CAUSAL_AVAILABILITY_BINDINGS_V2
        if item.availability_policy_identity
        == FRED_ALFRED_RELEASE_VINTAGE_CAUSAL_AVAILABILITY_IDENTITY
    )
    assert selected == ("DEXJPUS", "DGS2", "DGS10")
    assert {BINDINGS[item].availability_policy for item in selected} == {AVAILABILITY_CALENDAR_DAYS}


@pytest.mark.parametrize("series", ("DEXJPUS", "DGS2", "DGS10"))
def test_shared_release_binding(series: str) -> None:
    binding = BINDINGS[series]
    assert binding.availability_policy_identity == (
        FRED_ALFRED_RELEASE_VINTAGE_CAUSAL_AVAILABILITY_IDENTITY
    )
    assert binding.pit_classification == dict(PIT_CLASSIFICATIONS_V2)[series]


def test_dgs2_dgs10_share_exact_policy() -> None:
    assert (
        BINDINGS["DGS2"].availability_policy_identity
        == BINDINGS["DGS10"].availability_policy_identity
    )


def test_full_vintage_and_stlfsi4_policy_identities() -> None:
    assert BINDINGS["DTWEXBGS"].availability_policy_identity == (
        FRED_ALFRED_FULL_VINTAGE_CAUSAL_AVAILABILITY_IDENTITY
    )
    assert BINDINGS["STLFSI4"].availability_policy_identity == (
        FRED_ALFRED_STLFSI4_CAUSAL_AVAILABILITY_IDENTITY
    )
    assert BINDINGS["DTWEXBGS"].availability_policy == FULL_VINTAGE_AVAILABILITY
    assert BINDINGS["STLFSI4"].availability_policy == FULL_VINTAGE_AVAILABILITY


def test_nasdaq_policy_and_authority_are_unchanged() -> None:
    binding = BINDINGS["NASDAQCOM"]
    assert binding.availability_policy == CONSERVATIVE_RELEASE_USABLE_AT_POLICY
    assert binding.availability_calendar_identity == NASDAQ_CALENDAR_IDENTITY_V2


def test_cboe_vix_calendar_identity_source_timezone_and_range() -> None:
    assert CBOE_VIX_CALENDAR_AUTHORITY.identity == (CBOE_VIX_DAILY_CLOSE_SESSION_CALENDAR_IDENTITY)
    assert CBOE_VIX_CALENDAR_AUTHORITY.market_or_jurisdiction == (CBOE_VIX_MARKET_SOURCE_IDENTITY)
    assert CBOE_VIX_CALENDAR_AUTHORITY.timezone == CBOE_VIX_TIMEZONE == "America/New_York"
    assert (CBOE_VIX_CALENDAR_AUTHORITY.start, CBOE_VIX_CALENDAR_AUTHORITY.end) == (
        CALENDAR_START,
        CALENDAR_END,
    )


@pytest.mark.parametrize(
    "day,expected",
    (
        (date(2024, 1, 10), datetime(2024, 1, 10, 21, 15, tzinfo=UTC)),
        (date(2024, 7, 10), datetime(2024, 7, 10, 20, 15, tzinfo=UTC)),
    ),
)
def test_cboe_normal_close_observes_iana_dst(day: date, expected: datetime) -> None:
    assert cboe_vix_source_session_close(day) == expected
    assert cboe_vix_source_session_close(day).tzinfo is UTC


@pytest.mark.parametrize("day", (date(2024, 7, 4), date(2025, 1, 9), date(2026, 4, 3)))
def test_cboe_official_full_day_closures(day: date) -> None:
    assert not is_cboe_vix_source_session(day)
    with pytest.raises(ValueError, match="not a Cboe"):
        cboe_vix_source_session_close(day)


@pytest.mark.parametrize(
    "day,expected",
    (
        (date(2023, 11, 24), datetime(2023, 11, 24, 18, 15, tzinfo=UTC)),
        (date(2024, 7, 3), datetime(2024, 7, 3, 17, 15, tzinfo=UTC)),
        (date(2025, 12, 24), datetime(2025, 12, 24, 18, 15, tzinfo=UTC)),
    ),
)
def test_cboe_special_close(day: date, expected: datetime) -> None:
    assert cboe_vix_source_session_close(day) == expected


def test_previous_completed_cboe_session_rejects_naive_and_is_causal() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        previous_completed_cboe_vix_source_session(datetime(2024, 1, 10, 22))  # noqa: DTZ001
    assert previous_completed_cboe_vix_source_session(
        datetime(2024, 1, 10, 21, 15, tzinfo=UTC)
    ) == date(2024, 1, 10)
    assert previous_completed_cboe_vix_source_session(
        datetime(2024, 1, 10, 21, 14, tzinfo=UTC)
    ) == date(2024, 1, 9)


def test_vix_binding_preserves_source_calendar_and_plus_two_term() -> None:
    binding = BINDINGS["VIXCLS"]
    assert binding.availability_calendar_identity == (
        CBOE_VIX_DAILY_CLOSE_SESSION_CALENDAR_IDENTITY
    )
    assert binding.availability_policy == VIXCLS_USABLE_AT_POLICY
    assert "PLUS_2_CALENDAR_DAYS" in binding.availability_policy


def test_federal_calendar_identity_definition_and_range() -> None:
    authority = US_FEDERAL_BUSINESS_DAY_AUTHORITY
    assert authority.identity == US_FEDERAL_BUSINESS_DAY_CALENDAR_IDENTITY
    assert authority.market_or_jurisdiction == "UNITED_STATES_NATIONWIDE_FEDERAL_BUSINESS_DAY"
    assert (authority.start, authority.end) == (CALENDAR_START, CALENDAR_END)


@pytest.mark.parametrize(
    "day,expected",
    (
        (date(2024, 1, 10), True),
        (date(2024, 1, 13), False),
        (date(2024, 7, 4), False),
        (date(2025, 11, 11), False),
        (date(2026, 1, 19), False),
    ),
)
def test_federal_business_days_and_holidays(day: date, expected: bool) -> None:
    assert is_us_federal_business_day(day) is expected


def test_regional_inauguration_and_ad_hoc_closure_are_excluded() -> None:
    assert US_FEDERAL_REGIONAL_HOLIDAYS_EXCLUDED == frozenset({date(2025, 1, 20)})
    assert is_us_federal_business_day(date(2025, 1, 9))
    assert not is_us_federal_business_day(date(2025, 1, 20))


def test_range_start_labor_day_is_observed_holiday() -> None:
    assert not is_us_federal_business_day(date(2023, 9, 4))


def test_federal_business_day_count_is_start_exclusive_end_inclusive() -> None:
    assert count_us_federal_business_days(date(2024, 7, 3), date(2024, 7, 8)) == 2
    assert count_us_federal_business_days(date(2024, 7, 8), date(2024, 7, 8)) == 0


@pytest.mark.parametrize(
    "start,expected",
    ((date(2024, 1, 5), date(2024, 1, 7)), (date(2024, 7, 3), date(2024, 7, 5))),
)
def test_availability_delay_is_literal_calendar_days(start: date, expected: date) -> None:
    assert start + timedelta(days=2) == expected


def test_daily_staleness_exact_series_and_stlfsi4_exception() -> None:
    daily = tuple(
        item.series_id
        for item in FRED_CAUSAL_AVAILABILITY_BINDINGS_V2
        if item.staleness_policy == DAILY_FEDERAL_STALENESS
    )
    assert daily == ("DEXJPUS", "NASDAQCOM", "DTWEXBGS", "DGS2", "DGS10", "VIXCLS")
    assert BINDINGS["STLFSI4"].staleness_policy == "<=9_CALENDAR_DAYS"


def test_complete_frozen_seven_series_table() -> None:
    assert tuple(BINDINGS) == FRED_SERIES_V2
    assert len(FRED_CAUSAL_AVAILABILITY_BINDINGS_V2) == 7


@pytest.mark.parametrize(
    "forbidden",
    (
        "value_known_at",
        "latest eligible",
        "usable_at(",
        "httpx",
        "FRED_API_KEY",
        "manifest.json",
        "feature_",
        "Research",
    ),
)
def test_contract_module_has_no_pit_network_or_downstream_logic(forbidden: str) -> None:
    source = Path("app/research/capital_flow/fred_alfred_v2_availability.py").read_text(
        encoding="utf-8"
    )
    assert forbidden not in source


def test_no_xnys_nasdaq_or_cfe_fallback_in_cboe_calendar() -> None:
    source = Path("app/research/capital_flow/fred_alfred_v2_availability.py").read_text(
        encoding="utf-8"
    )
    assert all(token not in source for token in ("XNYS", "exchange_calendars", "CFE"))
