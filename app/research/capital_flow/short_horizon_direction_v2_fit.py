"""Shared, V2-only scientific fit execution with no ranking or benchmark policy."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from time import perf_counter

import numpy as np

from .tsunami_fold_engine import (
    DIRECTION_CLASSES,
    V2_DIRECTION_1H_TARGET_BINDING,
    V2_DIRECTION_4H_TARGET_BINDING,
    V2_FEATURE_SCHEMA,
    V2_WALK_FORWARD_FOLDS,
    V2_WALK_FORWARD_IDENTITY,
    FeatureSchema,
    PreprocessingV1,
    TargetBinding,
    evaluate_fold,
    fit_baseline_model,
    fit_probability_model,
    load_fitted_model,
    load_v2_fold_dataset,
    predict_from_fitted_model,
    predict_probability_model,
    preprocessing_binding_digest,
    serialize_fitted_model,
)

V2_SCIENTIFIC_FIT_IDENTITY = "CHAPPIE_SHORT_HORIZON_DIRECTION_V2_SCIENTIFIC_FIT/1.0.0"
V2_FIT_PREDICTIVE_EVALUATION_IDENTITY = (
    "CHAPPIE_SHORT_HORIZON_DIRECTION_V2_FIT_PREDICTIVE_EVALUATION/1.0.0"
)
FULL_V2_FEATURE_PROJECTION = tuple(range(V2_FEATURE_SCHEMA.feature_count))
FULL_V2_FEATURE_ORDER = V2_FEATURE_SCHEMA.feature_order
CAPITAL_FLOW_FEATURE_ORDER = tuple(
    name
    for name in FULL_V2_FEATURE_ORDER
    if V2_FEATURE_SCHEMA.family_mapping[name] == "CAPITAL_FLOW"
)
TECHNICAL_FEATURE_ORDER = tuple(
    name for name in FULL_V2_FEATURE_ORDER if V2_FEATURE_SCHEMA.family_mapping[name] == "TECHNICAL"
)
VOLUME_FEATURE_ORDER = tuple(
    name for name in FULL_V2_FEATURE_ORDER if V2_FEATURE_SCHEMA.family_mapping[name] == "VOLUME"
)
MARKET_STRUCTURE_FEATURE_ORDER = tuple(
    name
    for name in FULL_V2_FEATURE_ORDER
    if V2_FEATURE_SCHEMA.family_mapping[name] == "MARKET_STRUCTURE"
)


def _frozen_family_projection(*families: str) -> tuple[str, ...]:
    """Compose a released projection in immutable V2 schema order."""
    return tuple(
        name for name in FULL_V2_FEATURE_ORDER if V2_FEATURE_SCHEMA.family_mapping[name] in families
    )


FROZEN_FAMILY_COMBINATION_PROJECTIONS = {
    "TECHNICAL+VOLUME": _frozen_family_projection("TECHNICAL", "VOLUME"),
    "TECHNICAL+MARKET_STRUCTURE": _frozen_family_projection("TECHNICAL", "MARKET_STRUCTURE"),
    "VOLUME+MARKET_STRUCTURE": _frozen_family_projection("VOLUME", "MARKET_STRUCTURE"),
    "CAPITAL_FLOW+TECHNICAL": _frozen_family_projection("CAPITAL_FLOW", "TECHNICAL"),
    "CAPITAL_FLOW+TECHNICAL+VOLUME": _frozen_family_projection(
        "CAPITAL_FLOW", "TECHNICAL", "VOLUME"
    ),
}
FROZEN_LOGISTIC_FEATURE_PROJECTIONS = frozenset(
    (
        FULL_V2_FEATURE_ORDER,
        CAPITAL_FLOW_FEATURE_ORDER,
        TECHNICAL_FEATURE_ORDER,
        VOLUME_FEATURE_ORDER,
        MARKET_STRUCTURE_FEATURE_ORDER,
        *FROZEN_FAMILY_COMBINATION_PROJECTIONS.values(),
    )
)
BTC_ONLY_CAUSAL_INPUT_ORDER = ("entry_open", "sigma_h")
LOGISTIC_BASELINE_CONFIGURATION = (
    ("iterations", "100"),
    ("l2", "0.0"),
    ("solver", "deterministic_batch_gradient"),
)
BTC_ONLY_CONFIGURATION = (
    ("iterations", "100"),
    ("l2", "0.1"),
    ("preprocessing", "BTC_ONLY_TRAIN_SCALE/1.0.0"),
    ("solver", "deterministic_batch_gradient"),
)


@dataclass(frozen=True, slots=True)
class V2ScientificFitBinding:
    """All scientific inputs for one frozen Research-only V2 fit."""

    experiment_identity: str
    feature_schema: FeatureSchema
    dataset_semantic_digest: str
    manifest_semantic_digest: str
    target_binding: TargetBinding
    fold_schedule_identity: str
    fold_identity: str
    horizon: str
    feature_projection: tuple[str, ...]
    model_family: str
    model_configuration: tuple[tuple[str, str], ...]
    input_representation: str = "V2_FEATURE_STORE"
    serialize_model: bool = True
    reload_model: bool = True
    round_trip_prediction: bool = True


def build_v2_logistic_binding(
    *,
    experiment_identity: str,
    fold_identity: str = "V2_WF1",
    horizon: str = "1h",
    feature_projection: tuple[str, ...] = FULL_V2_FEATURE_ORDER,
) -> V2ScientificFitBinding:
    """Build an exact released logistic binding without benchmark policy."""
    target_binding = (
        V2_DIRECTION_1H_TARGET_BINDING
        if horizon == "1h"
        else V2_DIRECTION_4H_TARGET_BINDING
        if horizon == "4h"
        else None
    )
    if target_binding is None:
        raise ValueError("the shared logistic primitive supports only 1h/4h")
    return V2ScientificFitBinding(
        experiment_identity=experiment_identity,
        feature_schema=V2_FEATURE_SCHEMA,
        dataset_semantic_digest=V2_FEATURE_SCHEMA.dataset_semantic_digest,
        manifest_semantic_digest=V2_FEATURE_SCHEMA.manifest_semantic_digest,
        target_binding=target_binding,
        fold_schedule_identity=V2_WALK_FORWARD_IDENTITY,
        fold_identity=fold_identity,
        horizon=horizon,
        feature_projection=feature_projection,
        model_family="logistic_baseline",
        model_configuration=LOGISTIC_BASELINE_CONFIGURATION,
    )


def build_v2_btc_only_binding(*, experiment_identity: str) -> V2ScientificFitBinding:
    """Bind the released causal BTC price/volatility representation explicitly."""
    return V2ScientificFitBinding(
        experiment_identity=experiment_identity,
        feature_schema=V2_FEATURE_SCHEMA,
        dataset_semantic_digest=V2_FEATURE_SCHEMA.dataset_semantic_digest,
        manifest_semantic_digest=V2_FEATURE_SCHEMA.manifest_semantic_digest,
        target_binding=V2_DIRECTION_1H_TARGET_BINDING,
        fold_schedule_identity=V2_WALK_FORWARD_IDENTITY,
        fold_identity="V2_WF1",
        horizon="1h",
        feature_projection=BTC_ONLY_CAUSAL_INPUT_ORDER,
        model_family="btc_only_causal_price_volatility",
        model_configuration=BTC_ONLY_CONFIGURATION,
        input_representation="BTC_CAUSAL_PRICE_VOLATILITY",
    )


def validate_v2_scientific_fit_binding(binding: V2ScientificFitBinding) -> None:
    """Fail closed before any Research data is loaded."""
    if (
        binding.feature_schema is not V2_FEATURE_SCHEMA
        or binding.dataset_semantic_digest != V2_FEATURE_SCHEMA.dataset_semantic_digest
        or binding.manifest_semantic_digest != V2_FEATURE_SCHEMA.manifest_semantic_digest
        or binding.fold_schedule_identity != V2_WALK_FORWARD_IDENTITY
        or binding.fold_identity not in {fold.identity for fold in V2_WALK_FORWARD_FOLDS}
        or binding.target_binding
        is not (
            V2_DIRECTION_1H_TARGET_BINDING
            if binding.horizon == "1h"
            else V2_DIRECTION_4H_TARGET_BINDING
        )
        or binding.horizon != binding.target_binding.horizon
        or binding.target_binding.task != "DIRECTION"
        or binding.target_binding.classes != DIRECTION_CLASSES
        or binding.target_binding.research_start != V2_FEATURE_SCHEMA.research_start
        or binding.target_binding.research_end_exclusive != V2_FEATURE_SCHEMA.research_end_exclusive
    ):
        raise ValueError("V2 scientific fit binding mismatch")
    if binding.model_family == "btc_only_causal_price_volatility":
        valid_model = (
            binding.input_representation == "BTC_CAUSAL_PRICE_VOLATILITY"
            and binding.feature_projection == BTC_ONLY_CAUSAL_INPUT_ORDER
            and binding.model_configuration == BTC_ONLY_CONFIGURATION
        )
    else:
        valid_model = (
            binding.input_representation == "V2_FEATURE_STORE"
            and binding.model_family == "logistic_baseline"
            and binding.model_configuration == LOGISTIC_BASELINE_CONFIGURATION
            and binding.feature_projection in FROZEN_LOGISTIC_FEATURE_PROJECTIONS
        )
    if not valid_model:
        raise ValueError("V2 scientific model/projection binding mismatch")
    if not binding.experiment_identity:
        raise ValueError("V2 scientific fit requires experiment identity")


def evaluate_v2_fit_predictive_evidence(
    binding: V2ScientificFitBinding, *, work_unit_identity: str
) -> dict[str, object]:
    """Score one frozen V2 FIT unit against its held-out Research fold only.

    This intentionally refits the already-frozen specification because completed
    FIT checkpoints retain a model digest rather than a reloadable model payload.
    It neither tunes parameters nor reads Validation/OOS data.
    """
    validate_v2_scientific_fit_binding(binding)
    if not work_unit_identity.startswith("v2-authoritative-fit-"):
        raise ValueError("predictive evaluation requires an authoritative V2 FIT identity")
    train = load_v2_fold_dataset(binding.fold_identity, "TRAIN", binding.horizon)
    assessment = load_v2_fold_dataset(binding.fold_identity, "ASSESSMENT", binding.horizon)
    feature_indexes = (
        tuple(V2_FEATURE_SCHEMA.feature_order.index(name) for name in binding.feature_projection)
        if binding.model_family != "btc_only_causal_price_volatility"
        else tuple(range(len(BTC_ONLY_CAUSAL_INPUT_ORDER)))
    )
    model_evidence = evaluate_fold(
        train,
        assessment,
        binding.model_family,
        feature_indexes=feature_indexes,
        l2=0.0,
        schema=binding.feature_schema,
        target_binding=binding.target_binding,
    )
    baseline_evidence = evaluate_fold(
        train,
        assessment,
        "historical_frequency_probability",
        feature_indexes=feature_indexes,
        l2=0.0,
        schema=binding.feature_schema,
        target_binding=binding.target_binding,
    )
    model_metric = model_evidence.metrics.get("balanced_accuracy")
    baseline_metric = baseline_evidence.metrics.get("balanced_accuracy")
    if not isinstance(model_metric, (int, float)) or not isinstance(baseline_metric, (int, float)):
        raise TypeError("V2 direction balanced accuracy is unavailable")
    class_support = {
        item: sum(str(label) == item for label in model_evidence.true_labels)
        for item in model_evidence.class_order
    }
    payload: dict[str, object] = {
        "schema": V2_FIT_PREDICTIVE_EVALUATION_IDENTITY,
        "work_unit_identity": work_unit_identity,
        "experiment_identity": binding.experiment_identity,
        "feature_schema": binding.feature_schema.identity,
        "dataset_semantic_digest": binding.dataset_semantic_digest,
        "manifest_semantic_digest": binding.manifest_semantic_digest,
        "target_binding": binding.target_binding.identity,
        "fold_schedule": binding.fold_schedule_identity,
        "fold": binding.fold_identity,
        "horizon": binding.horizon,
        "feature_projection": list(binding.feature_projection),
        "model": binding.model_family,
        "model_configuration": list(binding.model_configuration),
        "scored_sample_count": len(model_evidence.true_labels),
        "class_support": class_support,
        "metrics": model_evidence.metrics,
        "baseline": {
            "identity": "historical_frequency_probability",
            "metrics": baseline_evidence.metrics,
        },
        "primary_metric": "direction_balanced_accuracy",
        "primary_metric_value": float(model_metric),
        "baseline_primary_metric_value": float(baseline_metric),
        "primary_metric_delta": float(model_metric - baseline_metric),
        "probability_quality_metric": None,
        "probability_quality_reason": "no canonical multiclass Brier metric is defined",
        "research_only": True,
        "validation_accessed": False,
        "blind_oos_accessed": False,
    }
    payload["semantic_digest"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()
    return payload


def execute_v2_fit_unit(
    binding: V2ScientificFitBinding,
    *,
    stage_callback: Callable[[str], None] | None = None,
) -> dict[str, object]:
    """Execute the fixed scientific sequence and return structural evidence only."""
    stage = stage_callback or (lambda _stage: None)
    stage("BINDING")
    validate_v2_scientific_fit_binding(binding)
    started = perf_counter()
    stage("LOAD")
    load_started = perf_counter()
    train = load_v2_fold_dataset(binding.fold_identity, "TRAIN", binding.horizon)
    assessment = load_v2_fold_dataset(binding.fold_identity, "ASSESSMENT", binding.horizon)
    load_wall_seconds = perf_counter() - load_started
    stage("PREPROCESS")
    preprocessing_started = perf_counter()
    if binding.model_family == "btc_only_causal_price_volatility":
        preprocessing_digest = "BTC_ONLY_TRAIN_SCALE/1.0.0"
        train_matrix = assessment_matrix = None
        preprocessing_wall_seconds = 0.0
    else:
        indexes = tuple(
            V2_FEATURE_SCHEMA.feature_order.index(name) for name in binding.feature_projection
        )
        preprocessing = PreprocessingV1.fit(train, indexes)
        train_matrix = preprocessing.transform(train, indexes)
        assessment_matrix = preprocessing.transform(assessment, indexes)
        preprocessing_digest = preprocessing_binding_digest(train, indexes)
        preprocessing_wall_seconds = perf_counter() - preprocessing_started
    fit_started = perf_counter()
    stage("FIT")
    if binding.model_family == "btc_only_causal_price_volatility":
        model = fit_baseline_model(train, binding.model_family, DIRECTION_CLASSES)
    else:
        model = fit_probability_model(
            train_matrix,
            train.labels,
            DIRECTION_CLASSES,
            model=binding.model_family,
            l2=0.0,
            preprocessing_digest=preprocessing_digest,
            feature_indexes=indexes,
        )
    if model is None:
        raise RuntimeError("V2 logistic fit did not yield a model")
    fit_seconds = perf_counter() - fit_started
    stage("PREDICT")
    predict_started = perf_counter()
    probabilities = (
        predict_from_fitted_model(model, assessment.btc_inputs)
        if binding.model_family == "btc_only_causal_price_volatility"
        else predict_probability_model(model, assessment_matrix)
    )
    predict_seconds = perf_counter() - predict_started
    if probabilities.shape != (len(assessment.timestamps), len(DIRECTION_CLASSES)):
        raise ValueError("V2 assessment probability shape mismatch")
    finite = bool(np.isfinite(probabilities).all())
    normalized = bool(np.allclose(probabilities.sum(axis=1), 1.0, atol=1e-12))
    if not finite or not normalized or model.classes != DIRECTION_CLASSES:
        raise ValueError("V2 structural probability check failed")
    stage("RESULT_ADAPT")
    evidence: dict[str, object] = {
        "experiment_identity": binding.experiment_identity,
        "fold": binding.fold_identity,
        "horizon": binding.horizon,
        "model": binding.model_family,
        "train_rows": len(train.timestamps),
        "assessment_rows": len(assessment.timestamps),
        "raw_feature_width": len(binding.feature_projection),
        "transformed_width": (
            len(BTC_ONLY_CAUSAL_INPUT_ORDER)
            if binding.model_family == "btc_only_causal_price_volatility"
            else int(train_matrix.shape[1])
        ),
        "load_wall_seconds": load_wall_seconds,
        "preprocessing_wall_seconds": preprocessing_wall_seconds,
        "fit_wall_seconds": fit_seconds,
        "predict_wall_seconds": predict_seconds,
        "probability_shape": list(probabilities.shape),
        "finite": finite,
        "normalized": normalized,
        "class_order_pass": model.classes == DIRECTION_CLASSES,
        "preprocessing_digest": preprocessing_digest,
        "total_wall_seconds": perf_counter() - started,
    }
    if binding.serialize_model:
        serialize_started = perf_counter()
        artifact = serialize_fitted_model(
            model,
            training_fold_identity=binding.fold_identity,
            task="DIRECTION",
            horizon=binding.horizon,
            schema=binding.feature_schema,
            target_binding=binding.target_binding,
        )
        evidence["serialization_wall_seconds"] = perf_counter() - serialize_started
        evidence["model_semantic_digest"] = artifact["model_semantic_digest"]
        if binding.reload_model:
            reload_started = perf_counter()
            reloaded = load_fitted_model(
                artifact,
                expected_preprocessing_digest=model.preprocessing_digest,
                expected_fold_identity=binding.fold_identity,
                feature_schema=binding.feature_schema,
                target_binding=binding.target_binding,
            )
            repeated = (
                predict_from_fitted_model(reloaded, assessment.btc_inputs)
                if binding.model_family == "btc_only_causal_price_volatility"
                else predict_probability_model(reloaded, assessment_matrix)
            )
            evidence["reload_wall_seconds"] = perf_counter() - reload_started
            evidence["reload_pass"] = True
            if binding.round_trip_prediction:
                evidence["exact_equality"] = bool(np.array_equal(probabilities, repeated))
                evidence["max_abs_difference"] = float(np.max(np.abs(probabilities - repeated)))
                if not evidence["exact_equality"]:
                    raise ValueError("V2 prediction round-trip mismatch")
    stage("CHECKPOINT_PREWRITE")
    evidence["total_wall_seconds"] = perf_counter() - started
    return evidence
