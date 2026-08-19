"""Strict source-bounded Project QA over Chappie-owned knowledge context."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.ai.errors import AIValidationError
from app.ai.schemas import FactV1

if TYPE_CHECKING:
    from app.knowledge import KnowledgeContextV1

PROJECT_QA_IDENTITY = "project_qa/1.0.0"
PROJECT_QA_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer", "supporting_knowledge_fact_ids", "conflicts", "unknowns", "confidence"],
    "properties": {
        "answer": {"type": "string", "minLength": 1},
        "supporting_knowledge_fact_ids": {"type": "array", "items": {"type": "string"}},
        "conflicts": {"type": "array", "items": {"type": "string"}},
        "unknowns": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}


@dataclass(frozen=True, slots=True)
class ProjectQAAnswerV1:
    answer: str
    supporting_knowledge_fact_ids: tuple[str, ...]
    conflicts: tuple[str, ...]
    unknowns: tuple[str, ...]
    confidence: float
    context_digest: str

    def payload(self):
        return {
            "answer": self.answer,
            "supporting_knowledge_fact_ids": list(self.supporting_knowledge_fact_ids),
            "conflicts": list(self.conflicts),
            "unknowns": list(self.unknowns),
            "confidence": self.confidence,
            "context_digest": self.context_digest,
        }


def knowledge_facts(context: KnowledgeContextV1, timestamp) -> tuple[FactV1, ...]:
    facts = []
    for item in context.records:
        for field, value in (
            ("statement", item.statement),
            ("authority", item.authority.value),
            ("status", item.status.value),
        ):
            facts.append(
                FactV1(
                    f"knowledge.{item.knowledge_id}.{field}",
                    "CHAPPIE_KNOWLEDGE",
                    timestamp,
                    field,
                    value,
                    None,
                    item.record_digest,
                    canonical_field=field,
                    value_type="STRING",
                )
            )
    return tuple(facts)


def parse_project_qa(value: object, context: KnowledgeContextV1) -> ProjectQAAnswerV1:
    if not isinstance(value, dict) or set(value) != set(PROJECT_QA_JSON_SCHEMA["required"]):
        raise AIValidationError("project QA must match strict schema")
    answer = value["answer"]
    ids = value["supporting_knowledge_fact_ids"]
    unknowns = value["unknowns"]
    conflicts = value["conflicts"]
    confidence = value["confidence"]
    if (
        not isinstance(answer, str)
        or not answer.strip()
        or not all(isinstance(x, str) for x in ids + unknowns + conflicts)
        or isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0 <= confidence <= 1
    ):
        raise AIValidationError("project QA output fields are invalid")
    allowed = {
        f"knowledge.{item.knowledge_id}.{field}"
        for item in context.records
        for field in ("statement", "authority", "status")
    }
    if set(ids).difference(allowed):
        raise AIValidationError("project QA cites unknown knowledge facts")
    if context.conflict_record_ids and not conflicts:
        raise AIValidationError("project QA must surface supplied conflicts")
    if not ids and not unknowns:
        raise AIValidationError("project QA unsupported answer must declare unknowns")
    return ProjectQAAnswerV1(
        answer,
        tuple(ids),
        tuple(conflicts),
        tuple(unknowns),
        float(confidence),
        context.context_digest,
    )
