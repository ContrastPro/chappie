"""Narrow boundary-aware dataset loader for frozen V2 Validation execution."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .short_horizon_direction_v2_materializer import (
    FINAL_FEATURE_ORDER,
    STATE_CODES,
    VALIDATION_FEATURE_STORE_ROOT,
)
from .short_horizon_direction_v2_validation_boundary import (
    VALIDATION_BOUNDARY_IDENTITY,
    verify_validation_boundary,
)
from .short_horizon_direction_v2_validation_targets import VALIDATION_TARGET_ROOT
from .tsunami_fold_engine import DIRECTION_CLASSES

VALIDATION_EXECUTION_IDENTITY = "CHAPPIE_SHORT_HORIZON_DIRECTION_V2_VALIDATION_EXECUTION/1.0.0"
FINAL_REFIT_VERIFICATION_REPORT = Path("E:/projects/chappie/data/research/staging/market_brain/short_horizon_direction_v2/final_refit_verification.json")
VALIDATION_EXECUTION_STATUS_ROOT = Path(
    "E:/projects/chappie/data/research/staging/market_brain/short_horizon_direction_v2/validation_execution"
)


def _canonical_status_payload(value: object) -> dict[str, object]:
    """Normalize JSON representations before durable status binding/comparison."""
    import json

    def default(item: object) -> object:
        if hasattr(item, "item"):
            return item.item()  # type: ignore[no-any-return]
        if hasattr(item, "tolist"):
            return item.tolist()  # type: ignore[no-any-return]
        if hasattr(item, "value"):
            return item.value  # type: ignore[no-any-return]
        raise TypeError(f"status payload cannot serialize {type(item).__name__}")

    canonical = json.dumps(value, default=default, sort_keys=True, separators=(",", ":"))
    normalized = json.loads(canonical)
    if not isinstance(normalized, dict):
        raise TypeError("status payload must canonicalize to an object")
    return normalized


@dataclass(frozen=True, slots=True)
class ValidationDirectionDatasetV2:
    timestamps: tuple[datetime, ...]
    values: tuple[tuple[float | None, ...], ...]
    states: tuple[tuple[int, ...], ...]
    labels: tuple[str, ...]
    btc_inputs: tuple[tuple[float, float], ...]
    boundary_digest: str


def load_v2_validation_direction_dataset(
    *, boundary_digest: str, feature_root=VALIDATION_FEATURE_STORE_ROOT, target_root=VALIDATION_TARGET_ROOT
) -> ValidationDirectionDatasetV2:
    """Load only the verified feature/target scored intersection; never OOS."""
    import pyarrow.dataset as ds

    boundary = verify_validation_boundary()
    if boundary.get("identity") != VALIDATION_BOUNDARY_IDENTITY or boundary.get("boundary_semantic_digest") != boundary_digest:
        raise ValueError("Validation loader requires the exact frozen boundary digest")
    feature_paths = sorted(feature_root.glob("experiment_matrix/year=*/month=*/data.parquet"))
    target_paths = sorted(target_root.glob("horizon=1h/year=*/month=*/data.parquet"))
    columns = ["timestamp_utc"] + [column for name in FINAL_FEATURE_ORDER for column in (name, name + "__state")]
    features = ds.dataset([str(path) for path in feature_paths], format="parquet").to_table(columns=columns).to_pydict()
    targets = ds.dataset([str(path) for path in target_paths], format="parquet").to_table(columns=["timestamp_utc", "direction_class", "entry_open", "sigma_h", "target_state"]).to_pydict()
    lookup = {timestamp: index for index, timestamp in enumerate(features["timestamp_utc"])}
    timestamps: list[datetime] = []; values = []; states = []; labels = []; btc = []
    for index, timestamp in enumerate(targets["timestamp_utc"]):
        if targets["target_state"][index] != "AVAILABLE" or timestamp not in lookup:
            raise ValueError("Validation target/feature intersection mismatch")
        row = lookup[timestamp]; label = str(targets["direction_class"][index])
        item_values = tuple(features[name][row] for name in FINAL_FEATURE_ORDER)
        item_states = tuple(features[name + "__state"][row] for name in FINAL_FEATURE_ORDER)
        if label not in DIRECTION_CLASSES or any(code not in STATE_CODES.values() for code in item_states):
            raise ValueError("Validation schema/target semantics mismatch")
        timestamps.append(timestamp.astimezone(UTC)); values.append(item_values); states.append(item_states); labels.append(label); btc.append((float(targets["entry_open"][index]), float(targets["sigma_h"][index])))
    if len(timestamps) != boundary["scored_intersection"]["row_count"] or timestamps[0].isoformat().replace("+00:00", "Z") != boundary["scored_intersection"]["first_timestamp"] or timestamps[-1].isoformat().replace("+00:00", "Z") != boundary["scored_intersection"]["last_timestamp"]:
        raise ValueError("Validation scored interval evidence mismatch")
    return ValidationDirectionDatasetV2(tuple(timestamps), tuple(values), tuple(states), tuple(labels), tuple(btc), boundary_digest)


@dataclass(frozen=True, slots=True)
class FinalResearchRefitDatasetV2:
    timestamps: object
    values: object
    states: object
    labels: object
    btc_inputs: object
    training_boundary_digest: str


def fit_array_native_preprocessing(values, states, indexes):  # type: ignore[no-untyped-def]
    """Exact NumPy algebra of frozen PreprocessingV1, without tuple materialization."""
    import numpy as np

    raw = values[:, indexes]
    medians = np.nanmedian(raw, axis=0)
    medians = np.where(np.isfinite(medians), medians, 0.0)
    filled = np.where(np.isnan(raw), medians, raw)
    means = filled.mean(axis=0)
    scales = filled.std(axis=0)
    scales = np.where(scales > 0, scales, 1.0)
    return medians, means, scales


def transform_array_native_preprocessing(values, states, indexes, medians, means, scales):  # type: ignore[no-untyped-def]
    """Exact frozen numeric-fill/scale/state-one-hot transform with finite enforcement."""
    import numpy as np

    raw = values[:, indexes]
    numeric = (np.where(np.isnan(raw), medians, raw) - means) / scales
    selected_states = states[:, indexes]
    result = np.concatenate((numeric, np.eye(9, dtype=float)[selected_states].reshape(len(selected_states), -1)), axis=1)
    if not np.isfinite(result).all():
        raise ValueError("array-native preprocessing produced nonfinite matrix")
    return result


def execute_v2_validation_candidate(
    candidate_identity: str,
    *,
    boundary_digest: str,
    stage_callback: Callable[[str, dict[str, object] | None], None] | None = None,
):
    """One frozen-candidate fit/predict primitive; evidence publication remains caller-owned."""
    import numpy as np

    from .short_horizon_direction_v2_fit import LOGISTIC_BASELINE_CONFIGURATION
    from .short_horizon_direction_v2_validation_freeze import frozen_validation_candidates
    from .tsunami_fold_engine import (
        DIRECTION_CLASSES,
        fit_probability_model,
        predict_probability_model,
    )

    def stage(name: str, metrics: dict[str, object] | None = None) -> None:
        if stage_callback is not None:
            stage_callback(name, metrics)

    candidate = next((item for item in frozen_validation_candidates() if item["identity"] == candidate_identity), None)
    if candidate is None:
        raise ValueError("candidate is not in immutable Validation freeze")
    research = load_v2_final_research_refit_dataset()
    stage("RESEARCH_DATA_LOADED", {"research_training_boundary_digest": research.training_boundary_digest})
    indexes = tuple(FINAL_FEATURE_ORDER.index(name) for name in candidate["projection"])
    stage("PREPROCESSING_FIT")
    medians, means, scales = fit_array_native_preprocessing(research.values, research.states, indexes)
    train = transform_array_native_preprocessing(research.values, research.states, indexes, medians, means, scales)
    stage("RESEARCH_TRANSFORMED")
    stage("MODEL_FIT_STARTED")
    model = fit_probability_model(train, tuple(research.labels.tolist()), DIRECTION_CLASSES, model="logistic_baseline", l2=0.0, preprocessing_digest="PREPROCESSING_IDENTITY", feature_indexes=indexes)
    stage("MODEL_FIT_COMPLETE")
    validation = load_v2_validation_direction_dataset(boundary_digest=boundary_digest)
    stage("VALIDATION_DATA_LOADED")
    values, states = np.asarray(validation.values, dtype=float), np.asarray(validation.states, dtype=np.uint8)
    scored = transform_array_native_preprocessing(values, states, indexes, medians, means, scales)
    stage("VALIDATION_TRANSFORMED")
    probabilities = predict_probability_model(model, scored)
    if not np.isfinite(probabilities).all() or not np.allclose(probabilities.sum(axis=1), 1.0, atol=1e-12):
        raise ValueError("Validation probability integrity failure")
    predicted = np.asarray(DIRECTION_CLASSES)[probabilities.argmax(axis=1)]
    stage("PREDICTION_COMPLETE")
    labels = np.asarray(validation.labels)
    accuracy = float((predicted == labels).mean())
    balanced = float(np.mean([(predicted[labels == item] == item).mean() for item in DIRECTION_CLASSES]))
    # FIT's historical-frequency baseline chooses one fixed class; macro recall is 1 / K.
    baseline = 1.0 / len(DIRECTION_CLASSES)
    result = {"candidate": candidate_identity, "scored_samples": len(labels), "balanced_accuracy": balanced, "baseline_balanced_accuracy": float(baseline), "delta": float(balanced - baseline), "raw_accuracy": accuracy, "class_support": {item: int((labels == item).sum()) for item in DIRECTION_CLASSES}, "model_configuration": list(LOGISTIC_BASELINE_CONFIGURATION), "probabilities_finite": True, "probabilities_normalized": True, "validation_consumed": True, "adaptive_tuning": False, "blind_oos_accessed": False}
    stage("METRICS_COMPLETE", result)
    return result


def run_v2_validation_candidate_with_status(
    candidate_identity: str,
    *,
    boundary_digest: str,
    path: Path | None = None,
) -> dict[str, object]:
    """Run one frozen candidate with atomic, reload-verified durable checkpoints."""
    import hashlib
    import json
    import os
    import traceback

    from .short_horizon_direction_v2_validation_freeze import candidate_freeze_payload

    report_path = path or VALIDATION_EXECUTION_STATUS_ROOT / (candidate_identity.replace("/", "_") + ".json")
    boundary = verify_validation_boundary()
    if boundary.get("boundary_semantic_digest") != boundary_digest:
        raise ValueError("status runner requires the exact frozen Validation boundary")
    freeze_digest = hashlib.sha256(
        json.dumps(candidate_freeze_payload(boundary), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    base = {
        "identity": VALIDATION_EXECUTION_IDENTITY,
        "candidate_identity": candidate_identity,
        "candidate_freeze_digest": freeze_digest,
        "research_training_boundary_digest": None,
        "validation_boundary_digest": boundary_digest,
    }

    last_metrics: dict[str, object] | None = None

    def persist(stage: str, metrics: dict[str, object] | None = None, error: BaseException | None = None) -> dict[str, object]:
        from datetime import datetime

        payload: dict[str, object] = {**base, "stage": stage, "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z")}
        if metrics is not None:
            payload["metrics"] = metrics
        elif error is not None and last_metrics is not None:
            payload["metrics"] = last_metrics
        if error is not None:
            payload["exception_type"] = type(error).__name__
            payload["exception_message"] = str(error)
            payload["traceback"] = traceback.format_exc()
            payload["last_completed_stage"] = latest_stage[0]
        normalized = _canonical_status_payload(payload)
        encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        pending = report_path.with_suffix(report_path.suffix + ".pending")
        pending.write_bytes(encoded)
        os.replace(pending, report_path)
        if json.loads(report_path.read_text(encoding="utf-8")) != normalized:
            raise ValueError("Validation execution status reload mismatch")
        return normalized

    latest_stage = ["STARTED"]
    persist("STARTED")

    def checkpoint(stage: str, metrics: dict[str, object] | None = None) -> None:
        nonlocal last_metrics
        if metrics is not None and "research_training_boundary_digest" in metrics:
            base["research_training_boundary_digest"] = metrics["research_training_boundary_digest"]
        if stage == "METRICS_COMPLETE" and metrics is not None:
            last_metrics = _canonical_status_payload({"metrics": metrics})["metrics"]  # type: ignore[assignment]
        latest_stage[0] = stage
        persist(stage, metrics)

    try:
        result = execute_v2_validation_candidate(candidate_identity, boundary_digest=boundary_digest, stage_callback=checkpoint)
        result["research_training_boundary_digest"] = base["research_training_boundary_digest"]
        base["research_training_boundary_digest"] = result["research_training_boundary_digest"]
        checkpoint("TERMINAL_SUCCESS", result)
        return result
    except BaseException as error:
        persist("TERMINAL_FAILURE", error=error)
        raise


def load_v2_final_research_refit_dataset():
    """Load the complete released Research feature/target intersection, never a fold."""
    import hashlib
    import json

    import pyarrow.parquet as pq

    from .short_horizon_direction_v2_materializer import FEATURE_STORE_ROOT, verify_feature_store
    from .tsunami_fold_engine import V2_DIRECTION_1H_TARGET_BINDING, validate_target_binding
    from .tsunami_research_targets import RESEARCH_TARGET_ROOT

    feature_evidence = verify_feature_store(FEATURE_STORE_ROOT)
    validate_target_binding(V2_DIRECTION_1H_TARGET_BINDING, RESEARCH_TARGET_ROOT)
    columns = ["timestamp_utc"] + [column for name in FINAL_FEATURE_ORDER for column in (name, name + "__state")]
    import numpy as np

    timestamp_chunks = []; value_chunks = []; state_chunks = []; label_chunks = []; btc_chunks = []
    for feature_path in sorted(FEATURE_STORE_ROOT.glob("experiment_matrix/year=*/month=*/data.parquet")):
        year, month = feature_path.parent.parent.name, feature_path.parent.name
        target_path = RESEARCH_TARGET_ROOT / "horizon=1h" / year / month / "data.parquet"
        if not target_path.exists():
            raise ValueError("Research target partition missing for feature month")
        feature = pq.ParquetFile(feature_path).read(columns=columns).to_pandas().set_index("timestamp_utc")
        target = pq.ParquetFile(target_path).read(
            columns=["timestamp_utc", "target_state", "direction_class", "entry_open", "sigma_h"]
        ).to_pandas()
        target = target.loc[target.target_state == "AVAILABLE"].set_index("timestamp_utc")
        if target.index.duplicated().any() or not target.index.is_monotonic_increasing:
            raise ValueError("Research target timestamps must be unique and ordered")
        local_timestamps = []; local_values = []; local_states = []; local_labels = []; local_btc = []
        for timestamp, item in target.iterrows():
            timestamp = timestamp.astimezone(UTC)
            if timestamp >= V2_DIRECTION_1H_TARGET_BINDING.research_end_exclusive:
                raise PermissionError("final Research refit cannot enter Validation")
            if timestamp not in feature.index:
                raise ValueError("Research feature/target timestamp alignment mismatch")
            row = feature.loc[timestamp]; label = str(item.direction_class)
            item_values = tuple(row[name] for name in FINAL_FEATURE_ORDER)
            item_states = tuple(row[name + "__state"] for name in FINAL_FEATURE_ORDER)
            if label not in DIRECTION_CLASSES or any(code not in STATE_CODES.values() for code in item_states):
                raise ValueError("Research refit schema/target semantics mismatch")
            if item.entry_open is None or item.sigma_h is None:
                raise ValueError("Research refit BTC inputs missing")
            local_timestamps.append(timestamp); local_values.append(item_values); local_states.append(item_states); local_labels.append(label); local_btc.append((float(item.entry_open), float(item.sigma_h)))
        if local_timestamps:
            timestamp_chunks.append(np.asarray(local_timestamps, dtype="datetime64[ns]"))
            value_chunks.append(np.asarray(local_values, dtype=np.float64))
            state_chunks.append(np.asarray(local_states, dtype=np.uint8))
            label_chunks.append(np.asarray(local_labels, dtype="U7"))
            btc_chunks.append(np.asarray(local_btc, dtype=np.float64))
        del feature, target
    timestamps = np.concatenate(timestamp_chunks); values = np.concatenate(value_chunks); states = np.concatenate(state_chunks); labels = np.concatenate(label_chunks); btc = np.concatenate(btc_chunks)
    if not len(timestamps) or not np.all(timestamps[1:] > timestamps[:-1]):
        raise ValueError("Research refit timestamps must be unique and increasing")
    payload = {"identity": "CHAPPIE_SHORT_HORIZON_DIRECTION_V2_FINAL_RESEARCH_REFIT/1.0.0", "row_count": len(timestamps), "first_timestamp": str(timestamps[0]), "last_timestamp": str(timestamps[-1]), "end_exclusive": V2_DIRECTION_1H_TARGET_BINDING.research_end_exclusive.isoformat(), "feature_dataset_digest": feature_evidence["dataset_semantic_digest"], "feature_manifest_digest": feature_evidence["manifest_semantic_digest"], "target_binding": V2_DIRECTION_1H_TARGET_BINDING.identity, "target_semantic_digest": V2_DIRECTION_1H_TARGET_BINDING.target_semantic_digest}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return FinalResearchRefitDatasetV2(timestamps, values, states, labels, btc, digest)


def write_final_refit_verification_report(path: Path = FINAL_REFIT_VERIFICATION_REPORT) -> dict[str, object]:
    """Persist a diagnostic-only compact-array verification after a real Research load."""
    import hashlib
    import json
    import os

    import numpy as np

    dataset = load_v2_final_research_refit_dataset()
    arrays = {
        "timestamps": dataset.timestamps,
        "feature_values": dataset.values,
        "feature_states": dataset.states,
        "labels": dataset.labels,
        "btc_inputs": dataset.btc_inputs,
    }
    content = hashlib.sha256()
    for name, array in arrays.items():
        content.update(name.encode()); content.update(str(array.dtype).encode()); content.update(array.tobytes())
    labels = set(dataset.labels.tolist())
    report = {
        "execution_state": "SUCCESS",
        "row_count": int(dataset.timestamps.shape[0]),
        "first_timestamp": str(dataset.timestamps[0]),
        "last_timestamp": str(dataset.timestamps[-1]),
        "strictly_pre_validation": bool(np.all(dataset.timestamps < np.datetime64("2025-06-01T00:00:00"))),
        "arrays": {name: {"shape": list(array.shape), "dtype": str(array.dtype), "nbytes": int(array.nbytes)} for name, array in arrays.items()},
        "total_retained_nbytes": int(sum(array.nbytes for array in arrays.values())),
        "no_object_dtype": bool(all(array.dtype != object for array in arrays.values())),
        "unique_increasing_timestamps": bool(np.all(dataset.timestamps[1:] > dataset.timestamps[:-1])),
        "finite_numeric": bool(np.isfinite(dataset.values).all() and np.isfinite(dataset.btc_inputs).all()),
        "valid_labels": labels == set(DIRECTION_CLASSES),
        "feature_target_alignment": True,
        "training_boundary_digest": dataset.training_boundary_digest,
        "full_universe_dataset_semantic_digest": content.hexdigest(),
        "validation_loaded": False,
        "models_fitted": False,
        "blind_oos_accessed": False,
    }
    encoded = json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(path.suffix + ".pending"); pending.write_bytes(encoded); os.replace(pending, path)
    if json.loads(path.read_text(encoding="utf-8")) != report:
        raise ValueError("final refit verification report reload mismatch")
    return report


def audit_final_refit_model_readiness() -> dict[str, object]:
    """State-aware, no-fit audit using the exact median/scale/one-hot FIT algebra."""
    import numpy as np

    from .short_horizon_direction_v2_fit import (
        FROZEN_FAMILY_COMBINATION_PROJECTIONS,
        FULL_V2_FEATURE_ORDER,
    )

    dataset = load_v2_final_research_refit_dataset()
    raw, states = dataset.values, dataset.states
    nonfinite = ~np.isfinite(raw)
    available = states == STATE_CODES["AVAILABLE"]
    illegal = nonfinite & available
    candidates = {"FULL_V2": FULL_V2_FEATURE_ORDER, "TECHNICAL+VOLUME": FROZEN_FAMILY_COMBINATION_PROJECTIONS["TECHNICAL+VOLUME"]}
    transformed = {}
    for name, projection in candidates.items():
        indexes = [FINAL_FEATURE_ORDER.index(feature) for feature in projection]
        value = raw[:, indexes]; state = states[:, indexes]
        medians = np.nanmedian(value, axis=0); medians = np.where(np.isfinite(medians), medians, 0.0)
        filled = np.where(np.isnan(value), medians, value)
        scales = filled.std(axis=0); scales = np.where(scales > 0, scales, 1.0)
        numeric = (filled - filled.mean(axis=0)) / scales
        ready = np.concatenate((numeric, np.eye(9, dtype=float)[state].reshape(len(state), -1)), axis=1)
        transformed[name] = {"shape": list(ready.shape), "dtype": str(ready.dtype), "nbytes": int(ready.nbytes), "non_finite": int((~np.isfinite(ready)).sum())}
    return {"raw_non_finite": int(nonfinite.sum()), "rows_with_raw_non_finite": int(nonfinite.any(axis=1).sum()), "illegal_non_finite_count": int(illegal.sum()), "entry_sigma_finite": bool(np.isfinite(dataset.btc_inputs).all()), "labels_valid": bool(set(dataset.labels.tolist()) == set(DIRECTION_CLASSES)), "transformed": transformed, "training_boundary_digest": dataset.training_boundary_digest}


def complete_final_refit_readiness_report(path: Path = FINAL_REFIT_VERIFICATION_REPORT) -> dict[str, object]:
    """Merge the completed no-fit audit with compact-dataset provenance atomically."""
    import hashlib
    import json
    import os

    from .short_horizon_direction_v2_materializer import (
        FEATURE_STORE_ROOT,
        IDENTITY,
        verify_feature_store,
    )
    from .tsunami_fold_engine import V2_DIRECTION_1H_TARGET_BINDING
    from .tsunami_research_targets import RESEARCH_TARGET_ROOT, verify_research_targets

    dataset = load_v2_final_research_refit_dataset()
    audit = json.loads(path.with_name("final_refit_audit.json").read_text(encoding="utf-8"))
    content = hashlib.sha256()
    for name, array in (("timestamps", dataset.timestamps), ("feature_values", dataset.values), ("feature_states", dataset.states), ("labels", dataset.labels), ("btc_inputs", dataset.btc_inputs)):
        content.update(name.encode()); content.update(str(array.dtype).encode()); content.update(array.tobytes())
    features, targets = verify_feature_store(FEATURE_STORE_ROOT), verify_research_targets(RESEARCH_TARGET_ROOT)
    expected = "83d7fb296bb71f2b2d6b87e91891282c3e90e34f7c5ffb710255ef79960550ab"
    report = {**audit, "row_count": len(dataset.timestamps), "first_timestamp": str(dataset.timestamps[0]), "last_timestamp": str(dataset.timestamps[-1]), "strictly_pre_validation": bool(str(dataset.timestamps[-1]) < "2025-06-01"), "unique_increasing_timestamps": bool((dataset.timestamps[1:] > dataset.timestamps[:-1]).all()), "feature_authority": {"identity": IDENTITY, "dataset_digest": features["dataset_semantic_digest"], "manifest_digest": features["manifest_semantic_digest"]}, "target_authority": {"identity": V2_DIRECTION_1H_TARGET_BINDING.target_identity, "semantic_digest": targets["target_semantic_digest"], "manifest_digest": targets["manifest_semantic_digest"], "semantic_identity": V2_DIRECTION_1H_TARGET_BINDING.identity}, "feature_schema_identity": "CHAPPIE_SHORT_HORIZON_DIRECTION_V2_FEATURE_SCHEMA/1.0.0", "feature_schema_digest": features["dataset_semantic_digest"], "training_boundary_digest": dataset.training_boundary_digest, "full_universe_dataset_semantic_digest": content.hexdigest(), "expected_content_digest": expected, "content_digest_matches_expected": content.hexdigest() == expected, "preprocessing_provenance": "PreprocessingV1 train-only median/scale plus frozen state one-hot", "validation_loaded": False, "models_fitted": False, "blind_oos_accessed": False}
    encoded = json.dumps(report, sort_keys=True, separators=(",", ":")).encode(); path.parent.mkdir(parents=True, exist_ok=True); pending = path.with_suffix(path.suffix + ".pending"); pending.write_bytes(encoded); os.replace(pending, path)
    if json.loads(path.read_text(encoding="utf-8")) != report: raise ValueError("final readiness report reload mismatch")
    return report
