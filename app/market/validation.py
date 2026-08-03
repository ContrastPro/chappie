import time
from datetime import UTC, datetime
from decimal import Decimal

from app.market.exceptions import (
    CandleSequenceValidationError,
    CandleValidationError,
)
from app.market.intervals import (
    BINANCE_SPOT_CALENDAR_INTERVALS,
    fixed_binance_spot_interval_ms,
)
from app.market.models import Candle

ZERO = Decimal(0)


def validate_candle(candle: Candle) -> None:
    """Validate invariants of one normalized candle."""

    prices = {
        "open": candle.open,
        "high": candle.high,
        "low": candle.low,
        "close": candle.close,
    }

    for field_name, value in prices.items():
        if not value.is_finite() or value <= ZERO:
            raise CandleValidationError(f"Candle {field_name} must be positive")

    if not candle.volume.is_finite() or candle.volume < ZERO:
        raise CandleValidationError("Candle volume must not be negative")

    if candle.low > min(candle.open, candle.close, candle.high):
        raise CandleValidationError(
            "Candle low must not be above open, close, or high"
        )

    if candle.high < max(candle.open, candle.close, candle.low):
        raise CandleValidationError(
            "Candle high must not be below open, close, or low"
        )

    if candle.open_time_ms >= candle.close_time_ms:
        raise CandleValidationError(
            "Candle open_time_ms must be less than close_time_ms"
        )


def validate_candle_sequence(
    candles: list[Candle],
    interval: str,
    *,
    allow_incomplete_last: bool,
    current_time_ms: int | None = None,
) -> None:
    """Validate candle values, ordering, continuity, ranges, and completeness."""

    if not candles:
        raise CandleSequenceValidationError("Candle sequence must not be empty")

    interval_ms = (
        None
        if interval in BINANCE_SPOT_CALENDAR_INTERVALS
        else fixed_binance_spot_interval_ms(interval)
    )
    reference_time_ms = (
        time.time_ns() // 1_000_000
        if current_time_ms is None
        else current_time_ms
    )

    if not isinstance(reference_time_ms, int) or isinstance(reference_time_ms, bool):
        raise TypeError("current_time_ms must be an integer")

    for candle in candles:
        validate_candle(candle)

    for index, candle in enumerate(candles[1:], start=1):
        previous = candles[index - 1]

        if candle.open_time_ms == previous.open_time_ms:
            raise CandleSequenceValidationError(
                f"Duplicate candle open_time_ms at index {index}"
            )

        if candle.open_time_ms < previous.open_time_ms:
            raise CandleSequenceValidationError(
                f"Candles are not strictly ordered at index {index}"
            )

        if not _has_expected_open_time(previous, candle, interval, interval_ms):
            raise CandleSequenceValidationError(
                f"Unexpected candle interval at index {index}"
            )

        if candle.open_time_ms <= previous.close_time_ms:
            raise CandleSequenceValidationError(
                f"Candle time ranges overlap at index {index}"
            )

    for index, candle in enumerate(candles[:-1]):
        if candle.close_time_ms >= reference_time_ms:
            raise CandleSequenceValidationError(
                f"Only the last candle may be incomplete; invalid candle at index {index}"
            )

    if candles[-1].close_time_ms >= reference_time_ms and not allow_incomplete_last:
        raise CandleSequenceValidationError("The last candle is incomplete")


def _has_expected_open_time(
    previous: Candle,
    candle: Candle,
    interval: str,
    interval_ms: int | None,
) -> bool:
    if interval_ms is not None:
        return candle.open_time_ms - previous.open_time_ms == interval_ms

    if interval == "1M":
        return candle.open_time_ms == _next_utc_month_start_ms(previous.open_time_ms)

    return False


def _next_utc_month_start_ms(open_time_ms: int) -> int:
    open_time = datetime.fromtimestamp(open_time_ms / 1_000, tz=UTC)

    if open_time.month == 12:
        next_month = datetime(open_time.year + 1, 1, 1, tzinfo=UTC)
    else:
        next_month = datetime(open_time.year, open_time.month + 1, 1, tzinfo=UTC)

    return int(next_month.timestamp() * 1_000)
