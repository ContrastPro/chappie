"""Immutable, UTC-only Capital Flow V1 authority contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.ai.schemas import digest

PROVIDER_AUTHORITY_IDENTITY = "chappie_capital_flow_provider_authority/1.0.0"
SOURCE_SERIES_MANIFEST_IDENTITY = "chappie_capital_flow_source_series_manifest/1.0.0"
ALIGNED_CONTEXT_IDENTITY = "chappie_capital_flow_aligned_context/1.0.0"
FEATURE_SIDECAR_IDENTITY = "chappie_capital_flow_feature_sidecar/1.0.0"


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be an aware UTC datetime")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class TemporalSemanticsV1:
    event_time: datetime
    source_observed_at: datetime | None
    published_at: datetime | None
    usable_at: datetime
    ingested_at: datetime

    def __post_init__(self) -> None:
        event = _utc(self.event_time, "event_time")
        usable = _utc(self.usable_at, "usable_at")
        ingested = _utc(self.ingested_at, "ingested_at")
        observed = (
            _utc(self.source_observed_at, "source_observed_at")
            if self.source_observed_at is not None
            else None
        )
        published = (
            _utc(self.published_at, "published_at") if self.published_at is not None else None
        )
        if usable < event:
            raise ValueError("usable_at cannot precede event_time")
        if ingested < usable:
            raise ValueError("ingested_at cannot precede usable_at")
        if observed is not None and observed < event:
            raise ValueError("source_observed_at cannot precede event_time")
        if published is not None and published < event:
            raise ValueError("published_at cannot precede event_time")

    def age_at(self, target_time: datetime) -> timedelta:
        target = _utc(target_time, "target_time")
        if self.usable_at > target:
            raise ValueError("future publication is unavailable")
        return target - self.event_time


@dataclass(frozen=True, slots=True)
class StalenessPolicyV1:
    policy_id: str
    maximum_age: timedelta
    regular_session_only: bool = False

    def __post_init__(self) -> None:
        if not self.policy_id or self.maximum_age <= timedelta(0):
            raise ValueError("staleness policy must have an id and positive maximum_age")


@dataclass(frozen=True, slots=True)
class ProviderAuthorityV1:
    provider_id: str
    endpoint_or_series: str
    feed_or_entitlement: str
    native_frequency: str
    units: str
    timezone: str
    event_time_semantics: str
    publication_time_semantics: str
    usable_at_semantics: str
    revision_or_vintage_semantics: str
    license_classification: str
    dominance_semantics: str | None
    request_parameters: tuple[tuple[str, str], ...]
    metadata_status: str
    retention_status: str
    blocker: str | None = None

    def __post_init__(self) -> None:
        required = (
            self.provider_id,
            self.endpoint_or_series,
            self.feed_or_entitlement,
            self.native_frequency,
            self.units,
            self.timezone,
            self.event_time_semantics,
            self.publication_time_semantics,
            self.usable_at_semantics,
            self.revision_or_vintage_semantics,
            self.license_classification,
            self.metadata_status,
            self.retention_status,
        )
        if not all(required):
            raise ValueError("provider authority fields are required")
        if self.request_parameters != tuple(sorted(set(self.request_parameters))):
            raise ValueError("request parameters must be sorted and unique")

    @property
    def semantic_digest(self) -> str:
        return digest(
            {
                "identity": PROVIDER_AUTHORITY_IDENTITY,
                "provider_id": self.provider_id,
                "endpoint_or_series": self.endpoint_or_series,
                "feed_or_entitlement": self.feed_or_entitlement,
                "native_frequency": self.native_frequency,
                "units": self.units,
                "timezone": self.timezone,
                "event_time_semantics": self.event_time_semantics,
                "publication_time_semantics": self.publication_time_semantics,
                "usable_at_semantics": self.usable_at_semantics,
                "revision_or_vintage_semantics": self.revision_or_vintage_semantics,
                "license_classification": self.license_classification,
                "dominance_semantics": self.dominance_semantics,
                "request_parameters": self.request_parameters,
                "metadata_status": self.metadata_status,
                "retention_status": self.retention_status,
                "blocker": self.blocker,
            }
        )


@dataclass(frozen=True, slots=True)
class SourceRecordV1:
    authority: ProviderAuthorityV1
    source_series_id: str
    symbol_or_asset_id: str
    temporal: TemporalSemanticsV1
    raw_digest: str
    value_available: bool
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.source_series_id or not self.symbol_or_asset_id or len(self.raw_digest) != 64:
            raise ValueError("source record identity or raw digest is invalid")
        if self.value_available == (self.unavailable_reason is not None):
            raise ValueError("missingness must be explicit and exclusive")
