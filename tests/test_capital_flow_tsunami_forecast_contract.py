from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise

import pytest

from app.research.capital_flow.free_v2_materializer import FEATURE_ORDER
from app.research.capital_flow.tsunami_forecast_contract import (
    ABLATION_METRICS,
    ALLOWED_MODEL_FAMILIES,
    ANALYTICAL_VIEW_REQUIRED,
    BASELINES,
    BLIND_OOS_END_EXCLUSIVE,
    BLIND_OOS_START,
    BOUND_DATASET_IDENTITY,
    BOUND_DATASET_SEMANTIC_DIGEST,
    BOUND_MANIFEST_SEMANTIC_DIGEST,
    CALIBRATION_BIN_REPORT_FIELDS,
    CALIBRATION_BUCKETS,
    CALIBRATION_FREEZE_POLICY,
    CONTRIBUTION_POLICY,
    DIAGNOSTIC_HORIZONS,
    DIRECTION_CONFIDENCE_BINS,
    DIRECTION_SIGMA,
    EMBARGO_DURATION,
    EXPERIMENT_EXECUTION_STATUS,
    EXPERIMENT_IDENTITY,
    FAMILY_CONTRIBUTION_POLICY,
    FEATURE_FAMILY_MEMBERSHIP,
    INTERACTION_POLICY,
    LEAD_TIME_METRICS,
    MAX_PRIMARY_HORIZON,
    MISSING_STATE_POLICY,
    MOVE_SIGMA,
    MULTIPLE_TESTING_POLICY,
    PAPER_TRADING_STATUS,
    PRIMARY_HORIZON,
    PRIMARY_HORIZONS,
    PRIMARY_METRICS,
    PURGE_DURATION,
    RARE_EVENT_CALIBRATION_BINS,
    RARE_EVENT_CALIBRATION_EDGES,
    RARE_EVENT_CALIBRATION_TARGETS,
    REQUIRED_PROBABILITIES,
    RESEARCH_END_EXCLUSIVE,
    RESEARCH_START,
    SECONDARY_HORIZONS,
    STRESS_SIGMA,
    TARGET_AUTHORITY,
    TARGET_PRICE_SEMANTICS,
    TARGET_SYMBOL,
    TRADING_CALCULATIONS_ALLOWED,
    TSUNAMI_SIGMA,
    UNCERTAINTY_POLICY,
    VALIDATION_END_EXCLUSIVE,
    VALIDATION_MAX_EXECUTIONS,
    VALIDATION_POLICY,
    VALIDATION_REUSE_FORBIDDEN,
    VALIDATION_START,
    VALIDATION_STATUS,
    VOLATILITY_BASE_FREQUENCY,
    VOLATILITY_DEVIATION,
    VOLATILITY_ESTIMATOR,
    VOLATILITY_PRICE_COUNT,
    VOLATILITY_RETURN_COUNT,
    WALK_FORWARD_FOLDS,
    Direction,
    ExperimentSplit,
    Severity,
    TsunamiCandidateV1,
    TsunamiDirection,
    causal_horizon_volatility,
    classify_standardized_return,
    cluster_tsunami_episodes,
    fold_target_is_eligible,
    future_log_return,
    future_return_endpoints,
    last_target_eligible_timestamp,
    rare_event_calibration_bin,
    target_is_split_eligible,
    validate_contract,
)


def test_identity_dataset_periods_and_blind_oos_are_exact() -> None:
    assert EXPERIMENT_IDENTITY == "CHAPPIE_CAPITAL_FLOW_TSUNAMI_FORECAST_EXPERIMENT/1.0.0"
    assert BOUND_DATASET_IDENTITY == "chappie_capital_flow_free_v2_feature_dataset/1.0.0"
    assert BOUND_DATASET_SEMANTIC_DIGEST == (
        "07898bfdc7fec01c249bd38b479624bcdfc353237e4d8ffcf0015fafb4977e23"
    )
    assert BOUND_MANIFEST_SEMANTIC_DIGEST == (
        "7d4518a01e2f936cde100435a989deba5e57e6492e82863a16c75cc6b872aaaf"
    )
    assert (RESEARCH_START, RESEARCH_END_EXCLUSIVE) == (
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2025, 6, 1, tzinfo=UTC),
    )
    assert VALIDATION_START == RESEARCH_END_EXCLUSIVE
    assert VALIDATION_END_EXCLUSIVE == datetime(2026, 6, 1, tzinfo=UTC)
    assert (BLIND_OOS_START, BLIND_OOS_END_EXCLUSIVE) == (
        datetime(2026, 9, 1, tzinfo=UTC),
        datetime(2027, 3, 1, tzinfo=UTC),
    )
    validate_contract()


def test_horizons_and_strictly_future_target_endpoints_are_frozen() -> None:
    assert PRIMARY_HORIZONS == tuple(timedelta(hours=value) for value in (1, 4, 12, 24, 48, 72))
    assert PRIMARY_HORIZON == timedelta(hours=24)
    assert SECONDARY_HORIZONS == tuple(timedelta(hours=value) for value in (1, 4, 12, 48, 72))
    assert DIAGNOSTIC_HORIZONS == tuple(timedelta(minutes=value) for value in (15, 30, 120, 480))
    timestamp = datetime(2024, 4, 1, 12, tzinfo=UTC)
    assert future_return_endpoints(timestamp, timedelta(hours=1)) == (
        timestamp + timedelta(minutes=1),
        timestamp + timedelta(hours=1),
    )
    with pytest.raises(ValueError, match="not frozen"):
        future_return_endpoints(timestamp, timedelta(hours=3))
    assert future_log_return(Decimal(100), Decimal(100)) == 0


def test_target_asset_price_causality_and_volatility_contract_are_explicit() -> None:
    assert TARGET_SYMBOL == "BTCUSDT"
    assert TARGET_AUTHORITY == "chappie_binance_breadth_historical_authority/2.0.0"
    assert "strictly after" in TARGET_PRICE_SEMANTICS
    assert "completed" in TARGET_PRICE_SEMANTICS
    assert "43,200 contiguous" in VOLATILITY_ESTIMATOR
    assert "ending at T" in VOLATILITY_ESTIMATOR
    assert "unavailable" in VOLATILITY_ESTIMATOR
    assert VOLATILITY_BASE_FREQUENCY == timedelta(minutes=1)
    assert VOLATILITY_RETURN_COUNT == 43_200
    assert VOLATILITY_PRICE_COUNT == 43_201
    assert VOLATILITY_DEVIATION == "SAMPLE_STANDARD_DEVIATION_N_MINUS_1"


def _volatility_closes(timestamp: datetime) -> dict[datetime, Decimal]:
    return {
        timestamp - timedelta(minutes=offset): Decimal(100) + Decimal(index % 17) / Decimal(100)
        for index, offset in enumerate(range(VOLATILITY_RETURN_COUNT, -1, -1))
    }


def test_sigma_h_is_deterministic_causal_and_scales_by_exact_square_root() -> None:
    timestamp = datetime(2025, 5, 1, tzinfo=UTC)
    closes = _volatility_closes(timestamp)
    sigma_1h = causal_horizon_volatility(closes, timestamp, timedelta(hours=1))
    assert sigma_1h == causal_horizon_volatility(closes, timestamp, timedelta(hours=1))
    sigma_4h = causal_horizon_volatility(closes, timestamp, timedelta(hours=4))
    assert abs(sigma_4h / sigma_1h - Decimal(2)) < Decimal("1e-48")
    future = dict(closes)
    future[timestamp + timedelta(minutes=1)] = future.pop(min(future))
    with pytest.raises(ValueError, match="exactly 43,201"):
        causal_horizon_volatility(future, timestamp, timedelta(hours=1))


def test_sigma_h_fails_closed_on_missing_nonpositive_and_zero_variance() -> None:
    timestamp = datetime(2025, 5, 1, tzinfo=UTC)
    closes = _volatility_closes(timestamp)
    missing = dict(closes)
    missing.pop(timestamp - timedelta(days=1))
    with pytest.raises(ValueError, match="exactly 43,201"):
        causal_horizon_volatility(missing, timestamp, timedelta(hours=1))
    nonpositive = dict(closes)
    nonpositive[timestamp] = Decimal(0)
    with pytest.raises(ValueError, match="positive and finite"):
        causal_horizon_volatility(nonpositive, timestamp, timedelta(hours=1))
    flat = {key: Decimal(100) for key in closes}
    with pytest.raises(ValueError, match="variance must be positive"):
        causal_horizon_volatility(flat, timestamp, timedelta(hours=1))


@pytest.mark.parametrize(
    ("value", "direction", "severity", "tsunami_direction"),
    (
        ("0.49", Direction.NO_MOVE, Severity.NONE, TsunamiDirection.NONE),
        ("0.5", Direction.UP, Severity.NONE, TsunamiDirection.NONE),
        ("-0.5", Direction.DOWN, Severity.NONE, TsunamiDirection.NONE),
        ("1", Direction.UP, Severity.MOVE, TsunamiDirection.NONE),
        ("-2", Direction.DOWN, Severity.STRESS, TsunamiDirection.NONE),
        ("4", Direction.UP, Severity.TSUNAMI, TsunamiDirection.UP_TSUNAMI),
        ("-4", Direction.DOWN, Severity.TSUNAMI, TsunamiDirection.DOWN_TSUNAMI),
    ),
)
def test_direction_and_nested_severity_thresholds(
    value: str,
    direction: Direction,
    severity: Severity,
    tsunami_direction: TsunamiDirection,
) -> None:
    label = classify_standardized_return(Decimal(value))
    assert (label.direction, label.severity, label.tsunami_direction) == (
        direction,
        severity,
        tsunami_direction,
    )
    assert (DIRECTION_SIGMA, MOVE_SIGMA, STRESS_SIGMA, TSUNAMI_SIGMA) == (
        Decimal("0.5"),
        Decimal(1),
        Decimal(2),
        Decimal(4),
    )
    with pytest.raises(FrozenInstanceError):
        label.direction = Direction.DOWN  # type: ignore[misc]


def test_episode_clustering_merges_overlapping_labels_and_freezes_peak_direction() -> None:
    start = datetime(2024, 6, 1, tzinfo=UTC)
    horizon = timedelta(hours=1)
    candidates = (
        TsunamiCandidateV1(start, start + horizon, Decimal("4.1")),
        TsunamiCandidateV1(
            start + timedelta(minutes=30), start + timedelta(minutes=90), Decimal(-5)
        ),
        TsunamiCandidateV1(start + timedelta(hours=2), start + timedelta(hours=3), Decimal("4.5")),
    )
    episodes = cluster_tsunami_episodes(candidates, horizon)
    assert len(episodes) == 2
    assert episodes[0].start == start
    assert episodes[0].end == start + timedelta(minutes=90)
    assert episodes[0].onset == start + horizon
    assert episodes[0].peak_severity == Decimal(5)
    assert episodes[0].direction == TsunamiDirection.DOWN_TSUNAMI
    assert episodes[0].candidate_count == 2


def test_walk_forward_is_ordered_purged_and_never_enters_validation() -> None:
    assert len(WALK_FORWARD_FOLDS) == 6
    assert PURGE_DURATION == EMBARGO_DURATION == timedelta(hours=72)
    previous_assessment_start = RESEARCH_START
    for fold in WALK_FORWARD_FOLDS:
        assert fold.train_start == RESEARCH_START
        assert fold.train_end_exclusive + MAX_PRIMARY_HORIZON == fold.assessment_start
        assert fold.train_end_exclusive < fold.assessment_start < fold.assessment_end_exclusive
        assert fold.assessment_start >= previous_assessment_start
        assert fold.assessment_end_exclusive <= RESEARCH_END_EXCLUSIVE
        previous_assessment_start = fold.assessment_start


@pytest.mark.parametrize(
    ("hours", "research_last", "validation_last"),
    (
        (1, datetime(2025, 5, 31, 23, tzinfo=UTC), datetime(2026, 5, 31, 23, tzinfo=UTC)),
        (4, datetime(2025, 5, 31, 20, tzinfo=UTC), datetime(2026, 5, 31, 20, tzinfo=UTC)),
        (12, datetime(2025, 5, 31, 12, tzinfo=UTC), datetime(2026, 5, 31, 12, tzinfo=UTC)),
        (24, datetime(2025, 5, 31, tzinfo=UTC), datetime(2026, 5, 31, tzinfo=UTC)),
        (48, datetime(2025, 5, 30, tzinfo=UTC), datetime(2026, 5, 30, tzinfo=UTC)),
        (72, datetime(2025, 5, 29, tzinfo=UTC), datetime(2026, 5, 29, tzinfo=UTC)),
    ),
)
def test_primary_horizon_last_eligible_timestamps_are_exact_and_contained(
    hours: int, research_last: datetime, validation_last: datetime
) -> None:
    horizon = timedelta(hours=hours)
    assert last_target_eligible_timestamp(ExperimentSplit.RESEARCH, horizon) == research_last
    assert last_target_eligible_timestamp(ExperimentSplit.VALIDATION, horizon) == validation_last
    assert target_is_split_eligible(ExperimentSplit.RESEARCH, research_last, horizon)
    assert not target_is_split_eligible(
        ExperimentSplit.RESEARCH, research_last + timedelta(minutes=1), horizon
    )
    assert target_is_split_eligible(ExperimentSplit.VALIDATION, validation_last, horizon)
    assert not target_is_split_eligible(
        ExperimentSplit.VALIDATION, validation_last + timedelta(minutes=1), horizon
    )


def test_diagnostic_horizons_obey_split_boundaries_and_never_read_validation_prices() -> None:
    for horizon in DIAGNOSTIC_HORIZONS:
        last_research = RESEARCH_END_EXCLUSIVE - horizon
        entry, endpoint = future_return_endpoints(last_research, horizon)
        assert entry > last_research
        assert endpoint == RESEARCH_END_EXCLUSIVE
        assert target_is_split_eligible(ExperimentSplit.RESEARCH, last_research, horizon)
        assert not target_is_split_eligible(
            ExperimentSplit.RESEARCH, last_research + timedelta(minutes=1), horizon
        )


def test_walk_forward_target_endpoints_never_cross_partition_boundaries() -> None:
    for fold in WALK_FORWARD_FOLDS:
        for horizon in PRIMARY_HORIZONS + DIAGNOSTIC_HORIZONS:
            train_last = fold.train_end_exclusive - horizon
            assessment_last = fold.assessment_end_exclusive - horizon
            assert fold_target_is_eligible(fold, "TRAIN", train_last, horizon)
            assert not fold_target_is_eligible(
                fold, "TRAIN", train_last + timedelta(minutes=1), horizon
            )
            assert fold_target_is_eligible(fold, "ASSESSMENT", assessment_last, horizon)
            assert not fold_target_is_eligible(
                fold, "ASSESSMENT", assessment_last + timedelta(minutes=1), horizon
            )
            assert train_last + horizon <= fold.train_end_exclusive < fold.assessment_start


def test_metrics_probabilities_baselines_calibration_and_lead_time_are_predeclared() -> None:
    assert len(REQUIRED_PROBABILITIES) == 8
    assert {"balanced_accuracy", "brier_score", "event_recall"} <= set(PRIMARY_METRICS)
    assert BASELINES == (
        "naive_majority_class",
        "research_historical_frequency_probability",
        "btc_only_causal_price_volatility",
        "causal_persistence_regime",
    )
    assert len(CALIBRATION_BUCKETS) == 5
    assert CALIBRATION_BUCKETS[0] == (Decimal("0.5"), Decimal("0.6"))
    assert CALIBRATION_BUCKETS[-1] == (Decimal("0.9"), Decimal("1.0"))
    assert {"lead_time_median", "lead_time_p25", "false_early_warnings"} <= set(LEAD_TIME_METRICS)


def test_rare_event_calibration_is_separate_full_domain_and_validation_frozen() -> None:
    assert CALIBRATION_BUCKETS is DIRECTION_CONFIDENCE_BINS
    assert RARE_EVENT_CALIBRATION_EDGES[0] == 0
    assert RARE_EVENT_CALIBRATION_EDGES[-1] == 1
    assert all(
        left_upper == right_lower
        for (_, left_upper), (right_lower, _) in pairwise(RARE_EVENT_CALIBRATION_BINS)
    )
    assert rare_event_calibration_bin(Decimal(0)) == 0
    assert rare_event_calibration_bin(Decimal("0.0001")) == 0
    assert rare_event_calibration_bin(Decimal("0.49")) < len(RARE_EVENT_CALIBRATION_BINS)
    assert rare_event_calibration_bin(Decimal(1)) == len(RARE_EVENT_CALIBRATION_BINS) - 1
    assert RARE_EVENT_CALIBRATION_TARGETS == (
        "MOVE",
        "STRESS",
        "TSUNAMI",
        "UP_TSUNAMI",
        "DOWN_TSUNAMI",
    )
    assert CALIBRATION_BIN_REPORT_FIELDS == (
        "predicted_mean_probability",
        "realized_frequency",
        "sample_count",
        "calibration_error",
    )
    assert "Research and Validation use identical edges" in CALIBRATION_FREEZE_POLICY


def test_exact_features_families_missingness_and_allowed_models_are_frozen() -> None:
    flattened = tuple(feature for _, members in FEATURE_FAMILY_MEMBERSHIP for feature in members)
    assert flattened == FEATURE_ORDER
    assert len(flattened) == 15
    assert len(FEATURE_FAMILY_MEMBERSHIP) == 8
    assert "never future-fill" in MISSING_STATE_POLICY
    assert ALLOWED_MODEL_FAMILIES[0] == "unregularized_logistic_baseline"
    assert "deep" not in " ".join(ALLOWED_MODEL_FAMILIES)


def test_contribution_ablation_family_interaction_and_uncertainty_are_frozen() -> None:
    assert "held-out" in CONTRIBUTION_POLICY
    assert "seven-day-block" in CONTRIBUTION_POLICY
    assert "leave-one-feature-out" in CONTRIBUTION_POLICY
    assert "100% only" in CONTRIBUTION_POLICY
    assert ABLATION_METRICS == (
        "tsunami_event_pr_auc",
        "direction_balanced_accuracy",
        "tsunami_brier_score",
        "tsunami_pr_auc",
        "tsunami_event_recall",
    )
    assert "leave-family-out" in FAMILY_CONTRIBUTION_POLICY
    assert "incremental metric delta" in INTERACTION_POLICY
    assert "moving-block bootstrap" in UNCERTAINTY_POLICY
    assert "2,000 replicates" in UNCERTAINTY_POLICY


def test_confirmatory_validation_single_use_and_no_execution_or_trading() -> None:
    assert "24h" in MULTIPLE_TESTING_POLICY
    assert "Holm" in MULTIPLE_TESTING_POLICY
    assert "Benjamini-Hochberg" in MULTIPLE_TESTING_POLICY
    assert "exactly once" in VALIDATION_POLICY
    assert "never refit" in VALIDATION_POLICY
    assert VALIDATION_MAX_EXECUTIONS == 1
    assert VALIDATION_REUSE_FORBIDDEN is True
    assert ANALYTICAL_VIEW_REQUIRED is True
    assert EXPERIMENT_EXECUTION_STATUS == "NOT_RUN"
    assert VALIDATION_STATUS == "NOT_ACCESSED"
    assert PAPER_TRADING_STATUS == "NOT_RUN"
    assert TRADING_CALCULATIONS_ALLOWED is False
