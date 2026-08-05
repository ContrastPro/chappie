class ExecutionError(Exception):
    """Base error for deterministic execution."""


class ExecutionConfigurationError(ExecutionError):
    """Raised when execution configuration is invalid."""


class ExecutionInvariantError(ExecutionError):
    """Raised when immutable inputs disagree across model boundaries."""


class ExecutionModelValidationError(ExecutionError, ValueError):
    """Raised when an execution-owned result model is invalid."""


class ExecutionIdFactoryError(ExecutionError):
    """Raised when an execution ID cannot be created."""


class FillIdFactoryError(ExecutionError):
    """Raised when a fill ID cannot be created."""


class ExecutionArithmeticError(ExecutionError, ArithmeticError):
    """Raised when exact execution arithmetic cannot be completed."""


class UnsupportedRiskDecisionError(ExecutionError):
    """Raised when a decision cannot be consumed by paper execution."""
