"""Bounded localhost-only Ollama health probe; never generates model output."""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum

import httpx


class OllamaReadinessStateV1(StrEnum):
    READY = "READY"
    NOT_READY = "NOT_READY"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class OllamaReadinessResultV1:
    state: OllamaReadinessStateV1
    endpoint: str
    latency_ms: int | None = None
    error_category: str | None = None


class OllamaReadinessProbe:
    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        endpoint: str = "http://localhost:11434",
        timeout_seconds: float = 2.0,
    ) -> None:
        if not endpoint.startswith("http://localhost:"):
            raise ValueError("Ollama readiness endpoint must be localhost")
        self.endpoint = endpoint.rstrip("/")
        self.client = client or httpx.Client(base_url=self.endpoint, timeout=timeout_seconds)
        self.owns = client is None

    def probe(self) -> OllamaReadinessResultV1:
        started = time.monotonic()
        try:
            response = self.client.get("/api/tags")
            if response.status_code != 200:
                return OllamaReadinessResultV1(
                    OllamaReadinessStateV1.ERROR, self.endpoint, error_category="HTTP_STATUS"
                )
            response.json()
            return OllamaReadinessResultV1(
                OllamaReadinessStateV1.READY,
                self.endpoint,
                round((time.monotonic() - started) * 1000),
            )
        except httpx.TimeoutException:
            return OllamaReadinessResultV1(
                OllamaReadinessStateV1.NOT_READY, self.endpoint, error_category="TIMEOUT"
            )
        except httpx.HTTPError:
            return OllamaReadinessResultV1(
                OllamaReadinessStateV1.NOT_READY, self.endpoint, error_category="UNAVAILABLE"
            )
        except ValueError:
            return OllamaReadinessResultV1(
                OllamaReadinessStateV1.ERROR, self.endpoint, error_category="MALFORMED_RESPONSE"
            )

    def close(self) -> None:
        if self.owns:
            self.client.close()
