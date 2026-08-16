"""Download a bounded public historical candle dataset for offline research."""

from __future__ import annotations

import argparse
from pathlib import Path

import httpx

from app.strategy_history import (
    PUBLIC_MARKET_DATA_URL,
    PublicKlineDownloader,
    utc_date_to_ms,
    write_dataset_atomic,
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
    args = parser.parse_args(argv)
    with httpx.Client(
        base_url=PUBLIC_MARKET_DATA_URL, timeout=15.0, headers={"User-Agent": "chappie-research/1"}
    ) as client:
        dataset = PublicKlineDownloader(client).download(
            symbol=args.symbol,
            interval=args.interval,
            start_ms=utc_date_to_ms(args.start),
            end_ms=utc_date_to_ms(args.end),
        )
    write_dataset_atomic(args.output, dataset)
    print(
        f"wrote {dataset.metadata.candle_count} {dataset.metadata.symbol} {dataset.metadata.interval} candles to {args.output}"
    )
    print(f"sha256={dataset.metadata.candles_sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
