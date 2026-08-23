"""Fail-closed advisory task preparation from validated development analysis."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from re import escape, search

from app.ai.schemas import digest
from app.development_analysis.context import DevelopmentContextMapV1, DevelopmentEvidenceHandleV1
from app.development_analysis.models import (
    DEVELOPMENT_ANALYSIS_IDENTITY,
    DEVELOPMENT_DIFF_REVIEW_IDENTITY,
    DevelopmentAnalysisReadinessV1,
    DevelopmentAnalysisStateV1,
    DevelopmentAnalysisV1,
    DevelopmentDiffAcceptanceEvidenceV1,
    DevelopmentDiffChangeEvidenceV1,
    DevelopmentDiffContractEvidenceV1,
    DevelopmentDiffReviewCheckResultV1,
    DevelopmentDiffReviewCheckV1,
    DevelopmentDiffReviewRequestV1,
    DevelopmentDiffReviewStateV1,
    DevelopmentDiffReviewStatusV1,
    DevelopmentDiffReviewV1,
    DevelopmentDiffTestEvidenceV1,
    DevelopmentEvidencePackV1,
    DevelopmentProposedScopeV1,
    validate_development_analysis,
)


def review_development_diff(request: DevelopmentDiffReviewRequestV1) -> DevelopmentDiffReviewV1:
    """Deterministically review bounded supplied evidence; never read or mutate a workspace."""
    invalid_reason = _validate_diff_review_inputs(request)
    if invalid_reason is not None:
        return _invalid_diff_review_result(request, invalid_reason)
    analysis = request.analysis
    handoff = request.handoff
    source_invalid = (
        analysis.state is not DevelopmentAnalysisStateV1.OK
        or analysis.evidence is None
        or not analysis.evidence.repository_clean
        or not request.target_identity.strip()
    )
    evidence = analysis.evidence
    context_map = analysis.context_map
    source_invalid = source_invalid or context_map is None
    if evidence is not None:
        source_invalid = source_invalid or any(
            actual is None or actual != expected
            for actual, expected in (
                (getattr(handoff, "state", None), CodexTaskStateV1.ADVISORY),
                (getattr(handoff, "source_analysis_identity", None), DEVELOPMENT_ANALYSIS_IDENTITY),
                (getattr(handoff, "source_commit", None), evidence.source_commit),
                (getattr(handoff, "evidence_digest", None), evidence.evidence_digest),
                (request.knowledge_identity, evidence.knowledge_identity),
                (request.knowledge_manifest_digest, evidence.knowledge_manifest_digest),
                (request.knowledge_context_digest, evidence.knowledge_context_digest),
            )
        )
        if context_map is not None:
            handoff_context = getattr(handoff, "context_digest", None)
            source_invalid = source_invalid or handoff_context != context_map.context_digest
        source_invalid = source_invalid or not getattr(handoff, "task_id", None)
    expected = tuple(
        sorted(
            set(
                getattr(handoff, "proposed_modified_files", ())
                + getattr(handoff, "proposed_new_files", ())
            )
        )
    )
    reviewed = tuple(sorted(set(request.changed_files)))
    unexpected = tuple(sorted(set(reviewed).difference(expected)))
    expected_owner = analysis.proposed_scope.proposed_owner if analysis.proposed_scope else None
    observed_owner = request.observed_owner or getattr(handoff, "proposed_owner", None)
    owner = _review_owner_check(expected_owner, observed_owner)
    contract = _review_declared_evidence(
        tuple(getattr(handoff, "proposed_contract_changes", ())),
        request.contract_changes,
        request.contract_evidence,
        "CONTRACT_SCOPE_MATCH",
        "CONTRACT_DRIFT",
        "CONTRACT_EVIDENCE_INCOMPLETE",
    )
    if unexpected:
        contract = DevelopmentDiffReviewCheckResultV1(
            DevelopmentDiffReviewCheckV1.FAIL, "CONTRACT_DRIFT"
        )
    tests = _review_declared_evidence(
        tuple(getattr(handoff, "required_tests", ())),
        (),
        request.tests,
        "TESTS_PASS",
        "TEST_FAILED",
        "TEST_EVIDENCE_INCOMPLETE",
    )
    authority = _review_authority_check(reviewed, request.changes)
    acceptance = _review_declared_evidence(
        tuple(getattr(handoff, "acceptance_criteria", ())),
        (),
        request.acceptance_evidence,
        "ACCEPTANCE_SATISFIED",
        "ACCEPTANCE_UNSATISFIED",
        "ACCEPTANCE_EVIDENCE_INCOMPLETE",
    )
    checks = (owner, contract, tests, acceptance, authority)
    rejected = source_invalid or any(
        item.status is DevelopmentDiffReviewCheckV1.FAIL for item in checks
    )
    needs_review = bool(analysis.unknowns) or any(
        item.status is DevelopmentDiffReviewCheckV1.UNKNOWN for item in checks
    )
    status = (
        DevelopmentDiffReviewStatusV1.REJECTED
        if rejected
        else (
            DevelopmentDiffReviewStatusV1.NEEDS_REVIEW
            if needs_review
            else DevelopmentDiffReviewStatusV1.MATCHES_APPROVED_SCOPE
        )
    )
    return DevelopmentDiffReviewV1(
        DEVELOPMENT_DIFF_REVIEW_IDENTITY,
        (
            DevelopmentDiffReviewStateV1.VALIDATION_ERROR
            if source_invalid
            else DevelopmentDiffReviewStateV1.COMPLETE
        ),
        "INVALID" if source_invalid else "VALID",
        status,
        _review_reason(source_invalid, rejected, analysis.unknowns, checks),
        getattr(handoff, "source_analysis_identity", None),
        evidence.source_commit if evidence else None,
        evidence.evidence_digest if evidence else None,
        CODEX_TASK_IDENTITY,
        getattr(handoff, "task_id", None),
        getattr(handoff, "evidence_digest", None),
        getattr(handoff, "context_digest", None),
        request.knowledge_identity,
        request.knowledge_manifest_digest,
        request.knowledge_context_digest,
        request.target_identity,
        reviewed,
        expected,
        unexpected,
        tuple(analysis.unknowns),
        owner,
        contract,
        tests,
        acceptance,
        authority,
        tuple(analysis.risks),
    )


def _review_owner_check(
    expected_owner: str | None, observed_owner: str | None
) -> DevelopmentDiffReviewCheckResultV1:
    if expected_owner is None or observed_owner is None:
        return DevelopmentDiffReviewCheckResultV1(
            DevelopmentDiffReviewCheckV1.UNKNOWN, "OWNER_EVIDENCE_MISSING"
        )
    return DevelopmentDiffReviewCheckResultV1(
        DevelopmentDiffReviewCheckV1.PASS
        if observed_owner == expected_owner
        else DevelopmentDiffReviewCheckV1.FAIL,
        "OWNER_MATCH" if observed_owner == expected_owner else "OWNER_CONFLICT",
    )


def _review_declared_evidence(
    expected: tuple[str, ...],
    observed: tuple[str, ...],
    evidence: tuple[
        DevelopmentDiffAcceptanceEvidenceV1
        | DevelopmentDiffContractEvidenceV1
        | DevelopmentDiffTestEvidenceV1,
        ...,
    ],
    pass_reason: str,
    fail_reason: str,
    incomplete_reason: str,
) -> DevelopmentDiffReviewCheckResultV1:
    expected_set = set(expected)
    observed_set = set(observed)
    evidence_by_identifier = {item.identifier: item.status for item in evidence}
    if observed_set.difference(expected_set) or set(evidence_by_identifier).difference(
        expected_set
    ):
        return DevelopmentDiffReviewCheckResultV1(DevelopmentDiffReviewCheckV1.FAIL, fail_reason)
    if any(
        status is DevelopmentDiffReviewCheckV1.FAIL for status in evidence_by_identifier.values()
    ):
        return DevelopmentDiffReviewCheckResultV1(DevelopmentDiffReviewCheckV1.FAIL, fail_reason)
    if not expected_set:
        return DevelopmentDiffReviewCheckResultV1(DevelopmentDiffReviewCheckV1.PASS, pass_reason)
    if expected_set.difference(observed_set | set(evidence_by_identifier)):
        return DevelopmentDiffReviewCheckResultV1(
            DevelopmentDiffReviewCheckV1.UNKNOWN, incomplete_reason
        )
    if any(
        evidence_by_identifier.get(item) is not DevelopmentDiffReviewCheckV1.PASS
        for item in expected_set
    ):
        return DevelopmentDiffReviewCheckResultV1(
            DevelopmentDiffReviewCheckV1.UNKNOWN, incomplete_reason
        )
    return DevelopmentDiffReviewCheckResultV1(DevelopmentDiffReviewCheckV1.PASS, pass_reason)


def _review_authority_check(
    reviewed: tuple[str, ...], changes: tuple[DevelopmentDiffChangeEvidenceV1, ...]
) -> DevelopmentDiffReviewCheckResultV1:
    evidence_paths = {change.path for change in changes}
    if set(reviewed).difference(evidence_paths):
        return DevelopmentDiffReviewCheckResultV1(
            DevelopmentDiffReviewCheckV1.UNKNOWN, "AUTHORITY_EVIDENCE_INCOMPLETE"
        )
    if any(change.authority_markers for change in changes):
        return DevelopmentDiffReviewCheckResultV1(
            DevelopmentDiffReviewCheckV1.FAIL, "AUTHORITY_VIOLATION"
        )
    return DevelopmentDiffReviewCheckResultV1(DevelopmentDiffReviewCheckV1.PASS, "AUTHORITY_SAFE")


def _review_reason(
    source_invalid: bool,
    rejected: bool,
    source_unknowns: tuple[str, ...],
    checks: tuple[DevelopmentDiffReviewCheckResultV1, ...],
) -> str | None:
    if source_invalid:
        return "SOURCE_BINDING_MISMATCH"
    if rejected:
        return "SCOPE_OR_EVIDENCE_REJECTED"
    if any(item.status is DevelopmentDiffReviewCheckV1.UNKNOWN for item in checks):
        return "REVIEW_EVIDENCE_INCOMPLETE"
    if source_unknowns:
        return "UNRESOLVED_SOURCE_UNKNOWN"
    return None


def _validate_diff_review_inputs(request: object) -> str | None:
    if not isinstance(request, DevelopmentDiffReviewRequestV1):
        return "REVIEW_INPUT_INVALID"
    if not _bounded_text(request.target_identity, MAX_DIFF_REVIEW_IDENTIFIER):
        return "TARGET_IDENTITY_INVALID"
    if not isinstance(request.analysis, DevelopmentAnalysisV1):
        return "SOURCE_BINDING_MISMATCH"
    if not _valid_analysis_for_diff_review(request.analysis):
        return "REVIEW_INPUT_INVALID"
    if not isinstance(request.handoff, CodexTaskV1):
        return "SOURCE_BINDING_MISMATCH"
    if not _valid_diff_paths(request.changed_files):
        return "REVIEW_INPUT_INVALID"
    if not _valid_change_evidence(request.changes):
        return "REVIEW_INPUT_INVALID"
    if not _valid_evidence_items(request.tests, DevelopmentDiffTestEvidenceV1):
        return "REVIEW_INPUT_INVALID"
    if not _valid_evidence_items(request.contract_evidence, DevelopmentDiffContractEvidenceV1):
        return "REVIEW_INPUT_INVALID"
    if not _valid_evidence_items(request.acceptance_evidence, DevelopmentDiffAcceptanceEvidenceV1):
        return "REVIEW_INPUT_INVALID"
    if not _valid_bounded_texts(request.contract_changes):
        return "REVIEW_INPUT_INVALID"
    if not _valid_sha256(request.knowledge_manifest_digest) or not _valid_sha256(
        request.knowledge_context_digest
    ):
        return "REVIEW_INPUT_INVALID"
    if not _bounded_text(request.knowledge_identity, MAX_DIFF_REVIEW_IDENTIFIER):
        return "REVIEW_INPUT_INVALID"
    if not _valid_handoff(request.handoff):
        return "SOURCE_BINDING_MISMATCH"
    return None


def _valid_analysis_for_diff_review(analysis: DevelopmentAnalysisV1) -> bool:
    if not isinstance(analysis.state, DevelopmentAnalysisStateV1):
        return False
    if not isinstance(analysis.evidence, DevelopmentEvidencePackV1):
        return False
    if not isinstance(analysis.context_map, DevelopmentContextMapV1):
        return False
    if not isinstance(analysis.proposed_scope, DevelopmentProposedScopeV1):
        return False
    evidence = analysis.evidence
    context_map = analysis.context_map
    scope = analysis.proposed_scope
    return (
        isinstance(evidence.repository_clean, bool)
        and _valid_commit(evidence.source_commit)
        and _bounded_text(evidence.knowledge_identity, MAX_DIFF_REVIEW_IDENTIFIER)
        and _valid_sha256(evidence.knowledge_manifest_digest)
        and _valid_sha256(evidence.knowledge_context_digest)
        and _valid_sha256(evidence.evidence_digest)
        and _valid_commit(context_map.source_commit)
        and _bounded_text(context_map.knowledge_identity, MAX_DIFF_REVIEW_IDENTIFIER)
        and _valid_sha256(context_map.knowledge_manifest_digest)
        and _valid_sha256(context_map.knowledge_context_digest)
        and _valid_sha256(context_map.context_digest)
        and _valid_optional_owner(scope.proposed_owner)
        and _valid_diff_paths(scope.proposed_modified_files)
        and _valid_diff_paths(scope.proposed_new_files)
        and _valid_bounded_texts(scope.proposed_contract_changes)
        and _valid_bounded_texts(analysis.unknowns)
        and _valid_bounded_texts(analysis.risks)
        and _valid_bounded_texts(analysis.required_tests)
        and _valid_bounded_texts(analysis.acceptance_criteria)
    )


def _invalid_diff_review_result(request: object, reason_code: str) -> DevelopmentDiffReviewV1:
    analysis = getattr(request, "analysis", None)
    evidence = getattr(analysis, "evidence", None)
    handoff = getattr(request, "handoff", None)
    unknown_check = DevelopmentDiffReviewCheckResultV1(
        DevelopmentDiffReviewCheckV1.UNKNOWN, "REVIEW_INPUT_INVALID"
    )
    return DevelopmentDiffReviewV1(
        DEVELOPMENT_DIFF_REVIEW_IDENTITY,
        DevelopmentDiffReviewStateV1.VALIDATION_ERROR,
        "INVALID",
        DevelopmentDiffReviewStatusV1.REJECTED,
        reason_code,
        _bounded_or_none(getattr(handoff, "source_analysis_identity", None)),
        _commit_or_none(getattr(evidence, "source_commit", None)),
        _sha256_or_none(getattr(evidence, "evidence_digest", None)),
        CODEX_TASK_IDENTITY if isinstance(handoff, CodexTaskV1) else None,
        _sha256_or_none(getattr(handoff, "task_id", None)),
        _sha256_or_none(getattr(handoff, "evidence_digest", None)),
        _sha256_or_none(getattr(handoff, "context_digest", None)),
        _bounded_or_none(getattr(request, "knowledge_identity", None)),
        _sha256_or_none(getattr(request, "knowledge_manifest_digest", None)),
        _sha256_or_none(getattr(request, "knowledge_context_digest", None)),
        _bounded_or_empty(getattr(request, "target_identity", None)),
        (),
        (),
        (),
        (),
        unknown_check,
        unknown_check,
        unknown_check,
        unknown_check,
        unknown_check,
        (),
    )


def _valid_handoff(handoff: CodexTaskV1) -> bool:
    return (
        handoff.state is CodexTaskStateV1.ADVISORY
        and handoff.source_analysis_identity == DEVELOPMENT_ANALYSIS_IDENTITY
        and _valid_commit(handoff.source_commit)
        and _valid_sha256(handoff.evidence_digest)
        and _valid_sha256(handoff.context_digest)
        and _valid_sha256(handoff.task_id)
        and handoff.approval_status == "HUMAN_APPROVAL_REQUIRED"
        and _valid_diff_paths(handoff.proposed_modified_files)
        and _valid_diff_paths(handoff.proposed_new_files)
        and _valid_bounded_texts(handoff.proposed_contract_changes)
        and _valid_optional_owner(handoff.proposed_owner)
    )


def _valid_change_evidence(values: object) -> bool:
    if not isinstance(values, tuple) or len(values) > MAX_DIFF_REVIEW_ITEMS:
        return False
    paths: list[str] = []
    for value in values:
        if not isinstance(value, DevelopmentDiffChangeEvidenceV1):
            return False
        if (
            not _valid_diff_paths((value.path,))
            or not _bounded_text(value.change_kind, MAX_DIFF_REVIEW_IDENTIFIER)
            or not _valid_sha256(value.before_digest)
            or not _valid_sha256(value.after_digest)
            or not isinstance(value.line_count, int)
            or not 0 <= value.line_count <= MAX_DIFF_REVIEW_LINE_COUNT
            or not _valid_bounded_texts(value.changed_symbols)
            or len(value.changed_symbols) > MAX_DIFF_REVIEW_ITEMS
            or not _valid_bounded_texts(value.authority_markers)
            or len(value.authority_markers) > MAX_DIFF_REVIEW_ITEMS
        ):
            return False
        paths.append(value.path)
    return len(paths) == len(set(paths))


def _valid_evidence_items(values: object, item_type: type[object]) -> bool:
    if not isinstance(values, tuple) or len(values) > MAX_DIFF_REVIEW_ITEMS:
        return False
    identifiers: list[str] = []
    for value in values:
        if (
            not isinstance(value, item_type)
            or not _bounded_text(value.identifier, MAX_DIFF_REVIEW_IDENTIFIER)
            or not isinstance(value.status, DevelopmentDiffReviewCheckV1)
            or not _valid_sha256(value.evidence_digest)
        ):
            return False
        identifiers.append(value.identifier)
    return len(identifiers) == len(set(identifiers))


def _valid_diff_paths(values: object) -> bool:
    if not isinstance(values, tuple) or len(values) > MAX_DIFF_REVIEW_ITEMS:
        return False
    if any(
        not isinstance(value, str) or len(value) > MAX_DIFF_REVIEW_TEXT or _unsafe_path(value)
        for value in values
    ):
        return False
    return len(values) == len(set(values))


def _valid_bounded_texts(values: object) -> bool:
    return (
        isinstance(values, tuple)
        and len(values) <= MAX_DIFF_REVIEW_ITEMS
        and all(_bounded_text(value, MAX_DIFF_REVIEW_TEXT) for value in values)
        and len(values) == len(set(values))
    )


def _valid_optional_owner(value: object) -> bool:
    return value is None or _bounded_text(value, MAX_DIFF_REVIEW_IDENTIFIER)


def _bounded_text(value: object, maximum: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum


def _valid_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(item in "0123456789abcdef" for item in value)
    )


def _valid_commit(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 40
        and all(item in "0123456789abcdef" for item in value)
    )


def _bounded_or_none(value: object) -> str | None:
    return value if _bounded_text(value, MAX_DIFF_REVIEW_IDENTIFIER) else None


def _bounded_or_empty(value: object) -> str:
    return value if _bounded_text(value, MAX_DIFF_REVIEW_IDENTIFIER) else ""


def _sha256_or_none(value: object) -> str | None:
    return value if _valid_sha256(value) else None


def _commit_or_none(value: object) -> str | None:
    return value if _valid_commit(value) else None


CODEX_TASK_IDENTITY = "chappie_codex_handoff/1.0.0"
FORBIDDEN_PATH_PREFIXES = ("data/", "logs/", ".venv/", ".env")
MAX_DIFF_REVIEW_ITEMS = 16
MAX_DIFF_REVIEW_TEXT = 1_000
MAX_DIFF_REVIEW_IDENTIFIER = 160
MAX_DIFF_REVIEW_LINE_COUNT = 1_000_000
FORBIDDEN_ACTIVE_AUTHORITY = (
    "edit file",
    "edit files",
    "write file",
    "write files",
    "git commit",
    "git push",
    "commit git changes",
    "push git changes",
    "use shell",
    "run shell command",
    "run shell commands",
    "run arbitrary shell",
    "execute shell command",
    "execute order",
    "execute orders",
    "execute trade",
    "execute trades",
    "submit order",
    "submit orders",
    "mutate risk state",
    "knowledge mutation",
    "mutate knowledge",
    "rebuild knowledge",
    "deploy",
    "execution authority",
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
    context_digest: str | None
    requested_role: str | None
    problem: str
    facts: tuple[str, ...] = ()
    inferences: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    architecture_owners: tuple[str, ...] = ()
    affected_contracts: tuple[str, ...] = ()
    current_evidence_handles: tuple[str, ...] = ()
    evidence_map: tuple[DevelopmentEvidenceHandleV1, ...] = ()
    allowed_files: tuple[str, ...] = ()
    proposed_modified_files: tuple[str, ...] = ()
    proposed_new_files: tuple[str, ...] = ()
    proposed_contract_changes: tuple[str, ...] = ()
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
    proposed_owner: str | None = None

    def projection(self) -> str:
        """Stable copy/paste text derived only from the validated DTO."""
        return "\n".join(
            (
                "Codex handoff (advisory; human approval required)",
                self.problem,
                "\nCodex consumption guidance",
                "- Begin from this bounded evidence pack; verify supplied files and contracts first.",
                "- Do not broadly re-explore the repository unless this pack is inconsistent or insufficient.",
                "- If scope expansion is required, report why before exceeding the validated scope.",
                "\nFacts",
                *_bullets(self.facts),
                "\nEvidence handles",
                *_bullets(self.current_evidence_handles),
                "\nEvidence map",
                *_bullets(
                    tuple(
                        f"{item.handle} [{item.category}] {item.statement}"
                        for item in self.evidence_map
                    )
                    or ("No evidence map is available.",)
                ),
                "\nScope",
                *_bullets(self.implementation_constraints),
                "\nCanonical proposed owner",
                *_bullets((self.proposed_owner,) if self.proposed_owner else ("None.",)),
                "\nAllowed files",
                *_bullets(self.allowed_files or ("None proposed by validated analysis.",)),
                "\nProposed modified files",
                *_bullets(self.proposed_modified_files or ("None.",)),
                "\nProposed new files",
                *_bullets(self.proposed_new_files or ("None.",)),
                "\nProposed contract changes",
                *_bullets(self.proposed_contract_changes or ("None.",)),
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
            "context_digest": self.context_digest,
            "requested_role": self.requested_role,
            "problem": self.problem,
            "facts": list(self.facts),
            "inferences": list(self.inferences),
            "unknowns": list(self.unknowns),
            "architecture_owners": list(self.architecture_owners),
            "affected_contracts": list(self.affected_contracts),
            "current_evidence_handles": list(self.current_evidence_handles),
            "evidence_map": [item.payload() for item in self.evidence_map],
            "allowed_files": list(self.allowed_files),
            "proposed_modified_files": list(self.proposed_modified_files),
            "proposed_new_files": list(self.proposed_new_files),
            "proposed_contract_changes": list(self.proposed_contract_changes),
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
            "proposed_owner": self.proposed_owner,
            "projection": self.projection(),
        }


# The existing endpoint remains additive-compatible; the v2 projection is explicitly a handoff.
CodexHandoffV1 = CodexTaskV1


def prepare_codex_task(request: CodexTaskPreparationRequestV1) -> CodexTaskV1:
    analysis = request.analysis
    try:
        if analysis.state != DevelopmentAnalysisStateV1.OK or analysis.evidence is None:
            raise ValueError("SOURCE_ANALYSIS_INVALID")
        evidence = analysis.evidence
        if not evidence.source_commit or not evidence.repository_clean:
            raise ValueError("SOURCE_EVIDENCE_MISMATCH")
        if (
            analysis.context_map is None
            or analysis.proposed_scope is None
            or analysis.proposed_scope.proposed_owner is None
            or analysis.proposed_scope.proposed_owner not in analysis.context_map.owner_candidates
        ):
            raise ValueError("OWNER_INVALID")
        if not analysis.required_tests:
            raise ValueError("MISSING_TESTS")
        if not analysis.acceptance_criteria:
            raise ValueError("MISSING_ACCEPTANCE_CRITERIA")
        if any(_unsafe_path(path) for path in analysis.likely_files):
            raise ValueError("UNSAFE_FILE_PATH")
        if any(
            _forbidden_authority_claim(text)
            for text in (*analysis.inferences, *analysis.risks, *analysis.acceptance_criteria)
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
    proposed_owner = analysis.proposed_scope.proposed_owner
    return CodexTaskV1(
        task_id,
        CodexTaskStateV1.ADVISORY,
        DEVELOPMENT_ANALYSIS_IDENTITY,
        evidence.source_commit,
        evidence.evidence_digest,
        analysis.context_map.context_digest if analysis.context_map else None,
        evidence.requested_role.value,
        analysis.request,
        analysis.facts,
        analysis.inferences,
        analysis.unknowns,
        analysis.architecture_owners,
        analysis.affected_contracts,
        analysis.current_evidence_handles,
        analysis.context_map.evidence_handles,
        analysis.likely_files,
        analysis.proposed_scope.proposed_modified_files,
        analysis.proposed_scope.proposed_new_files,
        analysis.proposed_scope.proposed_contract_changes,
        (
            "No Codex invocation, file edits, shell, Git, Knowledge, trading, Risk, or execution authority.",
        ),
        analysis.request,
        ("Use only the validated evidence and keep unknowns explicit.",),
        analysis.risks,
        analysis.required_tests,
        analysis.non_goals,
        analysis.acceptance_criteria,
        proposed_owner=proposed_owner,
    )


prepare_codex_handoff = prepare_codex_task


def _invalid(analysis: DevelopmentAnalysisV1, reason: str) -> CodexTaskV1:
    return CodexTaskV1(
        "",
        CodexTaskStateV1.VALIDATION_ERROR,
        DEVELOPMENT_ANALYSIS_IDENTITY,
        analysis.evidence.source_commit if analysis.evidence else None,
        analysis.evidence.evidence_digest if analysis.evidence else None,
        analysis.context_map.context_digest if analysis.context_map else None,
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


def _forbidden_authority_claim(value: str) -> bool:
    normalized = value.casefold()
    for claim in FORBIDDEN_ACTIVE_AUTHORITY:
        if search(rf"\b{escape(claim)}\b", normalized) is None:
            continue
        if any(
            f"{prefix}{claim}" in normalized
            for prefix in ("no ", "without ", "must not ", "do not ", "does not ")
        ):
            continue
        return True
    return False


def _bullets(items: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(f"- {item}" for item in items)
