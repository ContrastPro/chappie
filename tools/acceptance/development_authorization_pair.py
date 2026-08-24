"""Persist and restore a bounded Development authorization DTO pair.

This acceptance-only helper preserves the exact public DTO payloads required to
replay a later ``DevelopmentDiffReviewV1``.  It does not create analysis or
handoff objects, call a model, or alter any production state.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.ai.schemas import digest
from app.development_analysis.context import DevelopmentEvidenceHandleV1
from app.development_analysis.models import (
    DEVELOPMENT_ANALYSIS_IDENTITY,
    DevelopmentAnalysisReadinessV1,
    DevelopmentAnalysisStateV1,
    DevelopmentAnalysisV1,
    development_analysis_from_payload,
)
from app.development_analysis.outcome import (
    DevelopmentOutcomeStatusV1,
    project_development_outcome,
)
from app.development_analysis.task import (
    CODEX_TASK_IDENTITY,
    CodexTaskStateV1,
    CodexTaskV1,
)

AUTHORIZATION_PAIR_IDENTITY = "chappie_development_authorization_pair/1.0.0"
AUTHORIZATION_PAIR_FILENAME = "authorization_pair.json"

_HANDOFF_FIELDS = frozenset(
    {
        "identity",
        "task_id",
        "state",
        "source_analysis_identity",
        "source_commit",
        "evidence_digest",
        "context_digest",
        "requested_role",
        "problem",
        "facts",
        "inferences",
        "unknowns",
        "architecture_owners",
        "affected_contracts",
        "current_evidence_handles",
        "evidence_map",
        "allowed_files",
        "proposed_modified_files",
        "proposed_new_files",
        "proposed_contract_changes",
        "forbidden_scope",
        "implementation_objective",
        "implementation_constraints",
        "risks",
        "required_tests",
        "non_goals",
        "acceptance_criteria",
        "approval_status",
        "validation_status",
        "reason_code",
        "proposed_owner",
        "projection",
    }
)


@dataclass(frozen=True, slots=True)
class AuthorizationPairV1:
    analysis: DevelopmentAnalysisV1
    handoff: CodexTaskV1
    authorization_digest: str


def codex_handoff_from_payload(value: object) -> CodexTaskV1:
    """Strictly restore the exact public CodexHandoffV1 envelope."""
    if not isinstance(value, dict) or set(value) != _HANDOFF_FIELDS:
        raise ValueError("codex handoff payload is invalid")
    if value.get("identity") != CODEX_TASK_IDENTITY:
        raise ValueError("codex handoff identity is invalid")
    try:
        handoff = CodexTaskV1(
            task_id=_text(value["task_id"]),
            state=CodexTaskStateV1(value["state"]),
            source_analysis_identity=_text(value["source_analysis_identity"]),
            source_commit=_optional_text(value["source_commit"]),
            evidence_digest=_optional_text(value["evidence_digest"]),
            context_digest=_optional_text(value["context_digest"]),
            requested_role=_optional_text(value["requested_role"]),
            problem=_text(value["problem"]),
            facts=_texts(value["facts"]),
            inferences=_texts(value["inferences"]),
            unknowns=_texts(value["unknowns"]),
            architecture_owners=_texts(value["architecture_owners"]),
            affected_contracts=_texts(value["affected_contracts"]),
            current_evidence_handles=_texts(value["current_evidence_handles"]),
            evidence_map=_evidence_handles(value["evidence_map"]),
            allowed_files=_texts(value["allowed_files"]),
            proposed_modified_files=_texts(value["proposed_modified_files"]),
            proposed_new_files=_texts(value["proposed_new_files"]),
            proposed_contract_changes=_texts(value["proposed_contract_changes"]),
            forbidden_scope=_texts(value["forbidden_scope"]),
            implementation_objective=_text(value["implementation_objective"]),
            implementation_constraints=_texts(value["implementation_constraints"]),
            risks=_texts(value["risks"]),
            required_tests=_texts(value["required_tests"]),
            non_goals=_texts(value["non_goals"]),
            acceptance_criteria=_texts(value["acceptance_criteria"]),
            approval_status=_text(value["approval_status"]),
            validation_status=_text(value["validation_status"]),
            reason_code=_optional_text(value["reason_code"]),
            proposed_owner=_optional_text(value["proposed_owner"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("codex handoff payload is invalid") from error
    if handoff.payload() != value:
        raise ValueError("codex handoff payload projection is invalid")
    expected_task_id = digest(
        {
            "identity": CODEX_TASK_IDENTITY,
            "evidence": handoff.evidence_digest,
            "request": handoff.problem,
        }
    )
    if handoff.task_id != expected_task_id:
        raise ValueError("codex handoff task identity is invalid")
    return handoff


def authorization_pair_payload(
    analysis: DevelopmentAnalysisV1, handoff: CodexTaskV1
) -> dict[str, object]:
    """Create a deterministic artifact only for a validated approved pair."""
    _validate_authorized_pair(analysis, handoff)
    analysis_payload = analysis.payload()
    handoff_payload = handoff.payload()
    if development_analysis_from_payload(analysis_payload).payload() != analysis_payload:
        raise ValueError("development analysis round-trip is invalid")
    if codex_handoff_from_payload(handoff_payload).payload() != handoff_payload:
        raise ValueError("codex handoff round-trip is invalid")
    semantic = {"analysis": analysis_payload, "handoff": handoff_payload}
    authorization_digest = digest({"identity": AUTHORIZATION_PAIR_IDENTITY, **semantic})
    evidence = analysis.evidence
    context = analysis.context_map
    assert evidence is not None and context is not None
    return {
        "identity": AUTHORIZATION_PAIR_IDENTITY,
        "source_commit": evidence.source_commit,
        "knowledge_identity": evidence.knowledge_identity,
        "knowledge_manifest_digest": evidence.knowledge_manifest_digest,
        "knowledge_context_digest": evidence.knowledge_context_digest,
        "analysis_evidence_digest": evidence.evidence_digest,
        "analysis_context_digest": context.context_digest,
        "handoff_evidence_digest": handoff.evidence_digest,
        "handoff_context_digest": handoff.context_digest,
        "approval_status": handoff.approval_status,
        "authorization_digest": authorization_digest,
        **semantic,
    }


def persist_authorization_pair(
    path: Path, analysis: DevelopmentAnalysisV1, handoff: CodexTaskV1
) -> AuthorizationPairV1:
    """Atomically persist a new immutable local acceptance artifact."""
    if project_development_outcome(analysis).status is not DevelopmentOutcomeStatusV1.COMPLETED:
        raise ValueError("development analysis outcome is not implementation-authorizable")
    if path.exists():
        raise FileExistsError("authorization pair artifact already exists")
    payload = authorization_pair_payload(analysis, handoff)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return load_authorization_pair(path)


def load_authorization_pair(path: Path) -> AuthorizationPairV1:
    """Load only an exact, provenance-bound authorization pair."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("authorization pair artifact is invalid") from error
    if not isinstance(payload, dict) or payload.get("identity") != AUTHORIZATION_PAIR_IDENTITY:
        raise ValueError("authorization pair artifact is invalid")
    try:
        analysis = development_analysis_from_payload(payload.get("analysis"))
        handoff = codex_handoff_from_payload(payload.get("handoff"))
    except (TypeError, ValueError) as error:
        raise ValueError("authorization pair artifact is invalid") from error
    _validate_authorized_pair(analysis, handoff)
    expected = authorization_pair_payload(analysis, handoff)
    if payload != expected:
        raise ValueError("authorization pair artifact binding is invalid")
    return AuthorizationPairV1(analysis, handoff, str(payload["authorization_digest"]))


def _validate_authorized_pair(analysis: DevelopmentAnalysisV1, handoff: CodexTaskV1) -> None:
    evidence = analysis.evidence
    context = analysis.context_map
    if (
        analysis.state is not DevelopmentAnalysisStateV1.OK
        or analysis.validation_status != "VALID"
        or analysis.readiness is not DevelopmentAnalysisReadinessV1.READY_FOR_TASK
        or evidence is None
        or context is None
        or not evidence.repository_clean
        or handoff.state is not CodexTaskStateV1.ADVISORY
        or handoff.validation_status != "VALID"
        or handoff.approval_status != "HUMAN_APPROVAL_REQUIRED"
        or handoff.source_analysis_identity != DEVELOPMENT_ANALYSIS_IDENTITY
        or handoff.source_commit != evidence.source_commit
        or handoff.evidence_digest != evidence.evidence_digest
        or handoff.context_digest != context.context_digest
    ):
        raise ValueError("authorization pair is not a validated approved pair")


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError
    return value


def _optional_text(value: object) -> str | None:
    if value is not None and not isinstance(value, str):
        raise TypeError
    return value


def _texts(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise TypeError
    return tuple(value)


def _evidence_handles(value: object) -> tuple[DevelopmentEvidenceHandleV1, ...]:
    if not isinstance(value, list):
        raise TypeError
    try:
        handles = tuple(
            DevelopmentEvidenceHandleV1(
                _text(item["handle"]),
                _text(item["fact_id"]),
                _text(item["record_id"]),
                _text(item["record_digest"]),
                _text(item["category"]),
                _text(item["statement"]),
            )
            for item in value
            if isinstance(item, dict)
        )
        if len(handles) != len(value):
            raise TypeError
        return handles
    except (KeyError, TypeError) as error:
        raise TypeError from error
