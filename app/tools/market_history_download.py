"""Download a bounded public historical candle dataset for offline research."""

from __future__ import annotations

import argparse
from pathlib import Path

import httpx

from app.research.strategy.history import (
    PUBLIC_MARKET_DATA_URL,
    PublicKlineDownloader,
    download_segmented,
    utc_date_to_ms,
    write_dataset_atomic,
    write_segmented_provenance_atomic,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Download public Binance candles for offline research"
    )
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--interval", required=True, choices=("1m", "5m", "15m", "1h"))
    parser.add_argument("--start", required=True, help="UTC ISO date, inclusive")
    parser.add_argument("--end", required=True, help="UTC ISO date, exclusive")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--segmented",
        action="store_true",
        help="explicitly orchestrate bounded contiguous downloader-safe segments",
    )
    args = parser.parse_args(argv)
    with httpx.Client(
        base_url=PUBLIC_MARKET_DATA_URL, timeout=15.0, headers={"User-Agent": "chappie-research/1"}
    ) as client:
        downloader = PublicKlineDownloader(client)
        start_ms, end_ms = utc_date_to_ms(args.start), utc_date_to_ms(args.end)
        if args.segmented:
            acquisition = download_segmented(
                downloader,
                symbol=args.symbol,
                interval=args.interval,
                start_ms=start_ms,
                end_ms=end_ms,
            )
            print(f"requested range: {start_ms}..{end_ms}")
            print(f"planned {len(acquisition.segments)} contiguous segments")
            for index, segment in enumerate(acquisition.segments, start=1):
                print(
                    f"segment {index}: {segment.start_ms}..{segment.end_ms} "
                    f"({segment.candle_count} candles)"
                )
            provenance_path = args.output.with_suffix(
                args.output.suffix + ".segmented-acquisition.json"
            )
            write_segmented_provenance_atomic(provenance_path, acquisition)
            print(f"wrote segmented acquisition provenance to {provenance_path}")
            dataset = acquisition.dataset
        else:
            dataset = downloader.download(
                symbol=args.symbol,
                interval=args.interval,
                start_ms=start_ms,
                end_ms=end_ms,
            )
    write_dataset_atomic(args.output, dataset)
    print(
        f"wrote {dataset.metadata.candle_count} {dataset.metadata.symbol} {dataset.metadata.interval} candles to {args.output}"
    )
    print(f"sha256={dataset.metadata.candles_sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
