from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta

import pytest

from app.research.capital_flow import (
    ALIGNED_CONTEXT_IDENTITY,
    BREADTH_SYMBOLS_V1,
    FEATURE_CATALOGUE_IDENTITY,
    FEATURE_CATALOGUE_V1,
    FEATURE_FAMILIES_V1,
    FEATURE_SIDECAR_IDENTITY,
    PROVIDER_AUTHORITIES_V1,
    PROVIDER_AUTHORITY_IDENTITY,
    SOURCE_SERIES_MANIFEST_IDENTITY,
    STALENESS_POLICIES_V1,
    SourceRecordV1,
    TemporalSemanticsV1,
    align_fixture_record,
)
from app.research.capital_flow.providers import validate_frozen_breadth_universe


def _record(
    *, usable_offset: int = 0, event_offset: int = 0, available: bool = True
) -> SourceRecordV1:
    base = datetime(2025, 1, 2, 12, tzinfo=UTC)
    return SourceRecordV1(
        PROVIDER_AUTHORITIES_V1[2],
        "QQQ",
        "QQQ",
        TemporalSemanticsV1(
            base + timedelta(minutes=event_offset),
            base,
            base,
            base + timedelta(minutes=usable_offset),
            base + timedelta(minutes=usable_offset),
        ),
        "a" * 64,
        available,
        None if available else "SOURCE_VALUE_UNAVAILABLE",
    )


def test_contracts_are_immutable_and_provider_changes_change_semantics() -> None:
    authority = PROVIDER_AUTHORITIES_V1[0]
    with pytest.raises(FrozenInstanceError):
        authority.provider_id = "OTHER"
    assert (
        authority.semantic_digest
        != replace(authority, feed_or_entitlement="changed").semantic_digest
    )


def test_contract_identities_and_provider_bindings_are_exact() -> None:
    assert {
        PROVIDER_AUTHORITY_IDENTITY,
        SOURCE_SERIES_MANIFEST_IDENTITY,
        ALIGNED_CONTEXT_IDENTITY,
        FEATURE_SIDECAR_IDENTITY,
        FEATURE_CATALOGUE_IDENTITY,
    } == {
        "chappie_capital_flow_provider_authority/1.0.0",
        "chappie_capital_flow_source_series_manifest/1.0.0",
        "chappie_capital_flow_aligned_context/1.0.0",
        "chappie_capital_flow_feature_sidecar/1.0.0",
        "chappie_capital_flow_feature_catalogue/1.0.0",
    }
    provider_ids = {authority.provider_id for authority in PROVIDER_AUTHORITIES_V1}
    assert provider_ids == {
        "COINMARKETCAP_GLOBAL_DAILY",
        "DEFILLAMA_STABLECOINS",
        "ALPACA_QQQ_SIP",
        "CBOE_VIX_DAILY",
        "FRED_ALFRED_DAILY",
        "BINANCE_SPOT_BREADTH",
    }


def test_temporal_contract_is_utc_and_future_publication_is_rejected() -> None:
    with pytest.raises(ValueError, match="UTC"):
        TemporalSemanticsV1(
            datetime.fromisoformat("2025-01-01T00:00:00"),
            None,
            None,
            datetime(2025, 1, 1, tzinfo=UTC),
            datetime(2025, 1, 1, tzinfo=UTC),
        )
    decision = align_fixture_record(
        _record(usable_offset=1),
        btc_close=datetime(2025, 1, 2, 12, tzinfo=UTC),
        policy=STALENESS_POLICIES_V1["QQQ_INTRADAY"],
    )
    assert decision.reason == "USABLE_AT_IN_FUTURE"


def test_fixture_alignment_rejects_stale_session_closed_missing_and_never_falls_back() -> None:
    close = datetime(2025, 1, 2, 13, tzinfo=UTC)
    assert (
        align_fixture_record(
            _record(), btc_close=close, policy=STALENESS_POLICIES_V1["QQQ_INTRADAY"]
        ).reason
        == "STALE_SOURCE_RECORD"
    )
    assert (
        align_fixture_record(
            _record(),
            btc_close=datetime(2025, 1, 2, 12, tzinfo=UTC),
            policy=STALENESS_POLICIES_V1["QQQ_INTRADAY"],
            regular_session_open=False,
        ).reason
        == "REGULAR_SESSION_CLOSED"
    )
    assert (
        align_fixture_record(
            None, btc_close=close, policy=STALENESS_POLICIES_V1["QQQ_INTRADAY"]
        ).reason
        == "SOURCE_RECORD_MISSING"
    )
    assert (
        align_fixture_record(
            _record(available=False), btc_close=close, policy=STALENESS_POLICIES_V1["QQQ_INTRADAY"]
        ).reason
        == "SOURCE_VALUE_UNAVAILABLE"
    )


def test_prior_session_is_available_within_its_own_policy() -> None:
    assert align_fixture_record(
        _record(),
        btc_close=datetime(2025, 1, 4, 11, tzinfo=UTC),
        policy=STALENESS_POLICIES_V1["QQQ_PRIOR_SESSION"],
    ).available


def test_frozen_catalogue_families_sources_and_breadth_membership() -> None:
    assert len(FEATURE_CATALOGUE_V1) == 20
    assert len(FEATURE_FAMILIES_V1) == 8
    assert len(BREADTH_SYMBOLS_V1) == 18 and len(set(BREADTH_SYMBOLS_V1)) == 18
    assert all(
        feature.source_authorities and feature.staleness_policy_id in STALENESS_POLICIES_V1
        for feature in FEATURE_CATALOGUE_V1
    )
    with pytest.raises(ValueError, match="frozen"):
        validate_frozen_breadth_universe(BREADTH_SYMBOLS_V1[:-1])


def test_metadata_records_frozen_provider_blockers_and_cmc_semantics() -> None:
    cmc = next(
        item for item in PROVIDER_AUTHORITIES_V1 if item.provider_id == "COINMARKETCAP_GLOBAL_DAILY"
    )
    assert cmc.metadata_status == "CMC_ENTITLEMENT_REQUIRES_MANUAL_CONFIRMATION"
    assert cmc.dominance_semantics == "PROVIDER_SUPPLIED_SAME_SNAPSHOT"
    assert "dominance" in next(
        item.formula_identity.lower()
        for item in FEATURE_CATALOGUE_V1
        if item.name == "crypto.btc_dominance_change_1d"
    )


def test_daily_vix_fred_stablecoin_and_binance_authorities_have_required_semantics() -> None:
    authorities = {item.provider_id: item for item in PROVIDER_AUTHORITIES_V1}
    assert authorities["DEFILLAMA_STABLECOINS"].request_parameters == (
        ("assets", "1:USDT,2:USDC,3:DAI"),
    )
    assert authorities["CBOE_VIX_DAILY"].native_frequency == "daily"
    assert (
        "realtime_start/realtime_end"
        in authorities["FRED_ALFRED_DAILY"].revision_or_vintage_semantics
    )
    assert (
        authorities["BINANCE_SPOT_BREADTH"].metadata_status
        == "BREADTH_HISTORY_VERIFICATION_PENDING"
    )
