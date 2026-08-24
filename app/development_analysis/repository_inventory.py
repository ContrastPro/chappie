"""Explicit, bounded RepositoryInventory context recovery for Development."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from enum import StrEnum

from app.ai.schemas import canonical_json, digest
from app.repository_inventory import (
    POLICY_DIGEST,
    REPOSITORY_INVENTORY_IDENTITY,
    REPOSITORY_INVENTORY_POLICY_IDENTITY,
    RepositoryInventoryCompletenessV1,
    RepositoryInventoryStateV1,
    RepositoryInventoryV1,
    RepositoryRelationKindV1,
)

DEVELOPMENT_CONTEXT_REQUIREMENT_IDENTITY = "chappie_development_context_requirement/1.0.0"
REPOSITORY_INVENTORY_ATTACHMENT_IDENTITY = (
    "chappie_development_repository_inventory_attachment/1.0.0"
)
MAX_REPOSITORY_INVENTORY_ATTACHMENT_CHARACTERS = 48_000
MAX_ATTACHMENT_GROUP_FILES = 100
_ARTIFACT_CODES = {
    "DECLARED_SOURCE": "SOURCE",
    "DECLARED_TEST_FIXTURE": "FIXTURE",
    "DECLARED_DISTRIBUTION": "DISTRIBUTION",
    "UNCLASSIFIED": "UNCLASSIFIED",
}
_COVERAGE_CODES = {
    "COMPLETE": "COMPLETE",
    "NO_KNOWN_REFERENCE": "NO_REFERENCE",
    "UNSUPPORTED_FILE_KIND": "UNSUPPORTED",
    "PARSE_FAILED": "PARSE_FAILED",
    "BUDGET_EXCEEDED": "BUDGET_EXCEEDED",
    "NOT_APPLICABLE": "NOT_APPLICABLE",
}
_RELATION_CODES = {
    item: f"R{index}"
    for index, item in enumerate(sorted(value.value for value in RepositoryRelationKindV1), start=1)
}


class DevelopmentContextCapabilityV1(StrEnum):
    REPOSITORY_INVENTORY = "REPOSITORY_INVENTORY"


class DevelopmentEvidenceGapCodeV1(StrEnum):
    REPOSITORY_STRUCTURE_UNAVAILABLE = "REPOSITORY_STRUCTURE_UNAVAILABLE"
    MODULE_USAGE_PATTERNS_UNAVAILABLE = "MODULE_USAGE_PATTERNS_UNAVAILABLE"
    NON_IMPORT_CONSUMERS_UNAVAILABLE = "NON_IMPORT_CONSUMERS_UNAVAILABLE"
    TEST_DEPENDENCY_COVERAGE_INCOMPLETE = "TEST_DEPENDENCY_COVERAGE_INCOMPLETE"
    SAFE_CLEANUP_EVIDENCE_INSUFFICIENT = "SAFE_CLEANUP_EVIDENCE_INSUFFICIENT"


_GAP_FACTS = {
    "full repository structure unavailable": DevelopmentEvidenceGapCodeV1.REPOSITORY_STRUCTURE_UNAVAILABLE,
    "full repository file inventory unavailable": DevelopmentEvidenceGapCodeV1.REPOSITORY_STRUCTURE_UNAVAILABLE,
    "module usage patterns unavailable": DevelopmentEvidenceGapCodeV1.MODULE_USAGE_PATTERNS_UNAVAILABLE,
    "complete file usage patterns unavailable": DevelopmentEvidenceGapCodeV1.MODULE_USAGE_PATTERNS_UNAVAILABLE,
    "non-import consumer evidence unavailable": DevelopmentEvidenceGapCodeV1.NON_IMPORT_CONSUMERS_UNAVAILABLE,
    "incomplete knowledge of non-import consumers": DevelopmentEvidenceGapCodeV1.NON_IMPORT_CONSUMERS_UNAVAILABLE,
    "test/dependency coverage incomplete": DevelopmentEvidenceGapCodeV1.TEST_DEPENDENCY_COVERAGE_INCOMPLETE,
    "insufficient evidence to prove safe cleanup candidates": DevelopmentEvidenceGapCodeV1.SAFE_CLEANUP_EVIDENCE_INSUFFICIENT,
    "insufficient evidence of unused/redundant files": DevelopmentEvidenceGapCodeV1.SAFE_CLEANUP_EVIDENCE_INSUFFICIENT,
}
_REPOSITORY_REQUIRED_GAPS = frozenset(
    {
        DevelopmentEvidenceGapCodeV1.REPOSITORY_STRUCTURE_UNAVAILABLE,
        DevelopmentEvidenceGapCodeV1.MODULE_USAGE_PATTERNS_UNAVAILABLE,
        DevelopmentEvidenceGapCodeV1.NON_IMPORT_CONSUMERS_UNAVAILABLE,
        DevelopmentEvidenceGapCodeV1.SAFE_CLEANUP_EVIDENCE_INSUFFICIENT,
    }
)


@dataclass(frozen=True, slots=True)
class DevelopmentContextRequirementV1:
    capability: DevelopmentContextCapabilityV1
    blocker_code: str
    evidence_gap_codes: tuple[DevelopmentEvidenceGapCodeV1, ...]
    blocked_acceptance_criteria: tuple[str, ...]

    def payload(self) -> dict[str, object]:
        return {
            "identity": DEVELOPMENT_CONTEXT_REQUIREMENT_IDENTITY,
            "capability": self.capability.value,
            "blocker_code": self.blocker_code,
            "evidence_gap_codes": [item.value for item in self.evidence_gap_codes],
            "blocked_acceptance_criteria": list(self.blocked_acceptance_criteria),
        }


@dataclass(frozen=True, slots=True)
class RepositoryInventoryEvidenceGroupV1:
    handle: str
    records: tuple[tuple[object, ...], ...]
    group_digest: str

    def payload(self) -> dict[str, object]:
        return {
            "handle": self.handle,
            "records": [list(item) for item in self.records],
            "group_digest": self.group_digest,
        }


@dataclass(frozen=True, slots=True)
class DevelopmentRepositoryInventoryAttachmentV1:
    source_commit: str
    inventory_identity: str
    inventory_digest: str
    policy_identity: str
    policy_digest: str
    knowledge_identity: str
    knowledge_manifest_digest: str
    original_evidence_digest: str
    original_context_digest: str
    completeness: str
    taxonomy_counts: tuple[tuple[str, int], ...]
    coverage_counts: tuple[tuple[str, int], ...]
    owner_table: tuple[str, ...]
    evidence_groups: tuple[RepositoryInventoryEvidenceGroupV1, ...]
    attachment_digest: str

    def payload(self, *, include_digest: bool = True) -> dict[str, object]:
        value = {
            "identity": REPOSITORY_INVENTORY_ATTACHMENT_IDENTITY,
            "source_commit": self.source_commit,
            "inventory_identity": self.inventory_identity,
            "inventory_digest": self.inventory_digest,
            "policy_identity": self.policy_identity,
            "policy_digest": self.policy_digest,
            "knowledge_identity": self.knowledge_identity,
            "knowledge_manifest_digest": self.knowledge_manifest_digest,
            "original_evidence_digest": self.original_evidence_digest,
            "original_context_digest": self.original_context_digest,
            "completeness": self.completeness,
            "taxonomy_counts": dict(self.taxonomy_counts),
            "coverage_counts": dict(self.coverage_counts),
            "owner_table": list(self.owner_table),
            "file_record_shape": [
                "path",
                "kind",
                "owner",
                "artifact",
                "coverage",
                "out",
                "in",
                "relation_kinds",
                "knowledge_source",
            ],
            "artifact_codes": _ARTIFACT_CODES,
            "coverage_codes": _COVERAGE_CODES,
            "relation_codes": _RELATION_CODES,
            "evidence_groups": [item.payload() for item in self.evidence_groups],
        }
        if include_digest:
            value["attachment_digest"] = self.attachment_digest
        return value


class RepositoryInventoryIntegrationError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def development_context_requirement(analysis: object) -> DevelopmentContextRequirementV1 | None:
    """Derive a capability only from the complete exact blocker-fact set."""
    gaps = {
        code
        for item in getattr(analysis, "unknowns", ())
        if (code := _GAP_FACTS.get(str(item).strip().casefold().rstrip("."))) is not None
    }
    if not _REPOSITORY_REQUIRED_GAPS.issubset(gaps):
        return None
    return DevelopmentContextRequirementV1(
        DevelopmentContextCapabilityV1.REPOSITORY_INVENTORY,
        "REPOSITORY_INVENTORY_REQUIRED",
        tuple(sorted(gaps, key=lambda item: item.value)),
        tuple(getattr(analysis, "acceptance_criteria", ())),
    )


def build_repository_inventory_attachment(
    analysis: object, inventory: RepositoryInventoryV1
) -> DevelopmentRepositoryInventoryAttachmentV1:
    evidence = getattr(analysis, "evidence", None)
    context_map = getattr(analysis, "context_map", None)
    _validate_inventory(evidence, inventory)
    if context_map is None or not getattr(context_map, "context_digest", None):
        raise RepositoryInventoryIntegrationError("ORIGINAL_CONTEXT_BINDING_MISSING")
    outgoing = Counter(item.source_path for item in inventory.relations)
    incoming = Counter(
        item.target_ref for item in inventory.relations if item.target_kind.value == "FILE"
    )
    kinds: dict[str, set[str]] = defaultdict(set)
    knowledge_paths = set()
    for relation in inventory.relations:
        kinds[relation.source_path].add(relation.relation_kind.value)
        if relation.relation_kind is RepositoryRelationKindV1.KNOWLEDGE_SOURCE:
            knowledge_paths.add(relation.source_path)
    owner_table = tuple(
        sorted({item.canonical_owner for item in inventory.files if item.canonical_owner})
    )
    owner_refs = {owner: index for index, owner in enumerate(owner_table, start=1)}
    rows = []
    for item in inventory.files:
        rows.append(
            (
                item.path,
                item.file_kind.value,
                owner_refs.get(item.canonical_owner, 0),
                _ARTIFACT_CODES[item.artifact_classification.value],
                _COVERAGE_CODES[item.reference_coverage.value],
                outgoing[item.path],
                incoming[item.path],
                ",".join(_RELATION_CODES[value] for value in sorted(kinds[item.path])),
                item.path in knowledge_paths,
            )
        )
    groups = tuple(
        RepositoryInventoryEvidenceGroupV1(
            f"RI{index + 1}",
            tuple(rows[offset : offset + MAX_ATTACHMENT_GROUP_FILES]),
            digest(rows[offset : offset + MAX_ATTACHMENT_GROUP_FILES]),
        )
        for index, offset in enumerate(range(0, len(rows), MAX_ATTACHMENT_GROUP_FILES))
    )
    taxonomy = tuple(sorted(Counter(item.file_kind.value for item in inventory.files).items()))
    raw = {
        "source_commit": inventory.source_commit,
        "inventory_identity": inventory.identity,
        "inventory_digest": inventory.inventory_digest,
        "policy_identity": inventory.policy_identity,
        "policy_digest": inventory.policy_digest,
        "knowledge_identity": inventory.knowledge_identity,
        "knowledge_manifest_digest": inventory.knowledge_manifest_digest,
        "original_evidence_digest": evidence.evidence_digest,
        "original_context_digest": context_map.context_digest,
        "completeness": inventory.completeness.value,
        "taxonomy_counts": taxonomy,
        "coverage_counts": tuple(sorted(inventory.coverage.items())),
        "owner_table": owner_table,
        "evidence_groups": groups,
    }
    provisional = DevelopmentRepositoryInventoryAttachmentV1(**raw, attachment_digest="0" * 64)
    attachment = replace(
        provisional, attachment_digest=digest(provisional.payload(include_digest=False))
    )
    if len(canonical_json(attachment.payload())) > MAX_REPOSITORY_INVENTORY_ATTACHMENT_CHARACTERS:
        raise RepositoryInventoryIntegrationError("REPOSITORY_INVENTORY_ATTACHMENT_BUDGET_EXCEEDED")
    return attachment


def analyze_development_with_repository_inventory(
    service: object,
    request: object,
    original_analysis: object,
    inventory: RepositoryInventoryV1,
):
    requirement = development_context_requirement(original_analysis)
    if (
        requirement is None
        or requirement.capability is not DevelopmentContextCapabilityV1.REPOSITORY_INVENTORY
    ):
        raise RepositoryInventoryIntegrationError("REPOSITORY_INVENTORY_NOT_REQUIRED")
    attachment = build_repository_inventory_attachment(original_analysis, inventory)
    return service.analyze(replace(request, repository_inventory_attachment=attachment))


def _validate_inventory(evidence: object, inventory: RepositoryInventoryV1) -> None:
    if evidence is None:
        raise RepositoryInventoryIntegrationError("ORIGINAL_EVIDENCE_BINDING_MISSING")
    checks = (
        (inventory.state is RepositoryInventoryStateV1.COMPLETE, "REPOSITORY_INVENTORY_INCOMPLETE"),
        (
            inventory.completeness is RepositoryInventoryCompletenessV1.COMPLETE,
            "REPOSITORY_INVENTORY_INCOMPLETE",
        ),
        (inventory.inventory_digest is not None, "REPOSITORY_INVENTORY_DIGEST_MISSING"),
        (inventory.repository_clean, "REPOSITORY_INVENTORY_DIRTY"),
        (inventory.source_commit == evidence.source_commit, "REPOSITORY_INVENTORY_SOURCE_MISMATCH"),
        (
            inventory.knowledge_identity == evidence.knowledge_identity,
            "REPOSITORY_INVENTORY_KNOWLEDGE_MISMATCH",
        ),
        (
            inventory.knowledge_manifest_digest == evidence.knowledge_manifest_digest,
            "REPOSITORY_INVENTORY_KNOWLEDGE_MISMATCH",
        ),
        (
            inventory.identity == REPOSITORY_INVENTORY_IDENTITY,
            "REPOSITORY_INVENTORY_CONTRACT_UNSUPPORTED",
        ),
        (
            inventory.policy_identity == REPOSITORY_INVENTORY_POLICY_IDENTITY,
            "REPOSITORY_INVENTORY_POLICY_UNSUPPORTED",
        ),
        (inventory.policy_digest == POLICY_DIGEST, "REPOSITORY_INVENTORY_POLICY_UNSUPPORTED"),
    )
    for valid, code in checks:
        if not valid:
            raise RepositoryInventoryIntegrationError(code)
