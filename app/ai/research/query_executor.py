"""Read-only v1 executor for already-verified compact behavioral event caches."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean, median
from typing import Protocol

from app.ai.research.capabilities import (
    METHODOLOGY_DIGEST,
    METHODOLOGY_NAME,
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
from app.ai.research.query_compiler import (
    CanonicalRDEQueryV1,
    CanonicalResearchPredicateV1,
    ResearchEvidenceV1,
)
from app.ai.research.reasoning import RELATIONSHIPS
from app.ai.schemas import digest
from app.research.data.cache_identity import (
    VerifiedResearchCacheIdentityV1,
    verify_research_cache_identity,
)
from app.research.data.event_bridge import BRIDGE_SCHEMA
from app.research.data.outcomes import OUTCOME_SCHEMA_VERSION

EXECUTOR_IDENTITY = "rde_query_executor/1.0.0"
SUPPORTED_FIELDS = frozenset({"EVENT_TYPE"})
HORIZON_MINUTES = {"1h": 60, "3h": 180, "6h": 360, "12h": 720, "24h": 1440}
METRIC_COLUMNS = {
    "FORWARD_RETURN_BPS": "forward_return_bps",
    "MFE_BPS": "max_favorable_excursion_bps",
    "MAE_BPS": "max_adverse_excursion_bps",
    "UPSIDE_60_TOUCH": "hit_plus_60_bps",
    "UPSIDE_100_TOUCH": "hit_plus_100_bps",
    "UPSIDE_150_TOUCH": "hit_plus_150_bps",
    "DOWNSIDE_60_TOUCH": "hit_minus_60_bps",
    "DOWNSIDE_100_TOUCH": "hit_minus_100_bps",
    "DOWNSIDE_150_TOUCH": "hit_minus_150_bps",
}


def executor_digest() -> str:
    return digest(
        {
            "identity": EXECUTOR_IDENTITY,
            "supported_fields": sorted(SUPPORTED_FIELDS),
            "predicate": "EVENT_TYPE EQUALS verified intelligence_events_v1 event_type",
            "metrics": METRIC_COLUMNS,
            "horizons": HORIZON_MINUTES,
            "matching": f"{BRIDGE_SCHEMA}: verified cache only",
            "minimum_matched_events": 20,
            "evidence_policy": "predeclared_directional_mean_delta/1.0.0",
            "outcomes": OUTCOME_SCHEMA_VERSION,
        }
    )


@dataclass(frozen=True, slots=True)
class ExecutorResultV1:
    status: str
    evidence: ResearchEvidenceV1 | ExistingCacheResearchEvidenceV1 | None = None
    reason_codes: tuple[str, ...] = ()


class ExistingCacheFixtureStorage(Protocol):
    asset: str
    family: str
    pairs: tuple[object, ...]
    aligned_digest: str
    outcome_digest: str
    event_match_digest: str

    def read_events(self, asset: str, family: str, variant: str) -> tuple[object, ...]: ...

    def read_matches(self, event_ids: tuple[str, ...]) -> tuple[object, ...]: ...

    def read_outcomes(
        self, pairs: tuple[object, ...], metric: str, horizon: str
    ) -> tuple[object, ...]: ...


def existing_executor_digest() -> str:
    return digest(
        {
            "identity": "rde_existing_cache_executor/1.0.0",
            "validated_capability_boundary": "RDECapabilityResolverV1",
            "identity_before_storage": True,
            "persisted_event_reuse": "exact_event_id_and_anchor",
            "persisted_match_reuse": "exact_match_id_and_control",
            "outcome_lookup": "persisted_requested_metric_and_horizon_only",
            "unmatched": "count_exclude_no_control_fabrication",
            "outcome_unavailable": "count_exclude_no_zero_fill",
            "summary": "paired_event_and_control_mean_median_difference/1.0.0",
            "year_grouping": "event_anchor_utc_year",
            "same_asset_enforcement": "fail_closed",
            "metrics": sorted(METRIC_COLUMNS),
            "horizons": HORIZON_MINUTES,
            "methodology": [METHODOLOGY_NAME, "1.0.0", METHODOLOGY_DIGEST],
        }
    )


def _resolve_existing_query_capability(
    query: ExistingEventResearchQueryV1,
) -> CapabilityStatusV1:
    """Re-run the frozen capability boundary before any cache access."""
    canonical = CanonicalRDEQueryV1(
        predicates=(
            CanonicalResearchPredicateV1(
                query.asset,
                "EVENT_TYPE",
                "EQUALS",
                query.event_family,
                None,
                "AS_OF_FACT_TIMESTAMP",
                "AVAILABLE_ONLY",
                "existing-cache-query",
            ),
        ),
        target_asset=query.asset,
        target_metric=query.target_metric,
        target_horizon=query.target_horizon,
        comparison_basis=query.comparison_basis,
        expected_relationship=query.expected_relationship,
        source_intent_id="existing-cache-query",
        source_fact_ids=("existing-cache-query",),
        compiler_identity="existing-cache-query",
        compiler_digest=query.query_digest,
    )
    return RDECapabilityResolverV1().resolve(canonical).status


def _is_valid_existing_execution_query(query: ExistingEventResearchQueryV1) -> bool:
    return (
        bool(query.asset)
        and query.variant == "BASE"
        and query.target_metric in METRIC_COLUMNS
        and query.target_horizon in HORIZON_MINUTES
        and query.expected_relationship in RELATIONSHIPS
        and query.comparison_basis == "MATCHED_CONTROL"
        and query.methodology_name == METHODOLOGY_NAME
        and query.methodology_version == "1.0.0"
        and query.methodology_digest == METHODOLOGY_DIGEST
        and bool(query.existing_cache_identity)
        and bool(query.event_match_cache_identity)
        and bool(query.outcome_cache_identity)
    )


def assess_existing_cache_query(query: ExistingEventResearchQueryV1) -> CapabilityStatusV1:
    """Read-free validation and frozen capability assessment for orchestration layers."""
    if not _is_valid_existing_execution_query(query):
        return CapabilityStatusV1.INVALID_QUERY
    return _resolve_existing_query_capability(query)


def execute_canonical_existing_cache_query(
    query: CanonicalRDEQueryV1,
    identity: VerifiedResearchCacheIdentityV1,
    storage: ExistingCacheFixtureStorage,
) -> ExecutorResultV1:
    """Resolve a canonical query, then bind its execution to supplied verified caches."""
    assessment = RDECapabilityResolverV1().resolve(query)
    if assessment.status != CapabilityStatusV1.EXECUTABLE_EXISTING_CACHE:
        return ExecutorResultV1(assessment.status.value, reason_codes=assessment.reason_codes)
    assert assessment.existing_event_query is not None
    existing = replace(
        assessment.existing_event_query,
        existing_cache_identity=identity.research_cache_identity_digest,
        event_match_cache_identity=identity.event_match_content_digest,
        outcome_cache_identity=identity.outcome_content_digest,
    )
    return execute_existing_cache_query(existing, identity, storage)


def execute_existing_cache_query(
    query: ExistingEventResearchQueryV1,
    identity: VerifiedResearchCacheIdentityV1,
    storage: ExistingCacheFixtureStorage,
) -> ExecutorResultV1:
    """Fixture-only E1-B consumer: all validation happens before the first read."""
    capability = assess_existing_cache_query(query)
    if capability == CapabilityStatusV1.INVALID_QUERY:
        return ExecutorResultV1("QUERY_INVALID")
    if capability != CapabilityStatusV1.EXECUTABLE_EXISTING_CACHE:
        return ExecutorResultV1("CAPABILITY_NOT_EXECUTABLE")
    if (
        query.asset != identity.asset
        or query.methodology_name != METHODOLOGY_NAME
        or query.methodology_digest != METHODOLOGY_DIGEST
    ):
        return ExecutorResultV1("METHODOLOGY_MISMATCH")
    if query.existing_cache_identity != identity.research_cache_identity_digest:
        return ExecutorResultV1("VERIFIED_CACHE_IDENTITY_INVALID")
    if query.event_match_cache_identity != identity.event_match_content_digest:
        return ExecutorResultV1("EVENT_MATCH_CACHE_MISMATCH")
    if query.outcome_cache_identity != identity.outcome_content_digest:
        return ExecutorResultV1("OUTCOME_CACHE_MISMATCH")
    if (
        verify_research_cache_identity(
            identity,
            asset=storage.asset,
            aligned_content_digest=storage.aligned_digest,
            outcome_content_digest=storage.outcome_digest,
            event_match_content_digest=storage.event_match_digest,
        ).value
        != "VALID"
    ):
        return ExecutorResultV1("VERIFIED_CACHE_IDENTITY_INVALID")
    if query.asset != storage.asset:
        return ExecutorResultV1("ASSET_MISMATCH")
    if query.event_family != storage.family:
        return ExecutorResultV1("EVENT_FAMILY_MISMATCH")
    events = storage.read_events(query.asset, query.event_family, query.variant)
    selected_event_ids = tuple(item.event_id for item in events)
    if len(selected_event_ids) != len(set(selected_event_ids)):
        return ExecutorResultV1("EVENT_INTEGRITY_INVALID")
    matches = storage.read_matches(selected_event_ids)
    match_ids: set[str] = set()
    event_ids = set(selected_event_ids)
    matched_event_ids: set[str] = set()
    for item in matches:
        if (
            item.event_id not in event_ids
            or item.match_id is None
            or item.match_id in match_ids
            or item.event_id in matched_event_ids
        ):
            return ExecutorResultV1("MATCH_INTEGRITY_INVALID")
        if item.control_asset is not None and item.control_asset != item.asset:
            return ExecutorResultV1("ASSET_MISMATCH")
        match_ids.add(item.match_id)
        matched_event_ids.add(item.event_id)
    pairs = storage.read_outcomes(matches, query.target_metric, query.target_horizon)
    valid = []
    unmatched = len(event_ids - matched_event_ids)
    unavailable = 0
    for item in pairs:
        if item.event_id not in event_ids:
            return ExecutorResultV1("MATCH_INTEGRITY_INVALID")
        if item.control_asset is None:
            return ExecutorResultV1("MATCH_INTEGRITY_INVALID")
        if item.event_value is None or item.control_value is None:
            unavailable += 1
        else:
            valid.append(item)
    event_values = tuple(float(item.event_value) for item in valid)
    control_values = tuple(float(item.control_value) for item in valid)
    event_mean = mean(event_values) if event_values else None
    event_median = median(event_values) if event_values else None
    control_mean = mean(control_values) if control_values else None
    control_median = median(control_values) if control_values else None
    incremental_mean = (
        (sum(event_values) - sum(control_values)) / len(event_values) if event_values else None
    )
    incremental_median = (
        event_median - control_median
        if event_median is not None and control_median is not None
        else None
    )
    years: dict[int, list[object]] = {}
    for item in valid:
        years.setdefault(datetime.fromtimestamp(item.event_ms / 1000, UTC).year, []).append(item)
    slices = tuple(
        YearEvidenceSliceV1(
            year,
            len(items),
            len(items),
            mean([x.event_value for x in items]),
            median([x.event_value for x in items]),
            len(items),
            mean([x.control_value for x in items]),
            median([x.control_value for x in items]),
            (sum(x.event_value for x in items) - sum(x.control_value for x in items)) / len(items),
            median([x.event_value for x in items]) - median([x.control_value for x in items]),
        )
        for year, items in sorted(years.items())
    )
    status = (
        "INCONCLUSIVE"
        if incremental_mean is None or incremental_mean == 0
        else (
            "DESCRIPTIVE_DIRECTIONAL_SUPPORT"
            if (
                query.expected_relationship
                in {"GREATER_THAN_MATCHED_CONTROL", "HIGHER_PROBABILITY_THAN_MATCHED_CONTROL"}
            )
            == (incremental_mean > 0)
            else "DESCRIPTIVE_DIRECTIONAL_CONTRADICTION"
        )
    )
    evidence = ExistingCacheResearchEvidenceV1(
        query.query_digest,
        "rde_existing_cache_executor/1.0.0",
        existing_executor_digest(),
        METHODOLOGY_NAME,
        "1.0.0",
        METHODOLOGY_DIGEST,
        identity.research_cache_identity_digest,
        identity.aligned_content_digest,
        identity.outcome_content_digest,
        identity.event_match_content_digest,
        query.asset,
        query.event_family,
        query.variant,
        query.target_metric,
        query.target_horizon,
        query.expected_relationship,
        query.comparison_basis,
        sum(getattr(item, "raw_row_count", 1) for item in events),
        len(events),
        len(events),
        len(events),
        len(valid),
        unmatched,
        unavailable,
        tuple(
            PersistedEventReferenceV1(
                x.event_id, x.asset, query.event_family, query.variant, x.event_ms
            )
            for x in valid
        ),
        tuple(
            PersistedMatchReferenceV1(x.match_id, x.event_id, x.control_asset, x.control_ms)
            for x in valid
        ),
        event_values,
        control_values,
        event_mean,
        event_median,
        control_mean,
        control_median,
        incremental_mean,
        incremental_median,
        slices,
        status,
        (),
    )
    return ExecutorResultV1("EXECUTED", evidence)


class CompactRDEQueryExecutorV1:
    """Never writes, rebuilds, or repairs compact data/caches."""

    def __init__(self, store_path: Path) -> None:
        self.store_path = store_path

    def execute(self, query: CanonicalRDEQueryV1) -> ExecutorResultV1:
        if not self.store_path.is_file():
            return ExecutorResultV1("MISSING_COMPACT_DATA")
        if query.target_horizon not in HORIZON_MINUTES or query.target_metric not in METRIC_COLUMNS:
            return ExecutorResultV1("QUERY_INVALID")
        capability = RDECapabilityResolverV1().resolve(query)
        if capability.status != CapabilityStatusV1.EXECUTABLE_EXISTING_CACHE:
            return ExecutorResultV1(capability.status.value, reason_codes=capability.reason_codes)
        assert capability.existing_event_query is not None
        try:
            connection = sqlite3.connect(f"file:{self.store_path}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            return self._execute_read_only(
                connection, query, capability.existing_event_query.event_family
            )
        except sqlite3.Error:
            return ExecutorResultV1("MISSING_OUTCOME_CACHE")
        finally:
            if "connection" in locals():
                connection.close()

    def _execute_read_only(
        self, connection: sqlite3.Connection, query: CanonicalRDEQueryV1, event_type: str
    ) -> ExecutorResultV1:
        horizon = HORIZON_MINUTES[query.target_horizon]
        metric = METRIC_COLUMNS[query.target_metric]
        manifest = connection.execute(
            "SELECT state,bridge_schema FROM event_match_manifest_v1 WHERE asset=? AND event_type=?",
            (query.target_asset, event_type),
        ).fetchone()
        if manifest is None:
            return ExecutorResultV1("MISSING_OUTCOME_CACHE")
        if manifest["state"] != "VERIFIED" or manifest["bridge_schema"] != BRIDGE_SCHEMA:
            return ExecutorResultV1("METHODOLOGY_MISMATCH")
        rows = connection.execute(
            f"""SELECT e.event_anchor_timestamp_ms AS anchor, ev.{metric} AS event_value,
                       co.{metric} AS control_value
                FROM event_matches_v1 e
                JOIN intelligence_events_v1 i ON i.event_id=e.event_id
                JOIN intelligence_outcomes_v1 ev ON ev.asset=e.asset
                    AND ev.timestamp_ms=e.event_anchor_timestamp_ms AND ev.horizon_minutes=e.horizon_minutes
                JOIN intelligence_outcomes_v1 co ON co.asset=e.asset
                    AND co.timestamp_ms=e.control_timestamp_ms AND co.horizon_minutes=e.horizon_minutes
                WHERE e.asset=? AND i.event_type=? AND e.horizon_minutes=?
                    AND e.control_timestamp_ms IS NOT NULL
                    AND ev.outcome_available=1 AND co.outcome_available=1
                ORDER BY e.event_anchor_timestamp_ms""",
            (query.target_asset, event_type, horizon),
        ).fetchall()
        events = [float(row["event_value"]) for row in rows if row["event_value"] is not None]
        controls = [float(row["control_value"]) for row in rows if row["control_value"] is not None]
        if len(events) != len(controls):
            return ExecutorResultV1("MISSING_OUTCOME_CACHE")
        all_matches = connection.execute(
            "SELECT COUNT(*) FROM event_matches_v1 e JOIN intelligence_events_v1 i ON i.event_id=e.event_id "
            "WHERE e.asset=? AND i.event_type=? AND e.horizon_minutes=?",
            (query.target_asset, event_type, horizon),
        ).fetchone()[0]
        if len(events) < 20:
            status = "INSUFFICIENT_DATA"
        else:
            delta = sum(events) / len(events) - sum(controls) / len(controls)
            supports = (
                query.expected_relationship == "GREATER_THAN_MATCHED_CONTROL" and delta > 0
            ) or (query.expected_relationship == "LESS_THAN_MATCHED_CONTROL" and delta < 0)
            status = "SUPPORT" if supports else "CONTRADICTED" if delta else "NO_CLEAR_INFORMATION"
        delta = None if not events else sum(events) / len(events) - sum(controls) / len(controls)
        years: dict[int, list[float]] = {}
        for row in rows:
            year = datetime.fromtimestamp(int(row["anchor"]) / 1000, UTC).year
            years.setdefault(year, []).append(
                float(row["event_value"]) - float(row["control_value"])
            )
        reason = () if status != "INSUFFICIENT_DATA" else ("MIN_MATCHED_EVENTS_20",)
        evidence = ResearchEvidenceV1(
            query.query_digest,
            len(events),
            len(controls),
            query.target_metric,
            query.target_horizon,
            tuple(events),
            tuple(controls),
            delta,
            tuple((year, sum(values) / len(values)) for year, values in sorted(years.items())),
            None,
            status,
            EXECUTOR_IDENTITY,
            executor_digest(),
            "compact_matched_event_research/1.0.0",
            "b52776bd5045532f83054fd43e6cdd8929d2de95d61301269b8b05751ebff690",
            all_matches,
            all_matches,
            all_matches,
            len(events),
            all_matches - len(events),
            reason,
        )
        return ExecutorResultV1("EXECUTED", evidence, reason)
