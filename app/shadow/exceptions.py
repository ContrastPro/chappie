class ShadowDecisionError(Exception):
    """Base error for the shadow decision application layer."""


class InvalidShadowConfigurationError(ShadowDecisionError):
    pass


class InvalidShadowDecisionRecordError(ShadowDecisionError):
    pass


class ShadowEngineStateError(ShadowDecisionError):
    pass


class ShadowFactoryError(ShadowDecisionError):
    pass


class ShadowCacheReadError(ShadowDecisionError):
    pass


class ShadowEventCacheMismatchError(ShadowDecisionError):
    pass


class ShadowSnapshotBuildError(ShadowDecisionError):
    pass


class ShadowPipelineEvaluationError(ShadowDecisionError):
    pass


class ShadowObserverError(ShadowDecisionError):
    pass
