"""Opt-in local Ollama critique of an immutable RDE evidence artifact.

This tool is research-only. It reads one evidence artifact and one original
hypothesis file, runs no RDE query, writes no canonical evidence, and cannot
place or recommend orders.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from app.ai import AIConfig, ChappieAI, ModelProfile
from app.ai.providers import OllamaProvider
from app.ai.research.evidence_review import (
    OriginalHypothesisV1,
    context_from_evidence_artifact,
)
from app.ai.router import ModelRouter


def _cutoff(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("invalid ISO-8601 information cutoff") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("information cutoff must be timezone-aware")
    return parsed.astimezone(UTC)


def _hypothesis(path: Path) -> OriginalHypothesisV1:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return OriginalHypothesisV1(
            str(payload["hypothesis_id"]),
            str(payload["claim"]),
            float(payload["confidence"]),
            str(payload["intent_id"]) if payload.get("intent_id") is not None else None,
        )
    except (KeyError, OSError, TypeError, ValueError) as error:
        raise argparse.ArgumentTypeError("hypothesis artifact is invalid") from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("ollama",), required=True)
    parser.add_argument("--model", default="qwen3:8b")
    parser.add_argument("--endpoint", default="http://localhost:11434")
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--hypothesis", type=Path, required=True)
    parser.add_argument("--information-cutoff", type=_cutoff, required=True)
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        original = _hypothesis(args.hypothesis)
        context = context_from_evidence_artifact(
            args.evidence,
            original_hypothesis=original,
            information_cutoff=args.information_cutoff,
        )
    except (argparse.ArgumentTypeError, ValueError) as error:
        print(f"INVALID_INPUT: {error}")
        return 2
    provider = OllamaProvider(
        endpoint=args.endpoint,
        model=args.model,
        timeout_seconds=args.timeout_seconds,
    )
    ai = ChappieAI(
        ModelRouter({ModelProfile.LOCAL_DEFAULT: provider}),
        AIConfig(
            profile=ModelProfile.LOCAL_DEFAULT,
            endpoint=args.endpoint,
            model=args.model,
            timeout_seconds=args.timeout_seconds,
        ),
    )
    try:
        run = ai.review_research_evidence(context=context)
    finally:
        ai.close()
    if args.verbose:
        print("RDE EVIDENCE")
        print(f"Status: {context.evidence.evidence_status}")
        print(f"Incremental mean: {context.evidence.incremental_mean} bps")
        print(f"Incremental median: {context.evidence.incremental_median} bps")
        print("\nCHAPPIE AI REVIEW")
        print(f"Original confidence: {run.review.confidence_before}")
        print(f"Revised confidence: {run.review.confidence_after}")
        print(f"Assessment: {run.review.assessment.value}")
    print(json.dumps(run.payload(), sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
