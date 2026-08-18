"""Build bounded, public flow-positioning research datasets.

The command is deliberately research-only: it uses public Binance Vision
archives and public funding history, writes only under ``data/research``, and
never constructs a runtime or execution client.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import os
import tempfile
import zipfile
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from time import sleep

import httpx

from app.market_intelligence import ContextDataset, ContextObservation
from app.market_intelligence_flow import (
    MetricsDataset,
    SpotFlowObservation,
    aggregate_completed_spot_5m,
    build_flow_observations,
    canonicalize_metrics_rows,
    flow_digest,
    flow_to_payload,
    load_flow_payload,
    load_metrics_payload,
    metrics_digest,
    metrics_to_payload,
    parse_spot_minute_rows_excluding_invalid,
)
from app.strategy_history import utc_date_to_ms
from app.tools.strategy_evaluate import write_report_atomic

ARCHIVE_BASE_URL = "https://data.binance.vision"
FUTURES_BASE_URL = "https://fapi.binance.com"
MAX_MONTHS = 72
MAX_WORKERS = 8
MAX_RETRIES = 3
SYMBOLS = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "BNBUSDT",
    "AVAXUSDT",
    "XRPUSDT",
    "LINKUSDT",
    "DOGEUSDT",
)


@dataclass(frozen=True, slots=True)
class DailyMetricsResult:
    day: str
    status: str
    dataset: MetricsDataset | None
    source_digest: str | None
    source_bytes: int
    reused: bool
    reason: str | None = None


def _months(start: str, end: str) -> tuple[str, ...]:
    current = datetime.fromisoformat(start).replace(tzinfo=UTC)
    finish = datetime.fromisoformat(end).replace(tzinfo=UTC)
    if current >= finish:
        raise ValueError("start must precede exclusive end")
    current = current.replace(day=1)
    result: list[str] = []
    while current < finish:
        result.append(current.strftime("%Y-%m"))
        current = datetime(
            current.year + (current.month == 12), current.month % 12 + 1, 1, tzinfo=UTC
        )
    if len(result) > MAX_MONTHS:
        raise ValueError(f"range exceeds bounded {MAX_MONTHS}-month downloader")
    return tuple(result)


def _days(start: str, end: str) -> tuple[str, ...]:
    current = datetime.fromisoformat(start).replace(tzinfo=UTC)
    finish = datetime.fromisoformat(end).replace(tzinfo=UTC)
    result: list[str] = []
    while current < finish:
        result.append(current.date().isoformat())
        current += timedelta(days=1)
    return tuple(result)


def _archive_rows(client: httpx.Client, path: str) -> tuple[tuple[tuple[str, ...], ...], str]:
    response = client.get(path)
    response.raise_for_status()
    source = f"{ARCHIVE_BASE_URL}{path}"
    try:
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        members = archive.namelist()
        if len(members) != 1 or not members[0].endswith(".csv"):
            raise ValueError("unexpected public archive members")
        return tuple(
            tuple(row)
            for row in csv.reader(io.TextIOWrapper(archive.open(members[0]), encoding="utf-8"))
        ), hashlib.sha256(response.content).hexdigest()
    except zipfile.BadZipFile as error:
        raise ValueError(f"invalid public archive: {source}") from error


def _spot_cache_path(cache_root: Path, symbol: str, month: str) -> Path:
    return cache_root / symbol / month[:4] / f"{month}.zip"


def _spot_archive_rows(
    client: httpx.Client, *, cache_root: Path, symbol: str, month: str
) -> tuple[tuple[tuple[str, ...], ...], str, bool]:
    """Load one immutable monthly spot archive, reusing its atomic local cache."""
    cache_path = _spot_cache_path(cache_root, symbol, month)
    if cache_path.exists():
        raw = cache_path.read_bytes()
        reused = True
    else:
        path = f"/data/spot/monthly/klines/{symbol}/1m/{symbol}-1m-{month}.zip"
        response = client.get(path)
        response.raise_for_status()
        raw = response.content
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{cache_path.name}.", dir=cache_path.parent
        )
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, cache_path)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise
        reused = False
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
        members = archive.namelist()
        if len(members) != 1 or not members[0].endswith(".csv"):
            raise ValueError("unexpected public archive members")
        rows = tuple(
            tuple(row)
            for row in csv.reader(io.TextIOWrapper(archive.open(members[0]), encoding="utf-8"))
        )
    except zipfile.BadZipFile as error:
        raise ValueError(f"invalid public archive cache: {cache_path}") from error
    return rows, hashlib.sha256(raw).hexdigest(), reused


def _cache_path(cache_root: Path, symbol: str, day: str) -> Path:
    return cache_root / symbol / day[:4] / day[5:7] / f"{day}.json"


def _load_cached_metrics(cache_root: Path, symbol: str, day: str) -> DailyMetricsResult | None:
    path = _cache_path(cache_root, symbol, day)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        metadata = payload["metadata"]
        if (
            not isinstance(metadata, dict)
            or metadata.get("symbol") != symbol
            or metadata.get("requested_date") != day
            or metadata.get("integrity_status") != "VERIFIED"
        ):
            raise ValueError("cache metadata does not match requested artifact")
        dataset = load_metrics_payload(payload.get("dataset"))
        if metadata.get("canonical_digest") != metrics_digest(dataset):
            raise ValueError("cache canonical digest does not match dataset")
        return DailyMetricsResult(
            day,
            "VERIFIED",
            dataset,
            str(metadata["source_digest"]),
            int(metadata["source_bytes"]),
            True,
        )
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as error:
        return DailyMetricsResult(day, "INVALID_SOURCE", None, None, 0, True, str(error))


def _write_cached_metrics(
    cache_root: Path,
    *,
    symbol: str,
    day: str,
    source_url: str,
    source_digest: str,
    source_bytes: int,
    dataset: MetricsDataset,
) -> None:
    observations = dataset.observations
    payload = {
        "format": "chappie-usdm-metrics-cache/v1",
        "metadata": {
            "symbol": symbol,
            "requested_date": day,
            "source_url": source_url,
            "source_digest": source_digest,
            "source_bytes": source_bytes,
            "downloaded_at_utc": datetime.now(UTC).isoformat(),
            "parsed_observation_count": len(observations),
            "first_timestamp_ms": observations[0].timestamp_ms,
            "last_timestamp_ms": observations[-1].timestamp_ms,
            "availability_status": "AVAILABLE",
            "integrity_status": "VERIFIED",
            "canonical_digest": metrics_digest(dataset),
        },
        "dataset": metrics_to_payload(dataset),
    }
    write_report_atomic(_cache_path(cache_root, symbol, day), payload)


def _download_metrics_day(
    client: httpx.Client, cache_root: Path, symbol: str, day: str
) -> DailyMetricsResult:
    cached = _load_cached_metrics(cache_root, symbol, day)
    if cached is not None:
        return cached
    path = f"/data/futures/um/daily/metrics/{symbol}/{symbol}-metrics-{day}.zip"
    source_url = f"{ARCHIVE_BASE_URL}{path}"
    for attempt in range(MAX_RETRIES):
        try:
            response = client.get(path)
            if response.status_code == 404:
                return DailyMetricsResult(day, "MISSING_ARCHIVE", None, None, 0, False, "HTTP 404")
            if response.status_code == 429 or response.status_code >= 500:
                if attempt + 1 < MAX_RETRIES:
                    sleep(2**attempt / 10)
                    continue
                return DailyMetricsResult(
                    day, "TRANSIENT_FAILURE", None, None, 0, False, f"HTTP {response.status_code}"
                )
            response.raise_for_status()
            rows, digest = _archive_rows_from_content(response.content, source_url)
            dataset = canonicalize_metrics_rows(rows, symbol=symbol, source=source_url)
            _write_cached_metrics(
                cache_root,
                symbol=symbol,
                day=day,
                source_url=source_url,
                source_digest=digest,
                source_bytes=len(response.content),
                dataset=dataset,
            )
            return DailyMetricsResult(
                day, "VERIFIED", dataset, digest, len(response.content), False
            )
        except (httpx.TimeoutException, httpx.TransportError) as error:
            if attempt + 1 < MAX_RETRIES:
                sleep(2**attempt / 10)
                continue
            return DailyMetricsResult(day, "TRANSIENT_FAILURE", None, None, 0, False, str(error))
        except httpx.HTTPStatusError as error:
            return DailyMetricsResult(
                day,
                "INVALID_SOURCE",
                None,
                None,
                0,
                False,
                f"HTTP {error.response.status_code}",
            )
        except (ValueError, zipfile.BadZipFile, UnicodeDecodeError) as error:
            return DailyMetricsResult(day, "INVALID_SOURCE", None, None, 0, False, str(error))
    raise AssertionError("retry loop must return")


def _archive_rows_from_content(
    content: bytes, source: str
) -> tuple[tuple[tuple[str, ...], ...], str]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
        members = archive.namelist()
        if len(members) != 1 or not members[0].endswith(".csv"):
            raise ValueError("unexpected public archive members")
        rows = tuple(
            tuple(row)
            for row in csv.reader(io.TextIOWrapper(archive.open(members[0]), encoding="utf-8"))
        )
    except zipfile.BadZipFile as error:
        raise ValueError(f"invalid public archive: {source}") from error
    return rows, hashlib.sha256(content).hexdigest()


def _assemble_metrics(results: tuple[DailyMetricsResult, ...], *, symbol: str) -> MetricsDataset:
    verified = tuple(item.dataset for item in results if item.status == "VERIFIED" and item.dataset)
    if not verified:
        raise ValueError("no verified futures metrics days")
    observations = tuple(
        sorted(
            (item for dataset in verified for item in dataset.observations),
            key=lambda item: item.timestamp_ms,
        )
    )
    if len({item.timestamp_ms for item in observations}) != len(observations):
        raise ValueError("duplicate futures metrics timestamp across daily archives")
    gaps = tuple(
        later.timestamp_ms
        for previous, later in pairwise(observations)
        if later.timestamp_ms - previous.timestamp_ms != 300_000
    )
    return MetricsDataset(
        symbol,
        ARCHIVE_BASE_URL,
        observations,
        sum(dataset.source_row_count for dataset in verified),
        all(dataset.was_source_ordered for dataset in verified),
        gaps,
    )


def _progress_payload(
    results: Iterable[DailyMetricsResult], *, requested_days: tuple[str, ...] | None = None
) -> dict[str, object]:
    observed = tuple(results)
    by_day = {item.day: item for item in observed}
    if len(by_day) != len(observed):
        raise ValueError("duplicate daily progress result")
    requested = requested_days or tuple(sorted(by_day))
    materialized = tuple(
        sorted(
            (
                by_day.get(day, DailyMetricsResult(day, "PENDING", None, None, 0, False))
                for day in requested
            ),
            key=lambda item: item.day,
        )
    )
    counts = {
        status: sum(item.status == status for item in materialized)
        for status in (
            "VERIFIED",
            "MISSING_ARCHIVE",
            "TRANSIENT_FAILURE",
            "INVALID_SOURCE",
            "PENDING",
        )
    }
    return {
        "format": "chappie-usdm-metrics-progress/v1",
        "updated_at_utc": datetime.now(UTC).isoformat(),
        "requested_days": len(materialized),
        "coverage_percent": str(Decimal(counts["VERIFIED"]) / Decimal(len(materialized)) * 100)
        if materialized
        else "0",
        "counts": counts,
        "days": [
            {"date": item.day, "status": item.status, "reused": item.reused, "reason": item.reason}
            for item in materialized
        ],
    }


def _reject_cross_day_duplicates(
    results: tuple[DailyMetricsResult, ...],
) -> tuple[tuple[DailyMetricsResult, ...], tuple[dict[str, object], ...]]:
    """Fail closed on a source overlap; neither conflicting day enters a segment."""
    owners: dict[int, str] = {}
    invalid_days: set[str] = set()
    anomalies: list[dict[str, object]] = []
    for result in results:
        if result.status != "VERIFIED" or result.dataset is None:
            continue
        for observation in result.dataset.observations:
            owner = owners.get(observation.timestamp_ms)
            if owner is not None and owner != result.day:
                invalid_days.update((owner, result.day))
                anomalies.append(
                    {
                        "type": "CROSS_DAY_DUPLICATE_METRICS_TIMESTAMP",
                        "timestamp_ms": observation.timestamp_ms,
                        "first_day": owner,
                        "second_day": result.day,
                    }
                )
            else:
                owners[observation.timestamp_ms] = result.day
    return (
        tuple(
            replace(
                result,
                status="INVALID_SOURCE",
                dataset=None,
                reason="cross-day duplicate metrics timestamp",
            )
            if result.day in invalid_days
            else result
            for result in results
        ),
        tuple(anomalies),
    )


def _funding(client: httpx.Client, symbol: str, start_ms: int, end_ms: int) -> ContextDataset:
    observations: list[ContextObservation] = []
    cursor = start_ms
    for _ in range(20):
        response = client.get(
            "/fapi/v1/fundingRate",
            params={"symbol": symbol, "startTime": cursor, "endTime": end_ms - 1, "limit": 1000},
        )
        response.raise_for_status()
        raw = response.json()
        if not isinstance(raw, list) or not raw:
            break
        for item in raw:
            timestamp = int(item["fundingTime"])
            observations.append(
                ContextObservation(
                    timestamp,
                    timestamp,
                    Decimal(item["fundingRate"]),
                    FUTURES_BASE_URL,
                )
            )
        next_cursor = observations[-1].timestamp_ms + 1
        if next_cursor <= cursor:
            raise ValueError("funding pagination did not advance")
        cursor = next_cursor
        if len(raw) < 1000:
            break
    else:
        raise ValueError("funding pagination bound exhausted")
    if not observations:
        raise ValueError(f"no public funding observations for {symbol}")
    return ContextDataset("funding_rate", symbol, "8h", FUTURES_BASE_URL, tuple(observations))


def _write_gzip_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as output:
                output.write(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _read_gzip_payload(path: Path) -> dict[str, object]:
    """Read one locally verified JSON artifact; never fetch as a fallback here."""
    with gzip.open(path, "rt", encoding="utf-8") as source:
        payload = json.load(source)
    if not isinstance(payload, dict):
        raise TypeError(f"invalid JSON object in {path}")
    return payload


def _funding_payload(dataset: ContextDataset) -> dict[str, object]:
    rows = [
        {
            "timestamp_ms": item.timestamp_ms,
            "published_at_ms": item.published_at_ms,
            "value": str(item.value),
            "source": item.source,
        }
        for item in dataset.observations
    ]
    digest = hashlib.sha256(
        json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "format": "chappie-funding-rate/v1",
        "symbol": dataset.symbol,
        "interval": dataset.frequency,
        "source": dataset.source,
        "rows": rows,
        "content_digest": digest,
    }


def _funding_cache_path(cache_root: Path, symbol: str, start: str, end: str) -> Path:
    return cache_root / "funding" / symbol / f"{start}_{end}.json.gz"


def _cached_funding(
    *, cache_root: Path, symbol: str, start: str, end: str
) -> ContextDataset | None:
    path = _funding_cache_path(cache_root, symbol, start, end)
    if not path.exists():
        return None
    payload = _read_gzip_payload(path)
    if (
        payload.get("format") != "chappie-funding-rate/v1"
        or payload.get("symbol") != symbol
        or not isinstance(payload.get("rows"), list)
    ):
        raise ValueError("funding cache identity does not match requested artifact")
    rows = payload["rows"]
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    if payload.get("content_digest") != hashlib.sha256(canonical).hexdigest():
        raise ValueError("funding cache digest does not match rows")
    observations = tuple(
        ContextObservation(
            int(row["timestamp_ms"]),
            int(row["published_at_ms"]),
            Decimal(str(row["value"])),
            str(row["source"]),
        )
        for row in rows
    )
    if not observations or tuple(sorted(item.timestamp_ms for item in observations)) != tuple(
        item.timestamp_ms for item in observations
    ):
        raise ValueError("funding cache observations must be non-empty and ordered")
    return ContextDataset("funding_rate", symbol, "8h", str(payload["source"]), observations)


def _load_spot_source(symbol: str) -> tuple[SpotFlowObservation, ...]:
    """Use only the separately verified canonical spot timeline."""
    from app.research_paths import spot_source_dir

    payload = _read_gzip_payload(spot_source_dir(symbol) / "flow_5m.json.gz")
    rows = load_flow_payload(payload)
    if not rows or any(row.symbol != symbol for row in rows):
        raise ValueError("canonical spot flow identity does not match requested symbol")
    return tuple(row.spot for row in rows)


def _derivatives_source_manifest(
    *,
    symbol: str,
    start: str,
    end: str,
    metrics: MetricsDataset,
    funding: ContextDataset,
    daily_results: dict[str, DailyMetricsResult],
    days: tuple[str, ...],
    cross_day_anomalies: tuple[dict[str, object], ...],
    flow: tuple[object, ...],
    metrics_path: Path,
    funding_path: Path,
    flow_path: Path,
) -> dict[str, object]:
    metric_digest = metrics_digest(metrics)
    funding_payload = _funding_payload(funding)
    flow_payload = flow_to_payload(flow)  # type: ignore[arg-type]
    counts = _progress_payload(daily_results.values(), requested_days=days)["counts"]
    return {
        "format": "chappie-derivatives-source/v1",
        "state": "VERIFIED",
        "schema_version": "market-intelligence-derivatives/v1",
        "symbol": symbol,
        "requested_start": start,
        "requested_end": end,
        "metrics": {
            "source": ARCHIVE_BASE_URL,
            "dataset": str(metrics_path),
            "content_digest": metric_digest,
            "first_timestamp_ms": metrics.observations[0].timestamp_ms,
            "last_timestamp_ms": metrics.observations[-1].timestamp_ms,
            "row_count": len(metrics.observations),
            "segment_count": len(metrics.gaps_after_ms) + 1,
            "gap_count": len(metrics.gaps_after_ms),
            "gaps_after_ms": list(metrics.gaps_after_ms),
            "source_ordered": metrics.was_source_ordered,
            "daily_status_counts": counts,
            "source_anomalies": list(cross_day_anomalies),
        },
        "funding": {
            "source": funding.source,
            "dataset": str(funding_path),
            "content_digest": funding_payload["content_digest"],
            "first_timestamp_ms": funding.observations[0].timestamp_ms,
            "last_timestamp_ms": funding.observations[-1].timestamp_ms,
            "row_count": len(funding.observations),
        },
        "flow": {
            "dataset": str(flow_path),
            "content_digest": flow_payload["digest"],
            "row_count": len(flow),
        },
    }


def _build_derivatives_only(
    *,
    symbols: tuple[str, ...],
    start: str,
    end: str,
    workers: int,
    cache_root: Path,
) -> dict[str, object]:
    """Backfill only futures facts onto pre-verified canonical spot flow.

    This is deliberately separate from the old V3B study builder: it neither
    downloads spot data nor recreates study-local source copies.
    """
    from app.research_paths import futures_source_dir

    days = _days(start, end)
    start_ms, end_ms = utc_date_to_ms(start), utc_date_to_ms(end)
    output: dict[str, object] = {
        "format": "chappie-derivatives-backfill-manifest/v1",
        "requested_start": start,
        "requested_end": end,
        "assets": {},
    }
    with (
        httpx.Client(base_url=ARCHIVE_BASE_URL, timeout=60.0) as archive_client,
        httpx.Client(base_url=FUTURES_BASE_URL, timeout=30.0) as futures_client,
    ):
        for symbol in symbols:
            try:
                target = futures_source_dir(symbol)
                daily_results: dict[str, DailyMetricsResult] = {}
                pending = []
                for day in days:
                    cached = _load_cached_metrics(cache_root, symbol, day)
                    if cached is None:
                        pending.append(day)
                    else:
                        daily_results[day] = cached
                progress = cache_root / "progress" / f"{symbol}_{start}_{end}.json"
                write_report_atomic(
                    progress, _progress_payload(daily_results.values(), requested_days=days)
                )
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    jobs = {
                        pool.submit(
                            _download_metrics_day, archive_client, cache_root, symbol, day
                        ): day
                        for day in pending
                    }
                    for job in as_completed(jobs):
                        item = job.result()
                        daily_results[item.day] = item
                        write_report_atomic(
                            progress, _progress_payload(daily_results.values(), requested_days=days)
                        )
                validated, anomalies = _reject_cross_day_duplicates(
                    tuple(daily_results[day] for day in days)
                )
                daily_results = {item.day: item for item in validated}
                metrics = _assemble_metrics(validated, symbol=symbol)
                funding = _cached_funding(
                    cache_root=cache_root, symbol=symbol, start=start, end=end
                )
                if funding is None:
                    funding = _funding(futures_client, symbol, start_ms, end_ms)
                    _write_gzip_atomic(
                        _funding_cache_path(cache_root, symbol, start, end),
                        _funding_payload(funding),
                    )
                spot = _load_spot_source(symbol)
                flow = build_flow_observations(
                    symbol=symbol, spot=spot, metrics=metrics, funding=funding
                )
                metrics_path = target / "metrics_5m.json.gz"
                funding_path = target / "funding_8h.json.gz"
                flow_path = target / "flow_5m.json.gz"
                _write_gzip_atomic(metrics_path, metrics_to_payload(metrics))
                _write_gzip_atomic(funding_path, _funding_payload(funding))
                _write_gzip_atomic(flow_path, flow_to_payload(flow))
                manifest = _derivatives_source_manifest(
                    symbol=symbol,
                    start=start,
                    end=end,
                    metrics=metrics,
                    funding=funding,
                    daily_results=daily_results,
                    days=days,
                    cross_day_anomalies=anomalies,
                    flow=flow,
                    metrics_path=metrics_path,
                    funding_path=funding_path,
                    flow_path=flow_path,
                )
                write_report_atomic(target / "source_manifest.json", manifest)
                output["assets"][symbol] = manifest
            except (OSError, RuntimeError, ValueError, httpx.HTTPError) as error:
                output["assets"][symbol] = {
                    "state": "FAILED",
                    "symbol": symbol,
                    "detail": str(error),
                }
    return output


def _build_spot_only(
    *,
    symbols: tuple[str, ...],
    start: str,
    end: str,
    workers: int,
    cache_root: Path,
) -> dict[str, object]:
    """Build canonical spot-only flow artifacts without any derivatives request."""
    from app.research_paths import spot_source_dir

    months = _months(start, end)
    start_ms, end_ms = utc_date_to_ms(start), utc_date_to_ms(end)
    output: dict[str, object] = {"format": "chappie-spot-flow-source-manifest/v1", "assets": {}}
    with httpx.Client(base_url=ARCHIVE_BASE_URL, timeout=60.0) as client:
        for symbol in symbols:
            target_root = spot_source_dir(symbol)
            try:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    archives = tuple(
                        pool.map(
                            lambda month, symbol=symbol: _spot_archive_rows(
                                client, cache_root=cache_root, symbol=symbol, month=month
                            ),
                            months,
                        )
                    )
                source_rows: list[tuple[str, ...]] = []
                digests: list[str] = []
                reused = 0
                for rows, digest, was_reused in archives:
                    source_rows.extend(rows)
                    digests.append(digest)
                    reused += int(was_reused)
                parsed, anomalies = parse_spot_minute_rows_excluding_invalid(source_rows)
                minutes = tuple(item for item in parsed if start_ms <= item.open_time_ms < end_ms)
                spot = aggregate_completed_spot_5m(minutes)
                if not spot:
                    raise ValueError("no completed five-minute spot observations")
                metrics = MetricsDataset(symbol, "NOT_REQUESTED", (), 0, True, ())
                flow = build_flow_observations(
                    symbol=symbol, spot=spot, metrics=metrics, funding=None
                )
                dataset_path = target_root / "flow_5m.json.gz"
                _write_gzip_atomic(dataset_path, flow_to_payload(flow))
                manifest = {
                    "format": "chappie-spot-flow-source/v1",
                    "state": "VERIFIED",
                    "symbol": symbol,
                    "requested_start": start,
                    "requested_end": end,
                    "flow_dataset": str(dataset_path),
                    "flow_digest": flow_digest(flow),
                    "spot_1m": {
                        "source": ARCHIVE_BASE_URL,
                        "first_timestamp_ms": spot[0].timestamp_ms,
                        "last_timestamp_ms": spot[-1].timestamp_ms,
                        "observation_count": len(spot),
                        "source_digest": hashlib.sha256("".join(digests).encode()).hexdigest(),
                        "anomalies": list(anomalies),
                        "reused_archives": reused,
                        "newly_downloaded_archives": len(months) - reused,
                    },
                    "derivatives": "NOT_REQUESTED",
                }
                write_report_atomic(target_root / "source_manifest.json", manifest)
                output["assets"][symbol] = manifest
            except (OSError, RuntimeError, ValueError, httpx.HTTPError) as error:
                output["assets"][symbol] = {
                    "state": "FAILED",
                    "symbol": symbol,
                    "detail": str(error),
                }
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build public flow/positioning research inputs")
    from app.research_paths import download_cache_dir, results_dir, study_dir

    parser.add_argument("--root", type=Path, default=study_dir("market_intelligence") / "v3b")
    parser.add_argument("--cache-root", type=Path, default=download_cache_dir() / "usdm_metrics")
    parser.add_argument("--spot-cache-root", type=Path, default=download_cache_dir() / "spot_1m")
    parser.add_argument("--start", default="2022-01-01")
    parser.add_argument("--end", default="2026-08-01", help="exclusive UTC calendar date")
    parser.add_argument("--symbols", nargs="+", choices=SYMBOLS, default=list(SYMBOLS))
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--spot-only",
        action="store_true",
        help="build canonical spot flow with derivatives explicitly NOT_REQUESTED",
    )
    parser.add_argument(
        "--derivatives-only",
        action="store_true",
        help="extend verified canonical spot flow with canonical USD-M/funding facts only",
    )
    args = parser.parse_args(argv)
    if not 1 <= args.workers <= MAX_WORKERS:
        parser.error(f"--workers must be between 1 and {MAX_WORKERS}")
    if args.spot_only and args.derivatives_only:
        parser.error("--spot-only and --derivatives-only are mutually exclusive")
    if args.spot_only:
        result = _build_spot_only(
            symbols=tuple(args.symbols),
            start=args.start,
            end=args.end,
            workers=args.workers,
            cache_root=args.spot_cache_root,
        )
        write_report_atomic(results_dir() / "spot_flow_backfill_manifest.json", result)
        print(json.dumps(result, sort_keys=True))
        return int(any(item.get("state") == "FAILED" for item in result["assets"].values()))
    if args.derivatives_only:
        result = _build_derivatives_only(
            symbols=tuple(args.symbols),
            start=args.start,
            end=args.end,
            workers=args.workers,
            cache_root=args.cache_root,
        )
        write_report_atomic(results_dir() / "derivatives_backfill_manifest.json", result)
        print(json.dumps(result, sort_keys=True))
        return int(any(item.get("state") == "FAILED" for item in result["assets"].values()))
    months = _months(args.start, args.end)
    days = _days(args.start, args.end)
    start_ms, end_ms = utc_date_to_ms(args.start), utc_date_to_ms(args.end)
    manifest: dict[str, object] = {
        "format": "chappie-market-intelligence-v3b-source-manifest/v1",
        "requested_start": args.start,
        "requested_end": args.end,
        "sources": {},
    }
    with (
        httpx.Client(
            base_url=ARCHIVE_BASE_URL, timeout=60.0, headers={"User-Agent": "chappie-research/1"}
        ) as archive_client,
        httpx.Client(
            base_url=FUTURES_BASE_URL, timeout=30.0, headers={"User-Agent": "chappie-research/1"}
        ) as futures_client,
    ):
        metrics_by_symbol: dict[
            str,
            tuple[
                MetricsDataset,
                dict[str, DailyMetricsResult],
                dict[str, object],
                tuple[dict[str, object], ...],
                Path,
            ],
        ] = {}
        # Cache all fine-grained daily inputs before reading the larger monthly
        # spot archives. An interrupted multi-year run therefore always advances
        # the durable metrics cache for every requested symbol.
        for symbol in args.symbols:
            progress_path = args.cache_root / "progress" / f"{symbol}_{args.start}_{args.end}.json"
            daily_results: dict[str, DailyMetricsResult] = {}
            pending_days: list[str] = []
            for day in days:
                cached = _load_cached_metrics(args.cache_root, symbol, day)
                if cached is None:
                    pending_days.append(day)
                else:
                    daily_results[day] = cached
            write_report_atomic(
                progress_path, _progress_payload(daily_results.values(), requested_days=days)
            )
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                pending = {
                    pool.submit(
                        _download_metrics_day, archive_client, args.cache_root, symbol, day
                    ): day
                    for day in pending_days
                }
                for future in as_completed(pending):
                    result = future.result()
                    daily_results[result.day] = result
                    write_report_atomic(
                        progress_path,
                        _progress_payload(daily_results.values(), requested_days=days),
                    )
            validated_days, cross_day_anomalies = _reject_cross_day_duplicates(
                tuple(daily_results[day] for day in days)
            )
            daily_results = {item.day: item for item in validated_days}
            write_report_atomic(
                progress_path, _progress_payload(daily_results.values(), requested_days=days)
            )
            metrics = _assemble_metrics(validated_days, symbol=symbol)
            daily_summary = _progress_payload(daily_results.values(), requested_days=days)
            metrics_by_symbol[symbol] = (
                metrics,
                daily_results,
                daily_summary,
                cross_day_anomalies,
                progress_path,
            )
        for symbol in args.symbols:
            metrics, daily_results, daily_summary, cross_day_anomalies, progress_path = (
                metrics_by_symbol[symbol]
            )
            spot_rows: list[tuple[str, ...]] = []
            spot_digests: list[str] = []
            spot_paths = tuple(
                f"/data/spot/monthly/klines/{symbol}/1m/{symbol}-1m-{month}.zip" for month in months
            )
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                spot_archives = tuple(
                    pool.map(lambda path: _archive_rows(archive_client, path), spot_paths)
                )
            for rows, digest in spot_archives:
                spot_rows.extend(rows)
                spot_digests.append(digest)
            parsed_minutes, spot_anomalies = parse_spot_minute_rows_excluding_invalid(spot_rows)
            minutes = tuple(
                item for item in parsed_minutes if start_ms <= item.open_time_ms < end_ms
            )
            spot = aggregate_completed_spot_5m(minutes)
            funding = _funding(futures_client, symbol, start_ms, end_ms)
            flow = build_flow_observations(
                symbol=symbol, spot=spot, metrics=metrics, funding=funding
            )
            dataset_path = args.root / "datasets" / f"{symbol.lower()}_flow_5m.json.gz"
            _write_gzip_atomic(dataset_path, flow_to_payload(flow))
            manifest["sources"][symbol] = {
                "flow_dataset": str(dataset_path),
                "flow_digest": flow_digest(flow),
                "spot_1m": {
                    "source": ARCHIVE_BASE_URL,
                    "first_timestamp_ms": spot[0].timestamp_ms,
                    "last_timestamp_ms": spot[-1].timestamp_ms,
                    "cadence": "5m derived from completed 1m candles",
                    "observation_count": len(spot),
                    "source_digest": hashlib.sha256("".join(spot_digests).encode()).hexdigest(),
                    "anomalies": list(spot_anomalies),
                },
                "futures_metrics": {
                    "source": ARCHIVE_BASE_URL,
                    "first_timestamp_ms": metrics.observations[0].timestamp_ms,
                    "last_timestamp_ms": metrics.observations[-1].timestamp_ms,
                    "cadence": "5m",
                    "observation_count": len(metrics.observations),
                    "source_digest": hashlib.sha256(
                        "".join(
                            item.source_digest or ""
                            for item in (daily_results[day] for day in days)
                            if item.status == "VERIFIED"
                        ).encode()
                    ).hexdigest(),
                    "source_ordered": metrics.was_source_ordered,
                    "gaps_after_ms": list(metrics.gaps_after_ms),
                    "progress_manifest": str(progress_path),
                    "daily_status_counts": daily_summary["counts"],
                    "cross_day_anomalies": list(cross_day_anomalies),
                    "requested_days": len(days),
                    "reused_verified_days": sum(
                        item.status == "VERIFIED" and item.reused for item in daily_results.values()
                    ),
                    "newly_downloaded_days": sum(
                        item.status == "VERIFIED" and not item.reused
                        for item in daily_results.values()
                    ),
                },
                "funding": {
                    "source": FUTURES_BASE_URL,
                    "first_timestamp_ms": funding.observations[0].timestamp_ms,
                    "last_timestamp_ms": funding.observations[-1].timestamp_ms,
                    "cadence": "8h",
                    "observation_count": len(funding.observations),
                },
            }
    write_report_atomic(args.root / "source_manifest.json", manifest)
    print(f"wrote {args.root / 'source_manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
