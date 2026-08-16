"""Small exact-Decimal indicators shared by pure research strategies."""

from collections.abc import Sequence
from decimal import Decimal, localcontext

from app.trading.exceptions import InvalidStrategyConfigurationError, StrategyInvariantError


def rolling_mean(values: Sequence[Decimal], window: int) -> Decimal:
    if not isinstance(window, int) or isinstance(window, bool) or window < 2:
        raise InvalidStrategyConfigurationError("rolling window must be at least 2")
    if len(values) < window:
        raise StrategyInvariantError("not enough values for rolling mean")
    with localcontext() as context:
        context.prec = 28
        return sum(values[-window:], Decimal(0)) / Decimal(window)


def true_range(high: Decimal, low: Decimal, previous_close: Decimal) -> Decimal:
    return max(high - low, abs(high - previous_close), abs(low - previous_close))


def average_true_range(
    highs: Sequence[Decimal], lows: Sequence[Decimal], closes: Sequence[Decimal], window: int
) -> Decimal:
    if len(highs) != len(lows) or len(lows) != len(closes):
        raise StrategyInvariantError("OHLC sequences must have matching lengths")
    if len(closes) < window + 1:
        raise StrategyInvariantError("not enough values for ATR")
    ranges = [
        true_range(highs[index], lows[index], closes[index - 1]) for index in range(1, len(closes))
    ]
    with localcontext() as context:
        context.prec = 28
        return sum(ranges[-window:], Decimal(0)) / Decimal(window)


def simple_regime(
    highs: Sequence[Decimal], lows: Sequence[Decimal], closes: Sequence[Decimal]
) -> str:
    """Deterministic report-only market-regime classification shared by research strategies."""
    if len(highs) != len(lows) or len(lows) != len(closes):
        raise StrategyInvariantError("OHLC sequences must have matching lengths")
    # Preserve the original report-only label's warm-up behavior: index 14
    # was the first classified candle, hence 15 closed candles are required.
    if len(closes) < 15:
        return "UNKNOWN"
    window_highs, window_lows, window_closes = highs[-14:], lows[-14:], closes[-14:]
    close = window_closes[-1]
    if close <= 0:
        raise StrategyInvariantError("close must be positive")
    range_bps = (max(window_highs) - min(window_lows)) / close * Decimal(10_000)
    trend_bps = abs(window_closes[-1] - window_closes[0]) / close * Decimal(10_000)
    if range_bps >= Decimal(250):
        return "HIGH_VOLATILITY"
    return "TREND" if trend_bps >= Decimal(60) else "RANGE"
