"""High-level Chappie AI facade and strict validation pipeline."""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from app.ai.errors import AIValidationError
from app.ai.models import ModelRequest, ModelResponse
from app.ai.prompts import prompt_digest, system_instruction
from app.ai.router import ModelRouter
from app.ai.schemas import (
    REASONING_OUTPUT_JSON_SCHEMA,
    FactV1,
    digest,
    parse_reasoning_output,
    timestamp_ms,
)
from app.ai.tasks import AITask, ModelProfile, task_contract


@dataclass(frozen=True, slots=True)
class AIConfig:
    profile: ModelProfile = ModelProfile.LOCAL_DEFAULT
    temperature: float = 0.0
    timeout_seconds: float = 30.0
    endpoint: str = "http://localhost:11434"
    model: str = "qwen3:8b"

    def __post_init__(self) -> None:
        if not math.isfinite(self.temperature) or not 0 <= self.temperature <= 2:
            raise AIValidationError("temperature must be finite and between zero and two")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise AIValidationError("timeout must be finite and positive")


class ChappieAI:
    def __init__(self, router: ModelRouter, config: AIConfig | None = None) -> None:
        self.router = router
        self.config = config or AIConfig()

    def generate(
        self,
        *,
        task: AITask,
        facts: tuple[FactV1, ...],
        information_cutoff: datetime,
        context: dict[str, object] | None = None,
        request_id: str | None = None,
        created_at: datetime | None = None,
    ) -> ModelResponse:
        cutoff_ms = timestamp_ms(information_cutoff)
        identifiers = tuple(fact.fact_id for fact in facts)
        if len(set(identifiers)) != len(identifiers):
            raise AIValidationError("fact IDs must be unique")
        future = [fact.fact_id for fact in facts if timestamp_ms(fact.timestamp) > cutoff_ms]
        if future:
            raise AIValidationError(f"facts exceed information cutoff: {future}")
        contract = task_contract(task)
        context_payload = context or {}
        input_payload = {
            "task": task.value,
            "profile": self.config.profile.value,
            "prompt_version": contract.prompt_identity,
            "prompt_digest": prompt_digest(contract),
            "information_cutoff_ms": cutoff_ms,
            "facts": [fact.payload() for fact in facts],
            "context": context_payload,
            "temperature": self.config.temperature,
            "timeout_seconds": self.config.timeout_seconds,
        }
        request = ModelRequest(
            request_id or uuid.uuid4().hex,
            task,
            self.config.profile,
            system_instruction(contract),
            contract.prompt_identity,
            facts,
            context_payload,
            REASONING_OUTPUT_JSON_SCHEMA,
            self.config.temperature,
            self.config.timeout_seconds,
            information_cutoff,
            digest(input_payload),
        )
        provider = self.router.provider_for(self.config.profile)
        raw = provider.generate(request)
        validated = parse_reasoning_output(raw.structured_output, set(identifiers))
        return ModelResponse(
            request.request_id,
            provider.provider_identity,
            raw.model,
            raw.model_version,
            task,
            contract.prompt_identity,
            validated,
            raw.latency_ms,
            request.input_digest,
            digest(validated.payload()),
            created_at or datetime.now(tz=UTC),
            "VALID",
            raw.input_tokens,
            raw.output_tokens,
            tuple(sorted(raw.provider_metadata)),
        )

    def close(self) -> None:
        self.router.close()
