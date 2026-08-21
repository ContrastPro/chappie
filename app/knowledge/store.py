"""Deterministic, provenance-bound repository knowledge; never model memory."""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path

from app.ai.schemas import canonical_json, digest

KNOWLEDGE_IDENTITY = "chappie_knowledge/1.0.0"
KNOWLEDGE_CONTRACT_DIGEST = digest(
    {
        "identity": KNOWLEDGE_IDENTITY,
        "types": [
            "ARCHITECTURE",
            "MODULE_RESPONSIBILITY",
            "AUTHORITY_BOUNDARY",
            "CONTRACT",
            "RESEARCH_RESULT",
            "DECISION",
            "LIMITATION",
            "BACKLOG",
        ],
        "statuses": ["ACTIVE", "FROZEN", "HISTORICAL", "SUPERSEDED", "REJECTED", "PLANNED"],
        "authorities": [
            "SOURCE_CODE",
            "VERSIONED_CONTRACT",
            "VERIFIED_RESEARCH_EVIDENCE",
            "PROJECT_DECISION",
            "DOCUMENTATION",
            "GENERATED_SUMMARY",
        ],
    }
)
KNOWLEDGE_STORE_PATH = Path("data/knowledge/chappie_knowledge_v1.json")
SOURCE_ALLOWLIST = frozenset(
    {
        "docs/research/README.md",
        "docs/research/CHAPPIE_AI.md",
        "docs/research/DATASETS.md",
        "docs/research/NEWS_EVENT_INTELLIGENCE.md",
        "app/research_methodology.py",
        "app/research/intelligence/universe.py",
        "app/research/intelligence/multi_asset_flow.py",
        "app/ai/research/capabilities.py",
        "app/ai/research/evidence_review.py",
        "docs/control_plane/README.md",
        "docs/knowledge/README.md",
        "docs/platform/SETUP_CONTRACT.md",
        "VISION.md",
        "ARCHITECTURE.md",
    }
)


class KnowledgeTypeV1(StrEnum):
    ARCHITECTURE = "ARCHITECTURE"
    MODULE_RESPONSIBILITY = "MODULE_RESPONSIBILITY"
    AUTHORITY_BOUNDARY = "AUTHORITY_BOUNDARY"
    CONTRACT = "CONTRACT"
    RESEARCH_RESULT = "RESEARCH_RESULT"
    DECISION = "DECISION"
    LIMITATION = "LIMITATION"
    BACKLOG = "BACKLOG"


class KnowledgeStatusV1(StrEnum):
    ACTIVE = "ACTIVE"
    FROZEN = "FROZEN"
    HISTORICAL = "HISTORICAL"
    SUPERSEDED = "SUPERSEDED"
    REJECTED = "REJECTED"
    PLANNED = "PLANNED"


class KnowledgeAuthorityV1(StrEnum):
    SOURCE_CODE = "SOURCE_CODE"
    VERSIONED_CONTRACT = "VERSIONED_CONTRACT"
    VERIFIED_RESEARCH_EVIDENCE = "VERIFIED_RESEARCH_EVIDENCE"
    PROJECT_DECISION = "PROJECT_DECISION"
    DOCUMENTATION = "DOCUMENTATION"
    GENERATED_SUMMARY = "GENERATED_SUMMARY"


@dataclass(frozen=True, slots=True)
class RepositorySnapshotV1:
    commit: str
    clean: bool

    def __post_init__(self) -> None:
        if len(self.commit) < 7 or not all(
            char in "0123456789abcdef" for char in self.commit.lower()
        ):
            raise ValueError("repository snapshot requires a commit hash")


@dataclass(frozen=True, slots=True)
class KnowledgeSourceReferenceV1:
    source_type: KnowledgeAuthorityV1
    path: str
    content_digest: str
    snapshot_commit: str
    locator: str
    contract_or_evidence_identity: str | None = None

    def __post_init__(self) -> None:
        if (
            self.path not in SOURCE_ALLOWLIST
            or Path(self.path).is_absolute()
            or ".." in Path(self.path).parts
        ):
            raise ValueError("knowledge source path is not allowlisted")
        if len(self.content_digest) != 64 or not self.locator:
            raise ValueError("knowledge source requires digest and bounded locator")

    def payload(self) -> dict[str, object]:
        return {
            "source_type": self.source_type.value,
            "path": self.path,
            "content_digest": self.content_digest,
            "snapshot_commit": self.snapshot_commit,
            "locator": self.locator,
            "contract_or_evidence_identity": self.contract_or_evidence_identity,
        }


@dataclass(frozen=True, slots=True)
class KnowledgeRecordV1:
    knowledge_id: str
    knowledge_type: KnowledgeTypeV1
    title: str
    statement: str
    authority: KnowledgeAuthorityV1
    status: KnowledgeStatusV1
    sources: tuple[KnowledgeSourceReferenceV1, ...]
    tags: tuple[str, ...]
    related_record_ids: tuple[str, ...] = ()
    created_from: str = KNOWLEDGE_IDENTITY

    def __post_init__(self) -> None:
        if not self.title or not self.statement or not self.sources or not self.tags:
            raise ValueError("knowledge records require statement, sources, and bounded tags")
        expected = knowledge_id(
            self.knowledge_type,
            self.title,
            self.statement,
            self.authority,
            self.status,
            self.sources,
            self.tags,
            self.related_record_ids,
        )
        if self.knowledge_id != expected:
            raise ValueError("knowledge ID must be deterministic")

    @property
    def record_digest(self) -> str:
        return digest(self.payload())

    def payload(self) -> dict[str, object]:
        return {
            "knowledge_id": self.knowledge_id,
            "knowledge_type": self.knowledge_type.value,
            "title": self.title,
            "statement": self.statement,
            "authority": self.authority.value,
            "status": self.status.value,
            "sources": [item.payload() for item in self.sources],
            "tags": list(self.tags),
            "related_record_ids": list(self.related_record_ids),
            "created_from": self.created_from,
        }


@dataclass(frozen=True, slots=True)
class KnowledgeConflictV1:
    record_ids: tuple[str, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class KnowledgeManifestV1:
    snapshot: RepositorySnapshotV1
    record_digests: tuple[str, ...]
    store_content_digest: str

    @property
    def manifest_digest(self) -> str:
        return digest(
            {
                "identity": KNOWLEDGE_IDENTITY,
                "contract_digest": KNOWLEDGE_CONTRACT_DIGEST,
                "snapshot": asdict(self.snapshot),
                "record_digests": self.record_digests,
                "store_content_digest": self.store_content_digest,
                "verification": "VERIFIED",
            }
        )

    def payload(self) -> dict[str, object]:
        return {
            "knowledge_identity": KNOWLEDGE_IDENTITY,
            "knowledge_contract_digest": KNOWLEDGE_CONTRACT_DIGEST,
            "snapshot": asdict(self.snapshot),
            "record_count": len(self.record_digests),
            "record_digests": list(self.record_digests),
            "store_content_digest": self.store_content_digest,
            "verification": "VERIFIED",
            "manifest_digest": self.manifest_digest,
        }


@dataclass(frozen=True, slots=True)
class KnowledgeContextRecordV1:
    knowledge_id: str
    knowledge_type: KnowledgeTypeV1
    title: str
    statement: str
    authority: KnowledgeAuthorityV1
    status: KnowledgeStatusV1
    sources: tuple[KnowledgeSourceReferenceV1, ...]
    related_record_ids: tuple[str, ...]
    record_digest: str


@dataclass(frozen=True, slots=True)
class KnowledgeContextV1:
    records: tuple[KnowledgeContextRecordV1, ...]
    retrieval_identity: str
    knowledge_manifest_digest: str
    conflict_record_ids: tuple[tuple[str, ...], ...]
    context_digest: str

    @property
    def knowledge_digest(self) -> str:
        return self.context_digest


@dataclass(frozen=True, slots=True)
class KnowledgeCandidateV1:
    """Future AI proposal shape only; it has no write path in Phase 1."""

    statement: str


def knowledge_id(kind, title, statement, authority, status, sources, tags, related) -> str:
    return (
        "knowledge-"
        + digest(
            {
                "type": kind.value,
                "title": title,
                "statement": statement,
                "authority": authority.value,
                "status": status.value,
                "sources": [item.payload() for item in sources],
                "tags": list(tags),
                "related": list(related),
            }
        )[:24]
    )


def committed_snapshot(repo: Path = Path(".")) -> RepositorySnapshotV1:
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    clean = not subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=repo, text=True
    ).strip()
    return RepositorySnapshotV1(commit, clean)


def _source(
    root: Path,
    snapshot: RepositorySnapshotV1,
    path: str,
    authority,
    locator: str,
    identity: str | None = None,
) -> KnowledgeSourceReferenceV1:
    content = (root / path).read_bytes()
    return KnowledgeSourceReferenceV1(
        authority, path, digest(content.decode("utf-8")), snapshot.commit, locator, identity
    )


def initial_records(
    root: Path = Path("."), snapshot: RepositorySnapshotV1 | None = None
) -> tuple[KnowledgeRecordV1, ...]:
    snapshot = snapshot or committed_snapshot(root)
    if not snapshot.clean:
        raise ValueError("REFUSE_CANONICAL_INGESTION: repository working tree is dirty")
    definitions = (
        (
            KnowledgeTypeV1.ARCHITECTURE,
            "Chappie AI provider abstraction",
            "Chappie AI uses a provider abstraction independent of any model backend.",
            KnowledgeAuthorityV1.DOCUMENTATION,
            KnowledgeStatusV1.ACTIVE,
            "docs/research/CHAPPIE_AI.md",
            "Provider abstraction",
            None,
            ("ai", "architecture"),
        ),
        (
            KnowledgeTypeV1.AUTHORITY_BOUNDARY,
            "RDE evidence authority",
            "RDE evidence is immutable authority; AI cannot rewrite evidence or EvidenceRelation.",
            KnowledgeAuthorityV1.VERSIONED_CONTRACT,
            KnowledgeStatusV1.FROZEN,
            "app/ai/research/evidence_review.py",
            "EvidenceRelationV1",
            "research_evidence_critique/1.1.0",
            ("ai", "authority", "evidence", "rde", "research", "verified"),
        ),
        (
            KnowledgeTypeV1.CONTRACT,
            "Compact matched-event methodology",
            "compact_matched_event_research/1.0.0 is the frozen matched-event research methodology.",
            KnowledgeAuthorityV1.VERSIONED_CONTRACT,
            KnowledgeStatusV1.FROZEN,
            "app/research_methodology.py",
            "METHODOLOGY_NAME",
            "compact_matched_event_research/1.0.0",
            ("research", "methodology"),
        ),
        (
            KnowledgeTypeV1.RESEARCH_RESULT,
            "Multi-asset flow result",
            "MULTI_ASSET_ADDS_NO_CLEAR_INFORMATION is the recorded multi-asset flow research verdict.",
            KnowledgeAuthorityV1.VERIFIED_RESEARCH_EVIDENCE,
            KnowledgeStatusV1.HISTORICAL,
            "app/research/intelligence/multi_asset_flow.py",
            "summary verdict",
            "MULTI_ASSET_ADDS_NO_CLEAR_INFORMATION",
            ("research", "multi-asset"),
        ),
        (
            KnowledgeTypeV1.DECISION,
            "Derivatives expansion decision",
            "DO_NOT_EXPAND_DERIVATIVES_YET is the project decision following the multi-asset result.",
            KnowledgeAuthorityV1.PROJECT_DECISION,
            KnowledgeStatusV1.ACTIVE,
            "app/research/intelligence/multi_asset_flow.py",
            "remaining derivatives recommendation",
            "DO_NOT_EXPAND_DERIVATIVES_YET",
            ("decision", "derivatives"),
        ),
        (
            KnowledgeTypeV1.LIMITATION,
            "Generic OI predicate limitation",
            "Generic OI_STATE predicates require separately validated methodology before autonomous use.",
            KnowledgeAuthorityV1.DOCUMENTATION,
            KnowledgeStatusV1.ACTIVE,
            "docs/research/CHAPPIE_AI.md",
            "Current local calibration evidence",
            None,
            ("limitation", "predicate"),
        ),
        (
            KnowledgeTypeV1.LIMITATION,
            "AI confidence limitation",
            "qwen3:8b confidence is not execution authority or calibrated probability.",
            KnowledgeAuthorityV1.DOCUMENTATION,
            KnowledgeStatusV1.ACTIVE,
            "docs/research/CHAPPIE_AI.md",
            "Current local calibration evidence",
            None,
            ("ai", "limitation"),
        ),
        (
            KnowledgeTypeV1.BACKLOG,
            "Semantic retrieval backlog",
            "Chappie Knowledge semantic retrieval is planned and not implemented in Phase 1.",
            KnowledgeAuthorityV1.PROJECT_DECISION,
            KnowledgeStatusV1.PLANNED,
            "docs/research/CHAPPIE_AI.md",
            "Current local calibration evidence",
            None,
            ("knowledge", "backlog"),
        ),
        (
            KnowledgeTypeV1.ARCHITECTURE,
            "Chappie Control Plane",
            "Chappie Control Plane is the frontend-neutral local application boundary that projects bounded status, diagnostics, capabilities, and read-only Project QA.",
            KnowledgeAuthorityV1.DOCUMENTATION,
            KnowledgeStatusV1.ACTIVE,
            "docs/control_plane/README.md",
            "Chappie Local Control Plane v1",
            "chappie_control_plane/1.2.0",
            (
                "application",
                "boundary",
                "capabilities",
                "chappie",
                "control",
                "plane",
                "project",
                "qa",
                "status",
            ),
        ),
        (
            KnowledgeTypeV1.MODULE_RESPONSIBILITY,
            "Project QA",
            "Project QA answers one bounded project question only from selected canonical Knowledge facts and has read-only application authority.",
            KnowledgeAuthorityV1.DOCUMENTATION,
            KnowledgeStatusV1.ACTIVE,
            "docs/knowledge/README.md",
            "Project QA",
            "PROJECT_QA_V1",
            ("bounded", "grounded", "knowledge", "project", "qa", "read-only"),
        ),
        (
            KnowledgeTypeV1.ARCHITECTURE,
            "Knowledge Store",
            "Chappie Knowledge Store is a deterministic, rebuildable index of concise canonical project facts, contracts, decisions, research conclusions, limitations, and backlog.",
            KnowledgeAuthorityV1.DOCUMENTATION,
            KnowledgeStatusV1.ACTIVE,
            "docs/knowledge/README.md",
            "Chappie Knowledge v1",
            KNOWLEDGE_IDENTITY,
            ("canonical", "deterministic", "facts", "knowledge", "store"),
        ),
        (
            KnowledgeTypeV1.ARCHITECTURE,
            "Trading Agent",
            "Trading Agent is a future bounded role that applies approved decisions through the runtime and has no direct order authority.",
            KnowledgeAuthorityV1.DOCUMENTATION,
            KnowledgeStatusV1.PLANNED,
            "VISION.md",
            "Direction",
            None,
            ("agent", "approved", "future", "runtime", "trading"),
        ),
        (
            KnowledgeTypeV1.MODULE_RESPONSIBILITY,
            "Setup Engine",
            "SetupEngineV1 owns the cross-platform setup lifecycle: check, decide, authorize required external work, apply, verify, bind, smoke, and ready.",
            KnowledgeAuthorityV1.VERSIONED_CONTRACT,
            KnowledgeStatusV1.ACTIVE,
            "docs/platform/SETUP_CONTRACT.md",
            "Lifecycle",
            "chappie_platform_setup/1.0.0",
            ("apply", "authorize", "bind", "engine", "setup", "smoke", "verify"),
        ),
        (
            KnowledgeTypeV1.ARCHITECTURE,
            "Chappie Architecture",
            "Chappie is a modular in-process application whose active MVP composes market, trading-decision, runtime, risk, Paper trading, persistence, exchange-execution, operations, and offline research boundaries.",
            KnowledgeAuthorityV1.DOCUMENTATION,
            KnowledgeStatusV1.ACTIVE,
            "ARCHITECTURE.md",
            "Active MVP",
            None,
            ("application", "architecture", "boundaries", "modules", "mvp", "system"),
        ),
    )
    records = []
    for kind, title, statement, authority, status, path, locator, identity, tags in definitions:
        source = _source(root, snapshot, path, authority, locator, identity)
        records.append(
            KnowledgeRecordV1(
                knowledge_id(kind, title, statement, authority, status, (source,), tags, ()),
                kind,
                title,
                statement,
                authority,
                status,
                (source,),
                tags,
            )
        )
    return tuple(records)


def manifest(
    records: tuple[KnowledgeRecordV1, ...], snapshot: RepositorySnapshotV1
) -> KnowledgeManifestV1:
    content = canonical_json([item.payload() for item in records])
    return KnowledgeManifestV1(
        snapshot, tuple(item.record_digest for item in records), digest(content)
    )


def verify_knowledge_store(
    records: tuple[KnowledgeRecordV1, ...], value: KnowledgeManifestV1
) -> tuple[KnowledgeConflictV1, ...]:
    if (
        len({item.knowledge_id for item in records}) != len(records)
        or tuple(item.record_digest for item in records) != value.record_digests
    ):
        raise ValueError("knowledge manifest or semantic identities are invalid")
    ids = {item.knowledge_id for item in records}
    if any(set(item.related_record_ids).difference(ids) for item in records):
        raise ValueError("knowledge record has dangling related IDs")
    active: dict[tuple[KnowledgeTypeV1, str], list[str]] = {}
    for item in records:
        if item.status == KnowledgeStatusV1.ACTIVE:
            active.setdefault((item.knowledge_type, item.title), []).append(item.knowledge_id)
    return tuple(
        KnowledgeConflictV1(tuple(ids), "multiple active records share type/title")
        for ids in active.values()
        if len(ids) > 1
    )


def detect_conflicts(records: tuple[KnowledgeRecordV1, ...]) -> tuple[KnowledgeConflictV1, ...]:
    active: dict[tuple[KnowledgeTypeV1, str], list[str]] = {}
    for item in records:
        if item.status == KnowledgeStatusV1.ACTIVE:
            active.setdefault((item.knowledge_type, item.title), []).append(item.knowledge_id)
    return tuple(
        KnowledgeConflictV1(tuple(ids), "multiple active records share type/title")
        for ids in active.values()
        if len(ids) > 1
    )


def write_store(
    path: Path, records: tuple[KnowledgeRecordV1, ...], value: KnowledgeManifestV1
) -> None:
    verify_knowledge_store(records, value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        canonical_json(
            {"manifest": value.payload(), "records": [item.payload() for item in records]}
        )
        + "\n",
        encoding="utf-8",
    )


def read_store(path: Path) -> tuple[tuple[KnowledgeRecordV1, ...], KnowledgeManifestV1]:
    """Load and verify an existing store without rebuilding canonical Knowledge."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        raw_manifest = value["manifest"]
        snapshot = RepositorySnapshotV1(**raw_manifest["snapshot"])
        records = tuple(_record_from_payload(item) for item in value["records"])
        computed = manifest(records, snapshot)
        if raw_manifest != computed.payload():
            raise ValueError("knowledge store manifest does not match its records")
        verify_knowledge_store(records, computed)
        return records, computed
    except (KeyError, OSError, TypeError, ValueError) as error:
        raise ValueError("knowledge store is invalid") from error


def _record_from_payload(value: dict[str, object]) -> KnowledgeRecordV1:
    sources = tuple(
        KnowledgeSourceReferenceV1(
            KnowledgeAuthorityV1(item["source_type"]),
            str(item["path"]),
            str(item["content_digest"]),
            str(item["snapshot_commit"]),
            str(item["locator"]),
            item.get("contract_or_evidence_identity"),
        )
        for item in value["sources"]
    )
    return KnowledgeRecordV1(
        str(value["knowledge_id"]),
        KnowledgeTypeV1(value["knowledge_type"]),
        str(value["title"]),
        str(value["statement"]),
        KnowledgeAuthorityV1(value["authority"]),
        KnowledgeStatusV1(value["status"]),
        sources,
        tuple(str(item) for item in value["tags"]),
        tuple(str(item) for item in value.get("related_record_ids", ())),
        str(value.get("created_from", KNOWLEDGE_IDENTITY)),
    )


def query(
    records: tuple[KnowledgeRecordV1, ...],
    *,
    knowledge_id: str | None = None,
    knowledge_type: KnowledgeTypeV1 | None = None,
    status: KnowledgeStatusV1 | None = None,
    tag: str | None = None,
    authority: KnowledgeAuthorityV1 | None = None,
) -> tuple[KnowledgeRecordV1, ...]:
    return tuple(
        item
        for item in records
        if (knowledge_id is None or item.knowledge_id == knowledge_id)
        and (knowledge_type is None or item.knowledge_type == knowledge_type)
        and (status is None or item.status == status)
        and (tag is None or tag in item.tags)
        and (authority is None or item.authority == authority)
    )


def context(records: tuple[KnowledgeRecordV1, ...]) -> KnowledgeContextV1:
    selected = tuple(
        KnowledgeContextRecordV1(
            item.knowledge_id,
            item.knowledge_type,
            item.title,
            item.statement,
            item.authority,
            item.status,
            item.sources,
            item.related_record_ids,
            item.record_digest,
        )
        for item in records
    )
    return KnowledgeContextV1(
        selected,
        "exact_filter/phase1",
        "UNBOUND",
        (),
        digest([item.record_digest for item in records]),
    )


def reject_ai_write(candidate: KnowledgeCandidateV1) -> None:
    raise PermissionError("AI candidates cannot write canonical knowledge in Phase 1")
