"""Typed future-executor records; they create no event, match, or cache identity."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PersistedEventReferenceV1:
    event_id: str
    asset: str
    event_family: str
    variant: str
    event_timestamp_ms: int


@dataclass(frozen=True, slots=True)
class PersistedMatchReferenceV1:
    match_id: str
    event_id: str
    control_asset: str
    control_timestamp_ms: int


@dataclass(frozen=True, slots=True)
class YearEvidenceSliceV1:
    year: int
    matched_count: int
    event_count: int
    event_mean: float | None
    event_median: float | None
    control_count: int
    control_mean: float | None
    control_median: float | None
    incremental_mean: float | None
    incremental_median: float | None


@dataclass(frozen=True, slots=True)
class ExistingCacheResearchEvidenceV1:
    query_digest: str
    executor_identity: str
    executor_digest: str
    methodology_name: str
    methodology_version: str
    methodology_digest: str
    verified_cache_identity_digest: str
    aligned_cache_identity: str
    outcome_cache_identity: str
    event_match_cache_identity: str
    asset: str
    event_family: str
    variant: str
    target_metric: str
    target_horizon: str
    expected_relationship: str
    comparison_basis: str
    raw_qualifying_rows: int | None
    collapsed_events: int | None
    independent_events: int | None
    outcome_available_events: int
    matched_count: int
    unmatched_count: int
    outcome_unavailable_count: int
    event_references: tuple[PersistedEventReferenceV1, ...]
    match_references: tuple[PersistedMatchReferenceV1, ...]
    event_values: tuple[float, ...]
    control_values: tuple[float, ...]
    event_mean: float | None
    event_median: float | None
    control_mean: float | None
    control_median: float | None
    incremental_mean: float | None
    incremental_median: float | None
    year_slices: tuple[YearEvidenceSliceV1, ...]
    evidence_status: str
    reason_codes: tuple[str, ...]
