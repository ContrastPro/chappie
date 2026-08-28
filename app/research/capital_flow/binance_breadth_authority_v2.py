"""Offline, create-only Binance breadth V2 publication with canonical quote volume."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .binance_breadth_authority import (
    BINANCE_BREADTH_AUTHORITY_IDENTITY,
    BINANCE_BREADTH_SYMBOLS_V1,
    END_MS,
    INTERVAL_MS,
    START_MS,
    months,
    normalize_native_timestamp,
)

BINANCE_BREADTH_AUTHORITY_V2_IDENTITY = "chappie_binance_breadth_historical_authority/2.0.0"
BINANCE_BREADTH_V1_STATUS = "VALIDATED_HISTORICAL_REPRODUCIBLE"
BINANCE_BREADTH_V2_STATUS = "PUBLICATION_ENGINE_IMPLEMENTED_REAL_AUTHORITY_NOT_YET_PUBLISHED"
BINANCE_BREADTH_V1_SCHEMA = ("open_time_ms", "open", "high", "low", "close", "volume")
BINANCE_BREADTH_V2_SCHEMA = (*BINANCE_BREADTH_V1_SCHEMA, "quote_volume")
QUOTE_VOLUME_SEMANTIC_IDENTITY = "BINANCE_KLINE_QUOTE_ASSET_VOLUME_DIRECT/1.0.0"
QUOTE_VOLUME_SOURCE_FIELD = "OFFICIAL_BINANCE_KLINE_FIELD_8_QUOTE_ASSET_VOLUME"
EXPECTED_ARCHIVE_COUNT = 551
EXPECTED_ROWS_PER_SYMBOL = 1_270_080
BINANCE_BREADTH_V1_ROOT = Path("data/research/source/capital_flow/binance_breadth_free_v1")
BINANCE_BREADTH_V2_ROOT = Path("data/research/source/capital_flow/binance_breadth_free_v2")


@dataclass(frozen=True, slots=True)
class BinanceBreadthV2Scope:
    symbols: tuple[str, ...]
    months: tuple[str, ...]
    start_ms: int
    end_ms_exclusive: int
    expected_rows_per_symbol: int


PRODUCTION_SCOPE = BinanceBreadthV2Scope(
    BINANCE_BREADTH_SYMBOLS_V1,
    months(),
    START_MS,
    END_MS,
    EXPECTED_ROWS_PER_SYMBOL,
)


@dataclass(frozen=True, slots=True)
class BinanceBreadthV2SymbolSummary:
    symbol: str
    row_count: int
    first_open_time_ms: int
    last_open_time_ms: int
    canonical_sha256: str
    source_archives: tuple[dict[str, str], ...]
    gaps: int = 0
    duplicates: int = 0
    malformed: int = 0


@dataclass(frozen=True, slots=True)
class PublishedBinanceBreadthAuthorityV2:
    root: Path
    canonical_digests: tuple[str, ...]
    semantic_authority_digest: str
    manifest_semantic_digest: str
    status: str = "VALIDATED"


def _json_bytes(value: object, *, pretty: bool = False) -> bytes:
    if pretty:
        return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _decimal(raw: str, field: str, *, positive: bool = False) -> Decimal:
    try:
        value = Decimal(raw)
    except InvalidOperation as error:
        raise ValueError(f"malformed {field}") from error
    if not value.is_finite() or value < 0 or (positive and value == 0):
        raise ValueError(f"invalid {field}")
    return value


def _load_v1_manifest(root: Path, scope: BinanceBreadthV2Scope) -> dict[str, object]:
    path = root / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    claimed = manifest.pop("manifest_semantic_digest", None)
    if claimed != _digest_bytes(_json_bytes(manifest)):
        raise ValueError("V1 manifest semantic digest mismatch")
    if (
        manifest.get("identity") != BINANCE_BREADTH_AUTHORITY_IDENTITY
        or tuple(manifest.get("symbols", ())) != scope.symbols
        or manifest.get("requested_start_ms") != scope.start_ms
        or manifest.get("requested_end_ms_exclusive") != scope.end_ms_exclusive
        or manifest.get("interval") != "1m"
        or manifest.get("common_timestamp_count") != scope.expected_rows_per_symbol
    ):
        raise ValueError("V1 manifest scope mismatch")
    details = manifest.get("symbols_detail")
    if (
        not isinstance(details, list)
        or tuple(item.get("symbol") for item in details) != scope.symbols
    ):
        raise ValueError("V1 manifest symbol details invalid")
    manifest["manifest_semantic_digest"] = claimed
    return manifest


def _archive_bindings(
    manifest: Mapping[str, object], scope: BinanceBreadthV2Scope
) -> dict[tuple[str, str], dict[str, str]]:
    bindings: dict[tuple[str, str], dict[str, str]] = {}
    for detail in manifest["symbols_detail"]:  # type: ignore[index]
        symbol = detail["symbol"]
        archives = detail["archives"]
        if tuple(item["month"] for item in archives) != scope.months:
            raise ValueError("V1 source archive month binding mismatch")
        for item in archives:
            if set(item) != {"month", "archive_sha256", "url"}:
                raise ValueError("V1 source archive binding shape invalid")
            checksum = item["archive_sha256"]
            if len(checksum) != 64:
                raise ValueError("V1 source archive checksum invalid")
            bindings[(symbol, item["month"])] = dict(item)
    if len(bindings) != len(scope.symbols) * len(scope.months):
        raise ValueError("source archive coverage mismatch")
    return bindings


def _validate_source_archives(
    v1_root: Path,
    bindings: Mapping[tuple[str, str], Mapping[str, str]],
    scope: BinanceBreadthV2Scope,
) -> None:
    cache = v1_root / "download-cache"
    expected = {
        cache / symbol / f"{month}.zip" for symbol in scope.symbols for month in scope.months
    }
    actual = set(cache.glob("*/*.zip"))
    if actual != expected:
        raise ValueError("retained source archive set is not exact")
    for (symbol, month), item in bindings.items():
        path = cache / symbol / f"{month}.zip"
        if _digest_path(path) != item["archive_sha256"]:
            raise ValueError("retained source archive checksum mismatch")
        try:
            with zipfile.ZipFile(path) as archive:
                expected_member = f"{symbol}-1m-{month}.csv"
                if archive.namelist() != [expected_member] or archive.testzip() is not None:
                    raise ValueError("retained source archive member invalid")
        except zipfile.BadZipFile as error:
            raise ValueError("retained source archive corrupt") from error


def _canonical_line(row: list[str], month: str) -> tuple[int, bytes]:
    if len(row) != 12:
        raise ValueError("provider kline row must contain exactly 12 fields")
    timestamp = normalize_native_timestamp(row[0], month)
    scale = 1 if month < "2025-01" else 1_000
    if int(row[6]) != int(row[0]) + INTERVAL_MS * scale - 1:
        raise ValueError("invalid candle close timestamp")
    opened = _decimal(row[1], "open", positive=True)
    high = _decimal(row[2], "high", positive=True)
    low = _decimal(row[3], "low", positive=True)
    closed = _decimal(row[4], "close", positive=True)
    _decimal(row[5], "volume")
    _decimal(row[7], "quote_volume")
    if low > min(opened, closed) or high < max(opened, closed):
        raise ValueError("invalid candle OHLC")
    line = f"{timestamp},{row[1]},{row[2]},{row[3]},{row[4]},{row[5]},{row[7]}\n"
    return timestamp, line.encode()


def _write_symbol(
    symbol: str,
    target: Path,
    v1_root: Path,
    bindings: Mapping[tuple[str, str], Mapping[str, str]],
    scope: BinanceBreadthV2Scope,
) -> BinanceBreadthV2SymbolSummary:
    header = (",".join(BINANCE_BREADTH_V2_SCHEMA) + "\n").encode()
    digest = hashlib.sha256(header)
    count = 0
    first = last = 0
    previous: int | None = None
    source: list[dict[str, str]] = []
    with target.open("wb") as output:
        output.write(header)
        for month in scope.months:
            binding = bindings[(symbol, month)]
            source.append(dict(binding))
            path = v1_root / "download-cache" / symbol / f"{month}.zip"
            with (
                zipfile.ZipFile(path) as archive,
                archive.open(f"{symbol}-1m-{month}.csv") as raw,
            ):
                reader = csv.reader(line.decode("utf-8") for line in raw)
                for row in reader:
                    timestamp, line = _canonical_line(row, month)
                    if previous is not None and timestamp != previous + INTERVAL_MS:
                        raise ValueError(
                            "duplicate candle" if timestamp == previous else "candle gap"
                        )
                    if not scope.start_ms <= timestamp < scope.end_ms_exclusive:
                        raise ValueError("candle outside authorized range")
                    output.write(line)
                    digest.update(line)
                    first = timestamp if count == 0 else first
                    last = previous = timestamp
                    count += 1
        output.flush()
        os.fsync(output.fileno())
    if (
        count != scope.expected_rows_per_symbol
        or first != scope.start_ms
        or last + INTERVAL_MS != scope.end_ms_exclusive
    ):
        raise ValueError("symbol coverage does not exactly match V2 scope")
    return BinanceBreadthV2SymbolSummary(
        symbol, count, first, last, digest.hexdigest(), tuple(source)
    )


def _semantic_manifest(
    summaries: tuple[BinanceBreadthV2SymbolSummary, ...], scope: BinanceBreadthV2Scope
) -> dict[str, object]:
    return {
        "authority_identity": BINANCE_BREADTH_AUTHORITY_V2_IDENTITY,
        "supersedes_for_active_free_v2": BINANCE_BREADTH_AUTHORITY_IDENTITY,
        "v1_status": BINANCE_BREADTH_V1_STATUS,
        "provider": "BINANCE_PUBLIC_ARCHIVES",
        "interval": "1m",
        "symbols": list(scope.symbols),
        "requested_start_ms": scope.start_ms,
        "requested_end_ms_exclusive": scope.end_ms_exclusive,
        "expected_archive_count": len(scope.symbols) * len(scope.months),
        "expected_rows_per_symbol": scope.expected_rows_per_symbol,
        "canonical_schema": list(BINANCE_BREADTH_V2_SCHEMA),
        "canonical_timestamp_unit": "UTC_milliseconds",
        "quote_volume_semantic_identity": QUOTE_VOLUME_SEMANTIC_IDENTITY,
        "quote_volume_source": QUOTE_VOLUME_SOURCE_FIELD,
        "quality_status": "VERIFIED_EXACT_COMMON_GRID_WITH_DIRECT_QUOTE_VOLUME",
        "symbols_detail": [asdict(item) for item in summaries],
    }


def publish_binance_breadth_v2(
    v1_root: Path = BINANCE_BREADTH_V1_ROOT,
    v2_root: Path = BINANCE_BREADTH_V2_ROOT,
    *,
    scope: BinanceBreadthV2Scope = PRODUCTION_SCOPE,
    phase_hook: Callable[[str], None] | None = None,
) -> PublishedBinanceBreadthAuthorityV2:
    """Republish V2 offline from exact V1-manifest-bound retained source archives."""
    if v2_root.exists():
        raise FileExistsError("Binance breadth V2 target already exists")
    notify = phase_hook or (lambda _: None)
    created = False
    try:
        manifest_v1 = _load_v1_manifest(v1_root, scope)
        bindings = _archive_bindings(manifest_v1, scope)
        _validate_source_archives(v1_root, bindings, scope)
        notify("source_archives_verified")
        canonical = v2_root / "canonical"
        canonical.mkdir(parents=True)
        created = True
        summaries = tuple(
            _write_symbol(symbol, canonical / f"{symbol}.csv", v1_root, bindings, scope)
            for symbol in scope.symbols
        )
        notify("canonical_staged")
        _verify_canonical(canonical, summaries, scope)
        notify("canonical_reloaded")
        semantic = _semantic_manifest(summaries, scope)
        authority_digest = _digest_bytes(_json_bytes(semantic))
        manifest: dict[str, object] = {
            **semantic,
            "semantic_authority_digest": authority_digest,
            "status": "VALIDATED",
        }
        manifest["manifest_semantic_digest"] = _digest_bytes(_json_bytes(manifest))
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".manifest.", suffix=".pending", dir=v2_root
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_json_bytes(manifest, pretty=True))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, v2_root / "manifest.json")
        notify("manifest_published")
        return verify_binance_breadth_v2(v2_root, v1_root=v1_root, scope=scope)
    except BaseException:
        if created:
            shutil.rmtree(v2_root, ignore_errors=True)
        raise


def _verify_canonical(
    canonical: Path,
    summaries: tuple[BinanceBreadthV2SymbolSummary, ...],
    scope: BinanceBreadthV2Scope,
) -> None:
    if tuple(item.symbol for item in summaries) != scope.symbols:
        raise ValueError("V2 symbol order mismatch")
    expected_paths = {canonical / f"{symbol}.csv" for symbol in scope.symbols}
    if set(canonical.glob("*.csv")) != expected_paths:
        raise ValueError("V2 canonical artifact set mismatch")
    common: tuple[int, int, int] | None = None
    for summary in summaries:
        path = canonical / f"{summary.symbol}.csv"
        if _digest_path(path) != summary.canonical_sha256:
            raise ValueError("V2 canonical digest mismatch")
        count = 0
        first = last = 0
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.reader(handle)
            if tuple(next(reader, ())) != BINANCE_BREADTH_V2_SCHEMA:
                raise ValueError("V2 canonical schema mismatch")
            previous: int | None = None
            for row in reader:
                if len(row) != len(BINANCE_BREADTH_V2_SCHEMA):
                    raise ValueError("V2 canonical row shape mismatch")
                timestamp = int(row[0])
                for index, field in enumerate(BINANCE_BREADTH_V2_SCHEMA[1:], 1):
                    _decimal(row[index], field, positive=field in {"open", "high", "low", "close"})
                if previous is not None and timestamp != previous + INTERVAL_MS:
                    raise ValueError("V2 canonical ordering mismatch")
                first = timestamp if count == 0 else first
                last = previous = timestamp
                count += 1
        evidence = (count, first, last)
        if evidence != (summary.row_count, summary.first_open_time_ms, summary.last_open_time_ms):
            raise ValueError("V2 canonical summary mismatch")
        common = evidence if common is None else common
        if evidence != common:
            raise ValueError("V2 cross-symbol coverage mismatch")


def verify_binance_breadth_v2(
    root: Path = BINANCE_BREADTH_V2_ROOT,
    *,
    v1_root: Path = BINANCE_BREADTH_V1_ROOT,
    scope: BinanceBreadthV2Scope = PRODUCTION_SCOPE,
) -> PublishedBinanceBreadthAuthorityV2:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    manifest_digest = manifest.pop("manifest_semantic_digest", None)
    if manifest_digest != _digest_bytes(_json_bytes(manifest)):
        raise ValueError("V2 manifest semantic digest mismatch")
    authority_digest = manifest.pop("semantic_authority_digest", None)
    status = manifest.pop("status", None)
    if status != "VALIDATED" or authority_digest != _digest_bytes(_json_bytes(manifest)):
        raise ValueError("V2 semantic authority digest mismatch")
    if manifest.get("authority_identity") != BINANCE_BREADTH_AUTHORITY_V2_IDENTITY:
        raise ValueError("V2 authority identity mismatch")
    if manifest.get("canonical_schema") != list(BINANCE_BREADTH_V2_SCHEMA):
        raise ValueError("V2 canonical schema binding mismatch")
    if manifest.get("symbols") != list(scope.symbols):
        raise ValueError("V2 symbol-order binding mismatch")
    if manifest.get("expected_archive_count") != len(scope.symbols) * len(scope.months):
        raise ValueError("V2 archive-count binding mismatch")
    if (
        manifest.get("requested_start_ms") != scope.start_ms
        or manifest.get("requested_end_ms_exclusive") != scope.end_ms_exclusive
        or manifest.get("expected_rows_per_symbol") != scope.expected_rows_per_symbol
        or manifest.get("quote_volume_semantic_identity") != QUOTE_VOLUME_SEMANTIC_IDENTITY
        or manifest.get("quote_volume_source") != QUOTE_VOLUME_SOURCE_FIELD
    ):
        raise ValueError("V2 authority contract mismatch")
    details = manifest.get("symbols_detail")
    if not isinstance(details, list):
        raise TypeError("V2 symbol details missing")
    summaries = tuple(
        BinanceBreadthV2SymbolSummary(**{**item, "source_archives": tuple(item["source_archives"])})
        for item in details
    )
    v1_manifest = _load_v1_manifest(v1_root, scope)
    bindings = _archive_bindings(v1_manifest, scope)
    _validate_source_archives(v1_root, bindings, scope)
    for summary in summaries:
        if summary.source_archives != tuple(
            dict(bindings[(summary.symbol, month)]) for month in scope.months
        ):
            raise ValueError("V2 source-checksum binding mismatch")
    _verify_canonical(root / "canonical", summaries, scope)
    return PublishedBinanceBreadthAuthorityV2(
        root,
        tuple(item.canonical_sha256 for item in summaries),
        str(authority_digest),
        str(manifest_digest),
    )
