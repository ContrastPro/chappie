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


class DevelopmentAnalysisStateV1(StrEnum):
    OK = "OK"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    KNOWLEDGE_UNAVAILABLE = "KNOWLEDGE_UNAVAILABLE"
    REPOSITORY_UNAVAILABLE = "REPOSITORY_UNAVAILABLE"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    TIMEOUT = "TIMEOUT"
    ANALYSIS_INVALID = "ANALYSIS_INVALID"


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
    reason_code: str | None = None

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
            "reason_code": self.reason_code,
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


def evidence_digest(payload: dict[str, object]) -> str:
    return digest({"identity": DEVELOPMENT_ANALYSIS_IDENTITY, **payload})


def _relative_path(value: str) -> bool:
    path = PurePosixPath(value.replace("\\", "/"))
    return bool(value) and not path.is_absolute() and ".." not in path.parts and ":" not in value


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
