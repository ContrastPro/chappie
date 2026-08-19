"""Strict provider-neutral facts and reasoning output schemas."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.ai.errors import AIValidationError


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def timestamp_ms(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise AIValidationError("AI timestamps must be timezone-aware")
    return int(value.astimezone(UTC).timestamp() * 1000)


@dataclass(frozen=True, slots=True)
class FactV1:
    fact_id: str
    source_type: str
    timestamp: datetime
    field: str
    value: str | int | float | bool | None
    unit: str | None
    provenance_digest: str
    asset: str | None = None
    canonical_field: str | None = None
    value_type: str | None = None
    availability: str = "AVAILABLE"

    def __post_init__(self) -> None:
        if not self.fact_id or not self.source_type or not self.field:
            raise AIValidationError("fact identity, source type, and field are required")
        timestamp_ms(self.timestamp)
        if not self.provenance_digest:
            raise AIValidationError("fact provenance digest is required")
        if self.availability not in {"AVAILABLE", "UNAVAILABLE"}:
            raise AIValidationError("fact availability must be AVAILABLE or UNAVAILABLE")
        if self.value_type is not None and self.value_type not in {
            "STRING",
            "INTEGER",
            "FLOAT",
            "BOOLEAN",
            "NULL",
        }:
            raise AIValidationError("fact value type is unsupported")
        if self.value_type is not None and self.value_type != _value_type(self.value):
            raise AIValidationError("fact value type must match the supplied value")

    def payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "fact_id": self.fact_id,
            "source_type": self.source_type,
            "timestamp_ms": timestamp_ms(self.timestamp),
            "field": self.field,
            "value": self.value,
            "unit": self.unit,
            "provenance_digest": self.provenance_digest,
        }
        # Retain the exact frozen payload for legacy benchmark facts that do not
        # yet carry compiler metadata.
        metadata = {
            "asset": self.asset,
            "canonical_field": self.canonical_field,
            "value_type": self.value_type,
            "availability": self.availability,
        }
        if any(value is not None for key, value in metadata.items() if key != "availability") or (
            self.availability != "AVAILABLE"
        ):
            payload["metadata"] = metadata
        return payload


def _value_type(value: str | float | bool | None) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "BOOLEAN"
    if isinstance(value, int):
        return "INTEGER"
    if isinstance(value, float):
        return "FLOAT"
    return "STRING"


@dataclass(frozen=True, slots=True)
class ObservedFactReferenceV1:
    fact_id: str


@dataclass(frozen=True, slots=True)
class ReasonedStatementV1:
    statement: str
    supporting_fact_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HypothesisCandidateV1:
    claim: str
    supporting_fact_ids: tuple[str, ...]
    status: str = "UNVALIDATED"

    def __post_init__(self) -> None:
        if self.status != "UNVALIDATED":
            raise AIValidationError("AI hypotheses must remain UNVALIDATED")


@dataclass(frozen=True, slots=True)
class ReasoningOutputV1:
    observed_facts: tuple[ObservedFactReferenceV1, ...]
    interpretations: tuple[ReasonedStatementV1, ...]
    hypotheses: tuple[HypothesisCandidateV1, ...]
    contradictions: tuple[ReasonedStatementV1, ...]
    unknowns: tuple[str, ...]
    evidence_requests: tuple[str, ...]
    confidence: float

    def __post_init__(self) -> None:
        if isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float)):
            raise AIValidationError("confidence must be numeric")
        if not math.isfinite(float(self.confidence)) or not 0 <= self.confidence <= 1:
            raise AIValidationError("confidence must be finite and between zero and one")

    def payload(self) -> dict[str, object]:
        return {
            "observed_facts": [{"fact_id": item.fact_id} for item in self.observed_facts],
            "interpretations": [
                {"statement": item.statement, "supporting_fact_ids": list(item.supporting_fact_ids)}
                for item in self.interpretations
            ],
            "hypotheses": [
                {
                    "claim": item.claim,
                    "supporting_fact_ids": list(item.supporting_fact_ids),
                    "status": item.status,
                }
                for item in self.hypotheses
            ],
            "contradictions": [
                {"statement": item.statement, "supporting_fact_ids": list(item.supporting_fact_ids)}
                for item in self.contradictions
            ],
            "unknowns": list(self.unknowns),
            "evidence_requests": list(self.evidence_requests),
            "confidence": self.confidence,
        }


REASONING_OUTPUT_JSON_SCHEMA: dict[str, Any] = {
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
        "observed_facts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["fact_id"],
                "properties": {"fact_id": {"type": "string"}},
            },
        },
        "interpretations": {"type": "array", "items": {"$ref": "#/$defs/statement"}},
        "hypotheses": {"type": "array", "items": {"$ref": "#/$defs/hypothesis"}},
        "contradictions": {"type": "array", "items": {"$ref": "#/$defs/statement"}},
        "unknowns": {"type": "array", "items": {"type": "string"}},
        "evidence_requests": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "$defs": {
        "statement": {
            "type": "object",
            "additionalProperties": False,
            "required": ["statement", "supporting_fact_ids"],
            "properties": {
                "statement": {"type": "string"},
                "supporting_fact_ids": {"type": "array", "items": {"type": "string"}},
            },
        },
        "hypothesis": {
            "type": "object",
            "additionalProperties": False,
            "required": ["claim", "supporting_fact_ids", "status"],
            "properties": {
                "claim": {"type": "string"},
                "supporting_fact_ids": {"type": "array", "items": {"type": "string"}},
                "status": {"const": "UNVALIDATED"},
            },
        },
    },
}


def _strings(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise AIValidationError(f"{field} must be an array of strings")
    return tuple(value)


def _statements(value: object, field: str) -> tuple[ReasonedStatementV1, ...]:
    if not isinstance(value, list):
        raise AIValidationError(f"{field} must be an array")
    result = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"statement", "supporting_fact_ids"}:
            raise AIValidationError(f"invalid {field} item")
        statement = item["statement"]
        if not isinstance(statement, str) or not statement:
            raise AIValidationError(f"{field} statement must be non-empty")
        result.append(ReasonedStatementV1(statement, _strings(item["supporting_fact_ids"], field)))
    return tuple(result)


def parse_reasoning_output(value: object, known_fact_ids: set[str]) -> ReasoningOutputV1:
    if not isinstance(value, dict) or set(value) != set(REASONING_OUTPUT_JSON_SCHEMA["required"]):
        raise AIValidationError("structured output has missing or unknown fields")
    observed_raw = value["observed_facts"]
    if not isinstance(observed_raw, list):
        raise AIValidationError("observed_facts must be an array")
    observed = []
    referenced: set[str] = set()
    for item in observed_raw:
        if (
            not isinstance(item, dict)
            or set(item) != {"fact_id"}
            or not isinstance(item["fact_id"], str)
        ):
            raise AIValidationError("observed facts must contain only fact_id")
        observed.append(ObservedFactReferenceV1(item["fact_id"]))
        referenced.add(item["fact_id"])
    interpretations = _statements(value["interpretations"], "interpretations")
    contradictions = _statements(value["contradictions"], "contradictions")
    hypotheses_raw = value["hypotheses"]
    if not isinstance(hypotheses_raw, list):
        raise AIValidationError("hypotheses must be an array")
    hypotheses = []
    for item in hypotheses_raw:
        if not isinstance(item, dict) or set(item) != {"claim", "supporting_fact_ids", "status"}:
            raise AIValidationError("invalid hypothesis item")
        if not isinstance(item["claim"], str) or not item["claim"]:
            raise AIValidationError("hypothesis claim must be non-empty")
        hypothesis = HypothesisCandidateV1(
            item["claim"], _strings(item["supporting_fact_ids"], "hypothesis"), item["status"]
        )
        hypotheses.append(hypothesis)
    for statement in (*interpretations, *contradictions):
        referenced.update(statement.supporting_fact_ids)
    for hypothesis in hypotheses:
        referenced.update(hypothesis.supporting_fact_ids)
    unknown = referenced - known_fact_ids
    if unknown:
        raise AIValidationError(f"structured output references unknown fact IDs: {sorted(unknown)}")
    return ReasoningOutputV1(
        tuple(observed),
        interpretations,
        tuple(hypotheses),
        contradictions,
        _strings(value["unknowns"], "unknowns"),
        _strings(value["evidence_requests"], "evidence_requests"),
        value["confidence"],
    )
