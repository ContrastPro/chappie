"""Provider-neutral Chappie AI request, response, and observability models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.ai.research_reasoning import ResearchReasoningOutputV11
from app.ai.schemas import FactV1, ReasoningOutputV1
from app.ai.tasks import AITask, ModelProfile


@dataclass(frozen=True, slots=True)
class ModelRequest:
    request_id: str
    task: AITask
    model_profile: ModelProfile
    system_instruction: str
    prompt_version: str
    facts: tuple[FactV1, ...]
    context: dict[str, object]
    response_schema: dict[str, Any]
    temperature: float
    timeout_seconds: float
    information_cutoff: datetime
    input_digest: str


@dataclass(frozen=True, slots=True)
class ProviderResult:
    structured_output: object
    model: str
    model_version: str
    latency_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    provider_metadata: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ModelResponse:
    request_id: str
    provider: str
    model: str
    model_version: str
    task: AITask
    prompt_version: str
    structured_output: ReasoningOutputV1 | ResearchReasoningOutputV11
    latency_ms: int
    input_digest: str
    output_digest: str
    created_at: datetime
    validation_status: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    provider_metadata: tuple[tuple[str, str], ...] = ()
