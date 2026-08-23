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
        "docs/architecture/CLEAN_ARCHITECTURE.md",
        "docs/architecture/DEPENDENCY_RULES.md",
        "docs/architecture/FEATURE_IMPLEMENTATION_RULES.md",
        "docs/architecture/PACKAGE_BOUNDARIES.md",
        "docs/architecture/ARCHITECTURE_DECISIONS.md",
        "docs/architecture/REFACTOR_FINAL_REPORT.md",
        "app/development_analysis/__init__.py",
        "app/development_analysis/models.py",
        "app/development_analysis/task.py",
        "app/control_plane/transport.py",
        "tests/test_development_analysis.py",
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
    # These are deliberate, source-backed architecture facts, not extracted Markdown
    # fragments.  Stable semantic titles keep identities independent of headings and
    # formatting edits in the governance documents.
    existing_ids = {record.title: record.knowledge_id for record in records}
    architecture_definitions = _architecture_record_definitions()
    for kind, title, statement, path, locator, tags, related_titles in architecture_definitions:
        source = _source(root, snapshot, path, KnowledgeAuthorityV1.DOCUMENTATION, locator)
        related = tuple(existing_ids[item] for item in related_titles)
        records.append(
            KnowledgeRecordV1(
                knowledge_id(
                    kind,
                    title,
                    statement,
                    KnowledgeAuthorityV1.DOCUMENTATION,
                    KnowledgeStatusV1.ACTIVE,
                    (source,),
                    tags,
                    related,
                ),
                kind,
                title,
                statement,
                KnowledgeAuthorityV1.DOCUMENTATION,
                KnowledgeStatusV1.ACTIVE,
                (source,),
                tags,
                related,
            )
        )
    existing_ids = {record.title: record.knowledge_id for record in records}
    for (
        kind,
        title,
        statement,
        path,
        locator,
        tags,
        related_titles,
    ) in _development_record_definitions():
        source = _source(root, snapshot, path, KnowledgeAuthorityV1.SOURCE_CODE, locator)
        related = tuple(existing_ids[item] for item in related_titles)
        records.append(
            KnowledgeRecordV1(
                knowledge_id(
                    kind,
                    title,
                    statement,
                    KnowledgeAuthorityV1.SOURCE_CODE,
                    KnowledgeStatusV1.ACTIVE,
                    (source,),
                    tags,
                    related,
                ),
                kind,
                title,
                statement,
                KnowledgeAuthorityV1.SOURCE_CODE,
                KnowledgeStatusV1.ACTIVE,
                (source,),
                tags,
                related,
            )
        )
        existing_ids[title] = records[-1].knowledge_id
    return tuple(records)


def _architecture_record_definitions() -> tuple[
    tuple[
        KnowledgeTypeV1,
        str,
        str,
        str,
        str,
        tuple[str, ...],
        tuple[str, ...],
    ],
    ...,
]:
    """Explicit canonical architecture facts; never AI-authored or chunk-derived."""

    source = "docs/architecture/CLEAN_ARCHITECTURE.md"
    package_responsibilities = (
        ("AI", "app.ai", "Provider-neutral structured reasoning and read-only assistance"),
        (
            "AI research",
            "app.ai.research",
            "Research reasoning, RDE query, and evidence review workflows",
        ),
        ("AI evaluation", "app.ai.evaluation", "Offline AI benchmarks and calibration"),
        (
            "Control Plane",
            "app.control_plane",
            "Read-only local application projection and loopback transport",
        ),
        ("Coordination", "app.coordination", "Cross-service trading workflow coordination"),
        (
            "Exchange execution",
            "app.exchange_execution",
            "Durable exchange execution lifecycle and reconciliation",
        ),
        (
            "Exchange runtime",
            "app.exchange_runtime",
            "Exchange-mode application services and composition",
        ),
        (
            "Execution",
            "app.execution",
            "Execution contracts, provider ports, and paper execution boundary",
        ),
        ("Foundation", "app.foundation", "Neutral configuration and logging primitives"),
        (
            "Infrastructure",
            "app.infrastructure",
            "External-system adapters implementing bounded ports",
        ),
        (
            "Knowledge",
            "app.knowledge",
            "Clean-snapshot provenance store and deterministic retrieval",
        ),
        ("Market", "app.market", "Market data contracts, validation, events, and provider ports"),
        ("Paper trading", "app.paper", "Paper-trading application service"),
        ("Persistence", "app.persistence", "Durable repositories, migrations, and recovery"),
        (
            "Platform",
            "app.platform",
            "Host setup, provider installation, and workstation integration",
        ),
        ("Platform Ollama", "app.platform.ollama", "Shared and OS-specific Ollama adapters"),
        (
            "Platform setup",
            "app.platform.setup",
            "Setup orchestration and bounded verification smokes",
        ),
        ("Portfolio", "app.portfolio", "Portfolio state and accounting domain"),
        ("Research", "app.research", "Offline reproducible research and canonical data paths"),
        (
            "Research data",
            "app.research.data",
            "Research storage, alignment, outcomes, and provenance identities",
        ),
        (
            "Research intelligence",
            "app.research.intelligence",
            "Offline market, flow, event, and behavioral intelligence studies",
        ),
        (
            "Research strategy",
            "app.research.strategy",
            "Offline strategy history, lab, and walk-forward evaluation",
        ),
        ("Risk", "app.risk", "Deterministic risk decisions, limits, and rejection reasons"),
        ("Runtime", "app.runtime", "Application composition, process lifecycle, and status"),
        ("Shadow", "app.shadow", "Read-only shadow observation and comparison services"),
        ("Strategies", "app.strategies", "Advisory strategy implementations"),
        ("Trading", "app.trading", "Trading intent, snapshots, pipeline, and strategy ports"),
        ("Tools", "app.tools", "Leaf operator and offline command entry points"),
        ("CLI", "app.cli", "Stable command-line composition adapter"),
        ("Main", "app.main", "Stable runtime process adapter"),
        (
            "Research methodology",
            "app.research_methodology",
            "Stable frozen research methodology contract",
        ),
    )
    packages = tuple(
        (
            KnowledgeTypeV1.MODULE_RESPONSIBILITY,
            f"Package ownership: {title}",
            f"{path} owns {responsibility.casefold()}.",
            source,
            "Package ownership",
            (
                "architecture",
                "ownership",
                *path.split(".")[1:],
                *(("production", "code", "import") if path == "app.tools" else ()),
            ),
            ("Chappie Architecture",),
        )
        for title, path, responsibility in package_responsibilities
    )
    governance = (
        (
            KnowledgeTypeV1.ARCHITECTURE,
            "Clean Architecture layers",
            "Chappie separates interfaces, composition, platform, application services, domain, offline side planes, and a neutral foundation so authority does not leak outward.",
            source,
            "Layers and direction",
            ("architecture", "layers", "dependency", "authority"),
            ("Chappie Architecture",),
        ),
        (
            KnowledgeTypeV1.AUTHORITY_BOUNDARY,
            "Dependency direction",
            "Domain packages do not depend on composition, platform, UI, tools, research, or AI; composition owns lifecycle rather than business rules.",
            source,
            "Layers and direction",
            ("architecture", "dependency", "domain", "composition"),
            ("Chappie Architecture",),
        ),
        (
            KnowledgeTypeV1.AUTHORITY_BOUNDARY,
            "Risk decisions authority",
            "app.risk owns deterministic risk approvals, limits, and rejection reasons; AI and interfaces cannot approve risk.",
            source,
            "Package ownership",
            ("risk", "authority", "decisions", "architecture"),
            ("Chappie Architecture",),
        ),
        (
            KnowledgeTypeV1.AUTHORITY_BOUNDARY,
            "Execution authority",
            "Execution authority remains in deterministic execution and exchange-execution boundaries; AI, research, and UI cannot submit orders directly.",
            source,
            "Provenance and authority",
            ("execution", "authority", "trading", "ai", "interface"),
            ("RDE evidence authority",),
        ),
        (
            KnowledgeTypeV1.AUTHORITY_BOUNDARY,
            "AI authority boundary",
            "AI is provider-neutral, structured, and read-only: it may reason over supplied facts and evidence but owns no trading, Risk, execution, or portfolio authority.",
            source,
            "Layers and direction",
            ("ai", "authority", "risk", "execution", "execute", "trades", "directly", "portfolio"),
            ("RDE evidence authority",),
        ),
        (
            KnowledgeTypeV1.AUTHORITY_BOUNDARY,
            "Knowledge provenance boundary",
            "Knowledge owns clean-snapshot provenance, canonical records, and deterministic retrieval; it does not own arbitrary generation or application authority.",
            source,
            "Layers and direction",
            ("knowledge", "provenance", "authority", "snapshot", "allowlisted", "source", "move"),
            ("Knowledge Store",),
        ),
        (
            KnowledgeTypeV1.MODULE_RESPONSIBILITY,
            "Persistence state ownership",
            "Persistence owns durable representation, repositories, migrations, and recovery; persistence writes go through repository ports rather than bypassing owning services.",
            source,
            "Composition roots and state ownership",
            ("persistence", "state", "ownership", "recovery", "role"),
            ("Chappie Architecture",),
        ),
        (
            KnowledgeTypeV1.AUTHORITY_BOUNDARY,
            "Platform I/O boundary",
            "Infrastructure adapters own network, filesystem, OS, and subprocess I/O behind bounded ports; app.platform owns host setup and workstation integration, not trading decisions.",
            source,
            "Composition roots and state ownership",
            (
                "platform",
                "io",
                "infrastructure",
                "external",
                "provider",
                "adapter",
                "adapters",
                "installation",
                "mechanism",
            ),
            ("Setup Engine",),
        ),
        (
            KnowledgeTypeV1.AUTHORITY_BOUNDARY,
            "Control Plane boundary",
            "The Control Plane projects read-only state over loopback and may invoke bounded Project QA, but cannot become a runtime or trading composition root.",
            source,
            "Layers and direction",
            ("control", "plane", "read-only", "loopback", "interface"),
            ("Chappie Control Plane", "Project QA"),
        ),
        (
            KnowledgeTypeV1.AUTHORITY_BOUNDARY,
            "VS Code interface boundary",
            "VS Code Workbench and webviews own presentation and user interaction only; they do not own backend business rules or directly execute provider, network, or process logic.",
            source,
            "VS Code boundary",
            ("vscode", "interface", "ui", "presentation", "authority"),
            ("Chappie Control Plane",),
        ),
        (
            KnowledgeTypeV1.DECISION,
            "Feature implementation placement",
            "A new feature determines its owning package before coding, places contracts at the lowest stable owner, preserves dependency direction, and avoids cross-package feature blobs.",
            "docs/architecture/FEATURE_IMPLEMENTATION_RULES.md",
            "Feature placement",
            (
                "feature",
                "implementation",
                "ownership",
                "dependency",
                "contracts",
                "architecture",
                "layers",
                "split",
            ),
            ("Chappie Architecture",),
        ),
        (
            KnowledgeTypeV1.DECISION,
            "Feature integration boundaries",
            "External integrations belong behind adapters, persistence is separate from business authority, UI consumes bounded interfaces, and architecture guards and tests are required.",
            "docs/architecture/FEATURE_IMPLEMENTATION_RULES.md",
            "Feature implementation",
            ("feature", "adapters", "persistence", "ui", "tests"),
            ("Chappie Architecture",),
        ),
        (
            KnowledgeTypeV1.CONTRACT,
            "Frozen contract discipline",
            "A semantic change to a frozen or versioned contract requires a version bump, digest update, dependent tests, and migration notes; file moves alone do not change a semantic identity.",
            source,
            "OOP and contracts",
            ("contracts", "frozen", "version", "digest", "compatibility"),
            ("Compact matched-event methodology",),
        ),
        (
            KnowledgeTypeV1.LIMITATION,
            "Current capability versus planned capability",
            "Chappie distinguishes current implemented capabilities from explicitly planned future roles; planned agents or features are not active authority.",
            "docs/architecture/ARCHITECTURE_DECISIONS.md",
            "Current and deferred decisions",
            ("current", "future", "planned", "capability", "authority"),
            ("Trading Agent",),
        ),
        (
            KnowledgeTypeV1.ARCHITECTURE,
            "Governance rules",
            "Package ownership, dependency direction, authority boundaries, provenance, and permanent architecture guards govern future Chappie implementation.",
            "docs/architecture/PACKAGE_BOUNDARIES.md",
            "Governance",
            ("architecture", "governance", "ownership", "dependency", "guards"),
            ("Chappie Architecture",),
        ),
        (
            KnowledgeTypeV1.AUTHORITY_BOUNDARY,
            "Forbidden dependency enforcement",
            "Dependency rules reject forbidden upward and authority-crossing imports, including domain dependencies on runtime, platform, UI, research, or AI.",
            "docs/architecture/DEPENDENCY_RULES.md",
            "Forbidden dependencies",
            ("architecture", "dependency", "forbidden", "guards", "authority"),
            ("Chappie Architecture",),
        ),
        (
            KnowledgeTypeV1.DECISION,
            "Refactor completion status",
            "The structural refactor completed with zero production module strongly connected components; deferred items remain explicitly classified rather than silently accepted as architecture authority.",
            "docs/architecture/REFACTOR_FINAL_REPORT.md",
            "Final structural audit",
            ("architecture", "refactor", "current", "deferred", "cycles"),
            ("Chappie Architecture",),
        ),
    )
    return packages + governance


def _development_record_definitions() -> tuple[
    tuple[KnowledgeTypeV1, str, str, str, str, tuple[str, ...], tuple[str, ...]], ...
]:
    """Source-backed facts for the implemented read-only development capability."""
    return (
        (
            KnowledgeTypeV1.MODULE_RESPONSIBILITY,
            "Package ownership: Development Intelligence",
            "app.development_analysis owns read-only development analysis, evidence packs, and advisory Codex task preparation.",
            "app/development_analysis/__init__.py",
            "package public API",
            (
                "app",
                "development",
                "development_analysis",
                "intelligence",
                "ownership",
                "read-only",
            ),
            ("Chappie Architecture",),
        ),
        (
            KnowledgeTypeV1.CONTRACT,
            "DevelopmentAnalysisRequestV1",
            "DevelopmentAnalysisRequestV1 binds a bounded request, explicit development model role, and optional workspace context.",
            "app/development_analysis/models.py",
            "DevelopmentAnalysisRequestV1",
            ("development", "request", "contract", "developmentanalysisrequestv1"),
            ("Package ownership: Development Intelligence",),
        ),
        (
            KnowledgeTypeV1.CONTRACT,
            "DevelopmentEvidencePackV1",
            "DevelopmentEvidencePackV1 binds a clean source commit, canonical Knowledge provenance, selected records, and requested/effective model identity before inference.",
            "app/development_analysis/models.py",
            "DevelopmentEvidencePackV1",
            ("development", "evidence", "contract", "provenance", "developmentevidencepackv1"),
            ("Package ownership: Development Intelligence",),
        ),
        (
            KnowledgeTypeV1.CONTRACT,
            "DevelopmentAnalysisV1",
            "DevelopmentAnalysisV1 separates facts, inferences, unknowns, scope, risks, tests, acceptance criteria, structural validation, and task readiness.",
            "app/development_analysis/models.py",
            "DevelopmentAnalysisV1",
            ("development", "analysis", "contract", "developmentanalysisv1", "readiness"),
            ("Package ownership: Development Intelligence",),
        ),
        (
            KnowledgeTypeV1.CONTRACT,
            "CodexTaskPreparationRequestV1",
            "CodexTaskPreparationRequestV1 accepts only a validated development analysis as the source for advisory task preparation.",
            "app/development_analysis/task.py",
            "CodexTaskPreparationRequestV1",
            ("development", "codex", "task", "contract", "codextaskpreparationrequestv1"),
            ("DevelopmentAnalysisV1",),
        ),
        (
            KnowledgeTypeV1.CONTRACT,
            "CodexTaskV1",
            "CodexTaskV1 is a deterministic advisory projection from validated, ready development analysis and requires human approval before Codex receives a task.",
            "app/development_analysis/task.py",
            "CodexTaskV1",
            ("development", "codex", "task", "contract", "codextaskv1", "advisory"),
            ("DevelopmentAnalysisV1", "CodexTaskPreparationRequestV1"),
        ),
        (
            KnowledgeTypeV1.CONTRACT,
            "POST /v1/development/analyze",
            "POST /v1/development/analyze exposes bounded read-only DevelopmentAnalysisV1 through the local Control Plane transport.",
            "app/control_plane/transport.py",
            "development analysis route",
            ("development", "endpoint", "control", "plane", "analyze", "v1"),
            ("DevelopmentAnalysisV1",),
        ),
        (
            KnowledgeTypeV1.CONTRACT,
            "POST /v1/development/task",
            "POST /v1/development/task exposes bounded advisory CodexTaskV1 preparation through the local Control Plane transport.",
            "app/control_plane/transport.py",
            "development task route",
            ("development", "endpoint", "control", "plane", "task", "v1"),
            ("CodexTaskV1",),
        ),
        (
            KnowledgeTypeV1.AUTHORITY_BOUNDARY,
            "Development Intelligence advisory boundary",
            "Development Intelligence is read-only and advisory: it has no file, shell, Git, Knowledge, trading, Risk, or execution mutation authority, and CodexTaskV1 requires HUMAN_APPROVAL_REQUIRED.",
            "app/development_analysis/task.py",
            "CodexTaskV1 approval boundary",
            (
                "development",
                "authority",
                "advisory",
                "human_approval_required",
                "read-only",
                "non-mutation",
            ),
            ("CodexTaskV1",),
        ),
        (
            KnowledgeTypeV1.MODULE_RESPONSIBILITY,
            "Development Intelligence analysis tests",
            "tests/test_development_analysis.py verifies evidence construction, fail-closed unavailable or stale inputs, and bounded development analysis validation.",
            "tests/test_development_analysis.py",
            "development analysis tests",
            ("development", "tests", "analysis", "readiness", "boundary"),
            ("DevelopmentAnalysisV1",),
        ),
    )


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
