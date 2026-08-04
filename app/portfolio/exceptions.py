class PortfolioError(Exception):
    """Base error for portfolio accounting."""


class PortfolioConfigurationError(PortfolioError):
    pass


class PortfolioInvariantError(PortfolioError):
    pass


class PortfolioFillError(PortfolioError):
    pass


class PortfolioStreamMismatchError(PortfolioFillError):
    pass


class PortfolioAssetMismatchError(PortfolioFillError):
    pass


class InsufficientQuoteBalanceError(PortfolioFillError):
    pass


class InsufficientBaseBalanceError(PortfolioFillError):
    pass


class PositionRequiredError(PortfolioFillError):
    pass


class FillQuantityExceedsPositionError(InsufficientBaseBalanceError):
    pass


class DuplicateFillConflictError(PortfolioFillError):
    pass


class InvalidInstrumentRulesError(PortfolioConfigurationError):
    pass


class InvalidPortfolioTimestampError(PortfolioError):
    pass


class InvalidPortfolioMarkPriceError(PortfolioError):
    pass


class FillArithmeticMismatchError(PortfolioFillError):
    pass


class FillRuleViolationError(PortfolioFillError):
    pass
