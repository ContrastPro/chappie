"""Operator CLI for create-only Research BTC target materialization."""

from __future__ import annotations

import argparse
import json
from itertools import chain
from pathlib import Path

from app.research.capital_flow.free_v2_analytical_view import ANALYTICAL_ROOT
from app.research.capital_flow.tsunami_research_targets import (
    BINANCE_V2_ROOT,
    HORIZON_ORDER,
    RESEARCH_TARGET_ROOT,
    iter_analytical_research_timestamps,
    iter_research_targets_for_horizon,
    load_research_btcusdt_candles,
    publish_research_targets,
    validate_analytical_source,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Materialize feature-free Research BTC targets from validated local inputs."
    )
    parser.add_argument("--analytical-root", type=Path, default=ANALYTICAL_ROOT)
    parser.add_argument("--binance-v2-root", type=Path, default=BINANCE_V2_ROOT)
    parser.add_argument("--output-root", type=Path, default=RESEARCH_TARGET_ROOT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    validate_analytical_source(args.analytical_root)
    candles = load_research_btcusdt_candles(args.binance_v2_root)
    rows = chain.from_iterable(
        iter_research_targets_for_horizon(
            iter_analytical_research_timestamps(args.analytical_root), candles, horizon
        )
        for horizon in HORIZON_ORDER
    )
    manifest = publish_research_targets(rows, args.output_root)
    print(
        json.dumps(
            {
                "target_identity": manifest["target_identity"],
                "target_semantic_digest": manifest["target_semantic_digest"],
                "partition_count": manifest["partition_count"],
                "total_rows": manifest["total_rows"],
                "root": str(args.output_root),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
