import json
from collections import Counter
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from inspect import signature

import pytest

from app.research.market_precursor import analysis
from app.research.market_precursor import report as precursor_report
from app.research.market_precursor.analysis import (
    DISCOVERY_CONDITION_CATALOGUE_IDENTITY,
    NO_VALIDATED_PRECURSOR_FOUND_V1,
    PRIMARY_DISCOVERY_HORIZONS_V1,
    STRONG_VALIDATION_ABSOLUTE_LIFT_THRESHOLD_V1,
    STRONG_VALIDATION_RETENTION_THRESHOLD_V1,
    AllOfConditionV1,
    CandidateAssessmentReasonV1,
    ConditionalEffectV1,
    ConditionTemporalConcentrationV1,
    DiscoveryConditionIdentityV1,
    DiscoveryConditionTemplateV1,
    DiscoverySemanticRoleV1,
    EqualityConditionSpecV1,
    EqualityFeatureConditionV1,
    EventBaselineV1,
    EventDefinitionV1,
    EventDirectionV1,
    FrozenConditionObservationV1,
    FrozenConditionSplitEvidenceV1,
    FrozenFeatureQuantilesV1,
    FrozenHorizonOutcomeV1,
    FutureLabelV1,
    MultiHorizonFrozenConditionObservationV1,
    PairedFrozenConditionEvidenceV1,
    PrecursorCandidateClassificationV1,
    PrecursorRankingCategoryV1,
    QuantileBucketV1,
    QuantileConditionSpecV1,
    QuantileFeatureConditionV1,
    SampleClassV1,
    assess_precursor_candidate,
    baseline,
    bind_discovery_condition_template,
    bucket_for_role,
    calculate_condition_temporal_concentration,
    classify_sample,
    conditional_effect,
    discovery_condition_catalogue_v1,
    evaluate_frozen_condition_on_split,
    evaluate_frozen_condition_pair,
    evaluate_frozen_condition_stability,
    fit_feature_research_quantiles,
    no_validated_precursor_found,
    project_observation_for_horizon,
    rank_precursor_candidates,
)


def _labels():
    return tuple(
        FutureLabelV1(
            i, Decimal(value), Decimal(value) + Decimal("0.01"), Decimal(value) - Decimal("0.01")
        )
        for i, value in enumerate(("0.004", "-0.004", "0.006", "-0.006"))
    )


def test_events_baseline_and_conditional_effects_are_decimal_deterministic():
    labels = _labels()
    up = EventDefinitionV1(15, 40, EventDirectionV1.UP)
    down = EventDefinitionV1(60, 40, EventDirectionV1.DOWN)
    absolute = EventDefinitionV1(240, 60, EventDirectionV1.ABS)
    assert up.matches(labels[0]) and down.matches(labels[1]) and absolute.matches(labels[2])
    assert (
        baseline(labels, up)
        == baseline(labels, up)
        == type(baseline(labels, up))(4, 2, Decimal("0.5"))
    )
    result = conditional_effect(labels, up, lambda item: item.timestamp_ms % 2 == 0)
    assert result.occurrence_count == result.event_count == 2
    assert result.conditional_probability == Decimal(1)
    assert result.absolute_lift == Decimal("0.5") and result.relative_lift == Decimal(2)
    assert result.median_future_return == Decimal("0.005")


def test_boundaries_zero_baseline_medians_and_sample_policy():
    labels = _labels()
    assert EventDefinitionV1(5, 60, EventDirectionV1.UP).threshold == Decimal("0.006")
    zero = EventDefinitionV1(15, 100, EventDirectionV1.UP)
    result = conditional_effect(labels, zero, lambda item: item.timestamp_ms == 0)
    assert result.relative_lift is None and result.median_adverse_excursion == Decimal("-0.006")
    assert classify_sample(99) is SampleClassV1.INSUFFICIENT_SAMPLE
    assert classify_sample(100) is SampleClassV1.WEAK_SAMPLE
    assert classify_sample(249) is SampleClassV1.WEAK_SAMPLE
    assert classify_sample(250) is SampleClassV1.INFORMATIVE_SAMPLE


def test_event_false_cases_empty_baseline_and_empty_condition_are_explicit():
    labels = _labels()
    assert not EventDefinitionV1(5, 60, EventDirectionV1.UP).matches(labels[0])
    assert not EventDefinitionV1(60, 60, EventDirectionV1.DOWN).matches(labels[0])
    assert not EventDefinitionV1(240, 60, EventDirectionV1.ABS).matches(labels[0])
    with pytest.raises(ValueError, match="requires labels"):
        baseline((), EventDefinitionV1(15, 40, EventDirectionV1.UP))
    effect = conditional_effect(
        labels, EventDefinitionV1(15, 40, EventDirectionV1.UP), lambda _: False
    )
    assert effect.occurrence_count == 0
    assert effect.conditional_probability is effect.absolute_lift is effect.relative_lift is None
    assert (
        effect.median_future_return
        is effect.median_favorable_excursion
        is effect.median_adverse_excursion
        is None
    )


def test_decimal_medians_are_exact_for_odd_even_and_mixed_values():
    event = EventDefinitionV1(15, 40, EventDirectionV1.UP)
    labels = (
        FutureLabelV1(1, Decimal("-0.03"), Decimal("0.03"), Decimal("-0.04")),
        FutureLabelV1(2, Decimal("-0.01"), Decimal("0.02"), Decimal("-0.02")),
        FutureLabelV1(3, Decimal("0.01"), Decimal("0.01"), Decimal("-0.01")),
        FutureLabelV1(4, Decimal("0.03"), Decimal("0.04"), Decimal("-0.03")),
    )
    effect = conditional_effect(labels, event, lambda _: True)
    assert effect.median_future_return == Decimal("0.00")
    assert effect.median_favorable_excursion == Decimal("0.025")
    assert effect.median_adverse_excursion == Decimal("-0.025")


def test_feature_research_quantiles_are_decimal_frozen_and_deterministic():
    values = tuple(Decimal(value) for value in range(10))
    fitted = fit_feature_research_quantiles("feature", values)
    assert tuple(QuantileBucketV1) == (
        QuantileBucketV1.BOTTOM_10,
        QuantileBucketV1.BOTTOM_25,
        QuantileBucketV1.MIDDLE_50,
        QuantileBucketV1.TOP_25,
        QuantileBucketV1.TOP_10,
    )
    assert (fitted.p10, fitted.p25, fitted.p75, fitted.p90, fitted.sample_count) == (
        Decimal("0.9"),
        Decimal("2.25"),
        Decimal("6.75"),
        Decimal("8.1"),
        10,
    )
    assert fitted == fit_feature_research_quantiles("feature", values)
    with pytest.raises(ValueError):
        fit_feature_research_quantiles("feature", ())
    with pytest.raises(ValueError):
        fit_feature_research_quantiles("feature", (Decimal("NaN"),))
    for invalid in (Decimal("Infinity"), Decimal("-Infinity")):
        with pytest.raises(ValueError):
            fit_feature_research_quantiles("feature", (invalid,))
    identical = fit_feature_research_quantiles("same", (Decimal(2),) * 4)
    assert identical.p10 == identical.p25 == identical.p75 == identical.p90 == Decimal(2)


def _quantiles() -> FrozenFeatureQuantilesV1:
    return FrozenFeatureQuantilesV1(
        "feature", Decimal(10), Decimal(25), Decimal(75), Decimal(90), 100
    )


@pytest.mark.parametrize(
    ("bucket", "value", "expected"),
    (
        (QuantileBucketV1.BOTTOM_10, "9", True),
        (QuantileBucketV1.BOTTOM_10, "10", True),
        (QuantileBucketV1.BOTTOM_10, "11", False),
        (QuantileBucketV1.BOTTOM_25, "24", True),
        (QuantileBucketV1.BOTTOM_25, "25", True),
        (QuantileBucketV1.BOTTOM_25, "26", False),
        (QuantileBucketV1.MIDDLE_50, "24", False),
        (QuantileBucketV1.MIDDLE_50, "25", True),
        (QuantileBucketV1.MIDDLE_50, "50", True),
        (QuantileBucketV1.MIDDLE_50, "75", True),
        (QuantileBucketV1.MIDDLE_50, "76", False),
        (QuantileBucketV1.TOP_25, "74", False),
        (QuantileBucketV1.TOP_25, "75", True),
        (QuantileBucketV1.TOP_25, "76", True),
        (QuantileBucketV1.TOP_10, "89", False),
        (QuantileBucketV1.TOP_10, "90", True),
        (QuantileBucketV1.TOP_10, "91", True),
    ),
)
def test_quantile_conditions_match_exact_inclusive_bucket_boundaries(bucket, value, expected):
    condition = QuantileFeatureConditionV1("feature", bucket, _quantiles())
    assert condition.matches("feature", Decimal(value)) is expected
    assert condition.matches("feature", Decimal(value)) is expected


def test_quantile_condition_boundary_overlaps_are_preserved():
    quantiles = _quantiles()
    assert QuantileFeatureConditionV1("feature", QuantileBucketV1.BOTTOM_25, quantiles).matches(
        "feature", Decimal(25)
    )
    assert QuantileFeatureConditionV1("feature", QuantileBucketV1.MIDDLE_50, quantiles).matches(
        "feature", Decimal(25)
    )
    assert QuantileFeatureConditionV1("feature", QuantileBucketV1.MIDDLE_50, quantiles).matches(
        "feature", Decimal(75)
    )
    assert QuantileFeatureConditionV1("feature", QuantileBucketV1.TOP_25, quantiles).matches(
        "feature", Decimal(75)
    )


def test_quantile_condition_feature_binding_invalid_decimals_and_immutability():
    quantiles = _quantiles()
    with pytest.raises(ValueError, match="does not match"):
        QuantileFeatureConditionV1("other", QuantileBucketV1.TOP_10, quantiles)
    condition = QuantileFeatureConditionV1("feature", QuantileBucketV1.TOP_10, quantiles)
    with pytest.raises(ValueError, match="evaluation feature"):
        condition.matches("other", Decimal(100))
    for invalid in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
        with pytest.raises(ValueError, match="finite Decimal"):
            condition.matches("feature", invalid)
    with pytest.raises(ValueError, match="finite Decimal"):
        condition.matches("feature", 100)
    with pytest.raises(FrozenInstanceError):
        condition.feature_name = "other"
    with pytest.raises(FrozenInstanceError):
        condition.frozen_quantiles = quantiles


def test_equality_condition_has_exact_feature_value_and_type_semantics_and_is_frozen():
    condition = EqualityFeatureConditionV1("agrees", True)
    assert condition.matches("agrees", True)
    assert condition.matches("agrees", True)
    assert not condition.matches("agrees", False)
    with pytest.raises(ValueError, match="evaluation feature"):
        condition.matches("other", True)
    assert not condition.matches("agrees", 1)
    assert not EqualityFeatureConditionV1("agrees", False).matches("agrees", 0)
    assert not condition.matches("agrees", "true")
    with pytest.raises(FrozenInstanceError):
        condition.expected_value = False


def _top_condition(feature_name: str = "level") -> QuantileFeatureConditionV1:
    quantiles = FrozenFeatureQuantilesV1(
        feature_name, Decimal(10), Decimal(25), Decimal(75), Decimal(90), 100
    )
    return QuantileFeatureConditionV1(feature_name, QuantileBucketV1.TOP_25, quantiles)


def test_all_of_matches_two_or_three_true_children_and_repeats_deterministically():
    two = AllOfConditionV1((_top_condition(), EqualityFeatureConditionV1("agrees", True)))
    features = {"level": Decimal(80), "agrees": True}
    assert two.matches(features)
    assert two.matches(features)
    three = AllOfConditionV1((*two.children, EqualityFeatureConditionV1("participates", "yes")))
    assert three.matches({**features, "participates": "yes"})


def test_all_of_false_missing_and_invalid_inputs_are_explicit():
    condition = AllOfConditionV1((_top_condition(), EqualityFeatureConditionV1("agrees", True)))
    assert not condition.matches({"level": Decimal(80), "agrees": False})
    with pytest.raises(ValueError, match="required feature is missing"):
        condition.matches({"level": Decimal(80)})
    with pytest.raises(ValueError, match="finite Decimal"):
        condition.matches({"level": Decimal("NaN"), "agrees": False})


def test_all_of_rejects_empty_duplicate_and_unsupported_children_and_is_immutable():
    with pytest.raises(ValueError, match="at least one child"):
        AllOfConditionV1(())
    with pytest.raises(ValueError, match="unique child features"):
        AllOfConditionV1((_top_condition(), EqualityFeatureConditionV1("level", True)))
    with pytest.raises(ValueError, match="unsupported child"):
        AllOfConditionV1((object(),))
    children = [_top_condition(), EqualityFeatureConditionV1("agrees", True)]
    condition = AllOfConditionV1(children)
    children.clear()
    assert len(condition.children) == 2
    with pytest.raises(FrozenInstanceError):
        condition.children = ()


def test_all_of_preserves_order_for_deterministic_equality():
    first = _top_condition()
    second = EqualityFeatureConditionV1("agrees", True)
    forward = AllOfConditionV1((first, second))
    reverse = AllOfConditionV1((second, first))
    assert forward == AllOfConditionV1((first, second))
    assert forward != reverse
    features = {"level": Decimal(80), "agrees": True}
    unchanged = features.copy()
    assert forward.matches(features) is reverse.matches(features) is True
    assert features == unchanged


def test_discovery_semantic_roles_and_fixed_bucket_policy_are_exact():
    expected = {
        DiscoverySemanticRoleV1.STRONG_HIGH: QuantileBucketV1.TOP_10,
        DiscoverySemanticRoleV1.HIGH: QuantileBucketV1.TOP_25,
        DiscoverySemanticRoleV1.ELEVATED: QuantileBucketV1.TOP_25,
        DiscoverySemanticRoleV1.HIGH_PARTICIPATION: QuantileBucketV1.TOP_25,
        DiscoverySemanticRoleV1.STRONG_LOW: QuantileBucketV1.BOTTOM_10,
        DiscoverySemanticRoleV1.LOW: QuantileBucketV1.BOTTOM_25,
        DiscoverySemanticRoleV1.COMPRESSED: QuantileBucketV1.BOTTOM_25,
        DiscoverySemanticRoleV1.MUTED: QuantileBucketV1.MIDDLE_50,
    }
    assert len(DiscoverySemanticRoleV1) == 8
    assert {role: bucket_for_role(role) for role in DiscoverySemanticRoleV1} == expected
    assert bucket_for_role(DiscoverySemanticRoleV1.HIGH) is bucket_for_role(
        DiscoverySemanticRoleV1.HIGH
    )
    with pytest.raises(TypeError, match="unknown discovery semantic role"):
        bucket_for_role("HIGH")


def test_role_policy_accepts_no_data_and_no_mining_api_exists():
    assert tuple(signature(bucket_for_role).parameters) == ("role",)
    for name in (
        "generate_combinations",
        "optimize_roles",
        "choose_bucket",
        "best_condition",
        "search_conditions",
        "optimize_threshold",
    ):
        assert not hasattr(analysis, name)


def _q(feature_name: str, bucket: QuantileBucketV1) -> QuantileConditionSpecV1:
    return QuantileConditionSpecV1(feature_name, bucket)


def test_discovery_condition_catalogue_identity_cardinality_and_order_are_frozen():
    catalogue = discovery_condition_catalogue_v1()
    assert catalogue.identity == "chappie_precursor_condition_catalogue/1.0.0"
    assert catalogue.identity == DISCOVERY_CONDITION_CATALOGUE_IDENTITY
    assert tuple(entry.identity for entry in catalogue.entries) == tuple(
        DiscoveryConditionIdentityV1
    )
    assert len(catalogue.entries) == len(DiscoveryConditionIdentityV1) == 8
    assert catalogue == discovery_condition_catalogue_v1()
    with pytest.raises(FrozenInstanceError):
        catalogue.entries = ()
    with pytest.raises(ValueError, match="identity is invalid"):
        type(catalogue)("other", catalogue.entries)
    with pytest.raises(ValueError, match="entries are invalid"):
        type(catalogue)(catalogue.identity, catalogue.entries[:-1])


def test_catalogue_template_and_catalogue_copy_mutable_inputs_to_tuples():
    template = discovery_condition_catalogue_v1().entries[0]
    children = list(template.children)
    copied_template = type(template)(template.identity, children)
    children.clear()
    assert copied_template.children == template.children
    entries = list(discovery_condition_catalogue_v1().entries)
    copied_catalogue = type(discovery_condition_catalogue_v1())(
        DISCOVERY_CONDITION_CATALOGUE_IDENTITY, entries
    )
    entries.clear()
    assert copied_catalogue == discovery_condition_catalogue_v1()


def test_discovery_condition_catalogue_exact_canonical_specs():
    entries = discovery_condition_catalogue_v1().entries
    assert entries[0].children == (
        _q("cross_market.eth_minus_btc_return_15m", QuantileBucketV1.TOP_10),
        _q("cross_market.sol_minus_btc_return_15m", QuantileBucketV1.TOP_10),
    )
    assert entries[1].children == (
        _q("cross_market.eth_minus_btc_return_15m", QuantileBucketV1.TOP_10),
        _q("eth.volume_ratio_60m", QuantileBucketV1.TOP_25),
    )
    assert entries[2].children == (
        _q("cross_market.sol_minus_btc_return_15m", QuantileBucketV1.TOP_10),
        _q("sol.volume_ratio_60m", QuantileBucketV1.TOP_25),
    )
    assert entries[3].children == (
        _q("cross_market.eth_minus_btc_return_15m", QuantileBucketV1.TOP_25),
        _q("cross_market.sol_minus_btc_return_15m", QuantileBucketV1.TOP_25),
        _q("volatility_compression_ratio", QuantileBucketV1.BOTTOM_25),
    )
    assert entries[4].children == (
        EqualityConditionSpecV1("cross_market.all_three_direction_agreement", True),
        _q("breakout_proximity", QuantileBucketV1.BOTTOM_25),
    )
    assert entries[5].children == (
        _q("eth.acceleration_5m_vs_15m", QuantileBucketV1.TOP_25),
        _q("sol.acceleration_5m_vs_15m", QuantileBucketV1.TOP_25),
        _q("return_15m", QuantileBucketV1.MIDDLE_50),
    )
    assert entries[6].children == (
        _q("eth.atr_14_bps", QuantileBucketV1.TOP_25),
        _q("sol.atr_14_bps", QuantileBucketV1.TOP_25),
        _q("volatility_compression_ratio", QuantileBucketV1.BOTTOM_25),
    )
    assert entries[7].children == (
        _q("cross_market.eth_minus_btc_return_15m", QuantileBucketV1.BOTTOM_10),
        _q("cross_market.sol_minus_btc_return_15m", QuantileBucketV1.BOTTOM_10),
    )


def test_catalogue_specs_have_no_thresholds_registration_or_mining_api():
    catalogue = discovery_condition_catalogue_v1()
    quantile_specs = tuple(
        child
        for entry in catalogue.entries
        for child in entry.children
        if isinstance(child, QuantileConditionSpecV1)
    )
    assert quantile_specs
    assert all(set(spec.__slots__) == {"feature_name", "bucket"} for spec in quantile_specs)
    assert tuple(signature(discovery_condition_catalogue_v1).parameters) == ()
    for name in (
        "register_condition",
        "append_condition",
        "generate_combinations",
        "search_conditions",
        "best_condition",
        "choose_bucket",
        "grid_search",
        "threshold_optimizer",
    ):
        assert not hasattr(analysis, name)


def _frozen(feature_name: str) -> FrozenFeatureQuantilesV1:
    return FrozenFeatureQuantilesV1(
        feature_name, Decimal(10), Decimal(25), Decimal(75), Decimal(90), 100
    )


def test_catalogue_binder_preserves_specs_order_and_ignores_extras_without_fitting(monkeypatch):
    entries = discovery_condition_catalogue_v1().entries
    two_child = entries[0]
    sources = {
        "cross_market.eth_minus_btc_return_15m": _frozen("cross_market.eth_minus_btc_return_15m"),
        "cross_market.sol_minus_btc_return_15m": _frozen("cross_market.sol_minus_btc_return_15m"),
        "unused": _frozen("unused"),
    }
    monkeypatch.setattr(
        analysis,
        "fit_feature_research_quantiles",
        lambda *_args, **_kwargs: pytest.fail("binder must not fit quantiles"),
    )
    bound = bind_discovery_condition_template(two_child, sources)
    assert tuple(child.feature_name for child in bound.children) == tuple(
        spec.feature_name for spec in two_child.children
    )
    assert tuple(child.bucket for child in bound.children) == (
        QuantileBucketV1.TOP_10,
        QuantileBucketV1.TOP_10,
    )
    assert bound == bind_discovery_condition_template(two_child, sources)
    assert bound == bind_discovery_condition_template(
        two_child, {name: source for name, source in sources.items() if name != "unused"}
    )


def test_catalogue_binder_handles_three_children_equality_and_invalid_sources():
    entries = discovery_condition_catalogue_v1().entries
    three_child = entries[3]
    sources = {spec.feature_name: _frozen(spec.feature_name) for spec in three_child.children}
    bound = bind_discovery_condition_template(three_child, sources)
    assert len(bound.children) == 3
    equality_template = entries[4]
    equality_bound = bind_discovery_condition_template(
        equality_template, {"breakout_proximity": _frozen("breakout_proximity")}
    )
    assert equality_bound.children[0] == EqualityFeatureConditionV1(
        "cross_market.all_three_direction_agreement", True
    )
    with pytest.raises(ValueError, match="required frozen quantiles are missing"):
        bind_discovery_condition_template(three_child, {})
    wrong = dict(sources)
    first_name = three_child.children[0].feature_name
    wrong[first_name] = _frozen("wrong")
    with pytest.raises(ValueError, match="does not match frozen quantiles"):
        bind_discovery_condition_template(three_child, wrong)


def _observation(timestamp: int, level: str, future_return: str) -> FrozenConditionObservationV1:
    future = Decimal(future_return)
    return FrozenConditionObservationV1(
        timestamp,
        {"level": Decimal(level)},
        future,
        future + Decimal("0.01"),
        future - Decimal("0.01"),
    )


def test_split_evidence_has_local_baseline_effect_medians_and_sample_policy():
    condition = AllOfConditionV1((_top_condition(),))
    event = EventDefinitionV1(15, 100, EventDirectionV1.UP)
    observations = (
        _observation(1, "80", "0.02"),
        _observation(2, "20", "0.02"),
        _observation(3, "90", "-0.01"),
        _observation(4, "10", "-0.01"),
    )
    evidence = evaluate_frozen_condition_on_split(
        observations,
        condition,
        event,
        "FEATURE_RESEARCH",
        DiscoveryConditionIdentityV1.JOINT_POSITIVE_RELATIVE_MOMENTUM,
    )
    assert evidence == evaluate_frozen_condition_on_split(
        observations,
        condition,
        event,
        "FEATURE_RESEARCH",
        DiscoveryConditionIdentityV1.JOINT_POSITIVE_RELATIVE_MOMENTUM,
    )
    assert evidence.frozen_condition is condition
    assert evidence.baseline_result.probability == Decimal("0.5")
    effect = evidence.conditional_effect_result
    assert (effect.occurrence_count, effect.event_count) == (2, 1)
    assert effect.conditional_probability == Decimal("0.5")
    assert effect.absolute_lift == Decimal(0)
    assert effect.relative_lift == Decimal(1)
    assert effect.median_future_return == Decimal("0.005")
    assert effect.median_favorable_excursion == Decimal("0.015")
    assert effect.median_adverse_excursion == Decimal("-0.005")
    assert effect.sample_class is SampleClassV1.INSUFFICIENT_SAMPLE


def test_paired_evidence_freezes_condition_event_and_split_local_baselines():
    identity = DiscoveryConditionIdentityV1.JOINT_POSITIVE_RELATIVE_MOMENTUM
    quantiles = fit_feature_research_quantiles("level", (Decimal(value) for value in range(100)))
    template = DiscoveryConditionTemplateV1(identity, (_q("level", QuantileBucketV1.TOP_25),))
    condition = bind_discovery_condition_template(template, {"level": quantiles})
    event = EventDefinitionV1(15, 100, EventDirectionV1.UP)
    research = (
        _observation(1, "80", "0.02"),
        _observation(2, "20", "0.02"),
        _observation(3, "90", "-0.01"),
        _observation(4, "10", "-0.01"),
    )
    validation = (
        _observation(10, "80", "0.02"),
        _observation(11, "90", "0.03"),
        _observation(12, "10", "-0.01"),
    )
    before = condition
    paired = evaluate_frozen_condition_pair(identity, condition, event, research, validation)
    assert paired.frozen_condition is condition
    assert paired.feature_research.frozen_condition is condition
    assert paired.validation.frozen_condition is condition
    assert paired.feature_research.event_definition == paired.validation.event_definition == event
    assert paired.feature_research.baseline_result.probability == Decimal("0.5")
    assert paired.validation.baseline_result.probability == Decimal(2) / Decimal(3)
    extreme = tuple(
        _observation(item.timestamp_ms, "1000", str(item.future_return)) for item in validation
    )
    changed = evaluate_frozen_condition_pair(identity, condition, event, research, extreme)
    assert changed.feature_research == paired.feature_research
    assert changed.validation != paired.validation
    assert changed.frozen_condition == before == condition
    assert condition.children[0].frozen_quantiles == quantiles
    mismatched_validation = replace(
        paired.validation,
        event_definition=EventDefinitionV1(15, 200, EventDirectionV1.UP),
    )
    with pytest.raises(ValueError, match="paired evidence identity is inconsistent"):
        PairedFrozenConditionEvidenceV1(
            identity,
            condition,
            event,
            paired.feature_research,
            mismatched_validation,
        )


def test_split_evaluation_rejects_missing_invalid_and_unordered_observations():
    condition = AllOfConditionV1((_top_condition(),))
    event = EventDefinitionV1(15, 100, EventDirectionV1.UP)
    identity = DiscoveryConditionIdentityV1.JOINT_POSITIVE_RELATIVE_MOMENTUM
    missing = FrozenConditionObservationV1(1, {}, Decimal(0), Decimal(0), Decimal(0))
    with pytest.raises(ValueError, match="required feature is missing"):
        evaluate_frozen_condition_on_split((missing,), condition, event, "split", identity)
    invalid = FrozenConditionObservationV1(
        1, {"level": Decimal("NaN")}, Decimal(0), Decimal(0), Decimal(0)
    )
    with pytest.raises(ValueError, match="finite Decimal"):
        evaluate_frozen_condition_on_split((invalid,), condition, event, "split", identity)
    with pytest.raises(ValueError, match="strictly increasing"):
        evaluate_frozen_condition_on_split(
            (_observation(2, "80", "0"), _observation(2, "80", "0")),
            condition,
            event,
            "split",
            identity,
        )
    with pytest.raises(ValueError, match="strictly increasing"):
        evaluate_frozen_condition_on_split(
            (_observation(3, "80", "0"), _observation(2, "80", "0")),
            condition,
            event,
            "split",
            identity,
        )


def test_observation_copies_feature_mapping_and_is_immutable():
    features = {"level": Decimal(80)}
    observation = FrozenConditionObservationV1(1, features, Decimal(0), Decimal(0), Decimal(0))
    features["level"] = Decimal(0)
    assert observation.features["level"] == Decimal(80)
    with pytest.raises(TypeError):
        observation.features["level"] = Decimal(0)
    with pytest.raises(FrozenInstanceError):
        observation.timestamp_ms = 2


def _temporal_observations(day_counts: tuple[tuple[str, int], ...]):
    observations = []
    for day, count in day_counts:
        start = datetime.fromisoformat(day).replace(tzinfo=UTC)
        observations.extend(
            FrozenConditionObservationV1(
                int((start + timedelta(seconds=index)).timestamp() * 1000),
                {"level": Decimal(80)},
                Decimal(0),
                Decimal(0),
                Decimal(0),
            )
            for index in range(count)
        )
    return tuple(sorted(observations, key=lambda item: item.timestamp_ms))


def test_temporal_concentration_spread_day_thresholds_and_determinism():
    condition = AllOfConditionV1((_top_condition(),))
    spread = _temporal_observations(tuple((f"2025-01-{day:02d}", 10) for day in range(1, 11)))
    evidence = calculate_condition_temporal_concentration(spread, condition)
    assert evidence == calculate_condition_temporal_concentration(spread, condition)
    assert (evidence.occurrence_count, evidence.distinct_day_count) == (100, 10)
    assert evidence.largest_day_share == Decimal("0.1")
    assert not evidence.cluster_concentrated
    exact = calculate_condition_temporal_concentration(
        _temporal_observations(
            (("2025-01-01", 25), ("2025-01-02", 25), ("2025-01-08", 25), ("2025-01-09", 25))
        ),
        condition,
    )
    assert exact.largest_day_share == Decimal("0.25")
    assert not exact.cluster_concentrated
    above = calculate_condition_temporal_concentration(
        _temporal_observations(
            (("2025-01-01", 26), ("2025-01-02", 24), ("2025-01-08", 25), ("2025-01-09", 25))
        ),
        condition,
    )
    assert above.largest_day_share == Decimal("0.26")
    assert above.cluster_concentrated


def test_temporal_concentration_week_thresholds_use_iso_year_week():
    condition = AllOfConditionV1((_top_condition(),))
    exact = calculate_condition_temporal_concentration(
        _temporal_observations(
            (
                ("2025-01-06", 20),
                ("2025-01-07", 20),
                ("2025-01-08", 20),
                ("2025-01-13", 20),
                ("2025-01-14", 20),
            )
        ),
        condition,
    )
    assert (exact.distinct_week_count, exact.largest_week_share) == (2, Decimal("0.6"))
    assert not exact.cluster_concentrated
    above = calculate_condition_temporal_concentration(
        _temporal_observations(
            (
                ("2025-01-06", 21),
                ("2025-01-07", 20),
                ("2025-01-08", 20),
                ("2025-01-13", 20),
                ("2025-01-14", 19),
            )
        ),
        condition,
    )
    assert above.largest_week_share == Decimal("0.61")
    assert above.cluster_concentrated
    boundary = calculate_condition_temporal_concentration(
        _temporal_observations((("2024-12-30", 50), ("2025-01-01", 50))), condition
    )
    assert boundary.distinct_week_count == 1


def test_temporal_concentration_zero_small_sample_utc_days_and_occurrences_only():
    condition = AllOfConditionV1((_top_condition(),))
    zero = calculate_condition_temporal_concentration(
        (
            FrozenConditionObservationV1(
                0, {"level": Decimal(0)}, Decimal(1), Decimal(1), Decimal(1)
            ),
        ),
        condition,
    )
    assert zero == ConditionTemporalConcentrationV1(0, 0, 0, None, 0, 0, None, False)
    small = calculate_condition_temporal_concentration(
        _temporal_observations((("2025-01-01", 99),)), condition
    )
    assert small.largest_day_share == Decimal(1)
    assert not small.cluster_concentrated
    one_day = calculate_condition_temporal_concentration(
        _temporal_observations((("2025-01-01", 100),)), condition
    )
    assert one_day.cluster_concentrated
    utc_boundary = calculate_condition_temporal_concentration(
        _temporal_observations((("2025-01-01T23:59:00", 1), ("2025-01-02T00:00:00", 1))),
        condition,
    )
    assert utc_boundary.distinct_day_count == 2
    duplicate = _temporal_observations((("2025-01-01", 1),))
    with pytest.raises(ValueError, match="strictly increasing"):
        calculate_condition_temporal_concentration((*duplicate, *duplicate), condition)
    with pytest.raises(ValueError, match="strictly increasing"):
        calculate_condition_temporal_concentration(
            (_observation(2, "80", "0"), _observation(1, "80", "0")), condition
        )
    missing = FrozenConditionObservationV1(1, {}, Decimal(0), Decimal(0), Decimal(0))
    with pytest.raises(ValueError, match="required feature is missing"):
        calculate_condition_temporal_concentration((missing,), condition)
    invalid = FrozenConditionObservationV1(
        1, {"level": Decimal("NaN")}, Decimal(0), Decimal(0), Decimal(0)
    )
    with pytest.raises(ValueError, match="finite Decimal"):
        calculate_condition_temporal_concentration((invalid,), condition)


def _effect(count: int, lift: Decimal | None) -> ConditionalEffectV1:
    probability = None if lift is None else Decimal("0.5") + lift
    return ConditionalEffectV1(
        count,
        0,
        Decimal("0.5"),
        probability,
        lift,
        None if probability is None else probability / Decimal("0.5"),
        None if probability is None else Decimal(0),
        None if probability is None else Decimal(0),
        None if probability is None else Decimal(0),
        classify_sample(count),
    )


def _concentration(count: int, concentrated: bool = False) -> ConditionTemporalConcentrationV1:
    if not count:
        return ConditionTemporalConcentrationV1(0, 0, 0, None, 0, 0, None, False)
    largest = count if concentrated else 1
    share = Decimal(largest) / Decimal(count)
    return ConditionTemporalConcentrationV1(
        count, count if not concentrated else 1, largest, share, count, largest, share, concentrated
    )


def _paired_stability(research_lift, validation_lift, research_count=250, validation_count=250):
    identity = DiscoveryConditionIdentityV1.JOINT_POSITIVE_RELATIVE_MOMENTUM
    condition = AllOfConditionV1((_top_condition(),))
    event = EventDefinitionV1(15, 100, EventDirectionV1.UP)
    research_effect = _effect(research_count, research_lift)
    validation_effect = _effect(validation_count, validation_lift)
    research = FrozenConditionSplitEvidenceV1(
        "FEATURE_RESEARCH",
        identity,
        condition,
        event,
        EventBaselineV1(research_count, 0, Decimal("0.5")),
        research_effect,
    )
    validation = FrozenConditionSplitEvidenceV1(
        "VALIDATION",
        identity,
        condition,
        event,
        EventBaselineV1(validation_count, 0, Decimal("0.5")),
        validation_effect,
    )
    return PairedFrozenConditionEvidenceV1(identity, condition, event, research, validation)


@pytest.mark.parametrize(
    ("research_lift", "validation_lift", "agrees"),
    (
        (Decimal("0.2"), Decimal("0.1"), True),
        (Decimal("-0.2"), Decimal("-0.1"), True),
        (Decimal("0.2"), Decimal("-0.1"), False),
        (Decimal("-0.2"), Decimal("0.1"), False),
        (Decimal(0), Decimal("0.1"), False),
        (None, None, False),
    ),
)
def test_stability_lift_direction_and_retention(research_lift, validation_lift, agrees):
    count = 0 if research_lift is None else 250
    validation_count = 0 if validation_lift is None else 250
    evidence = evaluate_frozen_condition_stability(
        _paired_stability(research_lift, validation_lift, count, validation_count),
        _concentration(count),
        _concentration(validation_count),
    )
    assert evidence.lift_direction_agrees is agrees
    expected_retention = Decimal("0.5") if agrees else None
    assert evidence.validation_lift_retention_ratio == expected_retention


def test_stability_sample_adequacy_clusters_and_stable_evidence_rule():
    paired = _paired_stability(Decimal("0.2"), Decimal("0.1"))
    stable = evaluate_frozen_condition_stability(paired, _concentration(250), _concentration(250))
    assert stable.validation_sample_adequate
    assert stable.validation_lift_nonzero
    assert stable.stable_evidence
    research_weak = _paired_stability(Decimal("0.2"), Decimal("0.1"), 200, 250)
    assert not evaluate_frozen_condition_stability(
        research_weak, _concentration(200), _concentration(250)
    ).stable_evidence
    validation_weak = _paired_stability(Decimal("0.2"), Decimal("0.1"), 250, 249)
    weak = evaluate_frozen_condition_stability(
        validation_weak, _concentration(250), _concentration(249)
    )
    assert not weak.validation_sample_adequate
    assert not weak.stable_evidence
    assert not evaluate_frozen_condition_stability(
        paired, _concentration(250, True), _concentration(250)
    ).stable_evidence
    assert not evaluate_frozen_condition_stability(
        paired, _concentration(250), _concentration(250, True)
    ).stable_evidence


def test_negative_lift_retention_uses_magnitudes_and_zero_research_is_none():
    negative = evaluate_frozen_condition_stability(
        _paired_stability(Decimal("-0.4"), Decimal("-0.1")),
        _concentration(250),
        _concentration(250),
    )
    assert negative.validation_lift_retention_ratio == Decimal("0.25")
    zero = evaluate_frozen_condition_stability(
        _paired_stability(Decimal(0), Decimal("0.1")),
        _concentration(250),
        _concentration(250),
    )
    assert zero.validation_lift_retention_ratio is None


def _assessment(
    identity=DiscoveryConditionIdentityV1.JOINT_POSITIVE_RELATIVE_MOMENTUM,
    direction=EventDirectionV1.UP,
    research_lift=Decimal("0.04"),
    validation_lift=Decimal("0.02"),
    research_count=250,
    validation_count=250,
    research_cluster=False,
    validation_cluster=False,
):
    paired = _paired_stability(research_lift, validation_lift, research_count, validation_count)
    event = EventDefinitionV1(15, 100, direction)
    research = replace(paired.feature_research, condition_identity=identity, event_definition=event)
    validation = replace(paired.validation, condition_identity=identity, event_definition=event)
    paired = PairedFrozenConditionEvidenceV1(
        identity, paired.frozen_condition, event, research, validation
    )
    research_concentration = _concentration(research_count, research_cluster)
    validation_concentration = _concentration(validation_count, validation_cluster)
    stability = evaluate_frozen_condition_stability(
        paired, research_concentration, validation_concentration
    )
    return assess_precursor_candidate(
        paired, stability, research_concentration, validation_concentration
    )


@pytest.mark.parametrize(
    ("kwargs", "classification", "reason"),
    (
        (
            {"research_lift": None, "research_count": 0},
            PrecursorCandidateClassificationV1.NO_EFFECT,
            CandidateAssessmentReasonV1.RESEARCH_LIFT_UNAVAILABLE,
        ),
        (
            {"research_lift": Decimal(0)},
            PrecursorCandidateClassificationV1.NO_EFFECT,
            CandidateAssessmentReasonV1.RESEARCH_LIFT_ZERO,
        ),
        (
            {"research_count": 249},
            PrecursorCandidateClassificationV1.NO_EFFECT,
            CandidateAssessmentReasonV1.RESEARCH_SAMPLE_NOT_INFORMATIVE,
        ),
        (
            {"validation_count": 249},
            PrecursorCandidateClassificationV1.RESEARCH_ONLY,
            CandidateAssessmentReasonV1.VALIDATION_SAMPLE_NOT_INFORMATIVE,
        ),
        (
            {"validation_lift": Decimal("-0.02")},
            PrecursorCandidateClassificationV1.RESEARCH_ONLY,
            CandidateAssessmentReasonV1.LIFT_DIRECTION_MISMATCH,
        ),
        (
            {"validation_lift": Decimal(0)},
            PrecursorCandidateClassificationV1.RESEARCH_ONLY,
            CandidateAssessmentReasonV1.VALIDATION_LIFT_ZERO,
        ),
        (
            {"validation_cluster": True},
            PrecursorCandidateClassificationV1.RESEARCH_ONLY,
            CandidateAssessmentReasonV1.CLUSTER_CONCENTRATED,
        ),
    ),
)
def test_candidate_classification_fail_closed_cases(kwargs, classification, reason):
    assessment = _assessment(**kwargs)
    assert assessment.classification is classification
    assert reason in assessment.reasons


def test_supported_and_strong_classification_threshold_boundaries_are_frozen():
    assert STRONG_VALIDATION_ABSOLUTE_LIFT_THRESHOLD_V1 == Decimal("0.02")
    assert STRONG_VALIDATION_RETENTION_THRESHOLD_V1 == Decimal("0.50")
    exact = _assessment(research_lift=Decimal("0.04"), validation_lift=Decimal("0.02"))
    assert exact.classification is PrecursorCandidateClassificationV1.STRONG_VALIDATION_CANDIDATE
    assert CandidateAssessmentReasonV1.STRONG_LIFT_THRESHOLD_MET in exact.reasons
    assert CandidateAssessmentReasonV1.RETENTION_THRESHOLD_MET in exact.reasons
    below_lift = _assessment(research_lift=Decimal("0.038"), validation_lift=Decimal("0.019"))
    assert below_lift.classification is PrecursorCandidateClassificationV1.VALIDATION_SUPPORTED
    below_retention = _assessment(research_lift=Decimal("0.041"), validation_lift=Decimal("0.02"))
    assert below_retention.classification is PrecursorCandidateClassificationV1.VALIDATION_SUPPORTED
    downside = _assessment(
        direction=EventDirectionV1.DOWN,
        research_lift=Decimal("-0.04"),
        validation_lift=Decimal("-0.02"),
    )
    assert downside.classification is PrecursorCandidateClassificationV1.STRONG_VALIDATION_CANDIDATE


def test_ranking_categories_eligibility_strength_and_top_n_are_deterministic():
    strong = _assessment()
    supported = _assessment(research_lift=Decimal("0.25"), validation_lift=Decimal("0.10"))
    research_only = _assessment(validation_count=249)
    no_effect = _assessment(research_lift=Decimal(0))
    downside = _assessment(direction=EventDirectionV1.DOWN)
    volatility = _assessment(direction=EventDirectionV1.ABS)
    assert strong.ranking_category is PrecursorRankingCategoryV1.UPSIDE_PRECURSORS
    assert downside.ranking_category is PrecursorRankingCategoryV1.DOWNSIDE_PRECURSORS
    assert volatility.ranking_category is PrecursorRankingCategoryV1.VOLATILITY_PRECURSORS
    assessments = (supported, research_only, no_effect, strong, downside, volatility)
    ranked = rank_precursor_candidates(
        assessments, PrecursorRankingCategoryV1.UPSIDE_PRECURSORS, limit=1
    )
    assert ranked == (strong,)
    assert ranked == rank_precursor_candidates(
        assessments, PrecursorRankingCategoryV1.UPSIDE_PRECURSORS, limit=1
    )
    with pytest.raises(ValueError, match="ranking limit is invalid"):
        rank_precursor_candidates(
            assessments, PrecursorRankingCategoryV1.UPSIDE_PRECURSORS, limit=0
        )


def _replace_validation_effect(assessment, **changes):
    validation = assessment.paired_evidence.validation
    effect = replace(validation.conditional_effect_result, **changes)
    validation = replace(validation, conditional_effect_result=effect)
    paired = replace(assessment.paired_evidence, validation=validation)
    return replace(assessment, paired_evidence=paired)


def test_ranking_numeric_and_identity_tie_breaks_are_exact():
    low_lift = _assessment(
        DiscoveryConditionIdentityV1.ETH_MOMENTUM_WITH_PARTICIPATION,
        research_lift=Decimal("0.04"),
        validation_lift=Decimal("0.02"),
    )
    high_lift = _assessment(
        DiscoveryConditionIdentityV1.SOL_MOMENTUM_WITH_PARTICIPATION,
        research_lift=Decimal("0.05"),
        validation_lift=Decimal("0.03"),
    )
    assert (
        rank_precursor_candidates(
            (low_lift, high_lift), PrecursorRankingCategoryV1.UPSIDE_PRECURSORS
        )[0]
        is high_lift
    )
    lower_relative = _replace_validation_effect(low_lift, relative_lift=Decimal("1.1"))
    higher_relative = _replace_validation_effect(
        _assessment(DiscoveryConditionIdentityV1.SOL_MOMENTUM_WITH_PARTICIPATION),
        relative_lift=Decimal("1.2"),
    )
    assert (
        rank_precursor_candidates(
            (lower_relative, higher_relative), PrecursorRankingCategoryV1.UPSIDE_PRECURSORS
        )[0]
        is higher_relative
    )
    lower_n = _replace_validation_effect(low_lift, occurrence_count=250)
    higher_n = _replace_validation_effect(
        _assessment(DiscoveryConditionIdentityV1.SOL_MOMENTUM_WITH_PARTICIPATION),
        occurrence_count=300,
    )
    assert (
        rank_precursor_candidates(
            (lower_n, higher_n), PrecursorRankingCategoryV1.UPSIDE_PRECURSORS
        )[0]
        is higher_n
    )
    lower_retention = _assessment(
        DiscoveryConditionIdentityV1.ETH_MOMENTUM_WITH_PARTICIPATION,
        research_lift=Decimal("0.04"),
        validation_lift=Decimal("0.02"),
    )
    higher_retention = _assessment(
        DiscoveryConditionIdentityV1.SOL_MOMENTUM_WITH_PARTICIPATION,
        research_lift=Decimal("0.025"),
        validation_lift=Decimal("0.02"),
    )
    assert (
        rank_precursor_candidates(
            (lower_retention, higher_retention), PrecursorRankingCategoryV1.UPSIDE_PRECURSORS
        )[0]
        is higher_retention
    )


def test_ranking_dispersion_then_identity_tie_breaks():
    first = _assessment(DiscoveryConditionIdentityV1.ETH_MOMENTUM_WITH_PARTICIPATION)
    second = _assessment(DiscoveryConditionIdentityV1.SOL_MOMENTUM_WITH_PARTICIPATION)
    better_day = replace(
        second,
        validation_concentration=replace(
            second.validation_concentration, largest_day_share=Decimal("0.001")
        ),
    )
    assert (
        rank_precursor_candidates(
            (first, better_day), PrecursorRankingCategoryV1.UPSIDE_PRECURSORS
        )[0]
        is better_day
    )
    better_week = replace(
        second,
        validation_concentration=replace(
            second.validation_concentration,
            largest_day_share=first.validation_concentration.largest_day_share,
            largest_week_share=Decimal("0.001"),
        ),
    )
    assert (
        rank_precursor_candidates(
            (first, better_week), PrecursorRankingCategoryV1.UPSIDE_PRECURSORS
        )[0]
        is better_week
    )
    assert (
        rank_precursor_candidates((second, first), PrecursorRankingCategoryV1.UPSIDE_PRECURSORS)[
            0
        ].condition_identity
        is DiscoveryConditionIdentityV1.ETH_MOMENTUM_WITH_PARTICIPATION
    )


def test_no_validated_precursor_helper_uses_supported_classes_only():
    assert NO_VALIDATED_PRECURSOR_FOUND_V1 == "NO_VALIDATED_PRECURSOR_FOUND_V1"
    assert no_validated_precursor_found(
        (_assessment(research_lift=Decimal(0)), _assessment(validation_count=249))
    )
    assert not no_validated_precursor_found(
        (_assessment(research_lift=Decimal("0.038"), validation_lift=Decimal("0.019")),)
    )
    assert not no_validated_precursor_found((_assessment(),))


def _outcome(horizon: int, value: str | None = None) -> FrozenHorizonOutcomeV1:
    result = Decimal(value if value is not None else str(horizon))
    return FrozenHorizonOutcomeV1(
        horizon,
        result,
        result + Decimal("0.1"),
        result - Decimal("0.1"),
    )


def _multi_observation(outcomes=None, features=None) -> MultiHorizonFrozenConditionObservationV1:
    return MultiHorizonFrozenConditionObservationV1(
        123,
        {"feature": Decimal(1)} if features is None else features,
        (_outcome(240), _outcome(15), _outcome(60)) if outcomes is None else outcomes,
    )


def test_horizon_outcome_requires_finite_decimals_and_is_immutable():
    outcome = _outcome(15, "0.01")
    assert outcome.future_return == Decimal("0.01")
    with pytest.raises(FrozenInstanceError):
        outcome.future_return = Decimal(0)
    for invalid in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity"), 1.0):
        with pytest.raises(ValueError, match="finite Decimals"):
            FrozenHorizonOutcomeV1(15, invalid, Decimal(0), Decimal(0))


@pytest.mark.parametrize(
    "horizons",
    (
        (60, 240),
        (15, 240),
        (15, 60),
        (5, 15, 60, 240),
        (15, 60, 240, 300),
    ),
)
def test_multi_horizon_observation_rejects_missing_and_extra_horizons(horizons):
    with pytest.raises(ValueError, match="exactly 15, 60, and 240"):
        _multi_observation(tuple(_outcome(horizon) for horizon in horizons))


def test_multi_horizon_observation_rejects_duplicate_horizons():
    with pytest.raises(ValueError, match="duplicates"):
        _multi_observation((_outcome(15), _outcome(60), _outcome(240), _outcome(15)))


def test_multi_horizon_observation_copies_inputs_canonicalizes_and_is_deterministic():
    assert PRIMARY_DISCOVERY_HORIZONS_V1 == (15, 60, 240)
    features = {"feature": Decimal(1)}
    outcomes = [_outcome(240), _outcome(15), _outcome(60)]
    observation = _multi_observation(outcomes, features)
    features["feature"] = Decimal(2)
    outcomes.clear()
    assert observation.features["feature"] == Decimal(1)
    assert tuple(item.horizon_minutes for item in observation.outcomes) == (15, 60, 240)
    assert observation == _multi_observation((_outcome(60), _outcome(240), _outcome(15)))
    with pytest.raises(TypeError):
        observation.features["feature"] = Decimal(2)
    with pytest.raises(FrozenInstanceError):
        observation.outcomes = ()


@pytest.mark.parametrize("horizon", PRIMARY_DISCOVERY_HORIZONS_V1)
def test_multi_horizon_projection_is_exact_for_each_primary_horizon(horizon):
    observation = _multi_observation()
    projected = project_observation_for_horizon(observation, horizon)
    expected = _outcome(horizon)
    assert projected.timestamp_ms == observation.timestamp_ms
    assert dict(projected.features) == dict(observation.features)
    assert projected.future_return == expected.future_return
    assert projected.favorable_excursion == expected.favorable_excursion
    assert projected.adverse_excursion == expected.adverse_excursion
    with pytest.raises(ValueError, match="unsupported"):
        project_observation_for_horizon(observation, 5)


def test_multi_horizon_projection_does_not_reuse_cross_horizon_values():
    original = _multi_observation(
        (_outcome(15, "0.15"), _outcome(60, "0.60"), _outcome(240, "2.40"))
    )
    changed_15 = _multi_observation(
        (_outcome(15, "9.15"), _outcome(60, "0.60"), _outcome(240, "2.40"))
    )
    assert project_observation_for_horizon(original, 60) == project_observation_for_horizon(
        changed_15, 60
    )
    changed_60 = _multi_observation(
        (_outcome(15, "0.15"), _outcome(60, "9.60"), _outcome(240, "2.40"))
    )
    assert project_observation_for_horizon(original, 240) == project_observation_for_horizon(
        changed_60, 240
    )
    changed_240 = _multi_observation(
        (_outcome(15, "0.15"), _outcome(60, "0.60"), _outcome(240, "9.40"))
    )
    assert project_observation_for_horizon(original, 15) == project_observation_for_horizon(
        changed_240, 15
    )
    assert project_observation_for_horizon(original, 15).future_return == Decimal("0.15")
    assert project_observation_for_horizon(original, 60).future_return == Decimal("0.60")
    assert project_observation_for_horizon(original, 240).future_return == Decimal("2.40")


def _report_provenance():
    return precursor_report.DiscoveryProvenanceBindingsV1(
        "a" * 40,
        "base/1",
        "b" * 64,
        "context/1",
        "c" * 64,
        "authority/1",
        "d" * 64,
    )


def _report_observations(
    count: int, *, validation_shift: Decimal = Decimal(0), correlated_outcomes: bool = False
):
    start = datetime(2020, 1, 1, tzinfo=UTC)
    feature_names = (
        "breakout_proximity",
        "cross_market.eth_minus_btc_return_15m",
        "cross_market.sol_minus_btc_return_15m",
        "eth.acceleration_5m_vs_15m",
        "eth.atr_14_bps",
        "eth.volume_ratio_60m",
        "return_15m",
        "sol.acceleration_5m_vs_15m",
        "sol.atr_14_bps",
        "sol.volume_ratio_60m",
        "volatility_compression_ratio",
    )
    observations = []
    for index in range(count):
        value = Decimal(index) + validation_shift
        tail = correlated_outcomes and index >= count * 9 // 10
        outcome_15 = Decimal("0.10") if tail or not correlated_outcomes else Decimal(0)
        outcome_60 = Decimal("-0.10") if tail or not correlated_outcomes else Decimal(0)
        outcome_240 = Decimal("0.10") if tail else Decimal(0)
        features = {name: value for name in feature_names}
        features["cross_market.all_three_direction_agreement"] = True
        observations.append(
            MultiHorizonFrozenConditionObservationV1(
                int((start + timedelta(days=index)).timestamp() * 1000),
                features,
                (
                    FrozenHorizonOutcomeV1(
                        15, outcome_15, outcome_15 + Decimal("0.01"), Decimal("-0.01")
                    ),
                    FrozenHorizonOutcomeV1(
                        60, outcome_60, Decimal("0.01"), outcome_60 - Decimal("0.01")
                    ),
                    FrozenHorizonOutcomeV1(
                        240, outcome_240, outcome_240 + Decimal("0.01"), Decimal("-0.01")
                    ),
                ),
            )
        )
    return tuple(observations)


def test_report_engine_exact_matrix_quantiles_baselines_progress_and_digest(monkeypatch):
    research = _report_observations(100)
    validation = _report_observations(100, validation_shift=Decimal(10_000))
    config = precursor_report.discovery_execution_config_v1("FEATURE_RESEARCH", "VALIDATION")
    assert precursor_report.PRECURSOR_DISCOVERY_REPORT_IDENTITY == (
        "chappie_precursor_discovery_report/1.0.0"
    )
    assert len(config.event_definitions) == 18
    assert {event.horizon_minutes for event in config.event_definitions} == {15, 60, 240}
    calls = Counter()
    original_fit = precursor_report.fit_feature_research_quantiles

    def counted_fit(feature_name, values):
        calls[feature_name] += 1
        return original_fit(feature_name, values)

    monkeypatch.setattr(precursor_report, "fit_feature_research_quantiles", counted_fit)
    progress = []
    first = precursor_report.execute_precursor_discovery_v1(
        research,
        validation,
        config,
        _report_provenance(),
        created_at_utc="2026-01-01T00:00:00+00:00",
        progress_callback=progress.append,
    )
    assert first.identity == precursor_report.PRECURSOR_DISCOVERY_REPORT_IDENTITY
    assert len(first.catalogue_condition_identities) == 8
    assert len(first.assessments) == 144
    assert len(first.frozen_quantiles) == len(calls) == 11
    assert set(calls.values()) == {1}
    assert all(item.sample_count == len(research) for item in first.frozen_quantiles)
    assert max(item.p90 for item in first.frozen_quantiles) < Decimal(10_000)
    assert len(first.baselines) == 18
    baseline_by_event = {item.event_definition: item for item in first.baselines}
    for assessment in first.assessments:
        authority = baseline_by_event[assessment.event_definition]
        assert (
            assessment.paired_evidence.feature_research.baseline_result
            == authority.research_baseline
        )
        assert (
            assessment.paired_evidence.validation.baseline_result == authority.validation_baseline
        )
    assert sum(count for _, count in first.classification_counts) == 144
    assert len(first.rankings.upside_precursors) <= 5
    assert len(first.rankings.downside_precursors) <= 5
    assert len(first.rankings.volatility_precursors) <= 5
    assert first.no_validated_precursor_found_v1
    stages = tuple(dict.fromkeys(item.stage for item in progress))
    assert stages == tuple(precursor_report.DiscoveryProgressStageV1)
    assessment_progress = [
        item
        for item in progress
        if item.stage is precursor_report.DiscoveryProgressStageV1.EVALUATE_ASSESSMENTS
    ]
    assert (assessment_progress[-1].current, assessment_progress[-1].total) == (144, 144)
    second = precursor_report.execute_precursor_discovery_v1(
        research,
        validation,
        config,
        _report_provenance(),
        created_at_utc="2027-01-01T00:00:00+00:00",
    )
    assert first.frozen_quantiles == second.frozen_quantiles
    assert first.baselines == second.baselines
    assert first.assessments == second.assessments
    assert first.rankings == second.rankings
    assert first.semantic_digest == second.semantic_digest
    assert first.created_at_utc != second.created_at_utc
    assert first.provenance == _report_provenance()


def test_report_event_projection_uses_each_horizon_outcome_only():
    report = precursor_report.execute_precursor_discovery_v1(
        _report_observations(10),
        _report_observations(10),
        precursor_report.discovery_execution_config_v1("research", "validation"),
        _report_provenance(),
        created_at_utc="2026-01-01T00:00:00+00:00",
    )
    baselines = {
        (item.event_definition.horizon_minutes, item.event_definition.direction): item
        for item in report.baselines
        if item.event_definition.threshold_bps == 40
    }
    assert baselines[(15, EventDirectionV1.UP)].research_baseline.probability == Decimal(1)
    assert baselines[(60, EventDirectionV1.DOWN)].research_baseline.probability == Decimal(1)
    assert baselines[(240, EventDirectionV1.ABS)].research_baseline.probability == Decimal(0)


def test_report_atomic_writer_is_create_only_and_failed_run_does_not_publish(tmp_path, monkeypatch):
    report = precursor_report.execute_precursor_discovery_v1(
        _report_observations(10),
        _report_observations(10),
        precursor_report.discovery_execution_config_v1("research", "validation"),
        _report_provenance(),
        created_at_utc="2026-01-01T00:00:00+00:00",
    )
    destination = tmp_path / "report.json"
    assert precursor_report.write_precursor_discovery_report(report, destination) == destination
    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["semantic_digest"] == report.semantic_digest
    with pytest.raises(FileExistsError):
        precursor_report.write_precursor_discovery_report(report, destination)
    publication_failure = tmp_path / "publication-failure.json"

    def fail_link(_source, _destination):
        raise OSError("injected publication failure")

    monkeypatch.setattr(precursor_report.os, "link", fail_link)
    with pytest.raises(OSError, match="injected publication failure"):
        precursor_report.write_precursor_discovery_report(report, publication_failure)
    assert not publication_failure.exists()
    assert tuple(path.name for path in tmp_path.iterdir()) == ("report.json",)
    failed_destination = tmp_path / "failed.json"
    with pytest.raises(ValueError, match="VALIDATION observations are required"):
        precursor_report.execute_precursor_discovery_v1(
            _report_observations(10),
            (),
            precursor_report.discovery_execution_config_v1("research", "validation"),
            _report_provenance(),
        )
    assert not failed_destination.exists()


def test_report_engine_non_null_path_populates_all_three_rankings():
    observations = _report_observations(2500, correlated_outcomes=True)
    report = precursor_report.execute_precursor_discovery_v1(
        observations,
        observations,
        precursor_report.discovery_execution_config_v1("research", "validation"),
        _report_provenance(),
        created_at_utc="2026-01-01T00:00:00+00:00",
    )
    assert not report.no_validated_precursor_found_v1
    assert report.rankings.upside_precursors
    assert report.rankings.downside_precursors
    assert report.rankings.volatility_precursors
    assert all(
        len(ranking) <= 5
        for ranking in (
            report.rankings.upside_precursors,
            report.rankings.downside_precursors,
            report.rankings.volatility_precursors,
        )
    )
