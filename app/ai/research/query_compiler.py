"""Deterministic compiler from model-facing research intent to canonical RDE queries.

This module is research-only.  It defines neither SQL nor an RDE executor and
never interprets benchmark case IDs or model prose as query syntax.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from app.ai.research.reasoning import (
    RELATIONSHIPS,
    TARGET_HORIZONS,
    TARGET_METRICS,
    TestableHypothesisV11,
)
from app.ai.schemas import FactV1, digest

COMPILER_IDENTITY = "research_query_compiler/1.0.0"
COMPARISON_POLICY = "same_asset_matched_control/1.0.0"
AVAILABILITY_POLICY = "unavailable_facts_are_not_predictive_predicates/1.0.0"
METRIC_POLICY = "intent_metric_is_explicit/1.0.0"


class CompilationStatus(StrEnum):
    COMPILED = "COMPILED"
    AMBIGUOUS_INTENT = "AMBIGUOUS_INTENT"
    UNSUPPORTED_PREDICATE = "UNSUPPORTED_PREDICATE"
    UNKNOWN_FACT = "UNKNOWN_FACT"
    INVALID_TARGET = "INVALID_TARGET"
    INVALID_HORIZON = "INVALID_HORIZON"
    INVALID_METRIC = "INVALID_METRIC"
    INVALID_INTENT = "INVALID_INTENT"


class ExpectedDirectionV1(StrEnum):
    HIGHER = "HIGHER"
    LOWER = "LOWER"
    DIFFERENT = "DIFFERENT"
    UNCERTAIN = "UNCERTAIN"


class CompatibilityStatusV1(StrEnum):
    COMPATIBLE = "COMPATIBLE"
    PARTIAL = "PARTIAL"
    NOT_COMPATIBLE = "NOT_COMPATIBLE"


@dataclass(frozen=True, slots=True)
class FieldRegistryEntryV1:
    canonical_field: str
    rde_source_field: str | None
    allowed_units: tuple[str | None, ...]
    supports_predictive_predicate: bool
    availability_predicate_supported: bool = False


FIELD_REGISTRY_V1 = {
    "RETURN": FieldRegistryEntryV1("RETURN", "recent_return_bps", ("BPS", "PERCENT"), True),
    "OI_STATE": FieldRegistryEntryV1("OI_STATE", "oi_change_5m", (None,), True),
    "FUNDING_STATE": FieldRegistryEntryV1("FUNDING_STATE", "funding", (None,), True),
    "POSITIVE_BREADTH": FieldRegistryEntryV1("POSITIVE_BREADTH", None, ("RATIO", None), False),
    "RELATIVE_STRENGTH": FieldRegistryEntryV1("RELATIVE_STRENGTH", None, ("BPS", None), False),
    "TAKER_PRESSURE": FieldRegistryEntryV1("TAKER_PRESSURE", "futures_taker_state", (None,), True),
    "EVENT_TYPE": FieldRegistryEntryV1("EVENT_TYPE", "event_type", (None,), True),
    "EVENT_PRESENT": FieldRegistryEntryV1("EVENT_PRESENT", "event_present", (None,), True),
}


def compiler_digest() -> str:
    return digest(
        {
            "identity": COMPILER_IDENTITY,
            "field_registry": {
                key: {
                    "rde_source_field": item.rde_source_field,
                    "allowed_units": list(item.allowed_units),
                    "supports_predictive_predicate": item.supports_predictive_predicate,
                    "availability_predicate_supported": item.availability_predicate_supported,
                }
                for key, item in sorted(FIELD_REGISTRY_V1.items())
            },
            "comparison_policy": COMPARISON_POLICY,
            "availability_policy": AVAILABILITY_POLICY,
            "metric_policy": METRIC_POLICY,
        }
    )


@dataclass(frozen=True, slots=True)
class AIResearchIntentV1:
    intent_id: str
    target_asset: str
    target_horizon: str
    target_metric: str | None
    research_question: str
    expected_direction: ExpectedDirectionV1
    supporting_fact_ids: tuple[str, ...]
    unknowns: tuple[str, ...]
    status: str = "UNVALIDATED"

    def __post_init__(self) -> None:
        if not self.intent_id or not self.research_question or not self.supporting_fact_ids:
            raise ValueError("intent identity, question, and supporting facts are required")
        if self.status != "UNVALIDATED":
            raise ValueError("research intent must remain UNVALIDATED")

    def payload(self) -> dict[str, object]:
        return {
            "intent_id": self.intent_id,
            "target_asset": self.target_asset,
            "target_horizon": self.target_horizon,
            "target_metric": self.target_metric,
            "expected_direction": self.expected_direction.value,
            "supporting_fact_ids": list(self.supporting_fact_ids),
            "unknowns": list(self.unknowns),
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class CanonicalResearchPredicateV1:
    asset: str
    canonical_field: str
    operator: str
    typed_value: str | int | float | bool | None
    unit: str | None
    time_semantics: str
    availability_semantics: str
    source_fact_id: str

    def payload(self) -> dict[str, object]:
        return {
            "asset": self.asset,
            "canonical_field": self.canonical_field,
            "operator": self.operator,
            "typed_value": self.typed_value,
            "unit": self.unit,
            "time_semantics": self.time_semantics,
            "availability_semantics": self.availability_semantics,
            "source_fact_id": self.source_fact_id,
        }


@dataclass(frozen=True, slots=True)
class CanonicalRDEQueryV1:
    predicates: tuple[CanonicalResearchPredicateV1, ...]
    target_asset: str
    target_metric: str
    target_horizon: str
    comparison_basis: str
    expected_relationship: str
    source_intent_id: str
    source_fact_ids: tuple[str, ...]
    compiler_identity: str
    compiler_digest: str

    @property
    def query_digest(self) -> str:
        return digest(
            {
                "compiler_identity": self.compiler_identity,
                "compiler_digest": self.compiler_digest,
                "predicates": [item.payload() for item in self.predicates],
                "target_asset": self.target_asset,
                "target_metric": self.target_metric,
                "target_horizon": self.target_horizon,
                "comparison_basis": self.comparison_basis,
                "expected_relationship": self.expected_relationship,
                "source_intent_id": self.source_intent_id,
                "source_fact_ids": list(self.source_fact_ids),
            }
        )


@dataclass(frozen=True, slots=True)
class QueryCompilationResultV1:
    status: CompilationStatus
    query: CanonicalRDEQueryV1 | None = None
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PredicateCompilationResultV1:
    status: CompilationStatus
    predicate: CanonicalResearchPredicateV1 | None = None
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ResearchEvidenceV1:
    query_digest: str
    matched_event_count: int
    matched_control_count: int
    target_metric: str
    horizon: str
    event_distribution: tuple[float, ...]
    control_distribution: tuple[float, ...]
    incremental_effect: float | None
    year_slices: tuple[tuple[int, float | None], ...]
    placebo_status: str | None
    evidence_status: str
    executor_identity: str | None = None
    executor_digest: str | None = None
    methodology_identity: str | None = None
    methodology_digest: str | None = None
    candidate_rows: int = 0
    collapsed_events: int = 0
    independent_events: int = 0
    outcome_available_events: int = 0
    unmatched_count: int = 0
    reason_codes: tuple[str, ...] = ()


class RDEQueryExecutor(Protocol):
    def execute(self, query: CanonicalRDEQueryV1) -> ResearchEvidenceV1: ...


@dataclass(frozen=True, slots=True)
class V11CompatibilityAssessmentV1:
    status: CompatibilityStatusV1
    intent: AIResearchIntentV1 | None
    reason_codes: tuple[str, ...]


def compile_fact_to_predicate(fact: FactV1) -> PredicateCompilationResultV1:
    if fact.asset is None or fact.canonical_field is None or fact.value_type is None:
        return PredicateCompilationResultV1(
            CompilationStatus.UNSUPPORTED_PREDICATE, reason_codes=("FACT_METADATA_REQUIRED",)
        )
    entry = FIELD_REGISTRY_V1.get(fact.canonical_field)
    if entry is None or not entry.supports_predictive_predicate:
        return PredicateCompilationResultV1(
            CompilationStatus.UNSUPPORTED_PREDICATE,
            reason_codes=("CANONICAL_FIELD_UNSUPPORTED",),
        )
    if fact.availability != "AVAILABLE" or fact.value is None:
        return PredicateCompilationResultV1(
            CompilationStatus.UNSUPPORTED_PREDICATE,
            reason_codes=("UNAVAILABLE_FACT_NOT_PREDICTIVE",),
        )
    if fact.unit not in entry.allowed_units:
        return PredicateCompilationResultV1(
            CompilationStatus.UNSUPPORTED_PREDICATE, reason_codes=("UNIT_UNSUPPORTED",)
        )
    return PredicateCompilationResultV1(
        CompilationStatus.COMPILED,
        predicate=CanonicalResearchPredicateV1(
            fact.asset,
            fact.canonical_field,
            "EQUALS",
            fact.value,
            fact.unit,
            "AS_OF_FACT_TIMESTAMP",
            "AVAILABLE_ONLY",
            fact.fact_id,
        ),
    )


def compile_research_intent(
    intent: AIResearchIntentV1, facts: tuple[FactV1, ...]
) -> QueryCompilationResultV1:
    if intent.target_asset not in {
        "BTCUSDT",
        "ETHUSDT",
        "SOLUSDT",
        "BNBUSDT",
        "AVAXUSDT",
        "XRPUSDT",
        "LINKUSDT",
        "DOGEUSDT",
    }:
        return QueryCompilationResultV1(CompilationStatus.INVALID_TARGET)
    if intent.target_horizon not in TARGET_HORIZONS:
        return QueryCompilationResultV1(CompilationStatus.INVALID_HORIZON)
    if intent.target_metric is None or intent.expected_direction == ExpectedDirectionV1.UNCERTAIN:
        return QueryCompilationResultV1(CompilationStatus.AMBIGUOUS_INTENT)
    if intent.target_metric not in TARGET_METRICS:
        return QueryCompilationResultV1(CompilationStatus.INVALID_METRIC)
    relationship = {
        ExpectedDirectionV1.HIGHER: "GREATER_THAN_MATCHED_CONTROL",
        ExpectedDirectionV1.LOWER: "LESS_THAN_MATCHED_CONTROL",
        ExpectedDirectionV1.DIFFERENT: "HIGHER_PROBABILITY_THAN_MATCHED_CONTROL",
    }.get(intent.expected_direction)
    if relationship not in RELATIONSHIPS:
        return QueryCompilationResultV1(CompilationStatus.AMBIGUOUS_INTENT)
    known = {fact.fact_id: fact for fact in facts}
    missing = tuple(fact_id for fact_id in intent.supporting_fact_ids if fact_id not in known)
    if missing:
        return QueryCompilationResultV1(CompilationStatus.UNKNOWN_FACT, reason_codes=missing)
    predicates: list[CanonicalResearchPredicateV1] = []
    for fact_id in intent.supporting_fact_ids:
        compiled = compile_fact_to_predicate(known[fact_id])
        if compiled.status != CompilationStatus.COMPILED or compiled.predicate is None:
            return QueryCompilationResultV1(compiled.status, reason_codes=compiled.reason_codes)
        predicates.append(compiled.predicate)
    query = CanonicalRDEQueryV1(
        predicates=tuple(predicates),
        target_asset=intent.target_asset,
        target_metric=intent.target_metric,
        target_horizon=intent.target_horizon,
        comparison_basis="MATCHED_CONTROL",
        expected_relationship=relationship,
        source_intent_id=intent.intent_id,
        source_fact_ids=intent.supporting_fact_ids,
        compiler_identity=COMPILER_IDENTITY,
        compiler_digest=compiler_digest(),
    )
    return QueryCompilationResultV1(CompilationStatus.COMPILED, query=query)


def assess_v11_compatibility(
    hypothesis: TestableHypothesisV11, facts: tuple[FactV1, ...]
) -> V11CompatibilityAssessmentV1:
    direction = {
        "GREATER_THAN_MATCHED_CONTROL": ExpectedDirectionV1.HIGHER,
        "LESS_THAN_MATCHED_CONTROL": ExpectedDirectionV1.LOWER,
        "HIGHER_PROBABILITY_THAN_MATCHED_CONTROL": ExpectedDirectionV1.DIFFERENT,
        "LOWER_PROBABILITY_THAN_MATCHED_CONTROL": ExpectedDirectionV1.DIFFERENT,
    }.get(hypothesis.expected_relationship)
    if direction is None:
        return V11CompatibilityAssessmentV1(
            CompatibilityStatusV1.NOT_COMPATIBLE, None, ("RELATIONSHIP_UNKNOWN",)
        )
    intent = AIResearchIntentV1(
        intent_id=f"compat-{hypothesis.candidate_id}",
        target_asset=hypothesis.target_asset,
        target_horizon=hypothesis.target_horizon,
        target_metric=hypothesis.target_metric,
        research_question="Derived read-only compatibility intent.",
        expected_direction=direction,
        supporting_fact_ids=hypothesis.supporting_fact_ids,
        unknowns=hypothesis.unknown_dependencies,
    )
    result = compile_research_intent(intent, facts)
    if result.status == CompilationStatus.COMPILED:
        return V11CompatibilityAssessmentV1(CompatibilityStatusV1.COMPATIBLE, intent, ())
    if result.status in {CompilationStatus.UNSUPPORTED_PREDICATE, CompilationStatus.UNKNOWN_FACT}:
        return V11CompatibilityAssessmentV1(
            CompatibilityStatusV1.PARTIAL, intent, result.reason_codes
        )
    return V11CompatibilityAssessmentV1(
        CompatibilityStatusV1.NOT_COMPATIBLE, intent, result.reason_codes
    )
