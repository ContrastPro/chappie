"""Download public mark/index archives for offline leverage-stress research."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from app.research.intelligence.leverage_stress import (
    aggregate_completed_basis_5m,
    basis_payload,
    canonicalize_price_rows,
)
from app.tools.strategy_evaluate import write_report_atomic

BASE_URL = "https://data.binance.vision"
SYMBOLS = ("BTCUSDT", "ETHUSDT")
MAX_DAYS = 1_500
MAX_WORKERS = 8


def _days(start: str, end: str) -> tuple[str, ...]:
    current = datetime.fromisoformat(start).replace(tzinfo=UTC)
    finish = datetime.fromisoformat(end).replace(tzinfo=UTC)
    if current >= finish:
        raise ValueError("start must precede exclusive end")
    result: list[str] = []
    while current < finish:
        result.append(current.date().isoformat())
        current += timedelta(days=1)
    if len(result) > MAX_DAYS:
        raise ValueError(f"range exceeds bounded {MAX_DAYS}-day downloader")
    return tuple(result)


def _months(start: str, end: str) -> tuple[str, ...]:
    current = datetime.fromisoformat(start).replace(tzinfo=UTC).replace(day=1)
    finish = datetime.fromisoformat(end).replace(tzinfo=UTC).replace(day=1)
    if current >= finish:
        raise ValueError("start must precede exclusive end")
    result: list[str] = []
    while current < finish:
        result.append(current.strftime("%Y-%m"))
        current = current.replace(
            year=current.year + int(current.month == 12), month=current.month % 12 + 1
        )
    return tuple(result)


def _cache_path(cache_root: Path, symbol: str, kind: str, period: str, *, monthly: bool) -> Path:
    return (
        cache_root
        / ("monthly" if monthly else "daily")
        / symbol
        / kind
        / period[:4]
        / f"{period}.json"
    )


def _read_cache(path: Path) -> tuple[tuple[tuple[object, ...], ...], str] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows, digest = payload["rows"], payload["source_sha256"]
        if not isinstance(rows, list) or not isinstance(digest, str):
            raise TypeError("invalid cache payload")
        return tuple(tuple(row) for row in rows), digest
    except (OSError, TypeError, KeyError, ValueError, json.JSONDecodeError):
        return None


def _download_period(
    client: httpx.Client, *, cache_root: Path, symbol: str, kind: str, period: str, monthly: bool
) -> tuple[tuple[tuple[object, ...], ...], str, bool]:
    path = _cache_path(cache_root, symbol, kind, period, monthly=monthly)
    cached = _read_cache(path)
    if cached is not None:
        return *cached, True
    granularity = "monthly" if monthly else "daily"
    url = f"/data/futures/um/{granularity}/{kind}/{symbol}/1m/{symbol}-1m-{period}.zip"
    for attempt in range(3):
        response = client.get(url)
        if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
            time.sleep((attempt + 1) / 5)
            continue
        response.raise_for_status()
        try:
            archive = zipfile.ZipFile(io.BytesIO(response.content))
            members = archive.namelist()
            if len(members) != 1 or not members[0].endswith(".csv"):
                raise ValueError("unexpected archive members")
            rows = tuple(
                tuple(row)
                for row in csv.reader(io.TextIOWrapper(archive.open(members[0]), encoding="utf-8"))
            )
            if not rows or rows[0][0] != "open_time":
                raise ValueError("unexpected archive schema")
            canonicalize_price_rows(rows[1:])
        except zipfile.BadZipFile as error:
            raise ValueError("invalid public archive") from error
        digest = hashlib.sha256(response.content).hexdigest()
        write_report_atomic(
            path, {"source_url": f"{BASE_URL}{url}", "source_sha256": digest, "rows": rows[1:]}
        )
        return rows[1:], digest, False
    raise RuntimeError("unreachable retry loop")


def build_dataset(
    *,
    root: Path,
    cache_root: Path | None = None,
    start: str,
    end: str,
    symbols: tuple[str, ...] = SYMBOLS,
    monthly: bool = True,
    workers: int = 4,
) -> dict[str, object]:
    """Build deterministic completed-five-minute basis datasets from cached archives."""
    periods = _months(start, end) if monthly else _days(start, end)
    cache_root = cache_root or root / "cache" / "basis"
    if not 1 <= workers <= MAX_WORKERS:
        raise ValueError(f"workers must be 1..{MAX_WORKERS}")
    output: dict[str, object] = {
        "format": "chappie-leverage-stress-source-manifest/v1",
        "range": [start, end],
        "sources": {},
    }
    with httpx.Client(base_url=BASE_URL, timeout=45) as client:
        for symbol in symbols:
            kinds: dict[str, list[tuple[object, ...]]] = {
                "markPriceKlines": [],
                "indexPriceKlines": [],
            }
            source_digests: dict[str, list[str]] = {kind: [] for kind in kinds}
            reused = 0
            requests = [(kind, period) for period in periods for kind in kinds]
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(
                        _download_period,
                        client,
                        cache_root=cache_root,
                        symbol=symbol,
                        kind=kind,
                        period=period,
                        monthly=monthly,
                    ): (kind, period)
                    for kind, period in requests
                }
                completed = {futures[future]: future.result() for future in as_completed(futures)}
            for kind, period in requests:
                rows, digest, was_reused = completed[(kind, period)]
                kinds[kind].extend(rows)
                source_digests[kind].append(digest)
                reused += int(was_reused)
            rows = aggregate_completed_basis_5m(
                symbol=symbol,
                mark=canonicalize_price_rows(kinds["markPriceKlines"]),
                index=canonicalize_price_rows(kinds["indexPriceKlines"]),
            )
            path = root / "datasets" / f"{symbol.lower()}_basis_5m.json"
            write_report_atomic(path, basis_payload(rows))
            output["sources"][symbol] = {
                "basis_path": str(path),
                "basis_rows": len(rows),
                "source_periods": len(periods),
                "source_granularity": "monthly" if monthly else "daily",
                "reused_archives": reused,
                "mark_source_digests": source_digests["markPriceKlines"],
                "index_source_digests": source_digests["indexPriceKlines"],
            }
    write_report_atomic(root / "source_manifest.json", output)
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Download public mark/index basis research archives"
    )
    from app.research.paths import basis_archive_root, study_dir

    parser.add_argument("--root", type=Path, default=study_dir("leverage_stress") / "v4c")
    parser.add_argument("--cache-root", type=Path, default=basis_archive_root())
    parser.add_argument("--start", default="2023-01-01")
    parser.add_argument("--end", default=datetime.now(UTC).date().isoformat())
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--daily", action="store_true")
    args = parser.parse_args(argv)
    result = build_dataset(
        root=args.root,
        cache_root=args.cache_root,
        start=args.start,
        end=args.end,
        monthly=not args.daily,
        workers=args.workers,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
