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
