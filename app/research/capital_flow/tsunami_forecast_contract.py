"""Immutable, result-blind contract for the Capital Flow Free V2 forecast experiment."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from enum import StrEnum
from itertools import pairwise

from .free_v2_formulas import natural_log_return
from .free_v2_materializer import (
    CAPITAL_FLOW_FREE_V2_FEATURE_DATASET_IDENTITY,
    FEATURE_ORDER,
)

EXPERIMENT_IDENTITY = "CHAPPIE_CAPITAL_FLOW_TSUNAMI_FORECAST_EXPERIMENT/1.0.0"
BOUND_DATASET_IDENTITY = CAPITAL_FLOW_FREE_V2_FEATURE_DATASET_IDENTITY
BOUND_DATASET_SEMANTIC_DIGEST = "07898bfdc7fec01c249bd38b479624bcdfc353237e4d8ffcf0015fafb4977e23"
BOUND_ARTIFACT_SHA256 = "ad9ec8ec8e91375c7a579536b23a51f93cd51246685dc3e447501e7f7d6eeaed"
BOUND_MANIFEST_SEMANTIC_DIGEST = "7d4518a01e2f936cde100435a989deba5e57e6492e82863a16c75cc6b872aaaf"

RESEARCH_START = datetime(2024, 1, 1, tzinfo=UTC)
RESEARCH_END_EXCLUSIVE = datetime(2025, 6, 1, tzinfo=UTC)
VALIDATION_START = RESEARCH_END_EXCLUSIVE
VALIDATION_END_EXCLUSIVE = datetime(2026, 6, 1, tzinfo=UTC)
BLIND_OOS_START = datetime(2026, 9, 1, tzinfo=UTC)
BLIND_OOS_END_EXCLUSIVE = datetime(2027, 3, 1, tzinfo=UTC)

PRIMARY_HORIZON = timedelta(hours=24)
PRIMARY_HORIZONS = tuple(timedelta(hours=value) for value in (1, 4, 12, 24, 48, 72))
SECONDARY_HORIZONS = tuple(item for item in PRIMARY_HORIZONS if item != PRIMARY_HORIZON)
DIAGNOSTIC_HORIZONS = tuple(timedelta(minutes=value) for value in (15, 30, 120, 480))
MAX_PRIMARY_HORIZON = max(PRIMARY_HORIZONS)
PURGE_DURATION = MAX_PRIMARY_HORIZON
EMBARGO_DURATION = MAX_PRIMARY_HORIZON

TARGET_SYMBOL = "BTCUSDT"
TARGET_AUTHORITY = "chappie_binance_breadth_historical_authority/2.0.0"
TARGET_PRICE_SEMANTICS = (
    "entry is the BTCUSDT open of the first one-minute candle strictly after feature "
    "timestamp T; exit is the close of the candle ending exactly at T+H; both candles "
    "must be completed observations from the validated Binance Breadth V2 authority"
)
FUTURE_RETURN_SEMANTICS = "natural_log(exit_close / entry_open), unscaled, base e"

VOLATILITY_LOOKBACK = timedelta(days=30)
VOLATILITY_RETURN_COUNT = 43_200
VOLATILITY_PRICE_COUNT = VOLATILITY_RETURN_COUNT + 1
VOLATILITY_BASE_FREQUENCY = timedelta(minutes=1)
VOLATILITY_DEVIATION = "SAMPLE_STANDARD_DEVIATION_N_MINUS_1"
VOLATILITY_ESTIMATOR = (
    "sample standard deviation (n-1) of exactly 43,200 contiguous BTCUSDT one-minute "
    "close-to-close natural-log returns formed from exactly 43,201 completed closes ending "
    "at T, with minute endpoints [T-43,200m,T], scaled by sqrt(H minutes); unavailable on missing, "
    "nonpositive, nonfinite, incomplete, future, or zero-variance input"
)
DIRECTION_SIGMA = Decimal("0.5")
MOVE_SIGMA = Decimal(1)
STRESS_SIGMA = Decimal(2)
TSUNAMI_SIGMA = Decimal(4)


def _exact_utc_minute(value: datetime) -> datetime:
    if (
        value.tzinfo is None
        or value.utcoffset() != timedelta(0)
        or value.second
        or value.microsecond
    ):
        raise ValueError("timestamp must be an exact UTC minute")
    return value.astimezone(UTC)


class Direction(StrEnum):
    UP = "UP"
    DOWN = "DOWN"
    NO_MOVE = "NO_MOVE"


class Severity(StrEnum):
    NONE = "NONE"
    MOVE = "MOVE"
    STRESS = "STRESS"
    TSUNAMI = "TSUNAMI"


class TsunamiDirection(StrEnum):
    NONE = "NONE"
    UP_TSUNAMI = "UP_TSUNAMI"
    DOWN_TSUNAMI = "DOWN_TSUNAMI"


class ExperimentSplit(StrEnum):
    RESEARCH = "RESEARCH"
    VALIDATION = "VALIDATION"


@dataclass(frozen=True, slots=True)
class TargetLabelV1:
    standardized_return: Decimal
    direction: Direction
    severity: Severity
    tsunami_direction: TsunamiDirection


@dataclass(frozen=True, slots=True)
class TsunamiCandidateV1:
    forecast_timestamp: datetime
    outcome_endpoint: datetime
    standardized_return: Decimal

    def __post_init__(self) -> None:
        _exact_utc_minute(self.forecast_timestamp)
        _exact_utc_minute(self.outcome_endpoint)
        if self.outcome_endpoint <= self.forecast_timestamp:
            raise ValueError("outcome endpoint must be after forecast timestamp")
        if abs(self.standardized_return) < TSUNAMI_SIGMA:
            raise ValueError("episode candidates must satisfy the tsunami threshold")


@dataclass(frozen=True, slots=True)
class TsunamiEpisodeV1:
    start: datetime
    end: datetime
    onset: datetime
    peak_severity: Decimal
    direction: TsunamiDirection
    candidate_count: int


@dataclass(frozen=True, slots=True)
class WalkForwardFoldV1:
    identity: str
    train_start: datetime
    train_end_exclusive: datetime
    assessment_start: datetime
    assessment_end_exclusive: datetime

    def __post_init__(self) -> None:
        for value in (
            self.train_start,
            self.train_end_exclusive,
            self.assessment_start,
            self.assessment_end_exclusive,
        ):
            _exact_utc_minute(value)
        if self.train_start != RESEARCH_START:
            raise ValueError("walk-forward training must use the frozen expanding origin")
        if self.train_end_exclusive + MAX_PRIMARY_HORIZON != self.assessment_start:
            raise ValueError("walk-forward fold must have an exact 72-hour purge")
        if self.assessment_start >= self.assessment_end_exclusive:
            raise ValueError("assessment interval must be non-empty")
        if self.assessment_end_exclusive > RESEARCH_END_EXCLUSIVE:
            raise ValueError("walk-forward assessment may not enter Validation")


def _fold(identity: str, assessment_start: datetime, assessment_end: datetime) -> WalkForwardFoldV1:
    return WalkForwardFoldV1(
        identity,
        RESEARCH_START,
        assessment_start - MAX_PRIMARY_HORIZON,
        assessment_start,
        assessment_end,
    )


WALK_FORWARD_FOLDS = (
    _fold("WF1", datetime(2024, 7, 1, tzinfo=UTC), datetime(2024, 9, 1, tzinfo=UTC)),
    _fold("WF2", datetime(2024, 9, 1, tzinfo=UTC), datetime(2024, 11, 1, tzinfo=UTC)),
    _fold("WF3", datetime(2024, 11, 1, tzinfo=UTC), datetime(2025, 1, 1, tzinfo=UTC)),
    _fold("WF4", datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 3, 1, tzinfo=UTC)),
    _fold("WF5", datetime(2025, 3, 1, tzinfo=UTC), datetime(2025, 4, 15, tzinfo=UTC)),
    _fold("WF6", datetime(2025, 4, 15, tzinfo=UTC), RESEARCH_END_EXCLUSIVE),
)

FEATURE_FAMILY_MEMBERSHIP = (
    ("CRYPTO_PARTICIPATION", FEATURE_ORDER[0:2]),
    ("CRYPTO_ROTATION", FEATURE_ORDER[2:4]),
    ("STABLECOIN_LIQUIDITY", FEATURE_ORDER[4:6]),
    ("EQUITY_RISK", FEATURE_ORDER[6:7]),
    ("VOLATILITY_FEAR", FEATURE_ORDER[7:9]),
    ("USD_RATES_FX", FEATURE_ORDER[9:13]),
    ("DEFENSIVE_STRESS", FEATURE_ORDER[13:14]),
    ("COUPLING_REGIME", FEATURE_ORDER[14:15]),
)

REQUIRED_PROBABILITIES = (
    "P(MOVE)",
    "P(STRESS)",
    "P(TSUNAMI)",
    "P(UP)",
    "P(DOWN)",
    "P(NO_MOVE)",
    "P(UP_TSUNAMI)",
    "P(DOWN_TSUNAMI)",
)
PRIMARY_METRICS = (
    "balanced_accuracy",
    "precision",
    "recall",
    "f1",
    "roc_auc_where_defined",
    "pr_auc_where_defined",
    "brier_score",
    "log_loss",
    "expected_calibration_error",
    "confusion_matrix",
    "false_positive_rate",
    "false_alarm_rate",
    "event_precision",
    "event_recall",
    "up_accuracy",
    "down_accuracy",
    "no_move_accuracy",
    "macro_accuracy",
)
BASELINES = (
    "naive_majority_class",
    "research_historical_frequency_probability",
    "btc_only_causal_price_volatility",
    "causal_persistence_regime",
)
LEAD_TIME_METRICS = (
    "first_warning_time",
    "event_onset",
    "lead_time_mean",
    "lead_time_median",
    "lead_time_p25",
    "lead_time_p75",
    "lead_time_distribution",
    "false_early_warnings",
)
DIRECTION_CONFIDENCE_BINS = (
    (Decimal("0.5"), Decimal("0.6")),
    (Decimal("0.6"), Decimal("0.7")),
    (Decimal("0.7"), Decimal("0.8")),
    (Decimal("0.8"), Decimal("0.9")),
    (Decimal("0.9"), Decimal("1.0")),
)
CALIBRATION_BUCKETS = DIRECTION_CONFIDENCE_BINS
RARE_EVENT_CALIBRATION_EDGES = tuple(
    Decimal(item)
    for item in (
        "0",
        "0.001",
        "0.005",
        "0.01",
        "0.025",
        "0.05",
        "0.1",
        "0.2",
        "0.4",
        "0.6",
        "0.8",
        "1",
    )
)
RARE_EVENT_CALIBRATION_BINS = tuple(pairwise(RARE_EVENT_CALIBRATION_EDGES))
RARE_EVENT_CALIBRATION_TARGETS = (
    "MOVE",
    "STRESS",
    "TSUNAMI",
    "UP_TSUNAMI",
    "DOWN_TSUNAMI",
)
CALIBRATION_BIN_REPORT_FIELDS = (
    "predicted_mean_probability",
    "realized_frequency",
    "sample_count",
    "calibration_error",
)
CALIBRATION_FREEZE_POLICY = (
    "direction confidence uses fixed 50%-100% bins; rare binary events use the fixed full "
    "0-1 bins declared by this contract; Research and Validation use identical edges"
)
ALLOWED_MODEL_FAMILIES = (
    "unregularized_logistic_baseline",
    "regularized_logistic",
    "deterministic_tree_boosting_if_research_justified",
    "explicit_interaction_model_if_research_justified",
)
MISSING_STATE_POLICY = (
    "preserve each released unavailable-state category explicitly; add category indicators; "
    "fit numeric fill values on each Research training fold only; never future-fill"
)
CONTRIBUTION_POLICY = (
    "held-out contiguous-seven-day-block permutation importance with 20 deterministic "
    "permutations plus leave-one-feature-out ablation; report signed raw deltas; normalize "
    "positive held-out 24h tsunami event-PR-AUC drops to 100% only when their sum is positive"
)
ABLATION_METRICS = (
    "tsunami_event_pr_auc",
    "direction_balanced_accuracy",
    "tsunami_brier_score",
    "tsunami_pr_auc",
    "tsunami_event_recall",
)
FAMILY_CONTRIBUTION_POLICY = (
    "repeat the frozen held-out permutation and leave-family-out ablation by exact family"
)
INTERACTION_POLICY = (
    "compare the frozen additive model with the predeclared interaction model on identical "
    "held-out folds; report only the incremental metric delta as interaction contribution"
)
UNCERTAINTY_POLICY = (
    "95% confidence intervals from a deterministic moving-block bootstrap using seven-day "
    "blocks and 2,000 replicates, supplemented by horizon-level episode analysis"
)
MULTIPLE_TESTING_POLICY = (
    "24h tsunami event PR-AUC and 24h direction balanced accuracy are primary confirmatory "
    "endpoints with Holm family-wise correction at alpha 0.05; other horizons, features, "
    "families, interactions, and metrics are secondary exploratory with Benjamini-Hochberg "
    "FDR q=0.10 and may not replace the headline"
)
VALIDATION_POLICY = (
    "fit preprocessing, models, hyperparameters, thresholds, and calibration on Research "
    "only; freeze one versioned execution bundle; claim Validation exactly once; never refit, "
    "retune, recalibrate, or select a headline using Validation"
)
VALIDATION_MAX_EXECUTIONS = 1
VALIDATION_REUSE_FORBIDDEN = True
ANALYTICAL_VIEW_REQUIRED = True
ANALYTICAL_VIEW_POLICY = (
    "build in a separate reviewed slice under E:/projects/chappie/data; bind the authoritative "
    "dataset semantic digest; contain features and availability states only, no targets"
)
TRADING_CALCULATIONS_ALLOWED = False
FEATURE_DATASET_STATUS = "VALIDATED"
EXPERIMENT_CONTRACT_STATUS = "IMPLEMENTED"
EXPERIMENT_EXECUTION_STATUS = "NOT_RUN"
VALIDATION_STATUS = "NOT_ACCESSED"
PAPER_TRADING_STATUS = "NOT_RUN"


def future_return_endpoints(timestamp: datetime, horizon: timedelta) -> tuple[datetime, datetime]:
    """Return the strictly-future entry candle open and exact horizon exit candle close."""
    timestamp = _exact_utc_minute(timestamp)
    if horizon not in PRIMARY_HORIZONS + DIAGNOSTIC_HORIZONS:
        raise ValueError("horizon is not frozen by the experiment contract")
    return timestamp + timedelta(minutes=1), timestamp + horizon


def future_log_return(entry_open: Decimal, exit_close: Decimal) -> Decimal:
    """Calculate the frozen unscaled natural-log target on synthetic scalar inputs."""
    return natural_log_return(entry_open, exit_close)


def causal_horizon_volatility(
    completed_closes: Mapping[datetime, Decimal], timestamp: datetime, horizon: timedelta
) -> Decimal:
    """Return causal sigma-H from the exact trailing 30-day completed-minute window."""
    timestamp = _exact_utc_minute(timestamp)
    if horizon not in PRIMARY_HORIZONS + DIAGNOSTIC_HORIZONS:
        raise ValueError("horizon is not frozen by the experiment contract")
    required = tuple(
        timestamp - timedelta(minutes=offset) for offset in range(VOLATILITY_RETURN_COUNT, -1, -1)
    )
    if tuple(sorted(completed_closes)) != required:
        raise ValueError("volatility requires exactly 43,201 contiguous closes through T")
    closes = tuple(completed_closes[item] for item in required)
    if any(value <= 0 or not value.is_finite() for value in closes):
        raise ValueError("volatility closes must be positive and finite")
    returns = tuple(natural_log_return(start, end) for start, end in pairwise(closes))
    with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
        count = Decimal(len(returns))
        mean = sum(returns, Decimal(0)) / count
        variance = sum(((item - mean) ** 2 for item in returns), Decimal(0)) / (count - 1)
        if variance <= 0:
            raise ValueError("volatility variance must be positive")
        horizon_minutes = Decimal(int(horizon / timedelta(minutes=1)))
        return variance.sqrt() * horizon_minutes.sqrt()


def split_bounds(split: ExperimentSplit) -> tuple[datetime, datetime]:
    """Return the immutable observation/target containment interval for a split."""
    if split is ExperimentSplit.RESEARCH:
        return RESEARCH_START, RESEARCH_END_EXCLUSIVE
    if split is ExperimentSplit.VALIDATION:
        return VALIDATION_START, VALIDATION_END_EXCLUSIVE
    raise ValueError("unknown experiment split")


def last_target_eligible_timestamp(split: ExperimentSplit, horizon: timedelta) -> datetime:
    """Return the last T whose exact T+H endpoint is contained in the selected split."""
    if horizon not in PRIMARY_HORIZONS + DIAGNOSTIC_HORIZONS:
        raise ValueError("horizon is not frozen by the experiment contract")
    _, end_exclusive = split_bounds(split)
    return end_exclusive - horizon


def target_is_split_eligible(
    split: ExperimentSplit, timestamp: datetime, horizon: timedelta
) -> bool:
    """Fail closed at split edges so no target endpoint consumes the next split."""
    timestamp = _exact_utc_minute(timestamp)
    start, _ = split_bounds(split)
    return start <= timestamp <= last_target_eligible_timestamp(split, horizon)


def fold_target_is_eligible(
    fold: WalkForwardFoldV1, phase: str, timestamp: datetime, horizon: timedelta
) -> bool:
    """Apply target endpoint containment independently to each walk-forward partition."""
    timestamp = _exact_utc_minute(timestamp)
    if horizon not in PRIMARY_HORIZONS + DIAGNOSTIC_HORIZONS:
        raise ValueError("horizon is not frozen by the experiment contract")
    if phase == "TRAIN":
        start, end = fold.train_start, fold.train_end_exclusive
    elif phase == "ASSESSMENT":
        start, end = fold.assessment_start, fold.assessment_end_exclusive
    else:
        raise ValueError("fold phase must be TRAIN or ASSESSMENT")
    return start <= timestamp and timestamp + horizon <= end


def rare_event_calibration_bin(probability: Decimal) -> int:
    """Map a probability to one immutable full-domain rare-event calibration bin."""
    if not probability.is_finite() or probability < 0 or probability > 1:
        raise ValueError("probability must be finite and within [0,1]")
    for index, (lower, upper) in enumerate(RARE_EVENT_CALIBRATION_BINS):
        if lower <= probability < upper or probability == upper == Decimal(1):
            return index
    raise RuntimeError("rare-event calibration bins do not cover probability")


def classify_standardized_return(value: Decimal) -> TargetLabelV1:
    """Apply the frozen direction and nested event thresholds."""
    if not value.is_finite():
        raise ValueError("standardized return must be finite")
    magnitude = abs(value)
    direction = (
        Direction.UP
        if value >= DIRECTION_SIGMA
        else Direction.DOWN
        if value <= -DIRECTION_SIGMA
        else Direction.NO_MOVE
    )
    severity = (
        Severity.TSUNAMI
        if magnitude >= TSUNAMI_SIGMA
        else Severity.STRESS
        if magnitude >= STRESS_SIGMA
        else Severity.MOVE
        if magnitude >= MOVE_SIGMA
        else Severity.NONE
    )
    tsunami_direction = (
        TsunamiDirection.UP_TSUNAMI
        if value >= TSUNAMI_SIGMA
        else TsunamiDirection.DOWN_TSUNAMI
        if value <= -TSUNAMI_SIGMA
        else TsunamiDirection.NONE
    )
    return TargetLabelV1(value, direction, severity, tsunami_direction)


def cluster_tsunami_episodes(
    candidates: tuple[TsunamiCandidateV1, ...], horizon: timedelta
) -> tuple[TsunamiEpisodeV1, ...]:
    """Cluster overlapping labels; candidates no more than H apart form one episode."""
    if horizon not in PRIMARY_HORIZONS:
        raise ValueError("episodes are defined only for primary horizons")
    ordered = tuple(sorted(candidates, key=lambda item: item.forecast_timestamp))
    if len({item.forecast_timestamp for item in ordered}) != len(ordered):
        raise ValueError("duplicate candidate timestamp")
    groups: list[list[TsunamiCandidateV1]] = []
    for candidate in ordered:
        if not groups or candidate.forecast_timestamp - groups[-1][-1].forecast_timestamp > horizon:
            groups.append([candidate])
        else:
            groups[-1].append(candidate)
    episodes = []
    for group in groups:
        peak = max(
            group,
            key=lambda item: (abs(item.standardized_return), -item.forecast_timestamp.timestamp()),
        )
        direction = (
            TsunamiDirection.UP_TSUNAMI
            if peak.standardized_return > 0
            else TsunamiDirection.DOWN_TSUNAMI
        )
        episodes.append(
            TsunamiEpisodeV1(
                start=group[0].forecast_timestamp,
                end=max(item.outcome_endpoint for item in group),
                onset=group[0].outcome_endpoint,
                peak_severity=abs(peak.standardized_return),
                direction=direction,
                candidate_count=len(group),
            )
        )
    return tuple(episodes)


def validate_contract() -> None:
    """Fail closed if the immutable contract develops internal overlap or drift."""
    if RESEARCH_END_EXCLUSIVE != VALIDATION_START:
        raise RuntimeError("Research and Validation boundary mismatch")
    if VALIDATION_END_EXCLUSIVE > BLIND_OOS_START:
        raise RuntimeError("Validation overlaps prospective blind OOS")
    if PRIMARY_HORIZON not in PRIMARY_HORIZONS or len(PRIMARY_HORIZONS) != 6:
        raise RuntimeError("primary horizon contract drift")
    if (
        tuple(feature for _, members in FEATURE_FAMILY_MEMBERSHIP for feature in members)
        != FEATURE_ORDER
    ):
        raise RuntimeError("feature-family mapping must preserve exact feature order")
    if len(FEATURE_ORDER) != 15 or len(set(FEATURE_ORDER)) != 15:
        raise RuntimeError("experiment requires exactly 15 unique features")
    if TRADING_CALCULATIONS_ALLOWED:
        raise RuntimeError("trading calculations are forbidden")


validate_contract()
