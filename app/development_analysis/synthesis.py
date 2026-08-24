"""Dedicated bounded model contract for read-only Development Intelligence."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from app.ai.errors import AIProviderError, AIProviderTimeoutError, AIValidationError
from app.ai.models import ModelRequest
from app.ai.project_qa import knowledge_facts
from app.ai.schemas import canonical_json, digest
from app.ai.tasks import AITask
from app.development_analysis.context import (
    DevelopmentContextMapV1,
    build_context_digest,
    build_development_context_map,
)
from app.development_analysis.models import (
    DevelopmentCitationFailureV1,
    DevelopmentCitationKindV1,
    DevelopmentCitationRejectionV1,
    DevelopmentEvidencePackV1,
    DevelopmentProposedScopeV1,
    DevelopmentTaskIntentKindV1,
)
from app.development_analysis.review_targets import (
    ReviewProposalTargetProjectionDiagnosticsV1,
    ReviewProposalTargetProjectionError,
    ReviewProposalTargetSetV1,
    build_review_proposal_target_set,
)

if TYPE_CHECKING:
    from app.knowledge.store import KnowledgeContextV1

DEVELOPMENT_SYNTHESIS_IDENTITY = "chappie_development_synthesis/2.0.0"
DEVELOPMENT_GROUNDING_IDENTITY = "chappie_development_grounding/1.0.0"
DEVELOPMENT_PROPOSAL_IDENTITY = "chappie_development_proposal/1.0.0"
DEVELOPMENT_REVIEW_IDENTITY = "chappie_development_review/1.0.0"


class DevelopmentReviewModeV1(StrEnum):
    DETERMINISTIC_LOW_RISK = "DETERMINISTIC_LOW_RISK"
    MODEL_REVIEW = "MODEL_REVIEW"


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

GROUNDING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["relevant_evidence_handles", "task_interpretation", "unknowns"],
    "properties": {
        "relevant_evidence_handles": {"type": "array", "items": {"type": "string"}},
        "task_interpretation": {"type": "string"},
        "unknowns": {"type": "array", "items": {"type": "string"}},
    },
}
PROPOSED_SCOPE_FIELDS = (
    "proposed_owner",
    "proposed_modified_files",
    "proposed_new_files",
    "proposed_contract_changes",
    "proposed_components",
    "proposed_behavior",
    "rationale",
)
PROPOSED_SCOPE_LIST_FIELDS = PROPOSED_SCOPE_FIELDS[1:]
PROPOSED_SCOPE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": list(PROPOSED_SCOPE_FIELDS),
    "properties": {
        "proposed_owner": {"type": ["string", "null"]},
        **{
            field: {"type": "array", "items": {"type": "string"}}
            for field in PROPOSED_SCOPE_LIST_FIELDS
        },
    },
}
PROPOSAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "proposed_scope",
        "proposal_unknowns",
    ],
    "properties": {
        "proposed_scope": PROPOSED_SCOPE_SCHEMA,
        "proposal_unknowns": {"type": "array", "items": {"type": "string"}},
    },
}
REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "risks",
        "required_tests",
        "non_goals",
        "acceptance_criteria",
        "unresolved_unknowns",
        "confidence",
    ],
    "properties": {
        "risks": {"type": "array", "items": {"type": "string"}},
        "required_tests": {"type": "array", "items": {"type": "string"}},
        "non_goals": {"type": "array", "items": {"type": "string"}},
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
        "unresolved_unknowns": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}

_INTENT_COMMON_PROPERTIES = {
    "relevant_evidence_handles": {"type": "array", "items": {"type": "string"}},
    "answer": {"type": "string"},
    "unknowns": {"type": "array", "items": {"type": "string"}},
}
QUESTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["relevant_evidence_handles", "answer", "unknowns"],
    "properties": _INTENT_COMMON_PROPERTIES,
}
ANALYSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["relevant_evidence_handles", "answer", "inferences", "unknowns", "risks"],
    "properties": {
        **_INTENT_COMMON_PROPERTIES,
        "inferences": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
    },
}
PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "relevant_evidence_handles",
        "answer",
        "plan_items",
        "unknowns",
        "risks",
        "non_goals",
    ],
    "properties": {
        **_INTENT_COMMON_PROPERTIES,
        "plan_items": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "non_goals": {"type": "array", "items": {"type": "string"}},
    },
}


def proposal_provider_schema(owner_candidates: tuple[str, ...]) -> dict[str, Any]:
    """Return the exact Proposal schema, bounded to request-local canonical owners."""
    scope_properties = {
        **PROPOSED_SCOPE_SCHEMA["properties"],
        "proposed_owner": {
            "type": ["string", "null"],
            "enum": [*owner_candidates, None],
        },
    }
    return {
        **PROPOSAL_SCHEMA,
        "properties": {
            **PROPOSAL_SCHEMA["properties"],
            "proposed_scope": {
                **PROPOSED_SCOPE_SCHEMA,
                "properties": scope_properties,
            },
        },
    }


def proposal_instruction(owner_candidates: tuple[str, ...]) -> str:
    candidates = ", ".join(owner_candidates) if owner_candidates else "(none)"
    return f"""Return only the exact Proposal JSON schema. This stage produces ONLY future
implementation scope from the supplied goal, context map, and validated grounding. Do not state
proposals as CURRENT facts or claim authority. `proposed_scope` must contain exactly:
`proposed_owner`, `proposed_modified_files`, `proposed_new_files`,
`proposed_contract_changes`, `proposed_components`, `proposed_behavior`, and `rationale`.
Use `proposal_unknowns` for unresolved proposal questions. `proposed_owner` must be exactly one
canonical value from `owner_candidates`: {candidates}. Do not use descriptive labels or invent a
package name. If no candidate is justified, return `null` and explain the unresolved ownership
decision in `proposal_unknowns`. Do NOT produce `risks`, `tests`, `acceptance_criteria`, or
`non_goals`; those belong only to the later REVIEW stage. Do NOT use alternative keys such as
`description`, `ownership`, `contracts`, or `boundaries`. Do not include Markdown or prose outside
the structured object."""


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


class _ProposalAuthorityViolation(AIValidationError):
    """Internal marker for a fail-closed Proposal authority rejection."""


class DevelopmentStageError(AIValidationError):
    """A bounded stage failure; no model output is retained in the exception."""

    def __init__(
        self,
        stage: str,
        code: str,
        *,
        stage_elapsed_ms: tuple[tuple[str, int], ...] = (),
        proposal_diagnostics: ProposalParseDiagnosticsV1 | None = None,
        grounding_diagnostics: GroundingParseDiagnosticsV1 | None = None,
        review_diagnostics: ReviewParseDiagnosticsV1 | None = None,
        review_target_diagnostics: ReviewProposalTargetProjectionDiagnosticsV1 | None = None,
        completed_grounding: DevelopmentGroundingV1 | None = None,
        completed_proposal: DevelopmentProposalV1 | None = None,
    ) -> None:
        super().__init__("development staged synthesis failed", code=code)
        self.stage = stage
        self.stage_elapsed_ms = stage_elapsed_ms
        self.proposal_diagnostics = proposal_diagnostics
        self.grounding_diagnostics = grounding_diagnostics
        self.review_diagnostics = review_diagnostics
        self.review_target_diagnostics = review_target_diagnostics
        self.completed_grounding = completed_grounding
        self.completed_proposal = completed_proposal


@dataclass(frozen=True, slots=True)
class GroundingParseDiagnosticsV1:
    """Bounded structural metadata for an unsupported public evidence handle."""

    parser_stage: str
    reason_code: str
    bounded_error_path: str
    bounded_error_category: str

    def payload(self) -> dict[str, str]:
        return {
            "parser_stage": self.parser_stage,
            "reason_code": self.reason_code,
            "bounded_error_path": self.bounded_error_path,
            "bounded_error_category": self.bounded_error_category,
        }


@dataclass(frozen=True, slots=True)
class ReviewParseDiagnosticsV1:
    """Content-free structural metadata for Review specificity failures."""

    parser_stage: str
    reason_code: str
    bounded_error_path: str
    bounded_error_category: str
    action_recognized: bool
    target_recognized: bool
    condition_recognized: bool
    condition_category: str | None
    target_kind: str | None = None
    target_count: int | None = None

    def payload(self) -> dict[str, object]:
        return {
            "parser_stage": self.parser_stage,
            "reason_code": self.reason_code,
            "bounded_error_path": self.bounded_error_path,
            "bounded_error_category": self.bounded_error_category,
            "action_recognized": self.action_recognized,
            "target_recognized": self.target_recognized,
            "condition_recognized": self.condition_recognized,
            "condition_category": self.condition_category,
            "target_kind": self.target_kind,
            "target_count": self.target_count,
        }


@dataclass(frozen=True, slots=True)
class ProposalParseDiagnosticsV1:
    """Safe structural-only metadata for an acceptance-only rejected Proposal."""

    parser_stage: str
    reason_code: str
    transport_form: str
    response_length: int | None
    parsed_top_level_type: str
    parsed_top_level_keys: tuple[str, ...]
    missing_top_level_keys: tuple[str, ...]
    unexpected_top_level_keys: tuple[str, ...]
    proposed_scope_type: str | None
    proposed_scope_keys: tuple[str, ...]
    missing_scope_keys: tuple[str, ...]
    unexpected_scope_keys: tuple[str, ...]
    field_type_map: tuple[tuple[str, str], ...]
    bounded_error_path: str
    bounded_error_category: str

    def payload(self) -> dict[str, object]:
        return {
            "parser_stage": self.parser_stage,
            "reason_code": self.reason_code,
            "transport_form": self.transport_form,
            "response_length": self.response_length,
            "parsed_top_level_type": self.parsed_top_level_type,
            "parsed_top_level_keys": list(self.parsed_top_level_keys),
            "missing_top_level_keys": list(self.missing_top_level_keys),
            "unexpected_top_level_keys": list(self.unexpected_top_level_keys),
            "proposed_scope_type": self.proposed_scope_type,
            "proposed_scope_keys": list(self.proposed_scope_keys),
            "missing_scope_keys": list(self.missing_scope_keys),
            "unexpected_scope_keys": list(self.unexpected_scope_keys),
            "field_type_map": dict(self.field_type_map),
            "bounded_error_path": self.bounded_error_path,
            "bounded_error_category": self.bounded_error_category,
        }


@dataclass(frozen=True, slots=True)
class DevelopmentGroundingV1:
    relevant_evidence_handles: tuple[str, ...]
    task_interpretation: str
    unknowns: tuple[str, ...]

    def payload(self) -> dict[str, object]:
        return {
            "identity": DEVELOPMENT_GROUNDING_IDENTITY,
            "relevant_evidence_handles": list(self.relevant_evidence_handles),
            "task_interpretation": self.task_interpretation,
            "unknowns": list(self.unknowns),
        }


@dataclass(frozen=True, slots=True)
class DevelopmentProposalV1:
    proposed_scope: DevelopmentProposedScopeV1
    proposal_unknowns: tuple[str, ...]

    def payload(self) -> dict[str, object]:
        return {
            "identity": DEVELOPMENT_PROPOSAL_IDENTITY,
            "proposed_scope": _proposed_scope_payload(self.proposed_scope),
            "proposal_unknowns": list(self.proposal_unknowns),
        }


@dataclass(frozen=True, slots=True)
class DevelopmentReviewV1:
    risks: tuple[str, ...]
    required_tests: tuple[str, ...]
    non_goals: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    unresolved_unknowns: tuple[str, ...]
    confidence: float

    def payload(self) -> dict[str, object]:
        return {
            "identity": DEVELOPMENT_REVIEW_IDENTITY,
            "risks": list(self.risks),
            "required_tests": list(self.required_tests),
            "non_goals": list(self.non_goals),
            "acceptance_criteria": list(self.acceptance_criteria),
            "unresolved_unknowns": list(self.unresolved_unknowns),
            "confidence": self.confidence,
        }


@dataclass(frozen=True, slots=True)
class DevelopmentStagedSynthesisOutputV1:
    grounding: DevelopmentGroundingV1
    proposal: DevelopmentProposalV1
    review: DevelopmentReviewV1
    stage_elapsed_ms: tuple[tuple[str, int], ...] = ()
    review_mode: DevelopmentReviewModeV1 = DevelopmentReviewModeV1.MODEL_REVIEW


MAX_DETERMINISTIC_REVIEW_FILES = 8


def project_deterministic_low_risk_review(
    grounding: DevelopmentGroundingV1,
    proposal: DevelopmentProposalV1,
    context_map: DevelopmentContextMapV1,
    evidence: DevelopmentEvidencePackV1,
) -> DevelopmentReviewV1 | None:
    """Project Review evidence only for an exact, proven non-production scope."""
    scope = proposal.proposed_scope
    files = scope.proposed_modified_files
    if not (
        getattr(evidence, "repository_clean", False)
        and evidence.source_commit == context_map.source_commit
        and evidence.evidence_digest == context_map.evidence_digest
        and evidence.knowledge_manifest_digest == context_map.knowledge_manifest_digest
        and evidence.knowledge_context_digest == context_map.knowledge_context_digest
        and evidence.requested_role.value == context_map.requested_role
        and evidence.effective_model == context_map.effective_model
        and context_map.context_digest == build_context_digest(context_map)
        and len(context_map.owner_candidates) == 1
        and scope.proposed_owner == context_map.owner_candidates[0]
        and 0 < len(files) <= MAX_DETERMINISTIC_REVIEW_FILES
        and not scope.proposed_new_files
        and not scope.proposed_contract_changes
        and not scope.proposed_components
        and not grounding.unknowns
        and not proposal.proposal_unknowns
        and grounding.relevant_evidence_handles
        and context_map.existing_contracts
        and scope.proposed_behavior
        and scope.rationale
    ):
        return None
    normalized = tuple(path.replace("\\", "/") for path in files)
    if len(set(normalized)) != len(normalized) or any(
        not _safe_proposed_path(path) or path not in context_map.existing_files
        for path in normalized
    ):
        return None
    kinds = {_low_risk_file_kind(path) for path in normalized}
    if None in kinds or len(kinds) != 1:
        return None
    kind = kinds.pop()
    label = "documentation" if kind == "DOCUMENTATION" else "test"
    joined = ", ".join(normalized)
    return DevelopmentReviewV1(
        risks=(
            f"The bounded {label} change in {joined} could diverge from supplied current evidence.",
        ),
        required_tests=(
            f"Verify only the approved {label} paths change: {joined}.",
            "Verify production source remains unchanged and repository static checks pass.",
            "Verify referenced runtime contracts or symbols exist in the supplied bounded evidence.",
            "Verify the resulting diff remains inside the approved Proposal scope.",
        ),
        non_goals=(
            "No production runtime behavior or contract change.",
            "No file, shell, Git, Knowledge, trading, Risk, or execution authority.",
        ),
        acceptance_criteria=(
            f"The bounded {label} update changes only approved paths: {joined}.",
            "Production source remains unchanged and no unsupported authority is claimed.",
            "Applicable repository static checks pass and the diff matches the approved Proposal scope.",
        ),
        unresolved_unknowns=(
            "Runtime details outside the supplied bounded evidence remain unknown.",
        ),
        confidence=1.0,
    )


def _low_risk_file_kind(path: str) -> str | None:
    parsed = PurePosixPath(path)
    if parsed.suffix.casefold() == ".md" and parsed.parts[:1] == ("docs",):
        return "DOCUMENTATION"
    if parsed.suffix.casefold() == ".py" and parsed.parts[:1] == ("tests",):
        return "TEST"
    return None


@dataclass(frozen=True, slots=True)
class DevelopmentIntentSynthesisOutputV1:
    intent: DevelopmentTaskIntentKindV1
    relevant_evidence_handles: tuple[str, ...]
    answer: str
    unknowns: tuple[str, ...]
    inferences: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    plan_items: tuple[str, ...] = ()
    non_goals: tuple[str, ...] = ()
    elapsed_ms: int = 0


def parse_development_intent_synthesis(
    value: object,
    intent: DevelopmentTaskIntentKindV1,
    context_map: DevelopmentContextMapV1,
    repository_inventory_attachment: object | None = None,
) -> DevelopmentIntentSynthesisOutputV1:
    schemas = {
        DevelopmentTaskIntentKindV1.QUESTION: QUESTION_SCHEMA,
        DevelopmentTaskIntentKindV1.ANALYSIS: ANALYSIS_SCHEMA,
        DevelopmentTaskIntentKindV1.PLAN: PLAN_SCHEMA,
    }
    schema = schemas.get(intent)
    if schema is None or not isinstance(value, dict) or set(value) != set(schema["required"]):
        raise AIValidationError(
            "development intent synthesis is invalid", code="DEVELOPMENT_SCHEMA_INVALID"
        )

    def strings(key: str, *, required: bool = True) -> tuple[str, ...]:
        raw = value.get(key, [])
        if not isinstance(raw, list) or any(
            not isinstance(item, str) or not item.strip() for item in raw
        ):
            raise AIValidationError(
                "development intent synthesis is invalid", code="DEVELOPMENT_SCHEMA_INVALID"
            )
        if required and not raw:
            raise AIValidationError(
                "development intent synthesis is invalid", code="DEVELOPMENT_SCHEMA_INVALID"
            )
        if len(raw) > 16:
            raise AIValidationError(
                "development intent synthesis is invalid", code="DEVELOPMENT_SCHEMA_INVALID"
            )
        return tuple(raw)

    answer = value.get("answer")
    if not isinstance(answer, str) or not answer.strip() or len(answer) > 8000:
        raise AIValidationError(
            "development intent synthesis is invalid", code="DEVELOPMENT_SCHEMA_INVALID"
        )
    handles = strings("relevant_evidence_handles")
    allowed = set(public_grounding_evidence_handles(context_map, repository_inventory_attachment))
    if any(handle not in allowed for handle in handles):
        raise AIValidationError(
            "development intent evidence is invalid", code="UNSUPPORTED_KNOWLEDGE_CITATION"
        )
    return DevelopmentIntentSynthesisOutputV1(
        intent,
        tuple(dict.fromkeys(handles)),
        answer.strip(),
        strings("unknowns", required=False),
        strings("inferences", required=False),
        strings("risks", required=False),
        strings("plan_items", required=False),
        strings("non_goals", required=False),
    )


def parse_development_grounding(
    value: object,
    context_map: DevelopmentContextMapV1,
    repository_inventory_attachment: object | None = None,
) -> DevelopmentGroundingV1:
    if not isinstance(value, dict) or set(value) != set(GROUNDING_SCHEMA["required"]):
        raise DevelopmentStageError("GROUNDING", "GROUNDING_SCHEMA_INVALID")
    handles = value["relevant_evidence_handles"]
    interpretation = value["task_interpretation"]
    unknowns = value["unknowns"]
    if (
        not isinstance(handles, list)
        or not handles
        or any(not isinstance(item, str) for item in handles)
        or not isinstance(interpretation, str)
        or not interpretation.strip()
        or not isinstance(unknowns, list)
        or any(not isinstance(item, str) or not item.strip() for item in unknowns)
    ):
        raise DevelopmentStageError("GROUNDING", "GROUNDING_SCHEMA_INVALID")
    allowed = set(public_grounding_evidence_handles(context_map, repository_inventory_attachment))
    for index, item in enumerate(handles):
        if item not in allowed:
            raise DevelopmentStageError(
                "GROUNDING",
                "GROUNDING_UNSUPPORTED_EVIDENCE",
                grounding_diagnostics=GroundingParseDiagnosticsV1(
                    "GROUNDING_PARSE",
                    "GROUNDING_UNSUPPORTED_EVIDENCE",
                    f"$.relevant_evidence_handles[{index}]",
                    _unsupported_grounding_handle_category(item),
                ),
            )
    return DevelopmentGroundingV1(tuple(dict.fromkeys(handles)), interpretation, tuple(unknowns))


def public_grounding_evidence_handles(
    context_map: DevelopmentContextMapV1, repository_inventory_attachment: object | None = None
) -> tuple[str, ...]:
    """Return the exact request-local public handle namespace used by prompt and parser."""
    knowledge = tuple(item.handle for item in context_map.evidence_handles)
    if repository_inventory_attachment is None:
        return knowledge
    inventory = tuple(
        item.handle for item in getattr(repository_inventory_attachment, "evidence_groups", ())
    )
    return tuple(dict.fromkeys((*knowledge, *inventory)))


def _unsupported_grounding_handle_category(value: str) -> str:
    if value.startswith(("knowledge.", "knowledge-")) or any(
        marker in value for marker in ("/", "\\", "#", ".")
    ):
        return "INTERNAL_IDENTIFIER"
    if value.startswith(("E", "RI")) and value.removeprefix("RI").removeprefix("E").isdigit():
        return "UNKNOWN_HANDLE"
    return "MALFORMED_HANDLE"


def parse_development_proposal(
    value: object,
    context_map: DevelopmentContextMapV1,
    *,
    transport_form: str = "RAW_JSON",
    response_length: int | None = None,
) -> DevelopmentProposalV1:
    try:
        return _parse_development_proposal(value, context_map)
    except DevelopmentStageError as error:
        if error.proposal_diagnostics is None:
            error.proposal_diagnostics = _proposal_parse_diagnostics(
                value,
                reason_code=error.code,
                transport_form=transport_form,
                response_length=response_length,
                owner_candidates=context_map.owner_candidates,
            )
        raise


def _parse_development_proposal(
    value: object, context_map: DevelopmentContextMapV1
) -> DevelopmentProposalV1:
    if not isinstance(value, dict) or set(value) != set(PROPOSAL_SCHEMA["required"]):
        raise DevelopmentStageError("PROPOSAL", "PROPOSAL_SCHEMA_INVALID")
    unknowns = value["proposal_unknowns"]
    if not isinstance(unknowns, list) or any(
        not isinstance(item, str) or not item.strip() for item in unknowns
    ):
        raise DevelopmentStageError("PROPOSAL", "PROPOSAL_SCHEMA_INVALID")
    try:
        scope = _parse_proposed_scope(value["proposed_scope"], context_map)
    except DevelopmentCitationValidationError as error:
        raise DevelopmentStageError("PROPOSAL", "PROPOSAL_UNKNOWN_OWNER") from error
    except _ProposalAuthorityViolation as error:
        raise DevelopmentStageError("PROPOSAL", "PROPOSAL_AUTHORITY_VIOLATION") from error
    except AIValidationError as error:
        code = (
            "PROPOSAL_UNSAFE_PATH" if "path" in str(error).casefold() else "PROPOSAL_SCHEMA_INVALID"
        )
        raise DevelopmentStageError("PROPOSAL", code) from error
    if scope.proposed_owner is None and not unknowns:
        raise DevelopmentStageError("PROPOSAL", "PROPOSAL_SCHEMA_INVALID")
    return DevelopmentProposalV1(scope, tuple(unknowns))


def _proposal_parse_diagnostics(
    value: object,
    *,
    reason_code: str,
    transport_form: str,
    response_length: int | None,
    owner_candidates: tuple[str, ...],
) -> ProposalParseDiagnosticsV1:
    required_top = frozenset(PROPOSAL_SCHEMA["required"])
    required_scope = frozenset(PROPOSED_SCOPE_FIELDS)
    if not isinstance(value, dict):
        return ProposalParseDiagnosticsV1(
            "PROPOSAL_PARSE",
            reason_code,
            transport_form,
            response_length,
            type(value).__name__,
            (),
            (),
            (),
            None,
            (),
            (),
            (),
            (),
            "$",
            "NON_OBJECT_TOP_LEVEL",
        )
    keys = tuple(sorted(str(key) for key in value))
    missing = tuple(sorted(required_top.difference(value)))
    unexpected = tuple(sorted(set(value).difference(required_top)))
    scope = value.get("proposed_scope")
    scope_keys = tuple(sorted(str(key) for key in scope)) if isinstance(scope, dict) else ()
    missing_scope = (
        tuple(sorted(required_scope.difference(scope))) if isinstance(scope, dict) else ()
    )
    unexpected_scope = (
        tuple(sorted(set(scope).difference(required_scope))) if isinstance(scope, dict) else ()
    )
    types = {f"$.{key}": type(item).__name__ for key, item in value.items()}
    if isinstance(scope, dict):
        types.update(
            {f"$.proposed_scope.{key}": type(item).__name__ for key, item in scope.items()}
        )
    path, category = "$", "OTHER_SCHEMA_FAILURE"
    if missing:
        path, category = f"$.{missing[0]}", "MISSING_REQUIRED_FIELD"
    elif unexpected:
        path, category = f"$.{unexpected[0]}", "UNEXPECTED_FIELD"
    elif not isinstance(scope, dict):
        path, category = "$.proposed_scope", "INVALID_NULL" if scope is None else "WRONG_FIELD_TYPE"
    elif missing_scope:
        path, category = f"$.proposed_scope.{missing_scope[0]}", "MISSING_REQUIRED_FIELD"
    elif unexpected_scope:
        path, category = f"$.proposed_scope.{unexpected_scope[0]}", "UNEXPECTED_FIELD"
    elif value.get("proposal_unknowns") is None:
        path, category = "$.proposal_unknowns", "INVALID_NULL"
    elif not isinstance(value.get("proposal_unknowns"), list):
        path, category = "$.proposal_unknowns", "WRONG_FIELD_TYPE"
    else:
        invalid_unknown = next(
            (
                index
                for index, entry in enumerate(value["proposal_unknowns"])
                if not isinstance(entry, str) or not entry.strip()
            ),
            None,
        )
        if invalid_unknown is not None:
            path, category = f"$.proposal_unknowns[{invalid_unknown}]", "INVALID_ARRAY_ITEM"
        elif scope.get("proposed_owner") is not None and (
            not isinstance(scope["proposed_owner"], str)
            or scope["proposed_owner"] not in owner_candidates
        ):
            path, category = "$.proposed_scope.proposed_owner", "INVALID_OWNER"
        for field in PROPOSED_SCOPE_LIST_FIELDS:
            if category != "OTHER_SCHEMA_FAILURE":
                break
            item = scope[field]
            if item is None:
                path, category = f"$.proposed_scope.{field}", "INVALID_NULL"
                break
            if not isinstance(item, list):
                path, category = f"$.proposed_scope.{field}", "WRONG_FIELD_TYPE"
                break
            invalid = next(
                (
                    index
                    for index, entry in enumerate(item)
                    if not isinstance(entry, str) or not entry.strip()
                ),
                None,
            )
            if invalid is not None:
                path, category = f"$.proposed_scope.{field}[{invalid}]", "INVALID_ARRAY_ITEM"
                break
        if category == "OTHER_SCHEMA_FAILURE":
            semantic_failure = _proposal_semantic_failure(value, scope)
            if semantic_failure is not None:
                path, category = semantic_failure
    return ProposalParseDiagnosticsV1(
        "PROPOSAL_PARSE",
        reason_code,
        transport_form,
        response_length,
        "dict",
        keys,
        missing,
        unexpected,
        type(scope).__name__,
        scope_keys,
        missing_scope,
        unexpected_scope,
        tuple(sorted(types.items())),
        path,
        category,
    )


def parse_development_review(
    value: object,
    proposal: DevelopmentProposalV1,
    target_set: ReviewProposalTargetSetV1 | None = None,
) -> DevelopmentReviewV1:
    target_set = target_set or _review_target_set(proposal)
    if not isinstance(value, dict) or set(value) != set(REVIEW_SCHEMA["required"]):
        raise DevelopmentStageError("REVIEW", "REVIEW_SCHEMA_INVALID")
    lists = {}
    for key in (
        "risks",
        "required_tests",
        "non_goals",
        "acceptance_criteria",
        "unresolved_unknowns",
    ):
        raw = value[key]
        if not isinstance(raw, list) or any(
            not isinstance(item, str) or not item.strip() for item in raw
        ):
            raise DevelopmentStageError("REVIEW", "REVIEW_SCHEMA_INVALID")
        lists[key] = tuple(raw)
    confidence = value["confidence"]
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0 <= confidence <= 1
    ):
        raise DevelopmentStageError("REVIEW", "REVIEW_SCHEMA_INVALID")
    if failure := _review_specificity_failure(
        target_set, lists["required_tests"], require_action=True
    ):
        index, category, action, target, condition, condition_category, target_kind = failure
        raise DevelopmentStageError(
            "REVIEW",
            "REVIEW_TESTS_NOT_SPECIFIC",
            review_diagnostics=ReviewParseDiagnosticsV1(
                "REVIEW_PARSE",
                "REVIEW_TESTS_NOT_SPECIFIC",
                f"$.required_tests[{index}]",
                category,
                action,
                target,
                condition,
                condition_category,
                target_kind,
                len(target_set.targets),
            ),
        )
    if failure := _review_specificity_failure(
        target_set, lists["acceptance_criteria"], require_action=False
    ):
        index, category, action, target, condition, condition_category, target_kind = failure
        raise DevelopmentStageError(
            "REVIEW",
            "REVIEW_CRITERIA_NOT_SPECIFIC",
            review_diagnostics=ReviewParseDiagnosticsV1(
                "REVIEW_PARSE",
                "REVIEW_CRITERIA_NOT_SPECIFIC",
                f"$.acceptance_criteria[{index}]",
                category,
                action,
                target,
                condition,
                condition_category,
                target_kind,
                len(target_set.targets),
            ),
        )
    return DevelopmentReviewV1(**lists, confidence=float(confidence))


REVIEW_VERIFICATION_ACTIONS = (
    "assert",
    "check",
    "compare",
    "confirm",
    "ensure",
    "inspect",
    "test",
    "validate",
    "verify",
)
_VERIFICATION_ACTIONS = frozenset(REVIEW_VERIFICATION_ACTIONS)
_GENERIC_VERIFICATION_WORDS = frozenset({"consider", "look", "review"})


@dataclass(frozen=True, slots=True)
class ReviewObservableConditionV1:
    category: str
    markers: tuple[str, ...]
    subject_markers: tuple[str, ...] = ()


REVIEW_OBSERVABLE_CONDITIONS = (
    ReviewObservableConditionV1("PRESENCE", ("exists", "present")),
    ReviewObservableConditionV1("ABSENCE", ("absent", "missing", "removed")),
    ReviewObservableConditionV1(
        "EQUALITY", ("equal", "equals", "expected", "remain", "remains", "unchanged")
    ),
    ReviewObservableConditionV1("DIFFERENCE", ("changed", "differ", "differs", "unexpected")),
    ReviewObservableConditionV1("SUCCESS", ("pass", "passed", "passes", "succeed", "succeeds")),
    ReviewObservableConditionV1(
        "FAILURE", ("error", "errors", "fail", "fails", "failure", "reject", "rejects")
    ),
    ReviewObservableConditionV1(
        "REFERENCE_REMAINS",
        ("exists", "present", "remain", "remains"),
        ("consumer", "consumers", "reference", "references"),
    ),
    ReviewObservableConditionV1(
        "REFERENCE_REMOVED",
        ("absent", "missing", "removed"),
        ("consumer", "consumers", "reference", "references"),
    ),
)


def _review_specificity_failure(
    target_set: ReviewProposalTargetSetV1,
    values: tuple[str, ...],
    *,
    require_action: bool,
) -> tuple[int, str, bool, bool, bool, str | None, str | None] | None:
    allowed = {target.public_id: target for target in target_set.targets}
    for index, value in enumerate(values):
        tokens = _normalized_tokens(value)
        action = not require_action or bool(tokens.intersection(_VERIFICATION_ACTIONS))
        handles = _review_target_handles(value)
        recognized = handles[0] in allowed if len(handles) == 1 else False
        target_kind = allowed[handles[0]].kind.value if recognized else None
        condition_category = _observable_condition_category(tokens)
        condition = condition_category is not None
        if not target_set.targets:
            return index, "EMPTY_TARGET_SET", action, False, condition, condition_category, None
        if not action:
            category = (
                "GENERIC_VERIFICATION"
                if tokens.intersection(_GENERIC_VERIFICATION_WORDS)
                else "MISSING_VERIFICATION_ACTION"
            )
            return (
                index,
                category,
                action,
                recognized,
                condition,
                condition_category,
                target_kind,
            )
        if len(handles) > 1:
            return index, "MULTIPLE_TARGETS", action, False, condition, condition_category, None
        if not recognized:
            return (
                index,
                "TARGET_NOT_RECOGNIZED",
                action,
                False,
                condition,
                condition_category,
                None,
            )
        if not condition:
            return (
                index,
                "MISSING_VERIFICATION_CONDITION",
                action,
                recognized,
                condition,
                None,
                target_kind,
            )
    return None


def _observable_condition_category(tokens: frozenset[str]) -> str | None:
    for definition in reversed(REVIEW_OBSERVABLE_CONDITIONS):
        if definition.subject_markers and not tokens.intersection(definition.subject_markers):
            continue
        if tokens.intersection(definition.markers):
            return definition.category
    return None


def _review_target_handles(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"(?<![A-Za-z0-9_])RT[1-9][0-9]*(?![A-Za-z0-9_])", value))


def _normalized_tokens(value: str) -> frozenset[str]:
    return frozenset(
        token
        for item in value.casefold().replace("\\", "/").replace("/", " ").split()
        if (token := item.strip(".,:;()[]`'\""))
    )


def _proposed_scope_payload(value: DevelopmentProposedScopeV1) -> dict[str, object]:
    return {
        "proposed_owner": value.proposed_owner,
        "proposed_modified_files": list(value.proposed_modified_files),
        "proposed_new_files": list(value.proposed_new_files),
        "proposed_contract_changes": list(value.proposed_contract_changes),
        "proposed_components": list(value.proposed_components),
        "proposed_behavior": list(value.proposed_behavior),
        "rationale": list(value.rationale),
    }


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
) -> DevelopmentStagedSynthesisOutputV1 | DevelopmentIntentSynthesisOutputV1:
    """Run bounded fail-closed Development stages using one selected model."""
    now = datetime.now(tz=UTC)
    facts = knowledge_facts(context, now)
    allowed_fact_ids = tuple(sorted({fact.fact_id for fact in facts}))
    if allowed_fact_ids != evidence.allowed_fact_ids:
        raise AIValidationError(
            "development evidence allowlist is invalid", code="DEVELOPMENT_SCHEMA_INVALID"
        )
    context_map = build_development_context_map(context, evidence)
    provider = ai.router.provider_for(ai.config.profile)

    intent = getattr(request, "requested_intent", None)
    if intent in {
        DevelopmentTaskIntentKindV1.QUESTION,
        DevelopmentTaskIntentKindV1.ANALYSIS,
        DevelopmentTaskIntentKindV1.PLAN,
    }:
        schemas = {
            DevelopmentTaskIntentKindV1.QUESTION: QUESTION_SCHEMA,
            DevelopmentTaskIntentKindV1.ANALYSIS: ANALYSIS_SCHEMA,
            DevelopmentTaskIntentKindV1.PLAN: PLAN_SCHEMA,
        }
        try:
            result = provider.generate(
                ModelRequest(
                    uuid.uuid4().hex,
                    AITask.DEVELOPMENT_ANALYSIS_V1,
                    ai.config.profile,
                    _intent_instruction(
                        intent,
                        public_grounding_evidence_handles(
                            context_map, getattr(request, "repository_inventory_attachment", None)
                        ),
                    ),
                    f"chappie_development_{intent.value.casefold()}_synthesis/1.0.0",
                    (),
                    _with_repository_inventory(
                        {
                            "request": request.request,
                            "development_context_map": context_map.payload(),
                        },
                        request,
                    ),
                    schemas[intent],
                    ai.config.temperature,
                    ai.config.timeout_seconds,
                    now,
                    digest(
                        {
                            "intent": intent.value,
                            "request": request.request,
                            "context": context.context_digest,
                        }
                    ),
                )
            )
        except (AIProviderError, AIProviderTimeoutError, AIValidationError):
            raise
        except Exception as error:
            raise RuntimeError("development intent provider failed") from error
        parsed = parse_development_intent_synthesis(
            result.structured_output,
            intent,
            context_map,
            getattr(request, "repository_inventory_attachment", None),
        )
        return DevelopmentIntentSynthesisOutputV1(
            parsed.intent,
            parsed.relevant_evidence_handles,
            parsed.answer,
            parsed.unknowns,
            parsed.inferences,
            parsed.risks,
            parsed.plan_items,
            parsed.non_goals,
            result.latency_ms,
        )

    def call(
        stage: str,
        identity: str,
        instruction: str,
        schema: dict[str, Any],
        payload: dict[str, object],
    ):
        try:
            result = provider.generate(
                ModelRequest(
                    uuid.uuid4().hex,
                    AITask.DEVELOPMENT_ANALYSIS_V1,
                    ai.config.profile,
                    instruction,
                    identity,
                    (),
                    payload,
                    schema,
                    ai.config.temperature,
                    ai.config.timeout_seconds,
                    now,
                    digest(
                        {
                            "identity": identity,
                            "request": request.request,
                            "context": context.context_digest,
                        }
                    ),
                )
            )
            return result
        except (AIProviderError, AIProviderTimeoutError, AIValidationError):
            raise
        except Exception as error:
            raise DevelopmentStageError(stage, f"{stage}_PROVIDER_FAILED") from error

    grounding_result = call(
        "GROUNDING",
        DEVELOPMENT_GROUNDING_IDENTITY,
        _grounding_instruction(
            public_grounding_evidence_handles(
                context_map, getattr(request, "repository_inventory_attachment", None)
            )
        ),
        GROUNDING_SCHEMA,
        _with_repository_inventory(
            {"request": request.request, "development_context_map": context_map.payload()}, request
        ),
    )
    try:
        grounding = parse_development_grounding(
            grounding_result.structured_output,
            context_map,
            getattr(request, "repository_inventory_attachment", None),
        )
    except DevelopmentStageError as error:
        error.stage_elapsed_ms = (("GROUNDING_SYNTHESIS", grounding_result.latency_ms),)
        raise
    try:
        proposal_result = call(
            "PROPOSAL",
            DEVELOPMENT_PROPOSAL_IDENTITY,
            proposal_instruction(context_map.owner_candidates),
            proposal_provider_schema(context_map.owner_candidates),
            _with_repository_inventory(
                {
                    "request": request.request,
                    "development_context_map": context_map.payload(),
                    "grounding": grounding.payload(),
                },
                request,
            ),
        )
    except AIProviderTimeoutError as error:
        raise DevelopmentStageError(
            "PROPOSAL",
            "AI_PROVIDER_TIMEOUT",
            stage_elapsed_ms=(("GROUNDING_SYNTHESIS", grounding_result.latency_ms),),
            completed_grounding=grounding,
        ) from error
    except AIProviderError as error:
        raise DevelopmentStageError(
            "PROPOSAL",
            "AI_UNAVAILABLE",
            stage_elapsed_ms=(("GROUNDING_SYNTHESIS", grounding_result.latency_ms),),
            completed_grounding=grounding,
        ) from error
    except DevelopmentStageError as error:
        error.completed_grounding = grounding
        error.stage_elapsed_ms = (("GROUNDING_SYNTHESIS", grounding_result.latency_ms),)
        raise
    try:
        proposal = parse_development_proposal(
            proposal_result.structured_output,
            context_map,
            transport_form="RAW_JSON",
            response_length=_provider_response_length(proposal_result),
        )
    except DevelopmentStageError as error:
        error.stage_elapsed_ms = (
            ("GROUNDING_SYNTHESIS", grounding_result.latency_ms),
            ("PROPOSAL_SYNTHESIS", proposal_result.latency_ms),
        )
        error.completed_grounding = grounding
        raise
    deterministic_review = (
        project_deterministic_low_risk_review(grounding, proposal, context_map, evidence)
        if intent is DevelopmentTaskIntentKindV1.IMPLEMENTATION
        else None
    )
    if deterministic_review is not None:
        return DevelopmentStagedSynthesisOutputV1(
            grounding,
            proposal,
            deterministic_review,
            (
                ("GROUNDING_SYNTHESIS", grounding_result.latency_ms),
                ("PROPOSAL_SYNTHESIS", proposal_result.latency_ms),
                ("REVIEW_DETERMINISTIC", 0),
            ),
            DevelopmentReviewModeV1.DETERMINISTIC_LOW_RISK,
        )
    try:
        review_targets = _review_target_set(proposal)
    except DevelopmentStageError as error:
        error.completed_grounding = grounding
        error.completed_proposal = proposal
        error.stage_elapsed_ms = (
            ("GROUNDING_SYNTHESIS", grounding_result.latency_ms),
            ("PROPOSAL_SYNTHESIS", proposal_result.latency_ms),
        )
        raise
    try:
        review_result = call(
            "REVIEW",
            DEVELOPMENT_REVIEW_IDENTITY,
            _review_instruction(proposal, review_targets),
            REVIEW_SCHEMA,
            _with_repository_inventory(
                {
                    "request": request.request,
                    "grounding": grounding.payload(),
                    "proposal": proposal.payload(),
                    "review_proposal_targets": review_targets.payload(),
                    "authority_constraints": list(context_map.authority_constraints),
                },
                request,
            ),
        )
    except AIProviderTimeoutError as error:
        raise DevelopmentStageError(
            "REVIEW",
            "AI_PROVIDER_TIMEOUT",
            stage_elapsed_ms=(
                ("GROUNDING_SYNTHESIS", grounding_result.latency_ms),
                ("PROPOSAL_SYNTHESIS", proposal_result.latency_ms),
            ),
            completed_grounding=grounding,
            completed_proposal=proposal,
        ) from error
    except AIProviderError as error:
        raise DevelopmentStageError(
            "REVIEW",
            "AI_UNAVAILABLE",
            stage_elapsed_ms=(
                ("GROUNDING_SYNTHESIS", grounding_result.latency_ms),
                ("PROPOSAL_SYNTHESIS", proposal_result.latency_ms),
            ),
            completed_grounding=grounding,
            completed_proposal=proposal,
        ) from error
    except DevelopmentStageError as error:
        error.completed_grounding = grounding
        error.completed_proposal = proposal
        error.stage_elapsed_ms = (
            ("GROUNDING_SYNTHESIS", grounding_result.latency_ms),
            ("PROPOSAL_SYNTHESIS", proposal_result.latency_ms),
        )
        raise
    try:
        review = parse_development_review(review_result.structured_output, proposal, review_targets)
    except DevelopmentStageError as error:
        error.stage_elapsed_ms = (
            ("GROUNDING_SYNTHESIS", grounding_result.latency_ms),
            ("PROPOSAL_SYNTHESIS", proposal_result.latency_ms),
            ("REVIEW_SYNTHESIS", review_result.latency_ms),
        )
        error.completed_grounding = grounding
        error.completed_proposal = proposal
        raise
    return DevelopmentStagedSynthesisOutputV1(
        grounding,
        proposal,
        review,
        (
            ("GROUNDING_SYNTHESIS", grounding_result.latency_ms),
            ("PROPOSAL_SYNTHESIS", proposal_result.latency_ms),
            ("REVIEW_SYNTHESIS", review_result.latency_ms),
        ),
        DevelopmentReviewModeV1.MODEL_REVIEW,
    )


def _intent_instruction(intent: DevelopmentTaskIntentKindV1, handles: tuple[str, ...]) -> str:
    route = {
        DevelopmentTaskIntentKindV1.QUESTION: "Answer the question directly and ground it in supplied evidence.",
        DevelopmentTaskIntentKindV1.ANALYSIS: "Provide bounded analysis, separating inference, risk, and unknowns.",
        DevelopmentTaskIntentKindV1.PLAN: "Provide an advisory ordered plan; every item is PROPOSED, not authorized implementation scope.",
    }[intent]
    return (
        "Return only the exact JSON schema. "
        + route
        + " Cite only exact relevant_evidence_handles from: "
        + ", ".join(handles)
        + ". Preserve unknowns. Do not claim file, shell, Git, Knowledge, trading, Risk, or execution authority."
    )


def _with_repository_inventory(payload: dict[str, object], request: object) -> dict[str, object]:
    attachment = getattr(request, "repository_inventory_attachment", None)
    if attachment is None:
        return payload
    return {**payload, "repository_inventory_attachment": attachment.payload()}


def _grounding_instruction(public_handles: tuple[str, ...]) -> str:
    handles = ", ".join(public_handles)
    return (
        "Return only JSON. Select exact supplied public handles that establish CURRENT state, "
        "give one short task interpretation, and list UNKNOWNs. E-handles are canonical Knowledge "
        "evidence. RI-handles are verified grouped RepositoryInventory structural evidence. "
        f"For this request, relevant_evidence_handles may contain only: {handles}. "
        "Never invent or normalize a handle, and never propose files, contracts, or authority."
    )


def _review_instruction(
    proposal: DevelopmentProposalV1,
    target_set: ReviewProposalTargetSetV1 | None = None,
) -> str:
    target_set = target_set or _review_target_set(proposal)
    rendered_targets = "; ".join(
        f"{target.public_id} | {target.kind.value} | {target.display_summary}"
        for target in target_set.targets
    )
    conditions = "; ".join(
        f"{item.category}: {', '.join((*item.subject_markers, *item.markers))}"
        for item in REVIEW_OBSERVABLE_CONDITIONS
    )
    return (
        "Return only JSON. Critique the supplied proposal with bounded proposal-specific risks, "
        "required tests or verification, non-goals, acceptance criteria, unresolved unknowns, and "
        f"confidence. Allowed Proposal targets: {rendered_targets}. Every required_tests and "
        "acceptance_criteria item must contain exactly ONE allowed RT handle. Do not invent, "
        "normalize, alias, or paraphrase RT handles. Each required_tests item must use one exact action from: "
        f"{', '.join(REVIEW_VERIFICATION_ACTIONS)}; `ensure` is valid only with both a concrete "
        "target and observable result or failure condition. Use one observable condition from: "
        f"{conditions}. Reference/consumer categories require both a reference or consumer subject "
        "and a listed observable state. "
        "Generic testing or repository-wide verification is invalid. Do not redesign or claim "
        "authority."
    )


def _review_target_set(proposal: DevelopmentProposalV1) -> ReviewProposalTargetSetV1:
    try:
        return build_review_proposal_target_set(
            proposal, proposal_identity=DEVELOPMENT_PROPOSAL_IDENTITY
        )
    except ReviewProposalTargetProjectionError as error:
        raise DevelopmentStageError(
            "REVIEW", error.code, review_target_diagnostics=error.diagnostics
        ) from error


def _provider_response_length(result: object) -> int | None:
    for key, value in getattr(result, "provider_metadata", ()):
        if key == "response_length" and isinstance(value, str) and value.isdecimal():
            return int(value)
    try:
        return len(canonical_json(result.structured_output))
    except (TypeError, ValueError):
        return None


def development_allowed_fact_ids(context: KnowledgeContextV1) -> tuple[str, ...]:
    """Return the exact deterministic statement-fact namespace available to synthesis."""
    return tuple(sorted({fact.fact_id for fact in knowledge_facts(context, datetime.now(tz=UTC))}))


def _parse_proposed_scope(
    value: object, context_map: DevelopmentContextMapV1
) -> DevelopmentProposedScopeV1:
    required = set(PROPOSED_SCOPE_FIELDS)
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
    for key in PROPOSED_SCOPE_LIST_FIELDS:
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
    if _proposal_authority_location(lists) is not None:
        raise _ProposalAuthorityViolation("development proposal claims forbidden authority")
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


def _proposal_authority_location(values: dict[str, tuple[str, ...]]) -> tuple[str, int] | None:
    for field in ("proposed_behavior", "rationale"):
        for index, item in enumerate(values[field]):
            if _forbidden_authority(item):
                return field, index
    return None


def _proposal_semantic_failure(
    value: dict[object, object], scope: dict[object, object]
) -> tuple[str, str] | None:
    if scope.get("proposed_owner") is None and not value["proposal_unknowns"]:
        return "$.proposal_unknowns", "REQUIRED_UNKNOWN_MISSING"
    values = {field: tuple(scope[field]) for field in ("proposed_behavior", "rationale")}
    location = _proposal_authority_location(values)
    if location is None:
        return None
    field, index = location
    return f"$.proposed_scope.{field}[{index}]", "AUTHORITY_VIOLATION"


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
