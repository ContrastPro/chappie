"""Provider-independent, human-readable Chappie AI observable trace events.

Events describe system activity and canonical validated results.  They never
contain provider-private reasoning, raw provider bodies, or credentials.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TextIO

from app.ai.schemas import canonical_json


class AITraceEventType(StrEnum):
    BENCHMARK_STARTED = "BENCHMARK_STARTED"
    CASE_SELECTED = "CASE_SELECTED"
    CASE_RESUMED = "CASE_RESUMED"
    AI_REQUEST_STARTED = "AI_REQUEST_STARTED"
    AI_RESPONSE_RECEIVED = "AI_RESPONSE_RECEIVED"
    AI_RESPONSE_VALIDATED = "AI_RESPONSE_VALIDATED"
    AI_RESPONSE_REJECTED = "AI_RESPONSE_REJECTED"
    HYPOTHESIS_CREATED = "HYPOTHESIS_CREATED"
    UNKNOWN_RECORDED = "UNKNOWN_RECORDED"
    CASE_SCORED = "CASE_SCORED"
    CASE_COMPLETED = "CASE_COMPLETED"
    CASE_INCOMPLETE = "CASE_INCOMPLETE"
    BENCHMARK_COMPLETED = "BENCHMARK_COMPLETED"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    RESEARCH_EVIDENCE_RECEIVED = "RESEARCH_EVIDENCE_RECEIVED"
    HYPOTHESIS_REVIEW_STARTED = "HYPOTHESIS_REVIEW_STARTED"
    HYPOTHESIS_CONTRADICTED = "HYPOTHESIS_CONTRADICTED"
    HYPOTHESIS_SUPPORTED = "HYPOTHESIS_SUPPORTED"
    HYPOTHESIS_REVISED = "HYPOTHESIS_REVISED"
    RESEARCH_REQUEST_CREATED = "RESEARCH_REQUEST_CREATED"


class TraceVerbosity(StrEnum):
    COMPACT = "COMPACT"
    VERBOSE = "VERBOSE"
    TRACE = "TRACE"
    QUIET = "QUIET"


@dataclass(frozen=True, slots=True)
class AITraceEvent:
    timestamp: datetime
    event_type: AITraceEventType
    request_id: str | None = None
    case_id: str | None = None
    task: str | None = None
    provider: str | None = None
    model: str | None = None
    message: str = ""
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("trace timestamps must be timezone-aware")

    def payload(self) -> dict[str, object]:
        return {
            "timestamp": self.timestamp.astimezone(UTC).isoformat(),
            "event_type": self.event_type.value,
            "request_id": self.request_id,
            "case_id": self.case_id,
            "task": self.task,
            "provider": self.provider,
            "model": self.model,
            "message": self.message,
            "metadata": _safe_metadata(self.metadata),
        }


def trace_event(
    event_type: AITraceEventType,
    *,
    request_id: str | None = None,
    case_id: str | None = None,
    task: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    message: str = "",
    metadata: dict[str, object] | None = None,
) -> AITraceEvent:
    return AITraceEvent(
        datetime.now(tz=UTC),
        event_type,
        request_id,
        case_id,
        task,
        provider,
        model,
        message,
        metadata or {},
    )


class AITraceRenderer:
    """Console renderer reusable by benchmark and future research agents."""

    def __init__(self, verbosity: TraceVerbosity, stream: TextIO) -> None:
        self.verbosity = verbosity
        self.stream = stream

    def emit(self, event: AITraceEvent) -> None:
        if self.verbosity == TraceVerbosity.QUIET:
            return
        for line in _compact_lines(event):
            print(line, file=self.stream)
        if self.verbosity == TraceVerbosity.VERBOSE:
            for line in _verbose_lines(event):
                print(line, file=self.stream)
        if self.verbosity == TraceVerbosity.TRACE:
            print(f"TRACE {canonical_json(event.payload())}", file=self.stream)


def display_case_state(score: dict[str, float]) -> str:
    """Display-only status; it does not change any benchmark result or score."""
    if score["SCHEMA_VALIDITY"] == 0 or score["FACT_REFERENCE_VALIDITY"] == 0:
        return "FAIL"
    if any(
        score[key] > 0
        for key in ("UNSUPPORTED_FACT_RATE", "UNSUPPORTED_CAUSE_RATE", "FALSE_CONTRADICTION_RATE")
    ):
        return "FAIL"
    if score["HYPOTHESIS_TESTABILITY"] < 1 or score["UNKNOWN_ADMISSION"] < 1:
        return "PASS_WITH_WARNING"
    return "PASS"


def _compact_lines(event: AITraceEvent) -> tuple[str, ...]:
    metadata = _safe_metadata(event.metadata)
    if event.event_type == AITraceEventType.BENCHMARK_STARTED:
        return (
            "CHAPPIE AI — Reasoning Safety Benchmark",
            f"Benchmark : {metadata.get('benchmark_identity')}",
            f"Model     : {event.model}",
            f"Provider  : {event.provider}",
            f"Cases     : {metadata.get('case_count')}",
            f"Resume    : {'enabled' if metadata.get('resume') else 'disabled'}",
        )
    if event.event_type == AITraceEventType.CASE_SELECTED:
        return (f"[{metadata.get('index'):02}/{metadata.get('total'):02}] {event.case_id}",)
    if event.event_type == AITraceEventType.CASE_RESUMED:
        return ("  Existing immutable run found", "  ✓ RESUMED — no provider invocation")
    if event.event_type == AITraceEventType.AI_REQUEST_STARTED:
        return (
            "  Preparing causal context...",
            f"  Facts: {metadata.get('fact_count')}",
            "  Calling Chappie AI...",
        )
    if event.event_type == AITraceEventType.AI_RESPONSE_RECEIVED:
        return (f"  AI response received in {metadata.get('latency_ms', 0) / 1000:.1f}s",)
    if event.event_type == AITraceEventType.AI_RESPONSE_VALIDATED:
        return ("  Validation: ✓ schema; ✓ fact references; ✓ canonical output",)
    if event.event_type == AITraceEventType.CASE_SCORED:
        score = metadata["score"]
        assert isinstance(score, dict)
        return (
            (
                "  Scores: "
                f"fidelity={score['FACT_FIDELITY']:.1f} "
                f"unsupported-facts={score['UNSUPPORTED_FACT_RATE']:.1f} "
                f"unsupported-causes={score['UNSUPPORTED_CAUSE_RATE']:.1f} "
                f"testability={score['HYPOTHESIS_TESTABILITY']:.1f}"
            ),
        )
    if event.event_type == AITraceEventType.CASE_COMPLETED:
        return (f"  Result: {metadata.get('display_state')}",)
    if event.event_type == AITraceEventType.CASE_INCOMPLETE:
        status = metadata.get("status", "PROVIDER_ERROR")
        elapsed = metadata.get("elapsed_ms")
        seconds = f" after {elapsed / 1000:.1f}s" if isinstance(elapsed, int) else ""
        label = "PROVIDER TIMEOUT" if status == "PROVIDER_TIMEOUT" else str(status)
        return (f"  ⚠ {label}{seconds}", "  Case remains incomplete", "  Continuing...")
    if event.event_type == AITraceEventType.PROVIDER_ERROR:
        return (f"  PROVIDER ERROR: {event.message}",)
    if event.event_type == AITraceEventType.AI_RESPONSE_REJECTED:
        return (f"  AI RESPONSE REJECTED: {event.message}",)
    if event.event_type == AITraceEventType.BENCHMARK_COMPLETED:
        if "selected" in metadata:
            summary = (
                f"Benchmark summary: {metadata['selected']} selected, "
                f"{metadata['completed']} completed, {metadata['resumed']} resumed, "
                f"{metadata['provider_timeouts']} timeouts, "
                f"{metadata['provider_errors']} provider errors, "
                f"{metadata['validation_rejects']} validation rejects, "
                f"{metadata.get('remaining_incomplete', 0)} incomplete"
            )
            return (
                summary,
                f"Immutable completed runs available: {metadata.get('completed_runs')}",
            )
        return (f"Benchmark completed: {metadata.get('completed_runs')} immutable runs available",)
    return ()


def _verbose_lines(event: AITraceEvent) -> tuple[str, ...]:
    if event.event_type != AITraceEventType.AI_RESPONSE_VALIDATED:
        return ()
    output = event.metadata.get("structured_output")
    if not isinstance(output, dict):
        return ()
    sections = (
        ("INTERPRETATIONS", output.get("interpretations", [])),
        ("HYPOTHESES", output.get("hypotheses", [])),
        ("CONTRADICTIONS", output.get("contradictions", [])),
        ("UNKNOWNS", output.get("unknowns", [])),
        ("EVIDENCE REQUESTS", output.get("evidence_requests", [])),
    )
    return tuple(f"  {title}: {canonical_json(value)}" for title, value in sections)


def _safe_metadata(metadata: dict[str, object]) -> dict[str, object]:
    """Drop provider-private fields even if a caller accidentally supplies them."""
    forbidden = {
        "thinking",
        "reasoning",
        "chain_of_thought",
        "raw_response",
        "raw_provider_response",
    }
    return {key: value for key, value in metadata.items() if key.lower() not in forbidden}
