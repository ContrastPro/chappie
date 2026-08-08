class TradingCoordinationError(Exception):
    """Base error for shared decision coordination."""


class TradingCoordinationConfigurationError(TradingCoordinationError, ValueError):
    pass


class TradingCoordinationStateError(TradingCoordinationError):
    pass


class TradingCoordinationInvariantError(TradingCoordinationError):
    pass


class TradingCoordinationCacheError(TradingCoordinationError):
    pass


class TradingCoordinationSnapshotError(TradingCoordinationError):
    pass


class TradingCoordinationPipelineError(TradingCoordinationError):
    pass


class TradingCoordinationShadowError(TradingCoordinationError):
    pass


class TradingCoordinationPaperError(TradingCoordinationError):
    pass


class TradingCoordinationIdFactoryError(TradingCoordinationError):
    pass
