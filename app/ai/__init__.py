"""Chappie AI: research reasoning with no trading authority."""

from app.ai.adapters import facts_from_historical_event_context, historical_context_metadata
from app.ai.errors import (
    AIError,
    AIProviderError,
    AIProviderTimeoutError,
    AIRoutingError,
    AIValidationError,
)
from app.ai.runtime import AIConfig, ChappieAI
from app.ai.schemas import FactV1, ReasoningOutputV1
from app.ai.tasks import AITask, ModelProfile

__all__ = [
    "AIConfig",
    "AIError",
    "AIProviderError",
    "AIProviderTimeoutError",
    "AIRoutingError",
    "AITask",
    "AIValidationError",
    "ChappieAI",
    "FactV1",
    "ModelProfile",
    "ReasoningOutputV1",
    "facts_from_historical_event_context",
    "historical_context_metadata",
]
