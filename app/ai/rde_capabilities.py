"""Fail-closed resolver for frozen, verified event-cache capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.ai.research_query_compiler import CanonicalRDEQueryV1
from app.ai.research_reasoning import RELATIONSHIPS, TARGET_HORIZONS, TARGET_METRICS
from app.ai.schemas import digest
from app.research_event_bridge import EVENT_TYPES

CAPABILITY_RESOLVER_IDENTITY = "rde_capability_resolver/1.0.0"
METHODOLOGY_IDENTITY = "compact_matched_event_research/1.0.0"
METHODOLOGY_NAME = "compact_matched_event_research"
METHODOLOGY_DIGEST = "b52776bd5045532f83054fd43e6cdd8929d2de95d61301269b8b05751ebff690"
CAPABILITY_MATRIX_V1 = {
    "RETURN": "DERIVABLE_BUT_NOT_CANONICAL",
    "OI_STATE": "DERIVABLE_BUT_NOT_CANONICAL",
    "FUNDING_STATE": "DERIVABLE_BUT_NOT_CANONICAL",
    "POSITIVE_BREADTH": "UNSUPPORTED",
    "RELATIVE_STRENGTH": "UNSUPPORTED",
    "TAKER_PRESSURE": "DERIVABLE_BUT_NOT_CANONICAL",
    "EVENT_TYPE": "EXECUTABLE_EXISTING_CACHE",
    "EVENT_PRESENT": "UNSUPPORTED",
}


class CapabilityStatusV1(StrEnum):
    EXECUTABLE_EXISTING_CACHE = "EXECUTABLE_EXISTING_CACHE"
    QUERYABLE_NOT_EXECUTABLE = "QUERYABLE_NOT_EXECUTABLE"
    UNSUPPORTED_PREDICATE = "UNSUPPORTED_PREDICATE"
    METHODOLOGY_EXTENSION_REQUIRED = "METHODOLOGY_EXTENSION_REQUIRED"
    INVALID_QUERY = "INVALID_QUERY"


def capability_resolver_digest() -> str:
    return digest(
        {
            "identity": CAPABILITY_RESOLVER_IDENTITY,
            "matrix": CAPABILITY_MATRIX_V1,
            "event_families": EVENT_TYPES,
            "methodology_identity": METHODOLOGY_IDENTITY,
            "methodology_digest": METHODOLOGY_DIGEST,
        }
    )


@dataclass(frozen=True, slots=True)
class ExistingEventResearchQueryV1:
    query_digest: str
    asset: str
    event_family: str
    variant: str
    target_metric: str
    target_horizon: str
    expected_relationship: str
    comparison_basis: str
    existing_cache_identity: str
    event_match_cache_identity: str
    outcome_cache_identity: str
    methodology_name: str = METHODOLOGY_NAME
    methodology_version: str = "1.0.0"
    methodology_digest: str = METHODOLOGY_DIGEST

    def __post_init__(self) -> None:
        if self.event_family not in EVENT_TYPES or self.variant != "BASE":
            raise ValueError(
                "existing event query requires an exact frozen family and BASE variant"
            )
        if self.comparison_basis != "MATCHED_CONTROL" or not self.existing_cache_identity:
            raise ValueError("existing event query requires cache identity and MATCHED_CONTROL")
        if (
            not self.asset
            or self.target_metric not in TARGET_METRICS
            or self.target_horizon not in TARGET_HORIZONS
            or self.expected_relationship not in RELATIONSHIPS
            or not self.event_match_cache_identity
            or not self.outcome_cache_identity
        ):
            raise ValueError("existing event query has an invalid execution contract")
        if (
            self.methodology_name != METHODOLOGY_IDENTITY.rsplit("/", 1)[0]
            or self.methodology_version != "1.0.0"
            or self.methodology_digest != METHODOLOGY_DIGEST
        ):
            raise ValueError("existing event query methodology identity mismatch")


@dataclass(frozen=True, slots=True)
class RDECapabilityAssessmentV1:
    status: CapabilityStatusV1
    existing_event_query: ExistingEventResearchQueryV1 | None = None
    reason_codes: tuple[str, ...] = ()


class RDECapabilityResolverV1:
    def resolve(self, query: CanonicalRDEQueryV1) -> RDECapabilityAssessmentV1:
        if len(query.predicates) != 1:
            return RDECapabilityAssessmentV1(
                CapabilityStatusV1.METHODOLOGY_EXTENSION_REQUIRED,
                reason_codes=("MULTI_PREDICATE_EVENT_EXTRACTION_NOT_CANONICAL",),
            )
        predicate = query.predicates[0]
        classification = CAPABILITY_MATRIX_V1.get(predicate.canonical_field)
        if classification == "UNSUPPORTED":
            return RDECapabilityAssessmentV1(CapabilityStatusV1.UNSUPPORTED_PREDICATE)
        if classification != "EXECUTABLE_EXISTING_CACHE":
            return RDECapabilityAssessmentV1(
                CapabilityStatusV1.METHODOLOGY_EXTENSION_REQUIRED,
                reason_codes=("GENERIC_PREDICATE_EXTRACTION_NOT_CANONICAL",),
            )
        if predicate.operator != "EQUALS" or not isinstance(predicate.typed_value, str):
            return RDECapabilityAssessmentV1(CapabilityStatusV1.INVALID_QUERY)
        if predicate.typed_value not in EVENT_TYPES:
            return RDECapabilityAssessmentV1(
                CapabilityStatusV1.INVALID_QUERY, reason_codes=("UNKNOWN_EVENT_FAMILY",)
            )
        return RDECapabilityAssessmentV1(
            CapabilityStatusV1.EXECUTABLE_EXISTING_CACHE,
            ExistingEventResearchQueryV1(
                query.query_digest,
                query.target_asset,
                predicate.typed_value,
                "BASE",
                query.target_metric,
                query.target_horizon,
                query.expected_relationship,
                query.comparison_basis,
                digest(
                    {
                        "asset": query.target_asset,
                        "event_family": predicate.typed_value,
                        "methodology": METHODOLOGY_DIGEST,
                    }
                ),
                "event-match-cache-identity-required-at-execution",
                "outcome-cache-identity-required-at-execution",
            ),
        )
