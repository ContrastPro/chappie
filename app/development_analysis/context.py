"""Deterministic, request-local evidence handles for Development Reasoning 2.0."""

from __future__ import annotations

from dataclasses import dataclass

from app.ai.schemas import digest

DEVELOPMENT_CONTEXT_MAP_IDENTITY = "chappie_development_context_map/1.0.0"
MAX_DEVELOPMENT_EVIDENCE_HANDLES = 24


@dataclass(frozen=True, slots=True)
class DevelopmentEvidenceHandleV1:
    handle: str
    fact_id: str
    record_id: str
    record_digest: str
    category: str
    statement: str

    def payload(self) -> dict[str, str]:
        return {
            "handle": self.handle,
            "fact_id": self.fact_id,
            "record_id": self.record_id,
            "record_digest": self.record_digest,
            "category": self.category,
            "statement": self.statement,
        }


@dataclass(frozen=True, slots=True)
class DevelopmentContextMapV1:
    source_commit: str
    knowledge_identity: str
    knowledge_manifest_digest: str
    knowledge_context_digest: str
    evidence_digest: str
    requested_role: str
    effective_model: str
    evidence_handles: tuple[DevelopmentEvidenceHandleV1, ...]
    owner_candidates: tuple[str, ...]
    existing_contracts: tuple[str, ...]
    existing_files: tuple[str, ...]
    relevant_tests: tuple[str, ...]
    authority_constraints: tuple[str, ...]
    context_digest: str

    def payload(self) -> dict[str, object]:
        return {
            "identity": DEVELOPMENT_CONTEXT_MAP_IDENTITY,
            "source_commit": self.source_commit,
            "knowledge_identity": self.knowledge_identity,
            "knowledge_manifest_digest": self.knowledge_manifest_digest,
            "knowledge_context_digest": self.knowledge_context_digest,
            "evidence_digest": self.evidence_digest,
            "requested_role": self.requested_role,
            "effective_model": self.effective_model,
            "evidence_handles": [item.payload() for item in self.evidence_handles],
            "owner_candidates": list(self.owner_candidates),
            "existing_contracts": list(self.existing_contracts),
            "existing_files": list(self.existing_files),
            "relevant_tests": list(self.relevant_tests),
            "authority_constraints": list(self.authority_constraints),
            "context_digest": self.context_digest,
        }


def build_development_context_map(context: object, evidence: object) -> DevelopmentContextMapV1:
    """Project bounded selected Knowledge into immutable handles before inference."""
    records = tuple(sorted(context.records, key=lambda item: item.knowledge_id))
    if len(records) > MAX_DEVELOPMENT_EVIDENCE_HANDLES:
        records = records[:MAX_DEVELOPMENT_EVIDENCE_HANDLES]
    handles = tuple(
        DevelopmentEvidenceHandleV1(
            f"E{index}",
            f"knowledge.{record.knowledge_id}.statement",
            record.knowledge_id,
            record.record_digest,
            record.knowledge_type.value,
            record.statement,
        )
        for index, record in enumerate(records, start=1)
    )
    owner_candidates = tuple(sorted({record.knowledge_type.value for record in records}))
    contracts = tuple(sorted(record.title for record in records if "V1" in record.title))
    files = tuple(
        sorted({source.path for record in records for source in record.sources if source.path})
    )
    tests = tuple(path for path in files if path.startswith("tests/"))
    constraints = (
        "Advisory only; human approval is required.",
        "No file, shell, Git, Knowledge, trading, Risk, or execution authority.",
    )
    payload = {
        "source_commit": evidence.source_commit,
        "knowledge_identity": evidence.knowledge_identity,
        "knowledge_manifest_digest": evidence.knowledge_manifest_digest,
        "knowledge_context_digest": evidence.knowledge_context_digest,
        "evidence_digest": evidence.evidence_digest,
        "requested_role": evidence.requested_role.value,
        "effective_model": evidence.effective_model,
        "evidence_handles": handles,
        "owner_candidates": owner_candidates,
        "existing_contracts": contracts,
        "existing_files": files,
        "relevant_tests": tests,
        "authority_constraints": constraints,
    }
    digest_payload = {
        **payload,
        "evidence_handles": [item.payload() for item in handles],
    }
    return DevelopmentContextMapV1(
        **payload,
        context_digest=digest({"identity": DEVELOPMENT_CONTEXT_MAP_IDENTITY, **digest_payload}),
    )


def development_context_map_from_payload(value: object) -> DevelopmentContextMapV1:
    """Strictly restore the backend-produced, immutable context map."""
    if not isinstance(value, dict) or value.get("identity") != DEVELOPMENT_CONTEXT_MAP_IDENTITY:
        raise ValueError("development context map is invalid")
    raw_handles = value.get("evidence_handles")
    if not isinstance(raw_handles, list):
        raise TypeError("development context map handles are invalid")
    try:
        handles = tuple(
            DevelopmentEvidenceHandleV1(
                str(item["handle"]),
                str(item["fact_id"]),
                str(item["record_id"]),
                str(item["record_digest"]),
                str(item["category"]),
                str(item["statement"]),
            )
            for item in raw_handles
            if isinstance(item, dict)
        )
        if len(handles) != len(raw_handles) or any(
            handle.handle != f"E{index}" for index, handle in enumerate(handles, start=1)
        ):
            raise ValueError
        result = DevelopmentContextMapV1(
            str(value["source_commit"]),
            str(value["knowledge_identity"]),
            str(value["knowledge_manifest_digest"]),
            str(value["knowledge_context_digest"]),
            str(value["evidence_digest"]),
            str(value["requested_role"]),
            str(value["effective_model"]),
            handles,
            tuple(_strings(value["owner_candidates"])),
            tuple(_strings(value["existing_contracts"])),
            tuple(_strings(value["existing_files"])),
            tuple(_strings(value["relevant_tests"])),
            tuple(_strings(value["authority_constraints"])),
            str(value["context_digest"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("development context map is invalid") from error
    expected = build_context_digest(result)
    if result.context_digest != expected:
        raise ValueError("development context map digest is invalid")
    return result


def build_context_digest(value: DevelopmentContextMapV1) -> str:
    payload = value.payload()
    payload.pop("identity")
    payload.pop("context_digest")
    return digest({"identity": DEVELOPMENT_CONTEXT_MAP_IDENTITY, **payload})


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("development context map list is invalid")
    return tuple(value)
