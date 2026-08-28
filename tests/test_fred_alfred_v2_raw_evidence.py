from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import MappingProxyType

import pytest

from app.research.capital_flow.fred_alfred_v2_acquisition.contracts import (
    FRED_SERIES_OBSERVATIONS_ENDPOINT,
)
from app.research.capital_flow.fred_alfred_v2_acquisition.raw_evidence import (
    CACHE_CREATED,
    CACHE_REUSED,
    EVIDENCE_COMPLETE,
    EVIDENCE_CORRUPT,
    EVIDENCE_INCOMPLETE,
    FRED_ALFRED_V2_RAW_EVIDENCE_IDENTITY,
    FRED_ALFRED_V2_RAW_EVIDENCE_ROOT,
    FULL_RAW_EVIDENCE_COMPLETE,
    FULL_VINTAGE_DYNAMIC_REQUEST_DERIVATION,
    OBSERVATION_PARTS_COMPLETE,
    RAW_EVIDENCE_CACHE_CONFLICT,
    RAW_EVIDENCE_CACHE_CORRUPT,
    RELEASE_RAW_EVIDENCE_COMPLETE,
    VINTAGE_ENUMERATION_COMPLETE,
    RawEvidenceCacheError,
    build_raw_evidence,
    inspect_raw_evidence_set,
    load_raw_evidence,
    raw_evidence_relative_path,
    serialize_raw_evidence,
    verify_raw_evidence,
    write_raw_evidence,
)
from app.research.capital_flow.fred_alfred_v2_acquisition.request_plan import (
    build_fred_alfred_v2_request_plan,
    build_full_vintage_observation_parts,
)
from app.research.capital_flow.fred_alfred_v2_acquisition.transport import ProviderPageV2

OBVIOUS_SECRET = "SECRET-FRED-KEY-DO-NOT-SERIALIZE"


def _part(series_id: str = "NASDAQCOM", limit: int = 2):
    part = build_fred_alfred_v2_request_plan(series_id).initial_parts[0]
    parameters = tuple(
        (key, str(limit) if key == "limit" else value) for key, value in part.parameters
    )
    return replace(part, parameters=parameters, pagination=replace(part.pagination, limit=limit))


def _page(part=None, records=None, *, count: int | None = None):
    active = part or _part()
    if records is None:
        records = (
            (MappingProxyType({"date": "2024-01-02", "value": "."}),)
            if active.endpoint == FRED_SERIES_OBSERVATIONS_ENDPOINT
            else ("2024-01-02",)
        )
    provider_count = len(records) if count is None else count
    return ProviderPageV2(
        active.identity,
        active.series_id,
        active.endpoint,
        provider_count,
        int(dict(active.parameters)["offset"]),
        active.pagination.limit,
        len(records),
        tuple(records),
        (("content_type", "application/json"),),
    )


def _evidence(part=None, records=None):
    active = part or _part()
    return build_raw_evidence(active, _page(active, records))


def _mutate_cache(path: Path, key: str, value: object) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload[key] = value
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_raw_evidence_identity_and_storage_contract() -> None:
    assert FRED_ALFRED_V2_RAW_EVIDENCE_IDENTITY == "chappie_fred_alfred_v2_raw_evidence/1.0.0"
    assert FRED_ALFRED_V2_RAW_EVIDENCE_ROOT.as_posix() == (
        "data/research/source/capital_flow/fred_alfred_v2/raw"
    )


def test_raw_evidence_is_deeply_immutable() -> None:
    evidence = _evidence()
    with pytest.raises(FrozenInstanceError):
        evidence.series_id = "DGS2"
    with pytest.raises(TypeError):
        evidence.provider_records[0]["value"] = "1"


@pytest.mark.parametrize(
    "field",
    ["request_part_identity", "series_id", "endpoint", "provider_offset", "provider_limit"],
)
def test_request_page_cross_checks_reject_mismatch(field: str) -> None:
    part = _part()
    page = _page(part)
    replacement: object = {
        "request_part_identity": "0" * 64,
        "series_id": "DGS2",
        "endpoint": "fred/series/vintagedates",
        "provider_offset": 1,
        "provider_limit": 3,
    }[field]
    with pytest.raises(ValueError, match="binding mismatch"):
        build_raw_evidence(part, replace(page, **{field: replacement}))


def test_observation_records_order_and_dot_are_preserved() -> None:
    records = (
        MappingProxyType({"date": "2024-01-03", "value": "."}),
        MappingProxyType({"date": "2024-01-02", "value": "1.25"}),
    )
    evidence = _evidence(_part(), records)
    assert [row["date"] for row in evidence.provider_records] == ["2024-01-03", "2024-01-02"]
    assert evidence.provider_records[0]["value"] == "."


def test_vintage_date_strings_and_order_are_preserved() -> None:
    part = _part("DTWEXBGS", limit=3)
    values = ("2024-01-03", "2024-01-01", "2024-01-03")
    assert _evidence(part, values).provider_records == values


def test_serialization_and_digest_are_deterministic() -> None:
    first = _evidence()
    second = _evidence()
    assert serialize_raw_evidence(first) == serialize_raw_evidence(second)
    assert first.raw_semantic_digest == second.raw_semantic_digest
    assert serialize_raw_evidence(first).endswith(b"\n")


def test_cache_identity_is_root_independent_and_sanitized(tmp_path: Path) -> None:
    evidence = _evidence()
    relative = raw_evidence_relative_path(evidence.series_id, evidence.request_part_identity)
    assert relative == Path("NASDAQCOM") / f"{evidence.request_part_identity}.json"
    assert not relative.is_absolute()
    assert str(tmp_path) not in str(relative)
    assert OBVIOUS_SECRET not in str(relative) + evidence.raw_semantic_digest


@pytest.mark.parametrize(
    ("series", "identity"),
    [("../NASDAQCOM", "0" * 64), ("NASDAQCOM", "../bad"), ("NASDAQCOM", "g" * 64)],
)
def test_unsafe_cache_identity_rejected(series: str, identity: str) -> None:
    with pytest.raises(ValueError, match="unsafe"):
        raw_evidence_relative_path(series, identity)


def test_secret_is_absent_from_serialization_digest_and_path() -> None:
    evidence = _evidence()
    combined = serialize_raw_evidence(evidence) + evidence.raw_semantic_digest.encode()
    combined += str(
        raw_evidence_relative_path(evidence.series_id, evidence.request_part_identity)
    ).encode()
    assert OBVIOUS_SECRET.encode() not in combined


def test_atomic_write_reload_and_closed_handle_hook(tmp_path: Path) -> None:
    phases: list[str] = []

    def hook(phase: str, path: Path) -> None:
        phases.append(phase)
        if phase == "temporary_closed":
            with path.open("ab"):
                pass

    part = _part()
    result = write_raw_evidence(part, _page(part), tmp_path, phase_hook=hook)
    assert result.status == CACHE_CREATED
    assert phases == ["temporary_closed", "temporary_validated", "cache_created"]
    assert load_raw_evidence(tmp_path / result.relative_path, part) == result.evidence
    assert not tuple((tmp_path / part.series_id).glob("*.tmp"))


def test_failed_new_publication_cleans_owned_final_and_temporary(tmp_path: Path) -> None:
    part = _part()

    def hook(phase: str, _path: Path) -> None:
        if phase == "cache_created":
            raise RuntimeError("synthetic publication failure")

    with pytest.raises(RuntimeError, match="synthetic"):
        write_raw_evidence(part, _page(part), tmp_path, phase_hook=hook)
    assert not tuple(tmp_path.rglob("*.json"))
    assert not tuple(tmp_path.rglob("*.tmp"))


def test_valid_existing_cache_is_reused_without_overwrite(tmp_path: Path) -> None:
    part = _part()
    first = write_raw_evidence(part, _page(part), tmp_path)
    path = tmp_path / first.relative_path
    before = path.read_bytes()
    second = write_raw_evidence(part, _page(part), tmp_path)
    assert second.status == CACHE_REUSED
    assert path.read_bytes() == before


def test_conflicting_existing_cache_fails_closed(tmp_path: Path) -> None:
    part = _part()
    first = write_raw_evidence(part, _page(part), tmp_path)
    conflicting = _page(part, (MappingProxyType({"date": "2024-01-02", "value": "changed"}),))
    with pytest.raises(RawEvidenceCacheError) as caught:
        write_raw_evidence(part, conflicting, tmp_path)
    assert caught.value.category == RAW_EVIDENCE_CACHE_CONFLICT
    assert load_raw_evidence(tmp_path / first.relative_path, part) == first.evidence


@pytest.mark.parametrize(
    ("mutation", "value"),
    [
        ("schema_identity", "wrong"),
        ("authority_identity", "wrong"),
        ("series_id", "DGS2"),
        ("request_part_identity", "0" * 64),
        ("provider_page_identity", "0" * 64),
        ("raw_semantic_digest", "0" * 64),
    ],
)
def test_corrupt_cache_identity_or_digest_rejected(
    tmp_path: Path, mutation: str, value: object
) -> None:
    part = _part()
    result = write_raw_evidence(part, _page(part), tmp_path)
    path = tmp_path / result.relative_path
    _mutate_cache(path, mutation, value)
    with pytest.raises(RawEvidenceCacheError) as caught:
        load_raw_evidence(path, part)
    assert caught.value.category == RAW_EVIDENCE_CACHE_CORRUPT


@pytest.mark.parametrize("payload", [b"not-json", b'{"schema_identity":'])
def test_invalid_or_truncated_json_is_corrupt(tmp_path: Path, payload: bytes) -> None:
    part = _part()
    path = tmp_path / "bad.json"
    path.write_bytes(payload)
    with pytest.raises(RawEvidenceCacheError) as caught:
        load_raw_evidence(path, part)
    assert caught.value.category == RAW_EVIDENCE_CACHE_CORRUPT


@pytest.mark.parametrize("suffix", [".partial", ".tmp"])
def test_partial_and_temporary_files_are_non_authoritative(tmp_path: Path, suffix: str) -> None:
    part = _part()
    path = tmp_path / f"evidence{suffix}"
    path.write_bytes(serialize_raw_evidence(_evidence(part)))
    with pytest.raises(RawEvidenceCacheError):
        load_raw_evidence(path, part)


def test_verify_requires_matching_provider_page_identity() -> None:
    part = _part()
    with pytest.raises(RawEvidenceCacheError) as caught:
        verify_raw_evidence(_evidence(part), part, "0" * 64)
    assert caught.value.category == RAW_EVIDENCE_CACHE_CONFLICT


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider_records", "not-an-array"),
        ("provider_response_metadata", "not-an-array"),
        ("request_index", "zero"),
    ],
)
def test_invalid_structural_cache_types_are_corrupt(
    tmp_path: Path, field: str, value: object
) -> None:
    part = _part()
    result = write_raw_evidence(part, _page(part), tmp_path)
    path = tmp_path / result.relative_path
    _mutate_cache(path, field, value)
    with pytest.raises(RawEvidenceCacheError) as caught:
        load_raw_evidence(path, part)
    assert caught.value.category == RAW_EVIDENCE_CACHE_CORRUPT


def test_corrupt_existing_cache_is_not_overwritten(tmp_path: Path) -> None:
    part = _part()
    relative = raw_evidence_relative_path(part.series_id, part.identity)
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(b"truncated")
    with pytest.raises(RawEvidenceCacheError) as caught:
        write_raw_evidence(part, _page(part), tmp_path)
    assert caught.value.category == RAW_EVIDENCE_CACHE_CORRUPT
    assert path.read_bytes() == b"truncated"


def test_cache_error_is_sanitized() -> None:
    part = _part()
    error = RawEvidenceCacheError(RAW_EVIDENCE_CACHE_CORRUPT, part.series_id, part.identity)
    assert OBVIOUS_SECRET not in str(error) + repr(error)


def test_release_vintage_complete_and_missing_semantics() -> None:
    plan = build_fred_alfred_v2_request_plan("NASDAQCOM")
    incomplete = inspect_raw_evidence_set(plan, ())
    complete = inspect_raw_evidence_set(plan, (_evidence(plan.initial_parts[0]),))
    assert incomplete.status == EVIDENCE_INCOMPLETE
    assert incomplete.missing_request_part_identities == (plan.initial_parts[0].identity,)
    assert complete.status == EVIDENCE_COMPLETE
    assert complete.achieved_stages == (RELEASE_RAW_EVIDENCE_COMPLETE,)


def test_full_vintage_enumeration_only_is_not_complete() -> None:
    plan = build_fred_alfred_v2_request_plan("DTWEXBGS")
    enumeration = _evidence(plan.initial_parts[0], ("2024-01-01", "2024-01-02"))
    result = inspect_raw_evidence_set(plan, (enumeration,))
    assert result.status == EVIDENCE_INCOMPLETE
    assert result.achieved_stages == (VINTAGE_ENUMERATION_COMPLETE,)
    assert result.dynamic_request_derivation == FULL_VINTAGE_DYNAMIC_REQUEST_DERIVATION
    assert result.missing_request_part_identities


def test_full_vintage_complete_requires_all_derived_observation_parts() -> None:
    plan = build_fred_alfred_v2_request_plan("DTWEXBGS")
    dates = ("2024-01-01", "2024-01-02")
    enumeration = _evidence(plan.initial_parts[0], dates)
    observation_parts = build_full_vintage_observation_parts(plan.series_id, dates)
    observations = tuple(_evidence(part) for part in observation_parts)
    result = inspect_raw_evidence_set(plan, (enumeration, *observations))
    assert result.status == EVIDENCE_COMPLETE
    assert result.achieved_stages == (
        VINTAGE_ENUMERATION_COMPLETE,
        OBSERVATION_PARTS_COMPLETE,
        FULL_RAW_EVIDENCE_COMPLETE,
    )


def test_empty_vintage_enumeration_can_be_structurally_complete() -> None:
    plan = build_fred_alfred_v2_request_plan("STLFSI4")
    enumeration = build_raw_evidence(
        plan.initial_parts[0], _page(plan.initial_parts[0], (), count=0)
    )
    result = inspect_raw_evidence_set(plan, (enumeration,))
    assert result.status == EVIDENCE_COMPLETE
    assert result.achieved_stages[-1] == FULL_RAW_EVIDENCE_COMPLETE


def test_invalid_vintage_enumeration_marks_set_corrupt() -> None:
    plan = build_fred_alfred_v2_request_plan("STLFSI4")
    enumeration = _evidence(plan.initial_parts[0], ("not-a-date",))
    assert inspect_raw_evidence_set(plan, (enumeration,)).status == EVIDENCE_CORRUPT


def test_duplicate_evidence_part_marks_set_corrupt() -> None:
    plan = build_fred_alfred_v2_request_plan("NASDAQCOM")
    evidence = _evidence(plan.initial_parts[0])
    assert inspect_raw_evidence_set(plan, (evidence, evidence)).status == EVIDENCE_CORRUPT


def test_corrupt_evidence_set_is_distinct() -> None:
    plan = build_fred_alfred_v2_request_plan("NASDAQCOM")
    result = inspect_raw_evidence_set(plan, (), corrupt_request_part_identities=("0" * 64,))
    assert result.status == EVIDENCE_CORRUPT


def test_evidence_set_digest_is_deterministic() -> None:
    plan = build_fred_alfred_v2_request_plan("NASDAQCOM")
    evidence = _evidence(plan.initial_parts[0])
    assert (
        inspect_raw_evidence_set(plan, (evidence,)).semantic_digest
        == inspect_raw_evidence_set(plan, (evidence,)).semantic_digest
    )


def test_raw_evidence_module_has_no_http_or_domain_materialization() -> None:
    source = Path("app/research/capital_flow/fred_alfred_v2_acquisition/raw_evidence.py").read_text(
        encoding="utf-8"
    )
    forbidden = (
        "import httpx",
        "Decimal",
        "usable_at",
        "missingness",
        "manifest.json",
        "feature_",
        "research_split",
        "execute_request_part",
    )
    assert all(token not in source for token in forbidden)
