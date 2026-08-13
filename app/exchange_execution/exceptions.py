class ExchangeExecutionError(Exception):
    """Base error for infrastructure-neutral exchange execution."""


class ExchangeExecutionConfigurationError(ExchangeExecutionError, ValueError):
    """Raised when exchange execution configuration is invalid."""


class ExchangeExecutionInvariantError(ExchangeExecutionError, ValueError):
    """Raised when immutable exchange facts are inconsistent."""


class ExchangeOrderStateError(ExchangeExecutionInvariantError):
    """Raised for an invalid order state or transition."""


class ExchangeOrderIdentityError(ExchangeExecutionInvariantError):
    """Raised for invalid or conflicting durable order identity."""


class ExchangeTradeConflictError(ExchangeExecutionInvariantError):
    """Raised when authoritative trades conflict or cross order scope."""


class ExchangeSettlementUnsupportedError(ExchangeExecutionError):
    """Raised when exchange truth cannot be represented by Portfolio."""


class ExchangeSubmissionSafetyError(ExchangeExecutionError):
    """Raised when submission safety gates are not all satisfied."""


class ExchangeReconciliationRequiredError(ExchangeSubmissionSafetyError):
    """Raised when exchange reconciliation blocks submission."""


class ExchangeCapabilityError(ExchangeSubmissionSafetyError):
    """Raised when the process-local submission capability is absent or invalid."""


class Phase3ReviewError(ExchangeExecutionInvariantError):
    """Raised before persistence when a Phase 3 reviewed request is unsafe."""
