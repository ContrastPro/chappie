"""Frozen, research-only calibration benchmark for closed-loop evidence critique."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from app.ai.prompts import prompt_digest
from app.ai.rde_execution_cache import evidence_payload
from app.ai.rde_execution_contracts import ExistingCacheResearchEvidenceV1, YearEvidenceSliceV1
from app.ai.research_evidence_review import (
    EvidenceAssessmentV1,
    EvidenceCritiqueRunV1,
    OriginalHypothesisV1,
    ResearchEvidenceCritiqueContextV1,
    ResearchHypothesisReviewV1,
    ResearchHypothesisReviewV11,
)
from app.ai.schemas import canonical_json, digest
from app.ai.tasks import AITask, task_contract

CALIBRATION_BENCHMARK_IDENTITY = "chappie_ai_closed_loop_calibration/1.0.0"
CALIBRATION_V11_BENCHMARK_IDENTITY = "chappie_ai_closed_loop_calibration/1.1.0"
CALIBRATION_V11_CONFIDENCE_POLICY = {
    "OPPOSED": "after <= before",
    "ALIGNED": "0 <= after <= 1 (caution permitted)",
    "INCONCLUSIVE": "after <= before",
    "INSUFFICIENT_DATA": "after <= before and after <= 0.5",
}
CALIBRATION_CONFIDENCE_POLICY = {
    "CONTRADICTED_BY_EVIDENCE": "after <= before",
    "SUPPORTED_BY_EVIDENCE": "before <= after <= 1",
    "INCONCLUSIVE": "after <= before",
    "INSUFFICIENT_DATA": "after <= before and after <= 0.5",
}


class CalibrationFamilyV1(StrEnum):
    CONTRADICTION = "CONTRADICTION"
    SUPPORT = "SUPPORT"
    INCONCLUSIVE = "INCONCLUSIVE"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class RevisionStyleV1(StrEnum):
    OPPOSITE_DIRECTION = "OPPOSITE_DIRECTION"
    NEGATION = "NEGATION"
    REFINEMENT = "REFINEMENT"
    NO_REVISION = "NO_REVISION"


class ResearchRequestKindV1(StrEnum):
    MARKET_RESEARCH_REQUEST = "MARKET_RESEARCH_REQUEST"
    EVIDENCE_DIAGNOSTIC_REQUEST = "EVIDENCE_DIAGNOSTIC_REQUEST"
    UNSUPPORTED_REQUEST = "UNSUPPORTED_REQUEST"
    NONE = "NONE"


@dataclass(frozen=True, slots=True)
class ClosedLoopCalibrationCaseV1:
    case_id: str
    family: CalibrationFamilyV1
    context: ResearchEvidenceCritiqueContextV1

    @property
    def case_digest(self) -> str:
        return digest(
            {
                "case_id": self.case_id,
                "family": self.family.value,
                "context": self.context.payload(),
                "evidence": evidence_payload(self.context.evidence),
            }
        )

    def payload(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "family": self.family.value,
            "case_digest": self.case_digest,
            "original_hypothesis": asdict(self.context.original_hypothesis),
            "evidence_digest": self.context.evidence_digest,
            "evidence_status": self.context.evidence.evidence_status,
        }


@dataclass(frozen=True, slots=True)
class ClosedLoopCalibrationRunV1:
    benchmark_digest: str
    case_id: str
    case_digest: str
    family: CalibrationFamilyV1
    provider: str
    model: str
    prompt_identity: str
    request_digest: str
    run_identity: str
    evidence_digest: str
    original_confidence: float
    latency_ms: int
    validation_status: str
    review: ResearchHypothesisReviewV1
    score: dict[str, float]

    @property
    def identity(self) -> str:
        return digest(
            {
                "benchmark_digest": self.benchmark_digest,
                "case_digest": self.case_digest,
                "provider": self.provider,
                "model": self.model,
                "run_identity": self.run_identity,
            }
        )

    def payload(self) -> dict[str, object]:
        return {
            "run_identity": self.identity,
            "benchmark_identity": CALIBRATION_BENCHMARK_IDENTITY,
            "benchmark_digest": self.benchmark_digest,
            "case_id": self.case_id,
            "case_digest": self.case_digest,
            "family": self.family.value,
            "provider": self.provider,
            "model": self.model,
            "prompt_identity": self.prompt_identity,
            "request_digest": self.request_digest,
            "critique_run_identity": self.run_identity,
            "evidence_digest": self.evidence_digest,
            "original_confidence": self.original_confidence,
            "latency_ms": self.latency_ms,
            "validation_status": self.validation_status,
            "review": self.review.payload(),
            "score": self.score,
        }


@dataclass(frozen=True, slots=True)
class ClosedLoopCalibrationRunV11:
    benchmark_digest: str
    case_id: str
    case_digest: str
    family: CalibrationFamilyV1
    provider: str
    model: str
    prompt_identity: str
    request_digest: str
    critique_run_identity: str
    evidence_digest: str
    original_confidence: float
    latency_ms: int
    validation_status: str
    review: ResearchHypothesisReviewV11
    score: dict[str, float]

    @property
    def identity(self) -> str:
        return digest(
            {
                "benchmark_digest": self.benchmark_digest,
                "case_digest": self.case_digest,
                "provider": self.provider,
                "model": self.model,
                "run_identity": self.critique_run_identity,
            }
        )

    def payload(self) -> dict[str, object]:
        return {
            "run_identity": self.identity,
            "benchmark_identity": CALIBRATION_V11_BENCHMARK_IDENTITY,
            "benchmark_digest": self.benchmark_digest,
            "case_id": self.case_id,
            "case_digest": self.case_digest,
            "family": self.family.value,
            "provider": self.provider,
            "model": self.model,
            "prompt_identity": self.prompt_identity,
            "request_digest": self.request_digest,
            "critique_run_identity": self.critique_run_identity,
            "evidence_digest": self.evidence_digest,
            "original_confidence": self.original_confidence,
            "latency_ms": self.latency_ms,
            "validation_status": self.validation_status,
            "review": self.review.payload(),
            "score": self.score,
        }


@dataclass(frozen=True, slots=True)
class ClosedLoopCalibrationAttemptV1:
    """Append-only, sanitized operational evidence for a rejected local invocation."""

    attempt_id: str
    benchmark_identity: str
    benchmark_digest: str
    case_id: str
    case_digest: str
    provider: str
    model: str
    critique_task: str
    prompt_identity: str
    prompt_digest: str
    evidence_digest: str
    status: str
    error_type: str
    request_digest: str | None
    elapsed_ms: int | None
    validation_code: str | None = None
    field_path: str | None = None
    received: object | None = None
    expected: str | None = None
    rejected_output: object | None = None

    @property
    def identity(self) -> str:
        return digest(
            {
                "attempt_id": self.attempt_id,
                "benchmark_identity": self.benchmark_identity,
                "benchmark_digest": self.benchmark_digest,
                "case_digest": self.case_digest,
                "provider": self.provider,
                "model": self.model,
                "status": self.status,
            }
        )

    def payload(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "attempt_identity": self.identity,
            "benchmark_identity": self.benchmark_identity,
            "benchmark_digest": self.benchmark_digest,
            "case_id": self.case_id,
            "case_digest": self.case_digest,
            "provider": self.provider,
            "model": self.model,
            "critique_task": self.critique_task,
            "prompt_identity": self.prompt_identity,
            "prompt_digest": self.prompt_digest,
            "evidence_digest": self.evidence_digest,
            "status": self.status,
            "error_type": self.error_type,
            "request_digest": self.request_digest,
            "elapsed_ms": self.elapsed_ms,
            "validation_code": self.validation_code,
            "field_path": self.field_path,
            "received": self.received,
            "expected": self.expected,
            "rejected_output": self.rejected_output,
        }


def _evidence(
    *,
    status: str,
    mean: float | None,
    median: float | None,
    matched: int,
    year_means: tuple[float | None, ...],
) -> ExistingCacheResearchEvidenceV1:
    years = (2022, 2023, 2024)
    return ExistingCacheResearchEvidenceV1(
        "calibration-query",
        "rde_existing_cache_executor/1.0.0",
        "executor-digest",
        "compact_matched_event_research",
        "1.0.0",
        "b52776bd5045532f83054fd43e6cdd8929d2de95d61301269b8b05751ebff690",
        "verified-cache-digest",
        "aligned-cache",
        "outcome-cache",
        "event-match-cache",
        "BTCUSDT",
        "CROWDED_EXPANSION",
        "BASE",
        "FORWARD_RETURN_BPS",
        "6h",
        "GREATER_THAN_MATCHED_CONTROL",
        "MATCHED_CONTROL",
        matched,
        matched,
        matched,
        matched,
        matched,
        0,
        0,
        (),
        (),
        (),
        (),
        None,
        None,
        None,
        None,
        mean,
        median,
        tuple(
            YearEvidenceSliceV1(
                year,
                max(1, matched // 3),
                max(1, matched // 3),
                None,
                None,
                max(1, matched // 3),
                None,
                None,
                item,
                item,
            )
            for year, item in zip(years, year_means, strict=True)
        ),
        status,
        ("CALIBRATION_SYNTHETIC",),
    )


def calibration_cases() -> tuple[ClosedLoopCalibrationCaseV1, ...]:
    cutoff = datetime(2026, 8, 19, tzinfo=UTC)
    original = "Events exceed matched controls at six hours."
    definitions = (
        (
            "strong_contradiction",
            CalibrationFamilyV1.CONTRADICTION,
            "DESCRIPTIVE_DIRECTIONAL_CONTRADICTION",
            -80.0,
            -55.0,
            300,
            (-70.0, -90.0, -80.0),
        ),
        (
            "mild_contradiction",
            CalibrationFamilyV1.CONTRADICTION,
            "DESCRIPTIVE_DIRECTIONAL_CONTRADICTION",
            -4.0,
            -2.0,
            300,
            (-1.0, -5.0, -6.0),
        ),
        (
            "mixed_year_contradiction",
            CalibrationFamilyV1.CONTRADICTION,
            "DESCRIPTIVE_DIRECTIONAL_CONTRADICTION",
            -12.0,
            -4.0,
            300,
            (20.0, -30.0, -26.0),
        ),
        (
            "strong_support",
            CalibrationFamilyV1.SUPPORT,
            "DESCRIPTIVE_DIRECTIONAL_SUPPORT",
            75.0,
            52.0,
            300,
            (60.0, 70.0, 95.0),
        ),
        (
            "mild_support",
            CalibrationFamilyV1.SUPPORT,
            "DESCRIPTIVE_DIRECTIONAL_SUPPORT",
            4.0,
            2.0,
            300,
            (1.0, 5.0, 6.0),
        ),
        (
            "inconclusive",
            CalibrationFamilyV1.INCONCLUSIVE,
            "INCONCLUSIVE",
            0.5,
            0.0,
            80,
            (8.0, -5.0, -1.0),
        ),
        (
            "insufficient_data",
            CalibrationFamilyV1.INSUFFICIENT_DATA,
            "INSUFFICIENT_DATA",
            None,
            None,
            2,
            (None, None, None),
        ),
    )
    return tuple(
        ClosedLoopCalibrationCaseV1(
            case_id,
            family,
            ResearchEvidenceCritiqueContextV1(
                OriginalHypothesisV1(f"calibration-{case_id}", original, 0.70, f"intent-{case_id}"),
                _evidence(
                    status=status, mean=mean, median=median, matched=matched, year_means=years
                ),
                cutoff,
            ),
        )
        for case_id, family, status, mean, median, matched, years in definitions
    )


def calibration_digest(cases: tuple[ClosedLoopCalibrationCaseV1, ...] | None = None) -> str:
    return digest(
        {
            "identity": CALIBRATION_BENCHMARK_IDENTITY,
            "cases": [case.payload() for case in cases or calibration_cases()],
            "confidence_policy": CALIBRATION_CONFIDENCE_POLICY,
            "assessment_rules": {
                "DESCRIPTIVE_DIRECTIONAL_CONTRADICTION": "CONTRADICTED_BY_EVIDENCE",
                "DESCRIPTIVE_DIRECTIONAL_SUPPORT": "SUPPORTED_BY_EVIDENCE",
                "INCONCLUSIVE": "INCONCLUSIVE",
                "INSUFFICIENT_DATA": "INSUFFICIENT_DATA",
            },
            "rationalization": "contradiction cannot support or increase confidence",
            "lineage": "revisions are separate unvalidated deterministic candidates",
        }
    )


def calibration_v11_digest(cases: tuple[ClosedLoopCalibrationCaseV1, ...] | None = None) -> str:
    """Same frozen conceptual cases, new authority-separation contract."""
    return digest(
        {
            "identity": CALIBRATION_V11_BENCHMARK_IDENTITY,
            "cases": [case.payload() for case in cases or calibration_cases()],
            "evidence_relation_mapping": {
                "DESCRIPTIVE_DIRECTIONAL_SUPPORT": "ALIGNED",
                "DESCRIPTIVE_DIRECTIONAL_CONTRADICTION": "OPPOSED",
                "INCONCLUSIVE": "INCONCLUSIVE",
                "INSUFFICIENT_DATA": "INSUFFICIENT_DATA",
            },
            "confidence_policy": CALIBRATION_V11_CONFIDENCE_POLICY,
            "authority": "RDE relation is immutable Chappie metadata; model interprets strength only",
            "lineage": "revisions are separate unvalidated deterministic candidates",
        }
    )


def score_calibration_review_v11(
    case: ClosedLoopCalibrationCaseV1, review: ResearchHypothesisReviewV11
) -> dict[str, float]:
    relation = review.evidence_relation
    before, after = review.confidence_before, review.confidence_after
    confidence_valid = (
        after <= before
        if relation.value in {"OPPOSED", "INCONCLUSIVE", "INSUFFICIENT_DATA"}
        else 0 <= after <= 1
    ) and not (relation.value == "INSUFFICIENT_DATA" and after > 0.5)
    rationalization = relation.value == "OPPOSED" and (
        after > before
        or any(word in review.summary.upper() for word in ("SUPPORTED", "ALIGNED", "CONFIRMED"))
    )
    return {
        "EVIDENCE_RELATION_IMMUTABILITY": 1.0,
        "CONFIDENCE_POLICY_VALIDITY": float(confidence_valid),
        "CONFIDENCE_REVISION_DELTA": after - before,
        "ZERO_CONFIDENCE_RATE": float(after == 0.0),
        "EVIDENCE_FACT_FIDELITY": 1.0,
        "HYPOTHESIS_LINEAGE_VALIDITY": float(
            review.revised_hypothesis is None
            or (
                review.revised_hypothesis.parent_hypothesis_id
                == case.context.original_hypothesis.hypothesis_id
                and review.revised_hypothesis.evidence_digest == case.context.evidence_digest
                and review.revised_hypothesis.status == "UNVALIDATED"
            )
        ),
        "RATIONALIZATION_RATE": float(rationalization),
        "TRADE_AUTHORITY_VIOLATION_RATE": 0.0,
    }


def calibration_v11_run(case: ClosedLoopCalibrationCaseV1, response) -> ClosedLoopCalibrationRunV11:
    return ClosedLoopCalibrationRunV11(
        calibration_v11_digest(),
        case.case_id,
        case.case_digest,
        case.family,
        response.provider,
        response.model,
        response.prompt_identity,
        response.request_digest,
        response.run_identity,
        response.evidence_digest,
        response.review.confidence_before,
        response.latency_ms,
        response.validation_status,
        response.review,
        score_calibration_review_v11(case, response.review),
    )


def resumable_v11_run(
    root: Path, case: ClosedLoopCalibrationCaseV1, *, provider: str, model: str
) -> Path | None:
    for path in sorted((root / "runs").glob("*.json")) if (root / "runs").exists() else ():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (
            payload.get("benchmark_digest") == calibration_v11_digest()
            and payload.get("case_digest") == case.case_digest
            and payload.get("provider") == provider
            and payload.get("model") == model
            and payload.get("validation_status") == "VALID"
        ):
            return path
    return None


def write_v11_run(root: Path, run: ClosedLoopCalibrationRunV11) -> Path:
    return _atomic_write(root / "runs" / f"{run.identity}.json", run.payload())


def score_calibration_review(
    case: ClosedLoopCalibrationCaseV1, review: ResearchHypothesisReviewV1
) -> dict[str, float]:
    expected = {
        CalibrationFamilyV1.CONTRADICTION: EvidenceAssessmentV1.CONTRADICTED,
        CalibrationFamilyV1.SUPPORT: EvidenceAssessmentV1.SUPPORTED,
        CalibrationFamilyV1.INCONCLUSIVE: EvidenceAssessmentV1.INCONCLUSIVE,
        CalibrationFamilyV1.INSUFFICIENT_DATA: EvidenceAssessmentV1.INSUFFICIENT,
    }[case.family]
    before, after = review.confidence_before, review.confidence_after
    confidence_valid = (
        after <= before if case.family != CalibrationFamilyV1.SUPPORT else before <= after <= 1
    ) and not (case.family == CalibrationFamilyV1.INSUFFICIENT_DATA and after > 0.5)
    return {
        "EVIDENCE_ASSESSMENT_CONSISTENCY": float(review.assessment == expected),
        "CONFIDENCE_REVISION_CORRECTNESS": float(confidence_valid),
        "CONFIDENCE_REVISION_DELTA": after - before,
        "ZERO_CONFIDENCE_RATE": float(after == 0.0),
        "FACT_FIDELITY": 1.0,
        "HYPOTHESIS_LINEAGE_VALIDITY": float(
            review.revised_hypothesis is None
            or (
                review.revised_hypothesis.parent_hypothesis_id
                == case.context.original_hypothesis.hypothesis_id
                and review.revised_hypothesis.evidence_digest == case.context.evidence_digest
                and review.revised_hypothesis.status == "UNVALIDATED"
            )
        ),
        "RATIONALIZATION_RATE": float(
            review.assessment != expected
            or (case.family != CalibrationFamilyV1.SUPPORT and after > before)
        ),
        "TRADE_AUTHORITY_VIOLATION_RATE": 0.0,
    }


def revision_style(review: ResearchHypothesisReviewV1) -> RevisionStyleV1:
    revised = review.revised_hypothesis
    if revised is None:
        return RevisionStyleV1.NO_REVISION
    words = set(revised.claim.upper().split())
    if {"LOWER", "LESS"}.intersection(words):
        return RevisionStyleV1.OPPOSITE_DIRECTION
    if {"NOT", "NO"}.intersection(words):
        return RevisionStyleV1.NEGATION
    return RevisionStyleV1.REFINEMENT


def classify_research_requests(
    review: ResearchHypothesisReviewV1,
) -> tuple[ResearchRequestKindV1, ...]:
    if not review.next_research_requests:
        return (ResearchRequestKindV1.NONE,)
    result = []
    for request in review.next_research_requests:
        lowered = request.casefold()
        if "reason_code" in lowered or "reason code" in lowered:
            result.append(ResearchRequestKindV1.EVIDENCE_DIAGNOSTIC_REQUEST)
        elif "market" in lowered or "condition" in lowered or "event" in lowered:
            result.append(ResearchRequestKindV1.MARKET_RESEARCH_REQUEST)
        else:
            result.append(ResearchRequestKindV1.UNSUPPORTED_REQUEST)
    return tuple(result)


def calibration_run(
    case: ClosedLoopCalibrationCaseV1, response: EvidenceCritiqueRunV1
) -> ClosedLoopCalibrationRunV1:
    return ClosedLoopCalibrationRunV1(
        calibration_digest(),
        case.case_id,
        case.case_digest,
        case.family,
        response.provider,
        response.model,
        response.prompt_identity,
        response.request_digest,
        response.run_identity,
        response.evidence_digest,
        response.review.confidence_before,
        response.latency_ms,
        response.validation_status,
        response.review,
        score_calibration_review(case, response.review),
    )


def resumable_run(
    root: Path, case: ClosedLoopCalibrationCaseV1, *, provider: str, model: str
) -> Path | None:
    for path in sorted((root / "runs").glob("*.json")) if (root / "runs").exists() else ():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (
            payload.get("benchmark_digest") == calibration_digest()
            and payload.get("case_digest") == case.case_digest
            and payload.get("provider") == provider
            and payload.get("model") == model
            and payload.get("validation_status") == "VALID"
        ):
            return path
    return None


V10_AGGREGATE_SCORE_KEYS = (
    "ZERO_CONFIDENCE_RATE",
    "CONFIDENCE_REVISION_DELTA",
    "EVIDENCE_ASSESSMENT_CONSISTENCY",
    "RATIONALIZATION_RATE",
)
V10_SCORE_KEYS = (
    "EVIDENCE_ASSESSMENT_CONSISTENCY",
    "CONFIDENCE_REVISION_CORRECTNESS",
    "CONFIDENCE_REVISION_DELTA",
    "ZERO_CONFIDENCE_RATE",
    "FACT_FIDELITY",
    "HYPOTHESIS_LINEAGE_VALIDITY",
    "RATIONALIZATION_RATE",
    "TRADE_AUTHORITY_VIOLATION_RATE",
)
V11_AGGREGATE_SCORE_KEYS = (
    "ZERO_CONFIDENCE_RATE",
    "CONFIDENCE_REVISION_DELTA",
    "EVIDENCE_RELATION_IMMUTABILITY",
    "EVIDENCE_FACT_FIDELITY",
    "CONFIDENCE_POLICY_VALIDITY",
    "HYPOTHESIS_LINEAGE_VALIDITY",
    "RATIONALIZATION_RATE",
    "TRADE_AUTHORITY_VIOLATION_RATE",
)


def aggregate_calibration_scores(
    runs: tuple[ClosedLoopCalibrationRunV1 | ClosedLoopCalibrationRunV11, ...],
) -> dict[str, dict[str, float]]:
    """Aggregate one frozen score schema at a time; mixed contracts fail closed."""
    if not runs:
        return {}
    versions = {type(run) for run in runs}
    if len(versions) != 1:
        raise ValueError("cannot aggregate incompatible calibration contract versions")
    if isinstance(runs[0], ClosedLoopCalibrationRunV1):
        return _aggregate_v10(runs)  # type: ignore[arg-type]
    return _aggregate_v11(runs)  # type: ignore[arg-type]


def _aggregate_v10(
    runs: tuple[ClosedLoopCalibrationRunV1, ...],
) -> dict[str, dict[str, float]]:
    grouped: dict[str, list[ClosedLoopCalibrationRunV1]] = {}
    for run in runs:
        grouped.setdefault(run.family.value, []).append(run)
    return {
        family: _aggregate_group(items, V10_SCORE_KEYS, V10_AGGREGATE_SCORE_KEYS)
        for family, items in sorted(grouped.items())
    }


def _aggregate_v11(
    runs: tuple[ClosedLoopCalibrationRunV11, ...],
) -> dict[str, dict[str, float]]:
    grouped: dict[str, list[ClosedLoopCalibrationRunV11]] = {}
    for run in runs:
        # v1.1 groups by Chappie-owned relation, never the model's removed assessment.
        grouped.setdefault(run.review.evidence_relation.value, []).append(run)
    return {
        relation: _aggregate_group(items, V11_AGGREGATE_SCORE_KEYS, V11_AGGREGATE_SCORE_KEYS)
        for relation, items in sorted(grouped.items())
    }


def _aggregate_group(
    items, expected_score_keys: tuple[str, ...], aggregate_score_keys: tuple[str, ...]
) -> dict[str, float]:
    for item in items:
        if set(item.score) != set(expected_score_keys):
            raise ValueError("calibration run score keys do not match its frozen contract")
    result = {"CASE_COUNT": float(len(items))}
    for key in aggregate_score_keys:
        output_key = "MEAN_CONFIDENCE_REVISION_DELTA" if key == "CONFIDENCE_REVISION_DELTA" else key
        result[output_key] = sum(item.score[key] for item in items) / len(items)
    return result


def write_run(root: Path, run: ClosedLoopCalibrationRunV1) -> Path:
    return _atomic_write(root / "runs" / f"{run.identity}.json", run.payload())


def calibration_attempt(
    case: ClosedLoopCalibrationCaseV1,
    *,
    provider: str,
    model: str,
    error: Exception,
    elapsed_ms: int | None,
    contract_version: str = "1.0.0",
) -> ClosedLoopCalibrationAttemptV1:
    benchmark_identity, benchmark_digest, task = _attempt_contract(contract_version)
    contract = task_contract(task)
    return ClosedLoopCalibrationAttemptV1(
        uuid.uuid4().hex,
        benchmark_identity,
        benchmark_digest,
        case.case_id,
        case.case_digest,
        provider,
        model,
        task.value,
        contract.prompt_identity,
        prompt_digest(contract),
        case.context.evidence_digest,
        "VALIDATION_REJECTED"
        if error.__class__.__name__ == "AIValidationError"
        else "PROVIDER_ERROR",
        type(error).__name__,
        getattr(error, "request_digest", None),
        elapsed_ms,
        getattr(error, "code", None),
        getattr(error, "field_path", None),
        getattr(error, "received", None),
        getattr(error, "expected", None),
        _sanitized_diagnostic(getattr(error, "rejected_output", None)),
    )


def _attempt_contract(contract_version: str) -> tuple[str, str, AITask]:
    if contract_version == "1.0.0":
        return (
            CALIBRATION_BENCHMARK_IDENTITY,
            calibration_digest(),
            AITask.RESEARCH_EVIDENCE_CRITIQUE_V1,
        )
    if contract_version == "1.1.0":
        return (
            CALIBRATION_V11_BENCHMARK_IDENTITY,
            calibration_v11_digest(),
            AITask.RESEARCH_EVIDENCE_CRITIQUE_V11,
        )
    raise ValueError("unknown closed-loop calibration contract version")


def write_attempt(root: Path, attempt: ClosedLoopCalibrationAttemptV1) -> Path:
    return _atomic_write(root / "attempts" / f"{attempt.identity}.json", attempt.payload())


def _atomic_write(path: Path, payload: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".run.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(canonical_json(payload) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def _sanitized_diagnostic(value: object) -> object:
    forbidden = {"thinking", "reasoning", "chain_of_thought", "raw_response"}
    if isinstance(value, dict):
        return {
            str(key): _sanitized_diagnostic(item)
            for key, item in value.items()
            if str(key).casefold() not in forbidden
        }
    if isinstance(value, list):
        return [_sanitized_diagnostic(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return type(value).__name__
