from dataclasses import FrozenInstanceError, asdict

import pytest

from app.research.capital_flow.fred_alfred_authority import (
    FRED_ALFRED_GLOBAL_RISK_AUTHORITY_IDENTITY,
    SERIES_SEMANTICS_V1,
)
from app.research.capital_flow.fred_alfred_v2 import (
    CONSERVATIVE_RELEASE_USABLE_AT_POLICY,
    DAILY_EXTERNAL_STALENESS_V2,
    DEFERRED_GOLD_FEATURES_V2,
    FRED_ALFRED_GLOBAL_RISK_AUTHORITY_V2_IDENTITY,
    FRED_CREDENTIAL_ENVIRONMENT_VARIABLE_V2,
    FRED_CREDENTIAL_EXCLUDED_FROM_V2,
    FRED_SERIES_SEMANTICS_V2,
    FRED_SERIES_V2,
    GOLD_STATUS_V2,
    MISSINGNESS_HANDLING_V2,
    MISSINGNESS_STATES_V2,
    NASDAQ_CALENDAR_IDENTITY_V2,
    NASDAQ_CALENDAR_IMPLEMENTATION_STATUS,
    NASDAQ_SESSION_SEMANTIC_V2,
    PIT_CLASSIFICATIONS_V2,
    PIT_FULL_VINTAGE_REQUIRED,
    PIT_RELEASE_VINTAGE_REQUIRED,
    PROSPECTIVE_BLIND_OOS_RANGE_V2,
    RESEARCH_RANGE_V2,
    SOURCE_RANGE_V2,
    STLFSI4_STALENESS_V2,
    VALIDATION_RANGE_V2,
    VIXCLS_AUTHORITY_STATUS_V2,
    VIXCLS_USAGE_RESTRICTIONS_V2,
    AuthoritySupersessionV2,
    NasdaqSessionContractV2,
    VixclsResearchAuthorityV2,
)
from app.research.capital_flow.free_v1 import (
    FREE_FEATURE_CATALOGUE_IDENTITY,
    FREE_FEATURE_CATALOGUE_V1,
)
from app.research.capital_flow.free_v2 import (
    ACTIVE_FREE_V2_STATUS,
    FREE_FAMILIES_V2,
    FREE_FAMILY_MEMBERSHIP_V2,
    FREE_FEATURE_CATALOGUE_V2,
    FREE_FEATURE_CATALOGUE_V2_IDENTITY,
    FREE_FEATURE_NAMES_V2,
    VIX_FEATURES_V2,
    CatalogueSupersessionV2,
)


def test_v1_identities_and_gold_definitions_remain_preserved_and_superseded() -> None:
    authority = AuthoritySupersessionV2()
    catalogue = CatalogueSupersessionV2()
    assert authority.v1_identity == FRED_ALFRED_GLOBAL_RISK_AUTHORITY_IDENTITY
    assert authority.v1_status == "UNFULFILLABLE_FOR_APPROVED_TARGET_RANGE"
    assert authority.v1_supersession == "SUPERSEDED_BY_2.0.0"
    assert "GOLDAMGBD228NLBM" in authority.reason
    assert catalogue.v1_identity == FREE_FEATURE_CATALOGUE_IDENTITY
    assert catalogue.v1_status == "HISTORICAL_REPRODUCIBLE"
    assert catalogue.v1_supersession == "SUPERSEDED_BY_2.0.0"
    assert SERIES_SEMANTICS_V1["GOLDAMGBD228NLBM"] == "LBMA_AM_USD_FIX"
    old_gold = {
        feature.name: feature for feature in FREE_FEATURE_CATALOGUE_V1 if "gold" in feature.name
    }
    assert tuple(old_gold) == DEFERRED_GOLD_FEATURES_V2
    assert old_gold["defensive.gold_return_5d"].formula_identity == (
        "LOG_RETURN_GOLDAMGBD228NLBM_5_OBSERVATIONS"
    )


def test_v2_identities_exact_series_order_pit_and_gold_deferral_are_frozen() -> None:
    assert FRED_ALFRED_GLOBAL_RISK_AUTHORITY_V2_IDENTITY.endswith("/2.0.0")
    assert FREE_FEATURE_CATALOGUE_V2_IDENTITY.endswith("/2.0.0")
    assert ACTIVE_FREE_V2_STATUS == "ACTIVE_FREE_RESEARCH_CATALOGUE"
    assert FRED_SERIES_V2 == (
        "DEXJPUS",
        "NASDAQCOM",
        "DTWEXBGS",
        "DGS2",
        "DGS10",
        "STLFSI4",
        "VIXCLS",
    )
    assert "GOLDAMGBD228NLBM" not in FRED_SERIES_V2
    assert dict(PIT_CLASSIFICATIONS_V2) == {
        "DEXJPUS": PIT_RELEASE_VINTAGE_REQUIRED,
        "NASDAQCOM": PIT_RELEASE_VINTAGE_REQUIRED,
        "DTWEXBGS": PIT_FULL_VINTAGE_REQUIRED,
        "DGS2": PIT_RELEASE_VINTAGE_REQUIRED,
        "DGS10": PIT_RELEASE_VINTAGE_REQUIRED,
        "STLFSI4": PIT_FULL_VINTAGE_REQUIRED,
        "VIXCLS": PIT_RELEASE_VINTAGE_REQUIRED,
    }
    assert GOLD_STATUS_V2 == "GOLD_DEFERRED_NO_DEFENSIBLE_FREE_AUTHORITY"
    assert DEFERRED_GOLD_FEATURES_V2 == (
        "defensive.gold_return_5d",
        "coupling.btc_gold_correlation_60businessdays",
    )


def test_v2_catalogue_has_exact_order_and_family_membership() -> None:
    expected = tuple(
        feature.name
        for feature in FREE_FEATURE_CATALOGUE_V1
        if feature.name not in DEFERRED_GOLD_FEATURES_V2
    )
    assert len(FREE_FEATURE_CATALOGUE_V2) == 15
    assert FREE_FEATURE_NAMES_V2 == expected
    assert len(FREE_FAMILIES_V2) == 8
    assert dict(FREE_FAMILY_MEMBERSHIP_V2) == {
        family: tuple(
            feature.name for feature in FREE_FEATURE_CATALOGUE_V2 if feature.family == family
        )
        for family in FREE_FAMILIES_V2
    }
    assert not set(DEFERRED_GOLD_FEATURES_V2) & set(FREE_FEATURE_NAMES_V2)


def test_vixcls_is_internal_research_authority_without_changing_vix_formulas() -> None:
    contract = VixclsResearchAuthorityV2()
    assert contract.status == VIXCLS_AUTHORITY_STATUS_V2 == "FREE_RESEARCH_AUTHORITY"
    assert (contract.provider, contract.underlying_source, contract.series, contract.release) == (
        "FRED/ALFRED",
        "Chicago Board Options Exchange",
        "VIXCLS",
        "CBOE Market Statistics",
    )
    assert contract.semantic == "DAILY_VIX_CLOSE_INDEX_POINTS"
    assert contract.usage_restrictions == VIXCLS_USAGE_RESTRICTIONS_V2
    assert contract.pit_classification == PIT_RELEASE_VINTAGE_REQUIRED
    assert contract.usable_at_policy == CONSERVATIVE_RELEASE_USABLE_AT_POLICY
    v1 = {feature.name: feature for feature in FREE_FEATURE_CATALOGUE_V1}
    v2 = {feature.name: feature for feature in FREE_FEATURE_CATALOGUE_V2}
    assert all(name in v2 for name in VIX_FEATURES_V2)
    for name in VIX_FEATURES_V2:
        assert v2[name].formula_identity == v1[name].formula_identity
        assert v2[name].window == v1[name].window
        assert v2[name].units == v1[name].units
        assert v2[name].authorities == ("FRED_ALFRED_V2_VIXCLS",)


def test_nasdaq_contract_is_specific_pending_immutable_and_causal() -> None:
    contract = NasdaqSessionContractV2()
    assert contract.identity == NASDAQ_CALENDAR_IDENTITY_V2
    assert contract.implementation_status == NASDAQ_CALENDAR_IMPLEMENTATION_STATUS
    assert NASDAQ_SESSION_SEMANTIC_V2 == (
        "NASDAQCOM_PREVIOUS_COMPLETED_NASDAQ_REGULAR_SESSION_ONLY"
    )
    assert "XNYS" not in NASDAQ_SESSION_SEMANTIC_V2
    assert (contract.timezone, contract.regular_open, contract.regular_close) == (
        "America/New_York",
        "09:30",
        "16:00",
    )
    assert contract.early_close == "OFFICIAL_NASDAQ_PUBLISHED_CLOSE"
    assert contract.holidays_non_sessions == "OFFICIAL_NASDAQ_CALENDAR"
    assert contract.dst == "IANA_AMERICA_NEW_YORK"
    assert contract.reference_date == "NASDAQ_TRADE_SESSION_DATE"
    assert contract.pit_classification == PIT_RELEASE_VINTAGE_REQUIRED
    assert contract.usable_at_policy == CONSERVATIVE_RELEASE_USABLE_AT_POLICY
    with pytest.raises(FrozenInstanceError):
        contract.regular_close = "15:59"  # type: ignore[misc]


def test_other_semantics_ranges_staleness_missingness_and_credential_policy() -> None:
    semantics = dict(FRED_SERIES_SEMANTICS_V2)
    assert semantics["DEXJPUS"] == "JPY_PER_USD_NEVER_INVERTED"
    assert semantics["DTWEXBGS"] == "BROAD_TRADE_WEIGHTED_US_DOLLAR_INDEX"
    assert semantics["DGS2"].endswith("PERCENT")
    assert semantics["DGS10"].endswith("PERCENT")
    assert semantics["STLFSI4"] == "WEEKLY_ST_LOUIS_FED_FINANCIAL_STRESS_INDEX"
    assert SOURCE_RANGE_V2 == ("2023-09-01", "2026-05-31")
    assert RESEARCH_RANGE_V2 == ("2024-01-01", "2025-05-31")
    assert VALIDATION_RANGE_V2 == ("2025-06-01", "2026-05-31")
    assert PROSPECTIVE_BLIND_OOS_RANGE_V2 == ("2026-09-01", "2027-02-28")
    assert DAILY_EXTERNAL_STALENESS_V2 == "<=3_US_BUSINESS_DAYS"
    assert STLFSI4_STALENESS_V2 == "<=9_CALENDAR_DAYS"
    assert MISSINGNESS_STATES_V2 == (
        "VALID_SOURCE_ABSENCE",
        "ACQUISITION_GAP",
        "MALFORMED_SOURCE_RECORD",
    )
    assert MISSINGNESS_HANDLING_V2 == (
        "NO_ZERO_SUBSTITUTION",
        "NO_INTERPOLATION",
        "NO_UNLIMITED_FORWARD_FILL",
    )
    assert FRED_CREDENTIAL_ENVIRONMENT_VARIABLE_V2 == "FRED_API_KEY"
    assert FRED_CREDENTIAL_EXCLUDED_FROM_V2 == (
        "DTO_IDENTITY",
        "DIGEST",
        "CACHE_IDENTITY",
        "MANIFEST",
        "LOGS",
        "SERIALIZED_REQUEST_PLAN",
    )
    assert "FRED_API_KEY" not in repr(asdict(AuthoritySupersessionV2()))


def test_contract_modules_have_no_http_or_acquisition_surface() -> None:
    import app.research.capital_flow.fred_alfred_v2 as authority_module
    import app.research.capital_flow.free_v2 as catalogue_module

    public = set(dir(catalogue_module)) | set(dir(authority_module))
    assert not {"httpx", "requests", "acquire", "download", "publish"} & public
