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


class TradingPipelineError(TradingDomainError):
    pass


class InvalidPipelineConfigurationError(TradingPipelineError):
    pass


class PipelineInvariantError(TradingPipelineError):
    pass


class IntentPlanningError(TradingPipelineError):
    pass


class InvalidIntentPlannerConfigurationError(IntentPlanningError):
    pass


class IntentPlannerInvariantError(IntentPlanningError):
    pass


class IntentFactoryError(IntentPlanningError):
    pass


class PipelineRunIdFactoryError(TradingPipelineError):
    pass


class StrategyExecutionError(TradingPipelineError):
    pass


class PlannerExecutionError(TradingPipelineError):
    pass
