"""Deterministic cache and artifact service for verified existing-cache evidence."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from pathlib import Path

from app.ai.research.capabilities import (
    CapabilityStatusV1,
    ExistingEventResearchQueryV1,
    RDECapabilityResolverV1,
)
from app.ai.research.execution_contracts import (
    ExistingCacheResearchEvidenceV1,
    PersistedEventReferenceV1,
    PersistedMatchReferenceV1,
    YearEvidenceSliceV1,
)
from app.ai.research.query_compiler import CanonicalRDEQueryV1
from app.ai.research.query_executor import (
    ExecutorResultV1,
    ExistingCacheFixtureStorage,
    assess_existing_cache_query,
    execute_existing_cache_query,
    existing_executor_digest,
)
from app.ai.schemas import digest
from app.research.data.cache_identity import VerifiedResearchCacheIdentityV1

CACHE_SCHEMA = "rde_execution_cache/1.0.0"
ARTIFACT_SCHEMA = "rde_execution_artifact/1.0.0"
EXECUTOR_IDENTITY = "rde_existing_cache_executor/1.0.0"


class CacheStatusV1(StrEnum):
    MISS = "CACHE_MISS"
    REUSED = "CACHE_REUSED"
    DISABLED = "CACHE_DISABLED"
    INVALID_RECOMPUTED = "CACHE_INVALID_RECOMPUTED"
    DRY_RUN = "DRY_RUN"


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def evidence_payload(evidence: ExistingCacheResearchEvidenceV1) -> dict[str, object]:
    return asdict(evidence)


def research_evidence_digest(evidence: ExistingCacheResearchEvidenceV1) -> str:
    """Semantic evidence only: no paths, timestamps, report text, or cache state."""
    return digest(evidence_payload(evidence))


def execution_cache_identity(
    query: ExistingEventResearchQueryV1,
    *,
    executor_identity: str = EXECUTOR_IDENTITY,
    executor_digest: str | None = None,
) -> str:
    return digest(
        {
            "schema": CACHE_SCHEMA,
            "query_digest": query.query_digest,
            "executor": [executor_identity, executor_digest or existing_executor_digest()],
            "methodology": [
                query.methodology_name,
                query.methodology_version,
                query.methodology_digest,
            ],
            "verified_cache_identity": query.existing_cache_identity,
            "event_match_dependency": query.event_match_cache_identity,
            "outcome_dependency": query.outcome_cache_identity,
        }
    )


def cache_path(cache_root: Path, cache_identity: str) -> Path:
    return cache_root / cache_identity[:2] / f"{cache_identity}.json"


def _atomic_write(path: Path, payload: object) -> None:
    _atomic_write_text(path, _canonical_json(payload) + "\n")


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _evidence_from_payload(payload: object) -> ExistingCacheResearchEvidenceV1 | None:
    if not isinstance(payload, dict):
        return None
    try:
        normalized = dict(payload)
        normalized["event_references"] = tuple(
            PersistedEventReferenceV1(**item) for item in normalized["event_references"]
        )
        normalized["match_references"] = tuple(
            PersistedMatchReferenceV1(**item) for item in normalized["match_references"]
        )
        normalized["year_slices"] = tuple(
            YearEvidenceSliceV1(**item) for item in normalized["year_slices"]
        )
        for field in ("event_values", "control_values", "reason_codes"):
            normalized[field] = tuple(normalized[field])
        return ExistingCacheResearchEvidenceV1(**normalized)
    except (KeyError, TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class RDEExecutionCacheRecordV1:
    cache_identity: str
    cache_schema: str
    query_digest: str
    executor_identity: str
    executor_digest: str
    methodology_name: str
    methodology_version: str
    methodology_digest: str
    verified_cache_identity_digest: str
    event_match_dependency_identity: str
    outcome_dependency_identity: str
    evidence_digest: str
    verification_state: str
    evidence: ExistingCacheResearchEvidenceV1

    def payload(self) -> dict[str, object]:
        return {
            "cache_identity": self.cache_identity,
            "cache_schema": self.cache_schema,
            "query_digest": self.query_digest,
            "executor_identity": self.executor_identity,
            "executor_digest": self.executor_digest,
            "methodology_name": self.methodology_name,
            "methodology_version": self.methodology_version,
            "methodology_digest": self.methodology_digest,
            "verified_cache_identity_digest": self.verified_cache_identity_digest,
            "event_match_dependency_identity": self.event_match_dependency_identity,
            "outcome_dependency_identity": self.outcome_dependency_identity,
            "evidence_digest": self.evidence_digest,
            "verification_state": self.verification_state,
            "evidence": evidence_payload(self.evidence),
        }


def _record_from_payload(payload: object) -> RDEExecutionCacheRecordV1 | None:
    if not isinstance(payload, dict):
        return None
    evidence = _evidence_from_payload(payload.get("evidence"))
    if evidence is None:
        return None
    try:
        return RDEExecutionCacheRecordV1(
            **{key: value for key, value in payload.items() if key != "evidence"}, evidence=evidence
        )
    except TypeError:
        return None


def _query_matches_identity(
    query: ExistingEventResearchQueryV1, identity: VerifiedResearchCacheIdentityV1
) -> bool:
    return (
        query.asset == identity.asset
        and query.methodology_name == identity.methodology_name
        and query.methodology_version == identity.methodology_version
        and query.methodology_digest == identity.methodology_digest
        and query.existing_cache_identity == identity.research_cache_identity_digest
        and query.event_match_cache_identity == identity.event_match_content_digest
        and query.outcome_cache_identity == identity.outcome_content_digest
    )


def _record_matches(
    record: RDEExecutionCacheRecordV1,
    query: ExistingEventResearchQueryV1,
    cache_identity: str,
    executor_identity: str,
    executor_digest: str,
) -> bool:
    return (
        record.cache_identity == cache_identity
        and record.cache_schema == CACHE_SCHEMA
        and record.verification_state == "VERIFIED"
        and record.query_digest == query.query_digest
        and record.executor_identity == executor_identity
        and record.executor_digest == executor_digest
        and record.methodology_name == query.methodology_name
        and record.methodology_version == query.methodology_version
        and record.methodology_digest == query.methodology_digest
        and record.verified_cache_identity_digest == query.existing_cache_identity
        and record.event_match_dependency_identity == query.event_match_cache_identity
        and record.outcome_dependency_identity == query.outcome_cache_identity
        and record.evidence_digest == research_evidence_digest(record.evidence)
        and record.evidence.raw_qualifying_rows is not None
    )


@dataclass(frozen=True, slots=True)
class RDEExecutionServiceResultV1:
    status: str
    cache_status: CacheStatusV1
    cache_identity: str
    evidence: ExistingCacheResearchEvidenceV1 | None = None
    reason_codes: tuple[str, ...] = ()


class RDEResearchExecutionService:
    """Thin orchestration only; the existing executor retains all evidence semantics."""

    def __init__(
        self,
        cache_root: Path,
        *,
        executor: Callable[
            [
                ExistingEventResearchQueryV1,
                VerifiedResearchCacheIdentityV1,
                ExistingCacheFixtureStorage,
            ],
            ExecutorResultV1,
        ] = execute_existing_cache_query,
        executor_identity: str = EXECUTOR_IDENTITY,
        executor_digest: str | None = None,
    ) -> None:
        self.cache_root = cache_root
        self.executor = executor
        self.executor_identity = executor_identity
        self.executor_digest = executor_digest or existing_executor_digest()

    def execute(
        self,
        query: ExistingEventResearchQueryV1,
        identity: VerifiedResearchCacheIdentityV1,
        storage: ExistingCacheFixtureStorage,
        *,
        no_cache: bool = False,
        dry_run: bool = False,
    ) -> RDEExecutionServiceResultV1:
        cache_identity = execution_cache_identity(
            query,
            executor_identity=self.executor_identity,
            executor_digest=self.executor_digest,
        )
        capability = assess_existing_cache_query(query)
        if capability != CapabilityStatusV1.EXECUTABLE_EXISTING_CACHE:
            return RDEExecutionServiceResultV1(
                capability.value,
                CacheStatusV1.DRY_RUN if dry_run else CacheStatusV1.DISABLED,
                cache_identity,
            )
        if not _query_matches_identity(query, identity):
            return RDEExecutionServiceResultV1(
                "VERIFIED_CACHE_IDENTITY_INVALID",
                CacheStatusV1.DRY_RUN if dry_run else CacheStatusV1.DISABLED,
                cache_identity,
            )
        if dry_run:
            return RDEExecutionServiceResultV1(
                "EXECUTABLE_EXISTING_CACHE", CacheStatusV1.DRY_RUN, cache_identity
            )
        path = cache_path(self.cache_root, cache_identity)
        invalid = False
        if not no_cache and path.is_file():
            try:
                record = _record_from_payload(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                record = None
            if record is not None and _record_matches(
                record, query, cache_identity, self.executor_identity, self.executor_digest
            ):
                return RDEExecutionServiceResultV1(
                    "EXECUTED", CacheStatusV1.REUSED, cache_identity, record.evidence
                )
            invalid = True
        result = self.executor(query, identity, storage)
        if result.status != "EXECUTED" or not isinstance(
            result.evidence, ExistingCacheResearchEvidenceV1
        ):
            return RDEExecutionServiceResultV1(
                result.status,
                CacheStatusV1.DISABLED if no_cache else CacheStatusV1.MISS,
                cache_identity,
                reason_codes=result.reason_codes,
            )
        evidence = result.evidence
        cache_status = CacheStatusV1.DISABLED if no_cache else CacheStatusV1.MISS
        if not no_cache:
            record = RDEExecutionCacheRecordV1(
                cache_identity,
                CACHE_SCHEMA,
                query.query_digest,
                self.executor_identity,
                self.executor_digest,
                evidence.methodology_name,
                evidence.methodology_version,
                evidence.methodology_digest,
                evidence.verified_cache_identity_digest,
                evidence.event_match_cache_identity,
                evidence.outcome_cache_identity,
                research_evidence_digest(evidence),
                "VERIFIED",
                evidence,
            )
            _atomic_write(path, record.payload())
            if invalid:
                cache_status = CacheStatusV1.INVALID_RECOMPUTED
        return RDEExecutionServiceResultV1("EXECUTED", cache_status, cache_identity, evidence)

    def execute_canonical(
        self,
        query: CanonicalRDEQueryV1,
        identity: VerifiedResearchCacheIdentityV1,
        storage: ExistingCacheFixtureStorage,
        *,
        no_cache: bool = False,
        dry_run: bool = False,
    ) -> RDEExecutionServiceResultV1:
        assessment = RDECapabilityResolverV1().resolve(query)
        if assessment.status != CapabilityStatusV1.EXECUTABLE_EXISTING_CACHE:
            return RDEExecutionServiceResultV1(
                assessment.status.value,
                CacheStatusV1.DRY_RUN if dry_run else CacheStatusV1.DISABLED,
                digest({"canonical_query": query.query_digest}),
                reason_codes=assessment.reason_codes,
            )
        assert assessment.existing_event_query is not None
        existing = replace(
            assessment.existing_event_query,
            existing_cache_identity=identity.research_cache_identity_digest,
            event_match_cache_identity=identity.event_match_content_digest,
            outcome_cache_identity=identity.outcome_content_digest,
        )
        return self.execute(existing, identity, storage, no_cache=no_cache, dry_run=dry_run)

    def write_artifacts(
        self,
        output_root: Path,
        query: ExistingEventResearchQueryV1,
        result: RDEExecutionServiceResultV1,
    ) -> Path:
        """Write user-requested generated reports, distinct from reusable cache records."""
        target = output_root / result.cache_identity
        _atomic_write(target / "query.json", asdict(query))
        _atomic_write(
            target / "capability.json",
            {"status": assess_existing_cache_query(query).value},
        )
        _atomic_write(
            target / "execution_manifest.json",
            {
                "schema": ARTIFACT_SCHEMA,
                "query_digest": query.query_digest,
                "cache_identity": result.cache_identity,
                "cache_status": result.cache_status.value,
                "status": result.status,
                "executor": [self.executor_identity, self.executor_digest],
                "methodology": [
                    query.methodology_name,
                    query.methodology_version,
                    query.methodology_digest,
                ],
                "verified_cache_identity": query.existing_cache_identity,
                "event_match_dependency": query.event_match_cache_identity,
                "outcome_dependency": query.outcome_cache_identity,
                "evidence_digest": research_evidence_digest(result.evidence)
                if result.evidence is not None
                else None,
            },
        )
        if result.evidence is not None:
            _atomic_write(target / "evidence.json", evidence_payload(result.evidence))
            report = render_report(query, result)
            _atomic_write_text(target / "report.md", report)
        return target


def render_report(query: ExistingEventResearchQueryV1, result: RDEExecutionServiceResultV1) -> str:
    evidence = result.evidence
    lines = [
        "# CHAPPIE RDE — Existing Cache Research",
        "",
        f"- Asset: `{query.asset}`",
        f"- Event: `{query.event_family}` / `{query.variant}`",
        f"- Metric/horizon: `{query.target_metric}` / `{query.target_horizon}`",
        f"- Cache: `{result.cache_status.value}`",
    ]
    if evidence is not None:
        lines.extend(
            [
                f"- Matched/unmatched/unavailable: {evidence.matched_count}/{evidence.unmatched_count}/{evidence.outcome_unavailable_count}",
                f"- Event/control/incremental mean: {evidence.event_mean}/{evidence.control_mean}/{evidence.incremental_mean}",
                f"- Status: `{evidence.evidence_status}`",
            ]
        )
    lines.extend(
        [
            "",
            "This is descriptive research evidence. It does not establish profitability or a trade signal.",
        ]
    )
    return "\n".join(lines) + "\n"
