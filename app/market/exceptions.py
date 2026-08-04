class MarketProviderError(RuntimeError):
    """Ошибка при получении или обработке рыночных данных."""

    retryable = False


class MarketProviderTimeoutError(MarketProviderError):
    """A market provider request exceeded its timeout."""

    retryable = True


class MarketProviderConnectionError(MarketProviderError):
    """A market provider could not be reached."""

    retryable = True


class MarketProviderHTTPError(MarketProviderError):
    """A market provider returned a non-success HTTP status."""

    def __init__(
        self,
        status_code: int,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(f"Market provider returned HTTP {status_code}")
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds

    @property
    def retryable(self) -> bool:
        return self.status_code in {408, 429} or 500 <= self.status_code <= 599


class MarketProviderResponseError(MarketProviderError):
    """A market provider returned an invalid or unexpected response."""


class MarketDataValidationError(ValueError):
    """Base error for invalid normalized market data."""


class CandleValidationError(MarketDataValidationError):
    """A single candle violates market-data invariants."""


class CandleSequenceValidationError(MarketDataValidationError):
    """A candle collection violates ordering or continuity invariants."""


class CandleSequenceGapError(CandleSequenceValidationError):
    """Two adjacent candles do not match the configured stream interval."""

    def __init__(
        self,
        *,
        stream_key: object,
        previous_index: int,
        current_index: int,
        previous_open_time_ms: int,
        previous_close_time_ms: int,
        expected_open_time_ms: int,
        received_open_time_ms: int,
        interval: str,
    ) -> None:
        self.stream_key = stream_key
        self.previous_index = previous_index
        self.current_index = current_index
        self.previous_open_time_ms = previous_open_time_ms
        self.previous_close_time_ms = previous_close_time_ms
        self.expected_open_time_ms = expected_open_time_ms
        self.received_open_time_ms = received_open_time_ms
        self.interval = interval
        self.actual_difference_ms = received_open_time_ms - previous_open_time_ms
        super().__init__(
            "Unexpected candle interval: "
            f"previous_index={previous_index} current_index={current_index} "
            f"previous_open_time_ms={previous_open_time_ms} "
            f"expected_open_time_ms={expected_open_time_ms} "
            f"received_open_time_ms={received_open_time_ms} "
            f"actual_difference_ms={self.actual_difference_ms} interval={interval}"
        )


class IncompleteCandleSequenceError(CandleSequenceValidationError):
    """A candle batch contains more incomplete candles than policy permits."""

    def __init__(
        self,
        *,
        invalid_index: int,
        invalid_open_time_ms: int,
        invalid_close_time_ms: int,
        latest_open_time_ms: int,
        latest_close_time_ms: int,
        incomplete_candle_count: int,
        observed_at_ms: int,
        only_last_allowed: bool,
    ) -> None:
        self.invalid_index = invalid_index
        self.invalid_open_time_ms = invalid_open_time_ms
        self.invalid_close_time_ms = invalid_close_time_ms
        self.latest_open_time_ms = latest_open_time_ms
        self.latest_close_time_ms = latest_close_time_ms
        self.incomplete_candle_count = incomplete_candle_count
        self.observed_at_ms = observed_at_ms
        message = (
            "Only the last candle may be incomplete"
            if only_last_allowed
            else "The last candle is incomplete"
        )
        super().__init__(
            f"{message}; invalid candle at index {invalid_index}; "
            f"observed_at_ms={observed_at_ms} "
            f"open_time_ms={invalid_open_time_ms} "
            f"close_time_ms={invalid_close_time_ms} "
            f"latest_open_time_ms={latest_open_time_ms} "
            f"latest_close_time_ms={latest_close_time_ms} "
            f"incomplete_candle_count={incomplete_candle_count}"
        )


class UnsupportedMarketIntervalError(MarketDataValidationError):
    """A market interval has no supported explicit duration."""


class MarketCacheError(ValueError):
    """Base error for invalid market-cache operations."""


class MarketCacheConfigurationError(MarketCacheError):
    """The market cache was configured with invalid parameters."""


class MarketStreamKeyError(MarketCacheError):
    """A market stream key is invalid or not normalized."""


class MarketCacheReadError(MarketCacheError):
    """A market-cache read request has invalid parameters."""


class MarketCacheOutOfOrderError(MarketCacheError):
    """A candle is older than the latest candle in its stream."""


class MarketCacheDuplicateConflictError(MarketCacheError):
    """A duplicate candle conflicts with immutable cached data."""


class MarketCacheGapError(MarketCacheError):
    """A new candle does not follow the expected stream interval."""


class MarketCacheClosedCandleUpdateError(MarketCacheError):
    """An update attempted to change an already closed candle."""

    reason_code = "confirmed_closed_candle_conflict"

    def __init__(
        self,
        message: str,
        *,
        stream_key: object | None = None,
        stored_candle: object | None = None,
        received_candle: object | None = None,
        observed_at_ms: int | None = None,
        differing_fields: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.stream_key = stream_key
        self.stored_candle = stored_candle
        self.received_candle = received_candle
        self.observed_at_ms = observed_at_ms
        self.stored_lifecycle = "closed_confirmed"
        self.received_lifecycle = (
            "closed"
            if (
                received_candle is not None
                and observed_at_ms is not None
                and observed_at_ms > received_candle.close_time_ms
            )
            else "open"
        )
        self.differing_fields = differing_fields
        self.previously_confirmed_closed = True


class MarketCacheInvalidOpenCandleUpdateError(MarketCacheError):
    """An open-candle update violates monotonic update rules."""


class MarketCacheBatchError(MarketCacheError):
    """A candle batch is invalid and was rejected atomically."""


class PollingEngineError(RuntimeError):
    """Base error for polling-engine lifecycle and configuration failures."""


class PollingEngineConfigurationError(PollingEngineError):
    """The polling engine policy or dependencies are invalid."""


class PollingEngineStateError(PollingEngineError):
    """A polling operation is not valid in the current lifecycle state."""


class MarketEventError(RuntimeError):
    """Base error for market event creation and delivery."""


class InvalidEventSubscriptionError(MarketEventError):
    """An event subscription contains an invalid type or handler."""


class RecursiveEventPublishError(MarketEventError):
    """Recursive synchronous event publication is forbidden."""


class MarketEventProjectionError(MarketEventError):
    """Cache mutation details violate event projection invariants."""


class DuplicateCandleClosureError(MarketEventProjectionError):
    """A candle closure conflicts with the stream closure watermark."""


class InvalidMarketEventError(MarketEventError):
    """A market event contains invalid envelope or payload data."""
