"""Build immutable monthly public BTCUSDT research chunks and a local manifest."""

from __future__ import annotations

import argparse
import csv
import io
import json
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from functools import partial
from pathlib import Path

import httpx

from app.market.intervals import fixed_binance_spot_interval_ms
from app.market.models import Candle
from app.research_anomalies import (
    SegmentationResult,
    anomaly_ledger_payload,
    quality_status,
    split_historical_rows,
)
from app.research_paths import spot_source_dir
from app.strategy_history import (
    HistoricalDataset,
    HistoricalDatasetMetadata,
    candles_sha256,
    load_dataset,
    utc_date_to_ms,
    validate_candles,
    write_dataset_atomic,
)
from app.tools.strategy_evaluate import write_report_atomic

# Binance's public 1h archives before 2022 contain timestamp/gap anomalies
# rejected by the shared strict validator.  2022-01 is the earliest verified
# continuous long-history segment; no source candle is repaired or skipped.
STARTS = {"1h": "2022-01-01", "15m": "2023-01-01", "5m": "2025-01-01"}
ARCHIVE_BASE_URL = "https://data.binance.vision"


def _month_starts(start: str, end: str) -> tuple[tuple[str, str], ...]:
    current = datetime.fromisoformat(start).replace(tzinfo=UTC)
    finish = datetime.fromisoformat(end).replace(tzinfo=UTC)
    chunks: list[tuple[str, str]] = []
    while current < finish:
        next_month = datetime(
            current.year + (current.month == 12), current.month % 12 + 1, 1, tzinfo=UTC
        )
        end_month = min(next_month, finish)
        chunks.append((current.date().isoformat(), end_month.date().isoformat()))
        current = next_month
    return tuple(chunks)


def _archive_dataset(
    client: httpx.Client, symbol: str, interval: str, start: str, end: str
) -> HistoricalDataset:
    """Import one bounded public monthly archive, then apply the strict validator."""
    filename = f"{symbol}-{interval}-{start[:7]}.zip"
    response = client.get(f"/data/spot/monthly/klines/{symbol}/{interval}/{filename}")
    response.raise_for_status()
    try:
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        members = archive.namelist()
        if len(members) != 1 or not members[0].endswith(".csv"):
            raise ValueError("unexpected Binance Vision archive members")
        rows = list(csv.reader(io.TextIOWrapper(archive.open(members[0]), encoding="utf-8")))
    except zipfile.BadZipFile as error:
        raise ValueError("invalid Binance Vision archive") from error
    candles = validate_candles(
        tuple(
            Candle(
                open_time_ms=int(row[0]),
                open=Decimal(row[1]),
                high=Decimal(row[2]),
                low=Decimal(row[3]),
                close=Decimal(row[4]),
                volume=Decimal(row[5]),
                close_time_ms=int(row[6]),
            )
            for row in rows
            if len(row) >= 7
        ),
        interval,
    )
    start_ms, end_ms = utc_date_to_ms(start), utc_date_to_ms(end)
    interval_ms = fixed_binance_spot_interval_ms(interval)
    if candles[0].open_time_ms != start_ms or candles[-1].open_time_ms != end_ms - interval_ms:
        raise ValueError(f"incomplete public archive chunk: {start} through {end}")
    return HistoricalDataset(
        HistoricalDatasetMetadata(
            symbol,
            interval,
            start_ms,
            end_ms,
            candles[0].open_time_ms,
            candles[-1].open_time_ms,
            len(candles),
            datetime.now(UTC).isoformat(),
            f"{ARCHIVE_BASE_URL}/data/spot/monthly/klines/{symbol}/{interval}/{filename}",
            candles_sha256(candles),
        ),
        candles,
    )


def _archive_rows(
    client: httpx.Client, symbol: str, interval: str, start: str
) -> tuple[tuple[list[str], ...], str]:
    filename = f"{symbol}-{interval}-{start[:7]}.zip"
    source = f"{ARCHIVE_BASE_URL}/data/spot/monthly/klines/{symbol}/{interval}/{filename}"
    response = client.get(f"/data/spot/monthly/klines/{symbol}/{interval}/{filename}")
    response.raise_for_status()
    try:
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        members = archive.namelist()
        if len(members) != 1 or not members[0].endswith(".csv"):
            raise ValueError("unexpected Binance Vision archive members")
        return tuple(
            csv.reader(io.TextIOWrapper(archive.open(members[0]), encoding="utf-8"))
        ), source
    except zipfile.BadZipFile as error:
        raise ValueError("invalid Binance Vision archive") from error


def _verified_or_download(
    client: httpx.Client, path: Path, symbol: str, interval: str, start: str, end: str
) -> HistoricalDataset:
    start_ms, end_ms = utc_date_to_ms(start), utc_date_to_ms(end)
    if path.exists():
        dataset = load_dataset(path)
        if (
            dataset.metadata.symbol != symbol
            or dataset.metadata.interval != interval
            or dataset.metadata.requested_start_ms != start_ms
            or dataset.metadata.requested_end_ms != end_ms
        ):
            raise ValueError(f"existing chunk has incompatible metadata: {path}")
        return dataset
    dataset = _archive_dataset(client, symbol, interval, start, end)
    interval_ms = fixed_binance_spot_interval_ms(interval)
    if (
        dataset.metadata.actual_first_open_ms != start_ms
        or dataset.metadata.actual_last_open_ms != end_ms - interval_ms
    ):
        raise ValueError(f"incomplete public chunk: {start} through {end}")
    write_dataset_atomic(path, dataset)
    return dataset


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build long-history public spot research chunks")
    parser.add_argument("--root", type=Path, default=spot_source_dir("BTCUSDT") / "long_history")
    parser.add_argument(
        "--end", default=datetime.now(UTC).date().isoformat(), help="exclusive completed UTC date"
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--symbol",
        default="BTCUSDT",
        choices=("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "ETHBTC", "SOLBTC", "BNBBTC"),
    )
    parser.add_argument(
        "--interval", choices=tuple(STARTS), help="download one research interval instead of all"
    )
    args = parser.parse_args(argv)
    if args.workers < 1 or args.workers > 8:
        parser.error("--workers must be between 1 and 8")
    all_datasets: dict[str, tuple[HistoricalDataset, ...]] = {}
    ledgers: dict[str, dict[str, object]] = {}
    quality: dict[str, str] = {}
    with httpx.Client(
        base_url=ARCHIVE_BASE_URL, timeout=30.0, headers={"User-Agent": "chappie-research/1"}
    ) as client:
        selected_starts = {args.interval: STARTS[args.interval]} if args.interval else STARTS
        for interval, start in selected_starts.items():
            jobs = tuple(_month_starts(start, args.end))
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                monthly = tuple(
                    pool.map(partial(_segment_job, client, args.symbol, interval), jobs)
                )
            combined = SegmentationResult(
                tuple(segment for result in monthly for segment in result.segments),
                tuple(anomaly for result in monthly for anomaly in result.anomalies),
                sum(result.requested_rows for result in monthly),
            )
            # Monthly archive boundaries are not discontinuities. Merge only
            # exactly adjacent already-validated segments; never bridge a gap.
            merged: list[list[Candle]] = []
            interval_ms = fixed_binance_spot_interval_ms(interval)
            for segment in combined.segments:
                if merged and segment[0].open_time_ms == merged[-1][-1].open_time_ms + interval_ms:
                    merged[-1].extend(segment)
                else:
                    merged.append(list(segment))
            combined = SegmentationResult(
                tuple(tuple(segment) for segment in merged),
                combined.anomalies,
                combined.requested_rows,
            )
            segments: list[HistoricalDataset] = []
            for index, segment in enumerate(combined.segments, start=1):
                start_ms = segment[0].open_time_ms
                end_ms = segment[-1].open_time_ms + fixed_binance_spot_interval_ms(interval)
                dataset = HistoricalDataset(
                    HistoricalDatasetMetadata(
                        args.symbol,
                        interval,
                        start_ms,
                        end_ms,
                        start_ms,
                        segment[-1].open_time_ms,
                        len(segment),
                        datetime.now(UTC).isoformat(),
                        ARCHIVE_BASE_URL,
                        candles_sha256(segment),
                    ),
                    segment,
                )
                path = args.root / "segments" / interval / f"segment-{index:04}.json"
                if path.exists():
                    if load_dataset(path) != dataset:
                        raise ValueError(f"existing segment differs from source evidence: {path}")
                else:
                    write_dataset_atomic(path, dataset)
                segments.append(dataset)
            all_datasets[interval] = tuple(segments)
            ledgers[interval] = anomaly_ledger_payload(combined)
            quality[interval] = quality_status(combined)
            write_report_atomic(args.root / "ledgers" / f"{interval}.json", ledgers[interval])
    payload = {
        "format": "chappie-long-history-manifest/v1",
        "symbol": args.symbol,
        "integrity_status": "VERIFIED",
        "timeframes": {
            interval: {
                "requested_start": STARTS[interval],
                "accepted_candles": sum(item.metadata.candle_count for item in chunks),
                "segment_count": len(chunks),
                "quality_status": quality[interval],
                "anomaly_ledger": f"ledgers/{interval}.json",
                "chunks": [
                    {
                        "path": f"segments/{interval}/segment-{index:04}.json",
                        "metadata": asdict(item.metadata),
                    }
                    for index, item in enumerate(chunks, start=1)
                ],
            }
            for interval, chunks in all_datasets.items()
        },
    }
    write_report_atomic(args.root / "long_history_manifest.json", payload)
    print(
        json.dumps(
            {key: value["accepted_candles"] for key, value in payload["timeframes"].items()},
            sort_keys=True,
        )
    )
    return 0


def _segment_job(
    client: httpx.Client, symbol: str, interval: str, job: tuple[str, str]
) -> SegmentationResult:
    rows, source = _archive_rows(client, symbol, interval, job[0])
    return split_historical_rows(rows, source=source, symbol=symbol, interval=interval)


if __name__ == "__main__":
    raise SystemExit(main())
