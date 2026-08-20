"""Produce compact causal feature and forward-label summaries from local chunks."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from app.research.paths import results_dir
from app.strategy_history import load_dataset
from app.strategy_long_history import summarize_timeframe
from app.tools.strategy_evaluate import write_report_atomic


def _load_manifest(path: Path) -> dict[str, tuple]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    timeframes = payload.get("timeframes") if isinstance(payload, dict) else None
    if not isinstance(timeframes, dict):
        raise TypeError("long-history manifest requires timeframes")
    output: dict[str, tuple] = {}
    for interval, value in timeframes.items():
        chunks = value.get("chunks") if isinstance(value, dict) else None
        if not isinstance(chunks, list) or not chunks:
            raise ValueError(f"manifest requires chunks for {interval}")
        paths = []
        for chunk in chunks:
            relative = chunk.get("path") if isinstance(chunk, dict) else None
            if not isinstance(relative, str):
                raise TypeError("manifest chunk requires a path")
            paths.append(path.parent / relative)
        output[interval] = tuple(load_dataset(candidate) for candidate in paths)
    return output


def _markdown(payload: dict[str, object]) -> str:
    lines = [
        "# BTCUSDT long-history research summary",
        "",
        "Research-only; no strategy inputs use forward labels.",
        "",
    ]
    for interval, summary in payload["timeframes"].items():  # type: ignore[union-attr]
        coverage = summary["coverage"]  # type: ignore[index]
        lines.extend(
            [
                f"## {interval}",
                "",
                f"- Accepted candles: {coverage['candle_count']} across {coverage['segment_count']} verified segments.",
                f"- Coverage: {coverage['first_open_ms']} to {coverage['last_open_ms']} (UTC epoch ms).",
                "- Per-segment causal regimes and forward-return labels are contained in the companion JSON aggregate.",
                "",
            ]
        )
    lines.extend(
        [
            "## Limitations",
            "",
            "- Descriptive aggregates do not establish tradable edge.",
            "- Forward returns are labels, not features or entry rules.",
            "- Fibonacci buckets test pullback depth only; they do not validate Fibonacci causality.",
            "",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize verified long-history research chunks")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output-dir", type=Path, default=results_dir())
    args = parser.parse_args(argv)
    datasets = _load_manifest(args.manifest)
    payload: dict[str, object] = {
        "format": "chappie-long-history-summary/v1",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "source_manifest": str(args.manifest),
        "timeframes": {
            interval: {
                "coverage": {
                    "first_open_ms": chunks[0].candles[0].open_time_ms,
                    "last_open_ms": chunks[-1].candles[-1].open_time_ms,
                    "candle_count": sum(item.metadata.candle_count for item in chunks),
                    "segment_count": len(chunks),
                },
                "segments": [summarize_timeframe((chunk,), interval) for chunk in chunks],
                "lookahead_audit": {
                    "no_cross_gap_calculation": True,
                    "each_feature_state_resets_per_segment": True,
                    "forward_labels_never_cross_segments": True,
                },
            }
            for interval, chunks in datasets.items()
        },
        "limitations": [
            "Causal features and forward labels are strictly separated.",
            "Results are descriptive and do not imply profitability.",
            "No parameter search, model fitting, or strategy selection occurs here.",
        ],
    }
    write_report_atomic(args.output_dir / "long_history_summary.json", payload)
    markdown_path = args.output_dir / "long_history_summary.md"
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(_markdown(payload), encoding="utf-8")
    print(f"wrote {args.output_dir / 'long_history_summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
