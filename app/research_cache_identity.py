"""Additive provenance binding for already-verified compact research caches."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from app.ai.schemas import digest

IDENTITY_SCHEMA = "verified_research_cache_identity/1.0.0"
METHODOLOGY_NAME = "compact_matched_event_research"
METHODOLOGY_VERSION = "1.0.0"
METHODOLOGY_DIGEST = "b52776bd5045532f83054fd43e6cdd8929d2de95d61301269b8b05751ebff690"


class IdentityVerificationStatus(StrEnum):
    VALID = "VALID"
    INVALID = "INVALID"


@dataclass(frozen=True, slots=True)
class VerifiedResearchCacheIdentityV1:
    """Atomic event+match binding mirrors event_match_manifest_v1 persistence."""

    asset: str
    aligned_content_digest: str
    outcome_schema: str
    outcome_config_digest: str
    outcome_content_digest: str
    outcome_verified: bool
    event_match_schema: str
    event_match_config_digest: str
    event_match_content_digest: str
    event_match_verified: bool
    methodology_name: str = METHODOLOGY_NAME
    methodology_version: str = METHODOLOGY_VERSION
    methodology_digest: str = METHODOLOGY_DIGEST
    created_from_existing_verified_artifacts: bool = True

    @property
    def research_cache_identity_digest(self) -> str:
        return digest(
            {
                "schema": IDENTITY_SCHEMA,
                "asset": self.asset,
                "methodology": [
                    self.methodology_name,
                    self.methodology_version,
                    self.methodology_digest,
                ],
                "aligned_content_digest": self.aligned_content_digest,
                "outcome": [
                    self.outcome_schema,
                    self.outcome_config_digest,
                    self.outcome_content_digest,
                    self.outcome_verified,
                ],
                "event_match": [
                    self.event_match_schema,
                    self.event_match_config_digest,
                    self.event_match_content_digest,
                    self.event_match_verified,
                ],
                "additive_provenance": self.created_from_existing_verified_artifacts,
            }
        )


def verify_research_cache_identity(
    identity: VerifiedResearchCacheIdentityV1,
    *,
    asset: str,
    aligned_content_digest: str,
    outcome_content_digest: str,
    event_match_content_digest: str,
) -> IdentityVerificationStatus:
    if (
        identity.methodology_name != METHODOLOGY_NAME
        or identity.methodology_version != METHODOLOGY_VERSION
        or identity.methodology_digest != METHODOLOGY_DIGEST
        or identity.asset != asset
        or identity.aligned_content_digest != aligned_content_digest
        or identity.outcome_content_digest != outcome_content_digest
        or identity.event_match_content_digest != event_match_content_digest
        or not identity.outcome_verified
        or not identity.event_match_verified
    ):
        return IdentityVerificationStatus.INVALID
    return IdentityVerificationStatus.VALID


def build_verified_research_cache_identity(
    aligned: Mapping[str, object], outcome: Mapping[str, object], event_match: Mapping[str, object]
) -> VerifiedResearchCacheIdentityV1 | None:
    """Bind only durable canonical manifest values; never repair or infer them."""
    required = (
        (aligned, ("asset", "state", "alignment_schema", "content_digest")),
        (
            outcome,
            (
                "asset",
                "state",
                "outcome_schema",
                "aligned_content_digest",
                "outcome_config_digest",
                "content_digest",
            ),
        ),
        (
            event_match,
            (
                "asset",
                "state",
                "bridge_schema",
                "aligned_content_digest",
                "outcome_content_digest",
                "config_digest",
                "content_digest",
            ),
        ),
    )
    if any(any(not row.get(key) for key in keys) for row, keys in required):
        return None
    asset = str(aligned["asset"])
    if (
        outcome["asset"] != asset
        or event_match["asset"] != asset
        or aligned["state"] != "VERIFIED"
        or outcome["state"] != "VERIFIED"
        or event_match["state"] != "VERIFIED"
        or outcome["aligned_content_digest"] != aligned["content_digest"]
        or event_match["aligned_content_digest"] != aligned["content_digest"]
        or event_match["outcome_content_digest"] != outcome["content_digest"]
    ):
        return None
    return VerifiedResearchCacheIdentityV1(
        asset=asset,
        aligned_content_digest=str(aligned["content_digest"]),
        outcome_schema=str(outcome["outcome_schema"]),
        outcome_config_digest=str(outcome["outcome_config_digest"]),
        outcome_content_digest=str(outcome["content_digest"]),
        outcome_verified=True,
        event_match_schema=str(event_match["bridge_schema"]),
        event_match_config_digest=str(event_match["config_digest"]),
        event_match_content_digest=str(event_match["content_digest"]),
        event_match_verified=True,
    )
