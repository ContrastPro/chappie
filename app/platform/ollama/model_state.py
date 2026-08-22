"""Read-only local Ollama model-state inspection."""

from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass(frozen=True, slots=True)
class LocalOllamaModelStateV1:
    installed_models: tuple[str, ...]
    runtime_ready: bool


class LocalOllamaModelStateAdapterV1:
    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def read(self) -> LocalOllamaModelStateV1:
        try:
            response = self._client.get("/api/tags")
            if response.status_code != 200:
                return LocalOllamaModelStateV1((), False)
            payload = response.json()
            models = payload.get("models") if isinstance(payload, dict) else None
            if not isinstance(models, list):
                return LocalOllamaModelStateV1((), False)
            names = tuple(
                sorted(
                    item["name"]
                    for item in models
                    if isinstance(item, dict) and isinstance(item.get("name"), str)
                )
            )
            return LocalOllamaModelStateV1(names, True)
        except (httpx.HTTPError, ValueError):
            return LocalOllamaModelStateV1((), False)
