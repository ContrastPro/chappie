"""Pure deterministic builders for sanitized FRED/ALFRED V2 request plans."""

from __future__ import annotations

from datetime import date

from ..fred_alfred_v2 import (
    CONSERVATIVE_RELEASE_USABLE_AT_POLICY,
    FRED_SERIES_V2,
    NASDAQ_CALENDAR_IDENTITY_V2,
    NASDAQ_SESSION_SEMANTIC_V2,
    PIT_CLASSIFICATIONS_V2,
    PIT_FULL_VINTAGE_REQUIRED,
    PIT_RELEASE_VINTAGE_REQUIRED,
    SOURCE_RANGE_V2,
    VIXCLS_AUTHORITY_STATUS_V2,
    VIXCLS_USAGE_RESTRICTIONS_V2,
)
from .contracts import (
    FRED_OBSERVATIONS_LIMIT,
    FRED_SERIES_OBSERVATIONS_ENDPOINT,
    FRED_SERIES_VINTAGEDATES_ENDPOINT,
    FRED_VINTAGEDATES_LIMIT,
    FULL_VINTAGE_BATCH_SIZE,
    FULL_VINTAGE_SERIES_V2,
    MAX_FULL_VINTAGE_OBSERVATION_PARTS,
    RELEASE_VINTAGE_SERIES_V2,
    FullVintageObservationTemplateV2,
    PaginationContractV2,
    RequestPartV2,
    RequestPlanV2,
)

_SINGLE_PAGE_TERMINATION = "RETURNED_RECORDS_EQUALS_PROVIDER_COUNT_WITHIN_SINGLE_BOUNDED_PAGE"


def _pagination(limit: int, *, max_parts: int = 1) -> PaginationContractV2:
    return PaginationContractV2(
        limit=limit,
        initial_offset=0,
        offset_progression="NEXT_OFFSET_EQUALS_PREVIOUS_OFFSET_PLUS_LIMIT",
        max_parts=max_parts,
        termination_expectation=_SINGLE_PAGE_TERMINATION,
    )


def _release_observation_part(series_id: str) -> RequestPartV2:
    return RequestPartV2(
        series_id=series_id,
        endpoint=FRED_SERIES_OBSERVATIONS_ENDPOINT,
        parameters=(
            ("file_type", "json"),
            ("limit", str(FRED_OBSERVATIONS_LIMIT)),
            ("observation_end", SOURCE_RANGE_V2[1]),
            ("observation_start", SOURCE_RANGE_V2[0]),
            ("offset", "0"),
            ("output_type", "4"),
            ("realtime_end", SOURCE_RANGE_V2[1]),
            ("realtime_start", SOURCE_RANGE_V2[0]),
            ("series_id", series_id),
            ("sort_order", "asc"),
            ("units", "lin"),
        ),
        part_kind="INITIAL_RELEASE_OBSERVATIONS",
        part_index=0,
        pit_strategy=PIT_RELEASE_VINTAGE_REQUIRED,
        authorized_reference_bounds=SOURCE_RANGE_V2,
        pagination=_pagination(FRED_OBSERVATIONS_LIMIT),
    )


def _vintage_enumeration_part(series_id: str) -> RequestPartV2:
    return RequestPartV2(
        series_id=series_id,
        endpoint=FRED_SERIES_VINTAGEDATES_ENDPOINT,
        parameters=(
            ("file_type", "json"),
            ("limit", str(FRED_VINTAGEDATES_LIMIT)),
            ("offset", "0"),
            ("realtime_end", SOURCE_RANGE_V2[1]),
            ("realtime_start", SOURCE_RANGE_V2[0]),
            ("series_id", series_id),
            ("sort_order", "asc"),
        ),
        part_kind="BOUNDED_VINTAGE_DATE_ENUMERATION",
        part_index=0,
        pit_strategy=PIT_FULL_VINTAGE_REQUIRED,
        authorized_reference_bounds=SOURCE_RANGE_V2,
        pagination=_pagination(FRED_VINTAGEDATES_LIMIT),
    )


def _full_vintage_template(series_id: str) -> FullVintageObservationTemplateV2:
    return FullVintageObservationTemplateV2(
        series_id=series_id,
        endpoint=FRED_SERIES_OBSERVATIONS_ENDPOINT,
        fixed_parameters=(
            ("file_type", "json"),
            ("limit", str(FRED_OBSERVATIONS_LIMIT)),
            ("observation_end", SOURCE_RANGE_V2[1]),
            ("observation_start", SOURCE_RANGE_V2[0]),
            ("offset", "0"),
            ("output_type", "2"),
            ("series_id", series_id),
            ("sort_order", "asc"),
            ("units", "lin"),
        ),
        pit_strategy=PIT_FULL_VINTAGE_REQUIRED,
        vintage_batch_size=FULL_VINTAGE_BATCH_SIZE,
        max_parts=MAX_FULL_VINTAGE_OBSERVATION_PARTS,
    )


def _semantic_bindings(series_id: str) -> tuple[tuple[str, str], ...]:
    if series_id == "NASDAQCOM":
        return (
            ("calendar_identity", NASDAQ_CALENDAR_IDENTITY_V2),
            ("session_semantic", NASDAQ_SESSION_SEMANTIC_V2),
            ("future_usable_at_policy", CONSERVATIVE_RELEASE_USABLE_AT_POLICY),
        )
    if series_id == "VIXCLS":
        return (
            ("authority_status", VIXCLS_AUTHORITY_STATUS_V2),
            ("provider", "FRED/ALFRED"),
            ("underlying_source", "Chicago Board Options Exchange"),
            ("release", "CBOE Market Statistics"),
            ("semantic", "DAILY_VIX_CLOSE_INDEX_POINTS"),
            ("future_usable_at_policy", CONSERVATIVE_RELEASE_USABLE_AT_POLICY),
            *tuple(("usage_restriction", value) for value in VIXCLS_USAGE_RESTRICTIONS_V2),
        )
    return ()


def build_fred_alfred_v2_request_plan(series_id: str) -> RequestPlanV2:
    """Build one immutable plan for a frozen V2 series without reading credentials."""
    if series_id not in FRED_SERIES_V2:
        raise ValueError("series is outside the frozen FRED/ALFRED V2 authority")
    pit_strategy = dict(PIT_CLASSIFICATIONS_V2)[series_id]
    if series_id in RELEASE_VINTAGE_SERIES_V2:
        return RequestPlanV2(
            series_id=series_id,
            pit_strategy=pit_strategy,
            initial_parts=(_release_observation_part(series_id),),
            full_vintage_template=None,
            semantic_bindings=_semantic_bindings(series_id),
        )
    return RequestPlanV2(
        series_id=series_id,
        pit_strategy=pit_strategy,
        initial_parts=(_vintage_enumeration_part(series_id),),
        full_vintage_template=_full_vintage_template(series_id),
        semantic_bindings=(),
    )


def build_all_fred_alfred_v2_request_plans() -> tuple[RequestPlanV2, ...]:
    """Preserve the released seven-series authority order."""
    return tuple(build_fred_alfred_v2_request_plan(series_id) for series_id in FRED_SERIES_V2)


def _validated_vintage_dates(vintage_dates: tuple[str, ...]) -> tuple[str, ...]:
    try:
        normalized = tuple(
            sorted({date.fromisoformat(value).isoformat() for value in vintage_dates})
        )
    except ValueError as error:
        raise ValueError("invalid vintage date") from error
    if any(not SOURCE_RANGE_V2[0] <= value <= SOURCE_RANGE_V2[1] for value in normalized):
        raise ValueError("vintage date outside bounded realtime window")
    return normalized


def build_full_vintage_observation_parts(
    series_id: str, vintage_dates: tuple[str, ...]
) -> tuple[RequestPartV2, ...]:
    """Turn validated provider vintage dates into deterministic bounded observation parts."""
    if series_id not in FULL_VINTAGE_SERIES_V2:
        raise ValueError("observation vintage parts require a full-vintage series")
    normalized = _validated_vintage_dates(vintage_dates)
    batches = tuple(
        normalized[index : index + FULL_VINTAGE_BATCH_SIZE]
        for index in range(0, len(normalized), FULL_VINTAGE_BATCH_SIZE)
    )
    if len(batches) > MAX_FULL_VINTAGE_OBSERVATION_PARTS:
        raise ValueError("vintage enumeration exceeds bounded request-part count")
    template = _full_vintage_template(series_id)
    return tuple(
        RequestPartV2(
            series_id=series_id,
            endpoint=template.endpoint,
            parameters=template.fixed_parameters + (("vintage_dates", ",".join(batch)),),
            part_kind="FULL_VINTAGE_OBSERVATIONS",
            part_index=index,
            pit_strategy=PIT_FULL_VINTAGE_REQUIRED,
            authorized_reference_bounds=SOURCE_RANGE_V2,
            pagination=_pagination(FRED_OBSERVATIONS_LIMIT),
        )
        for index, batch in enumerate(batches)
    )
