from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from app.research.capital_flow.free_v2_materializer import FEATURE_ORDER
from app.research.capital_flow.tsunami_forecast_contract import (
    EXPERIMENT_IDENTITY,
    FEATURE_FAMILY_MEMBERSHIP,
    REQUIRED_PROBABILITIES,
    RESEARCH_END_EXCLUSIVE,
    WALK_FORWARD_FOLDS,
)
from app.research.capital_flow.tsunami_predictive_research import (
    ANALYTICAL_SEMANTIC_DIGEST,
    BH_FDR_Q,
    BOOTSTRAP_REPLICATES,
    CONTRIBUTION_METHOD,
    HOLM_ALPHA,
    HORIZON_NAMES,
    MODEL_CATALOGUE,
    MULTIPLE_TESTING_POLICY,
    PERMUTATION_REPLICATES,
    PRODUCTION_CONTRACT,
    RANDOM_SEEDS,
    RESEARCH_CONTRACT_DIGEST,
    RESEARCH_IDENTITY,
    RESULT_IDENTITY,
    SELECTION_RULE,
    TARGET_MANIFEST_SEMANTIC_DIGEST,
    TARGET_SEMANTIC_DIGEST,
    TASKS,
    VALIDATION_FREEZE_IDENTITY,
    CandidateEvidenceV1,
    ResearchExecutionContractV1,
    ValidationCandidateFreezeV1,
    assert_research_timestamp,
    bh_decisions,
    binary_metrics,
    bounded_synthetic_benchmark,
    calibration_table,
    classify_ablation,
    deterministic_block_permutation,
    event_metrics,
    fit_deterministic_stump,
    fit_logistic,
    historical_frequency_probabilities,
    holm_decisions,
    imputation_plan,
    interaction_row,
    moving_block_bootstrap_indices,
    normalize_positive_contributions,
    percentile_interval,
    permutation_contribution,
    publish_result_bundle,
    research_selection,
    select_candidate,
    validate_probabilities,
    verify_bound_manifests,
)
from app.research.capital_flow.tsunami_research_targets import RESEARCH_TARGET_IDENTITY
from app.tools.tsunami_predictive_research import main


def test_immutable_identity_digest_feature_horizon_fold_and_policy_bindings() -> None:
    assert RESEARCH_IDENTITY == "CHAPPIE_CAPITAL_FLOW_TSUNAMI_PREDICTIVE_RESEARCH/1.0.0"
    assert PRODUCTION_CONTRACT.experiment_identity == EXPERIMENT_IDENTITY
    assert PRODUCTION_CONTRACT.feature_order == FEATURE_ORDER
    assert len(FEATURE_ORDER) == 15
    assert HORIZON_NAMES == ("1h", "4h", "12h", "24h", "48h", "72h")
    assert len(WALK_FORWARD_FOLDS) == 6
    assert all(
        fold.train_end_exclusive + timedelta(hours=72) == fold.assessment_start
        for fold in WALK_FORWARD_FOLDS
    )
    assert len(RESEARCH_CONTRACT_DIGEST) == 64
    with pytest.raises(ValueError, match="15-feature"):
        ResearchExecutionContractV1(feature_order=FEATURE_ORDER[:-1])


def test_models_tasks_baselines_selection_and_research_seeds_are_frozen() -> None:
    assert len(MODEL_CATALOGUE) == 8
    assert {item.value for item in TASKS} == {
        "DIRECTION",
        "MOVE",
        "STRESS",
        "TSUNAMI",
        "UP_TSUNAMI",
        "DOWN_TSUNAMI",
    }
    assert "six-fold mean" in SELECTION_RULE
    assert RANDOM_SEEDS == (20240101, 20240601, 20250601)
    evidence = (
        CandidateEvidenceV1("regularized_logistic", 0.4, 0.2),
        CandidateEvidenceV1("deterministic_tree_boosting", 0.4, 0.2),
        CandidateEvidenceV1("logistic_baseline", 0.3, 0.1),
    )
    assert select_candidate(evidence).model == "regularized_logistic"


def test_unavailable_candidates_are_ineligible_without_score_substitution() -> None:
    unavailable = CandidateEvidenceV1("logistic_baseline", None, None, selection_eligible=False)
    valid = CandidateEvidenceV1("regularized_logistic", 0.4, 0.2)
    assert select_candidate((unavailable, valid)) == valid
    assert select_candidate((unavailable,)) is None
    assert unavailable.primary_metric is None
    assert unavailable.brier is None


def test_research_firewall_has_no_validation_path_or_timestamp_api(tmp_path: Path) -> None:
    with pytest.raises(PermissionError, match="forbids Validation"):
        assert_research_timestamp(RESEARCH_END_EXCLUSIVE)
    selection = research_selection("WF6", "ASSESSMENT", "24h", analytical_root=tmp_path)
    assert selection.end_exclusive == RESEARCH_END_EXCLUSIVE
    assert "2025-06-01" in selection.duckdb_sql()
    assert "ORDER BY" in selection.duckdb_sql()
    assert selection.feature_glob.endswith("partitions/year=*/month=*/data.parquet")
    assert selection.target_glob.endswith("horizon=24h/year=*/month=*/data.parquet")
    with pytest.raises(ValueError, match="phase"):
        research_selection("WF1", "VALIDATION", "24h")


def test_manifest_bindings_detect_tampering_without_opening_data(tmp_path: Path) -> None:
    analytical = tmp_path / "analytical"
    targets = tmp_path / "targets"
    analytical.mkdir()
    targets.mkdir()
    (analytical / "manifest.json").write_text(
        json.dumps(
            {
                "analytical_view_identity": PRODUCTION_CONTRACT.analytical_view_identity,
                "analytical_semantic_digest": ANALYTICAL_SEMANTIC_DIGEST,
            }
        ),
        encoding="utf-8",
    )
    target_payload = {
        "target_identity": RESEARCH_TARGET_IDENTITY,
        "target_semantic_digest": TARGET_SEMANTIC_DIGEST,
        "manifest_semantic_digest": TARGET_MANIFEST_SEMANTIC_DIGEST,
    }
    (targets / "manifest.json").write_text(json.dumps(target_payload), encoding="utf-8")
    verify_bound_manifests(analytical, targets)
    target_payload["target_semantic_digest"] = "0" * 64
    (targets / "manifest.json").write_text(json.dumps(target_payload), encoding="utf-8")
    with pytest.raises(ValueError, match="binding mismatch"):
        verify_bound_manifests(analytical, targets)


def _probabilities() -> dict[str, float]:
    return dict(
        zip(
            REQUIRED_PROBABILITIES,
            (0.4, 0.2, 0.1, 0.4, 0.3, 0.3, 0.06, 0.04),
            strict=True,
        )
    )


def test_probability_schema_coherence_and_baseline_semantics() -> None:
    validate_probabilities(_probabilities())
    broken = _probabilities()
    broken["P(TSUNAMI)"] = 0.9
    with pytest.raises(ValueError, match="nested"):
        validate_probabilities(broken)
    assert historical_frequency_probabilities(["UP", "UP", "DOWN"]) == {
        "DOWN": 1 / 3,
        "UP": 2 / 3,
    }


def test_training_only_imputation_preserves_unavailable_state_columns() -> None:
    rows = [{name: float(index) for name in FEATURE_ORDER} for index in range(3)]
    rows[1][FEATURE_ORDER[0]] = None
    plan = imputation_plan(rows)
    assert plan[FEATURE_ORDER[0]] == 1.0
    assert tuple(plan) == FEATURE_ORDER


def test_logistic_tree_and_interaction_models_are_deterministic_and_isolated() -> None:
    rows = ((-2.0, 0.0), (-1.0, 0.0), (1.0, 0.0), (2.0, 0.0))
    labels = (0, 0, 1, 1)
    logistic = fit_logistic(rows, labels, l2=0.1, iterations=50)
    assert logistic == fit_logistic(rows, labels, l2=0.1, iterations=50)
    assert logistic.predict_probability(rows[0]) < logistic.predict_probability(rows[-1])
    stump = fit_deterministic_stump(rows, labels)
    assert stump == fit_deterministic_stump(rows, labels)
    assert stump.predict_probability(rows[0]) < stump.predict_probability(rows[-1])
    additive = tuple(float(index + 1) for index in range(len(FEATURE_ORDER)))
    interacted = interaction_row(additive)
    assert interacted[: len(FEATURE_ORDER)] == additive
    assert len(interacted) > len(additive)


def test_binary_metrics_auc_calibration_and_event_metrics_are_separate() -> None:
    labels = [0, 0, 1, 1]
    probabilities = [0.1, 0.4, 0.35, 0.8]
    result = binary_metrics(labels, probabilities)
    assert result["roc_auc"] == pytest.approx(0.75)
    assert result["pr_auc"] is not None
    assert result["brier"] == pytest.approx(0.158125)
    assert (
        sum(
            item["sample_count"]
            for item in calibration_table(labels, probabilities, rare_event=True)
        )
        == 4
    )
    events = event_metrics(
        [None, "episode-1", "episode-1", "episode-2"], [True, False, True, False]
    )
    assert events["actual_events"] == 2
    assert events["detected_events"] == 1
    assert events["event_recall"] == 0.5


def test_permutation_ablation_and_family_contribution_policies_are_exact() -> None:
    first = deterministic_block_permutation(25_000, 3)
    assert first == deterministic_block_permutation(25_000, 3)
    assert sorted(first) == list(range(25_000))
    delta, classification = permutation_contribution(0.7, [0.6] * PERMUTATION_REPLICATES)
    assert delta == pytest.approx(0.1)
    assert classification == "POSITIVE"
    normalized = normalize_positive_contributions({"good": 3.0, "harmful": -1.0, "other": 1.0})
    assert normalized == {"good": 75.0, "harmful": None, "other": 25.0}
    assert classify_ablation(-0.1) == "HARMFUL"
    assert "seven-day-block" in CONTRIBUTION_METHOD
    assert len(FEATURE_FAMILY_MEMBERSHIP) == 8


def test_bootstrap_and_multiple_testing_are_deterministic() -> None:
    assert moving_block_bootstrap_indices(20_000, 7) == moving_block_bootstrap_indices(20_000, 7)
    values = [float(index) for index in range(BOOTSTRAP_REPLICATES)]
    assert percentile_interval(values) == (49.0, 1949.0)
    assert holm_decisions({"a": 0.01, "b": 0.06}) == {"a": True, "b": False}
    assert bh_decisions({"a": 0.01, "b": 0.04, "c": 0.8}) == {
        "a": True,
        "b": True,
        "c": False,
    }
    assert HOLM_ALPHA == 0.05 and BH_FDR_Q == 0.10
    assert "Holm" in MULTIPLE_TESTING_POLICY


def test_result_and_validation_freeze_are_create_only_and_digest_bound(tmp_path: Path) -> None:
    freeze = ValidationCandidateFreezeV1(
        selected_models=({"model": "regularized_logistic", "hyperparameters": {}},),
        preprocessing={"imputation": "training_median"},
        thresholds={"TSUNAMI": 0.1},
        calibration={"method": "research_only"},
        selection_evidence_digest="a" * 64,
    )
    assert freeze.payload()["identity"] == VALIDATION_FREEZE_IDENTITY
    manifest = publish_result_bundle({"metrics": {}}, freeze, tmp_path / "research")
    assert manifest["identity"] == RESULT_IDENTITY
    assert (tmp_path / "research" / "validation_candidate_freeze.json").exists()
    with pytest.raises(FileExistsError, match="create-only"):
        publish_result_bundle({"metrics": {}}, freeze, tmp_path / "research")


def test_benchmark_is_bounded_synthetic_and_cli_cannot_execute_research(capsys) -> None:
    benchmark = bounded_synthetic_benchmark(500)
    assert benchmark["synthetic_rows"] == 500
    assert benchmark["rows_per_second"] > 0
    assert main(["--describe"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["real_research_executed"] is False
    with pytest.raises(SystemExit, match="choose --execute"):
        main([])
