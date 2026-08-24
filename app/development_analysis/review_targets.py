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


class ReviewProposalTargetConstraintV1(StrEnum):
    TARGETS_PER_SOURCE_FIELD = "TARGETS_PER_SOURCE_FIELD"
    IDENTIFIER_LENGTH = "IDENTIFIER_LENGTH"
    TOTAL_TARGET_COUNT = "TOTAL_TARGET_COUNT"
    PUBLIC_ID_LENGTH = "PUBLIC_ID_LENGTH"
    SERIALIZED_SIZE = "SERIALIZED_SIZE"


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


@dataclass(frozen=True, slots=True)
class ReviewProposalTargetProjectionDiagnosticsV1:
    constraint: ReviewProposalTargetConstraintV1
    configured_limit: int
    source_field: str | None = None
    source_index: int | None = None
    field_count: int | None = None
    total_candidate_count: int | None = None
    identifier_length: int | None = None
    maximum_identifier_length: int | None = None
    target_count: int | None = None
    public_id_length: int | None = None
    projected_semantic_size: int | None = None

    def payload(self) -> dict[str, object]:
        values = {
            "constraint": self.constraint.value,
            "configured_limit": self.configured_limit,
            "source_field": self.source_field,
            "source_index": self.source_index,
            "field_count": self.field_count,
            "total_candidate_count": self.total_candidate_count,
            "identifier_length": self.identifier_length,
            "maximum_identifier_length": self.maximum_identifier_length,
            "target_count": self.target_count,
            "public_id_length": self.public_id_length,
            "projected_semantic_size": self.projected_semantic_size,
        }
        return {key: value for key, value in values.items() if value is not None}


class ReviewProposalTargetProjectionError(ValueError):
    def __init__(
        self,
        code: str,
        diagnostics: ReviewProposalTargetProjectionDiagnosticsV1 | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.diagnostics = diagnostics


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
            raise ReviewProposalTargetProjectionError(
                "REVIEW_TARGET_SET_BUDGET_EXCEEDED",
                ReviewProposalTargetProjectionDiagnosticsV1(
                    ReviewProposalTargetConstraintV1.TARGETS_PER_SOURCE_FIELD,
                    MAX_TARGETS_PER_SOURCE_FIELD,
                    source_field=source_field,
                    field_count=len(values),
                ),
            )
        for source_index, exact_identifier in enumerate(values):
            if not exact_identifier or len(exact_identifier) > MAX_REVIEW_TARGET_IDENTIFIER_CHARS:
                raise ReviewProposalTargetProjectionError(
                    "REVIEW_TARGET_SET_BUDGET_EXCEEDED",
                    ReviewProposalTargetProjectionDiagnosticsV1(
                        ReviewProposalTargetConstraintV1.IDENTIFIER_LENGTH,
                        MAX_REVIEW_TARGET_IDENTIFIER_CHARS,
                        source_field=source_field,
                        source_index=source_index,
                        identifier_length=len(exact_identifier),
                    ),
                )
            provenance = (kind, source_field, source_index)
            if exact_identifier in identifiers:
                # Repeated semantic identifiers have contradictory source provenance.
                raise ReviewProposalTargetProjectionError("REVIEW_TARGET_SET_INVALID")
            identifiers[exact_identifier] = provenance
            pending.append((kind, exact_identifier, source_field, source_index))
    if len(pending) > MAX_REVIEW_PROPOSAL_TARGETS:
        raise ReviewProposalTargetProjectionError(
            "REVIEW_TARGET_SET_BUDGET_EXCEEDED",
            ReviewProposalTargetProjectionDiagnosticsV1(
                ReviewProposalTargetConstraintV1.TOTAL_TARGET_COUNT,
                MAX_REVIEW_PROPOSAL_TARGETS,
                total_candidate_count=len(pending),
            ),
        )

    target_values: list[ReviewProposalTargetV1] = []
    for target_index, (kind, identifier, source_field, source_index) in enumerate(pending, start=1):
        public_id = f"RT{target_index}"
        if len(public_id) > MAX_REVIEW_TARGET_PUBLIC_ID_CHARS:
            raise ReviewProposalTargetProjectionError(
                "REVIEW_TARGET_SET_BUDGET_EXCEEDED",
                ReviewProposalTargetProjectionDiagnosticsV1(
                    ReviewProposalTargetConstraintV1.PUBLIC_ID_LENGTH,
                    MAX_REVIEW_TARGET_PUBLIC_ID_CHARS,
                    source_index=target_index - 1,
                    target_count=len(pending),
                    public_id_length=len(public_id),
                ),
            )
        target_values.append(
            ReviewProposalTargetV1(
                public_id=public_id,
                kind=kind,
                exact_identifier=identifier,
                display_summary=_display_summary(identifier),
                source_field=source_field,
                source_index=source_index,
            )
        )
    targets = tuple(target_values)

    proposal_payload = proposal.payload()
    proposal_semantic_digest = digest(proposal_payload)
    semantic_payload = {
        "identity": REVIEW_PROPOSAL_TARGETS_IDENTITY,
        "proposal_identity": proposal_identity,
        "proposal_digest": proposal_semantic_digest,
        "targets": [target.semantic_payload() for target in targets],
    }
    semantic_size = len(canonical_json(semantic_payload))
    if semantic_size > MAX_REVIEW_TARGET_SET_SERIALIZED_CHARS:
        raise ReviewProposalTargetProjectionError(
            "REVIEW_TARGET_SET_BUDGET_EXCEEDED",
            ReviewProposalTargetProjectionDiagnosticsV1(
                ReviewProposalTargetConstraintV1.SERIALIZED_SIZE,
                MAX_REVIEW_TARGET_SET_SERIALIZED_CHARS,
                projected_semantic_size=semantic_size,
            ),
        )
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
