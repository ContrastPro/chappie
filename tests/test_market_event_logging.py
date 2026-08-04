import logging
from dataclasses import replace
from decimal import Decimal

import pytest

from app.market.cache import InMemoryMarketCache, MarketStreamKey
from app.market.event_logging import register_market_event_logging
from app.market.events import (
    CandleClosed,
    CandleClosedReason,
    CandleEvicted,
    CandleEvictedReason,
    CandleInserted,
    CandleInsertedReason,
    CandleUpdated,
    CandleUpdatedReason,
    MarketEventDispatcher,
    MarketEventSource,
    MarketGapDetected,
    MarketGapPhase,
    MarketGapReason,
    MarketStreamFailed,
    MarketStreamFailureReason,
)
from app.market.models import CandleSnapshot

KEY = MarketStreamKey("BTCUSDT", "1m")


def envelope(event_id: str) -> dict[str, object]:
    return {
        "event_id": event_id,
        "occurred_at_ms": 1_700_000_060_000,
        "stream_key": KEY,
        "correlation_cycle_id": "cycle-1",
        "source": MarketEventSource.REST_POLLING,
    }


def test_logging_handlers_cover_cache_and_diagnostic_events(
    caplog: pytest.LogCaptureFixture,
) -> None:
    dispatcher = MarketEventDispatcher()
    logger = logging.getLogger("test.market.events")
    subscriptions = register_market_event_logging(dispatcher, logger)
    cache = InMemoryMarketCache()
    before = cache.get_snapshot(KEY)
    candle = CandleSnapshot(
        open_time_ms=1_700_000_000_000,
        close_time_ms=1_700_000_059_999,
        open=Decimal(100),
        high=Decimal(110),
        low=Decimal(90),
        close=Decimal(105),
        volume=Decimal(10),
    )
    updated = replace(candle, close=Decimal(106), high=Decimal(111))
    events = (
        CandleInserted(
            **envelope("event-1"),
            candle=candle,
            reason=CandleInsertedReason.NEW_CANDLE,
        ),
        CandleUpdated(
            **envelope("event-2"),
            previous=candle,
            current=updated,
            reason=CandleUpdatedReason.OPEN_CANDLE_REFRESH,
        ),
        CandleClosed(
            **envelope("event-3"),
            candle=updated,
            reason=CandleClosedReason.TIME_CONFIRMED,
        ),
        CandleEvicted(
            **envelope("event-4"),
            candle=updated,
            reason=CandleEvictedReason.CAPACITY_LIMIT,
        ),
        MarketGapDetected(
            **envelope("event-5"),
            expected_open_time_ms=1_700_000_060_000,
            received_open_time_ms=1_700_000_120_000,
            last_confirmed_open_time_ms=1_700_000_000_000,
            phase=MarketGapPhase.REGULAR,
            reason=MarketGapReason.INTERVAL_GAP,
        ),
        MarketStreamFailed(
            **envelope("event-6"),
            error_category="provider",
            consecutive_failures=1,
            terminal=True,
            reason=MarketStreamFailureReason.PROVIDER,
        ),
    )

    with caplog.at_level(logging.INFO, logger=logger.name):
        results = tuple(dispatcher.publish(event) for event in events)

    assert len(subscriptions) == 6
    assert all(result.failure_count == 0 for result in results)
    assert cache.get_snapshot(KEY) == before == ()
    messages = "\n".join(caplog.messages)
    for name in (
        "CandleInserted",
        "CandleUpdated",
        "CandleClosed",
        "CandleEvicted",
        "MarketGapDetected",
        "MarketStreamFailed",
    ):
        assert name in messages
    assert "event-6" in messages
    assert "cycle-1" in messages
    assert "provider" in messages


def test_bootstrap_insert_is_debug_while_regular_insert_remains_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    dispatcher = MarketEventDispatcher()
    logger = logging.getLogger("test.market.bootstrap.logging")
    register_market_event_logging(dispatcher, logger)
    candle = CandleSnapshot(
        open_time_ms=1_700_000_000_000,
        close_time_ms=1_700_000_059_999,
        open=Decimal(100),
        high=Decimal(110),
        low=Decimal(90),
        close=Decimal(105),
        volume=Decimal(10),
    )
    bootstrap = CandleInserted(
        **envelope("bootstrap-event"),
        candle=candle,
        reason=CandleInsertedReason.NEW_CANDLE,
        bootstrap=True,
    )
    regular = CandleInserted(
        **envelope("regular-event"),
        candle=candle,
        reason=CandleInsertedReason.NEW_CANDLE,
    )

    with caplog.at_level(logging.DEBUG, logger=logger.name):
        dispatcher.publish(bootstrap)
        dispatcher.publish(regular)

    levels = {record.getMessage(): record.levelno for record in caplog.records}
    bootstrap_message = next(message for message in levels if "bootstrap-event" in message)
    regular_message = next(message for message in levels if "regular-event" in message)
    assert levels[bootstrap_message] == logging.DEBUG
    assert levels[regular_message] == logging.INFO
