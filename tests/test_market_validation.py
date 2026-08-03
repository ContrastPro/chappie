from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.market.exceptions import (
    CandleSequenceValidationError,
    CandleValidationError,
    UnsupportedMarketIntervalError,
)
from app.market.models import Candle
from app.market.validation import validate_candle, validate_candle_sequence

OPEN_TIME_MS = 1_700_000_000_000
INTERVAL_MS = 60_000


def make_candle(
    *,
    open_time_ms: int = OPEN_TIME_MS,
    close_time_ms: int | None = None,
    open_price: str = "100",
    high: str = "110",
    low: str = "90",
    close: str = "105",
    volume: str = "10",
) -> Candle:
    return Candle(
        open_time_ms=open_time_ms,
        close_time_ms=(
            open_time_ms + INTERVAL_MS - 1
            if close_time_ms is None
            else close_time_ms
        ),
        open=Decimal(open_price),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(volume),
    )


def make_sequence(size: int = 3) -> list[Candle]:
    return [
        make_candle(open_time_ms=OPEN_TIME_MS + index * INTERVAL_MS)
        for index in range(size)
    ]


def utc_timestamp_ms(year: int, month: int, day: int = 1) -> int:
    return int(datetime(year, month, day, tzinfo=UTC).timestamp() * 1_000)


def make_monthly_sequence(year: int, month: int, next_year: int, next_month: int) -> list[Candle]:
    first_open = utc_timestamp_ms(year, month)
    second_open = utc_timestamp_ms(next_year, next_month)
    return [
        make_candle(open_time_ms=first_open, close_time_ms=second_open - 1),
        make_candle(
            open_time_ms=second_open,
            close_time_ms=utc_timestamp_ms(next_year + (next_month == 12), next_month % 12 + 1)
            - 1,
        ),
    ]


def validate_closed_sequence(candles: list[Candle]) -> None:
    validate_candle_sequence(
        candles,
        "1m",
        allow_incomplete_last=False,
        current_time_ms=OPEN_TIME_MS + 10 * INTERVAL_MS,
    )


def test_valid_candle_sequence() -> None:
    validate_closed_sequence(make_sequence())


def test_empty_sequence_is_rejected() -> None:
    with pytest.raises(CandleSequenceValidationError, match="must not be empty"):
        validate_closed_sequence([])


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("open_price", "0"),
        ("high", "-1"),
        ("low", "0"),
        ("close", "-1"),
    ],
)
def test_non_positive_price_is_rejected(field_name: str, value: str) -> None:
    with pytest.raises(CandleValidationError, match="must be positive"):
        validate_candle(make_candle(**{field_name: value}))


def test_negative_volume_is_rejected() -> None:
    with pytest.raises(CandleValidationError, match="volume"):
        validate_candle(make_candle(volume="-0.01"))


@pytest.mark.parametrize(
    "candle",
    [
        make_candle(low="106"),
        make_candle(high="99"),
    ],
)
def test_invalid_ohlc_is_rejected(candle: Candle) -> None:
    with pytest.raises(CandleValidationError, match="low|high"):
        validate_candle(candle)


def test_invalid_time_range_is_rejected() -> None:
    with pytest.raises(CandleValidationError, match="open_time_ms"):
        validate_candle(
            make_candle(
                close_time_ms=OPEN_TIME_MS,
            )
        )


def test_duplicate_open_time_is_rejected() -> None:
    candles = make_sequence(2)
    candles[1] = make_candle(open_time_ms=candles[0].open_time_ms)

    with pytest.raises(CandleSequenceValidationError, match="Duplicate"):
        validate_closed_sequence(candles)


def test_wrong_order_is_rejected() -> None:
    candles = list(reversed(make_sequence(2)))

    with pytest.raises(CandleSequenceValidationError, match="not strictly ordered"):
        validate_closed_sequence(candles)


def test_missing_candle_is_rejected() -> None:
    candles = [
        make_candle(),
        make_candle(open_time_ms=OPEN_TIME_MS + 2 * INTERVAL_MS),
    ]

    with pytest.raises(CandleSequenceValidationError, match="Unexpected candle interval"):
        validate_closed_sequence(candles)


def test_overlapping_time_ranges_are_rejected() -> None:
    candles = make_sequence(2)
    candles[0] = make_candle(close_time_ms=candles[1].open_time_ms)

    with pytest.raises(CandleSequenceValidationError, match="overlap"):
        validate_closed_sequence(candles)


def test_incomplete_last_candle_is_allowed() -> None:
    candles = make_sequence(2)

    validate_candle_sequence(
        candles,
        "1m",
        allow_incomplete_last=True,
        current_time_ms=candles[-1].close_time_ms,
    )


def test_incomplete_last_candle_is_rejected() -> None:
    candles = make_sequence(2)

    with pytest.raises(CandleSequenceValidationError, match="last candle is incomplete"):
        validate_candle_sequence(
            candles,
            "1m",
            allow_incomplete_last=False,
            current_time_ms=candles[-1].close_time_ms,
        )


def test_incomplete_non_last_candle_is_rejected() -> None:
    candles = make_sequence(2)

    with pytest.raises(CandleSequenceValidationError, match="Only the last"):
        validate_candle_sequence(
            candles,
            "1m",
            allow_incomplete_last=True,
            current_time_ms=candles[0].close_time_ms,
        )


def test_unsupported_interval_is_rejected() -> None:
    with pytest.raises(UnsupportedMarketIntervalError, match="Unsupported"):
        validate_candle_sequence(
            make_sequence(),
            "60s",
            allow_incomplete_last=False,
            current_time_ms=OPEN_TIME_MS + 10 * INTERVAL_MS,
        )


@pytest.mark.parametrize(
    ("year", "month", "next_year", "next_month"),
    [
        (2023, 1, 2023, 2),
        (2023, 2, 2023, 3),
        (2024, 2, 2024, 3),
        (2023, 12, 2024, 1),
    ],
)
def test_calendar_month_transitions_are_valid(
    year: int,
    month: int,
    next_year: int,
    next_month: int,
) -> None:
    candles = make_monthly_sequence(year, month, next_year, next_month)

    validate_candle_sequence(
        candles,
        "1M",
        allow_incomplete_last=False,
        current_time_ms=candles[-1].close_time_ms + 1,
    )


def test_calendar_month_is_not_treated_as_thirty_days() -> None:
    january_open = utc_timestamp_ms(2023, 1)
    thirty_days_later = january_open + 30 * 24 * 60 * 60 * 1_000
    candles = [
        make_candle(open_time_ms=january_open, close_time_ms=thirty_days_later - 1),
        make_candle(open_time_ms=thirty_days_later),
    ]

    with pytest.raises(CandleSequenceValidationError, match="Unexpected candle interval"):
        validate_candle_sequence(
            candles,
            "1M",
            allow_incomplete_last=False,
            current_time_ms=candles[-1].close_time_ms + 1,
        )
