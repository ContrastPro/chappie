"""Frozen semantic catalogue for the approved 20-feature reduced-scope V1."""

from __future__ import annotations

from dataclasses import dataclass

FEATURE_CATALOGUE_IDENTITY = "chappie_capital_flow_feature_catalogue/1.0.0"
FEATURE_FAMILIES_V1 = (
    "CRYPTO_CAPITAL_EXPANSION",
    "CRYPTO_ROTATION",
    "STABLECOIN_LIQUIDITY",
    "EQUITY_RISK_APPETITE",
    "VOLATILITY_FEAR",
    "USD_RATES",
    "CROSS_ASSET_LEAD_LAG",
    "MARKET_BREADTH",
)


@dataclass(frozen=True, slots=True)
class FeatureSpecV1:
    name: str
    family: str
    formula_identity: str
    source_authorities: tuple[str, ...]
    window: str
    lag: str
    units: str
    update_frequency: str
    causal_availability_rule: str
    staleness_policy_id: str

    def __post_init__(self) -> None:
        if self.family not in FEATURE_FAMILIES_V1 or not self.name or not self.source_authorities:
            raise ValueError("invalid Capital Flow feature binding")


def _feature(
    name: str,
    family: str,
    formula: str,
    sources: tuple[str, ...],
    window: str,
    lag: str,
    units: str,
    update: str,
    stale: str,
) -> FeatureSpecV1:
    return FeatureSpecV1(
        name,
        family,
        formula,
        sources,
        window,
        lag,
        units,
        update,
        "all source usable_at <= BTC close",
        stale,
    )


FEATURE_CATALOGUE_V1 = (
    _feature(
        "crypto.total_market_cap_return_1d",
        "CRYPTO_CAPITAL_EXPANSION",
        "LOG_RETURN_TOTAL_CAP_1D",
        ("COINMARKETCAP_GLOBAL_DAILY",),
        "1d",
        "published daily",
        "bps",
        "daily",
        "CMC_DAILY",
    ),
    _feature(
        "crypto.total_market_cap_return_7d",
        "CRYPTO_CAPITAL_EXPANSION",
        "LOG_RETURN_TOTAL_CAP_7D",
        ("COINMARKETCAP_GLOBAL_DAILY",),
        "7d",
        "published daily",
        "bps",
        "daily",
        "CMC_DAILY",
    ),
    _feature(
        "crypto.btc_dominance_change_1d",
        "CRYPTO_ROTATION",
        "PROVIDER_BTC_DOMINANCE_DELTA_1D",
        ("COINMARKETCAP_GLOBAL_DAILY",),
        "1d",
        "published daily",
        "percentage_points",
        "daily",
        "CMC_DAILY",
    ),
    _feature(
        "crypto.eth_dominance_change_1d",
        "CRYPTO_ROTATION",
        "PROVIDER_ETH_DOMINANCE_DELTA_1D",
        ("COINMARKETCAP_GLOBAL_DAILY",),
        "1d",
        "published daily",
        "percentage_points",
        "daily",
        "CMC_DAILY",
    ),
    _feature(
        "stablecoin.aggregate_market_cap_change_1d",
        "STABLECOIN_LIQUIDITY",
        "LOG_RETURN_USDT_USDC_DAI_CAP_1D",
        ("DEFILLAMA_STABLECOINS",),
        "1d",
        "published daily",
        "bps",
        "daily",
        "STABLECOIN_DAILY",
    ),
    _feature(
        "stablecoin.aggregate_market_cap_change_7d",
        "STABLECOIN_LIQUIDITY",
        "LOG_RETURN_USDT_USDC_DAI_CAP_7D",
        ("DEFILLAMA_STABLECOINS",),
        "7d",
        "published daily",
        "bps",
        "daily",
        "STABLECOIN_DAILY",
    ),
    _feature(
        "stablecoin.dominance_change_7d",
        "STABLECOIN_LIQUIDITY",
        "STABLE_CAP_OVER_TOTAL_CAP_DELTA_7D",
        ("DEFILLAMA_STABLECOINS", "COINMARKETCAP_GLOBAL_DAILY"),
        "7d",
        "both published",
        "percentage_points",
        "daily",
        "STABLECOIN_DAILY",
    ),
    _feature(
        "equity.qqq_return_completed_15m",
        "EQUITY_RISK_APPETITE",
        "LOG_RETURN_QQQ_COMPLETED_15M",
        ("ALPACA_QQQ_SIP",),
        "15m",
        "one completed bar",
        "bps",
        "15m",
        "QQQ_INTRADAY",
    ),
    _feature(
        "equity.qqq_return_completed_60m",
        "EQUITY_RISK_APPETITE",
        "LOG_RETURN_QQQ_COMPLETED_60M",
        ("ALPACA_QQQ_SIP",),
        "60m",
        "one completed bar",
        "bps",
        "15m",
        "QQQ_INTRADAY",
    ),
    _feature(
        "equity.qqq_previous_session_return",
        "EQUITY_RISK_APPETITE",
        "LOG_RETURN_QQQ_PREVIOUS_SESSION",
        ("ALPACA_QQQ_SIP",),
        "1 session",
        "previous completed session",
        "bps",
        "daily",
        "QQQ_PRIOR_SESSION",
    ),
    _feature(
        "risk.vix_change_1d",
        "VOLATILITY_FEAR",
        "LOG_RETURN_VIX_PRIOR_SESSION",
        ("CBOE_VIX_DAILY",),
        "1d",
        "previous completed session",
        "bps",
        "daily",
        "VIX_DAILY",
    ),
    _feature(
        "risk.vix_zscore_60sessions",
        "VOLATILITY_FEAR",
        "TRAILING_VIX_ZSCORE_60_SESSIONS",
        ("CBOE_VIX_DAILY",),
        "60 sessions",
        "previous completed session",
        "zscore",
        "daily",
        "VIX_DAILY",
    ),
    _feature(
        "usd.broad_index_return_5d",
        "USD_RATES",
        "LOG_RETURN_DTWEXBGS_5_OBSERVATIONS",
        ("FRED_ALFRED_DAILY",),
        "5 observations",
        "vintage usable",
        "bps",
        "daily",
        "FRED_DAILY",
    ),
    _feature(
        "rates.us2y_change_5d",
        "USD_RATES",
        "DGS2_DELTA_5_OBSERVATIONS",
        ("FRED_ALFRED_DAILY",),
        "5 observations",
        "vintage usable",
        "bp",
        "daily",
        "FRED_DAILY",
    ),
    _feature(
        "rates.us10y_change_5d",
        "USD_RATES",
        "DGS10_DELTA_5_OBSERVATIONS",
        ("FRED_ALFRED_DAILY",),
        "5 observations",
        "vintage usable",
        "bp",
        "daily",
        "FRED_DAILY",
    ),
    _feature(
        "cross_asset.qqq_minus_btc_return_60m",
        "CROSS_ASSET_LEAD_LAG",
        "QQQ_MINUS_BTC_SYNCHRONIZED_60M_RETURN",
        ("ALPACA_QQQ_SIP", "BTC_AUTHORITY"),
        "60m",
        "completed QQQ bar",
        "bps",
        "15m",
        "QQQ_INTRADAY",
    ),
    _feature(
        "cross_asset.qqq_btc_correlation_20sessions",
        "CROSS_ASSET_LEAD_LAG",
        "PEARSON_SYNCHRONIZED_QQQ_BTC_20_SESSIONS",
        ("ALPACA_QQQ_SIP", "BTC_AUTHORITY"),
        "20 sessions",
        "previous completed session",
        "correlation",
        "daily",
        "QQQ_PRIOR_SESSION",
    ),
    _feature(
        "breadth.crypto_positive_share_60m",
        "MARKET_BREADTH",
        "FROZEN_UNIVERSE_POSITIVE_SHARE_60M",
        ("BINANCE_SPOT_BREADTH",),
        "60m",
        "completed candles",
        "ratio",
        "1m",
        "BINANCE_BREADTH",
    ),
    _feature(
        "breadth.crypto_outperform_btc_share_60m",
        "MARKET_BREADTH",
        "FROZEN_UNIVERSE_OUTPERFORM_BTC_SHARE_60M",
        ("BINANCE_SPOT_BREADTH", "BTC_AUTHORITY"),
        "60m",
        "completed candles",
        "ratio",
        "1m",
        "BINANCE_BREADTH",
    ),
    _feature(
        "breadth.crypto_dispersion_60m",
        "MARKET_BREADTH",
        "FROZEN_UNIVERSE_RETURN_STDDEV_60M",
        ("BINANCE_SPOT_BREADTH",),
        "60m",
        "completed candles",
        "bps",
        "1m",
        "BINANCE_BREADTH",
    ),
)

if len(FEATURE_CATALOGUE_V1) != 20 or len({item.name for item in FEATURE_CATALOGUE_V1}) != 20:
    raise RuntimeError("Capital Flow V1 feature catalogue must contain exactly 20 unique features")
