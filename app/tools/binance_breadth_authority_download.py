"""Explicit operator entrypoint for the approved Binance breadth authority acquisition."""

from __future__ import annotations

import argparse
from pathlib import Path

from app.research.capital_flow.binance_breadth_authority import (
    BINANCE_BREADTH_SYMBOLS_V1,
    END_MS,
    START_MS,
    acquire_authority,
)

ROOT = Path("data/research/source/capital_flow/binance_breadth_free_v1")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Acquire only the frozen Binance breadth authority."
    )
    parser.add_argument(
        "--status", action="store_true", help="report non-authoritative cache/staging only"
    )
    arguments = parser.parse_args(argv)
    manifest = ROOT / "manifest.json"
    print(f"requested UTC ms range: [{START_MS}, {END_MS})")
    print(f"frozen symbols: {len(BINANCE_BREADTH_SYMBOLS_V1)}")
    print("raw cache and canonical staging are NON-AUTHORITATIVE until manifest publication")
    if arguments.status:
        print(f"authoritative manifest present: {manifest.exists()}")
        return 0
    print(acquire_authority(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
