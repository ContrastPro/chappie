from datetime import UTC, datetime, timedelta

import pytest
from exchange_calendars.errors import NotSessionError

from app.research.capital_flow.calendar import (
    NASDAQ_COMPLETION_USABLE_RULE,
    XNYS_CALENDAR_ID,
    XNYS_TIMEZONE,
    completed_session_available_at,
    is_regular_session,
    previous_completed_session,
    regular_session_close,
    regular_session_open,
    session_for_timestamp,
)
from app.research.capital_flow.free_source_validation import (
    BINANCE_CONTRACT_V1,
    BTC_DAILY_ALIGNMENT_RULE,
    CBOE_VIX_CONTRACT_V1,
    DEFILLAMA_CONTRACT_V1,
    FRED_ALFRED_CONTRACTS_V1,
    INCOMPLETE_COUPLING_WINDOW,
    INVALID_VINTAGE,
    PUBLICATION_NOT_YET_USABLE,
    SOURCE_RECORD_MISSING,
    STALE_SOURCE_RECORD,
    USABLE_AT_IN_FUTURE,
    CouplingObservationFixtureV1,
    DailySourceFixtureV1,
    validate_coupling_observation_window,
)
from app.research.capital_flow.free_v1 import (
    FREE_FEATURE_CATALOGUE_IDENTITY,
    FREE_FEATURE_CATALOGUE_V1,
)


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def test_xnys_calendar_authority_handles_sessions_holidays_early_closes_and_dst() -> None:
    normal = utc(2025, 6, 3)
    early = utc(2025, 7, 3)
    assert (XNYS_CALENDAR_ID, XNYS_TIMEZONE) == ("XNYS", "America/New_York")
    assert regular_session_open(normal) == utc(2025, 6, 3, 13, 30)
    assert regular_session_close(normal) == utc(2025, 6, 3, 20)
    assert regular_session_close(early) == utc(2025, 7, 3, 17)
    assert regular_session_open(utc(2025, 3, 10)) == utc(2025, 3, 10, 13, 30)
    assert not is_regular_session(utc(2025, 6, 7, 15))  # Saturday
    assert not is_regular_session(utc(2025, 6, 8, 15))  # Sunday
    assert not is_regular_session(utc(2025, 7, 4, 15))  # Independence Day


def test_session_membership_and_previous_completed_session_are_causal() -> None:
    june_2 = utc(2025, 6, 2)
    june_3 = utc(2025, 6, 3)
    assert session_for_timestamp(utc(2025, 6, 3, 13, 29)) is None
    assert session_for_timestamp(utc(2025, 6, 3, 13, 30)) == june_3
    assert session_for_timestamp(utc(2025, 6, 3, 16)) == june_3
    assert session_for_timestamp(utc(2025, 6, 3, 20)) == june_3
    assert session_for_timestamp(utc(2025, 6, 3, 20, 1)) is None
    assert previous_completed_session(utc(2025, 6, 3, 13, 29)) == june_2
    assert previous_completed_session(utc(2025, 6, 3, 16)) == june_2
    assert previous_completed_session(utc(2025, 6, 3, 20)) == june_2
    assert previous_completed_session(utc(2025, 6, 3, 20, 59)) == june_2
    assert previous_completed_session(utc(2025, 6, 3, 21)) == june_3
    assert previous_completed_session(utc(2025, 7, 4, 15)) == utc(2025, 7, 3)
    assert previous_completed_session(utc(2025, 7, 6, 15)) == utc(2025, 7, 3)


def test_early_close_uses_actual_close_and_conservative_publication_boundary() -> None:
    early = utc(2025, 7, 3)
    assert completed_session_available_at(early) == utc(2025, 7, 3, 18)
    assert previous_completed_session(utc(2025, 7, 3, 17)) == utc(2025, 7, 2)
    assert previous_completed_session(utc(2025, 7, 3, 18)) == early
    assert NASDAQ_COMPLETION_USABLE_RULE == "ACTUAL_XNYS_CLOSE_PLUS_FIXED_60_MINUTES"


def test_calendar_rejects_naive_timestamps_and_non_sessions() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        session_for_timestamp(datetime(2025, 6, 3, 13, 30))  # noqa: DTZ001
    with pytest.raises(NotSessionError):
        regular_session_open(utc(2025, 7, 4))


def test_source_contracts_are_frozen_and_vintages_fail_closed() -> None:
    assert len(FRED_ALFRED_CONTRACTS_V1) == 7
    assert CBOE_VIX_CONTRACT_V1[0:2] == (
        "CBOE_DAILY_VIX_CLOSE_PRIMARY",
        "FRED_VIXCLS_VERIFICATION_ONLY",
    )
    assert DEFILLAMA_CONTRACT_V1 == ((1, "USDT"), (2, "USDC"), (5, "DAI"))
    assert "BTCUSDT" in BINANCE_CONTRACT_V1[0]
    assert "UTC_DAILY_BOUNDARY" in BINANCE_CONTRACT_V1
    assert "SAME_DATE" in BTC_DAILY_ALIGNMENT_RULE
    fixture = DailySourceFixtureV1(
        "FRED_ALFRED_DAILY",
        "2025-06-03",
        utc(2025, 6, 3),
        utc(2025, 6, 4),
        utc(2025, 6, 6),
        utc(2025, 6, 4),
        utc(2025, 6, 4),
        timedelta(days=3),
    )
    assert fixture.unavailable_reason_at(utc(2025, 6, 3, 23)) == PUBLICATION_NOT_YET_USABLE
    assert fixture.unavailable_reason_at(utc(2025, 6, 5)) is None
    assert fixture.unavailable_reason_at(utc(2025, 6, 6)) == INVALID_VINTAGE
    stale = DailySourceFixtureV1(
        "DEFILLAMA_STABLECOINS",
        "2025-06-01",
        utc(2025, 6, 1),
        utc(2025, 6, 1),
        None,
        utc(2025, 6, 1),
        utc(2025, 6, 1),
        timedelta(hours=48),
    )
    assert stale.unavailable_reason_at(utc(2025, 6, 4)) == STALE_SOURCE_RECORD


def test_coupling_windows_require_complete_causal_nonstale_btc_and_external_data() -> None:
    target = utc(2025, 6, 4)
    point = CouplingObservationFixtureV1(True, True, True, target, target)
    assert (
        validate_coupling_observation_window((point, point), target_time=target, required_count=2)
        is None
    )
    assert (
        validate_coupling_observation_window((point,), target_time=target, required_count=2)
        == INCOMPLETE_COUPLING_WINDOW
    )
    assert (
        validate_coupling_observation_window(
            (CouplingObservationFixtureV1(False, True, True, target, target),),
            target_time=target,
            required_count=1,
        )
        == SOURCE_RECORD_MISSING
    )
    assert (
        validate_coupling_observation_window(
            (CouplingObservationFixtureV1(True, True, True, utc(2025, 6, 5), target),),
            target_time=target,
            required_count=1,
        )
        == USABLE_AT_IN_FUTURE
    )


def test_free_catalogue_remains_unchanged() -> None:
    assert (
        FREE_FEATURE_CATALOGUE_IDENTITY
        == "chappie_global_risk_capital_rotation_free_catalogue/1.0.0"
    )
    assert len(FREE_FEATURE_CATALOGUE_V1) == 17
