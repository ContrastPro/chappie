from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date, timedelta
from pathlib import Path
from types import MappingProxyType

import httpx
import pytest

from app.research.capital_flow.fred_alfred_v2 import FRED_SERIES_V2
from app.research.capital_flow.fred_alfred_v2_acquisition import acquire as acquire_module
from app.research.capital_flow.fred_alfred_v2_acquisition.acquire import (
    ACQUISITION_COMPLETE,
    ACQUISITION_FAILED,
    FRED_ALFRED_V2_RAW_EVIDENCE_COMPLETE,
    AllAcquisitionFailedV2,
    acquire_all_fred_alfred_v2_raw_evidence,
    acquire_series_raw_evidence,
)
from app.research.capital_flow.fred_alfred_v2_acquisition.raw_evidence import (
    RAW_EVIDENCE_CACHE_CONFLICT,
    RAW_EVIDENCE_CACHE_CORRUPT,
    RawEvidenceCacheError,
    load_raw_evidence,
    raw_evidence_relative_path,
    write_raw_evidence,
)
from app.research.capital_flow.fred_alfred_v2_acquisition.request_plan import (
    build_fred_alfred_v2_request_plan,
    build_full_vintage_observation_parts,
)
from app.research.capital_flow.fred_alfred_v2_acquisition.transport import (
    FredAlfredTransportError,
    ProviderPageV2,
)

SECRET = "OBVIOUS-ORCHESTRATOR-SECRET"


@pytest.fixture(autouse=True)
def _credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRED_API_KEY", SECRET)


class FredProvider:
    def __init__(
        self,
        *,
        vintage_dates: tuple[str, ...] = ("2024-01-01", "2024-01-02"),
        fail_series: str | None = None,
        fail_status: int = 401,
        fail_observation_number: int | None = None,
        malformed: bool = False,
    ) -> None:
        self.vintage_dates = vintage_dates
        self.fail_series = fail_series
        self.fail_status = fail_status
        self.fail_observation_number = fail_observation_number
        self.malformed = malformed
        self.calls: list[tuple[str, str]] = []
        self.observation_calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        series = request.url.params["series_id"]
        endpoint = request.url.path.removeprefix("/")
        self.calls.append((series, endpoint))
        if series == self.fail_series:
            return httpx.Response(self.fail_status)
        if endpoint.endswith("observations"):
            self.observation_calls += 1
            if self.observation_calls == self.fail_observation_number:
                return httpx.Response(self.fail_status)
        if self.malformed:
            return httpx.Response(200, json={"unexpected": True})
        records: list[object]
        key: str
        if endpoint.endswith("vintagedates"):
            key = "vintage_dates"
            records = list(self.vintage_dates)
        else:
            key = "observations"
            records = [{"date": "2024-01-02", "value": "."}]
        return httpx.Response(
            200,
            json={
                "count": len(records),
                "offset": request.url.params["offset"],
                "limit": request.url.params["limit"],
                key: records,
            },
        )


def _client(provider: FredProvider) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(provider))


def _page(part, records: tuple[object, ...]) -> ProviderPageV2:
    return ProviderPageV2(
        part.identity,
        part.series_id,
        part.endpoint,
        len(records),
        int(dict(part.parameters)["offset"]),
        part.pagination.limit,
        len(records),
        records,
        (("content_type", "application/json"),),
    )


def _seed(root: Path, part, records: tuple[object, ...]) -> None:
    write_raw_evidence(part, _page(part, records), root)


def _dates(count: int) -> tuple[str, ...]:
    start = date(2024, 1, 1)
    return tuple((start + timedelta(days=index)).isoformat() for index in range(count))


def test_exact_series_scope_and_order() -> None:
    assert FRED_SERIES_V2 == (
        "DEXJPUS",
        "NASDAQCOM",
        "DTWEXBGS",
        "DGS2",
        "DGS10",
        "STLFSI4",
        "VIXCLS",
    )


def test_unknown_series_fails_planner_gate_without_http(tmp_path: Path) -> None:
    provider = FredProvider()
    with _client(provider) as client, pytest.raises(ValueError, match="outside"):
        acquire_series_raw_evidence(tmp_path, "../UNKNOWN", client, lambda _: None)
    assert provider.calls == []


def test_result_dto_is_immutable_and_secret_free(tmp_path: Path) -> None:
    provider = FredProvider()
    with _client(provider) as client:
        result = acquire_series_raw_evidence(tmp_path, "NASDAQCOM", client, lambda _: None)
    with pytest.raises(FrozenInstanceError):
        result.status = "FAILED"
    assert SECRET not in repr(result) + result.semantic_digest


def test_equivalent_cold_runs_are_deterministic_across_roots(tmp_path: Path) -> None:
    results = []
    for name in ("first", "second"):
        provider = FredProvider()
        with _client(provider) as client:
            results.append(
                acquire_series_raw_evidence(tmp_path / name, "NASDAQCOM", client, lambda _: None)
            )
    assert results[0] == results[1]


@pytest.mark.parametrize("series_id", ["DEXJPUS", "NASDAQCOM", "DGS2", "DGS10", "VIXCLS"])
def test_cold_release_vintage_acquisition(series_id: str, tmp_path: Path) -> None:
    provider = FredProvider()
    with _client(provider) as client:
        result = acquire_series_raw_evidence(tmp_path, series_id, client, lambda _: None)
    assert result.status == FRED_ALFRED_V2_RAW_EVIDENCE_COMPLETE
    assert (result.required_part_count, result.downloaded_count, result.cache_reused_count) == (
        1,
        1,
        0,
    )
    assert len(provider.calls) == 1


def test_warm_release_vintage_cache_reuse_has_zero_http(tmp_path: Path) -> None:
    first_provider = FredProvider()
    with _client(first_provider) as client:
        acquire_series_raw_evidence(tmp_path, "NASDAQCOM", client, lambda _: None)
    warm = FredProvider(fail_series="NASDAQCOM")
    with _client(warm) as client:
        result = acquire_series_raw_evidence(tmp_path, "NASDAQCOM", client, lambda _: None)
    assert (result.cache_reused_count, result.downloaded_count) == (1, 0)
    assert warm.calls == []


def test_empty_release_cache_resumes_only_missing_part(tmp_path: Path) -> None:
    (tmp_path / "NASDAQCOM").mkdir()
    provider = FredProvider()
    with _client(provider) as client:
        result = acquire_series_raw_evidence(tmp_path, "NASDAQCOM", client, lambda _: None)
    assert result.downloaded_count == 1
    assert len(provider.calls) == 1


def test_full_vintage_cold_staged_flow_and_dynamic_derivation(tmp_path: Path) -> None:
    dates = _dates(51)
    provider = FredProvider(vintage_dates=dates)
    with _client(provider) as client:
        result = acquire_series_raw_evidence(tmp_path, "DTWEXBGS", client, lambda _: None)
    assert result.status == FRED_ALFRED_V2_RAW_EVIDENCE_COMPLETE
    assert result.required_part_count == 3
    assert result.downloaded_count == 3
    assert [endpoint.rsplit("/", 1)[-1] for _, endpoint in provider.calls] == [
        "vintagedates",
        "observations",
        "observations",
    ]


def test_full_vintage_single_batch_has_two_required_parts(tmp_path: Path) -> None:
    provider = FredProvider(vintage_dates=_dates(2))
    with _client(provider) as client:
        result = acquire_series_raw_evidence(tmp_path, "STLFSI4", client, lambda _: None)
    assert result.required_part_count == 2
    assert len(provider.calls) == 2


def test_exact_enumeration_strings_are_persisted_before_derivation(tmp_path: Path) -> None:
    dates = ("2024-01-02", "2024-01-01", "2024-01-02")
    provider = FredProvider(vintage_dates=dates)
    with _client(provider) as client:
        acquire_series_raw_evidence(tmp_path, "DTWEXBGS", client, lambda _: None)
    plan = build_fred_alfred_v2_request_plan("DTWEXBGS")
    path = tmp_path / raw_evidence_relative_path(plan.series_id, plan.initial_parts[0].identity)
    assert load_raw_evidence(path, plan.initial_parts[0]).provider_records == dates


def test_enumeration_only_is_not_reported_complete_on_interruption(tmp_path: Path) -> None:
    provider = FredProvider(fail_observation_number=1, fail_status=401)
    with _client(provider) as client, pytest.raises(FredAlfredTransportError):
        acquire_series_raw_evidence(tmp_path, "DTWEXBGS", client, lambda _: None)
    plan = build_fred_alfred_v2_request_plan("DTWEXBGS")
    enumeration_path = tmp_path / raw_evidence_relative_path(
        plan.series_id, plan.initial_parts[0].identity
    )
    assert enumeration_path.exists()
    assert len(tuple((tmp_path / plan.series_id).glob("*.json"))) == 1


def test_partial_full_vintage_interruption_and_resume_missing_only(tmp_path: Path) -> None:
    dates = _dates(51)
    interrupted = FredProvider(vintage_dates=dates, fail_observation_number=2, fail_status=401)
    with _client(interrupted) as client, pytest.raises(FredAlfredTransportError):
        acquire_series_raw_evidence(tmp_path, "DTWEXBGS", client, lambda _: None)
    assert len(tuple((tmp_path / "DTWEXBGS").glob("*.json"))) == 2
    resumed = FredProvider(vintage_dates=dates)
    with _client(resumed) as client:
        result = acquire_series_raw_evidence(tmp_path, "DTWEXBGS", client, lambda _: None)
    assert (result.cache_reused_count, result.downloaded_count) == (2, 1)
    assert len(resumed.calls) == 1
    assert resumed.calls[0][1].endswith("observations")


def test_warm_full_vintage_reuses_every_part_without_http(tmp_path: Path) -> None:
    dates = _dates(51)
    provider = FredProvider(vintage_dates=dates)
    with _client(provider) as client:
        acquire_series_raw_evidence(tmp_path, "STLFSI4", client, lambda _: None)
    warm = FredProvider(fail_series="STLFSI4")
    with _client(warm) as client:
        result = acquire_series_raw_evidence(tmp_path, "STLFSI4", client, lambda _: None)
    assert (result.required_part_count, result.cache_reused_count, result.downloaded_count) == (
        3,
        3,
        0,
    )
    assert warm.calls == []


def test_seeded_enumeration_and_some_observations_acquire_only_missing(tmp_path: Path) -> None:
    dates = _dates(51)
    plan = build_fred_alfred_v2_request_plan("DTWEXBGS")
    _seed(tmp_path, plan.initial_parts[0], dates)
    observations = build_full_vintage_observation_parts(plan.series_id, dates)
    _seed(
        tmp_path,
        observations[0],
        (MappingProxyType({"date": "2024-01-02", "value": "."}),),
    )
    provider = FredProvider(vintage_dates=dates)
    with _client(provider) as client:
        result = acquire_series_raw_evidence(tmp_path, plan.series_id, client, lambda _: None)
    assert (result.cache_reused_count, result.downloaded_count) == (2, 1)
    assert len(provider.calls) == 1


def test_corrupt_cache_fails_without_redownload_or_delete(tmp_path: Path) -> None:
    plan = build_fred_alfred_v2_request_plan("NASDAQCOM")
    path = tmp_path / raw_evidence_relative_path(plan.series_id, plan.initial_parts[0].identity)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"truncated")
    provider = FredProvider()
    with _client(provider) as client, pytest.raises(RawEvidenceCacheError) as caught:
        acquire_series_raw_evidence(tmp_path, plan.series_id, client, lambda _: None)
    assert caught.value.category == RAW_EVIDENCE_CACHE_CORRUPT
    assert provider.calls == []
    assert path.read_bytes() == b"truncated"


def test_cache_conflict_is_preserved(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def conflict(*args, **kwargs):
        part = args[0]
        raise RawEvidenceCacheError(RAW_EVIDENCE_CACHE_CONFLICT, part.series_id, part.identity)

    monkeypatch.setattr(acquire_module, "write_raw_evidence", conflict)
    provider = FredProvider()
    with _client(provider) as client, pytest.raises(RawEvidenceCacheError) as caught:
        acquire_series_raw_evidence(tmp_path, "NASDAQCOM", client, lambda _: None)
    assert caught.value.category == RAW_EVIDENCE_CACHE_CONFLICT
    assert len(provider.calls) == 1


@pytest.mark.parametrize(
    ("status", "category", "attempts"),
    [
        (401, "AUTHENTICATION_FAILURE", 1),
        (403, "AUTHORIZATION_FAILURE", 1),
        (429, "RATE_LIMITED", 3),
        (503, "TRANSIENT_PROVIDER_FAILURE", 3),
    ],
)
def test_transport_errors_preserved_without_outer_retry(
    status: int, category: str, attempts: int, tmp_path: Path
) -> None:
    provider = FredProvider(fail_series="NASDAQCOM", fail_status=status)
    with _client(provider) as client, pytest.raises(FredAlfredTransportError) as caught:
        acquire_series_raw_evidence(tmp_path, "NASDAQCOM", client, lambda _: None)
    assert caught.value.category == category
    assert len(provider.calls) == attempts


def test_sleeper_is_forwarded_only_to_transport_backoff(tmp_path: Path) -> None:
    provider = FredProvider(fail_series="NASDAQCOM", fail_status=429)
    sleeps: list[float] = []
    with _client(provider) as client, pytest.raises(FredAlfredTransportError):
        acquire_series_raw_evidence(tmp_path, "NASDAQCOM", client, sleeps.append)
    assert sleeps == [0.25, 0.5]


def test_malformed_provider_page_failure_preserved(tmp_path: Path) -> None:
    provider = FredProvider(malformed=True)
    with _client(provider) as client, pytest.raises(FredAlfredTransportError) as caught:
        acquire_series_raw_evidence(tmp_path, "NASDAQCOM", client, lambda _: None)
    assert caught.value.category == "MALFORMED_PROVIDER_RESPONSE"


def test_derived_vintage_validation_failure_preserved_and_enumeration_retained(
    tmp_path: Path,
) -> None:
    provider = FredProvider(vintage_dates=("not-a-date",))
    with _client(provider) as client, pytest.raises(ValueError, match="invalid vintage date"):
        acquire_series_raw_evidence(tmp_path, "DTWEXBGS", client, lambda _: None)
    assert len(provider.calls) == 1
    assert len(tuple((tmp_path / "DTWEXBGS").glob("*.json"))) == 1


def test_all_series_complete_and_frozen_order(tmp_path: Path) -> None:
    provider = FredProvider()
    with _client(provider) as client:
        result = acquire_all_fred_alfred_v2_raw_evidence(tmp_path, client, lambda _: None)
    assert result.status == ACQUISITION_COMPLETE
    assert tuple(item.series_id for item in result.series_results) == FRED_SERIES_V2
    first_call_by_series = tuple(dict.fromkeys(series for series, _ in provider.calls))
    assert first_call_by_series == FRED_SERIES_V2


def test_warm_all_series_reuses_cache_with_zero_http(tmp_path: Path) -> None:
    provider = FredProvider()
    with _client(provider) as client:
        acquire_all_fred_alfred_v2_raw_evidence(tmp_path, client, lambda _: None)
    warm = FredProvider(fail_series="DEXJPUS")
    with _client(warm) as client:
        result = acquire_all_fred_alfred_v2_raw_evidence(tmp_path, client, lambda _: None)
    assert result.status == ACQUISITION_COMPLETE
    assert warm.calls == []
    assert all(item.downloaded_count == 0 for item in result.series_results)


def test_all_result_is_immutable(tmp_path: Path) -> None:
    provider = FredProvider()
    with _client(provider) as client:
        result = acquire_all_fred_alfred_v2_raw_evidence(tmp_path, client, lambda _: None)
    with pytest.raises(FrozenInstanceError):
        result.status = ACQUISITION_FAILED


def test_one_series_failure_prevents_all_series_complete_and_preserves_cause(
    tmp_path: Path,
) -> None:
    provider = FredProvider(fail_series="DGS2", fail_status=403)
    with _client(provider) as client, pytest.raises(AllAcquisitionFailedV2) as caught:
        acquire_all_fred_alfred_v2_raw_evidence(tmp_path, client, lambda _: None)
    assert caught.value.result.status == ACQUISITION_FAILED
    assert caught.value.result.failed_series_id == "DGS2"
    assert caught.value.result.failure_category == "AUTHORIZATION_FAILURE"
    assert isinstance(caught.value.__cause__, FredAlfredTransportError)
    assert SECRET not in str(caught.value) + repr(caught.value.result)


def test_mid_all_series_failure_retains_prior_validated_caches(tmp_path: Path) -> None:
    provider = FredProvider(fail_series="NASDAQCOM", fail_status=401)
    with _client(provider) as client, pytest.raises(AllAcquisitionFailedV2):
        acquire_all_fred_alfred_v2_raw_evidence(tmp_path, client, lambda _: None)
    dex_plan = build_fred_alfred_v2_request_plan("DEXJPUS")
    dex_path = tmp_path / raw_evidence_relative_path("DEXJPUS", dex_plan.initial_parts[0].identity)
    assert dex_path.exists()


def test_request_plan_is_not_mutated(tmp_path: Path) -> None:
    before = build_fred_alfred_v2_request_plan("DTWEXBGS")
    provider = FredProvider()
    with _client(provider) as client:
        acquire_series_raw_evidence(tmp_path, "DTWEXBGS", client, lambda _: None)
    after = build_fred_alfred_v2_request_plan("DTWEXBGS")
    assert before == after


def test_dot_value_remains_raw_after_orchestration(tmp_path: Path) -> None:
    provider = FredProvider()
    with _client(provider) as client:
        acquire_series_raw_evidence(tmp_path, "NASDAQCOM", client, lambda _: None)
    plan = build_fred_alfred_v2_request_plan("NASDAQCOM")
    path = tmp_path / raw_evidence_relative_path(plan.series_id, plan.initial_parts[0].identity)
    evidence = load_raw_evidence(path, plan.initial_parts[0])
    assert evidence.provider_records[0]["value"] == "."


def test_orchestrator_source_has_no_secret_lookup_or_domain_materialization() -> None:
    source = Path("app/research/capital_flow/fred_alfred_v2_acquisition/acquire.py").read_text(
        encoding="utf-8"
    )
    forbidden = (
        "os.environ",
        "getenv",
        "Decimal",
        "missingness",
        "usable_at",
        "manifest.json",
        "feature_",
        "research_split",
        "httpx.get",
    )
    assert all(token not in source for token in forbidden)
