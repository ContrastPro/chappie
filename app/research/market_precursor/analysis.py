"""Pure, deterministic statistics for already-causal precursor rows."""

from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from statistics import median
from types import MappingProxyType


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


class DiscoveryConditionIdentityV1(StrEnum):
    JOINT_POSITIVE_RELATIVE_MOMENTUM = "JOINT_POSITIVE_RELATIVE_MOMENTUM"
    ETH_MOMENTUM_WITH_PARTICIPATION = "ETH_MOMENTUM_WITH_PARTICIPATION"
    SOL_MOMENTUM_WITH_PARTICIPATION = "SOL_MOMENTUM_WITH_PARTICIPATION"
    JOINT_CONTEXT_LEAD_WITH_BTC_COMPRESSION = "JOINT_CONTEXT_LEAD_WITH_BTC_COMPRESSION"
    DIRECTION_AGREEMENT_WITH_BREAKOUT_PROXIMITY = "DIRECTION_AGREEMENT_WITH_BREAKOUT_PROXIMITY"
    CONTEXT_ACCELERATION_WITH_MUTED_BTC = "CONTEXT_ACCELERATION_WITH_MUTED_BTC"
    CONTEXT_VOLATILITY_WITH_BTC_COMPRESSION = "CONTEXT_VOLATILITY_WITH_BTC_COMPRESSION"
    JOINT_NEGATIVE_RELATIVE_MOMENTUM = "JOINT_NEGATIVE_RELATIVE_MOMENTUM"


DISCOVERY_CONDITION_CATALOGUE_IDENTITY = "chappie_precursor_condition_catalogue/1.0.0"


@dataclass(frozen=True, slots=True)
class QuantileConditionSpecV1:
    feature_name: str
    bucket: QuantileBucketV1


@dataclass(frozen=True, slots=True)
class EqualityConditionSpecV1:
    feature_name: str
    expected_value: object


@dataclass(frozen=True, slots=True)
class DiscoveryConditionTemplateV1:
    identity: DiscoveryConditionIdentityV1
    children: tuple[QuantileConditionSpecV1 | EqualityConditionSpecV1, ...]

    def __post_init__(self) -> None:
        children = tuple(self.children)
        if not children or any(
            not isinstance(child, (QuantileConditionSpecV1, EqualityConditionSpecV1))
            for child in children
        ):
            raise ValueError("discovery condition template children are invalid")
        object.__setattr__(self, "children", children)


@dataclass(frozen=True, slots=True)
class DiscoveryConditionCatalogueV1:
    identity: str
    entries: tuple[DiscoveryConditionTemplateV1, ...]

    def __post_init__(self) -> None:
        entries = tuple(self.entries)
        if self.identity != DISCOVERY_CONDITION_CATALOGUE_IDENTITY:
            raise ValueError("discovery condition catalogue identity is invalid")
        if tuple(entry.identity for entry in entries) != tuple(DiscoveryConditionIdentityV1):
            raise ValueError("discovery condition catalogue entries are invalid")
        object.__setattr__(self, "entries", entries)


def discovery_condition_catalogue_v1() -> DiscoveryConditionCatalogueV1:
    quantile = QuantileConditionSpecV1
    equality = EqualityConditionSpecV1
    return DiscoveryConditionCatalogueV1(
        DISCOVERY_CONDITION_CATALOGUE_IDENTITY,
        (
            DiscoveryConditionTemplateV1(
                DiscoveryConditionIdentityV1.JOINT_POSITIVE_RELATIVE_MOMENTUM,
                (
                    quantile("cross_market.eth_minus_btc_return_15m", QuantileBucketV1.TOP_10),
                    quantile("cross_market.sol_minus_btc_return_15m", QuantileBucketV1.TOP_10),
                ),
            ),
            DiscoveryConditionTemplateV1(
                DiscoveryConditionIdentityV1.ETH_MOMENTUM_WITH_PARTICIPATION,
                (
                    quantile("cross_market.eth_minus_btc_return_15m", QuantileBucketV1.TOP_10),
                    quantile("eth.volume_ratio_60m", QuantileBucketV1.TOP_25),
                ),
            ),
            DiscoveryConditionTemplateV1(
                DiscoveryConditionIdentityV1.SOL_MOMENTUM_WITH_PARTICIPATION,
                (
                    quantile("cross_market.sol_minus_btc_return_15m", QuantileBucketV1.TOP_10),
                    quantile("sol.volume_ratio_60m", QuantileBucketV1.TOP_25),
                ),
            ),
            DiscoveryConditionTemplateV1(
                DiscoveryConditionIdentityV1.JOINT_CONTEXT_LEAD_WITH_BTC_COMPRESSION,
                (
                    quantile("cross_market.eth_minus_btc_return_15m", QuantileBucketV1.TOP_25),
                    quantile("cross_market.sol_minus_btc_return_15m", QuantileBucketV1.TOP_25),
                    quantile("volatility_compression_ratio", QuantileBucketV1.BOTTOM_25),
                ),
            ),
            DiscoveryConditionTemplateV1(
                DiscoveryConditionIdentityV1.DIRECTION_AGREEMENT_WITH_BREAKOUT_PROXIMITY,
                (
                    equality("cross_market.all_three_direction_agreement", True),
                    quantile("breakout_proximity", QuantileBucketV1.BOTTOM_25),
                ),
            ),
            DiscoveryConditionTemplateV1(
                DiscoveryConditionIdentityV1.CONTEXT_ACCELERATION_WITH_MUTED_BTC,
                (
                    quantile("eth.acceleration_5m_vs_15m", QuantileBucketV1.TOP_25),
                    quantile("sol.acceleration_5m_vs_15m", QuantileBucketV1.TOP_25),
                    quantile("return_15m", QuantileBucketV1.MIDDLE_50),
                ),
            ),
            DiscoveryConditionTemplateV1(
                DiscoveryConditionIdentityV1.CONTEXT_VOLATILITY_WITH_BTC_COMPRESSION,
                (
                    quantile("eth.atr_14_bps", QuantileBucketV1.TOP_25),
                    quantile("sol.atr_14_bps", QuantileBucketV1.TOP_25),
                    quantile("volatility_compression_ratio", QuantileBucketV1.BOTTOM_25),
                ),
            ),
            DiscoveryConditionTemplateV1(
                DiscoveryConditionIdentityV1.JOINT_NEGATIVE_RELATIVE_MOMENTUM,
                (
                    quantile("cross_market.eth_minus_btc_return_15m", QuantileBucketV1.BOTTOM_10),
                    quantile("cross_market.sol_minus_btc_return_15m", QuantileBucketV1.BOTTOM_10),
                ),
            ),
        ),
    )


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


def bind_discovery_condition_template(
    template: DiscoveryConditionTemplateV1,
    frozen_quantiles_by_feature: Mapping[str, FrozenFeatureQuantilesV1],
) -> AllOfConditionV1:
    children = []
    for spec in template.children:
        if isinstance(spec, QuantileConditionSpecV1):
            if spec.feature_name not in frozen_quantiles_by_feature:
                raise ValueError(f"required frozen quantiles are missing: {spec.feature_name}")
            children.append(
                QuantileFeatureConditionV1(
                    spec.feature_name,
                    spec.bucket,
                    frozen_quantiles_by_feature[spec.feature_name],
                )
            )
        else:
            children.append(EqualityFeatureConditionV1(spec.feature_name, spec.expected_value))
    return AllOfConditionV1(tuple(children))


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


@dataclass(frozen=True, slots=True)
class FrozenConditionObservationV1:
    timestamp_ms: int
    features: Mapping[str, object]
    future_return: Decimal
    favorable_excursion: Decimal
    adverse_excursion: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.timestamp_ms, int) or isinstance(self.timestamp_ms, bool):
            raise TypeError("observation timestamp is invalid")
        object.__setattr__(self, "features", MappingProxyType(dict(self.features)))
        if any(
            not isinstance(value, Decimal) or not value.is_finite()
            for value in (
                self.future_return,
                self.favorable_excursion,
                self.adverse_excursion,
            )
        ):
            raise ValueError("observation label values must be finite Decimals")


@dataclass(frozen=True, slots=True)
class FrozenConditionSplitEvidenceV1:
    split_name: str
    condition_identity: DiscoveryConditionIdentityV1
    frozen_condition: AllOfConditionV1
    event_definition: EventDefinitionV1
    baseline_result: EventBaselineV1
    conditional_effect_result: ConditionalEffectV1


@dataclass(frozen=True, slots=True)
class PairedFrozenConditionEvidenceV1:
    condition_identity: DiscoveryConditionIdentityV1
    frozen_condition: AllOfConditionV1
    event_definition: EventDefinitionV1
    feature_research: FrozenConditionSplitEvidenceV1
    validation: FrozenConditionSplitEvidenceV1

    def __post_init__(self) -> None:
        for evidence in (self.feature_research, self.validation):
            if (
                evidence.condition_identity is not self.condition_identity
                or evidence.frozen_condition is not self.frozen_condition
                or evidence.event_definition != self.event_definition
            ):
                raise ValueError("paired evidence identity is inconsistent")


def evaluate_frozen_condition_on_split(
    observations: Iterable[FrozenConditionObservationV1],
    condition: AllOfConditionV1,
    event_definition: EventDefinitionV1,
    split_name: str,
    condition_identity: DiscoveryConditionIdentityV1,
) -> FrozenConditionSplitEvidenceV1:
    items = tuple(observations)
    if not split_name:
        raise ValueError("split name is required")
    timestamps = tuple(item.timestamp_ms for item in items)
    if any(current <= previous for previous, current in pairwise(timestamps)):
        raise ValueError("observations must have strictly increasing timestamps")
    labels = tuple(
        FutureLabelV1(
            item.timestamp_ms,
            item.future_return,
            item.favorable_excursion,
            item.adverse_excursion,
        )
        for item in items
    )
    baseline_result = baseline(labels, event_definition)
    matches_by_timestamp = {item.timestamp_ms: condition.matches(item.features) for item in items}
    effect = conditional_effect(
        labels,
        event_definition,
        lambda label: matches_by_timestamp[label.timestamp_ms],
    )
    return FrozenConditionSplitEvidenceV1(
        split_name,
        condition_identity,
        condition,
        event_definition,
        baseline_result,
        effect,
    )


def evaluate_frozen_condition_pair(
    condition_identity: DiscoveryConditionIdentityV1,
    frozen_condition: AllOfConditionV1,
    event_definition: EventDefinitionV1,
    feature_research_observations: Iterable[FrozenConditionObservationV1],
    validation_observations: Iterable[FrozenConditionObservationV1],
) -> PairedFrozenConditionEvidenceV1:
    research = evaluate_frozen_condition_on_split(
        feature_research_observations,
        frozen_condition,
        event_definition,
        "FEATURE_RESEARCH",
        condition_identity,
    )
    validation = evaluate_frozen_condition_on_split(
        validation_observations,
        frozen_condition,
        event_definition,
        "VALIDATION",
        condition_identity,
    )
    return PairedFrozenConditionEvidenceV1(
        condition_identity,
        frozen_condition,
        event_definition,
        research,
        validation,
    )


@dataclass(frozen=True, slots=True)
class ConditionTemporalConcentrationV1:
    occurrence_count: int
    distinct_day_count: int
    largest_day_occurrence_count: int
    largest_day_share: Decimal | None
    distinct_week_count: int
    largest_week_occurrence_count: int
    largest_week_share: Decimal | None
    cluster_concentrated: bool


def calculate_condition_temporal_concentration(
    observations: Iterable[FrozenConditionObservationV1],
    condition: AllOfConditionV1,
) -> ConditionTemporalConcentrationV1:
    items = tuple(observations)
    timestamps = tuple(item.timestamp_ms for item in items)
    if any(current <= previous for previous, current in pairwise(timestamps)):
        raise ValueError("observations must have strictly increasing timestamps")
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    matched_datetimes = tuple(
        epoch + timedelta(milliseconds=item.timestamp_ms)
        for item in items
        if condition.matches(item.features)
    )
    occurrence_count = len(matched_datetimes)
    if not occurrence_count:
        return ConditionTemporalConcentrationV1(0, 0, 0, None, 0, 0, None, False)
    day_counts = Counter(item.date() for item in matched_datetimes)
    week_counts = Counter(
        (iso_year, iso_week)
        for item in matched_datetimes
        for iso_year, iso_week, _ in (item.isocalendar(),)
    )
    largest_day_count = max(day_counts.values())
    largest_week_count = max(week_counts.values())
    largest_day_share = Decimal(largest_day_count) / Decimal(occurrence_count)
    largest_week_share = Decimal(largest_week_count) / Decimal(occurrence_count)
    concentrated = occurrence_count >= 100 and (
        largest_day_share > Decimal("0.25") or largest_week_share > Decimal("0.60")
    )
    return ConditionTemporalConcentrationV1(
        occurrence_count,
        len(day_counts),
        largest_day_count,
        largest_day_share,
        len(week_counts),
        largest_week_count,
        largest_week_share,
        concentrated,
    )


@dataclass(frozen=True, slots=True)
class FrozenConditionStabilityEvidenceV1:
    research_sample_class: SampleClassV1
    validation_sample_class: SampleClassV1
    research_absolute_lift: Decimal | None
    validation_absolute_lift: Decimal | None
    lift_direction_agrees: bool
    validation_sample_adequate: bool
    research_cluster_concentrated: bool
    validation_cluster_concentrated: bool
    validation_lift_nonzero: bool
    validation_lift_retention_ratio: Decimal | None
    stable_evidence: bool


class PrecursorCandidateClassificationV1(StrEnum):
    NO_EFFECT = "NO_EFFECT"
    RESEARCH_ONLY = "RESEARCH_ONLY"
    VALIDATION_SUPPORTED = "VALIDATION_SUPPORTED"
    STRONG_VALIDATION_CANDIDATE = "STRONG_VALIDATION_CANDIDATE"


class PrecursorRankingCategoryV1(StrEnum):
    UPSIDE_PRECURSORS = "UPSIDE_PRECURSORS"
    DOWNSIDE_PRECURSORS = "DOWNSIDE_PRECURSORS"
    VOLATILITY_PRECURSORS = "VOLATILITY_PRECURSORS"


class CandidateAssessmentReasonV1(StrEnum):
    RESEARCH_LIFT_UNAVAILABLE = "RESEARCH_LIFT_UNAVAILABLE"
    RESEARCH_LIFT_ZERO = "RESEARCH_LIFT_ZERO"
    RESEARCH_SAMPLE_NOT_INFORMATIVE = "RESEARCH_SAMPLE_NOT_INFORMATIVE"
    VALIDATION_SAMPLE_NOT_INFORMATIVE = "VALIDATION_SAMPLE_NOT_INFORMATIVE"
    LIFT_DIRECTION_MISMATCH = "LIFT_DIRECTION_MISMATCH"
    VALIDATION_LIFT_ZERO = "VALIDATION_LIFT_ZERO"
    CLUSTER_CONCENTRATED = "CLUSTER_CONCENTRATED"
    STABLE_EVIDENCE = "STABLE_EVIDENCE"
    STRONG_LIFT_THRESHOLD_MET = "STRONG_LIFT_THRESHOLD_MET"
    RETENTION_THRESHOLD_MET = "RETENTION_THRESHOLD_MET"


NO_VALIDATED_PRECURSOR_FOUND_V1 = "NO_VALIDATED_PRECURSOR_FOUND_V1"
STRONG_VALIDATION_ABSOLUTE_LIFT_THRESHOLD_V1 = Decimal("0.02")
STRONG_VALIDATION_RETENTION_THRESHOLD_V1 = Decimal("0.50")


@dataclass(frozen=True, slots=True)
class PrecursorCandidateAssessmentV1:
    condition_identity: DiscoveryConditionIdentityV1
    event_definition: EventDefinitionV1
    classification: PrecursorCandidateClassificationV1
    paired_evidence: PairedFrozenConditionEvidenceV1
    stability_evidence: FrozenConditionStabilityEvidenceV1
    research_concentration: ConditionTemporalConcentrationV1
    validation_concentration: ConditionTemporalConcentrationV1
    ranking_category: PrecursorRankingCategoryV1
    reasons: tuple[CandidateAssessmentReasonV1, ...]


def _ranking_category(event: EventDefinitionV1) -> PrecursorRankingCategoryV1:
    if event.direction is EventDirectionV1.UP:
        return PrecursorRankingCategoryV1.UPSIDE_PRECURSORS
    if event.direction is EventDirectionV1.DOWN:
        return PrecursorRankingCategoryV1.DOWNSIDE_PRECURSORS
    return PrecursorRankingCategoryV1.VOLATILITY_PRECURSORS


def assess_precursor_candidate(
    paired_evidence: PairedFrozenConditionEvidenceV1,
    stability_evidence: FrozenConditionStabilityEvidenceV1,
    research_concentration: ConditionTemporalConcentrationV1,
    validation_concentration: ConditionTemporalConcentrationV1,
) -> PrecursorCandidateAssessmentV1:
    reasons = []
    research_lift = stability_evidence.research_absolute_lift
    validation_lift = stability_evidence.validation_absolute_lift
    if research_lift is None:
        reasons.append(CandidateAssessmentReasonV1.RESEARCH_LIFT_UNAVAILABLE)
    if research_lift == 0:
        reasons.append(CandidateAssessmentReasonV1.RESEARCH_LIFT_ZERO)
    if stability_evidence.research_sample_class is not SampleClassV1.INFORMATIVE_SAMPLE:
        reasons.append(CandidateAssessmentReasonV1.RESEARCH_SAMPLE_NOT_INFORMATIVE)
    if reasons:
        classification = PrecursorCandidateClassificationV1.NO_EFFECT
    elif not stability_evidence.stable_evidence:
        if not stability_evidence.validation_sample_adequate:
            reasons.append(CandidateAssessmentReasonV1.VALIDATION_SAMPLE_NOT_INFORMATIVE)
        if not stability_evidence.lift_direction_agrees:
            reasons.append(CandidateAssessmentReasonV1.LIFT_DIRECTION_MISMATCH)
        if not stability_evidence.validation_lift_nonzero:
            reasons.append(CandidateAssessmentReasonV1.VALIDATION_LIFT_ZERO)
        if (
            stability_evidence.research_cluster_concentrated
            or stability_evidence.validation_cluster_concentrated
        ):
            reasons.append(CandidateAssessmentReasonV1.CLUSTER_CONCENTRATED)
        classification = PrecursorCandidateClassificationV1.RESEARCH_ONLY
    else:
        reasons.append(CandidateAssessmentReasonV1.STABLE_EVIDENCE)
        strong_lift = (
            validation_lift is not None
            and abs(validation_lift) >= STRONG_VALIDATION_ABSOLUTE_LIFT_THRESHOLD_V1
        )
        strong_retention = (
            stability_evidence.validation_lift_retention_ratio is not None
            and stability_evidence.validation_lift_retention_ratio
            >= STRONG_VALIDATION_RETENTION_THRESHOLD_V1
        )
        if strong_lift:
            reasons.append(CandidateAssessmentReasonV1.STRONG_LIFT_THRESHOLD_MET)
        if strong_retention:
            reasons.append(CandidateAssessmentReasonV1.RETENTION_THRESHOLD_MET)
        classification = (
            PrecursorCandidateClassificationV1.STRONG_VALIDATION_CANDIDATE
            if strong_lift and strong_retention
            else PrecursorCandidateClassificationV1.VALIDATION_SUPPORTED
        )
    return PrecursorCandidateAssessmentV1(
        paired_evidence.condition_identity,
        paired_evidence.event_definition,
        classification,
        paired_evidence,
        stability_evidence,
        research_concentration,
        validation_concentration,
        _ranking_category(paired_evidence.event_definition),
        tuple(reasons),
    )


def rank_precursor_candidates(
    assessments: Iterable[PrecursorCandidateAssessmentV1],
    category: PrecursorRankingCategoryV1,
    limit: int = 5,
) -> tuple[PrecursorCandidateAssessmentV1, ...]:
    if not isinstance(category, PrecursorRankingCategoryV1):
        raise TypeError("ranking category is invalid")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ValueError("ranking limit is invalid")
    eligible = (
        assessment
        for assessment in assessments
        if assessment.ranking_category is category
        and assessment.classification
        in (
            PrecursorCandidateClassificationV1.VALIDATION_SUPPORTED,
            PrecursorCandidateClassificationV1.STRONG_VALIDATION_CANDIDATE,
        )
    )

    def optional_descending(value: Decimal | None) -> tuple[bool, Decimal]:
        return value is None, Decimal(0) if value is None else -value

    def optional_ascending(value: Decimal | None) -> tuple[bool, Decimal]:
        return value is None, Decimal(0) if value is None else value

    def key(assessment: PrecursorCandidateAssessmentV1):
        validation_effect = assessment.paired_evidence.validation.conditional_effect_result
        stability = assessment.stability_evidence
        return (
            0
            if assessment.classification
            is PrecursorCandidateClassificationV1.STRONG_VALIDATION_CANDIDATE
            else 1,
            -abs(stability.validation_absolute_lift or Decimal(0)),
            optional_descending(validation_effect.relative_lift),
            -validation_effect.occurrence_count,
            optional_descending(stability.validation_lift_retention_ratio),
            optional_ascending(assessment.validation_concentration.largest_day_share),
            optional_ascending(assessment.validation_concentration.largest_week_share),
            assessment.condition_identity.value,
            assessment.event_definition.direction.value,
            assessment.event_definition.horizon_minutes,
            assessment.event_definition.threshold_bps,
        )

    return tuple(sorted(eligible, key=key)[:limit])


def no_validated_precursor_found(
    assessments: Iterable[PrecursorCandidateAssessmentV1],
) -> bool:
    return not any(
        assessment.classification
        in (
            PrecursorCandidateClassificationV1.VALIDATION_SUPPORTED,
            PrecursorCandidateClassificationV1.STRONG_VALIDATION_CANDIDATE,
        )
        for assessment in assessments
    )


def evaluate_frozen_condition_stability(
    paired_evidence: PairedFrozenConditionEvidenceV1,
    research_concentration: ConditionTemporalConcentrationV1,
    validation_concentration: ConditionTemporalConcentrationV1,
) -> FrozenConditionStabilityEvidenceV1:
    research_effect = paired_evidence.feature_research.conditional_effect_result
    validation_effect = paired_evidence.validation.conditional_effect_result
    if research_concentration.occurrence_count != research_effect.occurrence_count:
        raise ValueError("research concentration occurrence count is inconsistent")
    if validation_concentration.occurrence_count != validation_effect.occurrence_count:
        raise ValueError("validation concentration occurrence count is inconsistent")
    research_lift = research_effect.absolute_lift
    validation_lift = validation_effect.absolute_lift
    direction_agrees = (
        research_lift is not None
        and validation_lift is not None
        and (
            (research_lift > 0 and validation_lift > 0)
            or (research_lift < 0 and validation_lift < 0)
        )
    )
    validation_lift_nonzero = validation_lift is not None and validation_lift != 0
    retention = (
        abs(validation_lift) / abs(research_lift)
        if direction_agrees and research_lift not in (None, Decimal(0))
        else None
    )
    validation_adequate = validation_effect.sample_class is SampleClassV1.INFORMATIVE_SAMPLE
    stable = (
        research_effect.sample_class is SampleClassV1.INFORMATIVE_SAMPLE
        and validation_adequate
        and direction_agrees
        and validation_lift_nonzero
        and not research_concentration.cluster_concentrated
        and not validation_concentration.cluster_concentrated
    )
    return FrozenConditionStabilityEvidenceV1(
        research_effect.sample_class,
        validation_effect.sample_class,
        research_lift,
        validation_lift,
        direction_agrees,
        validation_adequate,
        research_concentration.cluster_concentrated,
        validation_concentration.cluster_concentrated,
        validation_lift_nonzero,
        retention,
        stable,
    )
