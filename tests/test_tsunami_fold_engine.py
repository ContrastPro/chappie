from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from app.research.capital_flow.free_v2_materializer import FEATURE_ORDER
from app.research.capital_flow.tsunami_fold_engine import (
    BINARY_TASK_COLUMNS,
    DIRECTION_CLASSES,
    FOLD_ENGINE_IDENTITY,
    PREPROCESSING_IDENTITY,
    UNDEFINED,
    BaselineProbabilityModelV2,
    FoldDatasetV1,
    PreprocessingV1,
    ablation_evaluation,
    baseline_probabilities,
    bootstrap_batch,
    checkpoint_evidence_bindings,
    evaluate_fold,
    family_ablation_evaluation,
    fit_baseline_model,
    fit_probability_model,
    fit_tree_probability,
    load_fitted_model,
    permute_held_out_family,
    permute_held_out_feature,
    permute_held_out_sensor,
    predict_from_fitted_model,
    predict_probability_model,
    serialize_fitted_model,
)
from app.research.capital_flow.tsunami_forecast_contract import RESEARCH_END_EXCLUSIVE
from app.research.capital_flow.tsunami_predictive_research import interaction_row


def _dataset(phase: str, task: str = "MOVE", *, single_class: bool = False) -> FoldDatasetV1:
    start = (
        datetime(2024, 1, 1, tzinfo=UTC) if phase == "TRAIN" else datetime(2024, 7, 1, tzinfo=UTC)
    )
    count = 18 if phase == "TRAIN" else 9
    timestamps = tuple(start + timedelta(minutes=index) for index in range(count))
    values = tuple(
        tuple(
            None if index == 0 and feature == 2 else float(index + feature) for feature in range(15)
        )
        for index in range(count)
    )
    states = tuple(tuple(1 if value is None else 0 for value in row) for row in values)
    if task == "DIRECTION":
        labels = tuple(
            DIRECTION_CLASSES[0 if single_class else index % 3] for index in range(count)
        )
    else:
        labels = tuple(0 if single_class else index % 2 for index in range(count))
    btc = tuple((100.0 + index, 0.01 + index / 10_000) for index in range(count))
    return FoldDatasetV1(timestamps, values, states, labels, btc, "WF1", phase, "24h", task)


def test_identity_feature_tasks_and_validation_firewall_are_exact() -> None:
    assert FOLD_ENGINE_IDENTITY == "CHAPPIE_CAPITAL_FLOW_TSUNAMI_FOLD_ENGINE/1.0.0"
    assert tuple(BINARY_TASK_COLUMNS) == ("MOVE", "STRESS", "TSUNAMI", "UP_TSUNAMI", "DOWN_TSUNAMI")
    assert len(FEATURE_ORDER) == 15
    dataset = _dataset("ASSESSMENT")
    with pytest.raises(PermissionError, match="Validation"):
        FoldDatasetV1(
            (RESEARCH_END_EXCLUSIVE,),
            dataset.values[:1],
            dataset.states[:1],
            dataset.labels[:1],
            dataset.btc_inputs[:1],
            "WF6",
            "ASSESSMENT",
            "24h",
            "MOVE",
        )


def test_preprocessing_is_train_fitted_deterministic_and_state_explicit() -> None:
    train = _dataset("TRAIN")
    assessment = _dataset("ASSESSMENT")
    indexes = tuple(range(15))
    first = PreprocessingV1.fit(train, indexes)
    changed_assessment = PreprocessingV1.fit(train, indexes)
    assert first == changed_assessment
    assert first.identity == PREPROCESSING_IDENTITY
    matrix = first.transform(assessment, indexes)
    assert matrix.shape == (9, 15 + 15 * first.state_cardinality)
    assert np.isfinite(matrix).all()


def test_interaction_transform_preallocates_exact_legacy_term_order() -> None:
    train = _dataset("TRAIN")
    indexes = tuple(range(len(FEATURE_ORDER)))
    preprocessing = PreprocessingV1.fit(train, indexes)
    additive = preprocessing.transform(train, indexes)
    interaction_columns = np.asarray(
        [interaction_row(row[: len(FEATURE_ORDER)])[len(FEATURE_ORDER) :] for row in additive]
    )
    expected = np.concatenate((additive, interaction_columns), axis=1)

    actual = preprocessing.transform(train, indexes, interactions=True)

    np.testing.assert_array_equal(actual, expected)


def test_binary_and_multiclass_logistic_are_genuine_and_normalized() -> None:
    for task, classes in (("MOVE", ("0", "1")), ("DIRECTION", DIRECTION_CLASSES)):
        train = _dataset("TRAIN", task)
        preprocessing = PreprocessingV1.fit(train, tuple(range(15)))
        matrix = preprocessing.transform(train, tuple(range(15)))
        model = fit_probability_model(
            matrix,
            tuple(str(item) for item in train.labels),
            classes,
            model="regularized_logistic",
            l2=0.1,
            preprocessing_digest=preprocessing.digest,
            feature_indexes=tuple(range(15)),
            iterations=10,
        )
        assert model is not None
        probabilities = predict_probability_model(model, matrix)
        assert np.isfinite(probabilities).all()
        assert np.allclose(probabilities.sum(axis=1), 1, atol=1e-12)


def test_single_class_returns_explicit_unavailable_not_fake_model_or_zero_metric() -> None:
    train = _dataset("TRAIN", single_class=True)
    assessment = _dataset("ASSESSMENT")
    result = evaluate_fold(train, assessment, "regularized_logistic", l2=0.1)
    assert result.state == UNDEFINED
    assert result.probabilities == ()
    assert result.metrics["state"] == UNDEFINED


@pytest.mark.parametrize(
    "model",
    (
        "majority_class",
        "historical_frequency_probability",
        "btc_only_causal_price_volatility",
        "causal_persistence_regime",
        "logistic_baseline",
        "regularized_logistic",
        "deterministic_tree_boosting",
        "explicit_interaction_candidate",
    ),
)
def test_fold_evaluation_uses_held_out_rows_and_real_probabilities(model: str) -> None:
    train = _dataset("TRAIN")
    assessment = _dataset("ASSESSMENT")
    result = evaluate_fold(
        train,
        assessment,
        model,
        l2=0.1 if "regularized" in model else 0,
        interactions=model == "explicit_interaction_candidate",
    )
    assert len(result.timestamps) == len(assessment.timestamps)
    assert result.timestamps[0] == assessment.timestamps[0].isoformat()
    assert len(result.probabilities) == len(assessment.timestamps)
    assert all(abs(sum(row) - 1) <= 1e-12 for row in result.probabilities)
    assert "brier" in result.metrics


def test_all_baselines_use_training_evidence_only() -> None:
    train = _dataset("TRAIN", "DIRECTION")
    assessment = _dataset("ASSESSMENT", "DIRECTION")
    for kind in (
        "majority_class",
        "historical_frequency_probability",
        "btc_only_causal_price_volatility",
        "causal_persistence_regime",
    ):
        probabilities = baseline_probabilities(train, assessment, kind, DIRECTION_CLASSES)
        assert probabilities.shape == (len(assessment.timestamps), 3)
        assert np.allclose(probabilities.sum(axis=1), 1)


def test_ablation_refits_without_feature_and_family() -> None:
    train = _dataset("TRAIN")
    assessment = _dataset("ASSESSMENT")
    feature = FEATURE_ORDER[0]
    result = ablation_evaluation(train, assessment, "regularized_logistic", feature, l2=0.1)
    assert result.preprocessing is not None
    assert len(result.preprocessing["medians"]) == 14
    family = family_ablation_evaluation(
        train, assessment, "regularized_logistic", "CRYPTO_PARTICIPATION", l2=0.1
    )
    assert family.preprocessing is not None
    assert len(family.preprocessing["medians"]) == 13


def test_block_permutation_and_bootstrap_batches_are_deterministic() -> None:
    width = 15 * (1 + 9)
    matrix = np.arange(20_000 * width, dtype=float).reshape(20_000, width)
    first = permute_held_out_feature(matrix, 2, 3)
    second = permute_held_out_feature(matrix, 2, 3)
    assert np.array_equal(first, second)
    assert np.array_equal(first[:, 0], matrix[:, 0])
    values = tuple(float(index % 7) for index in range(20_000))
    batch = bootstrap_batch(values, 0, 100)
    assert batch == bootstrap_batch(values, 0, 100)
    assert len(batch) == 100


@pytest.mark.parametrize("feature", FEATURE_ORDER)
def test_raw_sensor_permutation_moves_value_and_state_together_for_all_features(
    feature: str,
) -> None:
    assessment = _dataset("ASSESSMENT")
    index = FEATURE_ORDER.index(feature)
    permuted = permute_held_out_sensor(assessment, feature, 0)
    original_pairs = sorted(
        repr((row[index], state[index]))
        for row, state in zip(assessment.values, assessment.states, strict=True)
    )
    permuted_pairs = sorted(
        repr((row[index], state[index]))
        for row, state in zip(permuted.values, permuted.states, strict=True)
    )
    assert original_pairs == permuted_pairs
    for other in range(len(FEATURE_ORDER)):
        if other != index:
            assert tuple(row[other] for row in permuted.values) == tuple(
                row[other] for row in assessment.values
            )
            assert tuple(row[other] for row in permuted.states) == tuple(
                row[other] for row in assessment.states
            )
    assert permuted.timestamps == assessment.timestamps
    assert permuted.labels == assessment.labels
    assert permuted.btc_inputs == assessment.btc_inputs
    assert permuted == permute_held_out_sensor(assessment, feature, 0)


def test_family_permutation_moves_only_complete_member_bundles() -> None:
    assessment = _dataset("ASSESSMENT")
    permuted = permute_held_out_family(assessment, "CRYPTO_PARTICIPATION", 1)
    member_indexes = (0, 1)
    for index in member_indexes:
        assert sorted(
            repr((row[index], state[index]))
            for row, state in zip(permuted.values, permuted.states, strict=True)
        ) == sorted(
            repr((row[index], state[index]))
            for row, state in zip(assessment.values, assessment.states, strict=True)
        )
    for index in range(2, len(FEATURE_ORDER)):
        assert tuple(row[index] for row in permuted.values) == tuple(
            row[index] for row in assessment.values
        )


def test_incomplete_transformed_bundle_and_training_permutation_fail_closed() -> None:
    with pytest.raises(ValueError, match="complete sensor bundle"):
        permute_held_out_feature(np.zeros((10, 15)), 0, 0)
    with pytest.raises(ValueError, match="held-out assessment"):
        permute_held_out_sensor(_dataset("TRAIN"), FEATURE_ORDER[0], 0)


@pytest.mark.parametrize(
    "model_name", ("logistic_baseline", "regularized_logistic", "explicit_interaction_candidate")
)
def test_logistic_and_interaction_model_artifacts_round_trip(model_name: str) -> None:
    train = _dataset("TRAIN")
    preprocessing = PreprocessingV1.fit(train, tuple(range(15)))
    matrix = preprocessing.transform(train, tuple(range(15)))
    model = fit_probability_model(
        matrix,
        tuple(str(item) for item in train.labels),
        ("0", "1"),
        model=model_name,
        l2=0.1,
        preprocessing_digest=preprocessing.digest,
        feature_indexes=tuple(range(15)),
        iterations=10,
    )
    assert model is not None
    artifact = serialize_fitted_model(model, training_fold_identity="WF1")
    reloaded = load_fitted_model(
        artifact, expected_preprocessing_digest=preprocessing.digest, expected_fold_identity="WF1"
    )
    assert np.array_equal(
        predict_from_fitted_model(model, matrix), predict_from_fitted_model(reloaded, matrix)
    )
    tampered = dict(artifact)
    tampered["training_fold_identity"] = "WF2"
    with pytest.raises(ValueError, match="semantic digest"):
        load_fitted_model(tampered)


def test_tree_and_baseline_artifacts_round_trip_without_training_data() -> None:
    train = _dataset("TRAIN")
    preprocessing = PreprocessingV1.fit(train, tuple(range(15)))
    matrix = preprocessing.transform(train, tuple(range(15)))
    tree = fit_tree_probability(
        matrix,
        tuple(str(item) for item in train.labels),
        ("0", "1"),
        preprocessing.digest,
        tuple(range(15)),
    )
    assert tree is not None
    for model in (
        tree,
        BaselineProbabilityModelV2("historical_frequency_probability", ("0", "1"), (0.4, 0.6)),
    ):
        artifact = serialize_fitted_model(
            model, training_fold_identity="WF1", task="MOVE", horizon="24h"
        )
        reloaded = load_fitted_model(artifact, expected_fold_identity="WF1")
        width = matrix.shape[1] if model is tree else 1
        inputs = matrix if model is tree else np.zeros((len(matrix), width))
        assert np.array_equal(
            predict_from_fitted_model(model, inputs), predict_from_fitted_model(reloaded, inputs)
        )


def test_permutation_reuses_same_reloaded_model_without_refit() -> None:
    train = _dataset("TRAIN")
    preprocessing = PreprocessingV1.fit(train, tuple(range(15)))
    matrix = preprocessing.transform(train, tuple(range(15)))
    model = fit_probability_model(
        matrix,
        tuple(str(item) for item in train.labels),
        ("0", "1"),
        model="regularized_logistic",
        l2=0.1,
        preprocessing_digest=preprocessing.digest,
        feature_indexes=tuple(range(15)),
        iterations=10,
    )
    assert model is not None
    reloaded = load_fitted_model(serialize_fitted_model(model, training_fold_identity="WF1"))
    permuted = permute_held_out_feature(matrix, 0, 0)
    probabilities = predict_from_fitted_model(reloaded, permuted)
    assert probabilities.shape == (len(matrix), 2)
    assert reloaded.digest == model.digest


def test_fold_checkpoint_bindings_include_model_prediction_and_metric_digests() -> None:
    evidence = evaluate_fold(
        _dataset("TRAIN"), _dataset("ASSESSMENT"), "regularized_logistic", l2=0.1
    )
    bindings = checkpoint_evidence_bindings(evidence)
    assert set(bindings) == {
        "fitted_model_artifact_digest",
        "held_out_prediction_artifact_digest",
        "metric_evidence_digest",
    }
    assert all(len(value) == 64 for value in bindings.values())


@pytest.mark.parametrize(
    "baseline",
    (
        "majority_class",
        "historical_frequency_probability",
        "btc_only_causal_price_volatility",
        "causal_persistence_regime",
    ),
)
def test_every_baseline_evidence_is_reloadable_digest_bound_and_no_refit(
    baseline: str,
) -> None:
    train = _dataset("TRAIN")
    assessment = _dataset("ASSESSMENT")
    evidence = evaluate_fold(train, assessment, baseline)
    assert evidence.fitted_model_artifact is not None
    digest = evidence.fitted_model_artifact["model_semantic_digest"]
    assert isinstance(digest, str) and len(digest) == 64
    live = fit_baseline_model(train, baseline, ("0", "1"))
    matrix = (
        assessment.btc_inputs
        if baseline == "btc_only_causal_price_volatility"
        else np.zeros((len(assessment.timestamps), 1))
    )
    original = predict_from_fitted_model(live, matrix)
    artifact = serialize_fitted_model(
        live, training_fold_identity="WF1", task="MOVE", horizon="24h"
    )
    del live
    reloaded = load_fitted_model(artifact, expected_fold_identity="WF1")
    round_trip = predict_from_fitted_model(reloaded, matrix)
    assert np.array_equal(original, round_trip)
    permuted = np.array(matrix, copy=True)[::-1]
    assert np.isfinite(predict_from_fitted_model(reloaded, permuted)).all()
    assert len(checkpoint_evidence_bindings(evidence)["fitted_model_artifact_digest"]) == 64
