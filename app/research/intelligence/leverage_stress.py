"""Gap-safe, offline leverage-stress research primitives.

This module is intentionally isolated from application composition.  It turns
public USD-M mark and index candles into completed five-minute basis rows; it
does not create a strategy, an order, or an execution dependency.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from itertools import pairwise

from app.research.intelligence.flow import FIVE_MINUTES_MS, ONE_MINUTE_MS


@dataclass(frozen=True, slots=True)
class PriceObservation:
    open_time_ms: int
    close_time_ms: int
    close: Decimal


@dataclass(frozen=True, slots=True)
class BasisObservation:
    timestamp_ms: int
    symbol: str
    mark_close: Decimal
    index_close: Decimal
    premium_bps: Decimal
    premium_change_5m: Decimal | None
    premium_change_15m: Decimal | None
    premium_change_1h: Decimal | None
    rolling_mean_bps: Decimal | None
    rolling_percentile: Decimal | None
    state: str | None


def _decimal(value: object) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError("invalid price close") from error
    if not result.is_finite() or result <= 0:
        raise ValueError("price close must be positive and finite")
    return result


def canonicalize_price_rows(rows: Iterable[Sequence[object]]) -> tuple[PriceObservation, ...]:
    """Parse archive kline rows, reject duplicate timestamps, then sort.

    Sorting establishes a deterministic representation; it never repairs a
    missing observation.  The public archive's first and seventh columns are
    open and close timestamps and the fifth column is the candle close.
    """
    parsed: list[PriceObservation] = []
    seen: set[int] = set()
    for row in rows:
        if len(row) < 7:
            raise ValueError("malformed price kline row")
        try:
            open_time, close_time = int(str(row[0])), int(str(row[6]))
        except ValueError as error:
            raise ValueError("invalid kline timestamp") from error
        if open_time in seen or close_time != open_time + ONE_MINUTE_MS - 1:
            raise ValueError("duplicate or invalid one-minute kline")
        seen.add(open_time)
        parsed.append(PriceObservation(open_time, close_time, _decimal(row[4])))
    if not parsed:
        raise ValueError("price archive contains no rows")
    return tuple(sorted(parsed, key=lambda item: item.open_time_ms))


def gaps_after(rows: Sequence[PriceObservation]) -> tuple[int, ...]:
    return tuple(
        later.open_time_ms
        for prior, later in pairwise(rows)
        if later.open_time_ms - prior.open_time_ms != ONE_MINUTE_MS
    )


def aggregate_completed_basis_5m(
    *, symbol: str, mark: Sequence[PriceObservation], index: Sequence[PriceObservation]
) -> tuple[BasisObservation, ...]:
    """Align one-minute closes and emit only fully completed five-minute bars."""
    if not symbol:
        raise ValueError("symbol is required")
    mark_by_time = {item.open_time_ms: item for item in mark}
    index_by_time = {item.open_time_ms: item for item in index}
    if len(mark_by_time) != len(mark) or len(index_by_time) != len(index):
        raise ValueError("duplicate price timestamps")
    shared = sorted(set(mark_by_time) & set(index_by_time))
    raw: list[tuple[int, Decimal, Decimal, Decimal]] = []
    for start in shared:
        if start % FIVE_MINUTES_MS:
            continue
        component_times = tuple(start + offset * ONE_MINUTE_MS for offset in range(5))
        if not all(time in mark_by_time and time in index_by_time for time in component_times):
            continue
        mark_close = mark_by_time[component_times[-1]].close
        index_close = index_by_time[component_times[-1]].close
        premium = (mark_close - index_close) / index_close * Decimal(10_000)
        # Flow observations use the completed bar's close timestamp (``...999``).
        raw.append(
            (mark_by_time[component_times[-1]].close_time_ms, mark_close, index_close, premium)
        )
    result: list[BasisObservation] = []
    premiums: list[Decimal] = []
    for position, (timestamp, mark_close, index_close, premium) in enumerate(raw):
        # A rolling value is unavailable after a gap: no cross-gap state.
        contiguous = position == 0 or timestamp - raw[position - 1][0] == FIVE_MINUTES_MS
        if not contiguous:
            premiums = []
        premiums.append(premium)
        window = premiums[-12:]
        percentile = (
            Decimal(sum(value <= premium for value in window)) / Decimal(len(window))
            if len(window) == 12
            else None
        )
        mean = sum(window, Decimal()) / Decimal(len(window)) if len(window) == 12 else None

        state = (
            "EXTREME_POSITIVE"
            if percentile is not None and percentile >= Decimal("0.95")
            else "EXTREME_NEGATIVE"
            if percentile is not None and percentile <= Decimal("0.05")
            else "POSITIVE"
            if premium > 0
            else "NEGATIVE"
            if premium < 0
            else "NEUTRAL"
        )
        result.append(
            BasisObservation(
                timestamp,
                symbol.upper(),
                mark_close,
                index_close,
                premium,
                _change(raw, position, premium, 1),
                _change(raw, position, premium, 3),
                _change(raw, position, premium, 12),
                mean,
                percentile,
                state,
            )
        )
    return tuple(result)


def _change(
    raw: Sequence[tuple[int, Decimal, Decimal, Decimal]], position: int, premium: Decimal, bars: int
) -> Decimal | None:
    if position < bars or not all(
        raw[n][0] - raw[n - 1][0] == FIVE_MINUTES_MS
        for n in range(position - bars + 1, position + 1)
    ):
        return None
    return premium - raw[position - bars][3]


def basis_digest(rows: Sequence[BasisObservation]) -> str:
    return hashlib.sha256(
        json.dumps(
            [asdict(row) for row in rows], default=str, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def basis_payload(rows: Sequence[BasisObservation]) -> dict[str, object]:
    return {
        "format": "chappie-leverage-stress-basis/v1",
        "digest": basis_digest(rows),
        "rows": json.loads(json.dumps([asdict(row) for row in rows], default=str)),
    }


def load_basis_payload(payload: object) -> tuple[BasisObservation, ...]:
    if not isinstance(payload, dict) or payload.get("format") != "chappie-leverage-stress-basis/v1":
        raise ValueError("unsupported basis dataset")
    raw = payload.get("rows")
    if not isinstance(raw, list):
        raise TypeError("basis rows missing")
    try:
        rows = tuple(
            BasisObservation(
                int(item["timestamp_ms"]),
                str(item["symbol"]),
                Decimal(str(item["mark_close"])),
                Decimal(str(item["index_close"])),
                Decimal(str(item["premium_bps"])),
                Decimal(str(item["premium_change_5m"]))
                if item["premium_change_5m"] is not None
                else None,
                Decimal(str(item["premium_change_15m"]))
                if item["premium_change_15m"] is not None
                else None,
                Decimal(str(item["premium_change_1h"]))
                if item["premium_change_1h"] is not None
                else None,
                Decimal(str(item["rolling_mean_bps"]))
                if item["rolling_mean_bps"] is not None
                else None,
                Decimal(str(item["rolling_percentile"]))
                if item["rolling_percentile"] is not None
                else None,
                str(item["state"]) if item["state"] is not None else None,
            )
            for item in raw
        )
    except (KeyError, ValueError, InvalidOperation, TypeError) as error:
        raise ValueError("malformed basis dataset") from error
    if tuple(sorted(row.timestamp_ms for row in rows)) != tuple(row.timestamp_ms for row in rows):
        raise ValueError("basis rows are not canonical")
    if basis_digest(rows) != payload.get("digest"):
        raise ValueError("basis digest does not match rows")
    return rows


def leverage_state(
    *, basis: BasisObservation, funding: Decimal | None, price_oi_state: str | None
) -> str | None:
    """Descriptive conjunction only; unavailable inputs stay unavailable."""
    if funding is None or price_oi_state is None or basis.premium_change_5m is None:
        return None
    if funding > 0 and basis.premium_bps > 0 and basis.premium_change_5m < 0:
        return "POSITIVE_FUNDING_PREMIUM_COMPRESSING"
    if funding > 0 and basis.premium_bps > 0 and price_oi_state == "PRICE_UP_OI_UP":
        return "POSITIVE_FUNDING_PREMIUM_OI_EXPANSION"
    if funding < 0 and basis.premium_bps < 0 and basis.premium_change_5m > 0:
        return "NEGATIVE_FUNDING_PREMIUM_NORMALIZING"
    if funding < 0 and basis.premium_bps < 0 and price_oi_state == "PRICE_DOWN_OI_UP":
        return "NEGATIVE_FUNDING_NEGATIVE_PREMIUM_OI_EXPANSION"
    return "BASIS_CONTEXT_OTHER"
