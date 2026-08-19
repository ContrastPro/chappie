"""Current-user Ollama start with bounded readiness polling."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from app.ollama_readiness import OllamaReadinessResultV1, OllamaReadinessStateV1


@dataclass(frozen=True, slots=True)
class OllamaRetryPolicyV1:
    max_attempts: int = 5
    interval_seconds: float = 0.5


class Probe(Protocol):
    def probe(self) -> OllamaReadinessResultV1: ...


class OllamaReadinessCoordinatorV1:
    def __init__(
        self,
        probe: Probe,
        executable: str,
        launcher: Callable[[list[str]], None],
        *,
        policy: OllamaRetryPolicyV1 | None = None,
        sleeper: Callable[[float], None] = lambda _: None,
    ) -> None:
        self.probe, self.executable, self.launcher, self.policy, self.sleeper = (
            probe,
            executable,
            launcher,
            policy or OllamaRetryPolicyV1(),
            sleeper,
        )

    def ensure_ready(self) -> OllamaReadinessResultV1:
        result = self.probe.probe()
        if result.state == OllamaReadinessStateV1.READY:
            return result
        if result.state == OllamaReadinessStateV1.ERROR:
            return result
        try:
            self.launcher([self.executable, "serve"])
        except Exception:  # noqa: BLE001 - injected launcher has no narrower exception contract
            return OllamaReadinessResultV1(
                OllamaReadinessStateV1.ERROR,
                "http://localhost:11434",
                error_category="START_FAILED",
            )
        for _ in range(self.policy.max_attempts):
            self.sleeper(self.policy.interval_seconds)
            result = self.probe.probe()
            if result.state == OllamaReadinessStateV1.READY:
                return result
        return OllamaReadinessResultV1(
            OllamaReadinessStateV1.NOT_READY,
            "http://localhost:11434",
            error_category="START_TIMEOUT",
        )
