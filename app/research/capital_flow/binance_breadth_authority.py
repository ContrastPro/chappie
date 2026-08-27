"""Bounded, archive-only Binance breadth authority acquisition; never feature calculation."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from .providers import BREADTH_SYMBOLS_V1

BINANCE_BREADTH_AUTHORITY_IDENTITY = "chappie_binance_breadth_historical_authority/1.0.0"
BINANCE_ARCHIVE_ROOT = "https://data.binance.vision/data/spot/monthly/klines"
BINANCE_BREADTH_SYMBOLS_V1 = ("BTCUSDT",) + BREADTH_SYMBOLS_V1
START_MS = 1_704_067_200_000
END_MS = 1_780_272_000_000
INTERVAL_MS = 60_000


@dataclass(frozen=True, slots=True)
class SymbolAuthoritySummaryV1:
    symbol: str
    candle_count: int
    first_timestamp_ms: int
    last_timestamp_ms: int
    canonical_sha256: str
    gaps: int
    duplicates: int
    malformed: int
    native_units: tuple[str, ...]
    archives: tuple[dict[str, str], ...]


def months() -> tuple[str, ...]:
    result: list[str] = []
    year, month = 2024, 1
    while (year, month) <= (2026, 5):
        result.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return tuple(result)


def native_timestamp_divisor(month: str) -> int:
    return 1_000 if month < "2025-01" else 1_000_000


def normalize_native_timestamp(value: str, month: str) -> int:
    if not value.isdigit():
        raise ValueError("malformed timestamp")
    timestamp = int(value)
    expected_digits = 13 if month < "2025-01" else 16
    if timestamp <= 0 or len(str(timestamp)) != expected_digits:
        raise ValueError("unexpected timestamp magnitude")
    return timestamp if month < "2025-01" else timestamp // 1_000


def archive_url(symbol: str, month: str) -> str:
    return f"{BINANCE_ARCHIVE_ROOT}/{symbol}/1m/{symbol}-1m-{month}.zip"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_archive(
    client: httpx.Client, symbol: str, month: str, cache: Path
) -> tuple[Path, str]:
    url = archive_url(symbol, month)
    checksum_response = client.get(url + ".CHECKSUM")
    checksum_response.raise_for_status()
    expected = checksum_response.text.strip().split()[0]
    if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
        raise ValueError("invalid official archive checksum")
    path = cache / symbol / f"{month}.zip"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and _sha256(path) == expected:
        print(f"cache verified {symbol} {month}")
        return path, expected
    temporary = path.with_suffix(".partial")
    with client.stream("GET", url) as response, temporary.open("wb") as handle:
        response.raise_for_status()
        for chunk in response.iter_bytes(1 << 20):
            handle.write(chunk)
    if _sha256(temporary) != expected:
        temporary.unlink(missing_ok=True)
        raise ValueError("archive checksum mismatch")
    os.replace(temporary, path)
    print(f"downloaded and checksum-verified {symbol} {month}")
    return path, expected


def _validate_and_write_symbol(
    symbol: str, destination: Path, cache: Path, client: httpx.Client
) -> SymbolAuthoritySummaryV1:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".pending")
    digest = hashlib.sha256()
    count = gaps = duplicates = malformed = 0
    previous: int | None = None
    first = last = 0
    provenance: list[dict[str, str]] = []
    try:
        with temporary.open("w", newline="", encoding="utf-8") as out:
            writer = csv.writer(out, lineterminator="\n")
            writer.writerow(("open_time_ms", "open", "high", "low", "close", "volume"))
            for month in months():
                print(f"staging NON-AUTHORITATIVE {symbol} {month}")
                archive, checksum = _download_archive(client, symbol, month, cache)
                provenance.append(
                    {"month": month, "archive_sha256": checksum, "url": archive_url(symbol, month)}
                )
                with zipfile.ZipFile(archive) as zipped:
                    members = [name for name in zipped.namelist() if name.endswith(".csv")]
                    if len(members) != 1:
                        raise ValueError("archive must contain exactly one CSV")
                    with zipped.open(members[0]) as raw:
                        for row in csv.reader(line.decode("utf-8") for line in raw):
                            if len(row) < 7:
                                malformed += 1
                                raise ValueError("malformed candle")
                            timestamp = normalize_native_timestamp(row[0], month)
                            raw_close_timestamp = int(row[6])
                            native_scale = 1 if month < "2025-01" else 1_000
                            if raw_close_timestamp != int(row[0]) + INTERVAL_MS * native_scale - 1:
                                malformed += 1
                                raise ValueError("invalid candle close timestamp")
                            try:
                                values = tuple(float(row[index]) for index in (1, 2, 3, 4, 5))
                            except ValueError as error:
                                malformed += 1
                                raise ValueError("invalid candle numeric value") from error
                            opened, high, low, closed, volume = values
                            if (
                                opened <= 0
                                or low <= 0
                                or closed <= 0
                                or volume < 0
                                or low > min(opened, closed)
                                or high < max(opened, closed)
                            ):
                                malformed += 1
                                raise ValueError("invalid candle OHLCV")
                            if previous is not None:
                                if timestamp == previous:
                                    duplicates += 1
                                    raise ValueError("duplicate candle")
                                if timestamp != previous + INTERVAL_MS:
                                    gaps += 1
                                    raise ValueError("candle gap")
                            if timestamp < START_MS or timestamp >= END_MS:
                                malformed += 1
                                raise ValueError("candle outside authorized range")
                            line = f"{timestamp},{row[1]},{row[2]},{row[3]},{row[4]},{row[5]}\n"
                            out.write(line)
                            digest.update(line.encode())
                            first = timestamp if count == 0 else first
                            last, previous, count = timestamp, timestamp, count + 1
        expected_count = (END_MS - START_MS) // INTERVAL_MS
        if count != expected_count or first != START_MS or last + INTERVAL_MS != END_MS:
            raise ValueError("symbol coverage does not exactly match authorized range")
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return SymbolAuthoritySummaryV1(
        symbol,
        count,
        first,
        last,
        digest.hexdigest(),
        gaps,
        duplicates,
        malformed,
        tuple("milliseconds" if month < "2025-01" else "microseconds" for month in months()),
        tuple(provenance),
    )


def acquire_authority(root: Path) -> Path:
    """Acquire all frozen symbols; manifest is published only after full validation."""
    canonical = root / "canonical"
    cache = root / "download-cache"
    manifest = root / "manifest.json"
    if manifest.exists():
        raise FileExistsError("authority manifest already exists")
    summaries: list[SymbolAuthoritySummaryV1] = []
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        for symbol in BINANCE_BREADTH_SYMBOLS_V1:
            print(f"validating NON-AUTHORITATIVE symbol {symbol}")
            summaries.append(
                _validate_and_write_symbol(symbol, canonical / f"{symbol}.csv", cache, client)
            )
    reference = summaries[0]
    if any(
        (item.candle_count, item.first_timestamp_ms, item.last_timestamp_ms)
        != (reference.candle_count, reference.first_timestamp_ms, reference.last_timestamp_ms)
        for item in summaries
    ):
        raise ValueError("cross-symbol timestamp alignment mismatch")
    payload: dict[str, object] = {
        "identity": BINANCE_BREADTH_AUTHORITY_IDENTITY,
        "provider": "BINANCE_PUBLIC_ARCHIVES",
        "requested_start_ms": START_MS,
        "requested_end_ms_exclusive": END_MS,
        "interval": "1m",
        "symbols": BINANCE_BREADTH_SYMBOLS_V1,
        "canonical_timestamp_unit": "UTC_milliseconds",
        "quality_status": "VERIFIED_EXACT_COMMON_GRID",
        "common_timestamp_count": reference.candle_count,
        "symbols_detail": [asdict(item) for item in summaries],
    }
    semantic = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    payload["manifest_semantic_digest"] = hashlib.sha256(semantic.encode()).hexdigest()
    descriptor, temporary_name = tempfile.mkstemp(prefix=".manifest.", dir=root)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary_name, manifest)
    print(f"published authoritative manifest {manifest}")
    return manifest
