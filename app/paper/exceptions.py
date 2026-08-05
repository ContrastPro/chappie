class PaperTradingError(Exception):
    """Base error for paper-trading orchestration."""


class PaperTradingConfigurationError(PaperTradingError, ValueError):
    pass


class PaperTradingStateError(PaperTradingError):
    pass


class PaperTradingInvariantError(PaperTradingError):
    pass


class PaperTradingDecisionError(PaperTradingInvariantError):
    pass


class PaperTradingRiskError(PaperTradingError):
    pass


class PaperTradingExecutionError(PaperTradingError):
    pass


class PaperTradingPortfolioError(PaperTradingError):
    pass


class PaperTradingObserverError(PaperTradingError):
    pass


class PaperTradingCycleIdFactoryError(PaperTradingError):
    pass


class PaperTradingArithmeticError(PaperTradingError, ArithmeticError):
    pass
