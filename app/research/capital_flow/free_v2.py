"""Active Free V2 catalogue contracts derived from, but not rewriting, Free V1."""

from __future__ import annotations

from dataclasses import dataclass

from .fred_alfred_v2 import DEFERRED_GOLD_FEATURES_V2
from .free_v1 import (
    FREE_FAMILIES_V1,
    FREE_FEATURE_CATALOGUE_IDENTITY,
    FREE_FEATURE_CATALOGUE_V1,
    FreeFeatureSpecV1,
)

FREE_FEATURE_CATALOGUE_V2_IDENTITY = "chappie_global_risk_capital_rotation_free_catalogue/2.0.0"
V1_CATALOGUE_STATUS = "HISTORICAL_REPRODUCIBLE"
V1_CATALOGUE_SUPERSESSION = "SUPERSEDED_BY_2.0.0"
ACTIVE_FREE_V2_STATUS = "ACTIVE_FREE_RESEARCH_CATALOGUE"

FREE_FAMILIES_V2 = FREE_FAMILIES_V1
VIX_FEATURES_V2 = ("risk.vix_change_1d", "risk.vix_zscore_60sessions")


@dataclass(frozen=True, slots=True)
class CatalogueSupersessionV2:
    v1_identity: str = FREE_FEATURE_CATALOGUE_IDENTITY
    v1_status: str = V1_CATALOGUE_STATUS
    v1_supersession: str = V1_CATALOGUE_SUPERSESSION
    active_identity: str = FREE_FEATURE_CATALOGUE_V2_IDENTITY
    active_status: str = ACTIVE_FREE_V2_STATUS


@dataclass(frozen=True, slots=True)
class FreeFeatureSpecV2:
    name: str
    family: str
    formula_identity: str
    authorities: tuple[str, ...]
    window: str
    lag: str
    units: str
    update_frequency: str
    staleness_policy: str


def _v2_feature(feature: FreeFeatureSpecV1) -> FreeFeatureSpecV2:
    authorities = feature.authorities
    if feature.name in VIX_FEATURES_V2:
        authorities = ("FRED_ALFRED_V2_VIXCLS",)
    return FreeFeatureSpecV2(
        name=feature.name,
        family=feature.family,
        formula_identity=feature.formula_identity,
        authorities=authorities,
        window=feature.window,
        lag=feature.lag,
        units=feature.units,
        update_frequency=feature.update_frequency,
        staleness_policy=feature.staleness_policy,
    )


FREE_FEATURE_CATALOGUE_V2 = tuple(
    _v2_feature(feature)
    for feature in FREE_FEATURE_CATALOGUE_V1
    if feature.name not in DEFERRED_GOLD_FEATURES_V2
)
FREE_FEATURE_NAMES_V2 = tuple(feature.name for feature in FREE_FEATURE_CATALOGUE_V2)
FREE_FAMILY_MEMBERSHIP_V2 = tuple(
    (
        family,
        tuple(feature.name for feature in FREE_FEATURE_CATALOGUE_V2 if feature.family == family),
    )
    for family in FREE_FAMILIES_V2
)

if len(FREE_FEATURE_CATALOGUE_V2) != 15 or len(set(FREE_FEATURE_NAMES_V2)) != 15:
    raise RuntimeError("Free V2 catalogue must contain exactly 15 unique features")
