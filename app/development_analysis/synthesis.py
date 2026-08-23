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
from app.development_analysis.models import (
    DevelopmentCitationFailureV1,
    DevelopmentCitationKindV1,
    DevelopmentCitationRejectionV1,
    DevelopmentEvidencePackV1,
)

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
fact IDs from `allowed_fact_ids` only. A real canonical Knowledge fact that is absent from
`allowed_fact_ids` is unsupported. Never invent a fact ID; model memory is not evidence. If the
supplied evidence does not establish a claim, record a concise UNKNOWN instead. Separate facts,
inferences, unknowns, and proposals: risks, tests, non-goals, and acceptance criteria are bounded
proposals derived from the request and supplied evidence. Name an owner only when its supplied
evidence establishes it. List a contract or file only when supplied evidence establishes it. If an
exact future contract or file is not established, record a concise unknown saying it cannot be
determined from current evidence; never invent one. Address ownership, contracts, files, authority
risks, required tests, non-goals, and acceptance criteria. This is read-only and advisory: do not
claim file, shell, Git, Knowledge, trading, Risk, or execution authority. Hidden reasoning is not
part of the result."""


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


class DevelopmentCitationValidationError(AIValidationError):
    """Typed parser rejection that deliberately keeps no model response content."""

    def __init__(self, failure: DevelopmentCitationFailureV1) -> None:
        super().__init__(
            "development synthesis cites unsupported Knowledge",
            code="UNSUPPORTED_KNOWLEDGE_CITATION",
        )
        self.failure = failure


def parse_development_synthesis(
    value: object,
    context: KnowledgeContextV1,
    *,
    allowed_fact_ids: tuple[str, ...] | None = None,
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
    allowed_facts = set(allowed_fact_ids or development_allowed_fact_ids(context))
    statement_facts = {fact_id for fact_id in allowed_facts if fact_id.endswith(".statement")}
    facts = tuple(values["facts"])
    if not facts:
        raise AIValidationError(
            "development synthesis facts are required", code="DEVELOPMENT_SCHEMA_INVALID"
        )
    unsupported_fact = next(
        (item for item in sorted(set(facts)) if item not in allowed_facts), None
    )
    if unsupported_fact is not None:
        raise _citation_error(
            DevelopmentCitationKindV1.FACT_ID,
            unsupported_fact,
            DevelopmentCitationRejectionV1.NOT_IN_ALLOWLIST,
        )
    unsupported_fact_kind = next(
        (item for item in sorted(set(facts)) if item not in statement_facts), None
    )
    if unsupported_fact_kind is not None:
        raise _citation_error(
            DevelopmentCitationKindV1.FACT_ID,
            unsupported_fact_kind,
            DevelopmentCitationRejectionV1.UNSUPPORTED_BY_CITED_FACT,
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
        unsupported_owner = next(
            owner for owner in sorted(set(owners)) if owner not in grounded_owner_types
        )
        raise _citation_error(
            DevelopmentCitationKindV1.OWNER,
            unsupported_owner,
            DevelopmentCitationRejectionV1.UNSUPPORTED_BY_CITED_FACT,
        )
    unsupported_contract = next(
        (item for item in sorted(set(values["affected_contracts"])) if item not in titles), None
    )
    if unsupported_contract is not None:
        raise _citation_error(
            DevelopmentCitationKindV1.CONTRACT,
            unsupported_contract,
            DevelopmentCitationRejectionV1.NOT_IN_ALLOWLIST,
        )
    unsupported_file = next(
        (item for item in sorted(set(values["likely_files"])) if item not in paths), None
    )
    if unsupported_file is not None:
        raise _citation_error(
            DevelopmentCitationKindV1.FILE,
            unsupported_file,
            DevelopmentCitationRejectionV1.NOT_IN_ALLOWLIST,
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
    ai: object,
    request: object,
    context: KnowledgeContextV1,
    evidence: DevelopmentEvidencePackV1,
) -> DevelopmentSynthesisOutputV1:
    """Use the configured provider directly under the Development-owned contract."""
    now = datetime.now(tz=UTC)
    facts = knowledge_facts(context, now)
    allowed_fact_ids = tuple(sorted({fact.fact_id for fact in facts}))
    if allowed_fact_ids != evidence.allowed_fact_ids:
        raise AIValidationError(
            "development evidence allowlist is invalid", code="DEVELOPMENT_SCHEMA_INVALID"
        )
    model_request = ModelRequest(
        uuid.uuid4().hex,
        AITask.DEVELOPMENT_ANALYSIS_V1,
        ai.config.profile,
        DEVELOPMENT_SYNTHESIS_INSTRUCTION,
        DEVELOPMENT_SYNTHESIS_IDENTITY,
        facts,
        {
            "request": request.request,
            "knowledge_context_digest": context.context_digest,
            "allowed_fact_ids": list(evidence.allowed_fact_ids),
        },
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
    return parse_development_synthesis(
        result.structured_output, context, allowed_fact_ids=evidence.allowed_fact_ids
    )


def development_allowed_fact_ids(context: KnowledgeContextV1) -> tuple[str, ...]:
    """Return the exact deterministic statement-fact namespace available to synthesis."""
    return tuple(sorted({fact.fact_id for fact in knowledge_facts(context, datetime.now(tz=UTC))}))


def _citation_error(
    kind: DevelopmentCitationKindV1,
    identifier: str,
    rejection: DevelopmentCitationRejectionV1,
) -> DevelopmentCitationValidationError:
    safe_identifier = identifier if _safe_identifier(identifier) else "malformed-identifier"
    return DevelopmentCitationValidationError(
        DevelopmentCitationFailureV1(
            kind,
            safe_identifier,
            rejection
            if safe_identifier != "malformed-identifier"
            else DevelopmentCitationRejectionV1.MALFORMED,
        )
    )


def _safe_identifier(value: str) -> bool:
    return (
        len(value) <= 160
        and value.isascii()
        and all(character.isalnum() or character in " ._:/-" for character in value)
    )
