"""Causal aggregation helpers for offline multi-timeframe research strategies."""

from collections.abc import Sequence
from decimal import Decimal
from itertools import pairwise

from app.market.models import Candle


def synchronized(candles: Sequence[Candle]) -> bool:
    """Return whether the closed lower-timeframe stream has no discontinuity."""
    if len(candles) < 2:
        return True
    interval_ms = candles[0].close_time_ms - candles[0].open_time_ms + 1
    return all(
        candle.close_time_ms - candle.open_time_ms + 1 == interval_ms
        and candle.open_time_ms == previous.close_time_ms + 1
        for previous, candle in pairwise(candles)
    )


def completed_groups(candles: Sequence[Candle], bars_per_group: int) -> tuple[Candle, ...]:
    """Aggregate only complete lower-bar groups already present in ``candles``.

    A group closes at its final lower candle.  Therefore this helper cannot use
    an incomplete or future medium/higher-timeframe candle at the current lower
    timeframe timestamp.
    """
    usable = len(candles) - (len(candles) % bars_per_group)
    return tuple(
        Candle(
            open_time_ms=group[0].open_time_ms,
            close_time_ms=group[-1].close_time_ms,
            open=group[0].open,
            high=max(candle.high for candle in group),
            low=min(candle.low for candle in group),
            close=group[-1].close,
            volume=sum((candle.volume for candle in group), Decimal(0)),
        )
        for index in range(0, usable, bars_per_group)
        if (group := candles[index : index + bars_per_group])
    )
