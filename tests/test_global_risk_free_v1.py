from datetime import UTC, datetime

import pytest

from app.research.capital_flow.catalogue import FEATURE_CATALOGUE_IDENTITY, FEATURE_CATALOGUE_V1
from app.research.capital_flow.free_v1 import (
    ACTIVE_FREE_STATUS,
    DEFERRED_FREE_V2,
    DEFERRED_PAID_AUTHORITIES_V1,
    DEFERRED_PAID_DATA,
    FRED_SERIES_V1,
    FREE_FAMILIES_V1,
    FREE_FEATURE_CATALOGUE_IDENTITY,
    FREE_FEATURE_CATALOGUE_V1,
    MARKET_PHYSICS_ROADMAP_V1,
    CatalogueSupersessionV1,
    CouplingFixturePointV1,
    FreeAuthoritySemanticsV1,
    FreeResearchProtocolV1,
    normalize_binance_timestamp_to_ms,
    validate_coupling_fixture_window,
)
from app.research.capital_flow.providers import BREADTH_SYMBOLS_V1


def test_paid_catalogue_is_preserved_and_explicitly_superseded() -> None:
    supersession = CatalogueSupersessionV1()
    assert len(FEATURE_CATALOGUE_V1) == 20
    assert supersession.old_identity == FEATURE_CATALOGUE_IDENTITY
    assert supersession.old_status == "SUPERSEDED_FOR_ACTIVE_RESEARCH"
    assert supersession.new_identity == FREE_FEATURE_CATALOGUE_IDENTITY
    assert supersession.active_status == ACTIVE_FREE_STATUS


def test_free_catalogue_is_exactly_seventeen_features_and_eight_families_without_paid_data() -> (
    None
):
    assert len(FREE_FEATURE_CATALOGUE_V1) == 17
    assert len({item.name for item in FREE_FEATURE_CATALOGUE_V1}) == 17
    assert len(FREE_FAMILIES_V1) == 8
    assert set(DEFERRED_PAID_AUTHORITIES_V1).isdisjoint(
        {authority for item in FREE_FEATURE_CATALOGUE_V1 for authority in item.authorities}
    )
    assert DEFERRED_PAID_DATA == "DEFERRED_PAID_DATA"
    assert DEFERRED_FREE_V2 == "DEFERRED_FREE_V2"


def test_free_authority_semantics_freeze_denominators_assets_and_series() -> None:
    semantics = FreeAuthoritySemanticsV1()
    assert len(BREADTH_SYMBOLS_V1) == 18
    assert semantics.breadth_denominator == "FROZEN_18_ALT_CONSTITUENTS_ONLY"
    assert semantics.btc_volume_denominator == "BTC_PLUS_FROZEN_18_ALT_CONSTITUENTS"
    assert semantics.stablecoin_assets == ((1, "USDT"), (2, "USDC"), (5, "DAI"))
    assert semantics.usdjpy_orientation == "JPY_PER_USD_DEXJPUS_NOT_INVERTED"
    assert FRED_SERIES_V1 == (
        "DEXJPUS",
        "GOLDAMGBD228NLBM",
        "NASDAQCOM",
        "DTWEXBGS",
        "DGS2",
        "DGS10",
        "STLFSI4",
    )


def test_binance_units_and_coupling_fixtures_fail_closed() -> None:
    before = datetime(2024, 12, 31, tzinfo=UTC)
    after = datetime(2025, 1, 1, tzinfo=UTC)
    assert (
        normalize_binance_timestamp_to_ms(1_735_603_200_000, event_time=before) == 1_735_603_200_000
    )
    assert (
        normalize_binance_timestamp_to_ms(1_735_603_200_000_000, event_time=after)
        == 1_735_603_200_000
    )
    target = datetime(2025, 2, 1, tzinfo=UTC)
    point = CouplingFixturePointV1(target, target)
    validate_coupling_fixture_window((point, point), target_time=target, required_count=2)
    with pytest.raises(ValueError, match="complete"):
        validate_coupling_fixture_window((point,), target_time=target, required_count=2)
    with pytest.raises(ValueError, match="future"):
        validate_coupling_fixture_window(
            (CouplingFixturePointV1(target, datetime(2025, 2, 2, tzinfo=UTC)),),
            target_time=target,
            required_count=1,
        )


def test_protocol_and_market_physics_are_metadata_only() -> None:
    protocol = FreeResearchProtocolV1()
    assert protocol.feature_research == ("2024-01-01", "2025-05-31")
    assert protocol.validation == ("2025-06-01", "2026-05-31")
    assert protocol.future_blind_oos == ("2026-09-01", "2027-02-28")
    assert "Fibonacci vs matched controls" in MARKET_PHYSICS_ROADMAP_V1
