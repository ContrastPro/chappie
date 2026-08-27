"""Pure, deterministic statistics for already-causal precursor rows."""

from collections.abc import Callable, Iterable, Mapping
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


class DiscoverySemanticRoleV1(StrEnum):
    STRONG_HIGH = "STRONG_HIGH"
    HIGH = "HIGH"
    ELEVATED = "ELEVATED"
    HIGH_PARTICIPATION = "HIGH_PARTICIPATION"
    STRONG_LOW = "STRONG_LOW"
    LOW = "LOW"
    COMPRESSED = "COMPRESSED"
    MUTED = "MUTED"


def bucket_for_role(role: DiscoverySemanticRoleV1) -> QuantileBucketV1:
    if not isinstance(role, DiscoverySemanticRoleV1):
        raise TypeError("unknown discovery semantic role")
    if role is DiscoverySemanticRoleV1.STRONG_HIGH:
        return QuantileBucketV1.TOP_10
    if role in (
        DiscoverySemanticRoleV1.HIGH,
        DiscoverySemanticRoleV1.ELEVATED,
        DiscoverySemanticRoleV1.HIGH_PARTICIPATION,
    ):
        return QuantileBucketV1.TOP_25
    if role is DiscoverySemanticRoleV1.STRONG_LOW:
        return QuantileBucketV1.BOTTOM_10
    if role in (DiscoverySemanticRoleV1.LOW, DiscoverySemanticRoleV1.COMPRESSED):
        return QuantileBucketV1.BOTTOM_25
    if role is DiscoverySemanticRoleV1.MUTED:
        return QuantileBucketV1.MIDDLE_50
    raise ValueError("unknown discovery semantic role")


@dataclass(frozen=True, slots=True)
class FrozenFeatureQuantilesV1:
    feature_name: str
    p10: Decimal
    p25: Decimal
    p75: Decimal
    p90: Decimal
    sample_count: int


@dataclass(frozen=True, slots=True)
class QuantileFeatureConditionV1:
    """A feature-bound condition using pre-fitted, immutable Decimal thresholds."""

    feature_name: str
    bucket: QuantileBucketV1
    frozen_quantiles: FrozenFeatureQuantilesV1

    def __post_init__(self) -> None:
        if self.feature_name != self.frozen_quantiles.feature_name:
            raise ValueError("condition feature does not match frozen quantiles")

    def matches(self, feature_name: str, value: Decimal) -> bool:
        if feature_name != self.feature_name:
            raise ValueError("evaluation feature does not match condition feature")
        if not isinstance(value, Decimal) or not value.is_finite():
            raise ValueError("condition value must be a finite Decimal")
        if self.bucket is QuantileBucketV1.BOTTOM_10:
            return value <= self.frozen_quantiles.p10
        if self.bucket is QuantileBucketV1.BOTTOM_25:
            return value <= self.frozen_quantiles.p25
        if self.bucket is QuantileBucketV1.MIDDLE_50:
            return self.frozen_quantiles.p25 <= value <= self.frozen_quantiles.p75
        if self.bucket is QuantileBucketV1.TOP_25:
            return value >= self.frozen_quantiles.p75
        if self.bucket is QuantileBucketV1.TOP_10:
            return value >= self.frozen_quantiles.p90
        return False


@dataclass(frozen=True, slots=True)
class EqualityFeatureConditionV1:
    """A feature-bound condition using exact value-and-type equality."""

    feature_name: str
    expected_value: object

    def matches(self, feature_name: str, value: object) -> bool:
        if feature_name != self.feature_name:
            raise ValueError("evaluation feature does not match condition feature")
        return type(value) is type(self.expected_value) and value == self.expected_value


@dataclass(frozen=True, slots=True)
class AllOfConditionV1:
    """An ordered conjunction with unique, explicitly bound child features."""

    children: tuple[QuantileFeatureConditionV1 | EqualityFeatureConditionV1, ...]

    def __post_init__(self) -> None:
        children = tuple(self.children)
        if not children:
            raise ValueError("all-of condition requires at least one child")
        if any(
            not isinstance(child, (QuantileFeatureConditionV1, EqualityFeatureConditionV1))
            for child in children
        ):
            raise ValueError("all-of condition contains an unsupported child")
        feature_names = tuple(child.feature_name for child in children)
        if len(set(feature_names)) != len(feature_names):
            raise ValueError("all-of condition requires unique child features")
        object.__setattr__(self, "children", children)

    def matches(self, features: Mapping[str, object]) -> bool:
        results = []
        for child in self.children:
            if child.feature_name not in features:
                raise ValueError(f"required feature is missing: {child.feature_name}")
            results.append(child.matches(child.feature_name, features[child.feature_name]))
        return all(results)


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
