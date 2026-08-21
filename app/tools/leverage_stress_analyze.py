"""Analyze local V3B flow data with local V4C completed basis data."""

from __future__ import annotations

import argparse
import gzip
import json
from decimal import Decimal
from pathlib import Path

from app.research.intelligence.flow import conditional_lift, future_return_bps, load_flow_payload
from app.research.intelligence.leverage_stress import leverage_state, load_basis_payload
from app.research.paths import flow_read_root, leverage_read_root, results_dir
from app.tools.strategy_evaluate import write_report_atomic

HORIZONS = {"1h": 12, "3h": 36, "6h": 72, "12h": 144, "24h": 288}
THRESHOLDS = (60, 100, 150)


def _flow(path: Path):
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return load_flow_payload(json.load(handle))


def _basis(path: Path):
    return load_basis_payload(json.loads(path.read_text(encoding="utf-8")))


def analyze(flow_rows, basis_rows) -> dict[str, object]:
    """Causal, as-of join.  No missing basis row is carried forward."""
    basis_by_time = {row.timestamp_ms: row for row in basis_rows}
    output: dict[str, object] = {
        "coverage": {"flow_rows": len(flow_rows), "basis_rows": len(basis_rows)},
        "horizons": {},
    }
    for name, bars in HORIZONS.items():
        values: list[tuple[int, str, Decimal]] = []
        for index, row in enumerate(flow_rows):
            basis = basis_by_time.get(row.timestamp_ms)
            future = future_return_bps(flow_rows, index, bars)
            if basis is None or future is None:
                continue
            state = leverage_state(
                basis=basis, funding=row.funding, price_oi_state=row.price_oi_state
            )
            if state is not None:
                values.append((row.timestamp_ms, state, future))
        output["horizons"][name] = {
            "observations": len(values),
            "up": {
                str(threshold): conditional_lift(values, threshold_bps=Decimal(threshold))
                for threshold in THRESHOLDS
            },
            "down": {
                str(threshold): conditional_lift(
                    ((time, state, -value) for time, state, value in values),
                    threshold_bps=Decimal(threshold),
                )
                for threshold in THRESHOLDS
            },
        }
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analyze V4C leverage-stress basis context")
    parser.add_argument("--basis-root", type=Path, default=leverage_read_root())
    parser.add_argument("--flow-root", type=Path, default=flow_read_root())
    parser.add_argument("--output-dir", type=Path, default=results_dir())
    args = parser.parse_args(argv)
    assets = {}
    for symbol in ("BTCUSDT", "ETHUSDT"):
        assets[symbol] = analyze(
            _flow(args.flow_root / "datasets" / f"{symbol.lower()}_flow_5m.json.gz"),
            _basis(args.basis_root / "datasets" / f"{symbol.lower()}_basis_5m.json"),
        )
    payload = {
        "format": "chappie-leverage-stress-v4c/v1",
        "assets": assets,
        "limitations": [
            "Descriptive research only; no state is a trading signal.",
            "Basis is mark/index-derived; premium-index candles are not substituted.",
            "Missing/gapped basis is unavailable rather than forward-filled.",
            "This compact first report is not an event-level liquidation study.",
        ],
    }
    manifest = {
        "format": "chappie-leverage-stress-v4c-manifest/v1",
        "basis_root": str(args.basis_root),
        "flow_root": str(args.flow_root),
        "result_format": payload["format"],
    }
    write_report_atomic(args.output_dir / "leverage_stress_v4c.json", payload)
    write_report_atomic(args.output_dir / "leverage_stress_v4c_manifest.json", manifest)
    (args.output_dir / "leverage_stress_v4c.md").write_text(
        "# Leverage stress v4C\n\nResearch-only basis/OI/funding descriptive report.\n",
        encoding="utf-8",
    )
    print(f"wrote {args.output_dir / 'leverage_stress_v4c.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
