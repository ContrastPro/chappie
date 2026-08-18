from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.ai import (
    AIConfig,
    AIProviderError,
    AIProviderTimeoutError,
    AIRoutingError,
    AITask,
    AIValidationError,
    ChappieAI,
    FactV1,
    ModelProfile,
    facts_from_historical_event_context,
    historical_context_metadata,
)
from app.ai.prompts import prompt_digest, system_instruction
from app.ai.provider import ModelProvider
from app.ai.providers import MockMode, MockProvider, OllamaProvider
from app.ai.router import ModelRouter
from app.ai.schemas import REASONING_OUTPUT_JSON_SCHEMA
from app.ai.tasks import task_contract
from app.news_event_macro import HistoricalEventAIContextV1

NOW = datetime(2026, 8, 18, 12, tzinfo=UTC)


def _fact(identifier: str = "btc-return", *, timestamp: datetime = NOW) -> FactV1:
    return FactV1(identifier, "RDE", timestamp, "BTC.return", -1.2, "%", "a" * 64)


def _ai(provider: MockProvider, **config: object) -> ChappieAI:
    return ChappieAI(
        ModelRouter({ModelProfile.LOCAL_DEFAULT: provider}),
        AIConfig(**config),
    )


def test_provider_protocol_router_and_mock_valid_response() -> None:
    provider = MockProvider()
    assert isinstance(provider, ModelProvider)
    response = _ai(provider).generate(
        task=AITask.MARKET_INTERPRETATION,
        facts=(_fact(),),
        information_cutoff=NOW,
        request_id="request-1",
        created_at=NOW,
    )
    assert response.provider == "mock/1.0.0"
    assert response.request_id == "request-1"
    assert response.validation_status == "VALID"
    assert response.structured_output.observed_facts[0].fact_id == "btc-return"
    assert provider.requests[0].response_schema == REASONING_OUTPUT_JSON_SCHEMA
    assert provider.requests[0].timeout_seconds == 30.0


def test_router_fails_closed_for_unconfigured_profile() -> None:
    with pytest.raises(AIRoutingError):
        ModelRouter({}).provider_for(ModelProfile.CLOUD_REVIEWER)


def test_prompts_are_versioned_deterministic_and_forbid_trading_and_invention() -> None:
    contract = task_contract(AITask.NEWS_INTERPRETATION)
    instruction = system_instruction(contract)
    assert contract.prompt_identity == "news_interpretation/1.0.0"
    assert prompt_digest(contract) == prompt_digest(contract)
    assert "Never invent causes" in instruction
    assert "trade orders" in instruction
    assert "UNVALIDATED" in instruction


@pytest.mark.parametrize(
    "mode",
    (MockMode.INVALID_SCHEMA, MockMode.UNKNOWN_FACT, MockMode.VALIDATED_HYPOTHESIS),
)
def test_invalid_provider_output_fails_closed(mode: MockMode) -> None:
    with pytest.raises(AIValidationError):
        _ai(MockProvider(mode)).generate(
            task=AITask.HYPOTHESIS_GENERATION,
            facts=(_fact(),),
            information_cutoff=NOW,
        )


@pytest.mark.parametrize(
    ("mode", "error"),
    ((MockMode.ERROR, AIProviderError), (MockMode.TIMEOUT, AIProviderTimeoutError)),
)
def test_provider_errors_remain_typed(mode: MockMode, error: type[Exception]) -> None:
    with pytest.raises(error):
        _ai(MockProvider(mode)).generate(
            task=AITask.MARKET_INTERPRETATION,
            facts=(_fact(),),
            information_cutoff=NOW,
        )


def test_future_fact_and_duplicate_fact_id_are_rejected_before_provider_call() -> None:
    provider = MockProvider()
    with pytest.raises(AIValidationError, match="exceed information cutoff"):
        _ai(provider).generate(
            task=AITask.MARKET_INTERPRETATION,
            facts=(_fact(timestamp=NOW + timedelta(seconds=1)),),
            information_cutoff=NOW,
        )
    with pytest.raises(AIValidationError, match="unique"):
        _ai(provider).generate(
            task=AITask.MARKET_INTERPRETATION,
            facts=(_fact(), _fact()),
            information_cutoff=NOW,
        )
    assert provider.requests == []


@pytest.mark.parametrize("confidence", (-0.1, 1.1, float("nan"), "high", True))
def test_confidence_validation_is_strict(confidence: object) -> None:
    output = {
        "observed_facts": [],
        "interpretations": [],
        "hypotheses": [],
        "contradictions": [],
        "unknowns": [],
        "evidence_requests": [],
        "confidence": confidence,
    }
    with pytest.raises(AIValidationError):
        _ai(MockProvider(output=output)).generate(
            task=AITask.MARKET_INTERPRETATION,
            facts=(),
            information_cutoff=NOW,
        )


def test_manual_qwen_failure_fixture_cannot_promote_an_invented_cause_to_fact() -> None:
    values = (
        ("btc-return", "BTC.return", -1.2, "%"),
        ("eth-return", "ETH.return", -2.1, "%"),
        ("sol-return", "SOL.return", -3.8, "%"),
        ("funding", "BTC.funding", "positive", None),
        ("oi", "BTC.open_interest", "rising", None),
        ("breadth", "universe.positive_breadth", "2/8", None),
    )
    facts = tuple(FactV1(i, "REGRESSION", NOW, f, v, u, "fixture") for i, f, v, u in values)
    provider = MockProvider(
        output={
            "observed_facts": [{"fact_id": item.fact_id} for item in facts],
            "interpretations": [
                {
                    "statement": "Price weakness coexists with positive funding and rising OI.",
                    "supporting_fact_ids": ["btc-return", "funding", "oi"],
                }
            ],
            "hypotheses": [
                {
                    "claim": "A technical correction may be occurring.",
                    "supporting_fact_ids": ["btc-return", "eth-return", "sol-return"],
                    "status": "UNVALIDATED",
                }
            ],
            "contradictions": [],
            "unknowns": ["The cause of the price decline is unknown."],
            "evidence_requests": ["Request causal market evidence."],
            "confidence": 0.25,
        }
    )
    result = (
        _ai(provider)
        .generate(
            task=AITask.MARKET_INTERPRETATION,
            facts=facts,
            information_cutoff=NOW,
            created_at=NOW,
        )
        .structured_output
    )
    assert all(item.fact_id in {fact.fact_id for fact in facts} for item in result.observed_facts)
    assert result.hypotheses[0].status == "UNVALIDATED"
    assert "technical correction" not in {item.fact_id for item in result.observed_facts}


def test_historical_event_adapter_preserves_cutoff_provenance_and_digest() -> None:
    cutoff = int(NOW.timestamp() * 1000)
    context = HistoricalEventAIContextV1(
        "event-1",
        {"event_id": "event-1", "market_known_at_ms": cutoff - 1000},
        {"anchor_timestamp_ms": cutoff, "assets": {"BTCUSDT": {"close": 60000.0}}},
        cutoff,
        "context-digest",
    )
    first = facts_from_historical_event_context(context)
    second = facts_from_historical_event_context(context)
    assert first == second
    assert max(fact.timestamp for fact in first) <= NOW
    assert {fact.provenance_digest for fact in first} == {"context-digest"}
    assert historical_context_metadata(context) == historical_context_metadata(context)
    assert "outcome" not in json.dumps(historical_context_metadata(context))


def test_historical_event_adapter_rejects_inconsistent_anchor() -> None:
    context = HistoricalEventAIContextV1(
        "event-1", {}, {"anchor_timestamp_ms": 1, "assets": {}}, 2, "digest"
    )
    with pytest.raises(AIValidationError, match="anchor"):
        facts_from_historical_event_context(context)


def test_request_and_output_digests_are_deterministic() -> None:
    first_provider = MockProvider()
    second_provider = MockProvider()
    first = _ai(first_provider).generate(
        task=AITask.MARKET_INTERPRETATION,
        facts=(_fact(),),
        information_cutoff=NOW,
        request_id="one",
        created_at=NOW,
    )
    second = _ai(second_provider).generate(
        task=AITask.MARKET_INTERPRETATION,
        facts=(_fact(),),
        information_cutoff=NOW,
        request_id="two",
        created_at=NOW,
    )
    assert first.input_digest == second.input_digest
    assert first.output_digest == second.output_digest


def test_ollama_normalizes_fixture_and_excludes_thinking() -> None:
    output = {
        "observed_facts": [{"fact_id": "btc-return"}],
        "interpretations": [],
        "hypotheses": [],
        "contradictions": [],
        "unknowns": [],
        "evidence_requests": [],
        "confidence": 0.5,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["format"] == REASONING_OUTPUT_JSON_SCHEMA
        assert body["think"] is False
        return httpx.Response(
            200,
            json={
                "model": "qwen3:8b",
                "message": {"content": json.dumps(output), "thinking": "private chain"},
                "thinking": "also private",
                "prompt_eval_count": 11,
                "eval_count": 7,
                "done_reason": "stop",
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://ollama.test")
    provider = OllamaProvider(client=client)
    result = ChappieAI(ModelRouter({ModelProfile.LOCAL_DEFAULT: provider})).generate(
        task=AITask.MARKET_INTERPRETATION,
        facts=(_fact(),),
        information_cutoff=NOW,
        created_at=NOW,
    )
    assert (result.input_tokens, result.output_tokens) == (11, 7)
    assert "thinking" not in str(result.provider_metadata).lower()
    client.close()


def test_ollama_invalid_json_is_provider_error() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json={"message": {"content": "not-json"}})
    )
    client = httpx.Client(transport=transport, base_url="http://ollama.test")
    provider = OllamaProvider(client=client)
    with pytest.raises(AIProviderError):
        ChappieAI(ModelRouter({ModelProfile.LOCAL_DEFAULT: provider})).generate(
            task=AITask.MARKET_INTERPRETATION,
            facts=(_fact(),),
            information_cutoff=NOW,
        )
    client.close()
