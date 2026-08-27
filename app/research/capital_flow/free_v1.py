"""Versioned, free-first Global Risk & Capital Rotation V1 contracts only."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .catalogue import FEATURE_CATALOGUE_IDENTITY

FREE_FEATURE_CATALOGUE_IDENTITY = "chappie_global_risk_capital_rotation_free_catalogue/1.0.0"
FREE_AUTHORITY_IDENTITY = "chappie_global_risk_capital_rotation_free_authority/1.0.0"
OLD_CATALOGUE_STATUS = "SUPERSEDED_FOR_ACTIVE_RESEARCH"
ACTIVE_FREE_STATUS = "ACTIVE_FREE_V1"
DEFERRED_PAID_DATA = "DEFERRED_PAID_DATA"
DEFERRED_FREE_V2 = "DEFERRED_FREE_V2"

FREE_FAMILIES_V1 = (
    "CRYPTO_PARTICIPATION",
    "CRYPTO_ROTATION",
    "STABLECOIN_LIQUIDITY",
    "EQUITY_RISK",
    "VOLATILITY_FEAR",
    "USD_RATES_FX",
    "DEFENSIVE_STRESS",
    "COUPLING_REGIME",
)
FRED_SERIES_V1 = (
    "DEXJPUS",
    "GOLDAMGBD228NLBM",
    "NASDAQCOM",
    "DTWEXBGS",
    "DGS2",
    "DGS10",
    "STLFSI4",
)
DEFERRED_PAID_AUTHORITIES_V1 = ("COINMARKETCAP_GLOBAL_DAILY", "ALPACA_QQQ_SIP")
MARKET_PHYSICS_ROADMAP_V1 = (
    "Fibonacci vs matched controls",
    "Hurst exponent",
    "entropy",
    "fractal dimension",
    "multifractal behavior",
    "volatility clustering/complexity",
)


@dataclass(frozen=True, slots=True)
class CatalogueSupersessionV1:
    old_identity: str = FEATURE_CATALOGUE_IDENTITY
    old_status: str = OLD_CATALOGUE_STATUS
    new_identity: str = FREE_FEATURE_CATALOGUE_IDENTITY
    active_status: str = ACTIVE_FREE_STATUS


@dataclass(frozen=True, slots=True)
class FreeFeatureSpecV1:
    name: str
    family: str
    formula_identity: str
    authorities: tuple[str, ...]
    window: str
    lag: str
    units: str
    update_frequency: str
    staleness_policy: str

    def __post_init__(self) -> None:
        if self.family not in FREE_FAMILIES_V1 or not self.authorities:
            raise ValueError("invalid Free V1 feature authority binding")


def _feature(
    name: str,
    family: str,
    formula: str,
    authorities: tuple[str, ...],
    window: str,
    lag: str,
    units: str,
    update: str,
    staleness: str,
) -> FreeFeatureSpecV1:
    return FreeFeatureSpecV1(
        name, family, formula, authorities, window, lag, units, update, staleness
    )


FREE_FEATURE_CATALOGUE_V1 = (
    _feature(
        "breadth.crypto_positive_share_60m",
        "CRYPTO_PARTICIPATION",
        "MEAN_ALT_LOG_RETURN_60M_POSITIVE",
        ("BINANCE_SPOT_BREADTH",),
        "60m",
        "completed candles",
        "ratio",
        "1m",
        "<=2m",
    ),
    _feature(
        "breadth.crypto_alt_outperform_btc_share_60m",
        "CRYPTO_ROTATION",
        "MEAN_ALT_RETURN_60M_GREATER_THAN_BTC",
        ("BINANCE_SPOT_BREADTH", "BTC_AUTHORITY"),
        "60m",
        "completed candles",
        "ratio",
        "1m",
        "<=2m",
    ),
    _feature(
        "breadth.crypto_dispersion_60m",
        "CRYPTO_PARTICIPATION",
        "POPULATION_SD_ALT_RETURN_60M",
        ("BINANCE_SPOT_BREADTH",),
        "60m",
        "completed candles",
        "bps",
        "1m",
        "<=2m",
    ),
    _feature(
        "breadth.crypto_btc_quote_volume_share_60m",
        "CRYPTO_ROTATION",
        "BTC_QUOTE_VOLUME_OVER_BTC_PLUS_ALT_UNIVERSE_60M",
        ("BINANCE_SPOT_BREADTH", "BTC_AUTHORITY"),
        "60m",
        "completed candles",
        "ratio",
        "1m",
        "<=2m",
    ),
    _feature(
        "stablecoin.aggregate_market_cap_change_1d",
        "STABLECOIN_LIQUIDITY",
        "LOG_RETURN_USDT_USDC_DAI_MARKET_CAP_1D",
        ("DEFILLAMA_STABLECOINS",),
        "1d",
        "published daily",
        "bps",
        "daily",
        "<=48h",
    ),
    _feature(
        "stablecoin.aggregate_market_cap_change_7d",
        "STABLECOIN_LIQUIDITY",
        "LOG_RETURN_USDT_USDC_DAI_MARKET_CAP_7D",
        ("DEFILLAMA_STABLECOINS",),
        "7d",
        "published daily",
        "bps",
        "daily",
        "<=48h",
    ),
    _feature(
        "fx.usdjpy_return_5d",
        "USD_RATES_FX",
        "LOG_RETURN_DEXJPUS_5_OBSERVATIONS",
        ("FRED_ALFRED_FREE",),
        "5 observations",
        "vintage usable",
        "bps",
        "daily",
        "<=3_US_business_days",
    ),
    _feature(
        "defensive.gold_return_5d",
        "DEFENSIVE_STRESS",
        "LOG_RETURN_GOLDAMGBD228NLBM_5_OBSERVATIONS",
        ("FRED_ALFRED_FREE",),
        "5 observations",
        "vintage usable",
        "bps",
        "daily",
        "<=3_US_business_days",
    ),
    _feature(
        "equity.nasdaq_previous_session_return",
        "EQUITY_RISK",
        "LOG_RETURN_NASDAQCOM_PREVIOUS_COMPLETED_SESSION",
        ("FRED_ALFRED_FREE",),
        "1 session",
        "previous completed session",
        "bps",
        "daily",
        "<=3_US_business_days",
    ),
    _feature(
        "risk.vix_change_1d",
        "VOLATILITY_FEAR",
        "LOG_RETURN_CBOE_VIX_PREVIOUS_COMPLETED_SESSION",
        ("CBOE_VIX_DAILY",),
        "1d",
        "previous completed session",
        "bps",
        "daily",
        "<=3_US_business_days",
    ),
    _feature(
        "risk.vix_zscore_60sessions",
        "VOLATILITY_FEAR",
        "TRAILING_CBOE_VIX_ZSCORE_60_SESSIONS",
        ("CBOE_VIX_DAILY",),
        "60 sessions",
        "previous completed session",
        "zscore",
        "daily",
        "<=3_US_business_days",
    ),
    _feature(
        "usd.broad_index_return_5d",
        "USD_RATES_FX",
        "LOG_RETURN_DTWEXBGS_5_OBSERVATIONS",
        ("FRED_ALFRED_FREE",),
        "5 observations",
        "vintage usable",
        "bps",
        "daily",
        "<=3_US_business_days",
    ),
    _feature(
        "rates.us2y_change_5d",
        "USD_RATES_FX",
        "DGS2_DELTA_5_OBSERVATIONS",
        ("FRED_ALFRED_FREE",),
        "5 observations",
        "vintage usable",
        "bp",
        "daily",
        "<=3_US_business_days",
    ),
    _feature(
        "rates.us10y_change_5d",
        "USD_RATES_FX",
        "DGS10_DELTA_5_OBSERVATIONS",
        ("FRED_ALFRED_FREE",),
        "5 observations",
        "vintage usable",
        "bp",
        "daily",
        "<=3_US_business_days",
    ),
    _feature(
        "stress.stlfsi4_level",
        "DEFENSIVE_STRESS",
        "LATEST_USABLE_STLFSI4_LEVEL",
        ("FRED_ALFRED_FREE",),
        "latest weekly",
        "vintage usable",
        "index",
        "weekly",
        "<=9_calendar_days",
    ),
    _feature(
        "coupling.btc_nasdaq_correlation_20sessions",
        "COUPLING_REGIME",
        "PEARSON_SYNCHRONIZED_COMPLETED_BTC_NASDAQ_RETURNS_20_SESSIONS",
        ("BTC_AUTHORITY", "FRED_ALFRED_FREE"),
        "20 sessions",
        "previous completed session",
        "correlation",
        "daily",
        "complete window required",
    ),
    _feature(
        "coupling.btc_gold_correlation_60businessdays",
        "COUPLING_REGIME",
        "PEARSON_SYNCHRONIZED_COMPLETED_BTC_GOLD_RETURNS_60_BUSINESS_DAYS",
        ("BTC_AUTHORITY", "FRED_ALFRED_FREE"),
        "60 business days",
        "vintage usable",
        "correlation",
        "daily",
        "complete window required",
    ),
)

if (
    len(FREE_FEATURE_CATALOGUE_V1) != 17
    or len({item.name for item in FREE_FEATURE_CATALOGUE_V1}) != 17
):
    raise RuntimeError("Free V1 catalogue must contain exactly 17 unique features")


@dataclass(frozen=True, slots=True)
class FreeAuthoritySemanticsV1:
    breadth_denominator: str = "FROZEN_18_ALT_CONSTITUENTS_ONLY"
    btc_volume_denominator: str = "BTC_PLUS_FROZEN_18_ALT_CONSTITUENTS"
    stablecoin_assets: tuple[tuple[int, str], ...] = ((1, "USDT"), (2, "USDC"), (5, "DAI"))
    stablecoin_aggregation: str = "SUM_USD_MARKET_CAP_USDT_USDC_DAI_NO_EXCHANGE_FLOW_INFERENCE"
    usdjpy_orientation: str = "JPY_PER_USD_DEXJPUS_NOT_INVERTED"
    gold_authority: str = "GOLDAMGBD228NLBM_LBMA_AM_USD_FIX"
    nasdaq_semantics: str = "NASDAQCOM_PREVIOUS_COMPLETED_XNYS_SESSION_ONLY"
    vix_semantics: str = "CBOE_DAILY_CLOSE_PRIMARY_FRED_VIXCLS_VERIFICATION_ONLY"
    treasury_units: str = "DGS2_DGS10_CHANGE_IN_BASIS_POINTS"
    stress_semantics: str = "STLFSI4_LATEST_USABLE_UNTRANSFORMED_LEVEL"
    binance_before_2025_unit: str = "milliseconds"
    binance_from_2025_unit: str = "microseconds"
    canonical_timestamp_unit: str = "UTC_milliseconds"


@dataclass(frozen=True, slots=True)
class FreeResearchProtocolV1:
    feature_research: tuple[str, str] = ("2024-01-01", "2025-05-31")
    validation: tuple[str, str] = ("2025-06-01", "2026-05-31")
    future_blind_oos: tuple[str, str] = ("2026-09-01", "2027-02-28")
    revealed_research_note: str = "JAN_MAY_2025_REVEALED_NOT_BLIND"
    directional_objective: str = "DI=P(UP|state)-P(DOWN|state);INCREMENTAL_DI=DI_joint-DI_precursor"


@dataclass(frozen=True, slots=True)
class CouplingFixturePointV1:
    event_time: datetime
    usable_at: datetime


def validate_coupling_fixture_window(
    points: tuple[CouplingFixturePointV1, ...], *, target_time: datetime, required_count: int
) -> None:
    if target_time.tzinfo is None or target_time.utcoffset() != timedelta(0):
        raise ValueError("target_time must be UTC")
    if len(points) != required_count:
        raise ValueError("coupling window must be complete")
    for point in points:
        if point.event_time.tzinfo is None or point.usable_at.tzinfo is None:
            raise ValueError("coupling timestamps must be aware")
        if point.usable_at > target_time:
            raise ValueError("coupling window contains future observation")
        if point.event_time > target_time:
            raise ValueError("coupling window contains future event")


def normalize_binance_timestamp_to_ms(value: int, *, event_time: datetime) -> int:
    if event_time.tzinfo is None or event_time.utcoffset() != UTC.utcoffset(event_time):
        raise ValueError("event_time must be UTC")
    boundary = datetime(2025, 1, 1, tzinfo=UTC)
    divisor = 1_000 if event_time < boundary else 1_000_000
    if value <= 0 or value % divisor:
        raise ValueError("Binance timestamp does not match its frozen native unit")
    return value // divisor
