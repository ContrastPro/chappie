from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType

import pytest

from app.research.capital_flow.fred_alfred_v2 import (
    FRED_ALFRED_GLOBAL_RISK_AUTHORITY_V2_IDENTITY,
    FRED_SERIES_V2,
)
from app.research.capital_flow.fred_alfred_v2_acquisition.raw_evidence import build_raw_evidence
from app.research.capital_flow.fred_alfred_v2_acquisition.request_plan import (
    build_fred_alfred_v2_request_plan,
    build_full_vintage_observation_parts,
)
from app.research.capital_flow.fred_alfred_v2_acquisition.transport import ProviderPageV2
from app.research.capital_flow.fred_alfred_v2_availability import (
    CBOE_VIX_DAILY_CLOSE_SESSION_CALENDAR_IDENTITY,
    US_FEDERAL_BUSINESS_DAY_CALENDAR_IDENTITY,
)
from app.research.capital_flow.fred_alfred_v2_pit import (
    FRED_ALFRED_V2_PIT_MATERIALIZATION_IDENTITY,
)
from app.research.capital_flow.fred_alfred_v2_publication import (
    FRED_ALFRED_V2_AUTHORITY_VALIDATED,
    FRED_ALFRED_V2_MANIFEST_NAME,
    publish_fred_alfred_v2_authority,
    verify_fred_alfred_v2_authority,
)
from app.research.capital_flow.fred_alfred_v2_semantic import (
    VALID_SOURCE_ABSENCE,
    parse_semantic_evidence_set,
)
from app.research.capital_flow.nasdaq_calendar import NASDAQ_CALENDAR_AUTHORITY_IDENTITY


def _raw(part, records: tuple[object, ...]):
    return build_raw_evidence(
        part,
        ProviderPageV2(
            part.identity,
            part.series_id,
            part.endpoint,
            len(records),
            0,
            part.pagination.limit,
            len(records),
            records,
            (("content_type", "application/json"),),
        ),
    )


def _release(
    series: str,
    value: str = "12.50",
    *,
    reference: str = "2024-01-02",
    realtime_start: str = "2024-01-03",
):
    plan = build_fred_alfred_v2_request_plan(series)
    row = MappingProxyType(
        {
            "date": reference,
            "value": value,
            "realtime_start": realtime_start,
            "realtime_end": "9999-12-31",
        }
    )
    return parse_semantic_evidence_set(series, (_raw(plan.initial_parts[0], (row,)),))


def _full(series: str):
    plan = build_fred_alfred_v2_request_plan(series)
    reference = "2024-01-02"
    enumeration = _raw(plan.initial_parts[0], (reference,))
    part = build_full_vintage_observation_parts(series, (reference,))[0]
    row = MappingProxyType(
        {
            "date": reference,
            f"{series}_20240103": "1.25",
            f"{series}_20240105": "1.50",
        }
    )
    return parse_semantic_evidence_set(series, (enumeration, _raw(part, (row,))))


def _sets():
    return (
        _release("DEXJPUS"),
        _release("NASDAQCOM"),
        _full("DTWEXBGS"),
        _release("DGS2", "."),
        _release("DGS10"),
        _full("STLFSI4"),
        _release("VIXCLS"),
    )


def _manifest(root: Path) -> dict[str, object]:
    return json.loads((root / FRED_ALFRED_V2_MANIFEST_NAME).read_text(encoding="utf-8"))


def _canonical(root: Path, series: str) -> list[dict[str, object]]:
    path = root / "canonical" / f"canonical_{series.lower()}.ndjson"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_identity_exact_series_layout_and_reload(tmp_path: Path) -> None:
    result = publish_fred_alfred_v2_authority(_sets(), tmp_path)
    manifest = _manifest(tmp_path)
    assert result.status == FRED_ALFRED_V2_AUTHORITY_VALIDATED
    assert manifest["authority_identity"] == FRED_ALFRED_GLOBAL_RISK_AUTHORITY_V2_IDENTITY
    assert manifest["pit_identity"] == FRED_ALFRED_V2_PIT_MATERIALIZATION_IDENTITY
    assert tuple(manifest["series_order"]) == FRED_SERIES_V2
    assert tuple(sorted(path.name for path in (tmp_path / "canonical").iterdir())) == tuple(
        sorted(f"canonical_{series.lower()}.ndjson" for series in FRED_SERIES_V2)
    )
    assert verify_fred_alfred_v2_authority(tmp_path) == result


def test_create_only_rejects_existing_authority(tmp_path: Path) -> None:
    publish_fred_alfred_v2_authority(_sets(), tmp_path)
    with pytest.raises(FileExistsError):
        publish_fred_alfred_v2_authority(_sets(), tmp_path)


def test_decimal_utc_and_missingness_round_trip(tmp_path: Path) -> None:
    publish_fred_alfred_v2_authority(_sets(), tmp_path)
    dex = _canonical(tmp_path, "DEXJPUS")[0]
    dgs2 = _canonical(tmp_path, "DGS2")[0]
    assert dex["value"] == "12.50"
    assert datetime.fromisoformat(str(dex["usable_at_utc"])).tzinfo is UTC
    assert str(dex["usable_at_utc"]).endswith("Z")
    assert (dgs2["value"], dgs2["value_state"]) == (None, VALID_SOURCE_ABSENCE)


def test_release_and_full_vintage_history_preserved(tmp_path: Path) -> None:
    publish_fred_alfred_v2_authority(_sets(), tmp_path)
    assert len(_canonical(tmp_path, "DEXJPUS")) == 1
    dtw = _canonical(tmp_path, "DTWEXBGS")
    stl = _canonical(tmp_path, "STLFSI4")
    assert [item["value"] for item in dtw] == ["1.25", "1.50"]
    assert [item["value"] for item in stl] == ["1.25", "1.50"]
    assert dtw[0]["effective_until_utc"] == dtw[1]["usable_at_utc"]
    assert stl[0]["effective_until_utc"] == stl[1]["usable_at_utc"]
    assert dtw[0]["selected_vintage_identity"] != dtw[1]["selected_vintage_identity"]


def test_calendar_and_staleness_bindings(tmp_path: Path) -> None:
    publish_fred_alfred_v2_authority(_sets(), tmp_path)
    manifest = _manifest(tmp_path)
    series = {item["series_id"]: item for item in manifest["series"]}
    assert (
        series["NASDAQCOM"]["availability_calendar_identity"] == NASDAQ_CALENDAR_AUTHORITY_IDENTITY
    )
    assert (
        series["NASDAQCOM"]["source_session_semantic"]
        == "NASDAQCOM_PREVIOUS_COMPLETED_NASDAQ_REGULAR_SESSION_ONLY"
    )
    assert (
        series["VIXCLS"]["availability_calendar_identity"]
        == CBOE_VIX_DAILY_CLOSE_SESSION_CALENDAR_IDENTITY
    )
    assert (
        series["VIXCLS"]["source_session_identity"]
        == "CBOE_VIX_SPOT_INDEX_DAILY_CLOSE_SOURCE_SESSION"
    )
    assert series["VIXCLS"]["usage_restrictions"]
    for series_id in ("DEXJPUS", "NASDAQCOM", "DTWEXBGS", "DGS2", "DGS10", "VIXCLS"):
        assert (
            series[series_id]["staleness_calendar_identity"]
            == US_FEDERAL_BUSINESS_DAY_CALENDAR_IDENTITY
        )
        assert series[series_id]["staleness_policy"] == "<=3_US_FEDERAL_BUSINESS_DAYS"
    assert series["STLFSI4"]["staleness_policy"] == "<=9_CALENDAR_DAYS"


def test_invalid_market_source_sessions_are_accounted_but_not_canonicalized(
    tmp_path: Path,
) -> None:
    evidence = list(_sets())
    evidence[1] = _release("NASDAQCOM", ".", reference="2024-01-06", realtime_start="2024-01-08")
    publish_fred_alfred_v2_authority(tuple(evidence), tmp_path)
    quality = _manifest(tmp_path)["series"][1]["quality"]
    assert quality["semantic_record_count"] == 1
    assert quality["canonical_row_count"] == 0
    assert quality["invalid_source_session_excluded_count"] == 1
    assert _canonical(tmp_path, "NASDAQCOM") == []


def test_digests_and_cross_root_content_are_deterministic(tmp_path: Path) -> None:
    first_root, second_root = tmp_path / "one", tmp_path / "two"
    first = publish_fred_alfred_v2_authority(_sets(), first_root)
    second = publish_fred_alfred_v2_authority(_sets(), second_root)
    assert first.canonical_digests == second.canonical_digests
    assert first.semantic_authority_digest == second.semantic_authority_digest
    assert first.manifest_semantic_digest == second.manifest_semantic_digest
    assert (first_root / "manifest.json").read_bytes() == (
        second_root / "manifest.json"
    ).read_bytes()
    for series in FRED_SERIES_V2:
        assert (first_root / "canonical" / f"canonical_{series.lower()}.ndjson").read_bytes() == (
            second_root / "canonical" / f"canonical_{series.lower()}.ndjson"
        ).read_bytes()


def test_manifest_is_last_and_failure_before_it_is_non_authoritative(tmp_path: Path) -> None:
    phases: list[str] = []

    def fail_after_canonical(phase: str) -> None:
        phases.append(phase)
        if phase == "canonical_published":
            raise RuntimeError("injected failure")

    with pytest.raises(RuntimeError, match="injected failure"):
        publish_fred_alfred_v2_authority(_sets(), tmp_path, phase_hook=fail_after_canonical)
    assert phases == ["canonical_validated", "canonical_published"]
    assert not (tmp_path / "manifest.json").exists()
    assert not (tmp_path / "canonical").exists()


@pytest.mark.parametrize("target", ("canonical", "manifest", "binding"))
def test_reload_detects_canonical_manifest_and_binding_tampering(
    tmp_path: Path, target: str
) -> None:
    publish_fred_alfred_v2_authority(_sets(), tmp_path)
    if target == "canonical":
        path = tmp_path / "canonical" / "canonical_dexjpus.ndjson"
        path.write_bytes(path.read_bytes() + b"\n")
    else:
        path = tmp_path / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if target == "manifest":
            manifest["provider"] = "tampered"
        else:
            manifest["series"][0]["staleness_policy"] = "tampered"
        manifest.pop("manifest_semantic_digest")
        canonical = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
        manifest["manifest_semantic_digest"] = hashlib.sha256(canonical).hexdigest()
        path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError):
        verify_fred_alfred_v2_authority(tmp_path)


def test_manifest_quality_blind_safety_and_no_machine_identity(tmp_path: Path) -> None:
    publish_fred_alfred_v2_authority(_sets(), tmp_path)
    payload = (tmp_path / "manifest.json").read_text(encoding="utf-8")
    manifest = json.loads(payload)
    assert manifest["ranges"]["source"] == ["2023-09-01", "2026-05-31"]
    assert manifest["blind_exclusion"] == {
        "declaration": "BLIND_OOS_MARKET_OBSERVATIONS_EXCLUDED",
        "range": ["2026-09-01", "2027-02-28"],
        "rows_published": 0,
    }
    assert all(item["quality"]["malformed_count"] == 0 for item in manifest["series"])
    assert all(
        item["quality"]["pit_usable_at_invariant_violations"] == 0 for item in manifest["series"]
    )
    assert all(
        token not in payload for token in ("FRED_API_KEY", str(tmp_path), "created_at", "hostname")
    )


def test_module_has_no_http_acquisition_features_or_research() -> None:
    source = Path("app/research/capital_flow/fred_alfred_v2_publication.py").read_text(
        encoding="utf-8"
    )
    assert all(
        token not in source
        for token in (
            "httpx",
            "requests.",
            "acquire_",
            "FRED_API_KEY",
            "feature_",
            "BTC",
            "correlation",
        )
    )
