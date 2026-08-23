"""Dedicated bounded model contract for read-only Development Intelligence."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.ai.errors import AIValidationError
from app.ai.models import ModelRequest
from app.ai.project_qa import knowledge_facts
from app.ai.schemas import digest
from app.ai.tasks import AITask

if TYPE_CHECKING:
    from app.knowledge.store import KnowledgeContextV1

DEVELOPMENT_SYNTHESIS_IDENTITY = "chappie_development_synthesis/1.0.0"
DEVELOPMENT_SYNTHESIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "facts",
        "inferences",
        "unknowns",
        "architecture_owners",
        "affected_contracts",
        "likely_files",
        "risks",
        "required_tests",
        "non_goals",
        "acceptance_criteria",
        "confidence",
    ],
    "properties": {
        "facts": {"type": "array", "items": {"type": "string"}},
        "inferences": {"type": "array", "items": {"type": "string"}},
        "unknowns": {"type": "array", "items": {"type": "string"}},
        "architecture_owners": {"type": "array", "items": {"type": "string"}},
        "affected_contracts": {"type": "array", "items": {"type": "string"}},
        "likely_files": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "required_tests": {"type": "array", "items": {"type": "string"}},
        "non_goals": {"type": "array", "items": {"type": "string"}},
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}

DEVELOPMENT_SYNTHESIS_INSTRUCTION = """You are Chappie Development Intelligence.
Analyze the USER DEVELOPMENT REQUEST, not the repository in general. Return only the required
JSON object. Repository Knowledge facts are authoritative. `facts` must contain exact supplied
fact IDs only. Separate facts, inferences, unknowns, and proposals: risks, tests, non-goals, and
acceptance criteria are bounded proposals derived from the request and supplied evidence. Name an
owner only when its supplied evidence establishes it. List a contract or file only when supplied
evidence establishes it. If an exact future contract or file is not established, record a concise
unknown saying it cannot be determined from current evidence; never invent one. Address ownership,
contracts, files, authority risks, required tests, non-goals, and acceptance criteria. This is
read-only and advisory: do not claim file, shell, Git, Knowledge, trading, Risk, or execution
authority. Hidden reasoning is not part of the result."""


@dataclass(frozen=True, slots=True)
class DevelopmentSynthesisOutputV1:
    facts: tuple[str, ...]
    inferences: tuple[str, ...]
    unknowns: tuple[str, ...]
    architecture_owners: tuple[str, ...]
    affected_contracts: tuple[str, ...]
    likely_files: tuple[str, ...]
    risks: tuple[str, ...]
    required_tests: tuple[str, ...]
    non_goals: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    confidence: float


def parse_development_synthesis(
    value: object, context: KnowledgeContextV1
) -> DevelopmentSynthesisOutputV1:
    if not isinstance(value, dict) or set(value) != set(DEVELOPMENT_SYNTHESIS_SCHEMA["required"]):
        raise AIValidationError(
            "development synthesis schema is invalid", code="DEVELOPMENT_SCHEMA_INVALID"
        )
    values = {key: value[key] for key in DEVELOPMENT_SYNTHESIS_SCHEMA["required"]}
    if any(
        not isinstance(values[key], list)
        or any(not isinstance(item, str) or not item.strip() for item in values[key])
        for key in values
        if key != "confidence"
    ):
        raise AIValidationError(
            "development synthesis fields are invalid", code="DEVELOPMENT_SCHEMA_INVALID"
        )
    confidence = values["confidence"]
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0 <= confidence <= 1
    ):
        raise AIValidationError(
            "development synthesis confidence is invalid", code="DEVELOPMENT_SCHEMA_INVALID"
        )
    known_facts = {
        fact.fact_id: fact.value for fact in knowledge_facts(context, datetime.now(tz=UTC))
    }
    statement_facts = {fact_id for fact_id in known_facts if fact_id.endswith(".statement")}
    facts = tuple(values["facts"])
    if not facts or set(facts).difference(statement_facts):
        raise AIValidationError(
            "development synthesis cites unsupported Knowledge",
            code="UNSUPPORTED_KNOWLEDGE_CITATION",
        )
    titles = {record.title for record in context.records}
    paths = {source.path for record in context.records for source in record.sources}
    owners = tuple(values["architecture_owners"])
    grounded_owner_types = {
        record.knowledge_type.value
        for record in context.records
        if f"knowledge.{record.knowledge_id}.statement" in facts
    }
    if any(owner not in grounded_owner_types for owner in owners):
        raise AIValidationError(
            "development synthesis owner is unsupported", code="UNSUPPORTED_KNOWLEDGE_CITATION"
        )
    if set(values["affected_contracts"]).difference(titles) or set(
        values["likely_files"]
    ).difference(paths):
        raise AIValidationError(
            "development synthesis scope is unsupported", code="UNSUPPORTED_KNOWLEDGE_CITATION"
        )
    required_nonempty = (
        "inferences",
        "unknowns",
        "risks",
        "required_tests",
        "non_goals",
        "acceptance_criteria",
    )
    if any(not values[key] for key in required_nonempty):
        raise AIValidationError(
            "development synthesis required fields are empty", code="DEVELOPMENT_SCHEMA_INVALID"
        )
    return DevelopmentSynthesisOutputV1(
        facts=facts,
        inferences=tuple(values["inferences"]),
        unknowns=tuple(values["unknowns"]),
        architecture_owners=owners,
        affected_contracts=tuple(values["affected_contracts"]),
        likely_files=tuple(values["likely_files"]),
        risks=tuple(values["risks"]),
        required_tests=tuple(values["required_tests"]),
        non_goals=tuple(values["non_goals"]),
        acceptance_criteria=tuple(values["acceptance_criteria"]),
        confidence=float(confidence),
    )


def synthesize_development(
    ai: object, request: object, context: KnowledgeContextV1
) -> DevelopmentSynthesisOutputV1:
    """Use the configured provider directly under the Development-owned contract."""
    now = datetime.now(tz=UTC)
    facts = knowledge_facts(context, now)
    model_request = ModelRequest(
        uuid.uuid4().hex,
        AITask.DEVELOPMENT_ANALYSIS_V1,
        ai.config.profile,
        DEVELOPMENT_SYNTHESIS_INSTRUCTION,
        DEVELOPMENT_SYNTHESIS_IDENTITY,
        facts,
        {"request": request.request, "knowledge_context_digest": context.context_digest},
        DEVELOPMENT_SYNTHESIS_SCHEMA,
        ai.config.temperature,
        ai.config.timeout_seconds,
        now,
        digest(
            {
                "identity": DEVELOPMENT_SYNTHESIS_IDENTITY,
                "request": request.request,
                "context": context.context_digest,
            }
        ),
    )
    result = ai.router.provider_for(ai.config.profile).generate(model_request)
    return parse_development_synthesis(result.structured_output, context)
