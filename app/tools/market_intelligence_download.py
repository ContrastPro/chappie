"""Download bounded public USD-M funding/OI research context; no credentials."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from decimal import Decimal
from pathlib import Path

import httpx

from app.research.intelligence.market import ContextDataset, ContextObservation, context_to_payload
from app.research.strategy.history import utc_date_to_ms

USD_M_FUTURES_URL = "https://fapi.binance.com"
MAX_REQUESTS = 100


def _write_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as output:
            json.dump(payload, output, sort_keys=True, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, path)
        os.unlink(temporary)
    except FileExistsError:
        os.unlink(temporary)
        raise FileExistsError(f"research dataset already exists: {path}") from None
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _funding(client: httpx.Client, symbol: str, start_ms: int, end_ms: int) -> ContextDataset:
    result: list[ContextObservation] = []
    cursor = start_ms
    for _ in range(MAX_REQUESTS):
        response = client.get(
            "/fapi/v1/fundingRate",
            params={
                "symbol": symbol,
                "startTime": str(cursor),
                "endTime": str(end_ms - 1),
                "limit": "1000",
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list) or not payload:
            break
        for item in payload:
            timestamp = int(item["fundingTime"])
            result.append(
                ContextObservation(
                    timestamp, timestamp, Decimal(item["fundingRate"]), USD_M_FUTURES_URL
                )
            )
        next_cursor = result[-1].timestamp_ms + 1
        if next_cursor <= cursor:
            raise ValueError("funding pagination did not advance")
        cursor = next_cursor
        if len(payload) < 1000:
            break
    else:
        raise ValueError("funding request bound exhausted")
    return ContextDataset("funding_rate", symbol, "8h", USD_M_FUTURES_URL, tuple(result))


def _open_interest(client: httpx.Client, symbol: str, start_ms: int, end_ms: int) -> ContextDataset:
    # Binance publishes this endpoint as public historical *recent* data with
    # a documented one-month retention limitation.  We retain that limitation
    # in the manifest rather than inventing unavailable old observations.
    response = client.get(
        "/futures/data/openInterestHist",
        params={
            "symbol": symbol,
            "period": "5m",
            "startTime": str(start_ms),
            "endTime": str(end_ms - 1),
            "limit": "500",
        },
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise TypeError("open interest response must be a list")
    observations = tuple(
        ContextObservation(
            int(item["timestamp"]),
            int(item["timestamp"]),
            Decimal(item["sumOpenInterest"]),
            USD_M_FUTURES_URL,
        )
        for item in payload
    )
    return ContextDataset("open_interest", symbol, "5m", USD_M_FUTURES_URL, observations)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download public BTCUSDT futures research context")
    parser.add_argument("--kind", choices=("funding", "open-interest"), required=True)
    parser.add_argument("--start", required=True, help="UTC ISO date, inclusive")
    parser.add_argument("--end", required=True, help="UTC ISO date, exclusive")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    with httpx.Client(
        base_url=USD_M_FUTURES_URL, timeout=20.0, headers={"User-Agent": "chappie-research/1"}
    ) as client:
        dataset = (_funding if args.kind == "funding" else _open_interest)(
            client, "BTCUSDT", utc_date_to_ms(args.start), utc_date_to_ms(args.end)
        )
    _write_atomic(args.output, context_to_payload(dataset))
    print(f"wrote {len(dataset.observations)} {dataset.kind} observations to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
