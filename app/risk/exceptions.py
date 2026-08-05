class RiskError(Exception):
    """Base error for the deterministic risk domain."""


class RiskConfigurationError(RiskError):
    pass


class RiskInvariantError(RiskError):
    pass


class RiskEvaluationError(RiskError):
    pass


class RiskDecisionFactoryError(RiskError):
    pass


class RiskSizingError(RiskEvaluationError):
    pass


class RiskArithmeticError(RiskSizingError):
    pass


class RiskModelValidationError(RiskError):
    pass
