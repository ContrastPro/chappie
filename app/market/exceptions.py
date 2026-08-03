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
