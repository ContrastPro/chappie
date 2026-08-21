"""CLI for the bounded Multi-Asset Flow Intelligence v1 experiment."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.research.intelligence.multi_asset_flow import analyze, report_digest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--store", type=Path, default=Path("data/research/compact/research.sqlite3")
    )
    parser.add_argument("--output", type=Path, default=Path("data/research/results"))
    args = parser.parse_args()
    report = analyze(args.store)
    args.output.mkdir(parents=True, exist_ok=True)
    json_path = args.output / "multi_asset_flow_intelligence_v1.json"
    md_path = args.output / "multi_asset_flow_intelligence_v1.md"
    manifest_path = args.output / "multi_asset_flow_intelligence_v1_manifest.json"
    digest = report_digest(report)
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    lines = [
        "# Multi-Asset Flow Intelligence v1",
        "",
        f"Report digest: `{digest}`",
        "",
        "## Coverage",
        "",
        f"- Exact common source rows: {report['coverage']['exact_common_source_rows']}",
        f"- Exact common first timestamp: {report['coverage']['exact_common_first_timestamp_ms']}",
        f"- Exact common last timestamp: {report['coverage']['exact_common_last_timestamp_ms']}",
        f"- Causal usable rows: {report['coverage']['causal_usable_rows']}",
        "",
        "## Incremental forward-return cells",
        "",
        "| State | Target | Horizon | Matches | Gate | Mean bps | Median bps |",
        "|---|---:|---:|---:|---|---:|---:|",
    ]
    for name, item in report["results"].items():
        for target, horizons in item["targets"].items():
            for horizon, cell in horizons.items():
                lines.append(
                    f"| {name} | {target} | {horizon}m | {cell['matches']} | {cell['sample_gate']} | {cell.get('incremental_mean_forward_return_bps', 0):.3f} | {cell.get('incremental_median_forward_return_bps', 0):.3f} |"
                )
    lines.extend(
        [
            "",
            "## Verdict",
            "",
            f"- Primary: **{report['verdict']}**",
            f"- Remaining derivatives: **{report['remaining_derivatives_recommendation']}**",
            f"- Placebo triggered: {report['placebo']['triggered']} ({report['placebo']['reason']})",
            "",
            "## Component disposition",
            "",
        ]
    )
    lines.extend(
        f"- {name}: **{value}**" for name, value in report["component_disposition"].items()
    )
    comparison = report["multiple_comparisons"]
    lines.extend(
        [
            "",
            "## Multiple-comparison context",
            "",
            f"- Primary cells: {comparison['primary_cells']}",
            f"- Median effect: {comparison['median_effect_bps']:.3f} bps",
            f"- Positive/negative cells: {comparison['sign_consistent_positive_cells']}/{comparison['sign_consistent_negative_cells']}",
            f"- Strongest positive: {comparison['strongest_positive']}",
            f"- Strongest negative: {comparison['strongest_negative']}",
        ]
    )
    md_path.write_text("\n".join(lines) + "\n")
    manifest = {
        "methodology": report["identity"]["methodology"],
        "universe": report["identity"]["universe"],
        "coverage": report["coverage"],
        "state_definitions": report["contract"]["states"],
        "hypothesis_definitions": report["contract"]["combinations"],
        "horizons_minutes": sorted(
            int(value) for value in next(iter(report["results"].values()))["targets"]["BTCUSDT"]
        ),
        "source_dataset_digests": report["coverage"]["source_dataset_digests"],
        "report_digest": digest,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "report": str(json_path),
                "markdown": str(md_path),
                "manifest": str(manifest_path),
                "digest": digest,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
