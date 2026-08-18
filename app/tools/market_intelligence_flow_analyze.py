"""Analyze local V3B flow/positioning datasets; no network or execution path."""

from __future__ import annotations

import argparse
import gzip
import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from app.market_intelligence_flow import (
    FlowObservation,
    conditional_lift,
    cross_asset_flow_state,
    flow_state,
    future_return_bps,
    load_flow_payload,
)
from app.tools.strategy_evaluate import write_report_atomic

HORIZONS = {"1h": 12, "3h": 36, "6h": 72, "12h": 144, "24h": 288}
THRESHOLDS = (20, 40, 60, 100, 150)


def _load(path: Path) -> tuple[FlowObservation, ...]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return load_flow_payload(json.load(handle))


def _funding_state(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if value <= Decimal("-0.0003"):
        return "NEGATIVE_EXTREME"
    if value < 0:
        return "NEGATIVE"
    if value < Decimal("0.0003"):
        return "POSITIVE"
    return "POSITIVE_EXTREME"


def _price_state(rows: tuple[FlowObservation, ...], index: int) -> str | None:
    if not index:
        return None
    return "PRICE_UP" if rows[index].close >= rows[index - 1].close else "PRICE_DOWN"


def _layer_states(rows: tuple[FlowObservation, ...], index: int) -> dict[str, str]:
    row = rows[index]
    price = _price_state(rows, index)
    output: dict[str, str] = {}
    if price:
        output["PRICE_ONLY"] = price
    if price and row.spot.signed_taker_quote_imbalance is not None:
        output["PRICE_SPOT_FLOW"] = (
            f"{price}_SPOT_BUY"
            if row.spot.signed_taker_quote_imbalance > Decimal("0.05")
            else f"{price}_SPOT_SELL"
        )
    if row.price_oi_state:
        output["PRICE_SPOT_FLOW_OI"] = row.price_oi_state
    flow = flow_state(row)
    if flow:
        output["PRICE_SPOT_FLOW_OI_FUTURES"] = flow
        if row.positioning_state and _funding_state(row.funding):
            output["PRICE_FLOW_POSITIONING_FUNDING"] = (
                f"{flow}|{row.positioning_state}|{_funding_state(row.funding)}"
            )
    return output


def _crosses_source_gap(start_ms: int, end_ms: int, gaps_after_ms: tuple[int, ...]) -> bool:
    return any(start_ms < gap <= end_ms for gap in gaps_after_ms)


def _tables(
    rows: tuple[FlowObservation, ...], *, gaps_after_ms: tuple[int, ...]
) -> dict[str, object]:
    output: dict[str, object] = {}
    for horizon, bars in HORIZONS.items():
        layers: dict[str, list[tuple[int, str, Decimal]]] = defaultdict(list)
        for index, row in enumerate(rows):
            future = future_return_bps(rows, index, bars)
            if future is None or _crosses_source_gap(
                row.timestamp_ms, row.timestamp_ms + bars * 300_000, gaps_after_ms
            ):
                continue
            for layer, state in _layer_states(rows, index).items():
                layers[layer].append((row.timestamp_ms, state, future))
        output[horizon] = {
            layer: {
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
            for layer, values in sorted(layers.items())
        }
    return output


def _cross_asset_tables(
    btc: tuple[FlowObservation, ...],
    eth: tuple[FlowObservation, ...],
    *,
    btc_gaps_after_ms: tuple[int, ...],
    eth_gaps_after_ms: tuple[int, ...],
) -> dict[str, object]:
    btc_by_time, eth_by_time = (
        {row.timestamp_ms: row for row in btc},
        {row.timestamp_ms: row for row in eth},
    )
    shared = tuple(sorted(set(btc_by_time) & set(eth_by_time)))
    output: dict[str, object] = {}
    for horizon, bars in HORIZONS.items():
        values: list[tuple[int, str, Decimal]] = []
        for index, timestamp in enumerate(shared):
            future_index = index + bars
            if future_index >= len(shared) or shared[future_index] != timestamp + bars * 300_000:
                continue
            if _crosses_source_gap(
                timestamp, shared[future_index], btc_gaps_after_ms
            ) or _crosses_source_gap(timestamp, shared[future_index], eth_gaps_after_ms):
                continue
            state = cross_asset_flow_state(btc_by_time[timestamp], eth_by_time[timestamp])
            if state is None:
                continue
            btc_return = btc_by_time[shared[future_index]].close / btc_by_time[timestamp].close - 1
            eth_return = eth_by_time[shared[future_index]].close / eth_by_time[timestamp].close - 1
            values.append((timestamp, state, (eth_return - btc_return) * Decimal(10_000)))
        output[horizon] = {
            "eth_outperforms_btc": {
                str(threshold): conditional_lift(values, threshold_bps=Decimal(threshold))
                for threshold in THRESHOLDS
            },
            "btc_outperforms_eth": {
                str(threshold): conditional_lift(
                    ((time, state, -value) for time, state, value in values),
                    threshold_bps=Decimal(threshold),
                )
                for threshold in THRESHOLDS
            },
        }
    return output


def _candidate_edges(
    tables: dict[str, object], *, minimum_samples: int = 500
) -> list[dict[str, object]]:
    """Conservative descriptive screen; it never calls an observation profitable."""
    candidates: list[dict[str, object]] = []
    for horizon, layers in tables.items():
        for layer, directions in layers.items():
            for direction, thresholds in directions.items():
                for threshold, study in thresholds.items():
                    states = study["states"]  # type: ignore[index]
                    for state, summary in states.items():
                        lift = summary["absolute_lift"]
                        if (
                            summary["sample_size"] < minimum_samples
                            or lift is None
                            or Decimal(lift) < Decimal("0.05")
                        ):
                            continue
                        annual = [
                            item.get(state) for item in study["years"].values() if item.get(state)
                        ]  # type: ignore[index]
                        if len(annual) < 2 or any(item["sample_size"] < 100 for item in annual):
                            continue
                        candidates.append(
                            {
                                "label": "CANDIDATE_FLOW_EDGE",
                                "horizon": horizon,
                                "layer": layer,
                                "direction": direction,
                                "threshold_bps": threshold,
                                "state": state,
                                "sample_size": summary["sample_size"],
                                "absolute_lift": lift,
                                "annual_samples": [item["sample_size"] for item in annual],
                                "note": "descriptive screen only; not a profitability claim",
                            }
                        )
    return candidates


def _markdown(payload: dict[str, object]) -> str:
    coverage = payload["coverage"]
    return "\n".join(
        [
            "# Market Intelligence v3B: flow and positioning research",
            "",
            "Research-only descriptive analysis. No strategy, execution, or capital-flow claim is made.",
            "",
            "## Coverage",
            "",
            f"- BTC rows: {coverage['BTCUSDT']['flow_rows']}",
            f"- ETH rows: {coverage['ETHUSDT']['flow_rows']}",
            "- Spot flow uses only completed 1m components; metrics are canonicalized and gaps stay unavailable.",
            "",
            "## Limitations",
            "",
            *[f"- {item}" for item in payload["limitations"]],
            "",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Analyze local V3B flow-positioning research datasets"
    )
    from app.research_paths import flow_read_root, results_dir

    parser.add_argument("--root", type=Path, default=flow_read_root())
    parser.add_argument("--output-dir", type=Path, default=results_dir())
    args = parser.parse_args(argv)
    btc = _load(args.root / "datasets" / "btcusdt_flow_5m.json.gz")
    eth = _load(args.root / "datasets" / "ethusdt_flow_5m.json.gz")
    source_manifest = json.loads((args.root / "source_manifest.json").read_text(encoding="utf-8"))
    sources = source_manifest.get("sources")
    if not isinstance(sources, dict):
        raise TypeError("source manifest is missing source coverage")

    def _gaps(symbol: str) -> tuple[int, ...]:
        item = sources.get(symbol)
        metrics = item.get("futures_metrics") if isinstance(item, dict) else None
        raw = metrics.get("gaps_after_ms") if isinstance(metrics, dict) else None
        if not isinstance(raw, list):
            raise TypeError(f"source manifest is missing metrics gaps for {symbol}")
        return tuple(int(value) for value in raw)

    btc_gaps, eth_gaps = _gaps("BTCUSDT"), _gaps("ETHUSDT")
    tables = {
        "BTCUSDT": _tables(btc, gaps_after_ms=btc_gaps),
        "ETHUSDT": _tables(eth, gaps_after_ms=eth_gaps),
    }
    output: dict[str, object] = {
        "format": "chappie-market-intelligence-v3b/v1",
        "coverage": {
            symbol: {
                "flow_rows": len(rows),
                "first_timestamp_ms": rows[0].timestamp_ms,
                "last_timestamp_ms": rows[-1].timestamp_ms,
            }
            for symbol, rows in (("BTCUSDT", btc), ("ETHUSDT", eth))
        },
        "source_integrity": source_manifest,
        "conditional_outcomes": tables,
        "cross_asset_flow": _cross_asset_tables(
            btc, eth, btc_gaps_after_ms=btc_gaps, eth_gaps_after_ms=eth_gaps
        ),
        "candidate_observations": _candidate_edges(tables["BTCUSDT"])
        + _candidate_edges(tables["ETHUSDT"]),
        "capital_flow_proxy_v2": {
            "meaning": "structured observable participation/positioning context, not literal dollar flow",
            "components": [
                "relative strength",
                "spot participation",
                "futures OI expansion/contraction",
                "futures taker long/short ratio",
                "positioning",
                "funding",
                "breadth where separately available",
            ],
            "states": [
                "BTC_FLOW_LEADERSHIP",
                "ETH_FLOW_LEADERSHIP",
                "BROAD_CRYPTO_INFLOW_PROXY",
                "BROAD_CRYPTO_OUTFLOW_PROXY",
                "MIXED_FLOW",
            ],
        },
        "limitations": [
            "All values are Binance-venue proxies, not direct observation of capital transfers.",
            "No source row is repaired, interpolated, or forward-filled across a metrics gap.",
            "Future returns are labels used only after feature construction; this report contains no strategy or execution rule.",
            "Candidate labels are descriptive screens and do not establish profitability or deployment suitability.",
        ],
    }
    manifest = {
        "format": "chappie-market-intelligence-v3b-manifest/v1",
        "dataset_root": str(args.root),
        "source_manifest": str(args.root / "source_manifest.json"),
        "result_format": output["format"],
    }
    write_report_atomic(args.output_dir / "market_intelligence_v3b.json", output)
    write_report_atomic(args.output_dir / "market_intelligence_v3b_manifest.json", manifest)
    (args.output_dir / "market_intelligence_v3b.md").write_text(_markdown(output), encoding="utf-8")
    print(f"wrote {args.output_dir / 'market_intelligence_v3b.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
