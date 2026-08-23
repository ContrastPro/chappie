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
from app.development_analysis.context import DevelopmentContextMapV1, build_development_context_map
from app.development_analysis.models import (
    DevelopmentCitationFailureV1,
    DevelopmentCitationKindV1,
    DevelopmentCitationRejectionV1,
    DevelopmentEvidencePackV1,
    DevelopmentProposedScopeV1,
)

if TYPE_CHECKING:
    from app.knowledge.store import KnowledgeContextV1

DEVELOPMENT_SYNTHESIS_IDENTITY = "chappie_development_synthesis/2.0.0"
DEVELOPMENT_SYNTHESIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "current_evidence_handles",
        "inferences",
        "unknowns",
        "proposed_scope",
        "risks",
        "required_tests",
        "non_goals",
        "acceptance_criteria",
        "confidence",
    ],
    "properties": {
        "current_evidence_handles": {"type": "array", "items": {"type": "string"}},
        "inferences": {"type": "array", "items": {"type": "string"}},
        "unknowns": {"type": "array", "items": {"type": "string"}},
        "proposed_scope": {"type": "object"},
        "risks": {"type": "array", "items": {"type": "string"}},
        "required_tests": {"type": "array", "items": {"type": "string"}},
        "non_goals": {"type": "array", "items": {"type": "string"}},
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}

DEVELOPMENT_SYNTHESIS_INSTRUCTION = """You are Chappie Development Intelligence.
Analyze the USER DEVELOPMENT REQUEST, not the repository in general. Return only the required
JSON object. Repository Knowledge facts are authoritative. `current_evidence_handles` may contain
only supplied E-handles from `development_context_map`; never invent a handle. A real canonical
Knowledge fact absent from the context map is unsupported; model memory is not evidence. If the
supplied evidence does not establish a claim, record a concise UNKNOWN instead. Separate facts,
inferences, unknowns, and proposals: risks, tests, non-goals, and acceptance criteria are bounded
proposals derived from the request and supplied evidence. CURRENT evidence handles describe only
existing state. `proposed_scope` is explicitly future-facing: it may name a safe repository-relative
new file or a proposed contract, but must never present either as already existing. Its proposed
owner must be one of the supplied owner candidates. If current evidence is insufficient, record a
concise unknown. Address ownership, contracts, files, authority risks, required tests, non-goals,
and acceptance criteria. This is read-only and advisory: do not
claim file, shell, Git, Knowledge, trading, Risk, or execution authority. Hidden reasoning is not
part of the result."""


@dataclass(frozen=True, slots=True, init=False)
class DevelopmentSynthesisOutputV1:
    current_evidence_handles: tuple[str, ...]
    inferences: tuple[str, ...]
    unknowns: tuple[str, ...]
    proposed_scope: DevelopmentProposedScopeV1
    risks: tuple[str, ...]
    required_tests: tuple[str, ...]
    non_goals: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    confidence: float
    _legacy_fact_ids: tuple[str, ...]
    _legacy_architecture_owners: tuple[str, ...]
    architecture_owners: tuple[str, ...]

    def __init__(
        self,
        *,
        current_evidence_handles: tuple[str, ...] = (),
        inferences: tuple[str, ...],
        unknowns: tuple[str, ...],
        proposed_scope: DevelopmentProposedScopeV1 | None = None,
        risks: tuple[str, ...],
        required_tests: tuple[str, ...],
        non_goals: tuple[str, ...],
        acceptance_criteria: tuple[str, ...],
        confidence: float,
        # Temporary in-process compatibility for existing deterministic runners.
        facts: tuple[str, ...] = (),
        architecture_owners: tuple[str, ...] = (),
        affected_contracts: tuple[str, ...] = (),
        likely_files: tuple[str, ...] = (),
    ) -> None:
        del affected_contracts
        if proposed_scope is None:
            proposed_scope = DevelopmentProposedScopeV1(
                None,
                likely_files,
                (),
                (),
                ("Bounded development capability proposal",),
                ("Remain advisory and read-only.",),
                ("Compatibility projection for a deterministic in-process runner.",),
            )
        object.__setattr__(self, "current_evidence_handles", current_evidence_handles)
        object.__setattr__(self, "inferences", inferences)
        object.__setattr__(self, "unknowns", unknowns)
        object.__setattr__(self, "proposed_scope", proposed_scope)
        object.__setattr__(self, "risks", risks)
        object.__setattr__(self, "required_tests", required_tests)
        object.__setattr__(self, "non_goals", non_goals)
        object.__setattr__(self, "acceptance_criteria", acceptance_criteria)
        object.__setattr__(self, "confidence", confidence)
        object.__setattr__(self, "_legacy_fact_ids", facts)
        object.__setattr__(self, "_legacy_architecture_owners", architecture_owners)
        object.__setattr__(self, "architecture_owners", architecture_owners)


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
    context_map: DevelopmentContextMapV1 | None = None,
) -> DevelopmentSynthesisOutputV1:
    if context_map is None and isinstance(value, dict) and "facts" in value:
        return _parse_legacy_test_synthesis(value, context)
    if not isinstance(value, dict) or set(value) != set(DEVELOPMENT_SYNTHESIS_SCHEMA["required"]):
        raise AIValidationError(
            "development synthesis schema is invalid", code="DEVELOPMENT_SCHEMA_INVALID"
        )
    values = {key: value[key] for key in DEVELOPMENT_SYNTHESIS_SCHEMA["required"]}
    if any(
        not isinstance(values[key], list)
        or any(not isinstance(item, str) or not item.strip() for item in values[key])
        for key in values
        if key not in {"confidence", "proposed_scope"}
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
    if context_map is None:
        raise AIValidationError(
            "development context map is required", code="DEVELOPMENT_SCHEMA_INVALID"
        )
    handles = tuple(values["current_evidence_handles"])
    if not handles:
        raise AIValidationError(
            "development synthesis evidence is required", code="DEVELOPMENT_SCHEMA_INVALID"
        )
    allowed_handles = {item.handle for item in context_map.evidence_handles}
    unsupported_handle = next(
        (item for item in sorted(set(handles)) if item not in allowed_handles), None
    )
    if unsupported_handle is not None:
        raise _citation_error(
            DevelopmentCitationKindV1.FACT_ID,
            unsupported_handle,
            DevelopmentCitationRejectionV1.NOT_IN_ALLOWLIST,
        )
    proposed_scope = _parse_proposed_scope(values["proposed_scope"], context_map)
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
        current_evidence_handles=handles,
        inferences=tuple(values["inferences"]),
        unknowns=tuple(values["unknowns"]),
        proposed_scope=proposed_scope,
        risks=tuple(values["risks"]),
        required_tests=tuple(values["required_tests"]),
        non_goals=tuple(values["non_goals"]),
        acceptance_criteria=tuple(values["acceptance_criteria"]),
        confidence=float(confidence),
    )


def _parse_legacy_test_synthesis(
    value: dict[str, object], context: KnowledgeContextV1
) -> DevelopmentSynthesisOutputV1:
    """Preserve direct in-process fixture compatibility; production always supplies a map."""
    required = {
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
    }
    if set(value) != required:
        raise AIValidationError(
            "development synthesis schema is invalid", code="DEVELOPMENT_SCHEMA_INVALID"
        )
    all_facts = development_allowed_fact_ids(context)
    fact_ids = value["facts"]
    if not isinstance(fact_ids, list) or any(not isinstance(item, str) for item in fact_ids):
        raise AIValidationError(
            "development synthesis schema is invalid", code="DEVELOPMENT_SCHEMA_INVALID"
        )
    unsupported = next((item for item in fact_ids if item not in all_facts), None)
    if unsupported is not None:
        raise _citation_error(
            DevelopmentCitationKindV1.FACT_ID,
            unsupported,
            DevelopmentCitationRejectionV1.NOT_IN_ALLOWLIST,
        )

    def strings(key: str) -> tuple[str, ...]:
        raw = value[key]
        if not isinstance(raw, list) or any(
            not isinstance(item, str) or not item.strip() for item in raw
        ):
            raise AIValidationError(
                "development synthesis schema is invalid", code="DEVELOPMENT_SCHEMA_INVALID"
            )
        return tuple(raw)

    owners = strings("architecture_owners")
    if any(
        owner not in {record.knowledge_type.value for record in context.records} for owner in owners
    ):
        raise _citation_error(
            DevelopmentCitationKindV1.OWNER,
            next(
                owner
                for owner in owners
                if owner not in {record.knowledge_type.value for record in context.records}
            ),
            DevelopmentCitationRejectionV1.UNSUPPORTED_BY_CITED_FACT,
        )
    files = strings("likely_files")
    evidence_files = {source.path for record in context.records for source in record.sources}
    unsupported_file = next((item for item in files if item not in evidence_files), None)
    if unsupported_file is not None:
        raise _citation_error(
            DevelopmentCitationKindV1.FILE,
            unsupported_file,
            DevelopmentCitationRejectionV1.NOT_IN_ALLOWLIST,
        )
    contracts = strings("affected_contracts")
    evidence_contracts = {record.title for record in context.records if "V1" in record.title}
    unsupported_contract = next(
        (item for item in contracts if item not in evidence_contracts), None
    )
    if unsupported_contract is not None:
        raise _citation_error(
            DevelopmentCitationKindV1.CONTRACT,
            unsupported_contract,
            DevelopmentCitationRejectionV1.NOT_IN_ALLOWLIST,
        )
    confidence = value["confidence"]
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0 <= confidence <= 1
    ):
        raise AIValidationError(
            "development synthesis confidence is invalid", code="DEVELOPMENT_SCHEMA_INVALID"
        )
    return DevelopmentSynthesisOutputV1(
        facts=tuple(fact_ids),
        architecture_owners=owners,
        likely_files=files,
        inferences=strings("inferences"),
        unknowns=strings("unknowns"),
        risks=strings("risks"),
        required_tests=strings("required_tests"),
        non_goals=strings("non_goals"),
        acceptance_criteria=strings("acceptance_criteria"),
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
    context_map = build_development_context_map(context, evidence)
    model_request = ModelRequest(
        uuid.uuid4().hex,
        AITask.DEVELOPMENT_ANALYSIS_V1,
        ai.config.profile,
        DEVELOPMENT_SYNTHESIS_INSTRUCTION,
        DEVELOPMENT_SYNTHESIS_IDENTITY,
        (),
        {
            "request": request.request,
            "knowledge_context_digest": context.context_digest,
            "development_context_map": context_map.payload(),
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
    return parse_development_synthesis(result.structured_output, context, context_map=context_map)


def development_allowed_fact_ids(context: KnowledgeContextV1) -> tuple[str, ...]:
    """Return the exact deterministic statement-fact namespace available to synthesis."""
    return tuple(sorted({fact.fact_id for fact in knowledge_facts(context, datetime.now(tz=UTC))}))


def _parse_proposed_scope(
    value: object, context_map: DevelopmentContextMapV1
) -> DevelopmentProposedScopeV1:
    required = {
        "proposed_owner",
        "proposed_modified_files",
        "proposed_new_files",
        "proposed_contract_changes",
        "proposed_components",
        "proposed_behavior",
        "rationale",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise AIValidationError(
            "development proposed scope is invalid", code="DEVELOPMENT_SCHEMA_INVALID"
        )
    owner = value["proposed_owner"]
    if owner is not None and (
        not isinstance(owner, str) or owner not in context_map.owner_candidates
    ):
        raise _citation_error(
            DevelopmentCitationKindV1.OWNER,
            owner if isinstance(owner, str) else "invalid-owner",
            DevelopmentCitationRejectionV1.NOT_IN_ALLOWLIST,
        )
    lists: dict[str, tuple[str, ...]] = {}
    for key in required - {"proposed_owner"}:
        raw = value[key]
        if not isinstance(raw, list) or any(
            not isinstance(item, str) or not item.strip() for item in raw
        ):
            raise AIValidationError(
                "development proposed scope is invalid", code="DEVELOPMENT_SCHEMA_INVALID"
            )
        lists[key] = tuple(raw)
    paths = (*lists["proposed_modified_files"], *lists["proposed_new_files"])
    if any(not _safe_proposed_path(path) for path in paths):
        raise AIValidationError(
            "development proposed path is invalid", code="DEVELOPMENT_SCHEMA_INVALID"
        )
    if any(
        _forbidden_authority(item) for item in (*lists["proposed_behavior"], *lists["rationale"])
    ):
        raise AIValidationError(
            "development proposal claims forbidden authority", code="DEVELOPMENT_SCHEMA_INVALID"
        )
    return DevelopmentProposedScopeV1(
        owner,
        lists["proposed_modified_files"],
        lists["proposed_new_files"],
        lists["proposed_contract_changes"],
        lists["proposed_components"],
        lists["proposed_behavior"],
        lists["rationale"],
    )


def _safe_proposed_path(value: str) -> bool:
    normalized = value.replace("\\", "/")
    return (
        bool(value)
        and not normalized.startswith(("/", "../", "data/", "logs/", ".venv/", ".env"))
        and ".." not in normalized.split("/")
        and ":" not in normalized
    )


def _forbidden_authority(value: str) -> bool:
    return any(
        token in value.casefold()
        for token in ("edit file", "write file", "shell", "git commit", "execute order")
    )


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
