"""Capital Flow V1 authority contracts; no provider clients or feature calculations."""

from .alignment import AlignmentDecisionV1, align_fixture_record
from .catalogue import FEATURE_CATALOGUE_IDENTITY, FEATURE_CATALOGUE_V1, FEATURE_FAMILIES_V1
from .contracts import (
    ALIGNED_CONTEXT_IDENTITY,
    FEATURE_SIDECAR_IDENTITY,
    PROVIDER_AUTHORITY_IDENTITY,
    SOURCE_SERIES_MANIFEST_IDENTITY,
    ProviderAuthorityV1,
    SourceRecordV1,
    TemporalSemanticsV1,
)
from .free_v1 import (
    ACTIVE_FREE_STATUS,
    DEFERRED_PAID_AUTHORITIES_V1,
    FREE_AUTHORITY_IDENTITY,
    FREE_FAMILIES_V1,
    FREE_FEATURE_CATALOGUE_IDENTITY,
    FREE_FEATURE_CATALOGUE_V1,
    CatalogueSupersessionV1,
    FreeAuthoritySemanticsV1,
    FreeResearchProtocolV1,
)
from .providers import BREADTH_SYMBOLS_V1, PROVIDER_AUTHORITIES_V1, STALENESS_POLICIES_V1

__all__ = [
    "ACTIVE_FREE_STATUS",
    "ALIGNED_CONTEXT_IDENTITY",
    "BREADTH_SYMBOLS_V1",
    "DEFERRED_PAID_AUTHORITIES_V1",
    "FEATURE_CATALOGUE_IDENTITY",
    "FEATURE_CATALOGUE_V1",
    "FEATURE_FAMILIES_V1",
    "FEATURE_SIDECAR_IDENTITY",
    "FREE_AUTHORITY_IDENTITY",
    "FREE_FAMILIES_V1",
    "FREE_FEATURE_CATALOGUE_IDENTITY",
    "FREE_FEATURE_CATALOGUE_V1",
    "PROVIDER_AUTHORITIES_V1",
    "PROVIDER_AUTHORITY_IDENTITY",
    "SOURCE_SERIES_MANIFEST_IDENTITY",
    "STALENESS_POLICIES_V1",
    "AlignmentDecisionV1",
    "CatalogueSupersessionV1",
    "FreeAuthoritySemanticsV1",
    "FreeResearchProtocolV1",
    "ProviderAuthorityV1",
    "SourceRecordV1",
    "TemporalSemanticsV1",
    "align_fixture_record",
]
