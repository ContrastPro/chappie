"""Immutable Research-only execution primitives for the Tsunami forecast experiment."""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from itertools import pairwise
from pathlib import Path

from .free_v2_analytical_view import ANALYTICAL_ROOT, ANALYTICAL_VIEW_IDENTITY
from .free_v2_materializer import FEATURE_ORDER
from .tsunami_forecast_contract import (
    ABLATION_METRICS,
    BASELINES,
    DIRECTION_CONFIDENCE_BINS,
    EXPERIMENT_IDENTITY,
    FEATURE_FAMILY_MEMBERSHIP,
    PRIMARY_HORIZON,
    PRIMARY_HORIZONS,
    PURGE_DURATION,
    RARE_EVENT_CALIBRATION_BINS,
    REQUIRED_PROBABILITIES,
    RESEARCH_END_EXCLUSIVE,
    RESEARCH_START,
    WALK_FORWARD_FOLDS,
)
from .tsunami_research_targets import RESEARCH_TARGET_IDENTITY, RESEARCH_TARGET_ROOT

RESEARCH_IDENTITY = "CHAPPIE_CAPITAL_FLOW_TSUNAMI_PREDICTIVE_RESEARCH/1.0.0"
ANALYTICAL_SEMANTIC_DIGEST = "46b7762bbee33d6831ab77d6525fc1f539f0db5505e9af13d1c3fb5fda8b7243"
TARGET_SEMANTIC_DIGEST = "11d4323fecc384665cfff30c619598f95337252f155f703af9a9d1b28888eee8"
TARGET_MANIFEST_SEMANTIC_DIGEST = "21f2a3e914528c13b2dd9ef28972969d56ad74b145622566e95f35dfb0a1c682"
RESULT_ROOT = Path("E:/projects/chappie/data/research/experiments/capital_flow/tsunami_v1/research")
RESULT_IDENTITY = "CHAPPIE_CAPITAL_FLOW_TSUNAMI_RESEARCH_RESULT/1.0.0"
VALIDATION_FREEZE_IDENTITY = "VALIDATION_CANDIDATE_FREEZE/1.0.0"
SEVEN_DAY_MINUTES = 10_080
PERMUTATION_REPLICATES = 20
BOOTSTRAP_REPLICATES = 2_000
CONFIDENCE_LEVEL = 0.95
HOLM_ALPHA = 0.05
BH_FDR_Q = 0.10
RANDOM_SEEDS = (20240101, 20240601, 20250601)
NO_TEMPORAL_SHUFFLE = True


class ResearchTask(StrEnum):
    DIRECTION = "DIRECTION"
    MOVE = "MOVE"
    STRESS = "STRESS"
    TSUNAMI = "TSUNAMI"
    UP_TSUNAMI = "UP_TSUNAMI"
    DOWN_TSUNAMI = "DOWN_TSUNAMI"


class ModelKind(StrEnum):
    MAJORITY = "majority_class"
    FREQUENCY = "historical_frequency_probability"
    BTC_ONLY = "btc_only_causal_price_volatility"
    PERSISTENCE = "causal_persistence_regime"
    LOGISTIC = "logistic_baseline"
    REGULARIZED_LOGISTIC = "regularized_logistic"
    TREE_BOOSTING = "deterministic_tree_boosting"
    INTERACTION = "explicit_interaction_candidate"


MODEL_CATALOGUE = tuple(ModelKind)
TASKS = tuple(ResearchTask)
HORIZON_NAMES = tuple(f"{int(item / timedelta(hours=1))}h" for item in PRIMARY_HORIZONS)
METRIC_CATALOGUE = (
    "balanced_accuracy",
    "precision",
    "recall",
    "f1",
    "roc_auc",
    "pr_auc",
    "brier",
    "log_loss",
    "calibration",
    "false_positive_rate",
    "false_alarm_rate",
    "event_precision",
    "event_recall",
    "direction_accuracy",
)
SELECTION_RULE = (
    "select greatest six-fold mean primary metric; then smallest mean Brier; then simplest "
    "frozen model rank; then lexical hyperparameter serialization"
)
CONTRIBUTION_METHOD = (
    "held-out contiguous seven-day-block permutation; 20 deterministic permutations; "
    "signed metric deltas; normalize positive deltas only when their sum is positive"
)
BOOTSTRAP_POLICY = "seven-day moving-block bootstrap; 2,000 deterministic replicates; 95% CI"
MULTIPLE_TESTING_POLICY = "Holm FWER 0.05 primary; Benjamini-Hochberg FDR 0.10 secondary"


def _canonical(value: object, *, pretty: bool = False) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=None if pretty else (",", ":"),
        indent=2 if pretty else None,
        ensure_ascii=True,
    ).encode()


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _utc_minute(value: datetime) -> datetime:
    if (
        value.tzinfo is None
        or value.utcoffset() != timedelta(0)
        or value.second
        or value.microsecond
    ):
        raise ValueError("timestamp must be an exact UTC minute")
    return value.astimezone(UTC)


def assert_research_timestamp(value: datetime) -> datetime:
    """Fail closed at the Validation boundary."""
    value = _utc_minute(value)
    if not RESEARCH_START <= value < RESEARCH_END_EXCLUSIVE:
        raise PermissionError("Research runner forbids Validation or out-of-Research timestamps")
    return value


@dataclass(frozen=True, slots=True)
class ResearchExecutionContractV1:
    identity: str = RESEARCH_IDENTITY
    experiment_identity: str = EXPERIMENT_IDENTITY
    analytical_view_identity: str = ANALYTICAL_VIEW_IDENTITY
    analytical_semantic_digest: str = ANALYTICAL_SEMANTIC_DIGEST
    target_identity: str = RESEARCH_TARGET_IDENTITY
    target_semantic_digest: str = TARGET_SEMANTIC_DIGEST
    target_manifest_semantic_digest: str = TARGET_MANIFEST_SEMANTIC_DIGEST
    feature_order: tuple[str, ...] = FEATURE_ORDER
    horizons: tuple[str, ...] = HORIZON_NAMES
    tasks: tuple[str, ...] = tuple(item.value for item in TASKS)
    models: tuple[str, ...] = tuple(item.value for item in MODEL_CATALOGUE)
    random_seeds: tuple[int, ...] = RANDOM_SEEDS

    def __post_init__(self) -> None:
        if self.identity != RESEARCH_IDENTITY or self.experiment_identity != EXPERIMENT_IDENTITY:
            raise ValueError("predictive Research identity binding mismatch")
        if self.feature_order != FEATURE_ORDER or len(self.feature_order) != 15:
            raise ValueError("predictive Research requires the exact 15-feature order")
        if self.horizons != HORIZON_NAMES or len(WALK_FORWARD_FOLDS) != 6:
            raise ValueError("predictive Research horizon/fold binding mismatch")
        if PURGE_DURATION != timedelta(hours=72) or not NO_TEMPORAL_SHUFFLE:
            raise ValueError("predictive Research temporal policy mismatch")

    def payload(self) -> dict[str, object]:
        return {
            **asdict(self),
            "feature_order": list(self.feature_order),
            "horizons": list(self.horizons),
            "tasks": list(self.tasks),
            "models": list(self.models),
            "random_seeds": list(self.random_seeds),
            "walk_forward_folds": [
                {
                    "identity": fold.identity,
                    "train_start": fold.train_start.isoformat(),
                    "train_end_exclusive": fold.train_end_exclusive.isoformat(),
                    "assessment_start": fold.assessment_start.isoformat(),
                    "assessment_end_exclusive": fold.assessment_end_exclusive.isoformat(),
                }
                for fold in WALK_FORWARD_FOLDS
            ],
            "purge_embargo_hours": 72,
            "primary_horizon": "24h",
            "metrics": list(METRIC_CATALOGUE),
            "baselines": list(BASELINES),
            "selection_rule": SELECTION_RULE,
            "contribution_method": CONTRIBUTION_METHOD,
            "bootstrap_policy": BOOTSTRAP_POLICY,
            "multiple_testing_policy": MULTIPLE_TESTING_POLICY,
        }


PRODUCTION_CONTRACT = ResearchExecutionContractV1()
RESEARCH_CONTRACT_DIGEST = _digest(PRODUCTION_CONTRACT.payload())


def verify_bound_manifests(analytical_root: Path, target_root: Path) -> tuple[dict, dict]:
    """Verify only immutable manifest bindings; never open Validation or source NDJSON."""
    analytical = json.loads((analytical_root / "manifest.json").read_text(encoding="utf-8"))
    target = json.loads((target_root / "manifest.json").read_text(encoding="utf-8"))
    if (
        analytical.get("analytical_view_identity") != ANALYTICAL_VIEW_IDENTITY
        or analytical.get("analytical_semantic_digest") != ANALYTICAL_SEMANTIC_DIGEST
    ):
        raise ValueError("analytical input binding mismatch")
    if (
        target.get("target_identity") != RESEARCH_TARGET_IDENTITY
        or target.get("target_semantic_digest") != TARGET_SEMANTIC_DIGEST
        or target.get("manifest_semantic_digest") != TARGET_MANIFEST_SEMANTIC_DIGEST
    ):
        raise ValueError("Research target input binding mismatch")
    return analytical, target


@dataclass(frozen=True, slots=True)
class ResearchInputSelectionV1:
    feature_glob: str
    target_glob: str
    horizon: str
    fold_identity: str
    phase: str
    start_inclusive: datetime
    end_exclusive: datetime
    columns: tuple[str, ...]

    def __post_init__(self) -> None:
        assert_research_timestamp(self.start_inclusive)
        if (
            self.end_exclusive > RESEARCH_END_EXCLUSIVE
            or self.end_exclusive <= self.start_inclusive
        ):
            raise PermissionError("selection may not enter Validation")
        if self.phase not in {"TRAIN", "ASSESSMENT"} or self.horizon not in HORIZON_NAMES:
            raise ValueError("invalid Research selection")

    def duckdb_sql(self) -> str:
        columns = ", ".join(f'f."{name}"' for name in self.columns)
        return (
            f"SELECT {columns} FROM read_parquet('{self.feature_glob}') f "
            f"JOIN read_parquet('{self.target_glob}') t USING (timestamp_utc) "
            f"WHERE f.timestamp_utc >= TIMESTAMPTZ '{self.start_inclusive.isoformat()}' "
            f"AND f.timestamp_utc < TIMESTAMPTZ '{self.end_exclusive.isoformat()}' "
            "ORDER BY f.timestamp_utc"
        )


def research_selection(
    fold_identity: str,
    phase: str,
    horizon: str,
    *,
    analytical_root: Path = ANALYTICAL_ROOT,
    target_root: Path = RESEARCH_TARGET_ROOT,
) -> ResearchInputSelectionV1:
    fold = next((item for item in WALK_FORWARD_FOLDS if item.identity == fold_identity), None)
    if fold is None:
        raise ValueError("unknown frozen fold")
    if phase == "TRAIN":
        start, end = fold.train_start, fold.train_end_exclusive
    elif phase == "ASSESSMENT":
        start, end = fold.assessment_start, fold.assessment_end_exclusive
    else:
        raise ValueError("phase must be TRAIN or ASSESSMENT")
    hour = int(horizon.removesuffix("h")) if horizon.endswith("h") else -1
    if horizon not in HORIZON_NAMES:
        raise ValueError("unknown frozen horizon")
    feature_glob = (analytical_root / "partitions/year=*/month=*/data.parquet").as_posix()
    target_glob = (target_root / f"horizon={hour}h/year=*/month=*/data.parquet").as_posix()
    columns = ("timestamp_utc", *FEATURE_ORDER)
    columns += tuple(name + "__state" for name in FEATURE_ORDER)
    columns += (
        "direction_class",
        "move_flag",
        "stress_flag",
        "tsunami_flag",
        "up_tsunami_flag",
        "down_tsunami_flag",
        "target_state",
        "entry_open",
        "sigma_h",
    )
    return ResearchInputSelectionV1(
        feature_glob, target_glob, horizon, fold_identity, phase, start, end, columns
    )


def validate_probabilities(probabilities: Mapping[str, float]) -> None:
    if tuple(probabilities) != REQUIRED_PROBABILITIES:
        raise ValueError("probability output order/schema mismatch")
    if any(not math.isfinite(value) or not 0 <= value <= 1 for value in probabilities.values()):
        raise ValueError("probabilities must be finite within [0,1]")
    if not math.isclose(
        sum(probabilities[name] for name in ("P(UP)", "P(DOWN)", "P(NO_MOVE)")),
        1.0,
        abs_tol=1e-12,
    ):
        raise ValueError("direction probabilities must sum to one")
    if not (probabilities["P(TSUNAMI)"] <= probabilities["P(STRESS)"] <= probabilities["P(MOVE)"]):
        raise ValueError("nested event probabilities are incoherent")
    if not math.isclose(
        probabilities["P(UP_TSUNAMI)"] + probabilities["P(DOWN_TSUNAMI)"],
        probabilities["P(TSUNAMI)"],
        abs_tol=1e-12,
    ):
        raise ValueError("directional Tsunami probabilities must reconcile")


def historical_frequency_probabilities(labels: Sequence[str]) -> dict[str, float]:
    """Training-only baseline probabilities with no assessment-label access."""
    if not labels:
        raise ValueError("frequency baseline needs Research training labels")
    counts = Counter(labels)
    total = len(labels)
    return {key: counts[key] / total for key in sorted(counts)}


def imputation_plan(
    rows: Sequence[Mapping[str, float | int | None]],
) -> dict[str, float]:
    """Fit deterministic numeric medians on the training fold only."""
    result = {}
    for feature in FEATURE_ORDER:
        values = sorted(float(row[feature]) for row in rows if row.get(feature) is not None)
        if not values:
            result[feature] = 0.0
        else:
            middle = len(values) // 2
            result[feature] = (
                values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2
            )
    return result


@dataclass(frozen=True, slots=True)
class FittedLogisticV1:
    intercept: float
    coefficients: tuple[float, ...]

    def predict_probability(self, row: Sequence[float]) -> float:
        if len(row) != len(self.coefficients):
            raise ValueError("model row width mismatch")
        score = self.intercept + sum(
            coefficient * value for coefficient, value in zip(self.coefficients, row, strict=True)
        )
        if score >= 0:
            return 1 / (1 + math.exp(-score))
        exponential = math.exp(score)
        return exponential / (1 + exponential)


def fit_logistic(
    rows: Sequence[Sequence[float]],
    labels: Sequence[int],
    *,
    l2: float,
    iterations: int = 200,
    learning_rate: float = 0.05,
) -> FittedLogisticV1:
    """Fit deterministic batch-gradient logistic regression on fold-preprocessed inputs."""
    if not rows or len(rows) != len(labels) or any(label not in (0, 1) for label in labels):
        raise ValueError("logistic training inputs are invalid")
    width = len(rows[0])
    if not width or any(len(row) != width for row in rows) or l2 < 0:
        raise ValueError("logistic training matrix or regularization is invalid")
    coefficients = [0.0] * width
    intercept = 0.0
    scale = learning_rate / len(rows)
    for _ in range(iterations):
        intercept_gradient = 0.0
        gradients = [0.0] * width
        for row, label in zip(rows, labels, strict=True):
            score = intercept + sum(
                coefficient * value for coefficient, value in zip(coefficients, row, strict=True)
            )
            probability = 1 / (1 + math.exp(-max(-40.0, min(40.0, score))))
            error = probability - label
            intercept_gradient += error
            for index, value in enumerate(row):
                gradients[index] += error * value
        intercept -= scale * intercept_gradient
        coefficients = [
            value - scale * (gradient + l2 * value)
            for value, gradient in zip(coefficients, gradients, strict=True)
        ]
    return FittedLogisticV1(intercept, tuple(coefficients))


@dataclass(frozen=True, slots=True)
class DecisionStumpV1:
    feature_index: int
    threshold: float
    below_probability: float
    above_probability: float

    def predict_probability(self, row: Sequence[float]) -> float:
        return (
            self.below_probability
            if row[self.feature_index] <= self.threshold
            else self.above_probability
        )


def fit_deterministic_stump(
    rows: Sequence[Sequence[float]], labels: Sequence[int]
) -> DecisionStumpV1:
    """Fit the frozen deterministic one-level tree candidate with stable tie-breaking."""
    if not rows or len(rows) != len(labels):
        raise ValueError("tree training inputs are invalid")
    width = len(rows[0])
    candidates: list[tuple[float, int, float, float, float]] = []
    for feature_index in range(width):
        values = sorted({float(row[feature_index]) for row in rows})
        thresholds = (
            values if len(values) == 1 else [(left + right) / 2 for left, right in pairwise(values)]
        )
        for threshold in thresholds:
            below = [
                label
                for row, label in zip(rows, labels, strict=True)
                if row[feature_index] <= threshold
            ]
            above = [
                label
                for row, label in zip(rows, labels, strict=True)
                if row[feature_index] > threshold
            ]
            below_probability = (sum(below) + 1) / (len(below) + 2)
            above_probability = (sum(above) + 1) / (len(above) + 2)
            loss = sum(
                (
                    label
                    - (below_probability if row[feature_index] <= threshold else above_probability)
                )
                ** 2
                for row, label in zip(rows, labels, strict=True)
            )
            candidates.append(
                (loss, feature_index, threshold, below_probability, above_probability)
            )
    _, feature_index, threshold, below_probability, above_probability = min(candidates)
    return DecisionStumpV1(feature_index, threshold, below_probability, above_probability)


INTERACTION_PAIRS = tuple(
    (FEATURE_ORDER.index(members[0]), FEATURE_ORDER.index(members[1]))
    for _, members in FEATURE_FAMILY_MEMBERSHIP
    if len(members) >= 2
)


def interaction_row(row: Sequence[float]) -> tuple[float, ...]:
    """Add only predeclared within-family pair interactions, isolated from additive models."""
    if len(row) != len(FEATURE_ORDER):
        raise ValueError("interaction input requires exact feature order")
    return (*row, *(row[left] * row[right] for left, right in INTERACTION_PAIRS))


def _rank_average(values: Sequence[float]) -> list[float]:
    ordered = sorted(range(len(values)), key=lambda index: (values[index], index))
    ranks = [0.0] * len(values)
    cursor = 0
    while cursor < len(ordered):
        end = cursor + 1
        while end < len(ordered) and values[ordered[end]] == values[ordered[cursor]]:
            end += 1
        rank = (cursor + end - 1) / 2 + 1
        for index in ordered[cursor:end]:
            ranks[index] = rank
        cursor = end
    return ranks


def roc_auc(labels: Sequence[int], probabilities: Sequence[float]) -> float | None:
    positives = sum(labels)
    negatives = len(labels) - positives
    if not positives or not negatives:
        return None
    ranks = _rank_average(probabilities)
    return (
        sum(rank for rank, label in zip(ranks, labels, strict=True) if label)
        - positives * (positives + 1) / 2
    ) / (positives * negatives)


def pr_auc(labels: Sequence[int], probabilities: Sequence[float]) -> float | None:
    positives = sum(labels)
    if not positives:
        return None
    ordered = sorted(
        zip(probabilities, labels, strict=True), key=lambda item: item[0], reverse=True
    )
    area = 0.0
    true_positive = 0
    previous_recall = 0.0
    for index, (_, label) in enumerate(ordered, 1):
        true_positive += label
        recall = true_positive / positives
        area += (recall - previous_recall) * (true_positive / index)
        previous_recall = recall
    return area


def binary_metrics(
    labels: Sequence[int], probabilities: Sequence[float], threshold: float = 0.5
) -> dict[str, float | None]:
    if len(labels) != len(probabilities) or not labels:
        raise ValueError("metric inputs must be aligned and non-empty")
    if any(label not in (0, 1) for label in labels):
        raise ValueError("binary labels must be zero or one")
    predictions = [int(value >= threshold) for value in probabilities]
    tp = sum(a == b == 1 for a, b in zip(labels, predictions, strict=True))
    tn = sum(a == b == 0 for a, b in zip(labels, predictions, strict=True))
    fp = sum(a == 0 and b == 1 for a, b in zip(labels, predictions, strict=True))
    fn = sum(a == 1 and b == 0 for a, b in zip(labels, predictions, strict=True))
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    tnr = tn / (tn + fp) if tn + fp else None
    eps = 1e-15
    clipped = [min(max(item, eps), 1 - eps) for item in probabilities]
    return {
        "balanced_accuracy": (recall + tnr) / 2 if recall is not None and tnr is not None else None,
        "precision": precision,
        "recall": recall,
        "f1": 2 * precision * recall / (precision + recall) if precision and recall else 0.0,
        "roc_auc": roc_auc(labels, probabilities),
        "pr_auc": pr_auc(labels, probabilities),
        "brier": sum(
            (value - label) ** 2 for label, value in zip(labels, probabilities, strict=True)
        )
        / len(labels),
        "log_loss": -sum(
            label * math.log(value) + (1 - label) * math.log(1 - value)
            for label, value in zip(labels, clipped, strict=True)
        )
        / len(labels),
        "false_positive_rate": fp / (fp + tn) if fp + tn else None,
        "false_alarm_rate": fp / (tp + fp) if tp + fp else None,
    }


def calibration_table(
    labels: Sequence[int], probabilities: Sequence[float], *, rare_event: bool
) -> tuple[dict[str, float | int | None], ...]:
    bins = RARE_EVENT_CALIBRATION_BINS if rare_event else DIRECTION_CONFIDENCE_BINS
    result = []
    for lower, upper in bins:
        indexes = [
            index
            for index, value in enumerate(probabilities)
            if float(lower) <= value < float(upper) or value == float(upper) == 1.0
        ]
        predicted = (
            sum(probabilities[index] for index in indexes) / len(indexes) if indexes else None
        )
        realized = sum(labels[index] for index in indexes) / len(indexes) if indexes else None
        result.append(
            {
                "lower": float(lower),
                "upper": float(upper),
                "predicted_mean_probability": predicted,
                "realized_frequency": realized,
                "sample_count": len(indexes),
                "calibration_error": abs(predicted - realized) if predicted is not None else None,
            }
        )
    return tuple(result)


def event_metrics(
    actual_episode_ids: Sequence[str | None], predicted_positive: Sequence[bool]
) -> dict[str, float | int | None]:
    if len(actual_episode_ids) != len(predicted_positive):
        raise ValueError("event inputs must be aligned")
    actual = {item for item in actual_episode_ids if item is not None}
    detected = {
        item
        for item, prediction in zip(actual_episode_ids, predicted_positive, strict=True)
        if item is not None and prediction
    }
    false_alarms = sum(
        item is None and prediction
        for item, prediction in zip(actual_episode_ids, predicted_positive, strict=True)
    )
    predicted_events = len(detected) + false_alarms
    return {
        "actual_events": len(actual),
        "detected_events": len(detected),
        "false_alarm_events": false_alarms,
        "event_precision": len(detected) / predicted_events if predicted_events else None,
        "event_recall": len(detected) / len(actual) if actual else None,
    }


def deterministic_block_permutation(
    length: int, replicate: int, seed: int = RANDOM_SEEDS[0]
) -> tuple[int, ...]:
    blocks = [
        list(range(start, min(start + SEVEN_DAY_MINUTES, length)))
        for start in range(0, length, SEVEN_DAY_MINUTES)
    ]
    generator = random.Random(seed + replicate)
    generator.shuffle(blocks)
    return tuple(index for block in blocks for index in block)


def permutation_contribution(reference: float, permuted: Sequence[float]) -> tuple[float, str]:
    if len(permuted) != PERMUTATION_REPLICATES:
        raise ValueError("exactly 20 deterministic permutations are required")
    delta = reference - sum(permuted) / len(permuted)
    return delta, "POSITIVE" if delta > 0 else "HARMFUL" if delta < 0 else "NEUTRAL"


def normalize_positive_contributions(deltas: Mapping[str, float]) -> dict[str, float | None]:
    positive_sum = sum(max(0.0, value) for value in deltas.values())
    return {
        key: (100 * value / positive_sum if value > 0 and positive_sum else None)
        for key, value in deltas.items()
    }


def classify_ablation(delta: float, tolerance: float = 1e-12) -> str:
    return "POSITIVE" if delta > tolerance else "HARMFUL" if delta < -tolerance else "NEUTRAL"


def moving_block_bootstrap_indices(
    length: int, replicate: int, seed: int = RANDOM_SEEDS[1]
) -> tuple[int, ...]:
    if length <= 0:
        raise ValueError("bootstrap input must be non-empty")
    generator = random.Random(seed + replicate)
    starts = range(max(1, length - SEVEN_DAY_MINUTES + 1))
    indexes: list[int] = []
    while len(indexes) < length:
        start = generator.choice(starts)
        indexes.extend(range(start, min(start + SEVEN_DAY_MINUTES, length)))
    return tuple(indexes[:length])


def percentile_interval(values: Sequence[float]) -> tuple[float, float]:
    if len(values) != BOOTSTRAP_REPLICATES:
        raise ValueError("exactly 2,000 bootstrap replicates are required")
    ordered = sorted(values)
    return ordered[49], ordered[1949]


def holm_decisions(p_values: Mapping[str, float], alpha: float = HOLM_ALPHA) -> dict[str, bool]:
    ordered = sorted(p_values.items(), key=lambda item: (item[1], item[0]))
    decisions = dict.fromkeys(p_values, False)
    for index, (name, value) in enumerate(ordered):
        if value > alpha / (len(ordered) - index):
            break
        decisions[name] = True
    return decisions


def bh_decisions(p_values: Mapping[str, float], q: float = BH_FDR_Q) -> dict[str, bool]:
    ordered = sorted(p_values.items(), key=lambda item: (item[1], item[0]))
    largest = -1
    for index, (_, value) in enumerate(ordered, 1):
        if value <= q * index / len(ordered):
            largest = index
    selected = {name for name, _ in ordered[:largest]} if largest >= 0 else set()
    return {name: name in selected for name in p_values}


@dataclass(frozen=True, slots=True)
class CandidateEvidenceV1:
    model: str
    primary_metric: float
    brier: float
    hyperparameters: tuple[tuple[str, str], ...] = ()


def select_candidate(candidates: Sequence[CandidateEvidenceV1]) -> CandidateEvidenceV1:
    if not candidates:
        raise ValueError("selection requires candidates")
    rank = {model.value: index for index, model in enumerate(MODEL_CATALOGUE)}
    if any(item.model not in rank for item in candidates):
        raise ValueError("selection contains an unfrozen model")
    return min(
        candidates,
        key=lambda item: (-item.primary_metric, item.brier, rank[item.model], item.hyperparameters),
    )


@dataclass(frozen=True, slots=True)
class ValidationCandidateFreezeV1:
    selected_models: tuple[dict[str, object], ...]
    preprocessing: dict[str, object]
    thresholds: dict[str, float]
    calibration: dict[str, object]
    selection_evidence_digest: str
    contract_digest: str = RESEARCH_CONTRACT_DIGEST
    feature_order: tuple[str, ...] = FEATURE_ORDER
    horizons: tuple[str, ...] = HORIZON_NAMES

    def payload(self) -> dict[str, object]:
        return {
            "identity": VALIDATION_FREEZE_IDENTITY,
            "research_identity": RESEARCH_IDENTITY,
            "experiment_identity": EXPERIMENT_IDENTITY,
            "analytical_semantic_digest": ANALYTICAL_SEMANTIC_DIGEST,
            "target_semantic_digest": TARGET_SEMANTIC_DIGEST,
            "target_manifest_semantic_digest": TARGET_MANIFEST_SEMANTIC_DIGEST,
            **asdict(self),
            "feature_order": list(self.feature_order),
            "horizons": list(self.horizons),
        }


def publish_result_bundle(
    result: Mapping[str, object],
    freeze: ValidationCandidateFreezeV1,
    output_root: Path = RESULT_ROOT,
) -> dict[str, object]:
    """Create immutable Research artifacts; output paths never include Validation."""
    if output_root.name != "research":
        raise ValueError("Research result root must end in research")
    if output_root.exists() and any(output_root.iterdir()):
        raise FileExistsError("Research result root must be empty and create-only")
    output_root.mkdir(parents=True, exist_ok=True)
    payload = {
        "identity": RESULT_IDENTITY,
        "research_contract": PRODUCTION_CONTRACT.payload(),
        "research_contract_digest": RESEARCH_CONTRACT_DIGEST,
        "result": dict(result),
        "validation_status": "NOT_ACCESSED",
        "blind_oos_status": "NOT_ACCESSED",
    }
    payload["result_semantic_digest"] = _digest(payload)
    freeze_payload = freeze.payload()
    freeze_payload["freeze_semantic_digest"] = _digest(freeze_payload)
    for name, value in (
        ("result.json", payload),
        ("validation_candidate_freeze.json", freeze_payload),
    ):
        pending = output_root / (name + ".pending")
        pending.write_bytes(_canonical(value, pretty=True))
        os.replace(pending, output_root / name)
    manifest = {
        "identity": RESULT_IDENTITY,
        "research_identity": RESEARCH_IDENTITY,
        "result_semantic_digest": payload["result_semantic_digest"],
        "freeze_semantic_digest": freeze_payload["freeze_semantic_digest"],
        "files": ["result.json", "validation_candidate_freeze.json"],
    }
    manifest["manifest_semantic_digest"] = _digest(manifest)
    pending = output_root / "manifest.json.pending"
    pending.write_bytes(_canonical(manifest, pretty=True))
    os.replace(pending, output_root / "manifest.json")
    return manifest


def bounded_synthetic_benchmark(rows: int = 25_000) -> dict[str, float | int]:
    """Measure bounded primitives on synthetic Research-shaped rows only."""
    if not 1 <= rows <= 100_000:
        raise ValueError("benchmark must remain bounded")
    started = time.perf_counter()
    labels = [int(index % 97 == 0) for index in range(rows)]
    probabilities = [0.02 + 0.01 * math.sin(index) for index in range(rows)]
    binary_metrics(labels, probabilities)
    for replicate in range(PERMUTATION_REPLICATES):
        deterministic_block_permutation(rows, replicate)
    elapsed = time.perf_counter() - started
    return {"synthetic_rows": rows, "elapsed_seconds": elapsed, "rows_per_second": rows / elapsed}


def validate_research_contract() -> None:
    if PRIMARY_HORIZON != timedelta(hours=24) or len(FEATURE_FAMILY_MEMBERSHIP) != 8:
        raise RuntimeError("primary horizon or family catalogue drift")
    if len(ABLATION_METRICS) != 5 or PERMUTATION_REPLICATES != 20:
        raise RuntimeError("contribution/ablation contract drift")
    if BOOTSTRAP_REPLICATES != 2_000 or len(MODEL_CATALOGUE) != 8:
        raise RuntimeError("uncertainty/model catalogue drift")


validate_research_contract()
