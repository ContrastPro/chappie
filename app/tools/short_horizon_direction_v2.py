"""CLI for non-predictive V2 feature materialization and preflight."""

from __future__ import annotations

import argparse
import json

from app.research.capital_flow.short_horizon_direction_v2_materializer import (
    benchmark_persisted_feature_store,
    materialize_research_only,
    verify_feature_store,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--materialize", action="store_true")
    parser.add_argument("--benchmark", action="store_true")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args(argv)
    if sum((args.materialize, args.benchmark, args.verify)) != 1:
        raise SystemExit("choose exactly one of --materialize, --benchmark, or --verify")
    if args.materialize:
        payload = materialize_research_only()
    elif args.benchmark:
        payload = benchmark_persisted_feature_store()
    else:
        payload = verify_feature_store()
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
