"""Deterministic orchestration and publication for Precursor Discovery V1 evidence."""

from __future__ import annotations

import json
import os
import tempfile
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum, StrEnum
from hashlib import sha256
from itertools import pairwise
from pathlib import Path

from .analysis import (
    DISCOVERY_CONDITION_CATALOGUE_IDENTITY,
    PRIMARY_DISCOVERY_HORIZONS_V1,
    AllOfConditionV1,
    ConditionTemporalConcentrationV1,
    DiscoveryConditionIdentityV1,
    EventBaselineV1,
    EventDefinitionV1,
    EventDirectionV1,
    FrozenFeatureQuantilesV1,
    MultiHorizonFrozenConditionObservationV1,
    PairedFrozenConditionEvidenceV1,
    PrecursorCandidateAssessmentV1,
    PrecursorCandidateClassificationV1,
    PrecursorRankingCategoryV1,
    QuantileConditionSpecV1,
    assess_precursor_candidate,
    baseline,
    bind_discovery_condition_template,
    calculate_condition_temporal_concentration,
    discovery_condition_catalogue_v1,
    evaluate_frozen_condition_pair,
    evaluate_frozen_condition_stability,
    fit_feature_research_quantiles,
    no_validated_precursor_found,
    project_observation_for_horizon,
    rank_precursor_candidates,
)

PRECURSOR_DISCOVERY_REPORT_IDENTITY = "chappie_precursor_discovery_report/2.0.0"
QUANTILE_POLICY_IDENTITY_V1 = "chappie_feature_research_quantiles/1.0.0"
SAMPLE_POLICY_IDENTITY_V1 = "chappie_precursor_sample_policy/1.0.0"
CONCENTRATION_POLICY_IDENTITY_V1 = "chappie_precursor_concentration_policy/1.0.0"
CLASSIFICATION_POLICY_IDENTITY_V1 = "chappie_precursor_classification_policy/1.0.0"


class DiscoveryProgressStageV1(StrEnum):
    VALIDATE_INPUT = "VALIDATE_INPUT"
    FIT_QUANTILES = "FIT_QUANTILES"
    BIND_CATALOGUE = "BIND_CATALOGUE"
    BASELINES = "BASELINES"
    EVALUATE_ASSESSMENTS = "EVALUATE_ASSESSMENTS"
    STABILITY_CLASSIFICATION = "STABILITY_CLASSIFICATION"
    RANK = "RANK"
    COMPLETE = "COMPLETE"


@dataclass(frozen=True, slots=True)
class DiscoveryProgressV1:
    stage: DiscoveryProgressStageV1
    current: int | None = None
    total: int | None = None


@dataclass(frozen=True, slots=True)
class DiscoveryProvenanceBindingsV1:
    source_head: str
    base_precursor_identity: str
    base_precursor_digest: str
    context_feature_identity: str
    context_dataset_identity: str
    context_feature_digest: str
    context_authority_identity: str
    context_authority_digest: str

    def __post_init__(self) -> None:
        if any(not value for value in self.__dict_values()):
            raise ValueError("discovery provenance bindings are incomplete")

    def __dict_values(self) -> tuple[str, ...]:
        return tuple(getattr(self, field.name) for field in fields(self))


@dataclass(frozen=True, slots=True)
class DiscoveryExecutionConfigV1:
    research_split_identity: str
    validation_split_identity: str
    event_definitions: tuple[EventDefinitionV1, ...]
    catalogue_identity: str
    quantile_policy_identity: str
    sample_policy_identity: str
    concentration_policy_identity: str
    classification_policy_identity: str
    ranking_limit: int

    def __post_init__(self) -> None:
        events = tuple(self.event_definitions)
        if events != primary_event_definitions_v1():
            raise ValueError("Discovery V1 primary event matrix is invalid")
        if (
            not self.research_split_identity
            or not self.validation_split_identity
            or self.catalogue_identity != DISCOVERY_CONDITION_CATALOGUE_IDENTITY
            or self.quantile_policy_identity != QUANTILE_POLICY_IDENTITY_V1
            or self.sample_policy_identity != SAMPLE_POLICY_IDENTITY_V1
            or self.concentration_policy_identity != CONCENTRATION_POLICY_IDENTITY_V1
            or self.classification_policy_identity != CLASSIFICATION_POLICY_IDENTITY_V1
            or self.ranking_limit != 5
        ):
            raise ValueError("Discovery V1 execution configuration is invalid")
        object.__setattr__(self, "event_definitions", events)


def primary_event_definitions_v1() -> tuple[EventDefinitionV1, ...]:
    return tuple(
        EventDefinitionV1(horizon, threshold, direction)
        for horizon in PRIMARY_DISCOVERY_HORIZONS_V1
        for threshold in (40, 60)
        for direction in EventDirectionV1
    )


def discovery_execution_config_v1(
    research_split_identity: str, validation_split_identity: str
) -> DiscoveryExecutionConfigV1:
    return DiscoveryExecutionConfigV1(
        research_split_identity,
        validation_split_identity,
        primary_event_definitions_v1(),
        DISCOVERY_CONDITION_CATALOGUE_IDENTITY,
        QUANTILE_POLICY_IDENTITY_V1,
        SAMPLE_POLICY_IDENTITY_V1,
        CONCENTRATION_POLICY_IDENTITY_V1,
        CLASSIFICATION_POLICY_IDENTITY_V1,
        5,
    )


@dataclass(frozen=True, slots=True)
class DiscoveryEventBaselineV1:
    event_definition: EventDefinitionV1
    research_baseline: EventBaselineV1
    validation_baseline: EventBaselineV1


@dataclass(frozen=True, slots=True)
class DiscoveryRankingsV1:
    upside_precursors: tuple[PrecursorCandidateAssessmentV1, ...]
    downside_precursors: tuple[PrecursorCandidateAssessmentV1, ...]
    volatility_precursors: tuple[PrecursorCandidateAssessmentV1, ...]


@dataclass(frozen=True, slots=True)
class PrecursorDiscoveryReportV1:
    identity: str
    execution_config: DiscoveryExecutionConfigV1
    provenance: DiscoveryProvenanceBindingsV1
    research_observation_count: int
    validation_observation_count: int
    frozen_quantiles: tuple[FrozenFeatureQuantilesV1, ...]
    catalogue_identity: str
    catalogue_condition_identities: tuple[DiscoveryConditionIdentityV1, ...]
    event_matrix: tuple[EventDefinitionV1, ...]
    baselines: tuple[DiscoveryEventBaselineV1, ...]
    assessments: tuple[PrecursorCandidateAssessmentV1, ...]
    classification_counts: tuple[tuple[PrecursorCandidateClassificationV1, int], ...]
    rankings: DiscoveryRankingsV1
    no_validated_precursor_found_v1: bool
    created_at_utc: str
    semantic_digest: str


ProgressCallback = Callable[[DiscoveryProgressV1], None]


def _emit(
    callback: ProgressCallback | None,
    stage: DiscoveryProgressStageV1,
    current: int | None = None,
    total: int | None = None,
) -> None:
    if callback is not None:
        callback(DiscoveryProgressV1(stage, current, total))


def _validate_observations(
    observations: tuple[MultiHorizonFrozenConditionObservationV1, ...], split: str
) -> None:
    if not observations:
        raise ValueError(f"{split} observations are required")
    timestamps = tuple(item.timestamp_ms for item in observations)
    if any(current <= previous for previous, current in pairwise(timestamps)):
        raise ValueError(f"{split} observations must have strictly increasing timestamps")


def _required_quantile_features() -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                child.feature_name
                for entry in discovery_condition_catalogue_v1().entries
                for child in entry.children
                if isinstance(child, QuantileConditionSpecV1)
            }
        )
    )


def _project_by_horizon(observations):
    return {
        horizon: tuple(project_observation_for_horizon(item, horizon) for item in observations)
        for horizon in PRIMARY_DISCOVERY_HORIZONS_V1
    }


def execute_precursor_discovery_v1(
    feature_research_observations: Iterable[MultiHorizonFrozenConditionObservationV1],
    validation_observations: Iterable[MultiHorizonFrozenConditionObservationV1],
    config: DiscoveryExecutionConfigV1,
    provenance: DiscoveryProvenanceBindingsV1,
    *,
    created_at_utc: str | None = None,
    progress_callback: ProgressCallback | None = None,
) -> PrecursorDiscoveryReportV1:
    _emit(progress_callback, DiscoveryProgressStageV1.VALIDATE_INPUT)
    research = tuple(feature_research_observations)
    validation = tuple(validation_observations)
    _validate_observations(research, "FEATURE_RESEARCH")
    _validate_observations(validation, "VALIDATION")
    catalogue = discovery_condition_catalogue_v1()

    _emit(progress_callback, DiscoveryProgressStageV1.FIT_QUANTILES)
    required_features = _required_quantile_features()
    frozen_quantiles = tuple(
        fit_feature_research_quantiles(
            feature_name, (item.features[feature_name] for item in research)
        )
        for feature_name in required_features
    )
    quantiles_by_feature = {item.feature_name: item for item in frozen_quantiles}

    _emit(progress_callback, DiscoveryProgressStageV1.BIND_CATALOGUE)
    bound_conditions: tuple[tuple[DiscoveryConditionIdentityV1, AllOfConditionV1], ...] = tuple(
        (
            template.identity,
            bind_discovery_condition_template(template, quantiles_by_feature),
        )
        for template in catalogue.entries
    )
    research_by_horizon = _project_by_horizon(research)
    validation_by_horizon = _project_by_horizon(validation)

    _emit(progress_callback, DiscoveryProgressStageV1.BASELINES)
    baselines = tuple(
        DiscoveryEventBaselineV1(
            event,
            baseline(
                tuple(
                    _label_from_observation(item)
                    for item in research_by_horizon[event.horizon_minutes]
                ),
                event,
            ),
            baseline(
                tuple(
                    _label_from_observation(item)
                    for item in validation_by_horizon[event.horizon_minutes]
                ),
                event,
            ),
        )
        for event in config.event_definitions
    )

    concentration_by_condition: dict[
        DiscoveryConditionIdentityV1,
        tuple[ConditionTemporalConcentrationV1, ConditionTemporalConcentrationV1],
    ] = {
        identity: (
            calculate_condition_temporal_concentration(research_by_horizon[15], condition),
            calculate_condition_temporal_concentration(validation_by_horizon[15], condition),
        )
        for identity, condition in bound_conditions
    }
    total = len(bound_conditions) * len(config.event_definitions)
    paired_items: list[
        tuple[
            PairedFrozenConditionEvidenceV1,
            ConditionTemporalConcentrationV1,
            ConditionTemporalConcentrationV1,
        ]
    ] = []
    for current, (identity, condition, event) in enumerate(
        (
            (identity, condition, event)
            for identity, condition in bound_conditions
            for event in config.event_definitions
        ),
        start=1,
    ):
        paired = evaluate_frozen_condition_pair(
            identity,
            condition,
            event,
            research_by_horizon[event.horizon_minutes],
            validation_by_horizon[event.horizon_minutes],
        )
        paired_items.append((paired, *concentration_by_condition[identity]))
        _emit(
            progress_callback,
            DiscoveryProgressStageV1.EVALUATE_ASSESSMENTS,
            current,
            total,
        )

    _emit(
        progress_callback,
        DiscoveryProgressStageV1.STABILITY_CLASSIFICATION,
        0,
        total,
    )
    assessments = tuple(
        assess_precursor_candidate(
            paired,
            evaluate_frozen_condition_stability(
                paired, research_concentration, validation_concentration
            ),
            research_concentration,
            validation_concentration,
        )
        for paired, research_concentration, validation_concentration in paired_items
    )
    counts = Counter(item.classification for item in assessments)
    classification_counts = tuple(
        (classification, counts[classification])
        for classification in PrecursorCandidateClassificationV1
    )

    _emit(progress_callback, DiscoveryProgressStageV1.RANK)
    rankings = DiscoveryRankingsV1(
        rank_precursor_candidates(
            assessments, PrecursorRankingCategoryV1.UPSIDE_PRECURSORS, config.ranking_limit
        ),
        rank_precursor_candidates(
            assessments, PrecursorRankingCategoryV1.DOWNSIDE_PRECURSORS, config.ranking_limit
        ),
        rank_precursor_candidates(
            assessments, PrecursorRankingCategoryV1.VOLATILITY_PRECURSORS, config.ranking_limit
        ),
    )
    report = PrecursorDiscoveryReportV1(
        PRECURSOR_DISCOVERY_REPORT_IDENTITY,
        config,
        provenance,
        len(research),
        len(validation),
        frozen_quantiles,
        catalogue.identity,
        tuple(entry.identity for entry in catalogue.entries),
        config.event_definitions,
        baselines,
        assessments,
        classification_counts,
        rankings,
        no_validated_precursor_found(assessments),
        created_at_utc or datetime.now(UTC).isoformat(),
        "",
    )
    report = replace(report, semantic_digest=_semantic_digest(report))
    _emit(progress_callback, DiscoveryProgressStageV1.COMPLETE)
    return report


def _label_from_observation(observation):
    from .analysis import FutureLabelV1

    return FutureLabelV1(
        observation.timestamp_ms,
        observation.future_return,
        observation.favorable_excursion,
        observation.adverse_excursion,
    )


def _normalize(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {field.name: _normalize(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {
            str(key): _normalize(item)
            for key, item in sorted(value.items(), key=lambda x: str(x[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_normalize(item) for item in value]
    return value


def precursor_discovery_report_payload(report: PrecursorDiscoveryReportV1) -> dict[str, object]:
    return _normalize(report)


def _semantic_digest(report: PrecursorDiscoveryReportV1) -> str:
    payload = precursor_discovery_report_payload(report)
    payload.pop("created_at_utc")
    payload.pop("semantic_digest")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return sha256(encoded.encode("utf-8")).hexdigest()


def write_precursor_discovery_report(report: PrecursorDiscoveryReportV1, path: Path) -> Path:
    destination = Path(path)
    if destination.exists():
        raise FileExistsError(f"authoritative report already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = precursor_discovery_report_payload(report)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=destination.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, destination)
        temporary.unlink()
        return destination
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
