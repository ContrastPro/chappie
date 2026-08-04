from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from app.market.cache import (
    BatchUpsertResult,
    CandleUpsertResult,
    InMemoryMarketCache,
    MarketStreamKey,
)
from app.market.exceptions import (
    MarketCacheBatchError,
    MarketCacheClosedCandleUpdateError,
    MarketCacheConfigurationError,
    MarketCacheDuplicateConflictError,
    MarketCacheGapError,
    MarketCacheInvalidOpenCandleUpdateError,
    MarketCacheOutOfOrderError,
    MarketCacheReadError,
    UnsupportedMarketIntervalError,
)
from app.market.models import Candle, CandleSnapshot

OPEN_TIME_MS = 1_700_000_000_000
INTERVAL_MS = 60_000
KEY = MarketStreamKey("BTCUSDT", "1m")


def make_candle(
    index: int = 0,
    *,
    open_price: str = "100",
    high: str = "110",
    low: str = "90",
    close: str = "105",
    volume: str = "10",
    open_time_ms: int | None = None,
) -> Candle:
    candle_open_time = OPEN_TIME_MS + index * INTERVAL_MS if open_time_ms is None else open_time_ms
    return Candle(
        open_time_ms=candle_open_time,
        close_time_ms=candle_open_time + INTERVAL_MS - 1,
        open=Decimal(open_price),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(volume),
    )


def observed_during(candle: Candle) -> int:
    return candle.close_time_ms


def observed_after(candle: Candle) -> int:
    return candle.close_time_ms + 1


def test_create_empty_cache() -> None:
    cache = InMemoryMarketCache()

    assert cache.capacity == 1000
    assert cache.get_snapshot(KEY) == ()
    assert cache.get_latest_candle(KEY) is None
    assert cache.get_latest_closed_candle(KEY, OPEN_TIME_MS) is None


def test_batch_result_exposes_immutable_committed_change_details() -> None:
    cache = InMemoryMarketCache(capacity=1)
    first = make_candle(0)
    second = make_candle(1)

    initial = cache.upsert_candles(KEY, [first], observed_during(first))
    duplicate = cache.upsert_candles(KEY, [first], observed_after(first))
    inserted = cache.upsert_candles(KEY, [second], observed_during(second))

    assert initial.changes[0].previous is None
    assert initial.changes[0].current == CandleSnapshot.from_candle(first)
    assert duplicate.changes[0].result is CandleUpsertResult.UNCHANGED
    assert duplicate.changes[0].previous == CandleSnapshot.from_candle(first)
    assert duplicate.changes[0].current == CandleSnapshot.from_candle(first)
    assert inserted.changes[0].previous == CandleSnapshot.from_candle(first)
    assert inserted.changes[0].current == CandleSnapshot.from_candle(second)
    assert inserted.changes[0].evicted == (CandleSnapshot.from_candle(first),)
    assert inserted.evicted_count == 1

    with pytest.raises(FrozenInstanceError):
        inserted.changes[0].observed_at_ms = 0  # type: ignore[misc]

    first.close = Decimal(1)
    second.close = Decimal(2)
    assert inserted.changes[0].previous.close == Decimal(105)  # type: ignore[union-attr]
    assert inserted.changes[0].current.close == Decimal(105)
    assert inserted.changes[0].evicted[0].close == Decimal(105)
    assert cache.get_latest_candle(KEY).close == Decimal(105)  # type: ignore[union-attr]


def test_insert_first_candle() -> None:
    cache = InMemoryMarketCache()
    candle = make_candle()

    result = cache.upsert_candle(KEY, candle, observed_during(candle))

    assert result is CandleUpsertResult.INSERTED
    assert cache.get_snapshot(KEY) == (candle,)


def test_insert_next_candle() -> None:
    cache = InMemoryMarketCache()
    first = make_candle()
    second = make_candle(1)
    cache.upsert_candle(KEY, first, observed_after(first))

    result = cache.upsert_candle(KEY, second, observed_during(second))

    assert result is CandleUpsertResult.INSERTED
    assert cache.get_snapshot(KEY) == (first, second)


def test_exact_duplicate_is_unchanged() -> None:
    cache = InMemoryMarketCache()
    candle = make_candle()
    cache.upsert_candle(KEY, candle, observed_during(candle))

    result = cache.upsert_candle(KEY, candle, observed_during(candle))

    assert result is CandleUpsertResult.UNCHANGED
    assert cache.get_snapshot(KEY) == (candle,)


def test_update_latest_open_candle() -> None:
    cache = InMemoryMarketCache()
    candle = make_candle()
    updated = make_candle(high="115", low="85", close="108", volume="12")
    cache.upsert_candle(KEY, candle, observed_during(candle))

    result = cache.upsert_candle(KEY, updated, observed_during(candle))

    assert result is CandleUpsertResult.UPDATED
    assert cache.get_latest_candle(KEY) == updated


@pytest.mark.parametrize(
    "updated",
    [
        make_candle(open_price="101"),
        make_candle(high="109"),
        make_candle(low="91"),
        make_candle(volume="9"),
    ],
)
def test_invalid_open_candle_updates_are_rejected(updated: Candle) -> None:
    cache = InMemoryMarketCache()
    candle = make_candle()
    cache.upsert_candle(KEY, candle, observed_during(candle))

    with pytest.raises(MarketCacheInvalidOpenCandleUpdateError):
        cache.upsert_candle(KEY, updated, observed_during(candle))

    assert cache.get_latest_candle(KEY) == candle


def test_closed_candle_update_is_rejected() -> None:
    cache = InMemoryMarketCache()
    candle = make_candle()
    updated = make_candle(close="106")
    cache.upsert_candle(KEY, candle, observed_after(candle))

    with pytest.raises(MarketCacheClosedCandleUpdateError):
        cache.upsert_candle(KEY, updated, observed_after(candle))


def test_conflicting_historical_duplicate_is_rejected() -> None:
    cache = InMemoryMarketCache()
    first = make_candle()
    second = make_candle(1)
    cache.upsert_candles(KEY, [first, second], observed_after(second))
    conflicting = make_candle(close="106")

    with pytest.raises(MarketCacheDuplicateConflictError):
        cache.upsert_candle(KEY, conflicting, observed_after(second))


def test_gap_is_rejected() -> None:
    cache = InMemoryMarketCache()
    first = make_candle()
    cache.upsert_candle(KEY, first, observed_after(first))

    with pytest.raises(MarketCacheGapError):
        cache.upsert_candle(KEY, make_candle(2), observed_after(make_candle(2)))


def test_out_of_order_is_rejected() -> None:
    cache = InMemoryMarketCache()
    second = make_candle(1)
    cache.upsert_candle(KEY, second, observed_after(second))
    out_of_order = make_candle(open_time_ms=OPEN_TIME_MS + INTERVAL_MS // 2)

    with pytest.raises(MarketCacheOutOfOrderError):
        cache.upsert_candle(KEY, out_of_order, observed_after(second))


def test_capacity_evicts_oldest_candle_explicitly() -> None:
    cache = InMemoryMarketCache(capacity=2)
    candles = [make_candle(index) for index in range(3)]

    for candle in candles:
        cache.upsert_candle(KEY, candle, observed_after(candle))

    assert cache.get_snapshot(KEY) == tuple(candles[1:])


def test_streams_are_independent_by_symbol_and_interval() -> None:
    cache = InMemoryMarketCache()
    btc_key = KEY
    eth_key = MarketStreamKey("ETHUSDT", "1m")
    hourly_key = MarketStreamKey("BTCUSDT", "1h")
    minute_candle = make_candle()
    hourly_candle = Candle(
        open_time_ms=OPEN_TIME_MS,
        close_time_ms=OPEN_TIME_MS + 3_600_000 - 1,
        open=Decimal(100),
        high=Decimal(110),
        low=Decimal(90),
        close=Decimal(105),
        volume=Decimal(10),
    )

    cache.upsert_candle(btc_key, minute_candle, observed_after(minute_candle))
    cache.upsert_candle(eth_key, minute_candle, observed_after(minute_candle))
    cache.upsert_candle(hourly_key, hourly_candle, observed_after(hourly_candle))

    assert len(cache.get_snapshot(btc_key)) == 1
    assert len(cache.get_snapshot(eth_key)) == 1
    assert len(cache.get_snapshot(hourly_key)) == 1


def test_snapshot_cannot_modify_internal_state() -> None:
    cache = InMemoryMarketCache()
    candle = make_candle()
    cache.upsert_candle(KEY, candle, observed_after(candle))
    snapshot = cache.get_snapshot(KEY)

    snapshot[0].close = Decimal(999)

    assert cache.get_latest_candle(KEY) == candle
    with pytest.raises(TypeError):
        snapshot[0] = candle  # type: ignore[index]


def test_snapshot_limit_returns_latest_candles() -> None:
    cache = InMemoryMarketCache()
    candles = [make_candle(index) for index in range(3)]
    cache.upsert_candles(KEY, candles, observed_after(candles[-1]))

    assert cache.get_snapshot(KEY, limit=2) == tuple(candles[-2:])


@pytest.mark.parametrize("limit", [0, -1, 1.5, True])
def test_invalid_snapshot_limit_is_rejected(limit: object) -> None:
    cache = InMemoryMarketCache()

    with pytest.raises(MarketCacheReadError):
        cache.get_snapshot(KEY, limit=limit)  # type: ignore[arg-type]


def test_latest_candle_returns_defensive_copy() -> None:
    cache = InMemoryMarketCache()
    candle = make_candle()
    cache.upsert_candle(KEY, candle, observed_after(candle))

    latest = cache.get_latest_candle(KEY)

    assert latest == candle
    assert latest is not candle
    assert latest is not None
    latest.close = Decimal(999)
    assert cache.get_latest_candle(KEY) == candle


def test_latest_closed_candle_uses_explicit_time() -> None:
    cache = InMemoryMarketCache()
    first = make_candle()
    second = make_candle(1)
    cache.upsert_candles(KEY, [first, second], observed_after(second))

    latest_closed = cache.get_latest_closed_candle(KEY, second.close_time_ms)

    assert latest_closed == first
    assert latest_closed is not None
    latest_closed.close = Decimal(999)
    assert cache.get_latest_closed_candle(KEY, second.close_time_ms) == first
    assert cache.get_latest_closed_candle(KEY, second.close_time_ms + 1) == second


def test_empty_stream_reads_are_safe() -> None:
    cache = InMemoryMarketCache()

    assert cache.get_snapshot(KEY, limit=10) == ()
    assert cache.get_latest_candle(KEY) is None
    assert cache.get_latest_closed_candle(KEY, OPEN_TIME_MS) is None


def test_successful_batch_is_atomic() -> None:
    cache = InMemoryMarketCache(capacity=2)
    candles = [make_candle(index) for index in range(3)]

    result = cache.upsert_candles(KEY, candles, observed_after(candles[-1]))

    assert result == BatchUpsertResult(
        results=(CandleUpsertResult.INSERTED,) * 3,
        evicted_count=1,
    )
    assert cache.get_snapshot(KEY) == tuple(candles[-2:])


def test_batch_rolls_back_on_error() -> None:
    cache = InMemoryMarketCache()
    first = make_candle()
    cache.upsert_candle(KEY, first, observed_after(first))
    invalid_batch = [make_candle(1), make_candle(3)]

    with pytest.raises(MarketCacheBatchError):
        cache.upsert_candles(KEY, invalid_batch, observed_after(invalid_batch[-1]))

    assert cache.get_snapshot(KEY) == (first,)


def test_invalid_batch_element_is_rejected_without_state_change() -> None:
    cache = InMemoryMarketCache()

    with pytest.raises(MarketCacheBatchError, match="only Candle"):
        cache.upsert_candles(KEY, [make_candle(), object()], OPEN_TIME_MS)  # type: ignore[list-item]

    assert cache.get_snapshot(KEY) == ()


def test_empty_batch_is_safe_no_op() -> None:
    cache = InMemoryMarketCache()

    assert cache.upsert_candles(KEY, [], OPEN_TIME_MS) == BatchUpsertResult((), 0)
    assert cache.get_snapshot(KEY) == ()


def test_unsupported_interval_is_rejected() -> None:
    cache = InMemoryMarketCache()
    key = MarketStreamKey("BTCUSDT", "60s")

    with pytest.raises(UnsupportedMarketIntervalError):
        cache.upsert_candle(key, make_candle(), OPEN_TIME_MS)


@pytest.mark.parametrize("capacity", [0, -1, 1.5, True])
def test_invalid_capacity_is_rejected(capacity: object) -> None:
    with pytest.raises(MarketCacheConfigurationError):
        InMemoryMarketCache(capacity=capacity)  # type: ignore[arg-type]


def test_runtime_open_tail_accepts_final_update_and_next_open_at_capacity() -> None:
    """Regression for the real bootstrap-to-regular Binance transition."""

    cache = InMemoryMarketCache(capacity=1000)
    bootstrap = [make_candle(index) for index in range(1000)]
    stored_open = bootstrap[-1]
    cache.upsert_candles(KEY, bootstrap, observed_during(stored_open))
    finalized = make_candle(
        999,
        high="112",
        low="89",
        close="108",
        volume="12",
    )
    next_open = make_candle(1000, close="101", volume="1")
    observed_at_ms = next_open.open_time_ms

    result = cache.upsert_candles(KEY, [finalized, next_open], observed_at_ms)

    assert result.results == (
        CandleUpsertResult.UPDATED,
        CandleUpsertResult.INSERTED,
    )
    assert result.evicted_count == 1
    assert len(cache.get_snapshot(KEY)) == 1000
    assert cache.get_snapshot(KEY)[-2:] == (finalized, next_open)


def test_exact_duplicate_crossing_close_boundary_confirms_once() -> None:
    cache = InMemoryMarketCache()
    item = make_candle()
    cache.upsert_candles(KEY, [item], observed_during(item))

    confirmed = cache.upsert_candles(KEY, [item], observed_after(item))
    repeated = cache.upsert_candles(KEY, [item], observed_after(item))

    assert confirmed.results == repeated.results == (CandleUpsertResult.UNCHANGED,)
    assert confirmed.changes[0].closure_confirmed
    assert confirmed.changes[0].current_closed_confirmed
    assert not repeated.changes[0].closure_confirmed
    assert repeated.changes[0].current_closed_confirmed


def test_final_open_update_confirms_then_protects_committed_version() -> None:
    cache = InMemoryMarketCache()
    original = make_candle()
    finalized = make_candle(high="111", low="89", close="108", volume="11")
    conflict = make_candle(high="112", low="88", close="109", volume="12")
    cache.upsert_candles(KEY, [original], observed_during(original))

    result = cache.upsert_candles(KEY, [finalized], observed_after(original))
    duplicate = cache.upsert_candles(KEY, [finalized], observed_after(original))

    assert result.results == (CandleUpsertResult.UPDATED,)
    assert result.changes[0].data_changed
    assert result.changes[0].closure_confirmed
    assert duplicate.results == (CandleUpsertResult.UNCHANGED,)
    with pytest.raises(MarketCacheBatchError) as captured:
        cache.upsert_candles(KEY, [conflict], observed_after(original))
    cause = captured.value.__cause__
    assert isinstance(cause, MarketCacheClosedCandleUpdateError)
    assert cause.reason_code == "confirmed_closed_candle_conflict"
    assert cause.differing_fields == ("high", "low", "close", "volume")
    assert cache.get_latest_candle(KEY) == finalized


def test_invalid_next_candle_rolls_back_data_and_closure_confirmation() -> None:
    cache = InMemoryMarketCache()
    original = make_candle()
    finalized = make_candle(high="111", close="108", volume="11")
    invalid_next = make_candle(2)
    cache.upsert_candles(KEY, [original], observed_during(original))

    with pytest.raises(MarketCacheBatchError):
        cache.upsert_candles(
            KEY,
            [finalized, invalid_next],
            observed_after(original),
        )

    assert cache.get_latest_candle(KEY) == original
    accepted = cache.upsert_candles(KEY, [finalized], observed_after(original))
    assert accepted.changes[0].closure_confirmed


def test_candle_inserted_after_close_is_immediately_protected() -> None:
    cache = InMemoryMarketCache()
    closed = make_candle()
    conflict = make_candle(high="111", close="108", volume="11")
    inserted = cache.upsert_candles(KEY, [closed], observed_after(closed))

    assert inserted.changes[0].current_closed_confirmed
    with pytest.raises(MarketCacheBatchError) as captured:
        cache.upsert_candles(KEY, [conflict], observed_after(closed))
    assert isinstance(captured.value.__cause__, MarketCacheClosedCandleUpdateError)
