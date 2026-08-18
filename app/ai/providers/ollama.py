"""Local Ollama provider; provider details never cross this adapter boundary."""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from app.ai.errors import AIProviderError, AIProviderTimeoutError
from app.ai.models import ModelRequest, ProviderResult


class OllamaProvider:
    def __init__(
        self,
        *,
        endpoint: str = "http://localhost:11434",
        model: str = "qwen3:8b",
        timeout_seconds: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self._owns_client = client is None
        self._client = client or httpx.Client(base_url=self.endpoint, timeout=timeout_seconds)

    @property
    def provider_identity(self) -> str:
        return "ollama/local"

    @property
    def model_identity(self) -> str:
        return self.model

    def is_ready(self) -> bool:
        try:
            response = self._client.get("/api/tags", timeout=self.timeout_seconds)
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    def generate(self, request: ModelRequest) -> ProviderResult:
        facts = [fact.payload() for fact in request.facts]
        body = {
            "model": self.model,
            "stream": False,
            "think": False,
            "format": request.response_schema,
            "options": {"temperature": request.temperature},
            "messages": [
                {"role": "system", "content": request.system_instruction},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "task": request.task.value,
                            "information_cutoff": request.information_cutoff.isoformat(),
                            "facts": facts,
                            "context": request.context,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                },
            ],
        }
        started = time.monotonic()
        try:
            response = self._client.post("/api/chat", json=body, timeout=request.timeout_seconds)
            response.raise_for_status()
        except httpx.TimeoutException as error:
            raise AIProviderTimeoutError("Ollama request timed out") from error
        except httpx.HTTPError as error:
            raise AIProviderError("Ollama request failed") from error
        latency_ms = round((time.monotonic() - started) * 1000)
        try:
            payload = response.json()
            content = payload["message"]["content"]
            structured = json.loads(content)
        except (ValueError, KeyError, TypeError) as error:
            raise AIProviderError("Ollama returned invalid structured JSON") from error
        # Provider-specific thinking is intentionally ignored. It is never canonical metadata.
        metadata: list[tuple[str, str]] = []
        if done_reason := payload.get("done_reason"):
            metadata.append(("done_reason", str(done_reason)))
        return ProviderResult(
            structured,
            str(payload.get("model", self.model)),
            str(payload.get("model", self.model)),
            latency_ms,
            _integer(payload.get("prompt_eval_count")),
            _integer(payload.get("eval_count")),
            tuple(metadata),
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
