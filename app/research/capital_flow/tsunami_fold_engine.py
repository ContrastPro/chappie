"""Genuine fold-local predictive engine for Capital Flow Tsunami Research V1."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from .free_v2_analytical_view import ANALYTICAL_ROOT, ANALYTICAL_VIEW_IDENTITY, STATE_NAME_BY_CODE
from .free_v2_materializer import FEATURE_ORDER
from .short_horizon_direction_v2_materializer import (
    FEATURE_STORE_ROOT as V2_FEATURE_STORE_ROOT,
)
from .short_horizon_direction_v2_materializer import (
    FINAL_FEATURE_ORDER as V2_FEATURE_ORDER,
)
from .short_horizon_direction_v2_materializer import (
    STATE_CODES as V2_STATE_CODES,
)
from .short_horizon_direction_v2_materializer import (
    verify_feature_store,
)
from .tsunami_forecast_contract import (
    FEATURE_FAMILY_MEMBERSHIP,
    RESEARCH_END_EXCLUSIVE,
    RESEARCH_START,
    WALK_FORWARD_FOLDS,
    WalkForwardFoldV1,
)
from .tsunami_predictive_research import (
    ANALYTICAL_SEMANTIC_DIGEST,
    HORIZON_NAMES,
    INTERACTION_PAIRS,
    MODEL_CATALOGUE,
    RANDOM_SEEDS,
    TARGET_MANIFEST_SEMANTIC_DIGEST,
    TARGET_SEMANTIC_DIGEST,
    binary_metrics,
    calibration_table,
    deterministic_block_permutation,
    event_metrics,
    verify_bound_manifests,
)
from .tsunami_research_targets import RESEARCH_TARGET_ROOT

FOLD_ENGINE_IDENTITY = "CHAPPIE_CAPITAL_FLOW_TSUNAMI_FOLD_ENGINE/1.0.0"
PREPROCESSING_IDENTITY = "TRAIN_ONLY_MEDIAN_STANDARD_SCALE_STATE_ONE_HOT/1.0.0"
CALIBRATION_IDENTITY = "TRAIN_ONLY_IDENTITY_PROBABILITY_CALIBRATION/1.0.0"
DIRECTION_CLASSES = ("UP", "DOWN", "NO_MOVE")
BINARY_TASK_COLUMNS = {
    "MOVE": "move_flag",
    "STRESS": "stress_flag",
    "TSUNAMI": "tsunami_flag",
    "UP_TSUNAMI": "up_tsunami_flag",
    "DOWN_TSUNAMI": "down_tsunami_flag",
}
REGULARIZATION_GRID = (0.01, 0.1, 1.0)
TREE_CANDIDATE = {"max_depth": 1, "laplace_smoothing": 1, "random_state": RANDOM_SEEDS[0]}
SOFTMAX_TOLERANCE = 1e-12
PREDICTION_ARTIFACT_IDENTITY = "CHAPPIE_TSUNAMI_HELD_OUT_PREDICTIONS/1.0.0"
FITTED_MODEL_ARTIFACT_IDENTITY = "CHAPPIE_TSUNAMI_FITTED_MODEL/2.0.0"
UNDEFINED = "UNAVAILABLE_UNDEFINED"


@dataclass(frozen=True, slots=True)
class FeatureSchema:
    """Immutable, versioned input binding for the shared fold-engine machinery."""

    identity: str
    dataset_identity: str
    dataset_semantic_digest: str
    manifest_semantic_digest: str
    feature_order: tuple[str, ...]
    family_mapping: dict[str, str]
    research_start: datetime
    research_end_exclusive: datetime

    @property
    def feature_count(self) -> int:
        return len(self.feature_order)


V1_FEATURE_SCHEMA = FeatureSchema(
    "CHAPPIE_CAPITAL_FLOW_TSUNAMI_FEATURE_SCHEMA/1.0.0",
    ANALYTICAL_VIEW_IDENTITY,
    ANALYTICAL_SEMANTIC_DIGEST,
    "c60ab829c5ad31c80b74b34c672f800353edb61d27c4a8eb9dd098acd3c88a21",
    FEATURE_ORDER,
    {feature: family for family, members in FEATURE_FAMILY_MEMBERSHIP for feature in members},
    RESEARCH_START,
    RESEARCH_END_EXCLUSIVE,
)
V2_FEATURE_SCHEMA = FeatureSchema(
    "CHAPPIE_SHORT_HORIZON_DIRECTION_V2_FEATURE_SCHEMA/1.0.0",
    "CHAPPIE_SHORT_HORIZON_DIRECTION_V2_FEATURE_STORE/1.0.0",
    "5f66259f5563ae625606f5a6527e3ba404b647b0346a49823e596d234dff0dfc",
    "6f95ec4eaf0699af203d386fc636588873ac1ee6f61d2275cf9d6bf992f34071",
    V2_FEATURE_ORDER,
    {
        name: "CAPITAL_FLOW"
        if index < 15
        else name.split(".", 1)[0].upper().replace("STRUCTURE", "MARKET_STRUCTURE")
        for index, name in enumerate(V2_FEATURE_ORDER)
    },
    RESEARCH_START,
    RESEARCH_END_EXCLUSIVE,
)


@dataclass(frozen=True, slots=True)
class TargetBinding:
    identity: str
    target_identity: str
    target_semantic_digest: str
    target_manifest_digest: str
    horizon: str
    task: str
    classes: tuple[str, ...]
    research_start: datetime
    research_end_exclusive: datetime
    timestamp_semantics: str
    available_state: str = "AVAILABLE"


@dataclass(frozen=True, slots=True)
class V2WalkForwardFold:
    identity: str
    train_start: datetime
    train_end_exclusive: datetime
    assessment_start: datetime
    assessment_end_exclusive: datetime
    purge: timedelta = timedelta(hours=4)
    embargo: timedelta = timedelta(hours=4)

    def __post_init__(self) -> None:
        if (
            self.train_end_exclusive + self.purge != self.assessment_start
            or self.purge != self.embargo
        ):
            raise ValueError("V2 fold requires exact four-hour purge/embargo")


V2_WALK_FORWARD_IDENTITY = "CHAPPIE_SHORT_HORIZON_DIRECTION_V2_WALK_FORWARD/1.0.0"
V2_WALK_FORWARD_FOLDS = tuple(
    V2WalkForwardFold(f"V2_WF{index}", RESEARCH_START, start - timedelta(hours=4), start, end)
    for index, (start, end) in enumerate(
        (
            (datetime(2024, 7, 1, tzinfo=UTC), datetime(2024, 9, 1, tzinfo=UTC)),
            (datetime(2024, 9, 1, tzinfo=UTC), datetime(2024, 11, 1, tzinfo=UTC)),
            (datetime(2024, 11, 1, tzinfo=UTC), datetime(2025, 1, 1, tzinfo=UTC)),
            (datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 3, 1, tzinfo=UTC)),
            (datetime(2025, 3, 1, tzinfo=UTC), datetime(2025, 4, 15, tzinfo=UTC)),
            (datetime(2025, 4, 15, tzinfo=UTC), RESEARCH_END_EXCLUSIVE),
        ),
        1,
    )
)


_V2_TARGET_IDENTITY = "CHAPPIE_CAPITAL_FLOW_TSUNAMI_RESEARCH_TARGETS/1.0.0"
_V2_TARGET_DIGEST = "11d4323fecc384665cfff30c619598f95337252f155f703af9a9d1b28888eee8"
_V2_TARGET_MANIFEST_DIGEST = "21f2a3e914528c13b2dd9ef28972969d56ad74b145622566e95f35dfb0a1c682"
V2_DIRECTION_1H_TARGET_BINDING = TargetBinding(
    "CHAPPIE_SHORT_HORIZON_DIRECTION_V2_TARGET_BINDING/1h/1.0.0",
    _V2_TARGET_IDENTITY,
    _V2_TARGET_DIGEST,
    _V2_TARGET_MANIFEST_DIGEST,
    "1h",
    "DIRECTION",
    DIRECTION_CLASSES,
    RESEARCH_START,
    RESEARCH_END_EXCLUSIVE,
    "EXACT_FEATURE_TIMESTAMP",
)
V2_DIRECTION_4H_TARGET_BINDING = TargetBinding(
    "CHAPPIE_SHORT_HORIZON_DIRECTION_V2_TARGET_BINDING/4h/1.0.0",
    _V2_TARGET_IDENTITY,
    _V2_TARGET_DIGEST,
    _V2_TARGET_MANIFEST_DIGEST,
    "4h",
    "DIRECTION",
    DIRECTION_CLASSES,
    RESEARCH_START,
    RESEARCH_END_EXCLUSIVE,
    "EXACT_FEATURE_TIMESTAMP",
)


def validate_target_binding(
    binding: TargetBinding, target_root: Path = RESEARCH_TARGET_ROOT
) -> None:
    payload = json.loads((target_root / "manifest.json").read_text(encoding="utf-8"))
    if (
        payload.get("target_identity"),
        payload.get("target_semantic_digest"),
        payload.get("manifest_semantic_digest"),
    ) != (binding.target_identity, binding.target_semantic_digest, binding.target_manifest_digest):
        raise ValueError("target authority digest binding mismatch")
    if (
        binding.task != "DIRECTION"
        or binding.horizon not in ("1h", "4h")
        or binding.classes != DIRECTION_CLASSES
    ):
        raise ValueError("target binding task/horizon/classes mismatch")
    if (payload.get("research_start"), payload.get("research_end_exclusive")) != (
        binding.research_start.isoformat(),
        binding.research_end_exclusive.isoformat(),
    ):
        raise ValueError("target Research bounds mismatch")


def schema_projection(schema: FeatureSchema, family: str) -> tuple[int, ...]:
    if family == "FULL_V2" and schema is V2_FEATURE_SCHEMA:
        return tuple(range(schema.feature_count))
    return tuple(
        index
        for index, name in enumerate(schema.feature_order)
        if schema.family_mapping[name] == family
    )


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class FoldDatasetV1:
    timestamps: tuple[datetime, ...]
    values: tuple[tuple[float | None, ...], ...]
    states: tuple[tuple[int, ...], ...]
    labels: tuple[str | int, ...]
    btc_inputs: tuple[tuple[float, float], ...]
    fold_identity: str
    phase: Literal["TRAIN", "ASSESSMENT"]
    horizon: str
    task: str
    schema: FeatureSchema = V1_FEATURE_SCHEMA

    def __post_init__(self) -> None:
        count = len(self.timestamps)
        if not count or not all(
            len(item) == count for item in (self.values, self.states, self.labels, self.btc_inputs)
        ):
            raise ValueError("fold dataset columns must be aligned and non-empty")
        if any(item >= self.schema.research_end_exclusive for item in self.timestamps):
            raise PermissionError("fold dataset may not enter Validation")
        if any(len(row) != self.schema.feature_count for row in self.values + self.states):
            raise ValueError("fold dataset requires exact schema feature order")


def _fold(identity: str) -> WalkForwardFoldV1:
    result = next((item for item in WALK_FORWARD_FOLDS if item.identity == identity), None)
    if result is None:
        raise ValueError("unknown frozen fold")
    return result


def _bounds(fold: WalkForwardFoldV1, phase: str, horizon: str) -> tuple[datetime, datetime]:
    hours = int(horizon.removesuffix("h")) if horizon in HORIZON_NAMES else -1
    if phase == "TRAIN":
        start, end = fold.train_start, fold.train_end_exclusive
    elif phase == "ASSESSMENT":
        start, end = fold.assessment_start, fold.assessment_end_exclusive
    else:
        raise ValueError("phase must be TRAIN or ASSESSMENT")
    end -= timedelta(hours=hours)
    if end <= start or end >= RESEARCH_END_EXCLUSIVE:
        raise PermissionError("fold target containment may not enter Validation")
    return start, end


def load_fold_dataset(
    fold_identity: str,
    phase: Literal["TRAIN", "ASSESSMENT"],
    horizon: str,
    task: str,
    *,
    analytical_root: Path = ANALYTICAL_ROOT,
    target_root: Path = RESEARCH_TARGET_ROOT,
) -> FoldDatasetV1:
    """Load and exactly join compact Research Parquet for one contained fold partition."""
    verify_bound_manifests(analytical_root, target_root)
    if task != "DIRECTION" and task not in BINARY_TASK_COLUMNS:
        raise ValueError("unknown frozen task")
    fold = _fold(fold_identity)
    start, end = _bounds(fold, phase, horizon)
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.dataset as ds

    feature_paths = sorted((analytical_root / "partitions").glob("year=*/month=*/data.parquet"))
    target_paths = sorted((target_root / f"horizon={horizon}").glob("year=*/month=*/data.parquet"))
    if not feature_paths or not target_paths:
        raise FileNotFoundError("compact Research Parquet partitions are missing")
    timestamp_filter = (ds.field("timestamp_utc") >= pa.scalar(start)) & (
        ds.field("timestamp_utc") < pa.scalar(end)
    )
    feature_columns = ["timestamp_utc"]
    for name in FEATURE_ORDER:
        feature_columns.extend((name, name + "__state"))
    target_column = "direction_class" if task == "DIRECTION" else BINARY_TASK_COLUMNS[task]
    features = ds.dataset([str(item) for item in feature_paths], format="parquet").to_table(
        columns=feature_columns, filter=timestamp_filter
    )
    targets = ds.dataset([str(item) for item in target_paths], format="parquet").to_table(
        columns=["timestamp_utc", "target_state", target_column, "entry_open", "sigma_h"],
        filter=timestamp_filter,
    )
    targets = targets.filter(pc.equal(targets["target_state"], "AVAILABLE"))
    feature_map = {
        item.as_py(): index for index, item in enumerate(features["timestamp_utc"].combine_chunks())
    }
    target_dict = targets.to_pydict()
    timestamps: list[datetime] = []
    values: list[tuple[float | None, ...]] = []
    states: list[tuple[int, ...]] = []
    labels: list[str | int] = []
    btc_inputs: list[tuple[float, float]] = []
    feature_dict = features.to_pydict()
    for target_index, timestamp in enumerate(target_dict["timestamp_utc"]):
        timestamp = timestamp.astimezone(UTC)
        if timestamp >= RESEARCH_END_EXCLUSIVE or timestamp not in feature_map:
            raise PermissionError("target/feature timestamp join crossed Research boundary")
        feature_index = feature_map[timestamp]
        row_values = tuple(feature_dict[name][feature_index] for name in FEATURE_ORDER)
        row_states = tuple(feature_dict[name + "__state"][feature_index] for name in FEATURE_ORDER)
        if any(code not in STATE_NAME_BY_CODE for code in row_states):
            raise ValueError("invalid analytical feature state")
        if any(value is not None and not math.isfinite(value) for value in row_values):
            raise ValueError("nonfinite analytical feature")
        label = target_dict[target_column][target_index]
        entry_open = target_dict["entry_open"][target_index]
        sigma_h = target_dict["sigma_h"][target_index]
        if label is None or entry_open is None or sigma_h is None:
            raise ValueError("AVAILABLE target contains null modeling evidence")
        timestamps.append(timestamp)
        values.append(row_values)
        states.append(row_states)
        labels.append(str(label) if task == "DIRECTION" else int(bool(label)))
        btc_inputs.append((float(entry_open), float(sigma_h)))
    if timestamps != sorted(timestamps) or len(timestamps) != len(set(timestamps)):
        raise ValueError("fold join must be unique and time ordered")
    return FoldDatasetV1(
        tuple(timestamps),
        tuple(values),
        tuple(states),
        tuple(labels),
        tuple(btc_inputs),
        fold_identity,
        phase,
        horizon,
        task,
    )


def load_v2_fold_dataset(
    fold_identity: str,
    phase: Literal["TRAIN", "ASSESSMENT"],
    horizon: str,
    *,
    target_root: Path = RESEARCH_TARGET_ROOT,
    feature_root: Path = V2_FEATURE_STORE_ROOT,
) -> FoldDatasetV1:
    """Load V2 features with the existing Research-only target binding and shared dataset type."""
    evidence = verify_feature_store(feature_root)
    if (
        evidence["dataset_semantic_digest"] != V2_FEATURE_SCHEMA.dataset_semantic_digest
        or evidence["manifest_semantic_digest"] != V2_FEATURE_SCHEMA.manifest_semantic_digest
    ):
        raise ValueError("V2 feature-store digest binding mismatch")
    binding = (
        V2_DIRECTION_1H_TARGET_BINDING
        if horizon == "1h"
        else V2_DIRECTION_4H_TARGET_BINDING
        if horizon == "4h"
        else None
    )
    if binding is None:
        raise ValueError("V2 direction binding supports only 1h/4h")
    validate_target_binding(binding, target_root)
    fold = next((item for item in V2_WALK_FORWARD_FOLDS if item.identity == fold_identity), None)
    if fold is None:
        raise ValueError("V2 execution requires an explicit V2 frozen fold")
    start, end = _bounds(fold, phase, horizon)
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.dataset as ds

    paths = sorted((feature_root / "experiment_matrix").glob("year=*/month=*/data.parquet"))
    target_paths = sorted((target_root / f"horizon={horizon}").glob("year=*/month=*/data.parquet"))
    columns = ["timestamp_utc"] + [
        column for name in V2_FEATURE_SCHEMA.feature_order for column in (name, name + "__state")
    ]
    predicate = (ds.field("timestamp_utc") >= pa.scalar(start)) & (
        ds.field("timestamp_utc") < pa.scalar(end)
    )
    features = ds.dataset([str(path) for path in paths], format="parquet").to_table(
        columns=columns, filter=predicate
    )
    targets = ds.dataset([str(path) for path in target_paths], format="parquet").to_table(
        columns=["timestamp_utc", "target_state", "direction_class", "entry_open", "sigma_h"],
        filter=predicate,
    )
    targets = targets.filter(pc.equal(targets["target_state"], "AVAILABLE"))
    source = features.to_pydict()
    target = targets.to_pydict()
    if len(source["timestamp_utc"]) != len(set(source["timestamp_utc"])) or len(
        target["timestamp_utc"]
    ) != len(set(target["timestamp_utc"])):
        raise ValueError("V2 feature/target timestamp must be unique")
    lookup = {value: index for index, value in enumerate(source["timestamp_utc"])}
    timestamps = []
    values = []
    states = []
    labels = []
    btc = []
    for index, timestamp in enumerate(target["timestamp_utc"]):
        if timestamp not in lookup or timestamp >= binding.research_end_exclusive:
            raise PermissionError("V2 target containment failure")
        row = lookup[timestamp]
        row_values = tuple(source[name][row] for name in V2_FEATURE_SCHEMA.feature_order)
        row_states = tuple(
            source[name + "__state"][row] for name in V2_FEATURE_SCHEMA.feature_order
        )
        if any(code not in V2_STATE_CODES.values() for code in row_states):
            raise ValueError("invalid V2 feature state")
        timestamps.append(timestamp.astimezone(UTC))
        values.append(row_values)
        states.append(row_states)
        label = str(target["direction_class"][index])
        if label not in binding.classes:
            raise ValueError("invalid V2 Direction target class")
        labels.append(label)
        btc.append((float(target["entry_open"][index]), float(target["sigma_h"][index])))
    return FoldDatasetV1(
        tuple(timestamps),
        tuple(values),
        tuple(states),
        tuple(labels),
        tuple(btc),
        fold_identity,
        phase,
        horizon,
        "DIRECTION",
        V2_FEATURE_SCHEMA,
    )


@dataclass(frozen=True, slots=True)
class PreprocessingV1:
    medians: tuple[float, ...]
    means: tuple[float, ...]
    scales: tuple[float, ...]
    state_cardinality: int = len(STATE_NAME_BY_CODE)
    identity: str = PREPROCESSING_IDENTITY

    @classmethod
    def fit(cls, dataset: FoldDatasetV1, feature_indexes: tuple[int, ...]) -> PreprocessingV1:
        import numpy as np

        raw = np.asarray(
            [
                [np.nan if row[index] is None else row[index] for index in feature_indexes]
                for row in dataset.values
            ],
            dtype=float,
        )
        medians = np.nanmedian(raw, axis=0)
        medians = np.where(np.isfinite(medians), medians, 0.0)
        filled = np.where(np.isnan(raw), medians, raw)
        means = filled.mean(axis=0)
        scales = filled.std(axis=0)
        scales = np.where(scales > 0, scales, 1.0)
        return cls(tuple(medians.tolist()), tuple(means.tolist()), tuple(scales.tolist()))

    def transform(
        self,
        dataset: FoldDatasetV1,
        feature_indexes: tuple[int, ...],
        *,
        interactions: bool = False,
    ):
        import numpy as np

        raw = np.asarray(
            [
                [np.nan if row[index] is None else row[index] for index in feature_indexes]
                for row in dataset.values
            ],
            dtype=float,
        )
        medians = np.asarray(self.medians)
        numeric = (np.where(np.isnan(raw), medians, raw) - np.asarray(self.means)) / np.asarray(
            self.scales
        )
        states = np.asarray(
            [[row[index] for index in feature_indexes] for row in dataset.states], dtype=int
        )
        if interactions:
            if feature_indexes != tuple(range(len(FEATURE_ORDER))):
                raise ValueError("interaction candidate requires the exact full feature set")
            numeric_width = len(feature_indexes)
            state_width = numeric_width * self.state_cardinality
            result = np.zeros(
                (len(states), numeric_width + state_width + len(INTERACTION_PAIRS)), dtype=float
            )
            result[:, :numeric_width] = numeric
            rows = np.arange(len(states))
            for feature_offset in range(numeric_width):
                result[
                    rows,
                    numeric_width
                    + feature_offset * self.state_cardinality
                    + states[:, feature_offset],
                ] = 1.0
            for interaction_offset, (left, right) in enumerate(INTERACTION_PAIRS):
                result[:, numeric_width + state_width + interaction_offset] = (
                    numeric[:, left] * numeric[:, right]
                )
        else:
            one_hot = np.eye(self.state_cardinality, dtype=float)[states].reshape(len(states), -1)
            result = np.concatenate((numeric, one_hot), axis=1)
        if not np.isfinite(result).all():
            raise ValueError("preprocessing produced nonfinite matrix")
        return result

    @property
    def digest(self) -> str:
        return _digest(asdict(self))


def preprocessing_binding_digest(dataset: FoldDatasetV1, feature_indexes: tuple[int, ...]) -> str:
    """Versioned V2-safe identity; V1's historic PreprocessingV1.digest is unchanged."""
    selected = tuple(dataset.schema.feature_order[index] for index in feature_indexes)
    return _digest(
        {
            "identity": PREPROCESSING_IDENTITY,
            "schema_identity": dataset.schema.identity,
            "dataset_semantic_digest": dataset.schema.dataset_semantic_digest,
            "manifest_semantic_digest": dataset.schema.manifest_semantic_digest,
            "feature_order": selected,
            "state_order": selected,
            "fold": dataset.fold_identity,
            "horizon": dataset.horizon,
            "phase": dataset.phase,
            "train_only": True,
            "state_encoding": "ONE_HOT_DETERMINISTIC",
        }
    )


@dataclass(frozen=True, slots=True)
class ProbabilityModelV1:
    model: str
    classes: tuple[str, ...]
    coefficients: tuple[tuple[float, ...], ...]
    intercepts: tuple[float, ...]
    hyperparameters: tuple[tuple[str, str], ...]
    preprocessing_digest: str
    feature_indexes: tuple[int, ...]

    @property
    def digest(self) -> str:
        return _digest(asdict(self))


@dataclass(frozen=True, slots=True)
class TreeProbabilityModelV1:
    feature_index: int
    threshold: float
    classes: tuple[str, ...]
    below_probabilities: tuple[float, ...]
    above_probabilities: tuple[float, ...]
    preprocessing_digest: str
    feature_indexes: tuple[int, ...]
    hyperparameters: tuple[tuple[str, str], ...] = tuple(
        (key, str(value)) for key, value in TREE_CANDIDATE.items()
    )
    model: str = "deterministic_tree_boosting"

    @property
    def digest(self) -> str:
        return _digest(asdict(self))


@dataclass(frozen=True, slots=True)
class BaselineProbabilityModelV2:
    model: str
    classes: tuple[str, ...]
    probabilities: tuple[float, ...]
    preprocessing_digest: str = "NOT_APPLICABLE"
    feature_indexes: tuple[int, ...] = ()

    @property
    def digest(self) -> str:
        return _digest(asdict(self))


@dataclass(frozen=True, slots=True)
class BtcBaselineModelV2:
    model: str
    classes: tuple[str, ...]
    coefficients: tuple[tuple[float, ...], ...]
    intercepts: tuple[float, ...]
    hyperparameters: tuple[tuple[str, str], ...]
    means: tuple[float, float]
    scales: tuple[float, float]
    preprocessing_digest: str
    feature_indexes: tuple[int, ...] = (0, 1)
    btc_feature_order: tuple[str, str] = ("entry_open", "sigma_h")

    @property
    def digest(self) -> str:
        return _digest(asdict(self))


ReloadableModel = (
    ProbabilityModelV1 | TreeProbabilityModelV1 | BaselineProbabilityModelV2 | BtcBaselineModelV2
)


def serialize_fitted_model(
    model: ReloadableModel,
    *,
    training_fold_identity: str,
    task: str | None = None,
    horizon: str | None = None,
    calibration_digest: str = CALIBRATION_IDENTITY,
    schema: FeatureSchema = V1_FEATURE_SCHEMA,
    target_binding: TargetBinding | None = None,
) -> dict[str, object]:
    """Return canonical, path/time-free fitted state sufficient for prediction."""
    valid_folds = {
        item.identity
        for item in (V2_WALK_FORWARD_FOLDS if schema is V2_FEATURE_SCHEMA else WALK_FORWARD_FOLDS)
    }
    if training_fold_identity not in valid_folds:
        raise ValueError("fitted model requires an exact frozen training fold")
    if isinstance(model, (BaselineProbabilityModelV2, BtcBaselineModelV2)) and (
        task is None or horizon is None
    ):
        raise ValueError("baseline fitted artifact requires task and horizon bindings")
    kind = (
        "LOGISTIC"
        if isinstance(model, ProbabilityModelV1)
        else "TREE"
        if isinstance(model, TreeProbabilityModelV1)
        else "BTC_BASELINE"
        if isinstance(model, BtcBaselineModelV2)
        else "BASELINE"
    )
    payload: dict[str, object] = {
        "identity": FITTED_MODEL_ARTIFACT_IDENTITY,
        "kind": kind,
        "model_family": model.model,
        "classes": list(model.classes),
        "parameters": asdict(model),
        "preprocessing_digest": model.preprocessing_digest,
        "calibration_digest": calibration_digest,
        "feature_order": list(schema.feature_order),
        "training_fold_identity": training_fold_identity,
        "task": task,
        "horizon": horizon,
        "analytical_semantic_digest": ANALYTICAL_SEMANTIC_DIGEST,
        "target_semantic_digest": TARGET_SEMANTIC_DIGEST,
        "target_manifest_semantic_digest": TARGET_MANIFEST_SEMANTIC_DIGEST,
        "numerical_representation": "IEEE_754_BINARY64",
    }
    if schema is not V1_FEATURE_SCHEMA:
        payload.update(
            {
                "feature_schema_identity": schema.identity,
                "dataset_semantic_digest": schema.dataset_semantic_digest,
                "manifest_semantic_digest": schema.manifest_semantic_digest,
            }
        )
        if target_binding is None:
            raise ValueError("V2 artifact requires explicit target binding")
        payload.update(
            {
                "fold_schedule_identity": V2_WALK_FORWARD_IDENTITY,
                "target_semantic_digest": target_binding.target_semantic_digest,
                "target_manifest_semantic_digest": target_binding.target_manifest_digest,
            }
        )
    if target_binding is not None:
        payload["target_binding_identity"] = target_binding.identity
    if model.model == "explicit_interaction_candidate":
        payload["interaction_specification"] = "EXACT_PREDECLARED_WITHIN_FAMILY_PAIRS"
    payload["model_semantic_digest"] = _digest(payload)
    return payload


def load_fitted_model(
    artifact: dict[str, object],
    *,
    expected_preprocessing_digest: str | None = None,
    expected_fold_identity: str | None = None,
    feature_schema: FeatureSchema = V1_FEATURE_SCHEMA,
    target_binding: TargetBinding | None = None,
) -> ReloadableModel:
    """Validate all bindings and reconstruct fitted state without training data."""
    payload = dict(artifact)
    claimed = payload.pop("model_semantic_digest", None)
    if claimed != _digest(payload):
        raise ValueError("fitted model artifact semantic digest mismatch")
    if feature_schema is V2_FEATURE_SCHEMA:
        if (
            target_binding is None
            or payload.get("feature_schema_identity") != feature_schema.identity
            or payload.get("dataset_semantic_digest") != feature_schema.dataset_semantic_digest
            or payload.get("manifest_semantic_digest") != feature_schema.manifest_semantic_digest
            or payload.get("feature_order") != list(feature_schema.feature_order)
            or payload.get("target_binding_identity") != target_binding.identity
            or payload.get("target_semantic_digest") != target_binding.target_semantic_digest
            or payload.get("target_manifest_semantic_digest")
            != target_binding.target_manifest_digest
            or payload.get("fold_schedule_identity") != V2_WALK_FORWARD_IDENTITY
        ):
            raise ValueError("V2 fitted model binding mismatch")
    elif (
        payload.get("identity") != FITTED_MODEL_ARTIFACT_IDENTITY
        or payload.get("feature_order") != list(FEATURE_ORDER)
        or payload.get("analytical_semantic_digest") != ANALYTICAL_SEMANTIC_DIGEST
        or payload.get("target_semantic_digest") != TARGET_SEMANTIC_DIGEST
        or payload.get("target_manifest_semantic_digest") != TARGET_MANIFEST_SEMANTIC_DIGEST
    ):
        raise ValueError("fitted model artifact binding mismatch")
    if (
        expected_preprocessing_digest is not None
        and payload.get("preprocessing_digest") != expected_preprocessing_digest
    ):
        raise ValueError("fitted model preprocessing digest mismatch")
    if (
        expected_fold_identity is not None
        and payload.get("training_fold_identity") != expected_fold_identity
    ):
        raise ValueError("fitted model training fold mismatch")
    parameters = payload.get("parameters")
    if not isinstance(parameters, dict):
        raise TypeError("fitted model parameters malformed")
    kind = payload.get("kind")
    if kind == "LOGISTIC":
        return ProbabilityModelV1(
            str(parameters["model"]),
            tuple(parameters["classes"]),
            tuple(tuple(item) for item in parameters["coefficients"]),
            tuple(parameters["intercepts"]),
            tuple(tuple(item) for item in parameters["hyperparameters"]),
            str(parameters["preprocessing_digest"]),
            tuple(parameters["feature_indexes"]),
        )
    if kind == "TREE":
        return TreeProbabilityModelV1(
            int(parameters["feature_index"]),
            float(parameters["threshold"]),
            tuple(parameters["classes"]),
            tuple(parameters["below_probabilities"]),
            tuple(parameters["above_probabilities"]),
            str(parameters["preprocessing_digest"]),
            tuple(parameters["feature_indexes"]),
            tuple(tuple(item) for item in parameters["hyperparameters"]),
        )
    if kind == "BASELINE":
        return BaselineProbabilityModelV2(
            str(parameters["model"]),
            tuple(parameters["classes"]),
            tuple(parameters["probabilities"]),
            str(parameters["preprocessing_digest"]),
            tuple(parameters["feature_indexes"]),
        )
    if kind == "BTC_BASELINE":
        return BtcBaselineModelV2(
            str(parameters["model"]),
            tuple(parameters["classes"]),
            tuple(tuple(item) for item in parameters["coefficients"]),
            tuple(parameters["intercepts"]),
            tuple(tuple(item) for item in parameters["hyperparameters"]),
            tuple(parameters["means"]),
            tuple(parameters["scales"]),
            str(parameters["preprocessing_digest"]),
            tuple(parameters["feature_indexes"]),
            tuple(parameters["btc_feature_order"]),
        )
    raise ValueError("unknown fitted model artifact kind")


def predict_from_fitted_model(model: ReloadableModel, matrix):
    """Predict from reloaded state; never receives or accesses training data."""
    import numpy as np

    if isinstance(model, ProbabilityModelV1):
        return predict_probability_model(model, matrix)
    if isinstance(model, TreeProbabilityModelV1):
        return predict_tree_probability(model, matrix)
    if isinstance(model, BtcBaselineModelV2):
        scaled = (np.asarray(matrix, dtype=float) - np.asarray(model.means)) / np.asarray(
            model.scales
        )
        proxy = ProbabilityModelV1(
            model.model,
            model.classes,
            model.coefficients,
            model.intercepts,
            model.hyperparameters,
            model.preprocessing_digest,
            model.feature_indexes,
        )
        return predict_probability_model(proxy, scaled)
    return np.tile(np.asarray(model.probabilities), (len(matrix), 1))


def fit_tree_probability(
    matrix,
    labels: tuple[str | int, ...],
    classes: tuple[str, ...],
    preprocessing_digest: str,
    feature_indexes: tuple[int, ...],
) -> TreeProbabilityModelV1 | None:
    """Fit a deterministic depth-one probability tree with Laplace-smoothed leaves."""
    import numpy as np

    if len(set(labels)) < 2:
        return None
    encoded = np.asarray([classes.index(str(item)) for item in labels], dtype=int)
    best: tuple[float, int, float, tuple[float, ...], tuple[float, ...]] | None = None
    for feature_index in range(matrix.shape[1]):
        values = np.asarray(matrix[:, feature_index])
        thresholds = np.unique(np.quantile(values, np.linspace(0.1, 0.9, 9)))
        for threshold in thresholds:
            below = values <= threshold
            if not below.any() or below.all():
                continue
            leaf_probabilities = []
            loss = 0.0
            for mask in (below, ~below):
                counts = np.bincount(encoded[mask], minlength=len(classes)).astype(float) + 1
                probabilities = counts / counts.sum()
                leaf_probabilities.append(tuple(probabilities.tolist()))
                loss += float(mask.sum()) * (1 - float(np.sum(probabilities**2)))
            candidate = (
                loss,
                feature_index,
                float(threshold),
                leaf_probabilities[0],
                leaf_probabilities[1],
            )
            if best is None or candidate < best:
                best = candidate
    if best is None:
        return None
    _, feature_index, threshold, below_probabilities, above_probabilities = best
    return TreeProbabilityModelV1(
        feature_index,
        threshold,
        classes,
        below_probabilities,
        above_probabilities,
        preprocessing_digest,
        feature_indexes,
    )


def predict_tree_probability(model: TreeProbabilityModelV1, matrix):
    import numpy as np

    below = np.asarray(matrix[:, model.feature_index]) <= model.threshold
    return np.where(
        below[:, None], np.asarray(model.below_probabilities), np.asarray(model.above_probabilities)
    )


def _softmax(scores):
    import numpy as np

    shifted = scores - scores.max(axis=1, keepdims=True)
    exponential = np.exp(np.clip(shifted, -40, 40))
    return exponential / exponential.sum(axis=1, keepdims=True)


def fit_probability_model(
    matrix,
    labels: tuple[str | int, ...],
    classes: tuple[str, ...],
    *,
    model: str,
    l2: float,
    preprocessing_digest: str,
    feature_indexes: tuple[int, ...],
    iterations: int = 100,
) -> ProbabilityModelV1 | None:
    """Fit deterministic binary/multinomial logistic probabilities; return None if undefined."""
    import numpy as np

    observed = tuple(dict.fromkeys(labels))
    if len(observed) < 2:
        return None
    class_index = {name: index for index, name in enumerate(classes)}
    encoded = np.asarray([class_index[str(label)] for label in labels], dtype=int)
    if any(str(label) not in class_index for label in labels):
        raise ValueError("label outside frozen class order")
    width = matrix.shape[1]
    weights = np.zeros((len(classes), width), dtype=float)
    intercepts = np.zeros(len(classes), dtype=float)
    learning_rate = 0.2
    for iteration in range(iterations):
        probabilities = _softmax(matrix @ weights.T + intercepts)
        residual = probabilities
        residual[np.arange(len(encoded)), encoded] -= 1
        weights -= learning_rate / len(encoded) * (residual.T @ matrix + l2 * weights)
        intercepts -= learning_rate / len(encoded) * residual.sum(axis=0)
        learning_rate = 0.2 / math.sqrt(iteration + 2)
    return ProbabilityModelV1(
        model,
        classes,
        tuple(tuple(item) for item in weights.tolist()),
        tuple(intercepts.tolist()),
        (
            ("l2", str(l2)),
            ("iterations", str(iterations)),
            ("solver", "deterministic_batch_gradient"),
        ),
        preprocessing_digest,
        feature_indexes,
    )


def predict_probability_model(model: ProbabilityModelV1, matrix):
    import numpy as np

    probabilities = _softmax(
        np.asarray(matrix) @ np.asarray(model.coefficients).T + np.asarray(model.intercepts)
    )
    if not np.isfinite(probabilities).all() or not np.allclose(
        probabilities.sum(axis=1), 1, atol=SOFTMAX_TOLERANCE
    ):
        raise ValueError("model probability normalization failure")
    return probabilities


def baseline_probabilities(
    train: FoldDatasetV1, assessment: FoldDatasetV1, kind: str, classes: tuple[str, ...]
):
    fitted = fit_baseline_model(train, kind, classes)
    matrix = (
        assessment.btc_inputs
        if isinstance(fitted, BtcBaselineModelV2)
        else ((0.0,),) * len(assessment.timestamps)
    )
    return predict_from_fitted_model(fitted, matrix)


def fit_baseline_model(
    train: FoldDatasetV1, kind: str, classes: tuple[str, ...]
) -> BaselineProbabilityModelV2 | BtcBaselineModelV2:
    """Fit and retain all training-derived state for one frozen baseline."""
    import numpy as np

    encoded = np.asarray([[int(str(label) == item) for item in classes] for label in train.labels])
    frequencies = encoded.mean(axis=0)
    if kind == "majority_class":
        probabilities = np.zeros(len(classes))
        probabilities[int(np.argmax(frequencies))] = 1.0
        return BaselineProbabilityModelV2(kind, classes, tuple(probabilities.tolist()))
    elif kind == "historical_frequency_probability":
        return BaselineProbabilityModelV2(kind, classes, tuple(frequencies.tolist()))
    elif kind == "btc_only_causal_price_volatility":
        train_matrix = np.asarray(train.btc_inputs, dtype=float)
        means = train_matrix.mean(axis=0)
        scales = train_matrix.std(axis=0)
        scales = np.where(scales > 0, scales, 1.0)
        train_matrix = (train_matrix - means) / scales
        preprocessing_digest = _digest(
            {
                "identity": "BTC_ONLY_TRAIN_SCALE/1.0.0",
                "means": means.tolist(),
                "scales": scales.tolist(),
            }
        )
        fitted = fit_probability_model(
            train_matrix,
            train.labels,
            classes,
            model=kind,
            l2=0.1,
            preprocessing_digest=preprocessing_digest,
            feature_indexes=(0, 1),
        )
        if fitted is None:
            return BaselineProbabilityModelV2(kind, classes, tuple(frequencies.tolist()))
        return BtcBaselineModelV2(
            kind,
            classes,
            fitted.coefficients,
            fitted.intercepts,
            fitted.hyperparameters,
            tuple(means.tolist()),
            tuple(scales.tolist()),
            preprocessing_digest,
        )
    elif kind == "causal_persistence_regime":
        # Lagged training outcome transition probabilities; assessment never supplies future labels.
        transitions = np.ones((len(classes), len(classes)), dtype=float)
        class_index = {item: index for index, item in enumerate(classes)}
        for previous, current in zip(train.labels, train.labels[1:], strict=False):
            transitions[class_index[str(previous)], class_index[str(current)]] += 1
        probabilities = transitions[class_index[str(train.labels[-1])]]
        probabilities /= probabilities.sum()
        return BaselineProbabilityModelV2(kind, classes, tuple(probabilities.tolist()))
    else:
        raise ValueError("unknown frozen baseline")


@dataclass(frozen=True, slots=True)
class HeldOutEvidenceV1:
    identity: str
    fold: str
    horizon: str
    task: str
    model: str
    timestamps: tuple[str, ...]
    true_labels: tuple[str | int, ...]
    class_order: tuple[str, ...]
    probabilities: tuple[tuple[float, ...], ...]
    metrics: dict[str, object]
    model_digest: str | None
    preprocessing: dict[str, object] | None
    fitted_model_artifact: dict[str, object] | None = None
    state: str = "AVAILABLE"

    @property
    def digest(self) -> str:
        return _digest(asdict(self))


def checkpoint_evidence_bindings(evidence: HeldOutEvidenceV1) -> dict[str, str]:
    """Return explicit genuine fold-evidence digests for checkpoint payloads."""
    if evidence.state == "AVAILABLE" and evidence.fitted_model_artifact is None:
        raise ValueError("available fitted evidence requires a reloadable model artifact")
    model_digest = (
        str(evidence.fitted_model_artifact["model_semantic_digest"])
        if evidence.fitted_model_artifact
        else _digest({"state": evidence.state, "model": evidence.model, "fold": evidence.fold})
    )
    predictions = {
        "timestamps": evidence.timestamps,
        "true_labels": evidence.true_labels,
        "class_order": evidence.class_order,
        "probabilities": evidence.probabilities,
    }
    return {
        "fitted_model_artifact_digest": model_digest,
        "held_out_prediction_artifact_digest": _digest(predictions),
        "metric_evidence_digest": _digest(evidence.metrics),
    }


def _direction_metrics(
    labels: tuple[str | int, ...], probabilities, classes: tuple[str, ...]
) -> dict[str, object]:
    import numpy as np

    predicted = np.asarray(probabilities).argmax(axis=1)
    actual = np.asarray([classes.index(str(item)) for item in labels])
    confusion = [
        [int(np.sum((actual == left) & (predicted == right))) for right in range(3)]
        for left in range(3)
    ]
    recalls = [row[index] / sum(row) if sum(row) else None for index, row in enumerate(confusion)]
    defined = [item for item in recalls if item is not None]
    return {
        "balanced_accuracy": sum(defined) / len(defined)
        if len(defined) == len(classes)
        else UNDEFINED,
        "direction_accuracy": float(np.mean(actual == predicted)),
        "per_direction_accuracy": dict(zip(classes, recalls, strict=True)),
        "confusion_matrix": confusion,
        "log_loss": float(
            -np.mean(np.log(np.clip(probabilities[np.arange(len(actual)), actual], 1e-15, 1)))
        ),
    }


def evaluate_fold(
    train: FoldDatasetV1,
    assessment: FoldDatasetV1,
    model: str,
    *,
    feature_indexes: tuple[int, ...] = tuple(range(len(FEATURE_ORDER))),
    l2: float = 0.0,
    interactions: bool = False,
    schema: FeatureSchema = V1_FEATURE_SCHEMA,
    target_binding: TargetBinding | None = None,
) -> HeldOutEvidenceV1:
    """Fit on TRAIN and persist genuine held-out assessment probabilities and metrics."""
    if (
        (train.fold_identity, train.horizon, train.task)
        != (
            assessment.fold_identity,
            assessment.horizon,
            assessment.task,
        )
        or train.phase != "TRAIN"
        or assessment.phase != "ASSESSMENT"
    ):
        raise ValueError("fold train/assessment identity mismatch")
    classes = DIRECTION_CLASSES if train.task == "DIRECTION" else ("0", "1")
    labels = tuple(str(item) for item in train.labels)
    assessment_labels = tuple(str(item) for item in assessment.labels)
    preprocessing: PreprocessingV1 | None = None
    fitted: ReloadableModel | None = None
    if model in {
        "majority_class",
        "historical_frequency_probability",
        "btc_only_causal_price_volatility",
        "causal_persistence_regime",
    }:
        fitted = fit_baseline_model(train, model, classes)
        baseline_matrix = (
            assessment.btc_inputs
            if isinstance(fitted, BtcBaselineModelV2)
            else ((0.0,),) * len(assessment.timestamps)
        )
        probabilities = predict_from_fitted_model(fitted, baseline_matrix)
    else:
        preprocessing = PreprocessingV1.fit(train, feature_indexes)
        train_matrix = preprocessing.transform(train, feature_indexes, interactions=interactions)
        if model == "deterministic_tree_boosting":
            fitted = fit_tree_probability(
                train_matrix, labels, classes, preprocessing.digest, feature_indexes
            )
        else:
            fitted = fit_probability_model(
                train_matrix,
                labels,
                classes,
                model=model,
                l2=l2,
                preprocessing_digest=preprocessing.digest,
                feature_indexes=feature_indexes,
            )
        del train_matrix
        if fitted is None:
            return HeldOutEvidenceV1(
                PREDICTION_ARTIFACT_IDENTITY,
                train.fold_identity,
                train.horizon,
                train.task,
                model,
                tuple(item.isoformat() for item in assessment.timestamps),
                assessment.labels,
                classes,
                (),
                {"state": UNDEFINED, "reason": "TRAINING_FOLD_SINGLE_CLASS"},
                None,
                asdict(preprocessing),
                None,
                UNDEFINED,
            )
        assessment_matrix = preprocessing.transform(
            assessment, feature_indexes, interactions=interactions
        )
        probabilities = (
            predict_tree_probability(fitted, assessment_matrix)
            if isinstance(fitted, TreeProbabilityModelV1)
            else predict_probability_model(fitted, assessment_matrix)
        )
    if train.task == "DIRECTION":
        metrics = _direction_metrics(assessment_labels, probabilities, classes)
    else:
        metrics = binary_metrics(
            tuple(int(item) for item in assessment.labels),
            tuple(float(row[1]) for row in probabilities),
        )
        metrics["calibration"] = calibration_table(
            tuple(int(item) for item in assessment.labels),
            tuple(float(row[1]) for row in probabilities),
            rare_event=True,
        )
    return HeldOutEvidenceV1(
        PREDICTION_ARTIFACT_IDENTITY,
        train.fold_identity,
        train.horizon,
        train.task,
        model,
        tuple(item.isoformat() for item in assessment.timestamps),
        assessment.labels,
        classes,
        tuple(tuple(float(value) for value in row) for row in probabilities),
        metrics,
        fitted.digest if fitted else None,
        asdict(preprocessing) if preprocessing else None,
        serialize_fitted_model(
            fitted,
            training_fold_identity=train.fold_identity,
            task=train.task,
            horizon=train.horizon,
            schema=schema,
            target_binding=target_binding,
        )
        if fitted
        else None,
    )


def ablation_evaluation(
    train: FoldDatasetV1, assessment: FoldDatasetV1, model: str, feature: str, *, l2: float
) -> HeldOutEvidenceV1:
    if feature not in FEATURE_ORDER:
        raise ValueError("unknown feature ablation")
    indexes = tuple(index for index, name in enumerate(FEATURE_ORDER) if name != feature)
    return evaluate_fold(train, assessment, model, feature_indexes=indexes, l2=l2)


def family_ablation_evaluation(
    train: FoldDatasetV1, assessment: FoldDatasetV1, model: str, family: str, *, l2: float
) -> HeldOutEvidenceV1:
    members = next((members for name, members in FEATURE_FAMILY_MEMBERSHIP if name == family), None)
    if members is None:
        raise ValueError("unknown feature family")
    indexes = tuple(index for index, name in enumerate(FEATURE_ORDER) if name not in members)
    return evaluate_fold(train, assessment, model, feature_indexes=indexes, l2=l2)


def permute_held_out_feature(matrix, feature_index: int, permutation_index: int):
    """Jointly permute one transformed sensor's numeric and state one-hot columns."""
    import numpy as np

    if not 0 <= feature_index < len(FEATURE_ORDER) or not 0 <= permutation_index < 20:
        raise ValueError("invalid deterministic permutation identity")
    feature_count = len(FEATURE_ORDER)
    expected_width = feature_count * (1 + len(STATE_NAME_BY_CODE))
    if matrix.ndim != 2 or matrix.shape[1] < expected_width:
        raise ValueError("transformed matrix lacks a complete sensor bundle")
    result = np.array(matrix, copy=True)
    ordering = deterministic_block_permutation(len(result), permutation_index)
    bundle = (
        feature_index,
        *range(
            feature_count + feature_index * len(STATE_NAME_BY_CODE),
            feature_count + (feature_index + 1) * len(STATE_NAME_BY_CODE),
        ),
    )
    result[:, bundle] = result[np.asarray(ordering)[:, None], np.asarray(bundle)]
    return result


def permute_held_out_sensor(
    dataset: FoldDatasetV1, feature: str, permutation_index: int
) -> FoldDatasetV1:
    """Permute a raw held-out (value,state) sensor bundle with one frozen row mapping."""
    if dataset.phase != "ASSESSMENT":
        raise ValueError("permutation is permitted only on held-out assessment evidence")
    if feature not in FEATURE_ORDER or not 0 <= permutation_index < 20:
        raise ValueError("invalid held-out sensor permutation identity")
    feature_index = FEATURE_ORDER.index(feature)
    ordering = deterministic_block_permutation(len(dataset.timestamps), permutation_index)
    values = [list(row) for row in dataset.values]
    states = [list(row) for row in dataset.states]
    for target_row, source_row in enumerate(ordering):
        values[target_row][feature_index] = dataset.values[source_row][feature_index]
        states[target_row][feature_index] = dataset.states[source_row][feature_index]
    return FoldDatasetV1(
        dataset.timestamps,
        tuple(tuple(row) for row in values),
        tuple(tuple(row) for row in states),
        dataset.labels,
        dataset.btc_inputs,
        dataset.fold_identity,
        dataset.phase,
        dataset.horizon,
        dataset.task,
    )


def permute_held_out_family(
    dataset: FoldDatasetV1, family: str, permutation_index: int
) -> FoldDatasetV1:
    """Move all raw member sensor bundles under one shared deterministic block mapping."""
    members = next((members for name, members in FEATURE_FAMILY_MEMBERSHIP if name == family), None)
    if members is None:
        raise ValueError("unknown feature family permutation")
    if dataset.phase != "ASSESSMENT" or not 0 <= permutation_index < 20:
        raise ValueError("invalid held-out family permutation identity")
    indexes = tuple(FEATURE_ORDER.index(member) for member in members)
    ordering = deterministic_block_permutation(len(dataset.timestamps), permutation_index)
    values = [list(row) for row in dataset.values]
    states = [list(row) for row in dataset.states]
    for target_row, source_row in enumerate(ordering):
        for feature_index in indexes:
            values[target_row][feature_index] = dataset.values[source_row][feature_index]
            states[target_row][feature_index] = dataset.states[source_row][feature_index]
    return FoldDatasetV1(
        dataset.timestamps,
        tuple(tuple(row) for row in values),
        tuple(tuple(row) for row in states),
        dataset.labels,
        dataset.btc_inputs,
        dataset.fold_identity,
        dataset.phase,
        dataset.horizon,
        dataset.task,
    )


def bootstrap_batch(
    values: tuple[float, ...], batch_start: int, batch_end_exclusive: int
) -> tuple[float, ...]:
    from .tsunami_predictive_research import moving_block_bootstrap_indices

    if (
        not values
        or batch_start < 0
        or batch_end_exclusive > 2_000
        or batch_end_exclusive - batch_start != 100
    ):
        raise ValueError("bootstrap must use exact 100-replicate production batches")
    return tuple(
        sum(values[index] for index in moving_block_bootstrap_indices(len(values), replicate))
        / len(values)
        for replicate in range(batch_start, batch_end_exclusive)
    )


def episode_evaluation(
    episode_ids: tuple[str | None, ...], probabilities: tuple[float, ...], threshold: float
) -> dict[str, float | int | None]:
    return event_metrics(episode_ids, tuple(item >= threshold for item in probabilities))


def validate_fold_engine() -> None:
    if tuple(BINARY_TASK_COLUMNS) != ("MOVE", "STRESS", "TSUNAMI", "UP_TSUNAMI", "DOWN_TSUNAMI"):
        raise RuntimeError("binary task drift")
    if len(FEATURE_ORDER) != 15 or len(MODEL_CATALOGUE) != 8:
        raise RuntimeError("feature/model catalogue drift")
    if (
        len(ANALYTICAL_SEMANTIC_DIGEST) != 64
        or len(TARGET_SEMANTIC_DIGEST) != 64
        or len(TARGET_MANIFEST_SEMANTIC_DIGEST) != 64
    ):
        raise RuntimeError("source binding drift")


validate_fold_engine()
