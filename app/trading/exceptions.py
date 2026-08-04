class TradingDomainError(ValueError):
    """Base error for invalid trading-domain values."""


class InvalidMarketSnapshotError(TradingDomainError):
    pass


class InvalidSignalError(TradingDomainError):
    pass


class InvalidOrderIntentError(TradingDomainError):
    pass


class InvalidRiskDecisionError(TradingDomainError):
    pass


class InvalidStrategyConfigurationError(TradingDomainError):
    pass


class StrategyInvariantError(TradingDomainError):
    pass


class InvalidDecimalValueError(TradingDomainError):
    pass


class InconsistentTradingTimestampError(TradingDomainError):
    pass
