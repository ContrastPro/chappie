from decimal import Decimal

import pytest

from app.research.market_precursor.analysis import (
    EventDefinitionV1,
    EventDirectionV1,
    FutureLabelV1,
    SampleClassV1,
    baseline,
    classify_sample,
    conditional_effect,
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
