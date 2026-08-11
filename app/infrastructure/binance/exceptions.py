class BinanceTestnetAdapterError(RuntimeError):
    """Base error for the isolated Binance Spot Testnet adapter."""


class BinanceConfigurationError(BinanceTestnetAdapterError, ValueError):
    """Adapter configuration is invalid or unsafe."""


class BinanceTransportError(BinanceTestnetAdapterError):
    """A read-only request failed at the transport boundary."""


class BinanceAmbiguousSubmissionError(BinanceTransportError):
    """Submission truth is unknown and reconciliation is required."""


class BinanceResponseError(BinanceTestnetAdapterError):
    """Binance returned an invalid response."""


class BinanceNormalizationError(BinanceResponseError):
    """A private response cannot be normalized safely."""


class BinanceUnsupportedContractError(BinanceNormalizationError):
    """A future or unsupported Binance contract was observed."""


class BinanceAuthenticationError(BinanceTestnetAdapterError):
    def __init__(self, *, code: int | None, status_code: int) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(f"Binance authentication failed; code={code} status={status_code}")


class BinanceRateLimitError(BinanceTestnetAdapterError):
    def __init__(self, *, status_code: int, retry_after_seconds: int | None) -> None:
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"Binance rate limit response; status={status_code}")


class BinanceOrderRejectedError(BinanceTestnetAdapterError):
    def __init__(self, *, code: int, category: str, safe_message: str) -> None:
        self.code = code
        self.category = category
        self.safe_message = safe_message
        super().__init__(f"Binance rejected order; code={code} category={category}")
