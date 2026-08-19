"""Research Reasoning Contract v1.1: testable, RDE-queryable hypotheses."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from app.ai.errors import AIValidationError
from app.ai.market_semantics import semantic_violation
from app.ai.schemas import FactV1, ReasonedStatementV1, digest, timestamp_ms

RESEARCH_REASONING_IDENTITY = "research_reasoning/1.1.0"
RESEARCH_REASONING_V111_IDENTITY = "research_reasoning/1.1.1"
TARGET_METRICS = frozenset(
    {
        "FORWARD_RETURN_BPS",
        "MFE_BPS",
        "MAE_BPS",
        "UPSIDE_60_TOUCH",
        "UPSIDE_100_TOUCH",
        "UPSIDE_150_TOUCH",
        "DOWNSIDE_60_TOUCH",
        "DOWNSIDE_100_TOUCH",
        "DOWNSIDE_150_TOUCH",
    }
)
TARGET_HORIZONS = frozenset({"1h", "3h", "6h", "12h", "24h"})
RELATIONSHIPS = frozenset(
    {
        "GREATER_THAN_MATCHED_CONTROL",
        "LESS_THAN_MATCHED_CONTROL",
        "HIGHER_PROBABILITY_THAN_MATCHED_CONTROL",
        "LOWER_PROBABILITY_THAN_MATCHED_CONTROL",
    }
)
COMPARISON_BASES = frozenset({"MATCHED_CONTROL"})
CONDITION_OPERATORS = frozenset({"IS", "EQUALS"})
ALLOWED_TARGET_ASSETS = frozenset(
    {"BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "AVAXUSDT", "XRPUSDT", "LINKUSDT", "DOGEUSDT"}
)


@dataclass(frozen=True, slots=True)
class ConditionClauseV1:
    fact_id: str
    operator: str
    value: str | int | float | bool | None

    def payload(self) -> dict[str, object]:
        return {"fact_id": self.fact_id, "operator": self.operator, "value": self.value}


@dataclass(frozen=True, slots=True)
class TestableHypothesisV11:
    condition: tuple[ConditionClauseV1, ...]
    target_asset: str
    target_metric: str
    target_horizon: str
    expected_relationship: str
    comparison_basis: str
    supporting_fact_ids: tuple[str, ...]
    unknown_dependencies: tuple[str, ...]
    falsification_condition: str
    status: str = "UNVALIDATED"

    def __post_init__(self) -> None:
        if self.target_asset not in ALLOWED_TARGET_ASSETS:
            raise AIValidationError(
                "hypothesis target asset is unsupported",
                code="HYPOTHESIS_TARGET_ASSET_INVALID",
                received=self.target_asset,
                expected=" | ".join(sorted(ALLOWED_TARGET_ASSETS)),
            )
        if self.target_metric not in TARGET_METRICS:
            raise AIValidationError(
                "hypothesis target metric is unsupported",
                code="HYPOTHESIS_TARGET_METRIC_INVALID",
                received=self.target_metric,
                expected=" | ".join(sorted(TARGET_METRICS)),
            )
        if self.target_horizon not in TARGET_HORIZONS:
            raise AIValidationError(
                "hypothesis target horizon is unsupported",
                code="HYPOTHESIS_TARGET_HORIZON_INVALID",
                received=self.target_horizon,
                expected=" | ".join(sorted(TARGET_HORIZONS)),
            )
        if self.expected_relationship not in RELATIONSHIPS:
            raise AIValidationError(
                "hypothesis relationship is unsupported",
                code="HYPOTHESIS_RELATIONSHIP_INVALID",
                received=self.expected_relationship,
                expected=" | ".join(sorted(RELATIONSHIPS)),
            )
        if self.comparison_basis not in COMPARISON_BASES:
            raise AIValidationError(
                "hypothesis comparison basis is unsupported",
                code="HYPOTHESIS_COMPARISON_BASIS_INVALID",
                received=self.comparison_basis,
                expected="MATCHED_CONTROL",
            )
        if self.status != "UNVALIDATED":
            raise AIValidationError(
                "research hypotheses must remain UNVALIDATED",
                code="HYPOTHESIS_STATUS_INVALID",
                received=self.status,
                expected="UNVALIDATED",
            )
        if not self.condition or not self.supporting_fact_ids:
            raise AIValidationError("hypothesis condition and supporting facts are required")
        if (
            not self.falsification_condition
            or self.falsification_condition == "if prediction is wrong"
        ):
            raise AIValidationError(
                "hypothesis requires a deterministic falsification condition",
                code="HYPOTHESIS_FALSIFICATION_INVALID",
                received=self.falsification_condition,
                expected="non-empty deterministic falsification condition",
            )

    @property
    def candidate_id(self) -> str:
        return f"hypothesis-{digest(self._identity_payload())[:24]}"

    def _identity_payload(self) -> dict[str, object]:
        """Canonical fields only; the derived ID must not hash itself."""
        return {
            "condition": [clause.payload() for clause in self.condition],
            "target_asset": self.target_asset,
            "target_metric": self.target_metric,
            "target_horizon": self.target_horizon,
            "expected_relationship": self.expected_relationship,
            "comparison_basis": self.comparison_basis,
            "supporting_fact_ids": list(self.supporting_fact_ids),
            "unknown_dependencies": list(self.unknown_dependencies),
            "falsification_condition": self.falsification_condition,
            "status": self.status,
        }

    def payload(self) -> dict[str, object]:
        return {**self._identity_payload(), "candidate_id": self.candidate_id}


@dataclass(frozen=True, slots=True)
class ResearchReasoningOutputV11:
    observed_facts: tuple[str, ...]
    interpretations: tuple[ReasonedStatementV1, ...]
    hypotheses: tuple[TestableHypothesisV11, ...]
    contradictions: tuple[ReasonedStatementV1, ...]
    unknowns: tuple[str, ...]
    evidence_requests: tuple[str, ...]
    confidence: float

    def payload(self) -> dict[str, object]:
        return {
            "observed_facts": [{"fact_id": item} for item in self.observed_facts],
            "interpretations": [
                {"statement": item.statement, "supporting_fact_ids": list(item.supporting_fact_ids)}
                for item in self.interpretations
            ],
            "hypotheses": [item.payload() for item in self.hypotheses],
            "contradictions": [
                {"statement": item.statement, "supporting_fact_ids": list(item.supporting_fact_ids)}
                for item in self.contradictions
            ],
            "unknowns": list(self.unknowns),
            "evidence_requests": list(self.evidence_requests),
            "confidence": self.confidence,
        }


_STRING_ARRAY_SCHEMA: dict[str, object] = {
    "type": "array",
    "items": {"type": "string", "minLength": 1},
}
_FACT_REFERENCE_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["fact_id"],
    "properties": {"fact_id": {"type": "string", "minLength": 1}},
}
_STATEMENT_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["statement", "supporting_fact_ids"],
    "properties": {
        "statement": {"type": "string", "minLength": 1},
        "supporting_fact_ids": _STRING_ARRAY_SCHEMA,
    },
}
_CONDITION_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["fact_id", "operator", "value"],
    "properties": {
        "fact_id": {"type": "string", "minLength": 1},
        "operator": {"enum": sorted(CONDITION_OPERATORS)},
        "value": {"type": ["string", "number", "boolean", "null"]},
    },
}
_HYPOTHESIS_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "condition",
        "target_asset",
        "target_metric",
        "target_horizon",
        "expected_relationship",
        "comparison_basis",
        "supporting_fact_ids",
        "unknown_dependencies",
        "falsification_condition",
        "status",
    ],
    "properties": {
        "condition": {"type": "array", "minItems": 1, "items": _CONDITION_SCHEMA},
        "target_asset": {"enum": sorted(ALLOWED_TARGET_ASSETS)},
        "target_metric": {"enum": sorted(TARGET_METRICS)},
        "target_horizon": {"enum": sorted(TARGET_HORIZONS)},
        "expected_relationship": {"enum": sorted(RELATIONSHIPS)},
        "comparison_basis": {"enum": sorted(COMPARISON_BASES)},
        "supporting_fact_ids": {**_STRING_ARRAY_SCHEMA, "minItems": 1},
        "unknown_dependencies": _STRING_ARRAY_SCHEMA,
        "falsification_condition": {"type": "string", "minLength": 1},
        "status": {"enum": ["UNVALIDATED"]},
    },
}


RESEARCH_REASONING_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "observed_facts",
        "interpretations",
        "hypotheses",
        "contradictions",
        "unknowns",
        "evidence_requests",
        "confidence",
    ],
    "properties": {
        "observed_facts": {"type": "array", "items": _FACT_REFERENCE_SCHEMA},
        "interpretations": {"type": "array", "items": _STATEMENT_SCHEMA},
        "hypotheses": {"type": "array", "items": _HYPOTHESIS_SCHEMA},
        "contradictions": {"type": "array", "items": _STATEMENT_SCHEMA},
        "unknowns": _STRING_ARRAY_SCHEMA,
        "evidence_requests": _STRING_ARRAY_SCHEMA,
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}


def parse_research_reasoning_output(
    value: object, facts: tuple[FactV1, ...]
) -> ResearchReasoningOutputV11:
    return _parse_research_reasoning_output(value, facts, require_condition_support=False)


def parse_research_reasoning_output_v111(
    value: object, facts: tuple[FactV1, ...]
) -> ResearchReasoningOutputV11:
    """Patch contract: condition facts must be explicit supporting evidence."""
    return _parse_research_reasoning_output(
        value, facts, require_condition_support=True, require_exact_condition_type=True
    )


def _parse_research_reasoning_output(
    value: object,
    facts: tuple[FactV1, ...],
    *,
    require_condition_support: bool,
    require_exact_condition_type: bool = False,
) -> ResearchReasoningOutputV11:
    if not isinstance(value, dict) or set(value) != set(RESEARCH_REASONING_JSON_SCHEMA["required"]):
        raise AIValidationError(
            "research reasoning output has missing or unknown fields",
            code="BASE_SCHEMA_INVALID",
            field_path="$",
            received=sorted(value) if isinstance(value, dict) else type(value).__name__,
            expected="exact ResearchReasoningOutputV11 fields",
        )
    known = {fact.fact_id: fact for fact in facts}
    observed = _observed(value["observed_facts"], known)
    interpretations = _statements(value["interpretations"], "interpretations", known)
    contradictions = _statements(value["contradictions"], "contradictions", known)
    for statement in (*interpretations, *contradictions):
        if violation := semantic_violation(statement.statement):
            raise AIValidationError(
                f"semantic violation: {violation}",
                code="SEMANTIC_VIOLATION",
                received=statement.statement,
            )
    hypotheses = _hypotheses(
        value["hypotheses"],
        known,
        require_condition_support=require_condition_support,
        require_exact_condition_type=require_exact_condition_type,
    )
    unknowns = _strings(value["unknowns"], "unknowns")
    evidence = _strings(value["evidence_requests"], "evidence_requests")
    confidence = value["confidence"]
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0 <= confidence <= 1
    ):
        raise AIValidationError("confidence must be between zero and one")
    return ResearchReasoningOutputV11(
        observed, interpretations, hypotheses, contradictions, unknowns, evidence, float(confidence)
    )


def _observed(value: object, known: dict[str, FactV1]) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise AIValidationError("observed_facts must be an array")
    result: list[str] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"fact_id"} or item["fact_id"] not in known:
            raise AIValidationError("observed fact must reference supplied fact")
        result.append(item["fact_id"])
    return tuple(result)


def _strings(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise AIValidationError(f"{field} must be an array of non-empty strings")
    return tuple(value)


def _statements(
    value: object, field: str, known: dict[str, FactV1]
) -> tuple[ReasonedStatementV1, ...]:
    if not isinstance(value, list):
        raise AIValidationError(f"{field} must be an array")
    result = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"statement", "supporting_fact_ids"}:
            raise AIValidationError(f"invalid {field} item")
        statement = item["statement"]
        references = _strings(item["supporting_fact_ids"], field)
        if not isinstance(statement, str) or not statement or not set(references) <= set(known):
            raise AIValidationError(f"invalid {field} references")
        result.append(ReasonedStatementV1(statement, references))
    return tuple(result)


def _hypotheses(
    value: object,
    known: dict[str, FactV1],
    *,
    require_condition_support: bool,
    require_exact_condition_type: bool,
) -> tuple[TestableHypothesisV11, ...]:
    if not isinstance(value, list):
        raise AIValidationError("hypotheses must be an array")
    required = {
        "condition",
        "target_asset",
        "target_metric",
        "target_horizon",
        "expected_relationship",
        "comparison_basis",
        "supporting_fact_ids",
        "unknown_dependencies",
        "falsification_condition",
        "status",
    }
    result = []
    for index, item in enumerate(value):
        if not isinstance(item, dict) or set(item) != required:
            raise AIValidationError(
                "invalid research hypothesis fields",
                code="HYPOTHESIS_SCHEMA_INVALID",
                field_path=f"hypotheses[{index}]",
                received=sorted(item) if isinstance(item, dict) else type(item).__name__,
                expected="exact TestableHypothesisV11 fields",
            )
        clauses_raw = item["condition"]
        if not isinstance(clauses_raw, list):
            raise AIValidationError("hypothesis condition must be an array")
        clauses = []
        for clause_index, clause in enumerate(clauses_raw):
            if not isinstance(clause, dict) or set(clause) != {"fact_id", "operator", "value"}:
                raise AIValidationError(
                    "invalid condition clause",
                    code="CONDITION_SCHEMA_INVALID",
                    field_path=f"hypotheses[{index}].condition[{clause_index}]",
                )
            fact_id = clause["fact_id"]
            if fact_id not in known or clause["operator"] not in CONDITION_OPERATORS:
                raise AIValidationError(
                    "condition must reference a supplied fact",
                    code="CONDITION_FACT_REFERENCE_INVALID",
                    field_path=f"hypotheses[{index}].condition[{clause_index}].fact_id",
                    received=fact_id,
                )
            exact_type_matches = type(clause["value"]) is type(known[fact_id].value)
            if clause["value"] != known[fact_id].value or (
                require_exact_condition_type and not exact_type_matches
            ):
                raise AIValidationError(
                    "condition value must equal supplied factual value",
                    code="CONDITION_VALUE_INVALID",
                    field_path=f"hypotheses[{index}].condition[{clause_index}].value",
                    received=clause["value"],
                    expected=repr(known[fact_id].value),
                )
            clauses.append(ConditionClauseV1(fact_id, clause["operator"], clause["value"]))
        references = _strings(item["supporting_fact_ids"], "supporting_fact_ids")
        if not set(references) <= set(known):
            raise AIValidationError(
                "hypothesis references unknown facts",
                code="HYPOTHESIS_FACT_REFERENCE_INVALID",
                field_path=f"hypotheses[{index}].supporting_fact_ids",
            )
        condition_fact_ids = {clause.fact_id for clause in clauses}
        if require_condition_support and not condition_fact_ids <= set(references):
            raise AIValidationError(
                "every condition fact must be included in supporting facts",
                code="CONDITION_FACT_NOT_SUPPORTED",
                field_path=f"hypotheses[{index}].supporting_fact_ids",
                received=sorted(condition_fact_ids - set(references)),
                expected="all condition fact IDs",
            )
        try:
            result.append(
                TestableHypothesisV11(
                    tuple(clauses),
                    item["target_asset"],
                    item["target_metric"],
                    item["target_horizon"],
                    item["expected_relationship"],
                    item["comparison_basis"],
                    references,
                    _strings(item["unknown_dependencies"], "unknown_dependencies"),
                    item["falsification_condition"],
                    item["status"],
                )
            )
        except AIValidationError as error:
            if error.field_path is None:
                error.field_path = f"hypotheses[{index}]"
            raise
    return tuple(result)


@dataclass(frozen=True, slots=True)
class RDEHypothesisQueryV1:
    candidate_id: str
    condition: tuple[ConditionClauseV1, ...]
    target_asset: str
    target_metric: str
    target_horizon: str
    expected_relationship: str
    comparison_basis: str
    supporting_fact_ids: tuple[str, ...]


def rde_query_from_hypothesis(hypothesis: TestableHypothesisV11) -> RDEHypothesisQueryV1:
    return RDEHypothesisQueryV1(
        hypothesis.candidate_id,
        hypothesis.condition,
        hypothesis.target_asset,
        hypothesis.target_metric,
        hypothesis.target_horizon,
        hypothesis.expected_relationship,
        hypothesis.comparison_basis,
        hypothesis.supporting_fact_ids,
    )


def is_rde_queryable(
    hypothesis: TestableHypothesisV11,
    facts: tuple[FactV1, ...],
    *,
    information_cutoff: datetime | None = None,
) -> bool:
    known = {fact.fact_id: fact for fact in facts}
    relevant_ids = {
        *hypothesis.supporting_fact_ids,
        *(clause.fact_id for clause in hypothesis.condition),
    }
    no_future_facts = information_cutoff is None or all(
        timestamp_ms(known[fact_id].timestamp) <= timestamp_ms(information_cutoff)
        for fact_id in relevant_ids
        if fact_id in known
    )
    return (
        hypothesis.status == "UNVALIDATED"
        and hypothesis.target_asset in ALLOWED_TARGET_ASSETS
        and hypothesis.target_metric in TARGET_METRICS
        and hypothesis.target_horizon in TARGET_HORIZONS
        and hypothesis.expected_relationship in RELATIONSHIPS
        and hypothesis.comparison_basis in COMPARISON_BASES
        and bool(hypothesis.condition)
        and set(hypothesis.supporting_fact_ids) <= set(known)
        and all(clause.fact_id in known for clause in hypothesis.condition)
        and no_future_facts
    )


@dataclass(frozen=True, slots=True)
class RDEFalsificationSpecV1:
    """Machine-authoritative failure condition derived from the target contract."""

    target_asset: str
    target_metric: str
    target_horizon: str
    comparison_basis: str
    required_relationship: str
    failure_relationship: str


@dataclass(frozen=True, slots=True)
class RDEHypothesisQueryV111:
    """v1.1.1 query contract; prose is deliberately excluded from authority."""

    candidate_id: str
    condition: tuple[ConditionClauseV1, ...]
    target_asset: str
    target_metric: str
    target_horizon: str
    expected_relationship: str
    comparison_basis: str
    supporting_fact_ids: tuple[str, ...]
    status: str
    falsification_spec: RDEFalsificationSpecV1


@dataclass(frozen=True, slots=True)
class CanonicalResearchPredicateV1:
    """Future executor input only; no benchmark-ID parsing or executor exists yet."""

    fact_id: str
    source_field: str
    operator: str
    value: str | int | float | bool | None
    asset: str | None
    time_semantics: str
    segment_requirement: str


@dataclass(frozen=True, slots=True)
class RDEExecutabilityAssessmentV1:
    level: RDEQueryabilityLevel
    status: RDEExecutabilityStatus
    reason_codes: tuple[str, ...]


class RDEQueryabilityLevel(StrEnum):
    STRUCTURALLY_VALID = "STRUCTURALLY_VALID"
    RDE_QUERYABLE = "RDE_QUERYABLE"
    RDE_EXECUTABLE = "RDE_EXECUTABLE"


class RDEExecutabilityStatus(StrEnum):
    EXECUTABLE = "EXECUTABLE"
    QUERYABLE_NOT_EXECUTABLE = "QUERYABLE_NOT_EXECUTABLE"
    NOT_QUERYABLE = "NOT_QUERYABLE"


_FALSIFICATION_RELATIONSHIPS = {
    "GREATER_THAN_MATCHED_CONTROL": "NOT_GREATER_THAN_MATCHED_CONTROL",
    "LESS_THAN_MATCHED_CONTROL": "NOT_LESS_THAN_MATCHED_CONTROL",
    "HIGHER_PROBABILITY_THAN_MATCHED_CONTROL": "NOT_HIGHER_PROBABILITY_THAN_MATCHED_CONTROL",
    "LOWER_PROBABILITY_THAN_MATCHED_CONTROL": "NOT_LOWER_PROBABILITY_THAN_MATCHED_CONTROL",
}


def derive_rde_falsification_spec_v111(
    hypothesis: TestableHypothesisV11,
) -> RDEFalsificationSpecV1:
    """Derive falsification from structured fields; free text is explanatory only."""
    return RDEFalsificationSpecV1(
        target_asset=hypothesis.target_asset,
        target_metric=hypothesis.target_metric,
        target_horizon=hypothesis.target_horizon,
        comparison_basis=hypothesis.comparison_basis,
        required_relationship=hypothesis.expected_relationship,
        failure_relationship=_FALSIFICATION_RELATIONSHIPS[hypothesis.expected_relationship],
    )


def is_rde_queryable_v111(
    hypothesis: TestableHypothesisV11,
    facts: tuple[FactV1, ...],
    *,
    information_cutoff: datetime | None = None,
) -> bool:
    """v1.1.1 adds explicit condition-evidence closure to the frozen v1.1 test."""
    return is_rde_queryable(hypothesis, facts, information_cutoff=information_cutoff) and {
        clause.fact_id for clause in hypothesis.condition
    } <= set(hypothesis.supporting_fact_ids)


def rde_query_from_hypothesis_v111(hypothesis: TestableHypothesisV11) -> RDEHypothesisQueryV111:
    return RDEHypothesisQueryV111(
        candidate_id=hypothesis.candidate_id,
        condition=hypothesis.condition,
        target_asset=hypothesis.target_asset,
        target_metric=hypothesis.target_metric,
        target_horizon=hypothesis.target_horizon,
        expected_relationship=hypothesis.expected_relationship,
        comparison_basis=hypothesis.comparison_basis,
        supporting_fact_ids=hypothesis.supporting_fact_ids,
        status=hypothesis.status,
        falsification_spec=derive_rde_falsification_spec_v111(hypothesis),
    )


def assess_rde_executability_v111(
    hypothesis: TestableHypothesisV11,
    facts: tuple[FactV1, ...],
    *,
    information_cutoff: datetime | None = None,
) -> RDEExecutabilityAssessmentV1:
    if not is_rde_queryable_v111(hypothesis, facts, information_cutoff=information_cutoff):
        return RDEExecutabilityAssessmentV1(
            RDEQueryabilityLevel.STRUCTURALLY_VALID,
            RDEExecutabilityStatus.NOT_QUERYABLE,
            ("NOT_RDE_QUERYABLE",),
        )
    return RDEExecutabilityAssessmentV1(
        RDEQueryabilityLevel.RDE_QUERYABLE,
        RDEExecutabilityStatus.QUERYABLE_NOT_EXECUTABLE,
        ("NO_CANONICAL_FACT_TO_RDE_PREDICATE_MAPPING",),
    )
