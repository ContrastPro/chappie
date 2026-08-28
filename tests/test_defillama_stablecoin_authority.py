from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.research.capital_flow.defillama_stablecoin_authority import (
    DEFILLAMA_FILTER_DOCUMENTATION_STATUS,
    DEFILLAMA_STABLECOIN_ASSETS,
    filter_evidence_from_prefix,
    parse_bounded_defillama_stablecoin_json,
)


def _record(timestamp: str, usd: str = "10", circulating: str = "10") -> str:
    return (
        '{"date":"'
        + timestamp
        + '","totalCirculating":{"peggedUSD":'
        + circulating
        + '},"totalCirculatingUSD":{"peggedUSD":'
        + usd
        + "}}"
    )


def _evidence(asset_id: int, symbol: str, peers: set[str] | None = None):
    return filter_evidence_from_prefix(asset_id, symbol, ({"date": str(asset_id)},), peers or set())


@pytest.mark.parametrize("asset_id,symbol", DEFILLAMA_STABLECOIN_ASSETS)
def test_exact_assets_schema_decimal_and_observed_filter_contract(
    asset_id: int, symbol: str
) -> None:
    result = parse_bounded_defillama_stablecoin_json(
        ["[", _record("1693526400", "10.50"), "]"],
        asset_id=asset_id,
        symbol=symbol,
        filter_evidence=_evidence(asset_id, symbol),
    )
    assert result.documentation_status == DEFILLAMA_FILTER_DOCUMENTATION_STATUS
    assert result.rows[0].market_cap_usd == Decimal("10.50")
    assert result.rows[0].reference_timestamp_utc == datetime(2023, 9, 1, tzinfo=UTC)


def test_filter_evidence_rejects_ignored_same_response() -> None:
    evidence = _evidence(1, "USDT")
    with pytest.raises(ValueError, match="FILTER_SEMANTICS_CHANGED"):
        filter_evidence_from_prefix(2, "USDC", ({"date": "1"},), {evidence.response_fingerprint})
    with pytest.raises(ValueError, match="FILTER_SEMANTICS_CHANGED"):
        parse_bounded_defillama_stablecoin_json(
            ["[]"], asset_id=2, symbol="USDC", filter_evidence=evidence
        )


@pytest.mark.parametrize("value", ["-1", "NaN", "Infinity", "-Infinity"])
def test_invalid_usd_values_reject(value: str) -> None:
    with pytest.raises(ValueError, match="USD"):
        parse_bounded_defillama_stablecoin_json(
            ["[", _record("1693526400", value), "]"],
            asset_id=1,
            symbol="USDT",
            filter_evidence=_evidence(1, "USDT"),
        )


def test_schema_timestamp_order_and_duplicates() -> None:
    evidence = _evidence(1, "USDT")
    with pytest.raises(ValueError, match="schema"):
        parse_bounded_defillama_stablecoin_json(
            ['[{"totalCirculating":{},"date":"1693526400"}]'],
            asset_id=1,
            symbol="USDT",
            filter_evidence=evidence,
        )
    with pytest.raises(ValueError, match="timestamp"):
        parse_bounded_defillama_stablecoin_json(
            ["[", _record("bad"), "]"], asset_id=1, symbol="USDT", filter_evidence=evidence
        )
    with pytest.raises(ValueError, match="SOURCE_ORDER_INVALID"):
        parse_bounded_defillama_stablecoin_json(
            ["[", _record("1693612800"), ",", _record("1693526400"), "]"],
            asset_id=1,
            symbol="USDT",
            filter_evidence=evidence,
        )
    result = parse_bounded_defillama_stablecoin_json(
        ["[", _record("1693526400"), ",", _record("1693526400"), "]"],
        asset_id=1,
        symbol="USDT",
        filter_evidence=evidence,
    )
    assert result.quality.identical_duplicate_count == 1
    with pytest.raises(ValueError, match="conflicting"):
        parse_bounded_defillama_stablecoin_json(
            ["[", _record("1693526400"), ",", _record("1693526400", "11"), "]"],
            asset_id=1,
            symbol="USDT",
            filter_evidence=evidence,
        )


def test_warmup_cutoff_does_not_parse_future_value_or_next_object() -> None:
    class Guarded:
        def __iter__(self):
            yield "["
            yield _record("1693440000")
            yield ","
            yield _record("1693526400", "11")
            yield ","
            yield '{"date":"1780358400","totalCirculating":{"peggedUSD":"SECRET"}'
            raise AssertionError("second future object requested")

    result = parse_bounded_defillama_stablecoin_json(
        Guarded(), asset_id=1, symbol="USDT", filter_evidence=_evidence(1, "USDT")
    )
    assert [item.market_cap_usd for item in result.rows] == [Decimal(11)]
    assert result.quality.cutoff_crossing_timestamp_utc == datetime(2026, 6, 2, tzinfo=UTC)
    assert result.quality.objects_seen_after_cutoff == 1


def test_repeated_parse_is_deterministic_and_no_absence_is_manufactured() -> None:
    payload = ["[", _record("1693526400"), ",", _record("1693699200", "12"), "]"]
    first = parse_bounded_defillama_stablecoin_json(
        payload, asset_id=5, symbol="DAI", filter_evidence=_evidence(5, "DAI")
    )
    second = parse_bounded_defillama_stablecoin_json(
        payload, asset_id=5, symbol="DAI", filter_evidence=_evidence(5, "DAI")
    )
    assert first == second
    assert len(first.rows) == 2
