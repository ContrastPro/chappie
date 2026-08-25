"""One explicit, read-only targeted contract recovery for Development."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from app.ai.schemas import canonical_json, digest
from app.development_analysis.implementation_context import (
    build_implementation_context_from_development,
)
from app.implementation_context import MissingContextRequestKindV1, MissingContextRequestV1
from app.targeted_context import resolve_missing_context

DEVELOPMENT_TARGETED_CONTEXT_RECOVERY_IDENTITY = (
    "chappie_development_targeted_context_recovery/1.0.0"
)
DEVELOPMENT_TARGETED_CONTEXT_ATTACHMENT_IDENTITY = (
    "chappie_development_targeted_context_attachment/1.0.0"
)
MAX_TARGETED_CONTEXT_ATTACHMENT_CHARACTERS = 4_096
_SUPPORTED_BLOCKER = "NO_GROUNDED_CONTRACT_SCOPE"
_CONTRACT = "app.development_analysis.models.DevelopmentDiffReviewV1"
_SOURCE_PATH = "app/development_analysis/models.py"


@dataclass(frozen=True, slots=True)
class DevelopmentTargetedContextAttachmentV1:
    source_commit: str
    knowledge_identity: str
    knowledge_manifest_digest: str
    original_evidence_digest: str
    original_context_digest: str
    contract_identifier: str
    source_path: str
    declaration_kind: str
    declaration_shape: str
    targeted_context_digest: str
    attachment_digest: str

    @property
    def identity(self) -> str:
        return DEVELOPMENT_TARGETED_CONTEXT_ATTACHMENT_IDENTITY

    def payload(self, *, include_digest: bool = True) -> dict[str, object]:
        value = {
            "identity": self.identity,
            "source_commit": self.source_commit,
            "knowledge_identity": self.knowledge_identity,
            "knowledge_manifest_digest": self.knowledge_manifest_digest,
            "original_evidence_digest": self.original_evidence_digest,
            "original_context_digest": self.original_context_digest,
            "contract_identifier": self.contract_identifier,
            "source_path": self.source_path,
            "declaration_kind": self.declaration_kind,
            "declaration_shape": self.declaration_shape,
            "targeted_context_digest": self.targeted_context_digest,
        }
        if include_digest:
            value["attachment_digest"] = self.attachment_digest
        return value


@dataclass(frozen=True, slots=True)
class DevelopmentTargetedContextRecoveryV1:
    original_request: str
    requested_role: str
    requested_intent: str
    missing_request: MissingContextRequestV1 | None
    status: str
    reason_code: str | None
    attachment: DevelopmentTargetedContextAttachmentV1 | None

    @property
    def identity(self) -> str:
        return DEVELOPMENT_TARGETED_CONTEXT_RECOVERY_IDENTITY

    def payload(self) -> dict[str, object]:
        return {
            "identity": self.identity,
            "original_request": self.original_request,
            "requested_role": self.requested_role,
            "requested_intent": self.requested_intent,
            "missing_request": self.missing_request.payload() if self.missing_request else None,
            "status": self.status,
            "reason_code": self.reason_code,
            "attachment": self.attachment.payload() if self.attachment else None,
        }


def targeted_context_attachment_from_payload(
    value: object,
) -> DevelopmentTargetedContextAttachmentV1:
    if not isinstance(value, dict):
        raise TypeError("targeted context attachment is invalid")
    required = {
        "identity",
        "source_commit",
        "knowledge_identity",
        "knowledge_manifest_digest",
        "original_evidence_digest",
        "original_context_digest",
        "contract_identifier",
        "source_path",
        "declaration_kind",
        "declaration_shape",
        "targeted_context_digest",
        "attachment_digest",
    }
    if (
        set(value) != required
        or value.get("identity") != DEVELOPMENT_TARGETED_CONTEXT_ATTACHMENT_IDENTITY
        or any(not isinstance(value[key], str) for key in required - {"identity"})
    ):
        raise ValueError("targeted context attachment is invalid")
    try:
        result = DevelopmentTargetedContextAttachmentV1(
            *(
                str(value[key])
                for key in (
                    "source_commit",
                    "knowledge_identity",
                    "knowledge_manifest_digest",
                    "original_evidence_digest",
                    "original_context_digest",
                    "contract_identifier",
                    "source_path",
                    "declaration_kind",
                    "declaration_shape",
                    "targeted_context_digest",
                    "attachment_digest",
                )
            )
        )
    except (TypeError, ValueError) as error:
        raise ValueError("targeted context attachment is invalid") from error
    if (
        result.contract_identifier != _CONTRACT
        or result.source_path != _SOURCE_PATH
        or result.declaration_kind != "CLASS"
        or not result.declaration_shape
        or not result.source_commit
        or not result.knowledge_identity
        or any(
            len(value) != 64 or any(character not in "0123456789abcdef" for character in value)
            for value in (
                result.knowledge_manifest_digest,
                result.original_evidence_digest,
                result.original_context_digest,
                result.targeted_context_digest,
                result.attachment_digest,
            )
        )
        or result.attachment_digest != digest(result.payload(include_digest=False))
    ):
        raise ValueError("targeted context attachment is invalid")
    return result


def recover_targeted_contract_context(
    analysis: object, snapshot: object, root: Path
) -> DevelopmentTargetedContextRecoveryV1:
    """Resolve only the registered contract for the one supported blocker."""
    evidence = getattr(analysis, "evidence", None)
    context_map = getattr(analysis, "context_map", None)
    intent = getattr(getattr(analysis, "task_intent", None), "intent", None)
    role = getattr(getattr(analysis, "evidence", None), "requested_role", None)
    base = (
        getattr(analysis, "request", ""),
        getattr(role, "value", ""),
        getattr(intent, "value", ""),
    )
    if (
        getattr(analysis, "reason_code", None) != _SUPPORTED_BLOCKER
        or getattr(intent, "value", None) != "IMPLEMENTATION"
        or evidence is None
        or context_map is None
    ):
        return DevelopmentTargetedContextRecoveryV1(
            *base, None, "REJECTED", "RECOVERY_NOT_SUPPORTED", None
        )
    if not getattr(snapshot, "clean", False):
        return DevelopmentTargetedContextRecoveryV1(
            *base, None, "REJECTED", "REPOSITORY_NOT_CLEAN", None
        )
    if getattr(snapshot, "commit", None) != evidence.source_commit:
        return DevelopmentTargetedContextRecoveryV1(
            *base, None, "REJECTED", "SOURCE_COMMIT_MISMATCH", None
        )
    implementation_context = build_implementation_context_from_development(context_map, analysis)
    request = MissingContextRequestV1(
        MissingContextRequestKindV1.CONTRACT,
        _CONTRACT,
        _SUPPORTED_BLOCKER,
        implementation_context.digest,
    )
    expansion = resolve_missing_context(
        implementation_context,
        (request,),
        snapshot,
        root,
        request_file_hints={_CONTRACT: _SOURCE_PATH},
    )
    if len(expansion.resolved) != 1 or len(expansion.contracts) != 1 or expansion.unresolved:
        return DevelopmentTargetedContextRecoveryV1(
            *base, request, "REJECTED", "CONTRACT_NOT_RESOLVED", None
        )
    contract = expansion.contracts[0]
    if contract.identity != _CONTRACT or contract.source_commit != evidence.source_commit:
        return DevelopmentTargetedContextRecoveryV1(
            *base, request, "REJECTED", "CONTRACT_BINDING_INVALID", None
        )
    targeted_payload = {
        "identity": expansion.identity,
        "source_context_digest": expansion.source_context_digest,
        "source_commit": expansion.source_commit,
        "requests": [item.payload() for item in expansion.requests],
        "resolved": [
            (item.request.payload(), item.reason.value, item.locator) for item in expansion.resolved
        ],
        "contracts": [item.payload() for item in expansion.contracts],
        "symbols": [item.payload() for item in expansion.symbols],
        "files": list(expansion.files),
    }
    targeted_digest = digest(targeted_payload)
    provisional = DevelopmentTargetedContextAttachmentV1(
        evidence.source_commit,
        evidence.knowledge_identity,
        evidence.knowledge_manifest_digest,
        evidence.evidence_digest,
        context_map.context_digest,
        contract.identity,
        _SOURCE_PATH,
        "CLASS",
        contract.current_shape[0],
        targeted_digest,
        "0" * 64,
    )
    if (
        len(canonical_json(provisional.payload(include_digest=False)))
        > MAX_TARGETED_CONTEXT_ATTACHMENT_CHARACTERS
    ):
        return DevelopmentTargetedContextRecoveryV1(
            *base, request, "REJECTED", "ATTACHMENT_BUDGET_EXCEEDED", None
        )
    attachment = replace(
        provisional,
        attachment_digest=digest(provisional.payload(include_digest=False)),
    )
    return DevelopmentTargetedContextRecoveryV1(*base, request, "RESOLVED", None, attachment)
