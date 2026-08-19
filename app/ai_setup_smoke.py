"""Bounded, structured ChappieAI smoke for the setup control plane."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from app.ai.errors import AIProviderError, AIProviderTimeoutError, AIValidationError
from app.ai.providers import OllamaProvider
from app.ai.router import ModelRouter
from app.ai.runtime import AIConfig, ChappieAI
from app.ai.schemas import FactV1
from app.ai.tasks import AITask, ModelProfile
from app.bootstrap import ChappieLocalConfigV1

AI_SETUP_SMOKE_IDENTITY = "chappie_ai_setup_smoke/1.0.0"
LOCAL_OLLAMA_ENDPOINT = "http://localhost:11434"
SETUP_SMOKE_TIMEOUT_SECONDS = 45.0


class AISetupSmokeStateV1(StrEnum):
    VALID = "VALID"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    TIMEOUT = "TIMEOUT"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    CONFIG_ERROR = "CONFIG_ERROR"


@dataclass(frozen=True, slots=True)
class AISetupSmokeResultV1:
    state: AISetupSmokeStateV1
    provider: str | None = None
    model: str | None = None
    latency_ms: int | None = None
    request_digest: str | None = None
    provider_reasoning_present: bool = False


AIBuilder = Callable[[ChappieLocalConfigV1], ChappieAI]


class ChappieAISetupSmokeV1:
    """Run one canonical structured request without retaining generated content."""

    def __init__(self, *, ai_builder: AIBuilder | None = None) -> None:
        self._ai_builder = ai_builder or build_configured_chappie_ai

    def run(self, config: ChappieLocalConfigV1) -> AISetupSmokeResultV1:
        if not valid_setup_ai_config(config):
            return AISetupSmokeResultV1(AISetupSmokeStateV1.CONFIG_ERROR)
        ai: ChappieAI | None = None
        try:
            ai = self._ai_builder(config)
            now = datetime.now(tz=UTC)
            response = ai.generate(
                task=AITask.MARKET_INTERPRETATION,
                facts=(
                    FactV1(
                        "setup-smoke-fact",
                        "SETUP_SMOKE",
                        now,
                        "Chappie.setup_smoke",
                        "structured-routing-check",
                        None,
                        "setup-smoke-fixture",
                    ),
                ),
                information_cutoff=now,
                context={"purpose": "setup_structured_smoke"},
            )
            return AISetupSmokeResultV1(
                AISetupSmokeStateV1.VALID,
                response.provider,
                response.model,
                response.latency_ms,
                response.input_digest,
                ("provider_reasoning_present", "true") in response.provider_metadata,
            )
        except AIProviderTimeoutError:
            return AISetupSmokeResultV1(AISetupSmokeStateV1.TIMEOUT, config.provider, config.model)
        except AIProviderError:
            return AISetupSmokeResultV1(
                AISetupSmokeStateV1.PROVIDER_ERROR, config.provider, config.model
            )
        except AIValidationError:
            return AISetupSmokeResultV1(
                AISetupSmokeStateV1.VALIDATION_ERROR, config.provider, config.model
            )
        finally:
            if ai is not None:
                ai.close()


def valid_setup_ai_config(config: ChappieLocalConfigV1) -> bool:
    return bool(
        config.provider == "ollama"
        and config.model
        and len(config.enabled_capabilities) == 1
        and config.enabled_capabilities[0] in {"LIGHT", "BALANCED", "ADVANCED"}
    )


def build_configured_chappie_ai(config: ChappieLocalConfigV1) -> ChappieAI:
    provider = OllamaProvider(
        endpoint=LOCAL_OLLAMA_ENDPOINT,
        model=config.model or "",
        timeout_seconds=SETUP_SMOKE_TIMEOUT_SECONDS,
    )
    return ChappieAI(
        ModelRouter({ModelProfile.LOCAL_DEFAULT: provider}),
        AIConfig(
            profile=ModelProfile.LOCAL_DEFAULT,
            timeout_seconds=SETUP_SMOKE_TIMEOUT_SECONDS,
            endpoint=LOCAL_OLLAMA_ENDPOINT,
            model=config.model or "",
        ),
    )
