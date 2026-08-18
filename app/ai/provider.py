"""Provider-neutral model backend protocol."""

from typing import Protocol, runtime_checkable

from app.ai.models import ModelRequest, ProviderResult


@runtime_checkable
class ModelProvider(Protocol):
    @property
    def provider_identity(self) -> str: ...

    @property
    def model_identity(self) -> str: ...

    def is_ready(self) -> bool: ...

    def generate(self, request: ModelRequest) -> ProviderResult: ...

    def close(self) -> None: ...
