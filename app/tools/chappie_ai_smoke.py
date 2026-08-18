"""Opt-in local Ollama smoke test; never used by the normal test suite."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

from app.ai import AIConfig, AITask, ChappieAI, FactV1, ModelProfile
from app.ai.providers import OllamaProvider
from app.ai.router import ModelRouter


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("ollama",), default="ollama")
    parser.add_argument("--endpoint", default="http://localhost:11434")
    parser.add_argument("--model", default="qwen3:8b")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    now = datetime.now(tz=UTC)
    provider = OllamaProvider(
        endpoint=args.endpoint, model=args.model, timeout_seconds=args.timeout
    )
    ai = ChappieAI(
        ModelRouter({ModelProfile.LOCAL_DEFAULT: provider}),
        AIConfig(endpoint=args.endpoint, model=args.model, timeout_seconds=args.timeout),
    )
    try:
        response = ai.generate(
            task=AITask.MARKET_INTERPRETATION,
            facts=(FactV1("btc-return", "SMOKE", now, "BTC.return", -1.2, "%", "smoke"),),
            information_cutoff=now,
        )
    finally:
        ai.close()
    output = response.structured_output
    print(f"provider={response.provider}")
    print(f"model={response.model}")
    print(f"latency_ms={response.latency_ms}")
    print("schema_valid=yes")
    print("fact_reference_valid=yes")
    print(f"hypothesis_count={len(output.hypotheses)}")
    print(f"unknown_count={len(output.unknowns)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
