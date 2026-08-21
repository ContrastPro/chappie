from __future__ import annotations

from decimal import Decimal

import pytest

from app.research.intelligence.flow import FIVE_MINUTES_MS, ONE_MINUTE_MS
from app.research.intelligence.leverage_stress import (
    PriceObservation,
    aggregate_completed_basis_5m,
    basis_payload,
    canonicalize_price_rows,
    gaps_after,
    leverage_state,
    load_basis_payload,
)


def _prices(*, start: int = 0, count: int = 12, offset: str = "0"):
    return tuple(
        PriceObservation(
            start + index * ONE_MINUTE_MS,
            start + (index + 1) * ONE_MINUTE_MS - 1,
            Decimal(100) + Decimal(index) + Decimal(offset),
        )
        for index in range(count)
    )


def test_mark_index_alignment_derives_completed_five_minute_premium() -> None:
    rows = aggregate_completed_basis_5m(symbol="BTCUSDT", mark=_prices(offset="1"), index=_prices())
    assert [item.timestamp_ms for item in rows] == [FIVE_MINUTES_MS - 1, 2 * FIVE_MINUTES_MS - 1]
    assert rows[0].premium_bps == Decimal(10000) / Decimal(104)
    assert rows[1].premium_change_5m == rows[1].premium_bps - rows[0].premium_bps


def test_incomplete_or_gapped_components_are_not_aggregated_or_filled() -> None:
    mark = _prices(count=10)
    index = tuple(item for item in _prices(count=10) if item.open_time_ms != 7 * ONE_MINUTE_MS)
    rows = aggregate_completed_basis_5m(symbol="BTCUSDT", mark=mark, index=index)
    assert len(rows) == 1
    assert gaps_after(index) == (8 * ONE_MINUTE_MS,)


def test_archive_rows_reject_duplicates_and_bad_close_boundary() -> None:
    row = ("0", "1", "1", "1", "1", "0", "59999")
    with pytest.raises(ValueError, match="duplicate"):
        canonicalize_price_rows((row, row))
    with pytest.raises(ValueError, match="invalid"):
        canonicalize_price_rows((("0", "1", "1", "1", "1", "0", "1"),))


def test_percentile_is_causal_and_payload_is_deterministic() -> None:
    rows = aggregate_completed_basis_5m(
        symbol="BTCUSDT", mark=_prices(count=65, offset="1"), index=_prices(count=65)
    )
    assert rows[10].rolling_percentile is None
    assert rows[11].rolling_percentile is not None
    payload = basis_payload(rows)
    assert payload["format"] == "chappie-leverage-stress-basis/v1"
    assert load_basis_payload(payload) == rows
    changed = list(rows)
    changed[-1] = PriceObservation(0, 0, Decimal(1))  # type: ignore[list-item]
    assert (
        rows[11]
        == aggregate_completed_basis_5m(
            symbol="BTCUSDT", mark=_prices(count=65, offset="1"), index=_prices(count=65)
        )[11]
    )


def test_basis_funding_oi_conjunction_is_descriptive_and_requires_inputs() -> None:
    row = aggregate_completed_basis_5m(
        symbol="BTCUSDT", mark=_prices(count=20, offset="1"), index=_prices(count=20)
    )[-1]
    assert leverage_state(basis=row, funding=None, price_oi_state="PRICE_UP_OI_UP") is None
    assert leverage_state(basis=row, funding=Decimal("0.001"), price_oi_state="PRICE_UP_OI_UP") in {
        "POSITIVE_FUNDING_PREMIUM_COMPRESSING",
        "POSITIVE_FUNDING_PREMIUM_OI_EXPANSION",
        "BASIS_CONTEXT_OTHER",
    }
