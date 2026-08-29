"""Operator CLI for create-only Capital Flow Free V2 analytical-view materialization."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.research.capital_flow.free_v2_analytical_view import (
    ANALYTICAL_ROOT,
    materialize_analytical_view,
)

SOURCE_ROOT = Path("E:/projects/chappie/data/research/derived/capital_flow/free_v2")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Stream the validated feature NDJSON into create-only monthly Parquet."
    )
    parser.add_argument("--source-root", type=Path, default=SOURCE_ROOT)
    parser.add_argument("--output-root", type=Path, default=ANALYTICAL_ROOT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest = materialize_analytical_view(args.source_root, args.output_root)
    print(
        json.dumps(
            {
                "analytical_view_identity": manifest["analytical_view_identity"],
                "analytical_semantic_digest": manifest["analytical_semantic_digest"],
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
