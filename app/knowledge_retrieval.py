"""Bounded exact retrieval over canonical Chappie Knowledge records."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.ai.schemas import digest
from app.ai.tasks import AITask
from app.knowledge import (
    SOURCE_ALLOWLIST,
    KnowledgeAuthorityV1,
    KnowledgeContextRecordV1,
    KnowledgeContextV1,
    KnowledgeManifestV1,
    KnowledgeRecordV1,
    KnowledgeStatusV1,
    KnowledgeTypeV1,
    detect_conflicts,
)

RETRIEVAL_IDENTITY = "chappie_knowledge_retrieval/1.0.0"
AUTHORITY_PRIORITY = (
    KnowledgeAuthorityV1.VERSIONED_CONTRACT,
    KnowledgeAuthorityV1.VERIFIED_RESEARCH_EVIDENCE,
    KnowledgeAuthorityV1.SOURCE_CODE,
    KnowledgeAuthorityV1.PROJECT_DECISION,
    KnowledgeAuthorityV1.DOCUMENTATION,
    KnowledgeAuthorityV1.GENERATED_SUMMARY,
)
RETRIEVAL_CONTRACT_DIGEST = digest(
    {
        "identity": RETRIEVAL_IDENTITY,
        "status_default": ["ACTIVE", "FROZEN"],
        "authority_priority": [x.value for x in AUTHORITY_PRIORITY],
        "ranking": [
            "EXACT_ID",
            "TYPE_MATCH",
            "AUTHORITY",
            "TAG_TOKEN_OVERLAP",
            "STATUS",
            "KNOWLEDGE_ID",
        ],
        "relation_depth_default": 1,
        "context_budget_default": 3000,
    }
)


class SelectionReasonV1(StrEnum):
    EXACT_ID = "EXACT_ID"
    TYPE_MATCH = "TYPE_MATCH"
    TAG_MATCH = "TAG_MATCH"
    TOKEN_MATCH = "TOKEN_MATCH"
    REQUIRED_AUTHORITY = "REQUIRED_AUTHORITY"
    RELATION_EXPANSION = "RELATION_EXPANSION"


@dataclass(frozen=True, slots=True)
class KnowledgeRetrievalRequestV1:
    knowledge_types: tuple[KnowledgeTypeV1, ...] = ()
    authorities: tuple[KnowledgeAuthorityV1, ...] = ()
    statuses: tuple[KnowledgeStatusV1, ...] = ()
    tags: tuple[str, ...] = ()
    record_ids: tuple[str, ...] = ()
    related_to_ids: tuple[str, ...] = ()
    topic_tokens: tuple[str, ...] = ()
    maximum_records: int = 8
    maximum_context_characters: int = 3000
    include_historical: bool = False
    include_superseded: bool = False
    include_planned: bool = False
    relation_depth: int = 1

    def __post_init__(self) -> None:
        if not 1 <= self.maximum_records <= 32 or not 1 <= self.maximum_context_characters <= 12000:
            raise ValueError("knowledge retrieval limits are out of bounds")
        if not 0 <= self.relation_depth <= 2:
            raise ValueError("knowledge relation depth is out of bounds")
        if any(
            not token or token != token.casefold() for token in (*self.tags, *self.topic_tokens)
        ):
            raise ValueError("knowledge tags and topic tokens must be normalized")

    @property
    def request_digest(self) -> str:
        return digest(
            self.__dict__
            if hasattr(self, "__dict__")
            else {field: getattr(self, field) for field in self.__dataclass_fields__}
        )


@dataclass(frozen=True, slots=True)
class KnowledgeRetrievalResultV1:
    request_digest: str
    knowledge_manifest_digest: str
    selected_record_ids: tuple[str, ...]
    selected_record_digests: tuple[str, ...]
    selection_reasons: tuple[tuple[str, tuple[SelectionReasonV1, ...]], ...]
    excluded_reason_counts: tuple[tuple[str, int], ...]
    result_digest: str


def retrieve(
    records: tuple[KnowledgeRecordV1, ...],
    manifest: KnowledgeManifestV1,
    request: KnowledgeRetrievalRequestV1,
) -> tuple[KnowledgeRetrievalResultV1, KnowledgeContextV1]:
    _verify_store(records, manifest)
    allowed = (
        set(request.statuses)
        if request.statuses
        else {KnowledgeStatusV1.ACTIVE, KnowledgeStatusV1.FROZEN}
    )
    if request.include_historical:
        allowed.add(KnowledgeStatusV1.HISTORICAL)
    if request.include_superseded:
        allowed.add(KnowledgeStatusV1.SUPERSEDED)
    if request.include_planned:
        allowed.add(KnowledgeStatusV1.PLANNED)
    by_id = {item.knowledge_id: item for item in records}
    candidates: list[tuple[KnowledgeRecordV1, tuple[SelectionReasonV1, ...], int]] = []
    excluded: dict[str, int] = {}
    for item in records:
        if item.status not in allowed:
            excluded["STATUS_POLICY"] = excluded.get("STATUS_POLICY", 0) + 1
            continue
        if request.knowledge_types and item.knowledge_type not in request.knowledge_types:
            excluded["TYPE_FILTER"] = excluded.get("TYPE_FILTER", 0) + 1
            continue
        if request.authorities and item.authority not in request.authorities:
            excluded["AUTHORITY_FILTER"] = excluded.get("AUTHORITY_FILTER", 0) + 1
            continue
        reasons: list[SelectionReasonV1] = []
        text = _tokens(item.title, item.tags)
        if item.knowledge_id in request.record_ids:
            reasons.append(SelectionReasonV1.EXACT_ID)
        if (
            set(request.related_to_ids).intersection(item.related_record_ids)
            or item.knowledge_id in request.related_to_ids
        ):
            reasons.append(SelectionReasonV1.RELATION_EXPANSION)
        if request.knowledge_types:
            reasons.append(SelectionReasonV1.TYPE_MATCH)
        if set(request.tags).intersection(item.tags):
            reasons.append(SelectionReasonV1.TAG_MATCH)
        if set(request.topic_tokens).intersection(text):
            reasons.append(SelectionReasonV1.TOKEN_MATCH)
        if request.authorities:
            reasons.append(SelectionReasonV1.REQUIRED_AUTHORITY)
        if not reasons and (request.record_ids or request.tags or request.topic_tokens):
            excluded["NO_EXACT_MATCH"] = excluded.get("NO_EXACT_MATCH", 0) + 1
            continue
        overlap = len(set(request.tags).intersection(item.tags)) + len(
            set(request.topic_tokens).intersection(text)
        )
        candidates.append((item, tuple(reasons), overlap))
    selected = _rank(candidates, request)
    if request.relation_depth:
        for item, _, _ in tuple(selected):
            for related in item.related_record_ids:
                if related in by_id and all(
                    existing[0].knowledge_id != related for existing in selected
                ):
                    selected.append((by_id[related], (SelectionReasonV1.RELATION_EXPANSION,), 0))
    selected = selected[: request.maximum_records]
    bounded: list[tuple[KnowledgeRecordV1, tuple[SelectionReasonV1, ...], int]] = []
    size = 0
    for item in selected:
        length = len(item[0].statement) + sum(
            len(source.path) + len(source.locator) for source in item[0].sources
        )
        if size + length > request.maximum_context_characters:
            excluded["CONTEXT_BUDGET"] = excluded.get("CONTEXT_BUDGET", 0) + 1
            continue
        bounded.append(item)
        size += length
    chosen = tuple(item[0] for item in bounded)
    conflicts = detect_conflicts(chosen)
    result_payload = {
        "identity": RETRIEVAL_IDENTITY,
        "contract_digest": RETRIEVAL_CONTRACT_DIGEST,
        "request_digest": request.request_digest,
        "manifest_digest": manifest.manifest_digest,
        "records": [(item.knowledge_id, item.record_digest) for item in chosen],
        "excluded": sorted(excluded.items()),
    }
    result = KnowledgeRetrievalResultV1(
        request.request_digest,
        manifest.manifest_digest,
        tuple(item.knowledge_id for item in chosen),
        tuple(item.record_digest for item in chosen),
        tuple((item.knowledge_id, reasons) for item, reasons, _ in bounded),
        tuple(sorted(excluded.items())),
        digest(result_payload),
    )
    context_records = tuple(
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
        for item in chosen
    )
    context_digest = digest(
        {
            "retrieval": RETRIEVAL_IDENTITY,
            "manifest": manifest.manifest_digest,
            "records": [(item.knowledge_id, item.record_digest) for item in chosen],
        }
    )
    return result, KnowledgeContextV1(
        context_records,
        RETRIEVAL_IDENTITY,
        manifest.manifest_digest,
        tuple(item.record_ids for item in conflicts),
        context_digest,
    )


def build_knowledge_request_for_task(
    task: AITask, *, tags: tuple[str, ...] = (), related_to_ids: tuple[str, ...] = ()
) -> KnowledgeRetrievalRequestV1:
    profiles = {
        AITask.RESEARCH_REASONING_V11: (
            KnowledgeTypeV1.ARCHITECTURE,
            KnowledgeTypeV1.CONTRACT,
            KnowledgeTypeV1.RESEARCH_RESULT,
            KnowledgeTypeV1.LIMITATION,
        ),
        AITask.RESEARCH_EVIDENCE_CRITIQUE_V1: (
            KnowledgeTypeV1.AUTHORITY_BOUNDARY,
            KnowledgeTypeV1.CONTRACT,
            KnowledgeTypeV1.RESEARCH_RESULT,
            KnowledgeTypeV1.DECISION,
        ),
        AITask.RESEARCH_EVIDENCE_CRITIQUE_V11: (
            KnowledgeTypeV1.AUTHORITY_BOUNDARY,
            KnowledgeTypeV1.CONTRACT,
            KnowledgeTypeV1.RESEARCH_RESULT,
            KnowledgeTypeV1.DECISION,
        ),
    }
    return KnowledgeRetrievalRequestV1(
        knowledge_types=profiles.get(
            task,
            (
                KnowledgeTypeV1.ARCHITECTURE,
                KnowledgeTypeV1.MODULE_RESPONSIBILITY,
                KnowledgeTypeV1.DECISION,
                KnowledgeTypeV1.CONTRACT,
            ),
        ),
        tags=tags,
        related_to_ids=related_to_ids,
    )


def _rank(items, request):
    priority = {authority: index for index, authority in enumerate(AUTHORITY_PRIORITY)}
    status = {
        KnowledgeStatusV1.ACTIVE: 0,
        KnowledgeStatusV1.FROZEN: 1,
        KnowledgeStatusV1.HISTORICAL: 2,
        KnowledgeStatusV1.PLANNED: 3,
        KnowledgeStatusV1.SUPERSEDED: 4,
        KnowledgeStatusV1.REJECTED: 5,
    }
    return sorted(
        items,
        key=lambda item: (
            0 if SelectionReasonV1.EXACT_ID in item[1] else 1,
            priority[item[0].authority],
            -item[2],
            status[item[0].status],
            item[0].knowledge_id,
        ),
    )


def _tokens(title: str, tags: tuple[str, ...]) -> set[str]:
    return set(title.casefold().replace("/", " ").replace("-", " ").split()).union(tags)


def _verify_store(records, manifest) -> None:
    if manifest.store_content_digest != digest(
        __import__("app.ai.schemas", fromlist=["canonical_json"]).canonical_json(
            [item.payload() for item in records]
        )
    ):
        raise ValueError("knowledge store manifest mismatch")
    if any(
        source.path not in SOURCE_ALLOWLIST or source.path.startswith(".")
        for item in records
        for source in item.sources
    ):
        raise ValueError("knowledge retrieval rejected a noncanonical source path")
