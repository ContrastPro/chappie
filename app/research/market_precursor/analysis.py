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
