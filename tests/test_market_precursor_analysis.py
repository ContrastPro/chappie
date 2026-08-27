from dataclasses import FrozenInstanceError
from decimal import Decimal
from inspect import signature

import pytest

from app.research.market_precursor import analysis
from app.research.market_precursor.analysis import (
    AllOfConditionV1,
    DiscoverySemanticRoleV1,
    EqualityFeatureConditionV1,
    EventDefinitionV1,
    EventDirectionV1,
    FrozenFeatureQuantilesV1,
    FutureLabelV1,
    QuantileBucketV1,
    QuantileFeatureConditionV1,
    SampleClassV1,
    baseline,
    bucket_for_role,
    classify_sample,
    conditional_effect,
    fit_feature_research_quantiles,
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
