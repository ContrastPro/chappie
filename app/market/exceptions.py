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
