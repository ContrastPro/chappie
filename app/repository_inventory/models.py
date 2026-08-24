"""Bounded, immutable contracts for repository structural facts."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath

REPOSITORY_INVENTORY_IDENTITY = "chappie_repository_inventory/1.0.0"
REPOSITORY_INVENTORY_REQUEST_IDENTITY = "chappie_repository_inventory_request/1.0.0"
REPOSITORY_INVENTORY_POLICY_IDENTITY = "chappie_repository_inventory_policy/1.0.2"

SAFE_ENV_TEMPLATE_NAMES = (".env.example", ".env.sample", ".env.template")
FORBIDDEN_LOCAL_STATE_ROOTS = ("cache", "data", "logs", "runtime", "tmp", "var")
FORBIDDEN_LOCAL_STATE_SUFFIXES = (".db", ".log", ".sqlite", ".sqlite3")
FORBIDDEN_CREDENTIAL_PARTS = ("credentials", "secrets", "tokens")
FORBIDDEN_PRIVATE_KEY_SUFFIXES = (".key", ".p12", ".pem", ".pfx")
FORBIDDEN_PRIVATE_KEY_NAMES = ("id_dsa", "id_ecdsa", "id_ed25519", "id_rsa")
APPROVED_TRACKED_FIXTURE_EVIDENCE = (
    (
        "tests/fixtures/m4_10_recovery_proof_v6.sqlite3",
        "tests/test_m410_recovery_proof.py",
    ),
)

MAX_TRACKED_FILES = 2048
MAX_RELATIONS = 8192
MAX_REFERENCES_PER_FILE = 64
MAX_DECLARED_SYMBOLS = 4096
MAX_DECLARED_SYMBOLS_PER_FILE = 64
MAX_PATH_CHARACTERS = 512
MAX_IDENTIFIER_CHARACTERS = 256
MAX_LOCATOR_CHARACTERS = 256
MAX_PARSEABLE_FILE_BYTES = 524288
MAX_SINGLE_FILE_BYTES = 8388608
MAX_TOTAL_FILE_BYTES = 67108864
MAX_SEMANTIC_ARTIFACT_BYTES = 2097152
GIT_TIMEOUT_SECONDS = 5

_SHA256 = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def semantic_digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def safe_path(value: str) -> str:
    value = value.replace("\\", "/")
    path = PurePosixPath(value)
    if (
        not value
        or len(value) > MAX_PATH_CHARACTERS
        or path.is_absolute()
        or ".." in path.parts
        or value.startswith("./")
    ):
        raise ValueError("path must be a bounded repository-relative POSIX path")
    return path.as_posix()


def _bounded(value: str, limit: int, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ValueError(f"{label} is invalid")
    return value


def _digest(value: str | None, label: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return value


class RepositoryInventoryScopeV1(StrEnum):
    TRACKED_FILES = "TRACKED_FILES"


class RepositoryInventoryStateV1(StrEnum):
    COMPLETE = "COMPLETE"
    SOURCE_COMMIT_MISMATCH = "SOURCE_COMMIT_MISMATCH"
    REPOSITORY_NOT_CLEAN = "REPOSITORY_NOT_CLEAN"
    FILE_BUDGET_EXCEEDED = "FILE_BUDGET_EXCEEDED"
    BYTE_BUDGET_EXCEEDED = "BYTE_BUDGET_EXCEEDED"
    RELATION_BUDGET_EXCEEDED = "RELATION_BUDGET_EXCEEDED"
    SERIALIZATION_BUDGET_EXCEEDED = "SERIALIZATION_BUDGET_EXCEEDED"
    INVALID_REPOSITORY_METADATA = "INVALID_REPOSITORY_METADATA"
    VALIDATION_ERROR = "VALIDATION_ERROR"


class RepositoryInventoryCompletenessV1(StrEnum):
    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"


class RepositoryFileKindV1(StrEnum):
    SOURCE = "SOURCE"
    TEST = "TEST"
    DOC = "DOC"
    CONFIG = "CONFIG"
    TOOLING = "TOOLING"
    ACCEPTANCE = "ACCEPTANCE"
    EXTENSION = "EXTENSION"
    GENERATED = "GENERATED"
    DISTRIBUTION = "DISTRIBUTION"
    ARCHITECTURE = "ARCHITECTURE"
    OTHER = "OTHER"


class ArtifactClassificationV1(StrEnum):
    DECLARED_SOURCE = "DECLARED_SOURCE"
    DECLARED_GENERATED = "DECLARED_GENERATED"
    DECLARED_DISTRIBUTION = "DECLARED_DISTRIBUTION"
    UNCLASSIFIED = "UNCLASSIFIED"


class ReferenceCoverageV1(StrEnum):
    COMPLETE = "COMPLETE"
    NO_KNOWN_REFERENCE = "NO_KNOWN_REFERENCE"
    UNSUPPORTED_FILE_KIND = "UNSUPPORTED_FILE_KIND"
    PARSE_FAILED = "PARSE_FAILED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class RepositoryRelationKindV1(StrEnum):
    IMPORTS = "IMPORTS"
    REFERENCES = "REFERENCES"
    TESTS = "TESTS"
    USES_FIXTURE = "USES_FIXTURE"
    EXPORTS = "EXPORTS"
    DECLARES = "DECLARES"
    BOOTSTRAPS = "BOOTSTRAPS"
    GENERATES = "GENERATES"
    PACKAGES = "PACKAGES"
    DISTRIBUTES = "DISTRIBUTES"
    CHECKSUMS = "CHECKSUMS"
    DOCUMENTS = "DOCUMENTS"
    KNOWLEDGE_SOURCE = "KNOWLEDGE_SOURCE"
    OWNED_BY = "OWNED_BY"


class RepositoryTargetKindV1(StrEnum):
    FILE = "FILE"
    MODULE = "MODULE"
    SYMBOL = "SYMBOL"
    CONTRACT = "CONTRACT"
    SCRIPT = "SCRIPT"
    OWNER = "OWNER"
    KNOWLEDGE_RECORD = "KNOWLEDGE_RECORD"


@dataclass(frozen=True, slots=True)
class RepositoryInventoryRequestV1:
    source_commit: str
    requested_relation_kinds: tuple[RepositoryRelationKindV1, ...]
    requesting_context_digest: str
    knowledge_identity: str
    knowledge_manifest_digest: str
    identity: str = REPOSITORY_INVENTORY_REQUEST_IDENTITY
    require_clean_repository: bool = True
    scope: RepositoryInventoryScopeV1 = RepositoryInventoryScopeV1.TRACKED_FILES
    required_capability: str = "REPOSITORY_INVENTORY"
    policy_identity: str = REPOSITORY_INVENTORY_POLICY_IDENTITY

    def __post_init__(self) -> None:
        if self.identity != REPOSITORY_INVENTORY_REQUEST_IDENTITY:
            raise ValueError("request identity mismatch")
        if not _COMMIT.fullmatch(self.source_commit):
            raise ValueError("source_commit is invalid")
        if self.scope is not RepositoryInventoryScopeV1.TRACKED_FILES:
            raise ValueError("only TRACKED_FILES is supported")
        if self.required_capability != "REPOSITORY_INVENTORY":
            raise ValueError("required capability mismatch")
        _bounded(self.policy_identity, MAX_IDENTIFIER_CHARACTERS, "policy identity")
        _bounded(self.knowledge_identity, MAX_IDENTIFIER_CHARACTERS, "knowledge identity")
        _digest(self.requesting_context_digest, "requesting context digest")
        _digest(self.knowledge_manifest_digest, "knowledge manifest digest")
        if not all(
            isinstance(item, RepositoryRelationKindV1) for item in self.requested_relation_kinds
        ):
            raise ValueError("requested relation kind is invalid")
        object.__setattr__(
            self, "requested_relation_kinds", tuple(sorted(set(self.requested_relation_kinds)))
        )


@dataclass(frozen=True, slots=True)
class RepositoryDeclaredSymbolV1:
    path: str
    name: str
    symbol_kind: str
    locator: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", safe_path(self.path))
        _bounded(self.name, MAX_IDENTIFIER_CHARACTERS, "symbol name")
        _bounded(self.symbol_kind, MAX_IDENTIFIER_CHARACTERS, "symbol kind")
        _bounded(self.locator, MAX_LOCATOR_CHARACTERS, "symbol locator")

    def payload(self) -> dict[str, str]:
        return {
            "path": self.path,
            "name": self.name,
            "symbol_kind": self.symbol_kind,
            "locator": self.locator,
        }


@dataclass(frozen=True, slots=True)
class RepositoryDirectRelationV1:
    relation_kind: RepositoryRelationKindV1
    source_path: str
    target_kind: RepositoryTargetKindV1
    target_ref: str
    evidence_kind: str
    evidence_locator: str
    source_content_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_path", safe_path(self.source_path))
        _bounded(self.target_ref, MAX_IDENTIFIER_CHARACTERS, "target ref")
        _bounded(self.evidence_kind, MAX_IDENTIFIER_CHARACTERS, "evidence kind")
        _bounded(self.evidence_locator, MAX_LOCATOR_CHARACTERS, "evidence locator")
        _digest(self.source_content_digest, "source content digest")

    def payload(self) -> dict[str, str]:
        return {
            "relation_kind": self.relation_kind.value,
            "source_path": self.source_path,
            "target_kind": self.target_kind.value,
            "target_ref": self.target_ref,
            "evidence_kind": self.evidence_kind,
            "evidence_locator": self.evidence_locator,
            "source_content_digest": self.source_content_digest,
        }


@dataclass(frozen=True, slots=True)
class RepositoryFileRecordV1:
    path: str
    file_kind: RepositoryFileKindV1
    tracking_state: str
    content_digest: str
    size_bytes: int
    canonical_owner: str | None
    owner_evidence: str | None
    module_identity: str | None
    artifact_classification: ArtifactClassificationV1
    reference_coverage: ReferenceCoverageV1

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", safe_path(self.path))
        if self.tracking_state != "TRACKED" or self.size_bytes < 0:
            raise ValueError("file record must describe a tracked file")
        _digest(self.content_digest, "content digest")
        for value in (self.canonical_owner, self.owner_evidence, self.module_identity):
            if value is not None:
                _bounded(value, MAX_IDENTIFIER_CHARACTERS, "file metadata")

    def payload(self) -> dict[str, object]:
        return {
            "path": self.path,
            "file_kind": self.file_kind.value,
            "tracking_state": self.tracking_state,
            "content_digest": self.content_digest,
            "size_bytes": self.size_bytes,
            "canonical_owner": self.canonical_owner,
            "owner_evidence": self.owner_evidence,
            "module_identity": self.module_identity,
            "artifact_classification": self.artifact_classification.value,
            "reference_coverage": self.reference_coverage.value,
        }


@dataclass(frozen=True, slots=True)
class KnowledgeSourceMetadataV1:
    record_id: str
    record_digest: str
    source_path: str

    def __post_init__(self) -> None:
        _bounded(self.record_id, MAX_IDENTIFIER_CHARACTERS, "record id")
        _digest(self.record_digest, "record digest")
        object.__setattr__(self, "source_path", safe_path(self.source_path))


@dataclass(frozen=True, slots=True)
class RepositoryInventoryV1:
    state: RepositoryInventoryStateV1
    completeness: RepositoryInventoryCompletenessV1
    reason_code: str | None
    source_commit: str
    repository_clean: bool
    scope: RepositoryInventoryScopeV1
    policy_identity: str
    policy_digest: str
    knowledge_identity: str
    knowledge_manifest_digest: str
    files: tuple[RepositoryFileRecordV1, ...]
    declared_symbols: tuple[RepositoryDeclaredSymbolV1, ...]
    relations: tuple[RepositoryDirectRelationV1, ...]
    coverage: dict[str, int]
    counts: dict[str, int]
    excluded_scopes: tuple[str, ...]
    file_set_completeness: RepositoryInventoryCompletenessV1
    relation_coverage: RepositoryInventoryCompletenessV1
    inventory_digest: str | None
    identity: str = REPOSITORY_INVENTORY_IDENTITY

    def __post_init__(self) -> None:
        if (self.state is RepositoryInventoryStateV1.COMPLETE) != (
            self.inventory_digest is not None
        ):
            raise ValueError("only COMPLETE inventory may carry a digest")
        if (
            self.state is RepositoryInventoryStateV1.COMPLETE
            and self.completeness is not RepositoryInventoryCompletenessV1.COMPLETE
        ):
            raise ValueError("COMPLETE state requires complete evidence")
        if self.state is RepositoryInventoryStateV1.COMPLETE and (
            self.file_set_completeness is not RepositoryInventoryCompletenessV1.COMPLETE
            or self.relation_coverage is not RepositoryInventoryCompletenessV1.COMPLETE
        ):
            raise ValueError("COMPLETE state requires complete file and relation coverage")
        _digest(self.policy_digest, "policy digest")
        _digest(self.knowledge_manifest_digest, "knowledge manifest digest")
        _digest(self.inventory_digest, "inventory digest", nullable=True)

    def semantic_payload(self, *, include_digest: bool = True) -> dict[str, object]:
        value = {
            "identity": self.identity,
            "state": self.state.value,
            "completeness": self.completeness.value,
            "reason_code": self.reason_code,
            "source_commit": self.source_commit,
            "repository_clean": self.repository_clean,
            "scope": self.scope.value,
            "policy_identity": self.policy_identity,
            "policy_digest": self.policy_digest,
            "knowledge_identity": self.knowledge_identity,
            "knowledge_manifest_digest": self.knowledge_manifest_digest,
            "files": [item.payload() for item in self.files],
            "declared_symbols": [item.payload() for item in self.declared_symbols],
            "relations": [item.payload() for item in self.relations],
            "coverage": dict(sorted(self.coverage.items())),
            "counts": dict(sorted(self.counts.items())),
            "excluded_scopes": list(self.excluded_scopes),
            "file_set_completeness": self.file_set_completeness.value,
            "relation_coverage": self.relation_coverage.value,
        }
        if include_digest:
            value["inventory_digest"] = self.inventory_digest
        return value


def repository_inventory_policy_payload() -> dict[str, object]:
    return {
        "identity": REPOSITORY_INVENTORY_POLICY_IDENTITY,
        "limits": {
            name: value
            for name, value in globals().items()
            if name.startswith("MAX_") or name == "GIT_TIMEOUT_SECONDS"
        },
        "scope": "TRACKED_FILES",
        "relations": [item.value for item in RepositoryRelationKindV1],
        "path_policy": {
            "safe_env_templates": SAFE_ENV_TEMPLATE_NAMES,
            "forbidden_local_state_roots": FORBIDDEN_LOCAL_STATE_ROOTS,
            "forbidden_local_state_suffixes": FORBIDDEN_LOCAL_STATE_SUFFIXES,
            "forbidden_credential_parts": FORBIDDEN_CREDENTIAL_PARTS,
            "forbidden_private_key_suffixes": FORBIDDEN_PRIVATE_KEY_SUFFIXES,
            "forbidden_private_key_names": FORBIDDEN_PRIVATE_KEY_NAMES,
            "approved_tracked_fixture_evidence": APPROVED_TRACKED_FIXTURE_EVIDENCE,
            "env_rule": "EXACT_TEMPLATE_ALLOWLIST_OTHER_ENV_NAMES_FORBIDDEN",
            "root_rule": "REPOSITORY_ROOT_ONLY",
        },
    }


POLICY_DIGEST = semantic_digest(repository_inventory_policy_payload())
