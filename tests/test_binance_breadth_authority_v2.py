from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from app.research.capital_flow.binance_breadth_authority import (
    BINANCE_BREADTH_AUTHORITY_IDENTITY,
)
from app.research.capital_flow.binance_breadth_authority_v2 import (
    BINANCE_BREADTH_AUTHORITY_V2_IDENTITY,
    BINANCE_BREADTH_V1_SCHEMA,
    BINANCE_BREADTH_V2_SCHEMA,
    EXPECTED_ARCHIVE_COUNT,
    EXPECTED_ROWS_PER_SYMBOL,
    PRODUCTION_SCOPE,
    QUOTE_VOLUME_SEMANTIC_IDENTITY,
    QUOTE_VOLUME_SOURCE_FIELD,
    BinanceBreadthV2Scope,
    publish_binance_breadth_v2,
    verify_binance_breadth_v2,
)


def _bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _scope() -> BinanceBreadthV2Scope:
    return BinanceBreadthV2Scope(
        ("BTCUSDT", "ETHUSDT"),
        ("2024-01", "2024-02"),
        1_704_067_200_000,
        1_704_067_320_000,
        2,
    )


def _provider_row(timestamp: int, *, quote: str = "100.2500") -> str:
    return (
        ",".join(
            (
                str(timestamp),
                "10.00",
                "12.00",
                "9.00",
                "11.00",
                "2.500",
                str(timestamp + 59_999),
                quote,
                "7",
                "1.250",
                "50.125",
                "0",
            )
        )
        + "\n"
    )


def _fixture(tmp_path: Path) -> tuple[Path, Path, BinanceBreadthV2Scope]:
    scope = _scope()
    v1 = tmp_path / "v1"
    cache = v1 / "download-cache"
    canonical = v1 / "canonical"
    canonical.mkdir(parents=True)
    details = []
    for symbol in scope.symbols:
        archives = []
        for index, month in enumerate(scope.months):
            path = cache / symbol / f"{month}.zip"
            path.parent.mkdir(parents=True, exist_ok=True)
            timestamp = scope.start_ms + index * 60_000
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
                archive.writestr(f"{symbol}-1m-{month}.csv", _provider_row(timestamp))
            archives.append(
                {
                    "month": month,
                    "archive_sha256": _sha(path.read_bytes()),
                    "url": f"https://data.binance.vision/{symbol}/{month}.zip",
                }
            )
        v1_bytes = b"open_time_ms,open,high,low,close,volume\nV1\n"
        (canonical / f"{symbol}.csv").write_bytes(v1_bytes)
        details.append(
            {
                "symbol": symbol,
                "candle_count": scope.expected_rows_per_symbol,
                "first_timestamp_ms": scope.start_ms,
                "last_timestamp_ms": scope.end_ms_exclusive - 60_000,
                "canonical_sha256": _sha(v1_bytes),
                "gaps": 0,
                "duplicates": 0,
                "malformed": 0,
                "native_units": ["milliseconds"] * len(scope.months),
                "archives": archives,
            }
        )
    manifest = {
        "identity": BINANCE_BREADTH_AUTHORITY_IDENTITY,
        "provider": "BINANCE_PUBLIC_ARCHIVES",
        "requested_start_ms": scope.start_ms,
        "requested_end_ms_exclusive": scope.end_ms_exclusive,
        "interval": "1m",
        "symbols": list(scope.symbols),
        "canonical_timestamp_unit": "UTC_milliseconds",
        "quality_status": "VERIFIED_EXACT_COMMON_GRID",
        "common_timestamp_count": scope.expected_rows_per_symbol,
        "symbols_detail": details,
    }
    manifest["manifest_semantic_digest"] = _sha(_bytes(manifest))
    (v1 / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    return v1, tmp_path / "v2", scope


def _resign_manifest(path: Path) -> None:
    value = json.loads(path.read_text())
    value.pop("manifest_semantic_digest", None)
    value.pop("semantic_authority_digest", None)
    value.pop("status", None)
    authority = _sha(_bytes(value))
    value["semantic_authority_digest"] = authority
    value["status"] = "VALIDATED"
    value["manifest_semantic_digest"] = _sha(_bytes(value))
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def _rebind_v1_archive(v1: Path, archive: Path) -> None:
    manifest_path = v1 / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["symbols_detail"][0]["archives"][0]["archive_sha256"] = _sha(archive.read_bytes())
    manifest.pop("manifest_semantic_digest")
    manifest["manifest_semantic_digest"] = _sha(_bytes(manifest))
    manifest_path.write_text(json.dumps(manifest))


def test_frozen_v1_v2_contracts_and_production_scope() -> None:
    assert BINANCE_BREADTH_AUTHORITY_IDENTITY.endswith("/1.0.0")
    assert (
        BINANCE_BREADTH_AUTHORITY_V2_IDENTITY
        == "chappie_binance_breadth_historical_authority/2.0.0"
    )
    assert BINANCE_BREADTH_V1_SCHEMA == ("open_time_ms", "open", "high", "low", "close", "volume")
    assert BINANCE_BREADTH_V2_SCHEMA == (*BINANCE_BREADTH_V1_SCHEMA, "quote_volume")
    assert len(PRODUCTION_SCOPE.symbols) == 19
    assert len(PRODUCTION_SCOPE.months) == 29
    assert EXPECTED_ARCHIVE_COUNT == 551
    assert EXPECTED_ROWS_PER_SYMBOL == 1_270_080


def test_offline_publication_maps_direct_quote_volume_and_preserves_v1(tmp_path) -> None:
    v1, v2, scope = _fixture(tmp_path)
    before = {path: path.read_bytes() for path in v1.rglob("*") if path.is_file()}
    phases = []
    result = publish_binance_breadth_v2(v1, v2, scope=scope, phase_hook=phases.append)
    assert phases == [
        "source_archives_verified",
        "canonical_staged",
        "canonical_reloaded",
        "manifest_published",
    ]
    assert result == verify_binance_breadth_v2(v2, v1_root=v1, scope=scope)
    rows = (v2 / "canonical/BTCUSDT.csv").read_text().splitlines()
    assert rows[0].split(",") == list(BINANCE_BREADTH_V2_SCHEMA)
    assert rows[1].split(",")[5:] == ["2.500", "100.2500"]
    assert "50.125" not in rows[1]
    assert {path: path.read_bytes() for path in v1.rglob("*") if path.is_file()} == before
    manifest = json.loads((v2 / "manifest.json").read_text())
    assert manifest["quote_volume_semantic_identity"] == QUOTE_VOLUME_SEMANTIC_IDENTITY
    assert manifest["quote_volume_source"] == QUOTE_VOLUME_SOURCE_FIELD


@pytest.mark.parametrize("quote", ["-1", "NaN", "Infinity", "bad"])
def test_invalid_quote_volume_fails_closed_and_cleans_output(tmp_path, quote) -> None:
    v1, v2, scope = _fixture(tmp_path)
    path = v1 / "download-cache/BTCUSDT/2024-01.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("BTCUSDT-1m-2024-01.csv", _provider_row(scope.start_ms, quote=quote))
    _rebind_v1_archive(v1, path)
    with pytest.raises(ValueError, match="quote_volume"):
        publish_binance_breadth_v2(v1, v2, scope=scope)
    assert not v2.exists()


@pytest.mark.parametrize("failure", ["missing", "checksum", "corrupt", "member"])
def test_source_archive_failures_are_rejected_before_publication(tmp_path, failure) -> None:
    v1, v2, scope = _fixture(tmp_path)
    path = v1 / "download-cache/BTCUSDT/2024-01.zip"
    if failure == "missing":
        path.unlink()
    elif failure == "checksum":
        path.write_bytes(path.read_bytes() + b"tamper")
    elif failure == "corrupt":
        path.write_bytes(b"not-a-zip")
        _rebind_v1_archive(v1, path)
    else:
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("WRONG.csv", _provider_row(scope.start_ms))
        _rebind_v1_archive(v1, path)
    with pytest.raises(ValueError):
        publish_binance_breadth_v2(v1, v2, scope=scope)
    assert not v2.exists()


def test_create_only_and_deterministic_publication(tmp_path) -> None:
    v1, v2, scope = _fixture(tmp_path)
    first = publish_binance_breadth_v2(v1, v2, scope=scope)
    with pytest.raises(FileExistsError):
        publish_binance_breadth_v2(v1, v2, scope=scope)
    second_root = tmp_path / "v2-copy"
    second = publish_binance_breadth_v2(v1, second_root, scope=scope)
    assert first.canonical_digests == second.canonical_digests
    assert first.semantic_authority_digest == second.semantic_authority_digest
    assert first.manifest_semantic_digest == second.manifest_semantic_digest


@pytest.mark.parametrize("tamper", ["quote", "swap", "schema", "row", "canonical"])
def test_canonical_tampering_is_detected(tmp_path, tamper) -> None:
    v1, v2, scope = _fixture(tmp_path)
    publish_binance_breadth_v2(v1, v2, scope=scope)
    path = v2 / "canonical/BTCUSDT.csv"
    lines = path.read_text().splitlines()
    if tamper == "quote":
        lines[1] = lines[1].replace("100.2500", "100.2501")
    elif tamper == "swap":
        fields = lines[1].split(",")
        fields[5], fields[6] = fields[6], fields[5]
        lines[1] = ",".join(fields)
    elif tamper == "schema":
        lines[0] = lines[0].replace(",quote_volume", "")
    elif tamper == "row":
        lines.pop()
    else:
        manifest = json.loads((v2 / "manifest.json").read_text())
        manifest["symbols_detail"][0]["canonical_sha256"] = "0" * 64
        (v2 / "manifest.json").write_text(json.dumps(manifest))
        with pytest.raises(ValueError):
            verify_binance_breadth_v2(v2, v1_root=v1, scope=scope)
        return
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(ValueError):
        verify_binance_breadth_v2(v2, v1_root=v1, scope=scope)


@pytest.mark.parametrize("tamper", ["manifest", "symbols", "source"])
def test_manifest_and_source_binding_tampering_is_detected(tmp_path, tamper) -> None:
    v1, v2, scope = _fixture(tmp_path)
    publish_binance_breadth_v2(v1, v2, scope=scope)
    path = v2 / "manifest.json"
    manifest = json.loads(path.read_text())
    if tamper == "manifest":
        manifest["provider"] = "tampered"
    elif tamper == "symbols":
        manifest["symbols"].reverse()
    else:
        manifest["symbols_detail"][0]["source_archives"][0]["archive_sha256"] = "0" * 64
    path.write_text(json.dumps(manifest))
    if tamper != "manifest":
        _resign_manifest(path)
    with pytest.raises(ValueError):
        verify_binance_breadth_v2(v2, v1_root=v1, scope=scope)
