from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from app.research.capital_flow.fred_alfred_v2 import (
    CONSERVATIVE_RELEASE_USABLE_AT_POLICY,
    FRED_ALFRED_GLOBAL_RISK_AUTHORITY_V2_IDENTITY,
    FRED_SERIES_V2,
    NASDAQ_CALENDAR_IDENTITY_V2,
    NASDAQ_SESSION_SEMANTIC_V2,
    PIT_FULL_VINTAGE_REQUIRED,
    PIT_RELEASE_VINTAGE_REQUIRED,
    SOURCE_RANGE_V2,
    VIXCLS_USAGE_RESTRICTIONS_V2,
)
from app.research.capital_flow.fred_alfred_v2_acquisition.contracts import (
    FRED_ALLOWED_ENDPOINTS,
    FRED_OBSERVATIONS_LIMIT,
    FRED_OFFICIAL_API_HOST,
    FRED_SERIES_OBSERVATIONS_ENDPOINT,
    FRED_SERIES_VINTAGEDATES_ENDPOINT,
    FRED_VINTAGEDATES_LIMIT,
    FULL_VINTAGE_BATCH_SIZE,
    FULL_VINTAGE_SERIES_V2,
    MAX_FULL_VINTAGE_OBSERVATION_PARTS,
    MISSINGNESS_STATES_V2,
    PAGE_COUNT_VALIDATION_INVARIANTS_V2,
    PROVIDER_ERROR_CATEGORIES_V2,
    RELEASE_VINTAGE_SERIES_V2,
    CredentialBoundaryV2,
    RequestPartV2,
)
from app.research.capital_flow.fred_alfred_v2_acquisition.request_plan import (
    build_all_fred_alfred_v2_request_plans,
    build_fred_alfred_v2_request_plan,
    build_full_vintage_observation_parts,
)


def parameters(part: RequestPartV2) -> dict[str, str]:
    return dict(part.parameters)


def test_exact_series_scope_order_and_pit_groups_are_frozen() -> None:
    plans = build_all_fred_alfred_v2_request_plans()
    assert (
        tuple(plan.series_id for plan in plans)
        == FRED_SERIES_V2
        == (
            "DEXJPUS",
            "NASDAQCOM",
            "DTWEXBGS",
            "DGS2",
            "DGS10",
            "STLFSI4",
            "VIXCLS",
        )
    )
    assert RELEASE_VINTAGE_SERIES_V2 == ("DEXJPUS", "NASDAQCOM", "DGS2", "DGS10", "VIXCLS")
    assert FULL_VINTAGE_SERIES_V2 == ("DTWEXBGS", "STLFSI4")
    assert all(
        plan.authority_identity == FRED_ALFRED_GLOBAL_RISK_AUTHORITY_V2_IDENTITY for plan in plans
    )
    with pytest.raises(ValueError, match="outside"):
        build_fred_alfred_v2_request_plan("UNAPPROVED")


@pytest.mark.parametrize("series_id", RELEASE_VINTAGE_SERIES_V2)
def test_each_release_vintage_series_uses_explicit_initial_release_strategy(series_id: str) -> None:
    plan = build_fred_alfred_v2_request_plan(series_id)
    assert plan.pit_strategy == PIT_RELEASE_VINTAGE_REQUIRED
    assert plan.full_vintage_template is None
    assert len(plan.initial_parts) == 1
    part = plan.initial_parts[0]
    assert part.endpoint == FRED_SERIES_OBSERVATIONS_ENDPOINT
    assert part.part_kind == "INITIAL_RELEASE_OBSERVATIONS"
    assert parameters(part) == {
        "file_type": "json",
        "limit": "100000",
        "observation_end": "2026-05-31",
        "observation_start": "2023-09-01",
        "offset": "0",
        "output_type": "4",
        "realtime_end": "2026-05-31",
        "realtime_start": "2023-09-01",
        "series_id": series_id,
        "sort_order": "asc",
        "units": "lin",
    }


@pytest.mark.parametrize("series_id", FULL_VINTAGE_SERIES_V2)
def test_each_full_vintage_series_enumerates_then_batches_explicit_vintages(series_id: str) -> None:
    plan = build_fred_alfred_v2_request_plan(series_id)
    assert plan.pit_strategy == PIT_FULL_VINTAGE_REQUIRED
    enumeration = plan.initial_parts[0]
    assert enumeration.endpoint == FRED_SERIES_VINTAGEDATES_ENDPOINT
    assert parameters(enumeration) == {
        "file_type": "json",
        "limit": "10000",
        "offset": "0",
        "realtime_end": "2026-05-31",
        "realtime_start": "2023-09-01",
        "series_id": series_id,
        "sort_order": "asc",
    }
    template = plan.full_vintage_template
    assert template is not None
    assert dict(template.fixed_parameters)["output_type"] == "2"
    assert template.vintage_batch_size == FULL_VINTAGE_BATCH_SIZE == 50
    assert template.max_parts == MAX_FULL_VINTAGE_OBSERVATION_PARTS == 21


def test_official_endpoints_ranges_and_blind_protection_are_explicit() -> None:
    assert FRED_OFFICIAL_API_HOST == "https://api.stlouisfed.org"
    assert FRED_ALLOWED_ENDPOINTS == (
        "fred/series/observations",
        "fred/series/vintagedates",
    )
    assert (FRED_OBSERVATIONS_LIMIT, FRED_VINTAGEDATES_LIMIT) == (100_000, 10_000)
    part = build_fred_alfred_v2_request_plan("DGS2").initial_parts[0]
    assert part.authorized_reference_bounds == SOURCE_RANGE_V2
    blind_parameters = tuple(
        (key, "2026-09-01" if key == "observation_end" else value) for key, value in part.parameters
    )
    with pytest.raises(ValueError, match="source range|blind"):
        replace(part, parameters=blind_parameters)


def test_full_vintage_parts_are_order_invariant_deduplicated_and_bounded() -> None:
    vintages = tuple(f"2024-01-{day:02d}" for day in range(1, 32)) + tuple(
        f"2024-02-{day:02d}" for day in range(1, 21)
    )
    parts = build_full_vintage_observation_parts(
        "DTWEXBGS", tuple(reversed(vintages)) + vintages[:2]
    )
    assert len(parts) == 2
    assert tuple(part.part_index for part in parts) == (0, 1)
    assert all(part.endpoint == FRED_SERIES_OBSERVATIONS_ENDPOINT for part in parts)
    assert all(parameters(part)["output_type"] == "2" for part in parts)
    first_dates = parameters(parts[0])["vintage_dates"].split(",")
    second_dates = parameters(parts[1])["vintage_dates"].split(",")
    assert len(first_dates) == 50
    assert len(second_dates) == 1
    assert first_dates + second_dates == sorted(set(vintages))
    with pytest.raises(ValueError, match="outside"):
        build_full_vintage_observation_parts("STLFSI4", ("2026-09-01",))
    with pytest.raises(ValueError, match="full-vintage"):
        build_full_vintage_observation_parts("DGS2", ("2024-01-01",))


def test_request_parts_are_immutable_deterministic_and_parameter_order_invariant() -> None:
    part = build_fred_alfred_v2_request_plan("DEXJPUS").initial_parts[0]
    reordered = replace(part, parameters=tuple(reversed(part.parameters)))
    assert reordered.parameters == part.parameters
    assert reordered.identity == part.identity
    with pytest.raises(FrozenInstanceError):
        part.series_id = "DGS2"  # type: ignore[misc]
    with pytest.raises(ValueError, match="credential"):
        replace(part, parameters=part.parameters + (("api_key", "secret"),))


def test_plan_and_secret_identity_are_structurally_separate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FRED_API_KEY", "first-secret")
    first = build_fred_alfred_v2_request_plan("NASDAQCOM")
    monkeypatch.setenv("FRED_API_KEY", "second-secret")
    second = build_fred_alfred_v2_request_plan("NASDAQCOM")
    assert first == second
    assert first.identity == second.identity
    assert "first-secret" not in repr(first)
    assert "second-secret" not in repr(second)
    assert "FRED_API_KEY" not in repr(first)
    credential = CredentialBoundaryV2()
    assert credential.environment_variable == "FRED_API_KEY"
    assert not credential.request_plans_contain_actual_credential
    assert credential.attach_at == "FUTURE_TRANSPORT_FINAL_REQUEST_BOUNDARY_ONLY"


def test_pagination_and_future_page_count_validation_are_finite_and_fail_closed() -> None:
    plans = build_all_fred_alfred_v2_request_plans()
    paginations = tuple(part.pagination for plan in plans for part in plan.initial_parts)
    assert all(page.initial_offset == 0 and page.max_parts == 1 for page in paginations)
    assert all(
        page.offset_progression == "NEXT_OFFSET_EQUALS_PREVIOUS_OFFSET_PLUS_LIMIT"
        for page in paginations
    )
    assert "NO_PREMATURE_EMPTY_PAGE_WHEN_PROVIDER_COUNT_HAS_MORE" in (
        PAGE_COUNT_VALIDATION_INVARIANTS_V2
    )
    assert "NO_REPEATED_OFFSET" in PAGE_COUNT_VALIDATION_INVARIANTS_V2
    assert "NO_OFFSET_REGRESSION" in PAGE_COUNT_VALIDATION_INVARIANTS_V2
    assert "NO_DUPLICATE_PAGE_IDENTITY" in PAGE_COUNT_VALIDATION_INVARIANTS_V2


def test_nasdaq_and_vix_bindings_preserve_future_availability_and_restrictions() -> None:
    nasdaq = dict(build_fred_alfred_v2_request_plan("NASDAQCOM").semantic_bindings)
    assert nasdaq == {
        "calendar_identity": NASDAQ_CALENDAR_IDENTITY_V2,
        "future_usable_at_policy": CONSERVATIVE_RELEASE_USABLE_AT_POLICY,
        "session_semantic": NASDAQ_SESSION_SEMANTIC_V2,
    }
    vix_bindings = build_fred_alfred_v2_request_plan("VIXCLS").semantic_bindings
    assert ("provider", "FRED/ALFRED") in vix_bindings
    assert ("underlying_source", "Chicago Board Options Exchange") in vix_bindings
    assert ("release", "CBOE Market Statistics") in vix_bindings
    assert ("future_usable_at_policy", CONSERVATIVE_RELEASE_USABLE_AT_POLICY) in vix_bindings
    assert tuple(value for key, value in vix_bindings if key == "usage_restriction") == tuple(
        sorted(VIXCLS_USAGE_RESTRICTIONS_V2)
    )


def test_provider_errors_missingness_and_no_implementation_creep_are_contract_only() -> None:
    assert PROVIDER_ERROR_CATEGORIES_V2 == (
        "AUTHENTICATION_FAILURE",
        "AUTHORIZATION_FAILURE",
        "RATE_LIMITED",
        "TRANSIENT_PROVIDER_FAILURE",
        "PERMANENT_PROVIDER_FAILURE",
        "MALFORMED_PROVIDER_RESPONSE",
    )
    assert MISSINGNESS_STATES_V2 == (
        "VALID_SOURCE_ABSENCE",
        "ACQUISITION_GAP",
        "MALFORMED_SOURCE_RECORD",
    )
    package = Path("app/research/capital_flow/fred_alfred_v2_acquisition")
    assert {path.name for path in package.iterdir() if path.is_file()} == {
        "__init__.py",
        "contracts.py",
        "raw_evidence.py",
        "request_plan.py",
        "transport.py",
    }
    planning_source = "\n".join(
        (package / name).read_text(encoding="utf-8") for name in ("contracts.py", "request_plan.py")
    )
    forbidden = (
        "import httpx",
        "import requests",
        "urllib.request",
        "urlopen(",
        "fred_api_key(",
        "open(",
        "write_text(",
    )
    assert all(token not in planning_source for token in forbidden)
