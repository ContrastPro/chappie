"""Create an offline event-validation report from local V3B flow datasets."""

from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path

from app.behavioral_market import analyze_behavioral_market
from app.market_intelligence_flow import FlowObservation, load_flow_payload
from app.research_paths import flow_read_root, results_dir
from app.tools.strategy_evaluate import write_report_atomic


def _load(path: Path) -> tuple[FlowObservation, ...]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return load_flow_payload(json.load(handle))


def _gaps(manifest: dict[str, object], symbol: str) -> tuple[int, ...]:
    sources = manifest.get("sources")
    source = sources.get(symbol) if isinstance(sources, dict) else None
    futures = source.get("futures_metrics") if isinstance(source, dict) else None
    raw = futures.get("gaps_after_ms") if isinstance(futures, dict) else None
    if not isinstance(raw, list):
        raise TypeError(f"missing metrics gap coverage for {symbol}")
    return tuple(int(value) for value in raw)


def _markdown(report: dict[str, object]) -> str:
    lines = [
        "# Behavioral Market Research v1",
        "",
        "Offline event validation only; not a trading signal or psychology claim.",
        "",
    ]
    for asset, result in report["assets"].items():  # type: ignore[index]
        lines += [
            f"## {asset}",
            "",
            "| Event | Collapsed | 12h independent | 12h class |",
            "|---|---:|---:|---|",
        ]
        for kind, item in result["event_types"].items():  # type: ignore[index]
            horizon = item["horizons"]["12h"]  # type: ignore[index]
            lines.append(
                f"| {kind} | {item['collapsed_events']} | {horizon['independent_events']} | {horizon['classification']} |"
            )
        lines.append("")
    lines += ["## Limitations", "", *[f"- {item}" for item in report["limitations"]], ""]  # type: ignore[index]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate behavioural market events from local V3B data"
    )
    parser.add_argument("--root", type=Path, default=flow_read_root())
    parser.add_argument("--output-dir", type=Path, default=results_dir())
    args = parser.parse_args(argv)
    manifest = json.loads((args.root / "source_manifest.json").read_text(encoding="utf-8"))
    datasets = {
        "BTCUSDT": _load(args.root / "datasets" / "btcusdt_flow_5m.json.gz"),
        "ETHUSDT": _load(args.root / "datasets" / "ethusdt_flow_5m.json.gz"),
    }
    report = analyze_behavioral_market(
        datasets, gaps_after_ms={asset: _gaps(manifest, asset) for asset in datasets}
    )
    write_report_atomic(args.output_dir / "behavioral_market_v1.json", report)
    (args.output_dir / "behavioral_market_v1.md").write_text(_markdown(report), encoding="utf-8")
    print(f"wrote {args.output_dir / 'behavioral_market_v1.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
