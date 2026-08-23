"""Fail-closed advisory task preparation from validated development analysis."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.ai.schemas import digest
from app.development_analysis.models import (
    DEVELOPMENT_ANALYSIS_IDENTITY,
    DevelopmentAnalysisReadinessV1,
    DevelopmentAnalysisStateV1,
    DevelopmentAnalysisV1,
    validate_development_analysis,
)

CODEX_TASK_IDENTITY = "chappie_codex_task/1.0.0"
KNOWN_OWNERS = frozenset(
    {"ARCHITECTURE", "MODULE_RESPONSIBILITY", "CONTRACT", "AUTHORITY_BOUNDARY"}
)
FORBIDDEN_PATH_PREFIXES = ("data/", "logs/", ".venv/", ".env")
FORBIDDEN_AUTHORITY = (
    "edit file",
    "write file",
    "git commit",
    "shell",
    "knowledge mutation",
    "trade",
    "risk",
    "execution",
)


class CodexTaskStateV1(StrEnum):
    ADVISORY = "ADVISORY"
    VALIDATION_ERROR = "VALIDATION_ERROR"


@dataclass(frozen=True, slots=True)
class CodexTaskPreparationRequestV1:
    analysis: DevelopmentAnalysisV1


@dataclass(frozen=True, slots=True)
class CodexTaskV1:
    task_id: str
    state: CodexTaskStateV1
    source_analysis_identity: str
    source_commit: str | None
    evidence_digest: str | None
    requested_role: str | None
    problem: str
    facts: tuple[str, ...] = ()
    inferences: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    architecture_owners: tuple[str, ...] = ()
    affected_contracts: tuple[str, ...] = ()
    allowed_files: tuple[str, ...] = ()
    forbidden_scope: tuple[str, ...] = ()
    implementation_objective: str = ""
    implementation_constraints: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    required_tests: tuple[str, ...] = ()
    non_goals: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    approval_status: str = "HUMAN_APPROVAL_REQUIRED"
    validation_status: str = "VALID"
    reason_code: str | None = None

    def projection(self) -> str:
        """Stable copy/paste text derived only from the validated DTO."""
        return "\n".join(
            (
                "Task",
                self.problem,
                "\nFacts",
                *_bullets(self.facts),
                "\nScope",
                *_bullets(self.implementation_constraints),
                "\nAllowed files",
                *_bullets(self.allowed_files or ("None proposed by validated analysis.",)),
                "\nForbidden scope",
                *_bullets(self.forbidden_scope),
                "\nRequired tests",
                *_bullets(self.required_tests),
                "\nAcceptance criteria",
                *_bullets(self.acceptance_criteria),
                "\nUnknowns",
                *_bullets(self.unknowns or ("None recorded.",)),
                "\nSafety constraints",
                "- Advisory only. Human approval is required before Codex receives this task.",
            )
        )

    def payload(self) -> dict[str, object]:
        return {
            "identity": CODEX_TASK_IDENTITY,
            "task_id": self.task_id,
            "state": self.state.value,
            "source_analysis_identity": self.source_analysis_identity,
            "source_commit": self.source_commit,
            "evidence_digest": self.evidence_digest,
            "requested_role": self.requested_role,
            "problem": self.problem,
            "facts": list(self.facts),
            "inferences": list(self.inferences),
            "unknowns": list(self.unknowns),
            "architecture_owners": list(self.architecture_owners),
            "affected_contracts": list(self.affected_contracts),
            "allowed_files": list(self.allowed_files),
            "forbidden_scope": list(self.forbidden_scope),
            "implementation_objective": self.implementation_objective,
            "implementation_constraints": list(self.implementation_constraints),
            "risks": list(self.risks),
            "required_tests": list(self.required_tests),
            "non_goals": list(self.non_goals),
            "acceptance_criteria": list(self.acceptance_criteria),
            "approval_status": self.approval_status,
            "validation_status": self.validation_status,
            "reason_code": self.reason_code,
            "projection": self.projection(),
        }


def prepare_codex_task(request: CodexTaskPreparationRequestV1) -> CodexTaskV1:
    analysis = request.analysis
    try:
        if analysis.state != DevelopmentAnalysisStateV1.OK or analysis.evidence is None:
            raise ValueError("SOURCE_ANALYSIS_INVALID")
        evidence = analysis.evidence
        if not evidence.source_commit or not evidence.repository_clean:
            raise ValueError("SOURCE_EVIDENCE_MISMATCH")
        if not set(analysis.architecture_owners).issubset(KNOWN_OWNERS):
            raise ValueError("OWNER_INVALID")
        if not analysis.required_tests:
            raise ValueError("MISSING_TESTS")
        if not analysis.acceptance_criteria:
            raise ValueError("MISSING_ACCEPTANCE_CRITERIA")
        if any(_unsafe_path(path) for path in analysis.likely_files):
            raise ValueError("UNSAFE_FILE_PATH")
        if any(
            token in text.casefold()
            for text in (*analysis.inferences, *analysis.risks, *analysis.acceptance_criteria)
            for token in FORBIDDEN_AUTHORITY
        ):
            raise ValueError("AUTHORITY_VIOLATION")
        if any(
            "model_ab" in item.casefold() or "benchmark" in item.casefold()
            for item in analysis.inferences
        ):
            raise ValueError("UNRELATED_BENCHMARK_ASSUMPTION")
        validate_development_analysis(analysis)
        if analysis.readiness != DevelopmentAnalysisReadinessV1.READY_FOR_TASK:
            raise ValueError("SOURCE_ANALYSIS_NOT_READY")
    except ValueError as error:
        return _invalid(analysis, str(error))
    task_id = digest(
        {
            "identity": CODEX_TASK_IDENTITY,
            "evidence": evidence.evidence_digest,
            "request": analysis.request,
        }
    )
    return CodexTaskV1(
        task_id,
        CodexTaskStateV1.ADVISORY,
        DEVELOPMENT_ANALYSIS_IDENTITY,
        evidence.source_commit,
        evidence.evidence_digest,
        evidence.requested_role.value,
        analysis.request,
        analysis.facts,
        analysis.inferences,
        analysis.unknowns,
        analysis.architecture_owners,
        analysis.affected_contracts,
        analysis.likely_files,
        (
            "No Codex invocation, file edits, shell, Git, Knowledge, trading, Risk, or execution authority.",
        ),
        analysis.request,
        ("Use only the validated evidence and keep unknowns explicit.",),
        analysis.risks,
        analysis.required_tests,
        analysis.non_goals,
        analysis.acceptance_criteria,
    )


def _invalid(analysis: DevelopmentAnalysisV1, reason: str) -> CodexTaskV1:
    return CodexTaskV1(
        "",
        CodexTaskStateV1.VALIDATION_ERROR,
        DEVELOPMENT_ANALYSIS_IDENTITY,
        analysis.evidence.source_commit if analysis.evidence else None,
        analysis.evidence.evidence_digest if analysis.evidence else None,
        analysis.evidence.requested_role.value if analysis.evidence else None,
        analysis.request,
        validation_status="INVALID",
        reason_code=reason,
    )


def _unsafe_path(value: str) -> bool:
    normalized = value.replace("\\", "/")
    return (
        not value
        or normalized.startswith(("/", "../"))
        or ":" in value
        or ".." in normalized.split("/")
        or normalized.startswith(FORBIDDEN_PATH_PREFIXES)
    )


def _bullets(items: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(f"- {item}" for item in items)
