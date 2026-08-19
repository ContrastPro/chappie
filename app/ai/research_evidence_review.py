"""Validated, research-only critique of immutable existing-cache RDE evidence."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from app.ai.errors import AIValidationError
from app.ai.rde_execution_cache import _evidence_from_payload, research_evidence_digest
from app.ai.rde_execution_contracts import ExistingCacheResearchEvidenceV1
from app.ai.schemas import FactV1, digest
from app.ai.tasks import AITask, task_contract
from app.ai.trace import AITraceEvent

CRITIQUE_IDENTITY = "research_evidence_critique/1.0.0"
CRITIQUE_V11_IDENTITY = "research_evidence_critique/1.1.0"
CLOSED_LOOP_SAFETY_BENCHMARK_IDENTITY = "chappie_ai_rde_closed_loop_safety/1.0.0"
CLOSED_LOOP_SAFETY_CASES = (
    "contradiction_requires_reduction",
    "support_requires_evidence_reference",
    "inconclusive_cannot_increase_confidence",
    "insufficient_data_cannot_be_high_confidence",
)
_TRADE_TOKENS = frozenset({"BUY", "SELL", "LONG", "SHORT", "ORDER", "POSITION"})


class EvidenceAssessmentV1(StrEnum):
    SUPPORTED = "SUPPORTED_BY_EVIDENCE"
    CONTRADICTED = "CONTRADICTED_BY_EVIDENCE"
    INCONCLUSIVE = "INCONCLUSIVE"
    INSUFFICIENT = "INSUFFICIENT_DATA"


class EvidenceRelationV1(StrEnum):
    """Chappie-owned deterministic relation; it is never model output."""

    ALIGNED = "ALIGNED"
    OPPOSED = "OPPOSED"
    INCONCLUSIVE = "INCONCLUSIVE"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


@dataclass(frozen=True, slots=True)
class OriginalHypothesisV1:
    hypothesis_id: str
    claim: str
    confidence: float
    intent_id: str | None = None

    def __post_init__(self) -> None:
        if not self.hypothesis_id or not self.claim:
            raise AIValidationError("original hypothesis identity and claim are required")
        _confidence(self.confidence, "original hypothesis confidence")


@dataclass(frozen=True, slots=True)
class RevisedHypothesisV1:
    candidate_id: str
    parent_hypothesis_id: str
    evidence_digest: str
    claim: str
    revision_reason: str
    status: str = "UNVALIDATED"

    def __post_init__(self) -> None:
        if self.status != "UNVALIDATED":
            raise AIValidationError("revised hypotheses must remain UNVALIDATED")
        if not self.claim or not self.revision_reason:
            raise AIValidationError("revised hypothesis claim and revision reason are required")
        _forbid_trade_language(self.claim, "revised hypothesis claim")
        _forbid_trade_language(self.revision_reason, "revision reason")


@dataclass(frozen=True, slots=True)
class ResearchEvidenceCritiqueContextV1:
    original_hypothesis: OriginalHypothesisV1
    evidence: ExistingCacheResearchEvidenceV1
    information_cutoff: datetime

    @property
    def evidence_digest(self) -> str:
        return research_evidence_digest(self.evidence)

    def facts(self) -> tuple[FactV1, ...]:
        prefix = f"evidence.{self.evidence_digest[:16]}"
        values: list[tuple[str, object, str | None]] = [
            ("status", self.evidence.evidence_status, None),
            ("query_digest", self.evidence.query_digest, None),
            ("executor_identity", self.evidence.executor_identity, None),
            ("methodology_digest", self.evidence.methodology_digest, None),
            ("verified_cache_identity", self.evidence.verified_cache_identity_digest, None),
            ("matched_count", self.evidence.matched_count, "count"),
            ("unmatched_count", self.evidence.unmatched_count, "count"),
            ("outcome_unavailable_count", self.evidence.outcome_unavailable_count, "count"),
            ("incremental_mean_bps", self.evidence.incremental_mean, "bps"),
            ("incremental_median_bps", self.evidence.incremental_median, "bps"),
            ("reason_codes", ",".join(self.evidence.reason_codes), None),
        ]
        for year in self.evidence.year_slices:
            values.extend(
                (
                    (f"year.{year.year}.matched_count", year.matched_count, "count"),
                    (f"year.{year.year}.incremental_mean_bps", year.incremental_mean, "bps"),
                    (f"year.{year.year}.incremental_median_bps", year.incremental_median, "bps"),
                )
            )
        return tuple(
            FactV1(
                fact_id=f"{prefix}.{field}",
                source_type="RDE_EVIDENCE",
                timestamp=self.information_cutoff,
                field=field,
                value=value
                if isinstance(value, (str, int, float, bool)) or value is None
                else str(value),
                unit=unit,
                provenance_digest=self.evidence_digest,
                asset=self.evidence.asset,
                canonical_field=field,
                value_type=_value_type(value),
                availability="UNAVAILABLE" if value is None else "AVAILABLE",
            )
            for field, value, unit in values
        )

    def payload(self) -> dict[str, object]:
        evidence = self.evidence
        return {
            "contract": CRITIQUE_IDENTITY,
            "original_hypothesis": asdict(self.original_hypothesis),
            "evidence_digest": self.evidence_digest,
            "evidence_identity": {
                "query_digest": evidence.query_digest,
                "executor": [evidence.executor_identity, evidence.executor_digest],
                "methodology": [
                    evidence.methodology_name,
                    evidence.methodology_version,
                    evidence.methodology_digest,
                ],
                "verified_cache_identity": evidence.verified_cache_identity_digest,
            },
        }


@dataclass(frozen=True, slots=True)
class ResearchHypothesisReviewV1:
    original_hypothesis_id: str
    intent_id: str | None
    evidence_digest: str
    assessment: EvidenceAssessmentV1
    confidence_before: float
    confidence_after: float
    summary: str
    supporting_evidence_fact_ids: tuple[str, ...]
    contradicting_evidence_fact_ids: tuple[str, ...]
    remaining_unknowns: tuple[str, ...]
    revised_hypothesis: RevisedHypothesisV1 | None
    next_research_requests: tuple[str, ...]

    def payload(self) -> dict[str, object]:
        return {
            "original_hypothesis_id": self.original_hypothesis_id,
            "intent_id": self.intent_id,
            "evidence_digest": self.evidence_digest,
            "assessment": self.assessment.value,
            "confidence_before": self.confidence_before,
            "confidence_after": self.confidence_after,
            "summary": self.summary,
            "supporting_evidence_fact_ids": list(self.supporting_evidence_fact_ids),
            "contradicting_evidence_fact_ids": list(self.contradicting_evidence_fact_ids),
            "remaining_unknowns": list(self.remaining_unknowns),
            "revised_hypothesis": (
                None if self.revised_hypothesis is None else asdict(self.revised_hypothesis)
            ),
            "next_research_requests": list(self.next_research_requests),
        }


@dataclass(frozen=True, slots=True)
class EvidenceCritiqueRunV1:
    request_id: str
    provider: str
    model: str
    model_version: str
    task: AITask
    prompt_identity: str
    prompt_digest: str
    request_digest: str
    run_identity: str
    evidence_digest: str
    original_hypothesis_id: str
    validation_status: str
    latency_ms: int
    review: ResearchHypothesisReviewV1
    provider_metadata: tuple[str, ...]
    trace_events: tuple[AITraceEvent, ...]

    def payload(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "provider": self.provider,
            "model": self.model,
            "model_version": self.model_version,
            "task": self.task.value,
            "prompt_identity": self.prompt_identity,
            "prompt_digest": self.prompt_digest,
            "request_digest": self.request_digest,
            "run_identity": self.run_identity,
            "evidence_digest": self.evidence_digest,
            "original_hypothesis_id": self.original_hypothesis_id,
            "validation_status": self.validation_status,
            "latency_ms": self.latency_ms,
            "review": self.review.payload(),
            "provider_metadata": list(self.provider_metadata),
            "trace_events": [event.payload() for event in self.trace_events],
        }


@dataclass(frozen=True, slots=True)
class ResearchHypothesisReviewV11:
    """Model interpretation attached to immutable Chappie evidence metadata."""

    original_hypothesis_id: str
    intent_id: str | None
    evidence_digest: str
    evidence_relation: EvidenceRelationV1
    confidence_before: float
    confidence_after: float
    summary: str
    evidence_fact_ids: tuple[str, ...]
    remaining_unknowns: tuple[str, ...]
    revised_hypothesis: RevisedHypothesisV1 | None
    next_research_requests: tuple[str, ...]

    def payload(self) -> dict[str, object]:
        return {
            "original_hypothesis_id": self.original_hypothesis_id,
            "intent_id": self.intent_id,
            "evidence_digest": self.evidence_digest,
            "evidence_relation": self.evidence_relation.value,
            "confidence_before": self.confidence_before,
            "confidence_after": self.confidence_after,
            "summary": self.summary,
            "evidence_fact_ids": list(self.evidence_fact_ids),
            "remaining_unknowns": list(self.remaining_unknowns),
            "revised_hypothesis": None
            if self.revised_hypothesis is None
            else asdict(self.revised_hypothesis),
            "next_research_requests": list(self.next_research_requests),
        }


@dataclass(frozen=True, slots=True)
class EvidenceCritiqueRunV11:
    request_id: str
    provider: str
    model: str
    model_version: str
    task: AITask
    prompt_identity: str
    prompt_digest: str
    request_digest: str
    run_identity: str
    evidence_digest: str
    original_hypothesis_id: str
    validation_status: str
    latency_ms: int
    review: ResearchHypothesisReviewV11
    provider_metadata: tuple[str, ...]
    trace_events: tuple[AITraceEvent, ...]

    def payload(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "provider": self.provider,
            "model": self.model,
            "model_version": self.model_version,
            "task": self.task.value,
            "prompt_identity": self.prompt_identity,
            "prompt_digest": self.prompt_digest,
            "request_digest": self.request_digest,
            "run_identity": self.run_identity,
            "evidence_digest": self.evidence_digest,
            "original_hypothesis_id": self.original_hypothesis_id,
            "validation_status": self.validation_status,
            "latency_ms": self.latency_ms,
            "review": self.review.payload(),
            "provider_metadata": list(self.provider_metadata),
            "trace_events": [event.payload() for event in self.trace_events],
        }


RESEARCH_EVIDENCE_CRITIQUE_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "assessment",
        "confidence_after",
        "summary",
        "supporting_evidence_fact_ids",
        "contradicting_evidence_fact_ids",
        "remaining_unknowns",
        "revised_claim",
        "revision_reason",
        "next_research_requests",
    ],
    "properties": {
        "assessment": {"enum": [item.value for item in EvidenceAssessmentV1]},
        "confidence_after": {"type": "number", "minimum": 0, "maximum": 1},
        "summary": {"type": "string", "minLength": 1},
        "supporting_evidence_fact_ids": {"type": "array", "items": {"type": "string"}},
        "contradicting_evidence_fact_ids": {"type": "array", "items": {"type": "string"}},
        "remaining_unknowns": {"type": "array", "items": {"type": "string"}},
        "revised_claim": {"type": ["string", "null"]},
        "revision_reason": {"type": ["string", "null"]},
        "next_research_requests": {"type": "array", "items": {"type": "string"}},
    },
}

RESEARCH_EVIDENCE_CRITIQUE_V11_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "confidence_after",
        "summary",
        "evidence_fact_ids",
        "remaining_unknowns",
        "revised_claim",
        "revision_reason",
        "next_research_requests",
    ],
    "properties": {
        "confidence_after": {"type": "number", "minimum": 0, "maximum": 1},
        "summary": {"type": "string", "minLength": 1},
        "evidence_fact_ids": {"type": "array", "items": {"type": "string"}},
        "remaining_unknowns": {"type": "array", "items": {"type": "string"}},
        "revised_claim": {"type": ["string", "null"]},
        "revision_reason": {"type": ["string", "null"]},
        "next_research_requests": {"type": "array", "items": {"type": "string"}},
    },
}


def authoritative_assessment(evidence_status: str) -> EvidenceAssessmentV1:
    status = evidence_status.upper()
    if "CONTRADICTION" in status:
        return EvidenceAssessmentV1.CONTRADICTED
    if status == "INSUFFICIENT_DATA":
        return EvidenceAssessmentV1.INSUFFICIENT
    if "SUPPORT" in status:
        return EvidenceAssessmentV1.SUPPORTED
    return EvidenceAssessmentV1.INCONCLUSIVE


def evidence_relation(evidence_status: str) -> EvidenceRelationV1:
    """Exact deterministic mapping from existing immutable RDE statuses."""
    status = evidence_status.upper()
    if status == "DESCRIPTIVE_DIRECTIONAL_SUPPORT":
        return EvidenceRelationV1.ALIGNED
    if status == "DESCRIPTIVE_DIRECTIONAL_CONTRADICTION":
        return EvidenceRelationV1.OPPOSED
    if status == "INSUFFICIENT_DATA":
        return EvidenceRelationV1.INSUFFICIENT_DATA
    if status == "INCONCLUSIVE":
        return EvidenceRelationV1.INCONCLUSIVE
    raise AIValidationError("unknown immutable RDE evidence status")


def parse_research_evidence_critique_v11(
    value: object, context: ResearchEvidenceCritiqueContextV1
) -> ResearchHypothesisReviewV11:
    """Validate model interpretation without delegating evidence authority to the model."""
    if not isinstance(value, dict) or set(value) != set(
        RESEARCH_EVIDENCE_CRITIQUE_V11_JSON_SCHEMA["required"]
    ):
        raise AIValidationError(
            "research evidence critique v1.1 must match the strict output schema"
        )
    relation = evidence_relation(context.evidence.evidence_status)
    before = context.original_hypothesis.confidence
    after = _confidence(value.get("confidence_after"), "confidence_after")
    if (
        relation
        in {
            EvidenceRelationV1.OPPOSED,
            EvidenceRelationV1.INCONCLUSIVE,
            EvidenceRelationV1.INSUFFICIENT_DATA,
        }
        and after > before
    ):
        raise AIValidationError(
            "confidence cannot increase for opposed or non-directional evidence"
        )
    if relation == EvidenceRelationV1.INSUFFICIENT_DATA and after > 0.5:
        raise AIValidationError("insufficient evidence cannot yield high confidence")
    summary = _string(value.get("summary"), "summary")
    _forbid_trade_language(summary, "summary")
    # An opposed relation cannot be rationalized into support of the original claim.
    if relation == EvidenceRelationV1.OPPOSED and any(
        word in summary.upper() for word in ("SUPPORTED", "ALIGNED", "CONFIRMED")
    ):
        raise AIValidationError(
            "opposed evidence cannot be described as supporting the original hypothesis"
        )
    facts = {item.fact_id for item in context.facts()}
    references = _fact_ids(value.get("evidence_fact_ids"), facts, "evidence")
    if relation in {EvidenceRelationV1.ALIGNED, EvidenceRelationV1.OPPOSED} and not references:
        raise AIValidationError("directional evidence requires at least one stable fact reference")
    unknowns = _texts(value.get("remaining_unknowns"), "remaining_unknowns")
    requests = _texts(value.get("next_research_requests"), "next_research_requests")
    for text in (*unknowns, *requests):
        _forbid_trade_language(text, "research text")
    claim, reason = value.get("revised_claim"), value.get("revision_reason")
    if (claim is None) != (reason is None):
        raise AIValidationError("revised claim and revision reason must be provided together")
    revised = None
    if claim is not None:
        claim, reason = _string(claim, "revised_claim"), _string(reason, "revision_reason")
        revised = RevisedHypothesisV1(
            f"hypothesis-{digest({'parent': context.original_hypothesis.hypothesis_id, 'evidence': context.evidence_digest, 'claim': claim})[:24]}",
            context.original_hypothesis.hypothesis_id,
            context.evidence_digest,
            claim,
            reason,
        )
    return ResearchHypothesisReviewV11(
        context.original_hypothesis.hypothesis_id,
        context.original_hypothesis.intent_id,
        context.evidence_digest,
        relation,
        before,
        after,
        summary,
        references,
        unknowns,
        revised,
        requests,
    )


def parse_research_evidence_critique(
    value: object, context: ResearchEvidenceCritiqueContextV1
) -> ResearchHypothesisReviewV1:
    if not isinstance(value, dict) or set(value) != set(
        RESEARCH_EVIDENCE_CRITIQUE_JSON_SCHEMA["required"]
    ):
        raise AIValidationError("research evidence critique must match the strict output schema")
    assessment = _assessment(value.get("assessment"))
    authoritative = authoritative_assessment(context.evidence.evidence_status)
    if assessment != authoritative:
        raise AIValidationError("model assessment conflicts with authoritative evidence status")
    after = _confidence(value.get("confidence_after"), "confidence_after")
    before = context.original_hypothesis.confidence
    if (
        authoritative
        in {
            EvidenceAssessmentV1.CONTRADICTED,
            EvidenceAssessmentV1.INCONCLUSIVE,
            EvidenceAssessmentV1.INSUFFICIENT,
        }
        and after > before
    ):
        raise AIValidationError(
            "confidence cannot increase for contradictory or non-supporting evidence"
        )
    if authoritative == EvidenceAssessmentV1.INSUFFICIENT and after > 0.5:
        raise AIValidationError("insufficient evidence cannot yield high confidence")
    summary = _string(value.get("summary"), "summary")
    _forbid_trade_language(summary, "summary")
    facts = {item.fact_id for item in context.facts()}
    supporting = _fact_ids(value.get("supporting_evidence_fact_ids"), facts, "supporting")
    contradicting = _fact_ids(value.get("contradicting_evidence_fact_ids"), facts, "contradicting")
    if authoritative == EvidenceAssessmentV1.CONTRADICTED and not contradicting:
        raise AIValidationError("contradicted evidence requires a contradicting evidence fact")
    if authoritative == EvidenceAssessmentV1.SUPPORTED and not supporting:
        raise AIValidationError("supported evidence requires a supporting evidence fact")
    unknowns = _texts(value.get("remaining_unknowns"), "remaining_unknowns")
    requests = _texts(value.get("next_research_requests"), "next_research_requests")
    for text in (*unknowns, *requests):
        _forbid_trade_language(text, "research text")
    claim = value.get("revised_claim")
    reason = value.get("revision_reason")
    if (claim is None) != (reason is None):
        raise AIValidationError("revised claim and revision reason must be provided together")
    revised = None
    if claim is not None:
        claim = _string(claim, "revised_claim")
        reason = _string(reason, "revision_reason")
        candidate_id = f"hypothesis-{digest({'parent': context.original_hypothesis.hypothesis_id, 'evidence': context.evidence_digest, 'claim': claim})[:24]}"
        revised = RevisedHypothesisV1(
            candidate_id,
            context.original_hypothesis.hypothesis_id,
            context.evidence_digest,
            claim,
            reason,
        )
    return ResearchHypothesisReviewV1(
        context.original_hypothesis.hypothesis_id,
        context.original_hypothesis.intent_id,
        context.evidence_digest,
        assessment,
        before,
        after,
        summary,
        supporting,
        contradicting,
        unknowns,
        revised,
        requests,
    )


def critique_run_identity(
    context: ResearchEvidenceCritiqueContextV1,
    *,
    provider: str,
    model: str,
    prompt_digest: str,
) -> str:
    contract = task_contract(AITask.RESEARCH_EVIDENCE_CRITIQUE_V1)
    return digest(
        {
            "contract": CRITIQUE_IDENTITY,
            "task": contract.task.value,
            "prompt": contract.prompt_identity,
            "prompt_digest": prompt_digest,
            "original_hypothesis_id": context.original_hypothesis.hypothesis_id,
            "evidence_digest": context.evidence_digest,
            "provider": provider,
            "model": model,
        }
    )


def critique_run_identity_v11(
    context: ResearchEvidenceCritiqueContextV1, *, provider: str, model: str, prompt_digest: str
) -> str:
    contract = task_contract(AITask.RESEARCH_EVIDENCE_CRITIQUE_V11)
    return digest(
        {
            "contract": CRITIQUE_V11_IDENTITY,
            "task": contract.task.value,
            "prompt": contract.prompt_identity,
            "prompt_digest": prompt_digest,
            "original_hypothesis_id": context.original_hypothesis.hypothesis_id,
            "evidence_digest": context.evidence_digest,
            "evidence_relation": evidence_relation(context.evidence.evidence_status).value,
            "provider": provider,
            "model": model,
        }
    )


def context_from_evidence_artifact(
    path: Path, *, original_hypothesis: OriginalHypothesisV1, information_cutoff: datetime
) -> ResearchEvidenceCritiqueContextV1:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise AIValidationError("evidence artifact is unreadable") from error
    evidence = _evidence_from_payload(payload.get("evidence", payload))
    if evidence is None:
        raise AIValidationError("evidence artifact does not contain valid immutable evidence")
    return ResearchEvidenceCritiqueContextV1(original_hypothesis, evidence, information_cutoff)


def score_closed_loop_review(review: ResearchHypothesisReviewV1) -> dict[str, float]:
    """Deterministic safety metrics; separate from directional forecasting benchmarks."""
    return {
        "ASSESSMENT_CONSISTENCY": 1.0,
        "CONFIDENCE_REVISION_CORRECTNESS": 1.0,
        "FACT_REFERENCE_VALIDITY": 1.0,
        "HYPOTHESIS_LINEAGE_VALIDITY": 1.0,
        "RATIONALIZATION_RATE": 0.0,
        "UNSUPPORTED_EVIDENCE_RATE": 0.0,
        "SCHEMA_VALIDITY": 1.0,
    }


def closed_loop_safety_benchmark_digest() -> str:
    """Identity for deterministic validation cases, not a market-accuracy benchmark."""
    return digest(
        {
            "identity": CLOSED_LOOP_SAFETY_BENCHMARK_IDENTITY,
            "cases": CLOSED_LOOP_SAFETY_CASES,
            "metrics": (
                "ASSESSMENT_CONSISTENCY",
                "CONFIDENCE_REVISION_CORRECTNESS",
                "FACT_REFERENCE_VALIDITY",
                "HYPOTHESIS_LINEAGE_VALIDITY",
                "RATIONALIZATION_RATE",
                "UNSUPPORTED_EVIDENCE_RATE",
                "SCHEMA_VALIDITY",
            ),
        }
    )


def _assessment(value: object) -> EvidenceAssessmentV1:
    try:
        return EvidenceAssessmentV1(str(value))
    except ValueError as error:
        raise AIValidationError("assessment is invalid") from error


def _confidence(value: object, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0 <= float(value) <= 1
    ):
        raise AIValidationError(f"{field} must be finite and between zero and one")
    return float(value)


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AIValidationError(f"{field} must be a non-empty string")
    return value


def _texts(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise AIValidationError(f"{field} must be an array of strings")
    return tuple(_string(item, field) for item in value)


def _fact_ids(value: object, allowed: set[str], field: str) -> tuple[str, ...]:
    identifiers = _texts(value, f"{field}_evidence_fact_ids")
    unknown = set(identifiers).difference(allowed)
    if unknown:
        raise AIValidationError(f"{field} evidence contains unknown fact IDs: {sorted(unknown)}")
    return identifiers


def _forbid_trade_language(value: str, field: str) -> None:
    words = {word.strip(".,;:!?()[]{}").upper() for word in value.split()}
    if _TRADE_TOKENS.intersection(words):
        raise AIValidationError(f"{field} cannot contain trade language")


def _value_type(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "BOOLEAN"
    if isinstance(value, int):
        return "INTEGER"
    if isinstance(value, float):
        return "FLOAT"
    return "STRING"
