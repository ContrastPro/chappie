"""Produce offline descriptive BTC derivatives/cross-asset studies."""

from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path

from app.market_intelligence import (
    FUNDING_MAX_AGE_MS,
    OI_MAX_AGE_MS,
    asof_observation,
    completed_candle_asof,
    conditional_study,
    context_digest,
    load_context_payload,
    price_oi_state,
)
from app.strategy_history import load_dataset
from app.tools.strategy_evaluate import write_report_atomic


def _load_context(path: Path):
    return load_context_payload(json.loads(path.read_text(encoding="utf-8")))


def _markdown(payload: dict[str, object]) -> str:
    return "\n".join(
        [
            "# Market intelligence v1",
            "",
            "Research-only descriptive conditional studies.",
            "",
            "## Limitations",
            "",
            *[f"- {item}" for item in payload["limitations"]],
            "",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analyze local causal market-intelligence inputs")
    parser.add_argument("--btc", type=Path, required=True)
    parser.add_argument("--ethusdt", type=Path, required=True)
    parser.add_argument("--ethbtc", type=Path, required=True)
    parser.add_argument("--funding", type=Path, required=True)
    parser.add_argument("--open-interest", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("data/research/results"))
    args = parser.parse_args(argv)
    btc, ethusdt, ethbtc = (
        load_dataset(args.btc),
        load_dataset(args.ethusdt),
        load_dataset(args.ethbtc),
    )
    funding = _load_context(args.funding)
    open_interest = _load_context(args.open_interest) if args.open_interest else None
    rows: list[tuple[int, str, Decimal]] = []
    eth_rows: list[tuple[int, str, Decimal]] = []
    closes = tuple(item.close for item in btc.candles)
    for index, candle in enumerate(btc.candles[:-6]):
        # At candle close T, only completed spot candles and published futures observations are eligible.
        oi = (
            asof_observation(
                open_interest.observations,
                available_at_ms=candle.close_time_ms,
                max_age_ms=OI_MAX_AGE_MS,
            )
            if open_interest
            else None
        )
        funding_item = asof_observation(
            funding.observations,
            available_at_ms=candle.close_time_ms,
            max_age_ms=FUNDING_MAX_AGE_MS,
        )
        label = (closes[index + 6] - candle.close) / candle.close * Decimal(10_000)
        if oi is not None and index:
            prior = (
                asof_observation(
                    open_interest.observations,
                    available_at_ms=btc.candles[index - 1].close_time_ms,
                    max_age_ms=OI_MAX_AGE_MS,
                )
                if open_interest
                else None
            )
            if prior is not None:
                rows.append(
                    (
                        candle.close_time_ms,
                        price_oi_state(candle.close, closes[index - 1], oi.value, prior.value),
                        label,
                    )
                )
        if funding_item is not None:
            rows.append(
                (
                    candle.close_time_ms,
                    "funding_positive" if funding_item.value >= 0 else "funding_negative",
                    label,
                )
            )
        eth = completed_candle_asof(ethbtc.candles, candle.close_time_ms)
        if eth is not None and index:
            previous = completed_candle_asof(ethbtc.candles, btc.candles[index - 1].close_time_ms)
            if previous is not None:
                eth_rows.append(
                    (
                        candle.close_time_ms,
                        "ethbtc_strengthening"
                        if eth.close >= previous.close
                        else "ethbtc_weakening",
                        label,
                    )
                )
    output = {
        "format": "chappie-market-intelligence/v1",
        "btc_interval": btc.metadata.interval,
        "sources": {
            "btc": btc.metadata.source,
            "ethusdt": ethusdt.metadata.source,
            "ethbtc": ethbtc.metadata.source,
            "funding": funding.source,
            "open_interest": open_interest.source if open_interest else None,
        },
        "coverage": {
            "btc_candles": len(btc.candles),
            "ethusdt_candles": len(ethusdt.candles),
            "ethbtc_candles": len(ethbtc.candles),
            "funding_observations": len(funding.observations),
            "open_interest_observations": len(open_interest.observations) if open_interest else 0,
        },
        "asof_semantics": {
            "spot": "only candles closed at or before BTC T",
            "funding_max_age_ms": FUNDING_MAX_AGE_MS,
            "open_interest_max_age_ms": OI_MAX_AGE_MS,
            "no_arbitrary_forward_fill": True,
        },
        "conditional_6h_outcomes": {
            "derivatives": {
                str(threshold): conditional_study(rows, threshold_bps=Decimal(threshold))
                for threshold in (20, 40, 60, 100, 150)
            },
            "ethbtc": {
                str(threshold): conditional_study(eth_rows, threshold_bps=Decimal(threshold))
                for threshold in (20, 40, 60, 100, 150)
            },
        },
        "limitations": [
            "Open-interest history is limited by the public Binance USD-M endpoint retention and is explicitly optional.",
            "Funding/OI and ETH features are descriptive states, not trading signals.",
            "All labels are future BTC returns and are excluded from feature construction.",
        ],
    }
    manifest = {
        "format": "chappie-market-intelligence-manifest/v1",
        "datasets": {
            "btc": {
                "symbol": btc.metadata.symbol,
                "interval": btc.metadata.interval,
                "digest": btc.metadata.candles_sha256,
            },
            "ethusdt": {
                "symbol": ethusdt.metadata.symbol,
                "interval": ethusdt.metadata.interval,
                "digest": ethusdt.metadata.candles_sha256,
            },
            "ethbtc": {
                "symbol": ethbtc.metadata.symbol,
                "interval": ethbtc.metadata.interval,
                "digest": ethbtc.metadata.candles_sha256,
            },
            "funding": {"kind": funding.kind, "digest": context_digest(funding)},
            "open_interest": (
                {"kind": open_interest.kind, "digest": context_digest(open_interest)}
                if open_interest
                else None
            ),
        },
        "valid_overlap": {
            "first_open_ms": btc.candles[0].open_time_ms,
            "last_open_ms": btc.candles[-1].open_time_ms,
        },
        "source_gap_policy": "optional sources remain unavailable; no arbitrary forward fill or cross-gap calculation",
    }
    write_report_atomic(args.output_dir / "market_intelligence_v1_manifest.json", manifest)
    write_report_atomic(args.output_dir / "market_intelligence_v1.json", output)
    markdown = args.output_dir / "market_intelligence_v1.md"
    markdown.parent.mkdir(parents=True, exist_ok=True)
    markdown.write_text(_markdown(output), encoding="utf-8")
    print(f"wrote {args.output_dir / 'market_intelligence_v1.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
