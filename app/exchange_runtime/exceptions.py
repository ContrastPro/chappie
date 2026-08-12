class ExchangeRuntimeError(RuntimeError):
    """Base fail-closed runtime integration error."""


class ExchangeSettlementError(ExchangeRuntimeError):
    """Durable exchange settlement failed."""
