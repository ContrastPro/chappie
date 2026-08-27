"""Pure, deterministic statistics for already-causal precursor rows."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from statistics import median


class EventDirectionV1(StrEnum):
    UP = "UP"
    DOWN = "DOWN"
    ABS = "ABS"


class SampleClassV1(StrEnum):
    INSUFFICIENT_SAMPLE = "INSUFFICIENT_SAMPLE"
    WEAK_SAMPLE = "WEAK_SAMPLE"
    INFORMATIVE_SAMPLE = "INFORMATIVE_SAMPLE"


class QuantileBucketV1(StrEnum):
    BOTTOM_10 = "BOTTOM_10"
    BOTTOM_25 = "BOTTOM_25"
    MIDDLE_50 = "MIDDLE_50"
    TOP_25 = "TOP_25"
    TOP_10 = "TOP_10"


@dataclass(frozen=True, slots=True)
class FrozenFeatureQuantilesV1:
    feature_name: str
    p10: Decimal
    p25: Decimal
    p75: Decimal
    p90: Decimal
    sample_count: int


def _percentile(values: tuple[Decimal, ...], numerator: int) -> Decimal:
    # Linear interpolation on the inclusive [0, n - 1] rank interval.
    rank = Decimal(numerator) * Decimal(len(values) - 1) / Decimal(100)
    lower = int(rank)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (rank - lower)


def fit_feature_research_quantiles(
    feature_name: str, feature_research_values: Iterable[Decimal]
) -> FrozenFeatureQuantilesV1:
    if not feature_name:
        raise ValueError("feature name is required")
    values = tuple(feature_research_values)
    if not values or any(
        not isinstance(value, Decimal) or not value.is_finite() for value in values
    ):
        raise ValueError("feature research values are invalid")
    ordered = tuple(sorted(values))
    return FrozenFeatureQuantilesV1(
        feature_name,
        _percentile(ordered, 10),
        _percentile(ordered, 25),
        _percentile(ordered, 75),
        _percentile(ordered, 90),
        len(ordered),
    )


@dataclass(frozen=True, slots=True)
class FutureLabelV1:
    timestamp_ms: int
    future_return: Decimal
    favorable_excursion: Decimal
    adverse_excursion: Decimal


@dataclass(frozen=True, slots=True)
class EventDefinitionV1:
    horizon_minutes: int
    threshold_bps: int
    direction: EventDirectionV1

    def __post_init__(self) -> None:
        if self.horizon_minutes not in (5, 15, 60, 240) or self.threshold_bps <= 0:
            raise ValueError("event definition is invalid")

    @property
    def threshold(self) -> Decimal:
        return Decimal(self.threshold_bps) / Decimal(10_000)

    def matches(self, label: FutureLabelV1) -> bool:
        if self.direction is EventDirectionV1.UP:
            return label.future_return >= self.threshold
        if self.direction is EventDirectionV1.DOWN:
            return label.future_return <= -self.threshold
        return abs(label.future_return) >= self.threshold


@dataclass(frozen=True, slots=True)
class EventBaselineV1:
    eligible_count: int
    event_count: int
    probability: Decimal


@dataclass(frozen=True, slots=True)
class ConditionalEffectV1:
    occurrence_count: int
    event_count: int
    baseline_probability: Decimal
    conditional_probability: Decimal | None
    absolute_lift: Decimal | None
    relative_lift: Decimal | None
    median_future_return: Decimal | None
    median_favorable_excursion: Decimal | None
    median_adverse_excursion: Decimal | None
    sample_class: SampleClassV1


def classify_sample(count: int) -> SampleClassV1:
    if count < 0:
        raise ValueError("sample count is invalid")
    if count < 100:
        return SampleClassV1.INSUFFICIENT_SAMPLE
    if count < 250:
        return SampleClassV1.WEAK_SAMPLE
    return SampleClassV1.INFORMATIVE_SAMPLE


def baseline(labels: Iterable[FutureLabelV1], event: EventDefinitionV1) -> EventBaselineV1:
    items = tuple(labels)
    if not items:
        raise ValueError("baseline requires labels")
    count = sum(event.matches(item) for item in items)
    return EventBaselineV1(len(items), count, Decimal(count) / Decimal(len(items)))


def conditional_effect(
    labels: Iterable[FutureLabelV1],
    event: EventDefinitionV1,
    predicate: Callable[[FutureLabelV1], bool],
) -> ConditionalEffectV1:
    items = tuple(labels)
    base = baseline(items, event)
    selected = tuple(item for item in items if predicate(item))
    if not selected:
        return ConditionalEffectV1(
            0, 0, base.probability, None, None, None, None, None, None, classify_sample(0)
        )
    event_count = sum(event.matches(item) for item in selected)
    probability = Decimal(event_count) / Decimal(len(selected))
    return ConditionalEffectV1(
        len(selected),
        event_count,
        base.probability,
        probability,
        probability - base.probability,
        None if base.probability == 0 else probability / base.probability,
        median(item.future_return for item in selected),
        median(item.favorable_excursion for item in selected),
        median(item.adverse_excursion for item in selected),
        classify_sample(len(selected)),
    )
