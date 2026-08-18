"""Deterministic offline model provider used by contract tests."""

from enum import StrEnum

from app.ai.errors import AIProviderError, AIProviderTimeoutError
from app.ai.models import ModelRequest, ProviderResult


class MockMode(StrEnum):
    VALID = "VALID"
    INVALID_SCHEMA = "INVALID_SCHEMA"
    ERROR = "ERROR"
    TIMEOUT = "TIMEOUT"
    UNKNOWN_FACT = "UNKNOWN_FACT"
    VALIDATED_HYPOTHESIS = "VALIDATED_HYPOTHESIS"


class MockProvider:
    def __init__(self, mode: MockMode = MockMode.VALID, output: object | None = None) -> None:
        self.mode = mode
        self.output = output
        self.requests: list[ModelRequest] = []

    @property
    def provider_identity(self) -> str:
        return "mock/1.0.0"

    @property
    def model_identity(self) -> str:
        return "deterministic-mock/1.0.0"

    def is_ready(self) -> bool:
        return self.mode not in {MockMode.ERROR, MockMode.TIMEOUT}

    def generate(self, request: ModelRequest) -> ProviderResult:
        self.requests.append(request)
        if self.mode == MockMode.TIMEOUT:
            raise AIProviderTimeoutError("mock provider timed out")
        if self.mode == MockMode.ERROR:
            raise AIProviderError("mock provider failed")
        if self.output is not None:
            output = self.output
        else:
            fact_id = request.facts[0].fact_id if request.facts else ""
            output = {
                "observed_facts": [{"fact_id": fact_id}] if fact_id else [],
                "interpretations": [],
                "hypotheses": [],
                "contradictions": [],
                "unknowns": [],
                "evidence_requests": [],
                "confidence": 0.5,
            }
        if self.mode == MockMode.INVALID_SCHEMA:
            output = {"answer": "not the contract"}
        elif self.mode == MockMode.UNKNOWN_FACT:
            output = {**output, "observed_facts": [{"fact_id": "hallucinated"}]}
        elif self.mode == MockMode.VALIDATED_HYPOTHESIS:
            output = {
                **output,
                "hypotheses": [
                    {"claim": "unsupported", "supporting_fact_ids": [], "status": "SUPPORTED"}
                ],
            }
        return ProviderResult(output, "deterministic-mock", "1.0.0", 0)

    def close(self) -> None:
        return None
