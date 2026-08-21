from __future__ import annotations

from app.ai.providers import MockMode, MockProvider
from app.ai.router import ModelRouter
from app.ai.runtime import AIConfig, ChappieAI
from app.ai.tasks import ModelProfile
from app.platform.bootstrap import ChappieLocalConfigV1
from app.platform.setup.ai_smoke import (
    SETUP_SMOKE_TIMEOUT_SECONDS,
    AISetupSmokeStateV1,
    ChappieAISetupSmokeV1,
)


def _config(*, model: str = "qwen3:8b") -> ChappieLocalConfigV1:
    return ChappieLocalConfigV1(provider="ollama", model=model, enabled_capabilities=("BALANCED",))


def _smoke(provider: MockProvider, captured: list[ChappieLocalConfigV1]) -> ChappieAISetupSmokeV1:
    def build(config: ChappieLocalConfigV1) -> ChappieAI:
        captured.append(config)
        return ChappieAI(
            ModelRouter({ModelProfile.LOCAL_DEFAULT: provider}),
            AIConfig(model=config.model or "", timeout_seconds=SETUP_SMOKE_TIMEOUT_SECONDS),
        )

    return ChappieAISetupSmokeV1(ai_builder=build)


def test_valid_smoke_uses_chappie_ai_and_configured_model() -> None:
    provider = MockProvider()
    captured: list[ChappieLocalConfigV1] = []

    result = _smoke(provider, captured).run(_config(model="selected-model:1"))

    assert result.state == AISetupSmokeStateV1.VALID
    assert captured == [_config(model="selected-model:1")]
    assert len(provider.requests) == 1
    assert provider.requests[0].timeout_seconds == SETUP_SMOKE_TIMEOUT_SECONDS
    assert result.request_digest


def test_provider_error_and_timeout_are_typed_and_fail_closed() -> None:
    for mode, expected in (
        (MockMode.ERROR, AISetupSmokeStateV1.PROVIDER_ERROR),
        (MockMode.TIMEOUT, AISetupSmokeStateV1.TIMEOUT),
    ):
        provider = MockProvider(mode)
        result = _smoke(provider, []).run(_config())
        assert result.state == expected
        assert len(provider.requests) == 1


def test_invalid_structured_response_is_validation_error() -> None:
    provider = MockProvider(MockMode.INVALID_SCHEMA)

    result = _smoke(provider, []).run(_config())

    assert result.state == AISetupSmokeStateV1.VALIDATION_ERROR


def test_invalid_config_fails_before_provider_generation() -> None:
    provider = MockProvider()
    result = _smoke(provider, []).run(ChappieLocalConfigV1())

    assert result.state == AISetupSmokeStateV1.CONFIG_ERROR
    assert provider.requests == []
