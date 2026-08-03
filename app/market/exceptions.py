class MarketProviderError(RuntimeError):
    """Ошибка при получении или обработке рыночных данных."""


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
