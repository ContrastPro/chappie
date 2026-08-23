"""Versioned, bounded contracts for read-only development analysis."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath

from app.ai.model_policy import DevelopmentModelRoleV1
from app.ai.schemas import digest

DEVELOPMENT_ANALYSIS_IDENTITY = "chappie_development_analysis/1.0.0"
MAX_DEVELOPMENT_REQUEST_CHARACTERS = 12_000
MAX_ANALYSIS_ITEMS = 16
MAX_LIFECYCLE_ELAPSED_MS = 120_000


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


class DevelopmentLifecycleStageV1(StrEnum):
    REQUEST_ACCEPTED = "REQUEST_ACCEPTED"
    ROLE_RESOLUTION = "ROLE_RESOLUTION"
    EVIDENCE_BUILD = "EVIDENCE_BUILD"
    SYNTHESIS = "SYNTHESIS"
    STRUCTURAL_VALIDATION = "STRUCTURAL_VALIDATION"
    READINESS_EVALUATION = "READINESS_EVALUATION"
    COMPLETE = "COMPLETE"


class DevelopmentLifecycleStatusV1(StrEnum):
    STARTED = "STARTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


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

    def __post_init__(self) -> None:
        if not self.request.strip() or len(self.request) > MAX_DEVELOPMENT_REQUEST_CHARACTERS:
            raise ValueError("development request is invalid")


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
        }


def validate_development_analysis(value: DevelopmentAnalysisV1) -> DevelopmentAnalysisV1:
    """Reject unsafe model-derived shape; the evidence pack stays authoritative."""
    if value.state != DevelopmentAnalysisStateV1.OK:
        return value
    if value.evidence is None or not value.evidence.evidence_digest:
        raise ValueError("development analysis evidence is required")
    if not value.facts or not value.inferences or not value.required_tests or not value.non_goals:
        raise ValueError("development analysis sections are required")
    if not value.acceptance_criteria:
        raise ValueError("development analysis acceptance criteria are required")
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
        )
    }
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
        evidence_digest=str(raw["evidence_digest"]),
    )
    lifecycle = _lifecycle_from_payload(value.get("lifecycle"))
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
    )


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("development analysis list is invalid")
    return tuple(value)


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
    return {
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
        "evidence_digest": value.evidence_digest,
    }


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
