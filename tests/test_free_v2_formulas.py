from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext

import pytest

from app.research.capital_flow.free_v2 import (
    FREE_FAMILIES_V2,
    FREE_FEATURE_NAMES_V2,
)
from app.research.capital_flow.free_v2_formulas import (
    ALT_SYMBOLS,
    CAPITAL_FLOW_FREE_V2_FEATURE_FORMULAS_IDENTITY,
    CRYPTO_SYMBOLS,
    FEATURE_TIMELINE_END_EXCLUSIVE,
    FEATURE_TIMELINE_MINUTES,
    FEATURE_TIMELINE_START,
    FEATURE_UNAVAILABLE_STATES,
    FREE_V2_FEATURE_FORMULA_CONTRACTS,
    INSUFFICIENT_HISTORY,
    MISSING_REQUIRED_SYMBOL,
    NASDAQ_V1_CONFLICT_RESOLUTION,
    NASDAQ_V2_SEMANTIC,
    NATURAL_LOG_RETURN_IDENTITY,
    SOURCE_UNAVAILABLE,
    SOURCE_VALUE_INVALID,
    WINDOW_INCOMPLETE,
    ZERO_VARIANCE,
    CausalDailyValueV1,
    FeatureFormulaUnavailable,
    btc_quote_volume_share,
    btc_session_candle_open,
    crypto_60m_return,
    crypto_alt_outperform_share,
    crypto_population_dispersion,
    crypto_positive_share,
    five_observation_log_return,
    nasdaq_previous_session_return,
    natural_log_return,
    pearson_population,
    population_standard_deviation,
    quote_volume_60m,
    select_stablecoin_common_pair,
    stablecoin_log_change,
    treasury_change_bp,
    validate_feature_timestamp,
    vix_point_change,
    vix_zscore_60,
)


def _reason(error: pytest.ExceptionInfo[FeatureFormulaUnavailable]) -> str:
    return error.value.reason


def test_identity_exact_catalogue_order_families_and_immutability() -> None:
    assert CAPITAL_FLOW_FREE_V2_FEATURE_FORMULAS_IDENTITY == (
        "CAPITAL_FLOW_FREE_V2_FEATURE_FORMULAS/1.0.0"
    )
    assert NATURAL_LOG_RETURN_IDENTITY == "NATURAL_LOG_RETURN/1.0.0"
    assert tuple(item.name for item in FREE_V2_FEATURE_FORMULA_CONTRACTS) == FREE_FEATURE_NAMES_V2
    assert len(FREE_V2_FEATURE_FORMULA_CONTRACTS) == 15
    assert {item.family for item in FREE_V2_FEATURE_FORMULA_CONTRACTS} == set(FREE_FAMILIES_V2)
    assert len({item.formula_identity for item in FREE_V2_FEATURE_FORMULA_CONTRACTS}) == 15
    with pytest.raises(FrozenInstanceError):
        FREE_V2_FEATURE_FORMULA_CONTRACTS[0].unit = "changed"  # type: ignore[misc]


def test_natural_log_return_is_unscaled_base_e_and_rejects_invalid_source() -> None:
    with localcontext() as context:
        context.prec = 50
        e = Decimal(1).exp()
    assert natural_log_return(Decimal(1), e) == Decimal(1)
    with pytest.raises(FeatureFormulaUnavailable) as caught:
        natural_log_return(Decimal(0), Decimal(1))
    assert _reason(caught) == SOURCE_VALUE_INVALID


def test_exact_60m_endpoints_continuity_and_no_future_candle() -> None:
    timestamp = datetime(2024, 1, 1, 1, tzinfo=UTC)
    closes = {
        timestamp - timedelta(minutes=offset): Decimal(121 - offset) for offset in range(60, -1, -1)
    }
    assert crypto_60m_return(closes, timestamp) == natural_log_return(Decimal(61), Decimal(121))
    missing = dict(closes)
    missing.pop(timestamp - timedelta(minutes=30))
    with pytest.raises(FeatureFormulaUnavailable) as caught:
        crypto_60m_return(missing, timestamp)
    assert _reason(caught) == WINDOW_INCOMPLETE
    future = dict(closes)
    future[timestamp + timedelta(minutes=1)] = future.pop(timestamp)
    with pytest.raises(FeatureFormulaUnavailable):
        crypto_60m_return(future, timestamp)


def test_exact_60_candle_quote_volume_window() -> None:
    timestamp = datetime(2024, 1, 1, 1, tzinfo=UTC)
    volumes = {timestamp - timedelta(minutes=offset): Decimal(1) for offset in range(59, -1, -1)}
    assert quote_volume_60m(volumes, timestamp) == Decimal(60)
    volumes[timestamp - timedelta(minutes=60)] = Decimal(1)
    with pytest.raises(FeatureFormulaUnavailable) as caught:
        quote_volume_60m(volumes, timestamp)
    assert _reason(caught) == WINDOW_INCOMPLETE


def test_fixed_19_and_18_denominators_ties_and_population_dispersion() -> None:
    returns = {symbol: Decimal(0) for symbol in CRYPTO_SYMBOLS}
    returns["BTCUSDT"] = Decimal(1)
    assert (len(CRYPTO_SYMBOLS), len(ALT_SYMBOLS)) == (19, 18)
    with localcontext() as context:
        context.prec = 50
        one_nineteenth = Decimal(1) / Decimal(19)
        sqrt_thirty = Decimal(30).sqrt()
    assert crypto_positive_share(returns) == one_nineteenth
    assert crypto_alt_outperform_share(returns) == 0
    indexed = {symbol: Decimal(index) for index, symbol in enumerate(CRYPTO_SYMBOLS)}
    assert crypto_population_dispersion(indexed) == sqrt_thirty
    missing = dict(returns)
    missing.pop(ALT_SYMBOLS[0])
    with pytest.raises(FeatureFormulaUnavailable) as caught:
        crypto_positive_share(missing)
    assert _reason(caught) == MISSING_REQUIRED_SYMBOL


def test_btc_quote_volume_share_uses_fixed_positive_total() -> None:
    volumes = {symbol: Decimal(1) for symbol in CRYPTO_SYMBOLS}
    with localcontext() as context:
        context.prec = 50
        expected = Decimal(1) / Decimal(19)
    assert btc_quote_volume_share(volumes) == expected
    with pytest.raises(FeatureFormulaUnavailable) as caught:
        btc_quote_volume_share({symbol: Decimal(0) for symbol in CRYPTO_SYMBOLS})
    assert _reason(caught) == SOURCE_VALUE_INVALID


def _daily(day: int, value: str, usable_day: int | None = None, *, stale: bool = False):
    return CausalDailyValueV1(
        date(2024, 1, day),
        Decimal(value),
        datetime(2024, 1, usable_day or day + 1, tzinfo=UTC),
        stale,
    )


def test_stablecoin_latest_common_date_exact_lag_and_t_plus_24_gating() -> None:
    observations = {
        symbol: (_daily(1, "10"), _daily(2, "20"), _daily(3, "30", usable_day=4))
        for symbol in ("USDT", "USDC", "DAI")
    }
    selected, current, prior = select_stablecoin_common_pair(
        observations, datetime(2024, 1, 3, tzinfo=UTC), lag_days=1
    )
    assert selected == date(2024, 1, 2)
    assert stablecoin_log_change(current, prior) == natural_log_return(Decimal(30), Decimal(60))
    selected, _, prior = select_stablecoin_common_pair(
        observations, datetime(2024, 1, 4, tzinfo=UTC), lag_days=1
    )
    assert selected == date(2024, 1, 3) and set(prior) == {"USDT", "USDC", "DAI"}


def test_stablecoin_never_mixes_dates_or_substitutes_missing_lag() -> None:
    observations = {
        "USDT": (_daily(1, "1"), _daily(2, "2")),
        "USDC": (_daily(2, "2"),),
        "DAI": (_daily(2, "2"),),
    }
    with pytest.raises(FeatureFormulaUnavailable) as caught:
        select_stablecoin_common_pair(observations, datetime(2024, 1, 3, tzinfo=UTC), lag_days=1)
    assert _reason(caught) == WINDOW_INCOMPLETE
    with pytest.raises(FeatureFormulaUnavailable) as caught:
        select_stablecoin_common_pair(
            {symbol: () for symbol in ("USDT", "USDC", "DAI")},
            datetime(2024, 1, 3, tzinfo=UTC),
            lag_days=7,
        )
    assert _reason(caught) == SOURCE_UNAVAILABLE


def test_fifth_prior_means_six_valid_observations_and_treasuries_convert_to_bp() -> None:
    values = tuple(Decimal(value) for value in (1, 2, 3, 4, 5, 6))
    assert five_observation_log_return(values) == natural_log_return(Decimal(1), Decimal(6))
    assert treasury_change_bp(values) == Decimal(500)
    with pytest.raises(FeatureFormulaUnavailable) as caught:
        five_observation_log_return(values[1:])
    assert _reason(caught) == INSUFFICIENT_HISTORY


def test_nasdaq_v2_semantic_return_and_legacy_conflict_resolution() -> None:
    assert NASDAQ_V2_SEMANTIC == "NASDAQCOM_PREVIOUS_COMPLETED_NASDAQ_REGULAR_SESSION_ONLY"
    assert NASDAQ_V1_CONFLICT_RESOLUTION == "V2_SEMANTIC_SUPERSEDES_V1_FOR_ACTIVE_FREE_V2"
    assert "XNYS" not in NASDAQ_V2_SEMANTIC
    assert nasdaq_previous_session_return(Decimal(100), Decimal(110)) == natural_log_return(
        Decimal(100), Decimal(110)
    )


def test_vix_point_change_and_exact_population_zscore() -> None:
    assert vix_point_change(Decimal("15.5"), Decimal("17.0")) == Decimal("1.5")
    values = tuple(Decimal(value) for value in range(1, 61))
    with localcontext() as context:
        context.prec = 50
        expected = (Decimal(60) - Decimal("30.5")) / population_standard_deviation(values)
    assert vix_zscore_60(values) == expected
    with pytest.raises(FeatureFormulaUnavailable) as caught:
        vix_zscore_60((Decimal(1),) * 60)
    assert _reason(caught) == ZERO_VARIANCE


def test_btc_session_close_alignment_and_population_pearson_20_pairs() -> None:
    close = datetime(2024, 7, 3, 17, tzinfo=UTC)
    assert btc_session_candle_open(close) == datetime(2024, 7, 3, 16, 59, tzinfo=UTC)
    left = tuple(Decimal(value) for value in range(1, 21))
    right = tuple(value * Decimal(2) for value in left)
    assert pearson_population(left, right) == Decimal(1)
    with pytest.raises(FeatureFormulaUnavailable) as caught:
        pearson_population(left, (Decimal(1),) * 20)
    assert _reason(caught) == ZERO_VARIANCE


def test_exact_non_blind_minute_grid_and_unavailable_state_identities() -> None:
    assert FEATURE_TIMELINE_MINUTES == 1_270_080
    assert validate_feature_timestamp(FEATURE_TIMELINE_START) == FEATURE_TIMELINE_START
    assert validate_feature_timestamp(FEATURE_TIMELINE_END_EXCLUSIVE - timedelta(minutes=1))
    with pytest.raises(ValueError, match="non-blind"):
        validate_feature_timestamp(FEATURE_TIMELINE_END_EXCLUSIVE)
    assert FEATURE_UNAVAILABLE_STATES == (
        "INSUFFICIENT_HISTORY",
        "SOURCE_UNAVAILABLE",
        "SOURCE_STALE",
        "INVALID_SOURCE_SESSION",
        "MISSING_REQUIRED_SYMBOL",
        "WINDOW_INCOMPLETE",
        "SOURCE_VALUE_INVALID",
        "ZERO_VARIANCE",
    )


def test_all_formula_contracts_bind_units_windows_causality_and_no_gold() -> None:
    assert all(
        item.formula and item.window and item.unit and item.causal_semantics
        for item in FREE_V2_FEATURE_FORMULA_CONTRACTS
    )
    assert all("gold" not in item.name for item in FREE_V2_FEATURE_FORMULA_CONTRACTS)
    by_name = {item.name: item for item in FREE_V2_FEATURE_FORMULA_CONTRACTS}
    assert by_name["risk.vix_change_1d"].unit == "VIX_index_points"
    assert by_name["rates.us2y_change_5d"].unit == "basis_points"
    assert by_name["stress.stlfsi4_level"].unit == "STLFSI4_index_level"
    assert "DDOF_0" in by_name["risk.vix_zscore_60sessions"].formula
    assert "LATEST_21" in by_name["coupling.btc_nasdaq_correlation_20sessions"].window
