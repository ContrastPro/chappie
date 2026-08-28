"""Immutable sanitized DTOs for FRED/ALFRED V2 request planning."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from ..fred_alfred_v2 import (
    FRED_ALFRED_GLOBAL_RISK_AUTHORITY_V2_IDENTITY,
    FRED_CREDENTIAL_ENVIRONMENT_VARIABLE_V2,
    FRED_CREDENTIAL_EXCLUDED_FROM_V2,
    FRED_SERIES_V2,
    PIT_CLASSIFICATIONS_V2,
    PROSPECTIVE_BLIND_OOS_RANGE_V2,
    SOURCE_RANGE_V2,
)

FRED_OFFICIAL_API_HOST = "https://api.stlouisfed.org"
FRED_OFFICIAL_PROVIDER_IDENTITY = "ST_LOUIS_FED_FRED_ALFRED_OFFICIAL_API"
FRED_SERIES_OBSERVATIONS_ENDPOINT = "fred/series/observations"
FRED_SERIES_VINTAGEDATES_ENDPOINT = "fred/series/vintagedates"
FRED_ALLOWED_ENDPOINTS = (
    FRED_SERIES_OBSERVATIONS_ENDPOINT,
    FRED_SERIES_VINTAGEDATES_ENDPOINT,
)
FRED_OBSERVATIONS_LIMIT = 100_000
FRED_VINTAGEDATES_LIMIT = 10_000
FULL_VINTAGE_BATCH_SIZE = 50
MAX_AUTHORIZED_CALENDAR_DATES = 1_004
MAX_FULL_VINTAGE_OBSERVATION_PARTS = 21

RELEASE_VINTAGE_SERIES_V2 = ("DEXJPUS", "NASDAQCOM", "DGS2", "DGS10", "VIXCLS")
FULL_VINTAGE_SERIES_V2 = ("DTWEXBGS", "STLFSI4")

PROVIDER_ERROR_CATEGORIES_V2 = (
    "AUTHENTICATION_FAILURE",
    "AUTHORIZATION_FAILURE",
    "RATE_LIMITED",
    "TRANSIENT_PROVIDER_FAILURE",
    "PERMANENT_PROVIDER_FAILURE",
    "MALFORMED_PROVIDER_RESPONSE",
)
MISSINGNESS_STATES_V2 = (
    "VALID_SOURCE_ABSENCE",
    "ACQUISITION_GAP",
    "MALFORMED_SOURCE_RECORD",
)
PAGE_COUNT_VALIDATION_INVARIANTS_V2 = (
    "PROVIDER_COUNT_NON_NEGATIVE",
    "OFFSET_NON_NEGATIVE",
    "LIMIT_WITHIN_ENDPOINT_MAXIMUM",
    "RETURNED_RECORDS_NOT_GREATER_THAN_LIMIT",
    "NO_PREMATURE_EMPTY_PAGE_WHEN_PROVIDER_COUNT_HAS_MORE",
    "NO_REPEATED_OFFSET",
    "NO_OFFSET_REGRESSION",
    "NO_DUPLICATE_PAGE_IDENTITY",
)


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _validate_series(series_id: str) -> None:
    if series_id not in FRED_SERIES_V2:
        raise ValueError("series is outside the frozen FRED/ALFRED V2 authority")


@dataclass(frozen=True, slots=True)
class PaginationContractV2:
    limit: int
    initial_offset: int
    offset_progression: str
    max_parts: int
    termination_expectation: str
    validation_invariants: tuple[str, ...] = PAGE_COUNT_VALIDATION_INVARIANTS_V2

    def __post_init__(self) -> None:
        if self.limit <= 0 or self.initial_offset != 0 or self.max_parts <= 0:
            raise ValueError("invalid finite pagination contract")


@dataclass(frozen=True, slots=True)
class RequestPartV2:
    series_id: str
    endpoint: str
    parameters: tuple[tuple[str, str], ...]
    part_kind: str
    part_index: int
    pit_strategy: str
    authorized_reference_bounds: tuple[str, str]
    pagination: PaginationContractV2
    provider_identity: str = FRED_OFFICIAL_PROVIDER_IDENTITY

    def __post_init__(self) -> None:
        _validate_series(self.series_id)
        if self.endpoint not in FRED_ALLOWED_ENDPOINTS:
            raise ValueError("endpoint is outside the frozen FRED API family")
        if self.part_index < 0:
            raise ValueError("request part index must be non-negative")
        normalized = tuple(sorted(self.parameters))
        if len({key for key, _ in normalized}) != len(normalized):
            raise ValueError("duplicate request parameter")
        forbidden = {"api_key", "apikey", "credential", "secret"}
        if forbidden & {key.lower() for key, _ in normalized}:
            raise ValueError("credential material is forbidden in request plans")
        object.__setattr__(self, "parameters", normalized)
        if self.authorized_reference_bounds != SOURCE_RANGE_V2:
            raise ValueError("request plan exceeds approved observation bounds")
        parameters = dict(normalized)
        if parameters.get("series_id") != self.series_id:
            raise ValueError("request part series parameter mismatch")
        if self.endpoint == FRED_SERIES_OBSERVATIONS_ENDPOINT:
            if (
                parameters.get("observation_start"),
                parameters.get("observation_end"),
            ) != SOURCE_RANGE_V2:
                raise ValueError("observation request exceeds approved source range")
            if parameters["observation_end"] >= PROSPECTIVE_BLIND_OOS_RANGE_V2[0]:
                raise ValueError("observation request overlaps prospective blind OOS")
        if int(parameters.get("limit", "0")) != self.pagination.limit:
            raise ValueError("request limit does not match pagination contract")
        if int(parameters.get("offset", "-1")) != self.pagination.initial_offset:
            raise ValueError("request offset does not match pagination contract")

    @property
    def identity(self) -> str:
        return _digest(asdict(self))


@dataclass(frozen=True, slots=True)
class FullVintageObservationTemplateV2:
    series_id: str
    endpoint: str
    fixed_parameters: tuple[tuple[str, str], ...]
    pit_strategy: str
    vintage_batch_size: int
    max_parts: int

    def __post_init__(self) -> None:
        _validate_series(self.series_id)
        if self.series_id not in FULL_VINTAGE_SERIES_V2:
            raise ValueError("full-vintage template requires a full-vintage series")
        if self.endpoint != FRED_SERIES_OBSERVATIONS_ENDPOINT:
            raise ValueError("full-vintage template requires observations endpoint")
        if self.vintage_batch_size != FULL_VINTAGE_BATCH_SIZE:
            raise ValueError("unexpected full-vintage batch size")
        normalized = tuple(sorted(self.fixed_parameters))
        if {"api_key", "apikey", "credential", "secret"} & {key.lower() for key, _ in normalized}:
            raise ValueError("credential material is forbidden in request templates")
        parameters = dict(normalized)
        if (
            parameters.get("observation_start"),
            parameters.get("observation_end"),
        ) != SOURCE_RANGE_V2 or parameters.get("output_type") != "2":
            raise ValueError("full-vintage template semantics are not authorized")
        object.__setattr__(self, "fixed_parameters", normalized)


@dataclass(frozen=True, slots=True)
class RequestPlanV2:
    series_id: str
    pit_strategy: str
    initial_parts: tuple[RequestPartV2, ...]
    full_vintage_template: FullVintageObservationTemplateV2 | None
    semantic_bindings: tuple[tuple[str, str], ...]
    authority_identity: str = FRED_ALFRED_GLOBAL_RISK_AUTHORITY_V2_IDENTITY

    def __post_init__(self) -> None:
        _validate_series(self.series_id)
        expected_pit = dict(PIT_CLASSIFICATIONS_V2)[self.series_id]
        if self.pit_strategy != expected_pit:
            raise ValueError("request plan PIT strategy mismatch")
        if not self.initial_parts or any(
            part.series_id != self.series_id or part.pit_strategy != self.pit_strategy
            for part in self.initial_parts
        ):
            raise ValueError("request plan parts do not match plan")
        normalized_bindings = tuple(sorted(self.semantic_bindings))
        if {"api_key", "apikey", "credential", "secret"} & {
            key.lower() for key, _ in normalized_bindings
        }:
            raise ValueError("credential material is forbidden in semantic bindings")
        object.__setattr__(self, "semantic_bindings", normalized_bindings)

    @property
    def identity(self) -> str:
        return _digest(asdict(self))


@dataclass(frozen=True, slots=True)
class CredentialBoundaryV2:
    environment_variable: str = FRED_CREDENTIAL_ENVIRONMENT_VARIABLE_V2
    attach_at: str = "FUTURE_TRANSPORT_FINAL_REQUEST_BOUNDARY_ONLY"
    excluded_from: tuple[str, ...] = FRED_CREDENTIAL_EXCLUDED_FROM_V2
    request_plans_contain_actual_credential: bool = False


if RELEASE_VINTAGE_SERIES_V2 + FULL_VINTAGE_SERIES_V2 != (
    "DEXJPUS",
    "NASDAQCOM",
    "DGS2",
    "DGS10",
    "VIXCLS",
    "DTWEXBGS",
    "STLFSI4",
):
    raise RuntimeError("FRED V2 strategy groups changed unexpectedly")
