"""Immutable executable formula contracts for the 15 active Capital Flow Free V2 features."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation, localcontext

from .binance_breadth_authority import BINANCE_BREADTH_SYMBOLS_V1
from .fred_alfred_v2 import GOLD_STATUS_V2
from .free_v2 import (
    FREE_FEATURE_CATALOGUE_V2,
    FREE_FEATURE_CATALOGUE_V2_IDENTITY,
    FREE_FEATURE_NAMES_V2,
)

CAPITAL_FLOW_FREE_V2_FEATURE_FORMULAS_IDENTITY = "CAPITAL_FLOW_FREE_V2_FEATURE_FORMULAS/1.0.0"
NATURAL_LOG_RETURN_IDENTITY = "NATURAL_LOG_RETURN/1.0.0"
NASDAQ_V2_SEMANTIC = "NASDAQCOM_PREVIOUS_COMPLETED_NASDAQ_REGULAR_SESSION_ONLY"
NASDAQ_V1_CONFLICT_RESOLUTION = "V2_SEMANTIC_SUPERSEDES_V1_FOR_ACTIVE_FREE_V2"
NASDAQ_CALENDAR_IDENTITY = "NASDAQ_EQUITY_REGULAR_SESSION_CALENDAR/1.0.0"
FEATURE_TIMELINE_START = datetime(2024, 1, 1, tzinfo=UTC)
FEATURE_TIMELINE_END_EXCLUSIVE = datetime(2026, 6, 1, tzinfo=UTC)
FEATURE_TIMELINE_MINUTES = 1_270_080
FORMULA_DECIMAL_PRECISION = 50

INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
SOURCE_STALE = "SOURCE_STALE"
INVALID_SOURCE_SESSION = "INVALID_SOURCE_SESSION"
MISSING_REQUIRED_SYMBOL = "MISSING_REQUIRED_SYMBOL"
WINDOW_INCOMPLETE = "WINDOW_INCOMPLETE"
SOURCE_VALUE_INVALID = "SOURCE_VALUE_INVALID"
ZERO_VARIANCE = "ZERO_VARIANCE"
FEATURE_UNAVAILABLE_STATES = (
    INSUFFICIENT_HISTORY,
    SOURCE_UNAVAILABLE,
    SOURCE_STALE,
    INVALID_SOURCE_SESSION,
    MISSING_REQUIRED_SYMBOL,
    WINDOW_INCOMPLETE,
    SOURCE_VALUE_INVALID,
    ZERO_VARIANCE,
)

CRYPTO_SYMBOLS = BINANCE_BREADTH_SYMBOLS_V1
ALT_SYMBOLS = tuple(symbol for symbol in CRYPTO_SYMBOLS if symbol != "BTCUSDT")
STABLECOIN_SYMBOLS = ("USDT", "USDC", "DAI")


class FeatureFormulaUnavailable(ValueError):
    """Fail-closed formula outcome carrying one frozen unavailable-state identity."""

    def __init__(self, reason: str) -> None:
        if reason not in FEATURE_UNAVAILABLE_STATES:
            raise ValueError("unknown feature unavailable state")
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class FeatureFormulaContractV1:
    name: str
    family: str
    formula_identity: str
    source_authorities: tuple[str, ...]
    formula: str
    window: str
    unit: str
    causal_semantics: str
    unavailable_policy: tuple[str, ...] = FEATURE_UNAVAILABLE_STATES
    contract_identity: str = CAPITAL_FLOW_FREE_V2_FEATURE_FORMULAS_IDENTITY
    catalogue_identity: str = FREE_FEATURE_CATALOGUE_V2_IDENTITY

    def __post_init__(self) -> None:
        if (
            self.name not in FREE_FEATURE_NAMES_V2
            or not self.family
            or not self.formula_identity.endswith("/1.0.0")
            or not self.source_authorities
            or self.unavailable_policy != FEATURE_UNAVAILABLE_STATES
        ):
            raise ValueError("invalid Free V2 formula contract")


@dataclass(frozen=True, slots=True)
class CausalDailyValueV1:
    reference_date: date
    value: Decimal
    usable_at_utc: datetime
    stale: bool = False

    def __post_init__(self) -> None:
        _minute_boundary(self.usable_at_utc)
        _finite(self.value)


def _contract(
    name: str,
    formula: str,
    window: str,
    unit: str,
    causal: str,
) -> FeatureFormulaContractV1:
    catalogue = {item.name: item for item in FREE_FEATURE_CATALOGUE_V2}[name]
    return FeatureFormulaContractV1(
        name=name,
        family=catalogue.family,
        formula_identity=f"CAPITAL_FLOW_FREE_V2/{name.upper().replace('.', '_')}/1.0.0",
        source_authorities=catalogue.authorities,
        formula=formula,
        window=window,
        unit=unit,
        causal_semantics=causal,
    )


FREE_V2_FEATURE_FORMULA_CONTRACTS = (
    _contract(
        "breadth.crypto_positive_share_60m",
        "COUNT(LN(P_s(T)/P_s(T-60m))>0 FOR FIXED_19)/19",
        "61_CONTIGUOUS_ENDPOINT_CLOSES_EXACT_60_ELAPSED_MINUTES",
        "share_[0,1]",
        "COMPLETE_BINANCE_1M_CLOSE_BOUNDARIES_AT_OR_BEFORE_T_ALL_19_REQUIRED",
    ),
    _contract(
        "breadth.crypto_alt_outperform_btc_share_60m",
        "COUNT(r_alt_60m>r_BTCUSDT_60m FOR FIXED_18)/18",
        "61_CONTIGUOUS_ENDPOINT_CLOSES_EXACT_60_ELAPSED_MINUTES",
        "share_[0,1]",
        "COMPLETE_BINANCE_1M_CLOSE_BOUNDARIES_AT_OR_BEFORE_T_ALL_19_REQUIRED",
    ),
    _contract(
        "breadth.crypto_dispersion_60m",
        "POPULATION_STANDARD_DEVIATION_DDOF_0_OF_FIXED_19_LOG_RETURNS",
        "61_CONTIGUOUS_ENDPOINT_CLOSES_EXACT_60_ELAPSED_MINUTES",
        "log_return_dispersion",
        "COMPLETE_BINANCE_1M_CLOSE_BOUNDARIES_AT_OR_BEFORE_T_ALL_19_REQUIRED",
    ),
    _contract(
        "breadth.crypto_btc_quote_volume_share_60m",
        "BTC_QUOTE_VOLUME_60_CANDLES/SUM_FIXED_19_QUOTE_VOLUME_60_CANDLES",
        "T_MINUS_60M_EXCLUSIVE_TO_T_INCLUSIVE_EXACT_60_CANDLES",
        "share_[0,1]",
        "QUOTE_VOLUME_ONLY_COMPLETE_WINDOWS_ALL_19_REQUIRED_TOTAL_POSITIVE",
    ),
    _contract(
        "stablecoin.aggregate_market_cap_change_1d",
        "LN(SUM_USDT_USDC_DAI(D)/SUM_USDT_USDC_DAI(D-1_CALENDAR_DAY))",
        "EXACT_COMMON_REFERENCE_D_AND_D_MINUS_1_CALENDAR_DAY",
        "dimensionless_log_return",
        "LATEST_SAME_DATE_ALL_THREE_USABLE_AND_NOT_STALE_NO_FALLBACK",
    ),
    _contract(
        "stablecoin.aggregate_market_cap_change_7d",
        "LN(SUM_USDT_USDC_DAI(D)/SUM_USDT_USDC_DAI(D-7_CALENDAR_DAYS))",
        "EXACT_COMMON_REFERENCE_D_AND_D_MINUS_7_CALENDAR_DAYS",
        "dimensionless_log_return",
        "LATEST_SAME_DATE_ALL_THREE_USABLE_AND_NOT_STALE_NO_FALLBACK",
    ),
    _contract(
        "fx.usdjpy_return_5d",
        "LN(DEXJPUS_CURRENT/DEXJPUS_FIFTH_PRIOR_VALID_OBSERVATION)",
        "CURRENT_PLUS_FIVE_PRIOR_VALID_REFERENCE_OBSERVATIONS",
        "dimensionless_log_return",
        "JPY_PER_USD_NEVER_INVERTED_CURRENT_CAUSALLY_USABLE_AND_NOT_STALE",
    ),
    _contract(
        "equity.nasdaq_previous_session_return",
        "LN(NASDAQCOM(S1)/NASDAQCOM(S0))",
        "LATEST_COMPLETED_NASDAQ_SESSION_AND_IMMEDIATELY_PRECEDING_SESSION",
        "dimensionless_log_return",
        f"{NASDAQ_V2_SEMANTIC};BOTH_CAUSALLY_USABLE_AT_T;NO_OLDER_BLOCK_FALLBACK",
    ),
    _contract(
        "risk.vix_change_1d",
        "VIXCLS(V1)-VIXCLS(V0)",
        "LATEST_COMPLETED_CBOE_VIX_SOURCE_SESSION_AND_IMMEDIATELY_PRECEDING_SESSION",
        "VIX_index_points",
        "BOTH_CAUSALLY_USABLE_AT_T_NO_OLDER_BLOCK_FALLBACK",
    ),
    _contract(
        "risk.vix_zscore_60sessions",
        "(CURRENT_VIX-MEAN_60)/POPULATION_STANDARD_DEVIATION_60_DDOF_0",
        "EXACT_60_CONSECUTIVE_VALID_COMPLETED_CBOE_VIX_SOURCE_SESSIONS",
        "dimensionless_zscore",
        "ALL_60_CAUSALLY_USABLE_AT_T_ZERO_VARIANCE_UNAVAILABLE",
    ),
    _contract(
        "usd.broad_index_return_5d",
        "LN(DTWEXBGS_CURRENT/DTWEXBGS_FIFTH_PRIOR_VALID_OBSERVATION)",
        "CURRENT_PLUS_FIVE_PRIOR_VALID_REFERENCE_OBSERVATIONS",
        "dimensionless_log_return",
        "EVERY_VALUE_PIT_ELIGIBLE_AT_T_NO_REVISION_LEAKAGE",
    ),
    _contract(
        "rates.us2y_change_5d",
        "(DGS2_CURRENT_PERCENT-DGS2_FIFTH_PRIOR_PERCENT)*100",
        "CURRENT_PLUS_FIVE_PRIOR_VALID_REFERENCE_OBSERVATIONS",
        "basis_points",
        "CURRENT_CAUSALLY_USABLE_AND_NOT_STALE_NO_MISSING_SUBSTITUTION",
    ),
    _contract(
        "rates.us10y_change_5d",
        "(DGS10_CURRENT_PERCENT-DGS10_FIFTH_PRIOR_PERCENT)*100",
        "CURRENT_PLUS_FIVE_PRIOR_VALID_REFERENCE_OBSERVATIONS",
        "basis_points",
        "CURRENT_CAUSALLY_USABLE_AND_NOT_STALE_NO_MISSING_SUBSTITUTION",
    ),
    _contract(
        "stress.stlfsi4_level",
        "LATEST_CAUSALLY_ELIGIBLE_NON_STALE_STLFSI4_LEVEL",
        "LATEST_WEEKLY_REFERENCE_OBSERVATION",
        "STLFSI4_index_level",
        "FULL_VINTAGE_PIT_SELECTION_NO_FINAL_REVISION_SUBSTITUTION",
    ),
    _contract(
        "coupling.btc_nasdaq_correlation_20sessions",
        "PEARSON_POPULATION_COVARIANCE_OVER_POPULATION_SD_EXACT_20_PAIRED_LOG_RETURNS",
        "LATEST_21_CONSECUTIVE_COMPLETED_NASDAQ_SESSIONS_TO_20_PAIRED_RETURNS",
        "dimensionless_correlation_[-1,1]",
        "BTC_1M_CLOSE_BOUNDARY_EQUALS_OFFICIAL_NASDAQ_CLOSE_ALL_INPUTS_USABLE_AT_T",
    ),
)


def _context() -> Context:
    return Context(prec=FORMULA_DECIMAL_PRECISION, rounding=ROUND_HALF_EVEN)


def _finite(value: Decimal, *, positive: bool = False) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or (positive and value <= 0):
        raise FeatureFormulaUnavailable(SOURCE_VALUE_INVALID)
    return value


def _minute_boundary(value: datetime) -> datetime:
    if (
        value.tzinfo is None
        or value.utcoffset() != timedelta(0)
        or value.second
        or value.microsecond
    ):
        raise ValueError("timestamp must be an exact UTC minute boundary")
    return value.astimezone(UTC)


def natural_log_return(start: Decimal, end: Decimal) -> Decimal:
    """Return ln(end/start) with a frozen deterministic Decimal context."""
    start, end = _finite(start, positive=True), _finite(end, positive=True)
    try:
        with localcontext(_context()):
            return (end / start).ln()
    except (InvalidOperation, ValueError) as error:
        raise FeatureFormulaUnavailable(SOURCE_VALUE_INVALID) from error


def crypto_60m_return(closes: Mapping[datetime, Decimal], timestamp: datetime) -> Decimal:
    """Validate all 61 close endpoints and calculate the exact elapsed-60m return."""
    timestamp = _minute_boundary(timestamp)
    required = tuple(timestamp - timedelta(minutes=offset) for offset in range(60, -1, -1))
    if tuple(sorted(closes)) != required:
        raise FeatureFormulaUnavailable(WINDOW_INCOMPLETE)
    for value in closes.values():
        _finite(value, positive=True)
    return natural_log_return(closes[required[0]], closes[required[-1]])


def quote_volume_60m(volumes: Mapping[datetime, Decimal], timestamp: datetime) -> Decimal:
    """Sum exactly 60 completed candle quote volumes in (T-60m,T]."""
    timestamp = _minute_boundary(timestamp)
    required = tuple(timestamp - timedelta(minutes=offset) for offset in range(59, -1, -1))
    if tuple(sorted(volumes)) != required:
        raise FeatureFormulaUnavailable(WINDOW_INCOMPLETE)
    if any(_finite(value) < 0 for value in volumes.values()):
        raise FeatureFormulaUnavailable(SOURCE_VALUE_INVALID)
    with localcontext(_context()):
        return sum((volumes[item] for item in required), Decimal(0))


def _fixed_crypto(values: Mapping[str, Decimal]) -> tuple[Decimal, ...]:
    if any(symbol not in values for symbol in CRYPTO_SYMBOLS):
        raise FeatureFormulaUnavailable(MISSING_REQUIRED_SYMBOL)
    if set(values) != set(CRYPTO_SYMBOLS):
        raise ValueError("crypto input must match the fixed 19-symbol universe")
    return tuple(_finite(values[symbol]) for symbol in CRYPTO_SYMBOLS)


def crypto_positive_share(returns: Mapping[str, Decimal]) -> Decimal:
    values = _fixed_crypto(returns)
    with localcontext(_context()):
        return Decimal(sum(value > 0 for value in values)) / Decimal(19)


def crypto_population_dispersion(returns: Mapping[str, Decimal]) -> Decimal:
    values = _fixed_crypto(returns)
    return population_standard_deviation(values)


def crypto_alt_outperform_share(returns: Mapping[str, Decimal]) -> Decimal:
    _fixed_crypto(returns)
    btc = returns["BTCUSDT"]
    with localcontext(_context()):
        return Decimal(sum(returns[symbol] > btc for symbol in ALT_SYMBOLS)) / Decimal(18)


def btc_quote_volume_share(volumes: Mapping[str, Decimal]) -> Decimal:
    values = _fixed_crypto(volumes)
    if any(value < 0 for value in values):
        raise FeatureFormulaUnavailable(SOURCE_VALUE_INVALID)
    with localcontext(_context()):
        total = sum(values, Decimal(0))
        if total <= 0:
            raise FeatureFormulaUnavailable(SOURCE_VALUE_INVALID)
        return volumes["BTCUSDT"] / total


def stablecoin_aggregate(values: Mapping[str, Decimal]) -> Decimal:
    if set(values) != set(STABLECOIN_SYMBOLS):
        raise FeatureFormulaUnavailable(SOURCE_UNAVAILABLE)
    checked = tuple(_finite(values[symbol]) for symbol in STABLECOIN_SYMBOLS)
    with localcontext(_context()):
        result = sum(checked, Decimal(0))
    return _finite(result, positive=True)


def stablecoin_log_change(current: Mapping[str, Decimal], prior: Mapping[str, Decimal]) -> Decimal:
    return natural_log_return(stablecoin_aggregate(prior), stablecoin_aggregate(current))


def select_stablecoin_common_pair(
    observations: Mapping[str, Sequence[CausalDailyValueV1]],
    timestamp: datetime,
    *,
    lag_days: int,
) -> tuple[date, dict[str, Decimal], dict[str, Decimal]]:
    """Select latest same-date USDT/USDC/DAI values plus the exact calendar lag date."""
    timestamp = _minute_boundary(timestamp)
    if lag_days not in (1, 7) or set(observations) != set(STABLECOIN_SYMBOLS):
        raise ValueError("stablecoin pair contract requires exact assets and a 1d/7d lag")
    eligible: dict[str, dict[date, Decimal]] = {}
    for symbol in STABLECOIN_SYMBOLS:
        eligible[symbol] = {
            item.reference_date: item.value
            for item in observations[symbol]
            if item.usable_at_utc <= timestamp and not item.stale
        }
    common = set.intersection(*(set(eligible[symbol]) for symbol in STABLECOIN_SYMBOLS))
    if not common:
        raise FeatureFormulaUnavailable(SOURCE_UNAVAILABLE)
    current_date = max(common)
    prior_date = current_date - timedelta(days=lag_days)
    if any(prior_date not in eligible[symbol] for symbol in STABLECOIN_SYMBOLS):
        raise FeatureFormulaUnavailable(WINDOW_INCOMPLETE)
    return (
        current_date,
        {symbol: eligible[symbol][current_date] for symbol in STABLECOIN_SYMBOLS},
        {symbol: eligible[symbol][prior_date] for symbol in STABLECOIN_SYMBOLS},
    )


def five_observation_log_return(values: Sequence[Decimal]) -> Decimal:
    if len(values) != 6:
        raise FeatureFormulaUnavailable(INSUFFICIENT_HISTORY)
    return natural_log_return(values[0], values[5])


def treasury_change_bp(values: Sequence[Decimal]) -> Decimal:
    if len(values) != 6:
        raise FeatureFormulaUnavailable(INSUFFICIENT_HISTORY)
    first, current = _finite(values[0]), _finite(values[5])
    with localcontext(_context()):
        return (current - first) * Decimal(100)


def nasdaq_previous_session_return(previous: Decimal, latest: Decimal) -> Decimal:
    return natural_log_return(previous, latest)


def vix_point_change(previous: Decimal, latest: Decimal) -> Decimal:
    return _finite(latest) - _finite(previous)


def population_standard_deviation(values: Sequence[Decimal]) -> Decimal:
    if not values:
        raise FeatureFormulaUnavailable(INSUFFICIENT_HISTORY)
    checked = tuple(_finite(value) for value in values)
    with localcontext(_context()):
        mean = sum(checked, Decimal(0)) / Decimal(len(checked))
        variance = sum(((value - mean) ** 2 for value in checked), Decimal(0)) / Decimal(
            len(checked)
        )
        return variance.sqrt()


def vix_zscore_60(values: Sequence[Decimal]) -> Decimal:
    if len(values) != 60:
        raise FeatureFormulaUnavailable(INSUFFICIENT_HISTORY)
    checked = tuple(_finite(value) for value in values)
    deviation = population_standard_deviation(checked)
    if deviation == 0:
        raise FeatureFormulaUnavailable(ZERO_VARIANCE)
    with localcontext(_context()):
        mean = sum(checked, Decimal(0)) / Decimal(60)
        return (checked[-1] - mean) / deviation


def btc_session_candle_open(session_close: datetime) -> datetime:
    """Return the exact Binance candle open for [C_S-1m,C_S)."""
    return _minute_boundary(session_close) - timedelta(minutes=1)


def pearson_population(left: Sequence[Decimal], right: Sequence[Decimal]) -> Decimal:
    if len(left) != 20 or len(right) != 20:
        raise FeatureFormulaUnavailable(INSUFFICIENT_HISTORY)
    x, y = tuple(map(_finite, left)), tuple(map(_finite, right))
    with localcontext(_context()):
        mean_x = sum(x, Decimal(0)) / Decimal(20)
        mean_y = sum(y, Decimal(0)) / Decimal(20)
        covariance = sum(
            ((a - mean_x) * (b - mean_y) for a, b in zip(x, y, strict=True)), Decimal(0)
        ) / Decimal(20)
        sd_x = population_standard_deviation(x)
        sd_y = population_standard_deviation(y)
        if sd_x == 0 or sd_y == 0:
            raise FeatureFormulaUnavailable(ZERO_VARIANCE)
        result = covariance / (sd_x * sd_y)
        return max(Decimal(-1), min(Decimal(1), result))


def validate_feature_timestamp(timestamp: datetime) -> datetime:
    timestamp = _minute_boundary(timestamp)
    if not FEATURE_TIMELINE_START <= timestamp < FEATURE_TIMELINE_END_EXCLUSIVE:
        raise ValueError("feature timestamp outside the non-blind canonical grid")
    return timestamp


if tuple(item.name for item in FREE_V2_FEATURE_FORMULA_CONTRACTS) != FREE_FEATURE_NAMES_V2:
    raise RuntimeError("formula contracts must preserve exact active Free V2 feature order")
if len(CRYPTO_SYMBOLS) != 19 or len(ALT_SYMBOLS) != 18:
    raise RuntimeError("formula contracts require the frozen 19/18 Binance universes")
if GOLD_STATUS_V2 != "GOLD_DEFERRED_NO_DEFENSIBLE_FREE_AUTHORITY":
    raise RuntimeError("Gold deferral contract changed unexpectedly")
if (
    int((FEATURE_TIMELINE_END_EXCLUSIVE - FEATURE_TIMELINE_START).total_seconds() // 60)
    != FEATURE_TIMELINE_MINUTES
):
    raise RuntimeError("canonical feature timeline minute count changed")
