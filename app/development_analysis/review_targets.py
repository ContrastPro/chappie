"""Deterministic public Proposal targets shared by Development Review."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from app.ai.schemas import canonical_json, digest
from app.development_analysis.models import DevelopmentProposedScopeV1

REVIEW_PROPOSAL_TARGETS_IDENTITY = "chappie_review_proposal_targets/1.0.0"
MAX_REVIEW_PROPOSAL_TARGETS = 32
MAX_TARGETS_PER_SOURCE_FIELD = 16
MAX_REVIEW_TARGET_IDENTIFIER_CHARS = 256
MAX_REVIEW_TARGET_SUMMARY_CHARS = 160
MAX_REVIEW_TARGET_PUBLIC_ID_CHARS = 8
MAX_REVIEW_TARGET_SET_SERIALIZED_CHARS = 16_384


class ReviewProposalTargetKindV1(StrEnum):
    FILE = "FILE"
    CONTRACT = "CONTRACT"
    COMPONENT = "COMPONENT"
    BEHAVIOR = "BEHAVIOR"
    RATIONALE = "RATIONALE"


_PUBLIC_ID = re.compile(r"RT[1-9][0-9]*")
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class ReviewProposalTargetV1:
    public_id: str
    kind: ReviewProposalTargetKindV1
    exact_identifier: str
    display_summary: str
    source_field: str
    source_index: int
    state: str = "PROPOSED"

    def __post_init__(self) -> None:
        if (
            not _PUBLIC_ID.fullmatch(self.public_id)
            or len(self.public_id) > MAX_REVIEW_TARGET_PUBLIC_ID_CHARS
            or not self.exact_identifier
            or len(self.exact_identifier) > MAX_REVIEW_TARGET_IDENTIFIER_CHARS
            or not self.display_summary
            or len(self.display_summary) > MAX_REVIEW_TARGET_SUMMARY_CHARS
            or self.source_index < 0
            or self.state != "PROPOSED"
        ):
            raise ValueError("review Proposal target is invalid")

    def semantic_payload(self) -> dict[str, object]:
        return {
            "public_id": self.public_id,
            "kind": self.kind.value,
            "exact_identifier": self.exact_identifier,
            "source_field": self.source_field,
            "source_index": self.source_index,
            "state": self.state,
        }

    def payload(self) -> dict[str, object]:
        return {**self.semantic_payload(), "display_summary": self.display_summary}


@dataclass(frozen=True, slots=True)
class ReviewProposalTargetSetV1:
    proposal_identity: str
    proposal_digest: str
    targets: tuple[ReviewProposalTargetV1, ...]
    target_set_digest: str
    identity: str = REVIEW_PROPOSAL_TARGETS_IDENTITY

    def __post_init__(self) -> None:
        if (
            self.identity != REVIEW_PROPOSAL_TARGETS_IDENTITY
            or not self.proposal_identity
            or not _SHA256.fullmatch(self.proposal_digest)
            or not _SHA256.fullmatch(self.target_set_digest)
            or len(self.targets) > MAX_REVIEW_PROPOSAL_TARGETS
            or tuple(target.public_id for target in self.targets)
            != tuple(f"RT{index}" for index in range(1, len(self.targets) + 1))
        ):
            raise ValueError("review Proposal target set is invalid")

    def payload(self) -> dict[str, object]:
        return {
            "identity": self.identity,
            "proposal_identity": self.proposal_identity,
            "proposal_digest": self.proposal_digest,
            "targets": [target.payload() for target in self.targets],
            "target_set_digest": self.target_set_digest,
        }


class ReviewProposalTargetProjectionError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _ProposalV1(Protocol):
    proposed_scope: DevelopmentProposedScopeV1

    def payload(self) -> dict[str, object]: ...


_SOURCE_FIELDS = (
    ("proposed_modified_files", ReviewProposalTargetKindV1.FILE),
    ("proposed_new_files", ReviewProposalTargetKindV1.FILE),
    ("proposed_contract_changes", ReviewProposalTargetKindV1.CONTRACT),
    ("proposed_components", ReviewProposalTargetKindV1.COMPONENT),
    ("proposed_behavior", ReviewProposalTargetKindV1.BEHAVIOR),
    ("rationale", ReviewProposalTargetKindV1.RATIONALE),
)


def build_review_proposal_target_set(
    proposal: _ProposalV1, *, proposal_identity: str
) -> ReviewProposalTargetSetV1:
    """Project an exact, bounded, request-local Review target namespace."""
    pending: list[tuple[ReviewProposalTargetKindV1, str, str, int]] = []
    identifiers: dict[str, tuple[ReviewProposalTargetKindV1, str, int]] = {}
    for source_field, kind in _SOURCE_FIELDS:
        values = tuple(getattr(proposal.proposed_scope, source_field))
        if len(values) > MAX_TARGETS_PER_SOURCE_FIELD:
            raise ReviewProposalTargetProjectionError("REVIEW_TARGET_SET_BUDGET_EXCEEDED")
        for source_index, exact_identifier in enumerate(values):
            if not exact_identifier or len(exact_identifier) > MAX_REVIEW_TARGET_IDENTIFIER_CHARS:
                raise ReviewProposalTargetProjectionError("REVIEW_TARGET_SET_BUDGET_EXCEEDED")
            provenance = (kind, source_field, source_index)
            if exact_identifier in identifiers:
                # Repeated semantic identifiers have contradictory source provenance.
                raise ReviewProposalTargetProjectionError("REVIEW_TARGET_SET_INVALID")
            identifiers[exact_identifier] = provenance
            pending.append((kind, exact_identifier, source_field, source_index))
    if len(pending) > MAX_REVIEW_PROPOSAL_TARGETS:
        raise ReviewProposalTargetProjectionError("REVIEW_TARGET_SET_BUDGET_EXCEEDED")

    targets = tuple(
        ReviewProposalTargetV1(
            public_id=f"RT{index}",
            kind=kind,
            exact_identifier=identifier,
            display_summary=_display_summary(identifier),
            source_field=source_field,
            source_index=source_index,
        )
        for index, (kind, identifier, source_field, source_index) in enumerate(pending, start=1)
    )
    if any(len(target.public_id) > MAX_REVIEW_TARGET_PUBLIC_ID_CHARS for target in targets):
        raise ReviewProposalTargetProjectionError("REVIEW_TARGET_SET_BUDGET_EXCEEDED")

    proposal_payload = proposal.payload()
    proposal_semantic_digest = digest(proposal_payload)
    semantic_payload = {
        "identity": REVIEW_PROPOSAL_TARGETS_IDENTITY,
        "proposal_identity": proposal_identity,
        "proposal_digest": proposal_semantic_digest,
        "targets": [target.semantic_payload() for target in targets],
    }
    if len(canonical_json(semantic_payload)) > MAX_REVIEW_TARGET_SET_SERIALIZED_CHARS:
        raise ReviewProposalTargetProjectionError("REVIEW_TARGET_SET_BUDGET_EXCEEDED")
    return ReviewProposalTargetSetV1(
        proposal_identity=proposal_identity,
        proposal_digest=proposal_semantic_digest,
        targets=targets,
        target_set_digest=digest(semantic_payload),
    )


def _display_summary(value: str) -> str:
    if len(value) <= MAX_REVIEW_TARGET_SUMMARY_CHARS:
        return value
    return value[: MAX_REVIEW_TARGET_SUMMARY_CHARS - 1] + "…"
