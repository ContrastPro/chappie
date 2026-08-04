import logging

from app.market.events import (
    CandleClosed,
    CandleEvicted,
    CandleInserted,
    CandleUpdated,
    MarketEventDispatcher,
    MarketGapDetected,
    MarketStreamFailed,
    SubscriptionToken,
)


def register_market_event_logging(
    dispatcher: MarketEventDispatcher,
    logger: logging.Logger,
) -> tuple[SubscriptionToken, ...]:
    """Register observability-only handlers for every Stage 1 market event."""

    def log_candle_event(event: CandleInserted | CandleClosed | CandleEvicted) -> None:
        log = logger.debug if isinstance(event, CandleInserted) and event.bootstrap else logger.info
        log(
            "%s symbol=%s interval=%s open_time_ms=%s close=%s "
            "event_id=%s correlation_cycle_id=%s reason=%s",
            type(event).__name__,
            event.stream_key.symbol,
            event.stream_key.interval,
            event.candle.open_time_ms,
            event.candle.close,
            event.event_id,
            event.correlation_cycle_id,
            event.reason.value,
        )

    def log_updated(event: CandleUpdated) -> None:
        logger.info(
            "CandleUpdated symbol=%s interval=%s open_time_ms=%s close=%s "
            "event_id=%s correlation_cycle_id=%s reason=%s",
            event.stream_key.symbol,
            event.stream_key.interval,
            event.current.open_time_ms,
            event.current.close,
            event.event_id,
            event.correlation_cycle_id,
            event.reason.value,
        )

    def log_gap(event: MarketGapDetected) -> None:
        logger.warning(
            "MarketGapDetected symbol=%s interval=%s expected_open_time_ms=%s "
            "received_open_time_ms=%s previous_index=%s current_index=%s "
            "previous_open_time_ms=%s previous_close_time_ms=%s "
            "actual_difference_ms=%s event_id=%s correlation_cycle_id=%s reason=%s",
            event.stream_key.symbol,
            event.stream_key.interval,
            event.expected_open_time_ms,
            event.received_open_time_ms,
            event.previous_index,
            event.current_index,
            event.previous_open_time_ms,
            event.previous_close_time_ms,
            event.actual_difference_ms,
            event.event_id,
            event.correlation_cycle_id,
            event.reason.value,
        )

    def log_failure(event: MarketStreamFailed) -> None:
        logger.error(
            "MarketStreamFailed symbol=%s interval=%s event_id=%s "
            "correlation_cycle_id=%s error_category=%s reason=%s terminal=%s",
            event.stream_key.symbol,
            event.stream_key.interval,
            event.event_id,
            event.correlation_cycle_id,
            event.error_category,
            event.reason.value,
            event.terminal,
        )

    return (
        dispatcher.subscribe(CandleInserted, log_candle_event),
        dispatcher.subscribe(CandleUpdated, log_updated),
        dispatcher.subscribe(CandleClosed, log_candle_event),
        dispatcher.subscribe(CandleEvicted, log_candle_event),
        dispatcher.subscribe(MarketGapDetected, log_gap),
        dispatcher.subscribe(MarketStreamFailed, log_failure),
    )
