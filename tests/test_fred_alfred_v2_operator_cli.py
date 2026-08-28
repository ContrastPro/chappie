from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from types import MappingProxyType

import httpx
import pytest

from app.research.capital_flow.fred_alfred_v2 import FRED_SERIES_V2
from app.research.capital_flow.fred_alfred_v2_acquisition.acquire import (
    ACQUISITION_COMPLETE,
    ACQUISITION_FAILED,
    FRED_ALFRED_V2_RAW_EVIDENCE_COMPLETE,
    AcquisitionResultV2,
    AllAcquisitionFailedV2,
    AllAcquisitionResultV2,
)
from app.research.capital_flow.fred_alfred_v2_acquisition.raw_evidence import (
    RAW_EVIDENCE_CACHE_CONFLICT,
    RAW_EVIDENCE_CACHE_CORRUPT,
    RawEvidenceCacheError,
    raw_evidence_relative_path,
    write_raw_evidence,
)
from app.research.capital_flow.fred_alfred_v2_acquisition.request_plan import (
    build_fred_alfred_v2_request_plan,
)
from app.research.capital_flow.fred_alfred_v2_acquisition.transport import (
    FredAlfredTransportError,
    ProviderPageV2,
)
from app.tools.fred_alfred_v2_acquire import main

SECRET = "CLI-SECRET-MUST-NEVER-APPEAR"


def _digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _series_result(
    series_id: str,
    *,
    reused: int = 0,
    downloaded: int = 1,
    required: int = 1,
) -> AcquisitionResultV2:
    part_digests = tuple(str(index).zfill(64) for index in range(reused + downloaded))
    payload = {
        "series_id": series_id,
        "status": FRED_ALFRED_V2_RAW_EVIDENCE_COMPLETE,
        "required_part_count": required,
        "cache_reused_count": reused,
        "downloaded_count": downloaded,
        "raw_evidence_set_status": "COMPLETE",
        "raw_evidence_set_digest": "e" * 64,
        "raw_evidence_part_digests": part_digests,
    }
    return AcquisitionResultV2(**payload, semantic_digest=_digest(payload))


def _all_result() -> AllAcquisitionResultV2:
    results = tuple(_series_result(series_id) for series_id in FRED_SERIES_V2)
    payload = {
        "status": ACQUISITION_COMPLETE,
        "series_result_digests": tuple(result.semantic_digest for result in results),
        "failed_series_id": None,
        "failure_category": None,
    }
    return AllAcquisitionResultV2(ACQUISITION_COMPLETE, results, None, None, _digest(payload))


def _client_factory() -> httpx.Client:
    def no_network(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("unexpected HTTP")

    return httpx.Client(transport=httpx.MockTransport(no_network))


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


def _seed(root: Path, series_id: str, records: tuple[object, ...]) -> None:
    part = build_fred_alfred_v2_request_plan(series_id).initial_parts[0]
    write_raw_evidence(part, _page(part, records), root)


def _run(argv, tmp_path: Path, **kwargs):
    output, error = io.StringIO(), io.StringIO()
    code = main(
        argv,
        root=tmp_path,
        environment=kwargs.pop("environment", {}),
        output=output,
        error_output=error,
        client_factory=kwargs.pop("client_factory", _client_factory),
        **kwargs,
    )
    return code, output.getvalue(), error.getvalue()


def test_status_is_local_only_and_makes_no_http(tmp_path: Path) -> None:
    calls = 0

    def forbidden_factory():
        nonlocal calls
        calls += 1
        raise AssertionError("status constructed an HTTP client")

    code, output, error = _run(["--status"], tmp_path, client_factory=forbidden_factory)
    assert code == 0
    assert calls == 0
    assert error == ""
    assert "overall_raw_evidence=ABSENT" in output


def test_cold_status_reports_all_exact_series_in_frozen_order(tmp_path: Path) -> None:
    code, output, _ = _run(["--status"], tmp_path)
    assert code == 0
    assert f"series={','.join(FRED_SERIES_V2)}" in output
    positions = [output.index(f"series_status={series_id}") for series_id in FRED_SERIES_V2]
    assert positions == sorted(positions)


def test_warm_complete_per_series_status_without_http(tmp_path: Path) -> None:
    _seed(
        tmp_path,
        "NASDAQCOM",
        (MappingProxyType({"date": "2024-01-02", "value": "."}),),
    )
    code, output, _ = _run(["--status", "--series", "NASDAQCOM"], tmp_path)
    assert code == 0
    assert "series_status=NASDAQCOM status=COMPLETE verified_parts=1/1" in output
    assert "overall_raw_evidence=COMPLETE" in output


def test_incomplete_full_vintage_status(tmp_path: Path) -> None:
    _seed(tmp_path, "DTWEXBGS", ("2024-01-01", "2024-01-02"))
    code, output, _ = _run(["--status", "--series", "DTWEXBGS"], tmp_path)
    assert code == 0
    assert "status=INCOMPLETE" in output
    assert "overall_raw_evidence=INCOMPLETE" in output


def test_corrupt_status_does_not_delete_or_repair(tmp_path: Path) -> None:
    plan = build_fred_alfred_v2_request_plan("NASDAQCOM")
    path = tmp_path / raw_evidence_relative_path("NASDAQCOM", plan.initial_parts[0].identity)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"truncated")
    code, output, _ = _run(["--status", "--series", "NASDAQCOM"], tmp_path)
    assert code == 0
    assert "status=CORRUPT" in output
    assert path.read_bytes() == b"truncated"


@pytest.mark.parametrize(
    ("environment", "label"), [({}, "ABSENT"), ({"FRED_API_KEY": SECRET}, "PRESENT")]
)
def test_credential_status_is_presence_only(
    environment: dict[str, str], label: str, tmp_path: Path
) -> None:
    _, output, error = _run(["--status"], tmp_path, environment=environment)
    assert f"FRED_API_KEY={label}" in output
    assert SECRET not in output + error


def test_missing_key_acquisition_fails_before_client_or_network(tmp_path: Path) -> None:
    constructed = 0

    def forbidden_factory():
        nonlocal constructed
        constructed += 1
        raise AssertionError

    code, output, error = _run([], tmp_path, client_factory=forbidden_factory)
    assert (code, constructed, output) == (2, 0, "")
    assert error == "FRED_API_KEY_REQUIRED\n"


def test_default_invokes_all_series_orchestrator_and_prints_frozen_order(
    tmp_path: Path,
) -> None:
    calls: list[Path] = []

    def acquire_all(root, _client):
        calls.append(root)
        return _all_result()

    code, output, error = _run(
        [], tmp_path, environment={"FRED_API_KEY": SECRET}, acquire_all=acquire_all
    )
    assert code == 0
    assert calls == [tmp_path]
    assert error == ""
    positions = [
        output.index(f"series {index}/7 {series}") for index, series in enumerate(FRED_SERIES_V2, 1)
    ]
    assert positions == sorted(positions)


@pytest.mark.parametrize("series_id", FRED_SERIES_V2)
def test_allowed_per_series_mode_invokes_released_orchestrator(
    series_id: str, tmp_path: Path
) -> None:
    calls: list[str] = []

    def acquire_series(_root, selected, _client):
        calls.append(selected)
        return _series_result(selected)

    code, output, _ = _run(
        ["--series", series_id],
        tmp_path,
        environment={"FRED_API_KEY": SECRET},
        acquire_series=acquire_series,
    )
    assert code == 0
    assert calls == [series_id]
    assert FRED_ALFRED_V2_RAW_EVIDENCE_COMPLETE in output


def test_unknown_series_rejected_before_client(tmp_path: Path) -> None:
    code, _, error = _run(
        ["--series", "GOLDAMGBD228NLBM"],
        tmp_path,
        environment={"FRED_API_KEY": SECRET},
    )
    assert code == 2
    assert "invalid choice" in error


@pytest.mark.parametrize(
    "arguments",
    [
        ["--start", "2020-01-01"],
        ["--end", "2027-02-28"],
        ["--force"],
        ["--overwrite"],
        ["--redownload-all"],
        ["--ignore-corruption"],
        ["--delete-cache"],
        ["--skip-validation"],
        ["--allow-partial"],
        ["--ignore-cache"],
        ["--repair"],
        ["--range", "2023-09-01..2027-02-28"],
        ["--blind-oos"],
        ["--print-records"],
        ["--show-key"],
        ["--max-attempts", "4"],
        ["--allow-series", "DEXJPUS"],
    ],
)
def test_unsafe_or_range_flags_are_unavailable(arguments: list[str], tmp_path: Path) -> None:
    code, _, error = _run(arguments, tmp_path, environment={"FRED_API_KEY": SECRET})
    assert code == 2
    assert error.startswith("USAGE_ERROR:")


def test_cache_reuse_and_partial_resume_counts_are_printed(tmp_path: Path) -> None:
    def acquire_series(_root, selected, _client):
        return _series_result(selected, reused=2, downloaded=1, required=3)

    code, output, _ = _run(
        ["--series", "DTWEXBGS"],
        tmp_path,
        environment={"FRED_API_KEY": SECRET},
        acquire_series=acquire_series,
    )
    assert code == 0
    assert "cache_reused=2 acquired=1 verified=3/3" in output


def test_release_and_full_vintage_progress_are_structural(tmp_path: Path) -> None:
    results = {
        "NASDAQCOM": _series_result("NASDAQCOM"),
        "DTWEXBGS": _series_result("DTWEXBGS", downloaded=2, required=2),
    }

    def acquire_series(_root, selected, _client):
        return results[selected]

    _, release, _ = _run(
        ["--series", "NASDAQCOM"],
        tmp_path,
        environment={"FRED_API_KEY": SECRET},
        acquire_series=acquire_series,
    )
    _, full, _ = _run(
        ["--series", "DTWEXBGS"],
        tmp_path,
        environment={"FRED_API_KEY": SECRET},
        acquire_series=acquire_series,
    )
    assert "kind=release_vintage" in release
    assert "kind=full_vintage" in full
    assert "stages=vintage_enumeration,derived_observation_parts,verification" in full


def test_success_reports_raw_evidence_only(tmp_path: Path) -> None:
    code, output, error = _run(
        [],
        tmp_path,
        environment={"FRED_API_KEY": SECRET},
        acquire_all=lambda _root, _client: _all_result(),
    )
    assert code == 0 and error == ""
    assert FRED_ALFRED_V2_RAW_EVIDENCE_COMPLETE in output
    assert "FRED_ALFRED_V2_AUTHORITY_VALIDATED" not in output
    assert "value" not in output.lower()


@pytest.mark.parametrize(
    "error",
    [
        FredAlfredTransportError("AUTHENTICATION_FAILURE", 1, 401, "http"),
        FredAlfredTransportError("AUTHORIZATION_FAILURE", 1, 403, "http"),
        FredAlfredTransportError("RATE_LIMITED", 3, 429, "http"),
        FredAlfredTransportError("MALFORMED_PROVIDER_RESPONSE", 1, 200, "json"),
        RawEvidenceCacheError(RAW_EVIDENCE_CACHE_CORRUPT, "NASDAQCOM", "0" * 64),
        RawEvidenceCacheError(RAW_EVIDENCE_CACHE_CONFLICT, "NASDAQCOM", "0" * 64),
    ],
)
def test_failure_categories_are_preserved_and_sanitized(error: Exception, tmp_path: Path) -> None:
    def acquire_series(*_args):
        raise error

    code, output, stderr = _run(
        ["--series", "NASDAQCOM"],
        tmp_path,
        environment={"FRED_API_KEY": SECRET},
        acquire_series=acquire_series,
    )
    expected = error.category
    assert code == 1
    assert stderr == f"{expected}\n"
    assert SECRET not in output + stderr
    assert "api.stlouisfed.org" not in output + stderr


def test_all_series_failure_category_is_preserved(tmp_path: Path) -> None:
    partial = _all_result()
    payload = {
        "status": ACQUISITION_FAILED,
        "series_result_digests": (),
        "failed_series_id": "DEXJPUS",
        "failure_category": "TRANSIENT_PROVIDER_FAILURE",
    }
    failed = AllAcquisitionResultV2(
        ACQUISITION_FAILED,
        (),
        "DEXJPUS",
        "TRANSIENT_PROVIDER_FAILURE",
        _digest(payload),
    )

    def acquire_all(*_args):
        raise AllAcquisitionFailedV2(failed)

    code, _, error = _run(
        [],
        tmp_path,
        environment={"FRED_API_KEY": SECRET},
        acquire_all=acquire_all,
    )
    assert partial.status == ACQUISITION_COMPLETE
    assert code == 1
    assert error == "TRANSIENT_PROVIDER_FAILURE\n"


def test_incomplete_result_has_deterministic_failure_exit(tmp_path: Path) -> None:
    complete = _series_result("NASDAQCOM")
    payload = {
        "series_id": complete.series_id,
        "status": "INCOMPLETE",
        "required_part_count": 1,
        "cache_reused_count": 0,
        "downloaded_count": 0,
        "raw_evidence_set_status": "INCOMPLETE",
        "raw_evidence_set_digest": "e" * 64,
        "raw_evidence_part_digests": (),
    }
    incomplete = AcquisitionResultV2(**payload, semantic_digest=_digest(payload))
    code, _, error = _run(
        ["--series", "NASDAQCOM"],
        tmp_path,
        environment={"FRED_API_KEY": SECRET},
        acquire_series=lambda *_args: incomplete,
    )
    assert code == 1
    assert error == "INCOMPLETE\n"


def test_cli_source_is_thin_and_has_no_domain_or_real_network_logic() -> None:
    source = Path("app/tools/fred_alfred_v2_acquire.py").read_text(encoding="utf-8")
    forbidden = (
        "Decimal",
        "missingness",
        "usable_at",
        "manifest.json",
        "feature_",
        "research_split",
        "api.stlouisfed.org",
        "httpx.get",
    )
    assert all(token not in source for token in forbidden)
