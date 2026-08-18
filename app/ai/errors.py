"""Typed fail-closed errors for Chappie AI."""


class AIError(Exception):
    """Base Chappie AI error."""


class AIValidationError(AIError):
    """A request or model response violated a canonical contract."""


class AIProviderError(AIError):
    """A model provider failed without producing a trustworthy result."""


class AIProviderTimeoutError(AIProviderError):
    """A provider exceeded its configured timeout."""


class AIRoutingError(AIError):
    """No configured provider can serve the requested model profile."""
