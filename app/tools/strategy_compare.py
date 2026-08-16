"""Offline JSON-candle comparison tool; it never constructs an exchange client."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

from app.market.models import Candle
from app.strategy_history import load_dataset
from app.strategy_lab import StrategyComparisonConfig, compare_all


def _candles(path: Path) -> tuple[Candle, ...]:
    records = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(records, dict) and records.get("format") == "chappie-historical-candles/v1":
        return load_dataset(path).candles
    if not isinstance(records, list):
        raise TypeError("candle JSON must be a list")
    return tuple(
        Candle(
            **{
                **record,
                **{
                    name: Decimal(str(record[name]))
                    for name in ("open", "high", "low", "close", "volume")
                },
            }
        )
        for record in records
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare pure strategies over local historical candle JSON"
    )
    parser.add_argument("candles", type=Path, help="local JSON candle fixture; no network is used")
    parser.add_argument("--starting-equity", type=Decimal, default=Decimal(10000))
    parser.add_argument("--fee-rate", type=Decimal, default=Decimal("0.001"))
    args = parser.parse_args(argv)
    results = compare_all(
        _candles(args.candles), StrategyComparisonConfig(args.starting_equity, args.fee_rate)
    )
    for result in results:
        print(
            json.dumps(
                {
                    name: str(value) if isinstance(value, Decimal) else value
                    for name, value in asdict(result).items()
                },
                sort_keys=True,
                default=str,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
