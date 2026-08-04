import time
from decimal import Decimal

from app.market.exceptions import (
    CandleSequenceGapError,
    CandleSequenceValidationError,
    CandleValidationError,
    IncompleteCandleSequenceError,
    MarketDataValidationError,
)
from app.market.intervals import next_binance_spot_open_time_ms
from app.market.models import Candle, MarketStreamKey

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
    stream_key: MarketStreamKey | None = None,
) -> None:
    """Validate candle values, ordering, continuity, ranges, and completeness."""

    if not candles:
        raise CandleSequenceValidationError("Candle sequence must not be empty")

    next_binance_spot_open_time_ms(candles[0].open_time_ms, interval)
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

        expected_open_time_ms = next_binance_spot_open_time_ms(
            previous.open_time_ms,
            interval,
        )
        if candle.open_time_ms != expected_open_time_ms:
            raise CandleSequenceGapError(
                stream_key=(
                    stream_key
                    if stream_key is not None
                    else MarketStreamKey("UNKNOWN", interval)
                ),
                previous_index=index - 1,
                current_index=index,
                previous_open_time_ms=previous.open_time_ms,
                previous_close_time_ms=previous.close_time_ms,
                expected_open_time_ms=expected_open_time_ms,
                received_open_time_ms=candle.open_time_ms,
                interval=interval,
            )

        if candle.open_time_ms <= previous.close_time_ms:
            raise CandleSequenceValidationError(
                f"Candle time ranges overlap at index {index}"
            )

    incomplete_indices = [
        index
        for index, candle in enumerate(candles)
        if candle.close_time_ms >= reference_time_ms
    ]
    invalid_index: int | None = None
    only_last_allowed = allow_incomplete_last
    if incomplete_indices:
        if incomplete_indices[:-1] or incomplete_indices[-1] != len(candles) - 1:
            invalid_index = incomplete_indices[0]
        elif not allow_incomplete_last:
            invalid_index = incomplete_indices[-1]
    if invalid_index is not None:
        invalid = candles[invalid_index]
        latest = candles[-1]
        raise IncompleteCandleSequenceError(
            invalid_index=invalid_index,
            invalid_open_time_ms=invalid.open_time_ms,
            invalid_close_time_ms=invalid.close_time_ms,
            latest_open_time_ms=latest.open_time_ms,
            latest_close_time_ms=latest.close_time_ms,
            incomplete_candle_count=len(incomplete_indices),
            observed_at_ms=reference_time_ms,
            only_last_allowed=only_last_allowed,
        )


def diagnose_candle_sequence(
    candles: list[Candle],
    interval: str,
    *,
    allow_incomplete_last: bool,
    current_time_ms: int | None = None,
    stream_key: MarketStreamKey | None = None,
) -> MarketDataValidationError | None:
    """Return the first validation error without retaining or logging the batch."""

    try:
        validate_candle_sequence(
            candles,
            interval,
            allow_incomplete_last=allow_incomplete_last,
            current_time_ms=current_time_ms,
            stream_key=stream_key,
        )
    except MarketDataValidationError as error:
        return error
    return None
