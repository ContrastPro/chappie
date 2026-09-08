"""Frozen, result-blind design contract for Short-Horizon Direction V2.

This module deliberately contains definitions and safety checks only.  It neither
loads an outcome, materializes a V2 dataset, nor starts an experiment.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .free_v2_materializer import FEATURE_ORDER as CAPITAL_FLOW_FEATURE_ORDER

EXPERIMENT_IDENTITY = "CHAPPIE_SHORT_HORIZON_MARKET_DIRECTION_V2/1.0.0"
RESEARCH_START = datetime(2024, 1, 1, tzinfo=UTC)
RESEARCH_END_EXCLUSIVE = datetime(2025, 6, 1, tzinfo=UTC)
VALIDATION_START = RESEARCH_END_EXCLUSIVE
VALIDATION_END_EXCLUSIVE = datetime(2026, 6, 1, tzinfo=UTC)
PRIMARY_HORIZON = timedelta(hours=1)
SECONDARY_HORIZON = timedelta(hours=4)
TARGET_SEMANTICS = "reuse CAPTIAL_FLOW_TSUNAMI_V1 causal UP/DOWN/NO_MOVE target unchanged"
DATASET_MATERIALIZED = False
RESEARCH_EXECUTED = False
VALIDATION_ACCESSED = False
BLIND_OOS_ACCESSED = False


@dataclass(frozen=True, slots=True)
class FeatureDefinition:
    name: str
    family: str
    formula: str
    completed_timeframes: tuple[str, ...]


TECHNICAL_FEATURES = (
    FeatureDefinition("technical.rsi_5m", "TECHNICAL", "Wilder RSI(14) on completed 5m closes", ("5m",)),
    FeatureDefinition("technical.rsi_15m", "TECHNICAL", "Wilder RSI(14) on completed 15m closes", ("15m",)),
    FeatureDefinition("technical.rsi_1h", "TECHNICAL", "Wilder RSI(14) on completed 1h closes", ("1h",)),
    FeatureDefinition("technical.rsi_4h", "TECHNICAL", "Wilder RSI(14) on completed 4h closes", ("4h",)),
    FeatureDefinition("technical.rsi_1h_slope", "TECHNICAL", "RSI_1h(T)-RSI_1h(T-1h)", ("1h",)),
    FeatureDefinition("technical.rsi_1h_distance_50", "TECHNICAL", "RSI_1h(T)-50", ("1h",)),
    FeatureDefinition("technical.rsi_1h_extreme", "TECHNICAL", "-1 if RSI<=30, +1 if RSI>=70, else 0", ("1h",)),
    FeatureDefinition("technical.normalized_atr_1h", "TECHNICAL", "Wilder ATR(14)/completed 1h close", ("1h",)),
    FeatureDefinition("technical.volatility_ratio_5m_1h", "TECHNICAL", "stdev(log returns,12 completed 5m)/stdev(log returns,24 completed 1h)", ("5m", "1h")),
    FeatureDefinition("technical.return_15m", "TECHNICAL", "log(close_T/close_T-15m)", ("15m",)),
    FeatureDefinition("technical.return_1h", "TECHNICAL", "log(close_T/close_T-1h)", ("1h",)),
    FeatureDefinition("technical.return_4h", "TECHNICAL", "log(close_T/close_T-4h)", ("4h",)),
    FeatureDefinition("technical.trend_strength_1h", "TECHNICAL", "abs(return_4h)/sum(abs(1h returns),4)", ("1h",)),
    FeatureDefinition("technical.macd_histogram_1h", "TECHNICAL", "EMA12(close)-EMA26(close)-EMA9(EMA12-EMA26)", ("1h",)),
    FeatureDefinition("technical.bollinger_location_1h", "TECHNICAL", "(close-SMA20)/(2*sample_stdev20), unavailable if zero", ("1h",)),
    FeatureDefinition("technical.body_to_range_1h", "TECHNICAL", "(close-open)/(high-low), unavailable if zero range", ("1h",)),
    FeatureDefinition("technical.upper_wick_to_range_1h", "TECHNICAL", "(high-max(open,close))/(high-low)", ("1h",)),
    FeatureDefinition("technical.lower_wick_to_range_1h", "TECHNICAL", "(min(open,close)-low)/(high-low)", ("1h",)),
    FeatureDefinition("technical.close_location_1h", "TECHNICAL", "(close-low)/(high-low)", ("1h",)),
    FeatureDefinition("technical.range_to_atr_1h", "TECHNICAL", "(high-low)/ATR14", ("1h",)),
    FeatureDefinition("technical.consecutive_direction_1h", "TECHNICAL", "signed run length of completed 1h close changes, capped at 5", ("1h",)),
)

VOLUME_FEATURES = (
    FeatureDefinition("volume.log_volume_1h", "VOLUME", "log(completed 1h base volume)", ("1h",)),
    FeatureDefinition("volume.relative_volume_1h", "VOLUME", "volume/SMA(volume,24)", ("1h",)),
    FeatureDefinition("volume.zscore_1h", "VOLUME", "(volume-mean(volume,24))/sample_stdev(volume,24)", ("1h",)),
    FeatureDefinition("volume.acceleration_1h", "VOLUME", "log(volume_T/volume_T-1h)", ("1h",)),
    FeatureDefinition("volume.spike_1h", "VOLUME", "1 if zscore>=2, else 0", ("1h",)),
    FeatureDefinition("volume.price_volume_divergence_4h", "VOLUME", "sign(return_4h)*sign(log(volume_4h/previous_4h_volume))", ("1h", "4h")),
    FeatureDefinition("volume.breakout_confirmation_1h", "VOLUME", "breakout_20h * relative_volume_1h", ("1h",)),
    FeatureDefinition("volume.vwap_distance_1h", "VOLUME", "(close-VWAP20)/VWAP20; VWAP uses completed typical-price*volume", ("1h",)),
    FeatureDefinition("volume.vwap_slope_1h", "VOLUME", "log(VWAP20_T/VWAP20_T-1h)", ("1h",)),
)

MARKET_STRUCTURE_FEATURES = (
    FeatureDefinition("structure.swing_trend_4h", "MARKET_STRUCTURE", "sign(last confirmed swing high and low both higher/lower; else 0), swing=2 bars each side", ("4h",)),
    FeatureDefinition("structure.distance_swing_high_4h", "MARKET_STRUCTURE", "(close-max high of completed trailing 20 4h bars)/close", ("4h",)),
    FeatureDefinition("structure.distance_swing_low_4h", "MARKET_STRUCTURE", "(close-min low of completed trailing 20 4h bars)/close", ("4h",)),
    FeatureDefinition("structure.breakout_20h", "MARKET_STRUCTURE", "+1/-1 when close exceeds prior 20 completed 1h highs/lows, else 0", ("1h",)),
    FeatureDefinition("structure.failed_breakout_20h", "MARKET_STRUCTURE", "prior-bar breakout whose current completed close returned inside prior range", ("1h",)),
    FeatureDefinition("structure.retest_20h", "MARKET_STRUCTURE", "within 0.25 ATR of prior breakout level after breakout", ("1h",)),
    FeatureDefinition("structure.compression_1h", "MARKET_STRUCTURE", "ATR14/SMA(ATR14,20)", ("1h",)),
    FeatureDefinition("structure.expansion_1h", "MARKET_STRUCTURE", "1 if compression ratio>=1.5, else 0", ("1h",)),
    FeatureDefinition("structure.support_resistance_distance_1h", "MARKET_STRUCTURE", "min distance to prior 20h high or low divided by ATR14", ("1h",)),
    FeatureDefinition("structure.inside_outside_bar_1h", "MARKET_STRUCTURE", "-1 inside prior range, +1 outside prior range, else 0", ("1h",)),
    FeatureDefinition("structure.engulfing_body_1h", "MARKET_STRUCTURE", "signed 1 when current body contains prior body and signs differ, else 0", ("1h",)),
)

MULTI_TIMEFRAME_FEATURES = (
    FeatureDefinition("mtf.trend_consensus", "TECHNICAL", "mean(sign(return)) across completed 5m,15m,1h,4h", ("5m", "15m", "1h", "4h")),
    FeatureDefinition("mtf.momentum_consensus", "TECHNICAL", "mean(sign(RSI-50)) across completed 5m,15m,1h,4h", ("5m", "15m", "1h", "4h")),
    FeatureDefinition("mtf.volatility_alignment", "TECHNICAL", "fraction of 5m,15m,1h,4h normalized ATR above own trailing median", ("5m", "15m", "1h", "4h")),
    FeatureDefinition("mtf.structure_alignment", "MARKET_STRUCTURE", "mean of completed 1h and 4h trend/breakout signs", ("1h", "4h")),
)

NEW_FEATURES = TECHNICAL_FEATURES + VOLUME_FEATURES + MARKET_STRUCTURE_FEATURES + MULTI_TIMEFRAME_FEATURES
FEATURE_FAMILIES = ("CAPITAL_FLOW", "TECHNICAL", "VOLUME", "MARKET_STRUCTURE")
MODEL_COMPARISONS = (
    "majority_historical_frequency", "btc_only_causal_price_volatility", "capital_flow_only",
    "technical_only", "volume_only", "market_structure_only", "technical_plus_volume",
    "technical_plus_market_structure", "volume_plus_market_structure", "capital_flow_plus_technical",
    "capital_flow_plus_technical_plus_volume", "full_v2",
)
PRIMARY_METRIC = "1h_direction_balanced_accuracy"
INCREMENTAL_METRICS = ("full_v2_minus_btc_only", "full_v2_minus_capital_flow_only")
PURGE_DURATION = timedelta(hours=4)
EMBARGO_DURATION = timedelta(hours=4)
WALK_FORWARD_POLICY = "expanding-origin chronological folds; no temporal shuffle; folds are frozen before execution"
CALIBRATION_POLICY = "Research-only calibration; report reliability at 50-60,60-70,70-80,80-90,90-100% where supported"
CONTRIBUTION_POLICY = "held-out block permutation and leave-one-feature-out; repeat both leave-family-out and family permutation"
BOOTSTRAP_POLICY = "deterministic moving seven-day-block bootstrap, 2,000 replicates, for both primary incremental deltas"
MULTIPLE_TESTING_POLICY = "Holm alpha=.05 for the two 1h primary incremental tests; BH q=.10 for secondary hypotheses"
ANALYTICAL_SCHEMA_POLICY = "partitioned Parquet, timestamp UTC plus numeric feature and availability-state columns; targets remain separate"
DATA_AUDIT = {
    "btc_1m_authority": "AVAILABLE_VALIDATED_RESEARCH_PREFIX",
    "derivable_completed_timeframes": ("5m", "15m", "1h", "4h", "1d"),
    "volume_complete": "REQUIRES_INPUT_VERIFY_AT_EXECUTION",
    "warmup": "requires 26 completed 1h bars plus 20 completed 4h bars; available before first assessment",
    "new_download_required": False,
    "crypto_rotation": "SOURCE_REQUIRED: reproducible licensed historical TOTAL/TOTAL2/TOTAL3/dominance source not bound",
    "cross_asset": "SOURCE_REQUIRED: DXY/XAU/Russell authority and PIT availability not yet bound",
    "whale_entity_flow": "DESIGN_ONLY: aggregate labelled flows; no identity-based trading rule",
}


def completed_candle_end(timestamp: datetime, timeframe: timedelta) -> datetime:
    """Return the latest completed candle end at a minute-resolution forecast timestamp."""
    if timestamp.tzinfo is None or timestamp.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be UTC")
    if timeframe not in tuple(timedelta(minutes=n) for n in (5, 15, 60, 240, 1440)):
        raise ValueError("timeframe is not frozen by V2")
    return timestamp.replace(second=0, microsecond=0) - (timestamp - datetime(1970, 1, 1, tzinfo=UTC)) % timeframe


def validate_contract() -> None:
    if RESEARCH_END_EXCLUSIVE != VALIDATION_START or VALIDATION_ACCESSED or BLIND_OOS_ACCESSED:
        raise RuntimeError("Research/Validation firewall drift")
    if len({item.name for item in NEW_FEATURES}) != len(NEW_FEATURES):
        raise RuntimeError("V2 feature names must be unique")
    if not all(item.completed_timeframes for item in NEW_FEATURES):
        raise RuntimeError("every feature must declare completed-candle semantics")
    if "full_v2" not in MODEL_COMPARISONS or len(CAPITAL_FLOW_FEATURE_ORDER) != 15:
        raise RuntimeError("baseline contract drift")


validate_contract()
