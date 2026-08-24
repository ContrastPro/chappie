"""Versioned, bounded contracts for read-only development analysis."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath

from app.ai.model_policy import DevelopmentModelRoleV1
from app.ai.schemas import digest
from app.development_analysis.context import (
    DevelopmentContextMapV1,
    development_context_map_from_payload,
)
from app.repository_inventory import (
    POLICY_DIGEST as REPOSITORY_INVENTORY_POLICY_DIGEST,
)
from app.repository_inventory import (
    REPOSITORY_INVENTORY_IDENTITY,
    REPOSITORY_INVENTORY_POLICY_IDENTITY,
)

DEVELOPMENT_ANALYSIS_IDENTITY = "chappie_development_analysis/2.0.0"
MAX_DEVELOPMENT_REQUEST_CHARACTERS = 12_000
MAX_ANALYSIS_ITEMS = 16
MAX_LIFECYCLE_ELAPSED_MS = 120_000
MAX_CITATION_IDENTIFIER_CHARACTERS = 160
_SAFE_CITATION_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._:/-]{0,159}")


class DevelopmentAnalysisStateV1(StrEnum):
    OK = "OK"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    KNOWLEDGE_UNAVAILABLE = "KNOWLEDGE_UNAVAILABLE"
    REPOSITORY_UNAVAILABLE = "REPOSITORY_UNAVAILABLE"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    TIMEOUT = "TIMEOUT"
    ANALYSIS_INVALID = "ANALYSIS_INVALID"


class DevelopmentAnalysisReadinessV1(StrEnum):
    """Task-readiness is distinct from structural evidence validation."""

    READY_FOR_TASK = "READY_FOR_TASK"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    NEEDS_REVIEW = "NEEDS_REVIEW"


DEVELOPMENT_DIFF_REVIEW_IDENTITY = "chappie_development_diff_review/1.0.0"


class DevelopmentDiffReviewCheckV1(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class DevelopmentDiffReviewStatusV1(StrEnum):
    MATCHES_APPROVED_SCOPE = "MATCHES_APPROVED_SCOPE"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    REJECTED = "REJECTED"


class DevelopmentDiffReviewStateV1(StrEnum):
    COMPLETE = "COMPLETE"
    VALIDATION_ERROR = "VALIDATION_ERROR"


@dataclass(frozen=True, slots=True)
class DevelopmentDiffReviewCheckResultV1:
    status: DevelopmentDiffReviewCheckV1
    reason_code: str


@dataclass(frozen=True, slots=True)
class DevelopmentDiffChangeEvidenceV1:
    path: str
    change_kind: str
    before_digest: str
    after_digest: str
    changed_symbols: tuple[str, ...]
    line_count: int
    authority_markers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DevelopmentDiffTestEvidenceV1:
    identifier: str
    status: DevelopmentDiffReviewCheckV1
    evidence_digest: str


@dataclass(frozen=True, slots=True)
class DevelopmentDiffContractEvidenceV1:
    identifier: str
    status: DevelopmentDiffReviewCheckV1
    evidence_digest: str


@dataclass(frozen=True, slots=True)
class DevelopmentDiffAcceptanceEvidenceV1:
    identifier: str
    status: DevelopmentDiffReviewCheckV1
    evidence_digest: str


@dataclass(frozen=True, slots=True)
class DevelopmentDiffReviewRequestV1:
    analysis: DevelopmentAnalysisV1
    handoff: object
    target_identity: str
    changed_files: tuple[str, ...]
    changes: tuple[DevelopmentDiffChangeEvidenceV1, ...]
    tests: tuple[DevelopmentDiffTestEvidenceV1, ...]
    knowledge_identity: str
    knowledge_manifest_digest: str
    knowledge_context_digest: str
    observed_owner: str | None = None
    contract_changes: tuple[str, ...] = ()
    contract_evidence: tuple[DevelopmentDiffContractEvidenceV1, ...] = ()
    acceptance_evidence: tuple[DevelopmentDiffAcceptanceEvidenceV1, ...] = ()


@dataclass(frozen=True, slots=True)
class DevelopmentDiffReviewV1:
    identity: str
    state: DevelopmentDiffReviewStateV1
    validation_status: str
    review_status: DevelopmentDiffReviewStatusV1
    reason_code: str | None
    source_analysis_identity: str | None
    source_commit: str | None
    source_analysis_evidence_digest: str | None
    source_handoff_identity: str | None
    source_handoff_task_id: str | None
    source_handoff_evidence_digest: str | None
    source_context_digest: str | None
    knowledge_identity: str | None
    knowledge_manifest_digest: str | None
    knowledge_context_digest: str | None
    target_identity: str
    reviewed_files: tuple[str, ...]
    expected_files: tuple[str, ...]
    unexpected_files: tuple[str, ...]
    unresolved_unknowns: tuple[str, ...]
    owner_check: DevelopmentDiffReviewCheckResultV1
    contract_check: DevelopmentDiffReviewCheckResultV1
    tests_check: DevelopmentDiffReviewCheckResultV1
    acceptance_check: DevelopmentDiffReviewCheckResultV1
    authority_check: DevelopmentDiffReviewCheckResultV1
    risks: tuple[str, ...]
    human_approval_status: str = "HUMAN_APPROVAL_REQUIRED"


class DevelopmentLifecycleStageV1(StrEnum):
    REQUEST_ACCEPTED = "REQUEST_ACCEPTED"
    ROLE_RESOLUTION = "ROLE_RESOLUTION"
    EVIDENCE_BUILD = "EVIDENCE_BUILD"
    SYNTHESIS = "SYNTHESIS"
    GROUNDING_SYNTHESIS = "GROUNDING_SYNTHESIS"
    GROUNDING_PARSE = "GROUNDING_PARSE"
    PROPOSAL_SYNTHESIS = "PROPOSAL_SYNTHESIS"
    PROPOSAL_PARSE = "PROPOSAL_PARSE"
    REVIEW_SYNTHESIS = "REVIEW_SYNTHESIS"
    REVIEW_PARSE = "REVIEW_PARSE"
    STRUCTURAL_VALIDATION = "STRUCTURAL_VALIDATION"
    READINESS_EVALUATION = "READINESS_EVALUATION"
    COMPLETE = "COMPLETE"


class DevelopmentLifecycleStatusV1(StrEnum):
    STARTED = "STARTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class DevelopmentCitationKindV1(StrEnum):
    FACT_ID = "FACT_ID"
    OWNER = "OWNER"
    CONTRACT = "CONTRACT"
    FILE = "FILE"


class DevelopmentCitationRejectionV1(StrEnum):
    NOT_IN_ALLOWLIST = "NOT_IN_ALLOWLIST"
    MALFORMED = "MALFORMED"
    UNSUPPORTED_BY_CITED_FACT = "UNSUPPORTED_BY_CITED_FACT"


@dataclass(frozen=True, slots=True)
class DevelopmentCitationFailureV1:
    """Bounded metadata for an evidence citation rejected before analysis exists."""

    citation_kind: DevelopmentCitationKindV1
    rejected_identifier: str
    rejection: DevelopmentCitationRejectionV1

    def __post_init__(self) -> None:
        if not _SAFE_CITATION_IDENTIFIER.fullmatch(self.rejected_identifier):
            raise ValueError("development citation identifier is unsafe")


@dataclass(frozen=True, slots=True)
class DevelopmentProposedScopeV1:
    """Architecture-validated future scope; no item asserts existing repository state."""

    proposed_owner: str | None
    proposed_modified_files: tuple[str, ...]
    proposed_new_files: tuple[str, ...]
    proposed_contract_changes: tuple[str, ...]
    proposed_components: tuple[str, ...]
    proposed_behavior: tuple[str, ...]
    rationale: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DevelopmentLifecycleEventV1:
    stage: DevelopmentLifecycleStageV1
    status: DevelopmentLifecycleStatusV1
    elapsed_ms: int
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if not 0 <= self.elapsed_ms <= MAX_LIFECYCLE_ELAPSED_MS:
            raise ValueError("development lifecycle elapsed time is invalid")
        if self.status != DevelopmentLifecycleStatusV1.FAILED and self.reason_code is not None:
            raise ValueError("development lifecycle reason is invalid")


@dataclass(frozen=True, slots=True)
class DevelopmentLifecycleV1:
    request_id: str
    operation: str
    requested_role: DevelopmentModelRoleV1
    effective_model: str | None
    events: tuple[DevelopmentLifecycleEventV1, ...]

    def __post_init__(self) -> None:
        if (
            len(self.request_id) != 32
            or not self.request_id.isascii()
            or self.operation != "DEVELOPMENT_ANALYSIS"
        ):
            raise ValueError("development lifecycle is invalid")


@dataclass(frozen=True, slots=True)
class DevelopmentAnalysisRequestV1:
    request: str
    requested_role: DevelopmentModelRoleV1
    workspace_context: object | None = None
    repository_inventory_attachment: object | None = None
    requested_intent: DevelopmentTaskIntentKindV1 | None = None

    def __post_init__(self) -> None:
        if not self.request.strip() or len(self.request) > MAX_DEVELOPMENT_REQUEST_CHARACTERS:
            raise ValueError("development request is invalid")
        if self.requested_intent is not None and not isinstance(
            self.requested_intent, DevelopmentTaskIntentKindV1
        ):
            raise ValueError("development requested intent is invalid")


DEVELOPMENT_TASK_INTENT_IDENTITY = "chappie_development_task_intent/1.0.0"


class DevelopmentTaskIntentKindV1(StrEnum):
    QUESTION = "QUESTION"
    ANALYSIS = "ANALYSIS"
    PLAN = "PLAN"
    IMPLEMENTATION = "IMPLEMENTATION"
    UNKNOWN = "UNKNOWN"


class DevelopmentTaskIntentSourceV1(StrEnum):
    EXPLICIT = "EXPLICIT"
    DETERMINISTIC_RULE = "DETERMINISTIC_RULE"


@dataclass(frozen=True, slots=True)
class DevelopmentTaskIntentV1:
    intent: DevelopmentTaskIntentKindV1
    source: DevelopmentTaskIntentSourceV1
    rule_id: str
    requesting_evidence_digest: str
    requesting_context_digest: str
    identity: str = DEVELOPMENT_TASK_INTENT_IDENTITY

    def __post_init__(self) -> None:
        if self.identity != DEVELOPMENT_TASK_INTENT_IDENTITY:
            raise ValueError("development task intent identity is invalid")
        if not self.rule_id or len(self.rule_id) > 64:
            raise ValueError("development task intent rule is invalid")
        if len(self.requesting_evidence_digest) != 64 or len(self.requesting_context_digest) != 64:
            raise ValueError("development task intent binding is invalid")

    def payload(self) -> dict[str, str]:
        return {
            "identity": self.identity,
            "intent": self.intent.value,
            "source": self.source.value,
            "rule_id": self.rule_id,
            "requesting_evidence_digest": self.requesting_evidence_digest,
            "requesting_context_digest": self.requesting_context_digest,
        }


@dataclass(frozen=True, slots=True)
class DevelopmentEvidencePackV1:
    source_commit: str
    repository_clean: bool
    knowledge_identity: str
    knowledge_manifest_digest: str
    knowledge_context_digest: str
    workspace_context_digest: str | None
    requested_role: DevelopmentModelRoleV1
    requested_model: str
    effective_model: str
    profile_identity: str
    capability_identity: str
    record_ids: tuple[str, ...]
    record_digests: tuple[str, ...]
    evidence_digest: str
    allowed_fact_ids: tuple[str, ...] = ()
    repository_inventory_identity: str | None = None
    repository_inventory_digest: str | None = None
    repository_inventory_policy_identity: str | None = None
    repository_inventory_policy_digest: str | None = None
    repository_inventory_attachment_digest: str | None = None
    original_evidence_digest: str | None = None
    original_context_digest: str | None = None

    def __post_init__(self) -> None:
        if self.allowed_fact_ids and (
            self.allowed_fact_ids != tuple(sorted(set(self.allowed_fact_ids)))
            or any(not item.startswith("knowledge.") for item in self.allowed_fact_ids)
        ):
            raise ValueError("development evidence allowlist is invalid")
        inventory_values = (
            self.repository_inventory_identity,
            self.repository_inventory_digest,
            self.repository_inventory_policy_identity,
            self.repository_inventory_policy_digest,
            self.repository_inventory_attachment_digest,
            self.original_evidence_digest,
            self.original_context_digest,
        )
        if any(item is not None for item in inventory_values):
            if any(item is None for item in inventory_values):
                raise ValueError("development inventory evidence binding is incomplete")
            if any(
                len(item) != 64
                for item in (
                    self.repository_inventory_digest,
                    self.repository_inventory_policy_digest,
                    self.repository_inventory_attachment_digest,
                    self.original_evidence_digest,
                    self.original_context_digest,
                )
            ):
                raise ValueError("development inventory evidence digest is invalid")
            if (
                self.repository_inventory_identity != REPOSITORY_INVENTORY_IDENTITY
                or self.repository_inventory_policy_identity != REPOSITORY_INVENTORY_POLICY_IDENTITY
                or self.repository_inventory_policy_digest != REPOSITORY_INVENTORY_POLICY_DIGEST
            ):
                raise ValueError("development inventory evidence contract is unsupported")


@dataclass(frozen=True, slots=True)
class DevelopmentAnalysisV1:
    state: DevelopmentAnalysisStateV1
    request: str
    evidence: DevelopmentEvidencePackV1 | None = None
    facts: tuple[str, ...] = ()
    inferences: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    architecture_owners: tuple[str, ...] = ()
    affected_contracts: tuple[str, ...] = ()
    likely_files: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    required_tests: tuple[str, ...] = ()
    non_goals: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    confidence: float | None = None
    validation_status: str = "UNAVAILABLE"
    readiness: DevelopmentAnalysisReadinessV1 | None = None
    reason_code: str | None = None
    lifecycle: DevelopmentLifecycleV1 | None = None
    citation_failure: DevelopmentCitationFailureV1 | None = None
    context_map: DevelopmentContextMapV1 | None = None
    current_evidence_handles: tuple[str, ...] = ()
    proposed_scope: DevelopmentProposedScopeV1 | None = None
    staged_results: dict[str, object] | None = None
    task_intent: DevelopmentTaskIntentV1 | None = None
    answer: str | None = None
    plan_items: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.answer is not None and (not self.answer.strip() or len(self.answer) > 8_000):
            raise ValueError("development answer is invalid")
        if len(self.plan_items) > MAX_ANALYSIS_ITEMS or any(
            not item.strip() or len(item) > 1_000 for item in self.plan_items
        ):
            raise ValueError("development plan items are invalid")
        if self.task_intent is not None:
            if self.evidence is None or self.context_map is None:
                raise ValueError("development task intent binding is incomplete")
            if (
                self.task_intent.requesting_evidence_digest != self.evidence.evidence_digest
                or self.task_intent.requesting_context_digest != self.context_map.context_digest
            ):
                raise ValueError("development task intent binding is invalid")

    def payload(self) -> dict[str, object]:
        return {
            "identity": DEVELOPMENT_ANALYSIS_IDENTITY,
            "state": self.state.value,
            "request": self.request,
            "evidence": _evidence_payload(self.evidence),
            "facts": list(self.facts),
            "inferences": list(self.inferences),
            "unknowns": list(self.unknowns),
            "architecture_owners": list(self.architecture_owners),
            "affected_contracts": list(self.affected_contracts),
            "likely_files": list(self.likely_files),
            "risks": list(self.risks),
            "required_tests": list(self.required_tests),
            "non_goals": list(self.non_goals),
            "acceptance_criteria": list(self.acceptance_criteria),
            "confidence": self.confidence,
            "validation_status": self.validation_status,
            "readiness": self.readiness.value if self.readiness else None,
            "reason_code": self.reason_code,
            "lifecycle": _lifecycle_payload(self.lifecycle),
            "citation_failure": _citation_failure_payload(self.citation_failure),
            "context_map": self.context_map.payload() if self.context_map else None,
            "current_evidence_handles": list(self.current_evidence_handles),
            "proposed_scope": _proposed_scope_payload(self.proposed_scope),
            "staged_results": self.staged_results,
            "task_intent": self.task_intent.payload() if self.task_intent else None,
            "answer": self.answer,
            "plan_items": list(self.plan_items),
        }


def validate_development_analysis(value: DevelopmentAnalysisV1) -> DevelopmentAnalysisV1:
    """Reject unsafe model-derived shape; the evidence pack stays authoritative."""
    if value.state != DevelopmentAnalysisStateV1.OK:
        return value
    if value.evidence is None or not value.evidence.evidence_digest or value.context_map is None:
        raise ValueError("development analysis evidence is required")
    if not value.facts or not value.inferences or not value.required_tests or not value.non_goals:
        raise ValueError("development analysis sections are required")
    if not value.acceptance_criteria:
        raise ValueError("development analysis acceptance criteria are required")
    if not value.current_evidence_handles or value.proposed_scope is None:
        raise ValueError("development analysis reasoning scope is required")
    if len(value.likely_files) > MAX_ANALYSIS_ITEMS or any(
        not _relative_path(item) for item in value.likely_files
    ):
        raise ValueError("development analysis file paths are unsafe")
    forbidden = ("edit file", "write file", "shell", "git commit", "execute order")
    if any(
        token in item.casefold()
        for item in (*value.inferences, *value.risks)
        for token in forbidden
    ):
        raise ValueError("development analysis claims forbidden authority")
    return value


def assess_development_analysis_readiness(
    value: DevelopmentAnalysisV1,
) -> DevelopmentAnalysisReadinessV1:
    """Fail closed when an otherwise valid analysis is too generic for task preparation."""
    return development_readiness(value)[0]


def development_readiness(
    value: DevelopmentAnalysisV1,
) -> tuple[DevelopmentAnalysisReadinessV1, str | None]:
    """Return a bounded backend-owned readiness state and public reason code."""
    if value.state != DevelopmentAnalysisStateV1.OK or value.validation_status != "VALID":
        return DevelopmentAnalysisReadinessV1.INSUFFICIENT_EVIDENCE, "INSUFFICIENT_EVIDENCE"
    if value.evidence is None or not value.facts or not _task_specific_evidence(value):
        return DevelopmentAnalysisReadinessV1.INSUFFICIENT_EVIDENCE, "NO_TASK_SPECIFIC_FACTS"
    if not _concrete_owner(value):
        return DevelopmentAnalysisReadinessV1.INSUFFICIENT_EVIDENCE, "NO_GROUNDED_OWNER"
    if not value.affected_contracts and not _grounded_unknown(value, "contract"):
        return DevelopmentAnalysisReadinessV1.INSUFFICIENT_EVIDENCE, "NO_GROUNDED_CONTRACT_SCOPE"
    if not value.likely_files and not _grounded_unknown(value, "file"):
        return DevelopmentAnalysisReadinessV1.INSUFFICIENT_EVIDENCE, "NO_GROUNDED_FILE_SCOPE"
    if not _task_specific(value.request, value.risks):
        return DevelopmentAnalysisReadinessV1.INSUFFICIENT_EVIDENCE, "TASK_RISKS_NOT_SPECIFIC"
    if not _task_specific(value.request, value.required_tests):
        return DevelopmentAnalysisReadinessV1.INSUFFICIENT_EVIDENCE, "TASK_TESTS_NOT_SPECIFIC"
    if not _task_specific(value.request, value.acceptance_criteria):
        return (
            DevelopmentAnalysisReadinessV1.INSUFFICIENT_EVIDENCE,
            "TASK_ACCEPTANCE_CRITERIA_NOT_SPECIFIC",
        )
    if not value.unknowns or any(
        item.casefold().strip().startswith("no unknowns identified") for item in value.unknowns
    ):
        return DevelopmentAnalysisReadinessV1.NEEDS_REVIEW, "INSUFFICIENT_EVIDENCE"
    scope = value.proposed_scope
    if scope is None or not (
        scope.proposed_modified_files
        or scope.proposed_new_files
        or scope.proposed_contract_changes
        or scope.proposed_components
    ):
        return DevelopmentAnalysisReadinessV1.INSUFFICIENT_EVIDENCE, "NO_PROPOSED_SCOPE"
    return DevelopmentAnalysisReadinessV1.READY_FOR_TASK, None


def evidence_digest(payload: dict[str, object]) -> str:
    return digest({"identity": DEVELOPMENT_ANALYSIS_IDENTITY, **payload})


def development_analysis_from_payload(value: object) -> DevelopmentAnalysisV1:
    """Strictly reconstruct only this package's own bounded response envelope."""
    if not isinstance(value, dict) or value.get("identity") != DEVELOPMENT_ANALYSIS_IDENTITY:
        raise ValueError("development analysis payload is invalid")
    raw = value.get("evidence")
    if not isinstance(raw, dict):
        raise TypeError("development analysis evidence is invalid")
    payload = {
        key: raw.get(key)
        for key in (
            "source_commit",
            "repository_clean",
            "knowledge_identity",
            "knowledge_manifest_digest",
            "knowledge_context_digest",
            "workspace_context_digest",
            "requested_role",
            "requested_model",
            "effective_model",
            "profile_identity",
            "capability_identity",
            "record_ids",
            "record_digests",
            "allowed_fact_ids",
        )
    }
    for key in (
        "repository_inventory_identity",
        "repository_inventory_digest",
        "repository_inventory_policy_identity",
        "repository_inventory_policy_digest",
        "repository_inventory_attachment_digest",
        "original_evidence_digest",
        "original_context_digest",
    ):
        if key in raw:
            payload[key] = raw[key]
    if raw.get("evidence_digest") != evidence_digest(payload):
        raise ValueError("development analysis evidence digest is invalid")
    evidence = DevelopmentEvidencePackV1(
        source_commit=str(payload["source_commit"]),
        repository_clean=payload["repository_clean"] is True,
        knowledge_identity=str(payload["knowledge_identity"]),
        knowledge_manifest_digest=str(payload["knowledge_manifest_digest"]),
        knowledge_context_digest=str(payload["knowledge_context_digest"]),
        workspace_context_digest=payload["workspace_context_digest"],
        requested_role=DevelopmentModelRoleV1(payload["requested_role"]),
        requested_model=str(payload["requested_model"]),
        effective_model=str(payload["effective_model"]),
        profile_identity=str(payload["profile_identity"]),
        capability_identity=str(payload["capability_identity"]),
        record_ids=tuple(_strings(payload["record_ids"])),
        record_digests=tuple(_strings(payload["record_digests"])),
        allowed_fact_ids=tuple(_strings(payload["allowed_fact_ids"])),
        evidence_digest=str(raw["evidence_digest"]),
        repository_inventory_identity=_optional_string(
            payload.get("repository_inventory_identity")
        ),
        repository_inventory_digest=_optional_string(payload.get("repository_inventory_digest")),
        repository_inventory_policy_identity=_optional_string(
            payload.get("repository_inventory_policy_identity")
        ),
        repository_inventory_policy_digest=_optional_string(
            payload.get("repository_inventory_policy_digest")
        ),
        repository_inventory_attachment_digest=_optional_string(
            payload.get("repository_inventory_attachment_digest")
        ),
        original_evidence_digest=_optional_string(payload.get("original_evidence_digest")),
        original_context_digest=_optional_string(payload.get("original_context_digest")),
    )
    lifecycle = _lifecycle_from_payload(value.get("lifecycle"))
    context_map = development_context_map_from_payload(value.get("context_map"))
    proposed_scope = _proposed_scope_from_payload(value.get("proposed_scope"))
    return DevelopmentAnalysisV1(
        DevelopmentAnalysisStateV1(value["state"]),
        str(value["request"]),
        evidence,
        facts=tuple(_strings(value.get("facts"))),
        inferences=tuple(_strings(value.get("inferences"))),
        unknowns=tuple(_strings(value.get("unknowns"))),
        architecture_owners=tuple(_strings(value.get("architecture_owners"))),
        affected_contracts=tuple(_strings(value.get("affected_contracts"))),
        likely_files=tuple(_strings(value.get("likely_files"))),
        risks=tuple(_strings(value.get("risks"))),
        required_tests=tuple(_strings(value.get("required_tests"))),
        non_goals=tuple(_strings(value.get("non_goals"))),
        acceptance_criteria=tuple(_strings(value.get("acceptance_criteria"))),
        confidence=value.get("confidence"),
        validation_status=str(value.get("validation_status", "")),
        readiness=(
            DevelopmentAnalysisReadinessV1(value["readiness"])
            if value.get("readiness") is not None
            else None
        ),
        reason_code=(str(value["reason_code"]) if value.get("reason_code") is not None else None),
        lifecycle=lifecycle,
        citation_failure=_citation_failure_from_payload(value.get("citation_failure")),
        context_map=context_map,
        current_evidence_handles=tuple(_strings(value.get("current_evidence_handles"))),
        proposed_scope=proposed_scope,
        task_intent=_task_intent_from_payload(value.get("task_intent")),
        answer=_optional_string(value.get("answer")),
        plan_items=tuple(_strings(value.get("plan_items", []))),
    )


def _task_intent_from_payload(value: object) -> DevelopmentTaskIntentV1 | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "identity",
        "intent",
        "source",
        "rule_id",
        "requesting_evidence_digest",
        "requesting_context_digest",
    }:
        raise ValueError("development task intent payload is invalid")
    return DevelopmentTaskIntentV1(
        DevelopmentTaskIntentKindV1(value["intent"]),
        DevelopmentTaskIntentSourceV1(value["source"]),
        str(value["rule_id"]),
        str(value["requesting_evidence_digest"]),
        str(value["requesting_context_digest"]),
        str(value["identity"]),
    )


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("development analysis list is invalid")
    return tuple(value)


def _optional_string(value: object) -> str | None:
    return str(value) if value is not None else None


def _lifecycle_from_payload(value: object) -> DevelopmentLifecycleV1 | None:
    if value is None:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("events"), list):
        raise TypeError("development lifecycle is invalid")
    try:
        events = tuple(
            DevelopmentLifecycleEventV1(
                DevelopmentLifecycleStageV1(item["stage"]),
                DevelopmentLifecycleStatusV1(item["status"]),
                item["elapsed_ms"],
                item.get("reason_code"),
            )
            for item in value["events"]
            if isinstance(item, dict)
        )
        if len(events) != len(value["events"]):
            raise ValueError
        return DevelopmentLifecycleV1(
            str(value["request_id"]),
            str(value["operation"]),
            DevelopmentModelRoleV1(value["requested_role"]),
            value.get("effective_model"),
            events,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("development lifecycle is invalid") from error


def _relative_path(value: str) -> bool:
    path = PurePosixPath(value.replace("\\", "/"))
    return bool(value) and not path.is_absolute() and ".." not in path.parts and ":" not in value


def _task_specific_evidence(value: DevelopmentAnalysisV1) -> bool:
    request_tokens = {token.casefold() for token in value.request.replace("/", " ").split()}
    evidence_tokens = {
        token.casefold().strip(".,:;()[]")
        for item in value.facts
        for token in item.replace("/", " ").split()
    }
    return bool(request_tokens.intersection(evidence_tokens)) or any(
        "app.development_analysis" in item or "codextask" in item.casefold() for item in value.facts
    )


def _concrete_owner(value: DevelopmentAnalysisV1) -> bool:
    return any("app.development_analysis owns" in item.casefold() for item in value.facts)


def _grounded_unknown(value: DevelopmentAnalysisV1, subject: str) -> bool:
    return any(
        subject in item.casefold() and "cannot" in item.casefold() for item in value.unknowns
    )


def _task_specific(request: str, values: tuple[str, ...]) -> bool:
    request_tokens = {
        token.casefold().strip(".,:;()[]") for token in request.split() if len(token) > 2
    }
    return bool(
        request_tokens.intersection(
            {token.casefold().strip(".,:;()[]") for item in values for token in item.split()}
        )
    )


def _evidence_payload(value: DevelopmentEvidencePackV1 | None) -> dict[str, object] | None:
    if value is None:
        return None
    payload = {
        "source_commit": value.source_commit,
        "repository_clean": value.repository_clean,
        "knowledge_identity": value.knowledge_identity,
        "knowledge_manifest_digest": value.knowledge_manifest_digest,
        "knowledge_context_digest": value.knowledge_context_digest,
        "workspace_context_digest": value.workspace_context_digest,
        "requested_role": value.requested_role.value,
        "requested_model": value.requested_model,
        "effective_model": value.effective_model,
        "profile_identity": value.profile_identity,
        "capability_identity": value.capability_identity,
        "record_ids": list(value.record_ids),
        "record_digests": list(value.record_digests),
        "allowed_fact_ids": list(value.allowed_fact_ids),
        "evidence_digest": value.evidence_digest,
    }
    optional = {
        "repository_inventory_identity": value.repository_inventory_identity,
        "repository_inventory_digest": value.repository_inventory_digest,
        "repository_inventory_policy_identity": value.repository_inventory_policy_identity,
        "repository_inventory_policy_digest": value.repository_inventory_policy_digest,
        "repository_inventory_attachment_digest": value.repository_inventory_attachment_digest,
        "original_evidence_digest": value.original_evidence_digest,
        "original_context_digest": value.original_context_digest,
    }
    payload.update({key: item for key, item in optional.items() if item is not None})
    return payload


def _lifecycle_payload(value: DevelopmentLifecycleV1 | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "request_id": value.request_id,
        "operation": value.operation,
        "requested_role": value.requested_role.value,
        "effective_model": value.effective_model,
        "events": [
            {
                "stage": event.stage.value,
                "status": event.status.value,
                "elapsed_ms": event.elapsed_ms,
                "reason_code": event.reason_code,
            }
            for event in value.events
        ],
    }


def _citation_failure_payload(
    value: DevelopmentCitationFailureV1 | None,
) -> dict[str, str] | None:
    if value is None:
        return None
    return {
        "citation_kind": value.citation_kind.value,
        "rejected_identifier": value.rejected_identifier,
        "rejection": value.rejection.value,
    }


def _citation_failure_from_payload(value: object) -> DevelopmentCitationFailureV1 | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError("development citation failure is invalid")
    try:
        return DevelopmentCitationFailureV1(
            DevelopmentCitationKindV1(value["citation_kind"]),
            str(value["rejected_identifier"]),
            DevelopmentCitationRejectionV1(value["rejection"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("development citation failure is invalid") from error


def _proposed_scope_payload(value: DevelopmentProposedScopeV1 | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "proposed_owner": value.proposed_owner,
        "proposed_modified_files": list(value.proposed_modified_files),
        "proposed_new_files": list(value.proposed_new_files),
        "proposed_contract_changes": list(value.proposed_contract_changes),
        "proposed_components": list(value.proposed_components),
        "proposed_behavior": list(value.proposed_behavior),
        "rationale": list(value.rationale),
    }


def _proposed_scope_from_payload(value: object) -> DevelopmentProposedScopeV1:
    if not isinstance(value, dict):
        raise TypeError("development proposed scope is invalid")
    try:
        owner = value["proposed_owner"]
        if owner is not None and not isinstance(owner, str):
            raise ValueError
        return DevelopmentProposedScopeV1(
            owner,
            tuple(_strings(value["proposed_modified_files"])),
            tuple(_strings(value["proposed_new_files"])),
            tuple(_strings(value["proposed_contract_changes"])),
            tuple(_strings(value["proposed_components"])),
            tuple(_strings(value["proposed_behavior"])),
            tuple(_strings(value["rationale"])),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("development proposed scope is invalid") from error
