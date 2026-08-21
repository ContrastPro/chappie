"""Analyze local immutable spot datasets for descriptive capital rotation."""

from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path

from app.market.intervals import fixed_binance_spot_interval_ms
from app.research.intelligence.capital_rotation import (
    ASSETS,
    RELATIVE_PAIRS,
    STATES,
    capital_flow_proxy,
    direct_pair_cross_check,
    funding_conditioned_outcomes,
    leadership_outcomes,
    rotation_observations,
    transition_matrix,
    volume_breadth_outcomes,
)
from app.research.intelligence.market import load_context_payload
from app.research.paths import results_dir
from app.research.strategy.history import load_dataset
from app.tools.strategy_evaluate import write_report_atomic


def _named_paths(items: list[str], *, expected: set[str]) -> dict[str, tuple[Path, ...]]:
    result: dict[str, tuple[Path, ...]] = {}
    for item in items:
        name, separator, raw_path = item.partition("=")
        paths = tuple(Path(path) for path in raw_path.split(",") if path)
        if not separator or name not in expected or not paths or name in result:
            raise ValueError(f"expected unique NAME=PATH entries for {sorted(expected)}")
        result[name] = paths
    if set(result) != expected:
        raise ValueError(f"missing required datasets: {sorted(expected - set(result))}")
    return result


def _observation_payload(item):
    return {
        "timestamp_ms": item.timestamp_ms,
        "close_prices": {key: str(value) for key, value in item.close_prices.items()},
        "absolute_returns_bps": {
            key: str(value) for key, value in item.absolute_returns_bps.items()
        },
        "relative_returns_bps": {
            key: str(value) for key, value in item.relative_returns_bps.items()
        },
        "relative_volume": {key: str(value) for key, value in item.relative_volume.items()},
        "breadth_positive": item.breadth_positive,
        "breadth_outperforming_btc": item.breadth_outperforming_btc,
        "median_return_bps": str(item.median_return_bps),
        "dispersion_bps": str(item.dispersion_bps),
        "leader": item.leader,
        "laggard": item.laggard,
        "state": item.state,
    }


def _candidate_edges(observations, *, bars: int, interval_ms: int) -> list[dict[str, object]]:
    """Small transparent discovery screen, never a strategy recommendation."""
    result: list[dict[str, object]] = []
    for asset in ASSETS[1:]:
        samples = []
        baseline = []
        for current, future in zip(observations[:-bars], observations[bars:], strict=True):
            if future.timestamp_ms - current.timestamp_ms != bars * interval_ms:
                continue
            value = (future.close_prices[asset] / current.close_prices[asset] - 1) * Decimal(
                10_000
            ) - (future.close_prices["BTC"] / current.close_prices["BTC"] - 1) * Decimal(10_000)
            baseline.append(value)
            if current.leader == asset and current.relative_volume[asset] >= Decimal(1):
                samples.append(value)
        if len(samples) < 100 or not baseline:
            continue
        probability = Decimal(sum(value >= 60 for value in samples)) / Decimal(len(samples))
        base = Decimal(sum(value >= 60 for value in baseline)) / Decimal(len(baseline))
        # Require a material, interpretable lift; this is evidence discovery only.
        if probability - base >= Decimal("0.05"):
            result.append(
                {
                    "label": "CANDIDATE_ROTATION_EDGE",
                    "condition": f"{asset}_LEADER_WITH_RELATIVE_VOLUME_EXPANSION",
                    "horizon_bars": bars,
                    "samples": len(samples),
                    "outperform_btc_by_60bps_probability": str(probability),
                    "same_period_baseline_probability": str(base),
                    "absolute_lift": str(probability - base),
                    "warning": "research observation only; not profitability or a trading signal",
                }
            )
    return result


def _markdown(output: dict[str, object]) -> str:
    coverage = output["coverage"]
    transitions = output["transition_matrices"]
    lines = [
        "# Capital rotation v1",
        "",
        "Offline-only descriptive research. `CAPITAL_FLOW_PROXY` describes observable leadership and does not measure literal money flows or create a trading signal.",
        "",
        "## Coverage",
        "",
        f"- Interval: {coverage['interval']}",
        f"- Aligned observations: {coverage['aligned_observations']}",
        f"- Overlap: {coverage['first_timestamp_ms']} through {coverage['last_timestamp_ms']}",
        "",
        "## Transition matrices",
        "",
    ]
    for horizon, matrix in transitions.items():
        lines.extend(
            [
                f"### {horizon}",
                "",
                "| Current | Samples | Future-state probabilities |",
                "|---|---:|---|",
            ]
        )
        for state in STATES:
            row = matrix[state]
            probabilities = ", ".join(
                f"{target}={value}"
                for target, value in row["probabilities"].items()
                if value is not None
            )
            lines.append(f"| {state} | {row['count']} | {probabilities} |")
        lines.append("")
    lines.extend(
        [
            "## Limitations",
            "",
            *[f"- {item}" for item in output["limitations"]],
            "",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline-only causal capital-rotation research")
    parser.add_argument("--spot", action="append", required=True, metavar="SYMBOL=PATH")
    parser.add_argument("--direct", action="append", default=[], metavar="SYMBOL=PATH")
    parser.add_argument("--funding", type=Path, help="optional verified BTCUSDT funding dataset")
    parser.add_argument("--output-dir", type=Path, default=results_dir())
    parser.add_argument("--lookback-bars", type=int, default=6)
    args = parser.parse_args(argv)
    spot_paths = _named_paths(args.spot, expected={f"{asset}USDT" for asset in ASSETS})
    direct_paths = (
        _named_paths(args.direct, expected=set(RELATIVE_PAIRS.values())) if args.direct else {}
    )
    spot_datasets = {
        symbol: tuple(load_dataset(path) for path in paths) for symbol, paths in spot_paths.items()
    }
    intervals = {
        dataset.metadata.interval for datasets in spot_datasets.values() for dataset in datasets
    }
    if len(intervals) != 1:
        raise ValueError("all spot datasets must have the same interval")
    interval = intervals.pop()
    spot = {
        symbol.removesuffix("USDT"): tuple(
            candle for dataset in datasets for candle in dataset.candles
        )
        for symbol, datasets in spot_datasets.items()
    }
    observations = rotation_observations(
        spot, interval_ms=fixed_binance_spot_interval_ms(interval), lookback_bars=args.lookback_bars
    )
    if not observations:
        raise ValueError("no contiguous fully aligned observation range")
    direct = {
        symbol: tuple(candle for path in paths for candle in load_dataset(path).candles)
        for symbol, paths in direct_paths.items()
    }
    funding = (
        load_context_payload(json.loads(args.funding.read_text(encoding="utf-8")))
        if args.funding
        else None
    )
    horizon_bars = {"1h": 1, "3h": 3, "6h": 6, "12h": 12, "24h": 24}
    if interval == "15m":
        horizon_bars = {name: value * 4 for name, value in horizon_bars.items()}
    output = {
        "format": "chappie-capital-rotation/v1",
        "coverage": {
            "interval": interval,
            "assets": list(ASSETS),
            "aligned_observations": len(observations),
            "first_timestamp_ms": observations[0].timestamp_ms,
            "last_timestamp_ms": observations[-1].timestamp_ms,
            "datasets": {
                symbol: {
                    "digests": [dataset.metadata.candles_sha256 for dataset in datasets],
                    "candles": sum(len(dataset.candles) for dataset in datasets),
                }
                for symbol, datasets in spot_datasets.items()
            },
        },
        "integrity": {
            "policy": "validated immutable inputs; intersection only; no repair, interpolation, forward-fill, or calculation across gaps",
            "direct_pair_cross_check": direct_pair_cross_check(
                spot,
                direct,
                interval_ms=fixed_binance_spot_interval_ms(interval),
                bars=args.lookback_bars,
            )
            if direct
            else {"status": "DIRECT_PAIR_DATA_UNAVAILABLE"},
        },
        "rotation_state_definitions": {
            "BROAD_RISK_ON": "all four absolute returns are positive over the causal lookback",
            "BROAD_RISK_OFF": "all four absolute returns are negative over the causal lookback",
            "BTC_LEADERSHIP": "BTC has the greatest relative return and all alts are at or below BTC",
            "ETH_LEADERSHIP": "ETH has the greatest positive return relative to BTC",
            "ALT_LEADERSHIP": "SOL or BNB has the greatest positive return relative to BTC",
            "MIXED": "none of the preceding transparent descriptions applies",
        },
        "capital_flow_proxy": capital_flow_proxy(observations[-1]),
        "latest_observation": _observation_payload(observations[-1]),
        "transition_matrices": {
            name: transition_matrix(observations, bars=bars) for name, bars in horizon_bars.items()
        },
        "leadership_persistence": {
            name: leadership_outcomes(observations, bars=bars)
            for name, bars in horizon_bars.items()
        },
        "volume_breadth_conditioned_12h": volume_breadth_outcomes(
            observations, bars=horizon_bars["12h"]
        ),
        "funding_conditioned_12h": (
            funding_conditioned_outcomes(observations, funding, bars=horizon_bars["12h"])
            if funding
            else {"status": "FUNDING_DATA_UNAVAILABLE"}
        ),
        "candidate_rotation_edges": _candidate_edges(
            observations,
            bars=horizon_bars["12h"],
            interval_ms=fixed_binance_spot_interval_ms(interval),
        ),
        "limitations": [
            "CAPITAL_FLOW_PROXY measures observable market leadership, not literal money transfers.",
            "Labels use completed candles only; future returns are outcomes, never features.",
            "Open interest remains unavailable unless a reliable public historical source is supplied; it is never fabricated.",
            "Candidate observations are not strategies, profitability claims, or execution authorization.",
        ],
    }
    manifest = {
        "format": "chappie-capital-rotation-manifest/v1",
        "inputs": output["coverage"]["datasets"],
        "interval": interval,
        "lookback_bars": args.lookback_bars,
        "direct_pairs": sorted(direct_paths),
        "integrity_policy": output["integrity"]["policy"],
    }
    write_report_atomic(args.output_dir / "capital_rotation_v1_manifest.json", manifest)
    write_report_atomic(args.output_dir / "capital_rotation_v1.json", output)
    (args.output_dir / "capital_rotation_v1.md").parent.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "capital_rotation_v1.md").write_text(_markdown(output), encoding="utf-8")
    print(f"wrote {args.output_dir / 'capital_rotation_v1.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
