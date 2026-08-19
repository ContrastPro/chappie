"""Typed fail-closed errors for Chappie AI."""


class AIError(Exception):
    """Base Chappie AI error."""


class AIValidationError(AIError):
    """A request or model response violated a canonical contract."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "VALIDATION_ERROR",
        field_path: str | None = None,
        received: object | None = None,
        expected: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.field_path = field_path
        self.received = received
        self.expected = expected


class AIProviderError(AIError):
    """A model provider failed without producing a trustworthy result."""


class AIProviderTimeoutError(AIProviderError):
    """A provider exceeded its configured timeout."""


class AIRoutingError(AIError):
    """No configured provider can serve the requested model profile."""
