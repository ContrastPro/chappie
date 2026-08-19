"""Deterministic tests for the frozen closed-loop calibration benchmark."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.ai.closed_loop_calibration import (
    CALIBRATION_BENCHMARK_IDENTITY,
    CALIBRATION_V11_BENCHMARK_IDENTITY,
    V10_AGGREGATE_SCORE_KEYS,
    V11_AGGREGATE_SCORE_KEYS,
    CalibrationFamilyV1,
    ResearchRequestKindV1,
    RevisionStyleV1,
    aggregate_calibration_scores,
    calibration_attempt,
    calibration_cases,
    calibration_digest,
    calibration_run,
    calibration_v11_digest,
    calibration_v11_run,
    classify_research_requests,
    resumable_run,
    resumable_v11_run,
    revision_style,
    score_calibration_review,
    score_calibration_review_v11,
    write_attempt,
    write_run,
    write_v11_run,
)
from app.ai.errors import AIValidationError
from app.ai.prompts import prompt_digest
from app.ai.providers.mock import MockProvider
from app.ai.research_evidence_review import EvidenceAssessmentV1, parse_research_evidence_critique
from app.ai.router import ModelRouter
from app.ai.runtime import AIConfig, ChappieAI
from app.ai.tasks import AITask, ModelProfile, task_contract
from app.tools.chappie_ai_closed_loop_benchmark import _parser, _selected


def _output(case, *, confidence: float | None = None, revised: str | None = None):
    expected = {
        CalibrationFamilyV1.CONTRADICTION: EvidenceAssessmentV1.CONTRADICTED.value,
        CalibrationFamilyV1.SUPPORT: EvidenceAssessmentV1.SUPPORTED.value,
        CalibrationFamilyV1.INCONCLUSIVE: EvidenceAssessmentV1.INCONCLUSIVE.value,
        CalibrationFamilyV1.INSUFFICIENT_DATA: EvidenceAssessmentV1.INSUFFICIENT.value,
    }[case.family]
    fields = {fact.field: fact.fact_id for fact in case.context.facts()}
    if confidence is None:
        confidence = 0.4 if case.family != CalibrationFamilyV1.SUPPORT else 0.8
    return {
        "assessment": expected,
        "confidence_after": confidence,
        "summary": "Supplied deterministic evidence was assessed without trading guidance.",
        "supporting_evidence_fact_ids": [fields["incremental_mean_bps"]]
        if expected == EvidenceAssessmentV1.SUPPORTED.value
        else [],
        "contradicting_evidence_fact_ids": [fields["incremental_mean_bps"]]
        if expected == EvidenceAssessmentV1.CONTRADICTED.value
        else [],
        "remaining_unknowns": ["The descriptive result does not establish causality."],
        "revised_claim": revised,
        "revision_reason": "This is a separate unvalidated proposal." if revised else None,
        "next_research_requests": ["Inspect reason codes only."]
        if case.family == CalibrationFamilyV1.INCONCLUSIVE
        else [],
    }


def _ai(output) -> ChappieAI:
    return ChappieAI(
        ModelRouter({ModelProfile.LOCAL_DEFAULT: MockProvider(output=output)}), AIConfig()
    )


def test_frozen_identity_digest_and_predeclared_inventory() -> None:
    cases = calibration_cases()
    assert CALIBRATION_BENCHMARK_IDENTITY == "chappie_ai_closed_loop_calibration/1.0.0"
    assert calibration_digest() == calibration_digest(cases)
    assert [case.case_id for case in cases] == [
        "strong_contradiction",
        "mild_contradiction",
        "mixed_year_contradiction",
        "strong_support",
        "mild_support",
        "inconclusive",
        "insufficient_data",
    ]


@pytest.mark.parametrize("case", calibration_cases(), ids=lambda item: item.case_id)
def test_mock_provider_scores_every_evidence_family(case) -> None:
    run = _ai(_output(case)).review_research_evidence(context=case.context)
    score = score_calibration_review(case, run.review)
    assert score["EVIDENCE_ASSESSMENT_CONSISTENCY"] == 1
    assert score["CONFIDENCE_REVISION_CORRECTNESS"] == 1
    assert score["RATIONALIZATION_RATE"] == 0
    assert score["TRADE_AUTHORITY_VIOLATION_RATE"] == 0


def test_invalid_rationalization_and_confidence_are_rejected() -> None:
    contradiction = calibration_cases()[0]
    rationalized = _output(contradiction, confidence=0.8)
    rationalized["assessment"] = "SUPPORTED_BY_EVIDENCE"
    with pytest.raises(AIValidationError, match="conflicts"):
        parse_research_evidence_critique(rationalized, contradiction.context)

    insufficient = calibration_cases()[-1]
    with pytest.raises(AIValidationError, match="high confidence"):
        parse_research_evidence_critique(
            _output(insufficient, confidence=0.7), insufficient.context
        )


def test_lineage_revision_style_request_classification_and_immutable_run(tmp_path: Path) -> None:
    case = calibration_cases()[0]
    response = _ai(
        _output(case, revised="Lower returns may be separately tested.")
    ).review_research_evidence(context=case.context)
    assert revision_style(response.review) == RevisionStyleV1.OPPOSITE_DIRECTION
    run = calibration_run(case, response)
    path = write_run(tmp_path, run)
    assert path.is_file()
    assert resumable_run(tmp_path, case, provider=run.provider, model=run.model) == path
    assert response.review.evidence_digest == case.context.evidence_digest
    assert response.review.revised_hypothesis is not None
    assert (
        response.review.revised_hypothesis.parent_hypothesis_id
        == case.context.original_hypothesis.hypothesis_id
    )

    inconclusive = calibration_cases()[-2]
    review = (
        _ai(_output(inconclusive)).review_research_evidence(context=inconclusive.context).review
    )
    assert classify_research_requests(review) == (
        ResearchRequestKindV1.EVIDENCE_DIAGNOSTIC_REQUEST,
    )


def test_cli_selection_is_bounded_and_local_provider_is_explicit() -> None:
    parsed = _parser().parse_args(["--provider", "ollama", "--model", "qwen3:8b", "--limit", "4"])
    assert len(_selected(parsed)) == 4
    resumed = _parser().parse_args(
        ["--provider", "ollama", "--model", "qwen3:8b", "--case-id", "strong_support", "--resume"]
    )
    assert [case.case_id for case in _selected(resumed)] == ["strong_support"]


def test_aggregate_reports_zero_confidence_and_delta_by_family() -> None:
    first, second = calibration_cases()[:2]
    first_run = calibration_run(
        first, _ai(_output(first, confidence=0.0)).review_research_evidence(context=first.context)
    )
    second_run = calibration_run(
        second,
        _ai(_output(second, confidence=0.4)).review_research_evidence(context=second.context),
    )
    aggregate = aggregate_calibration_scores((first_run, second_run))
    assert aggregate["CONTRADICTION"]["CASE_COUNT"] == 2
    assert aggregate["CONTRADICTION"]["ZERO_CONFIDENCE_RATE"] == 0.5
    assert aggregate["CONTRADICTION"]["MEAN_CONFIDENCE_REVISION_DELTA"] == pytest.approx(-0.5)


def test_rejected_provider_output_is_preserved_as_sanitized_attempt(tmp_path: Path) -> None:
    case = next(item for item in calibration_cases() if item.family == CalibrationFamilyV1.SUPPORT)
    output = _output(case)
    output["assessment"] = "CONTRADICTED_BY_EVIDENCE"
    with pytest.raises(AIValidationError) as caught:
        _ai(output).review_research_evidence(context=case.context)
    attempt = calibration_attempt(
        case,
        provider="mock/1.0.0",
        model="deterministic-mock/1.0.0",
        error=caught.value,
        elapsed_ms=1,
    )
    path = write_attempt(tmp_path, attempt)
    payload = path.read_text(encoding="utf-8")
    assert attempt.status == "VALIDATION_REJECTED"
    assert attempt.validation_code == "VALIDATION_ERROR"
    assert "thinking" not in payload


@pytest.mark.parametrize(
    ("contract_version", "benchmark_identity", "task"),
    [
        ("1.0.0", CALIBRATION_BENCHMARK_IDENTITY, AITask.RESEARCH_EVIDENCE_CRITIQUE_V1),
        ("1.1.0", CALIBRATION_V11_BENCHMARK_IDENTITY, AITask.RESEARCH_EVIDENCE_CRITIQUE_V11),
    ],
)
def test_attempt_provenance_follows_selected_contract(
    contract_version, benchmark_identity, task
) -> None:
    case = calibration_cases()[0]
    with pytest.raises(AIValidationError) as caught:
        _ai(_output(case, confidence=0.8)).review_research_evidence(context=case.context)
    attempt = calibration_attempt(
        case,
        provider="mock/1.0.0",
        model="mock",
        error=caught.value,
        elapsed_ms=1,
        contract_version=contract_version,
    )
    contract = task_contract(task)
    payload = attempt.payload()
    assert payload["benchmark_identity"] == benchmark_identity
    assert payload["critique_task"] == task.value
    assert payload["prompt_identity"] == contract.prompt_identity
    assert payload["prompt_digest"] == prompt_digest(contract)


def test_attempt_identities_are_distinct_between_contracts() -> None:
    case = calibration_cases()[0]
    with pytest.raises(AIValidationError) as caught:
        _ai(_output(case, confidence=0.8)).review_research_evidence(context=case.context)
    first = calibration_attempt(
        case,
        provider="mock",
        model="mock",
        error=caught.value,
        elapsed_ms=1,
        contract_version="1.0.0",
    )
    second = calibration_attempt(
        case,
        provider="mock",
        model="mock",
        error=caught.value,
        elapsed_ms=1,
        contract_version="1.1.0",
    )
    assert first.identity != second.identity


def _v11_output(
    case, *, confidence=0.7, summary="Evidence remains uncertain and requires further research."
):
    fields = {fact.field: fact.fact_id for fact in case.context.facts()}
    return {
        "confidence_after": confidence,
        "summary": summary,
        "evidence_fact_ids": [fields["incremental_mean_bps"]]
        if case.family in {CalibrationFamilyV1.CONTRADICTION, CalibrationFamilyV1.SUPPORT}
        else [],
        "remaining_unknowns": ["The result is descriptive."],
        "revised_claim": None,
        "revision_reason": None,
        "next_research_requests": [],
    }


def test_v11_is_separate_contract_with_deterministic_digest() -> None:
    assert calibration_v11_digest() != calibration_digest()
    assert len(calibration_v11_digest()) == 64


@pytest.mark.parametrize("case", calibration_cases(), ids=lambda item: item.case_id)
def test_v11_mock_proves_authority_separation(case) -> None:
    confidence = 0.5 if case.family == CalibrationFamilyV1.SUPPORT else 0.4
    response = _ai(_v11_output(case, confidence=confidence)).review_research_evidence_v11(
        context=case.context
    )
    score = score_calibration_review_v11(case, response.review)
    assert score["EVIDENCE_RELATION_IMMUTABILITY"] == 1
    assert score["CONFIDENCE_POLICY_VALIDITY"] == 1
    assert score["RATIONALIZATION_RATE"] == 0
    assert calibration_v11_run(case, response).benchmark_digest == calibration_v11_digest()


def test_v11_real_rejected_shape_is_valid_when_cautious_and_fact_faithful() -> None:
    support = next(item for item in calibration_cases() if item.case_id == "strong_support")
    # Sanitized shape from the historical v1.0 Qwen rejection: cautious, factual, no trade claim.
    response = _ai(_v11_output(support, confidence=0.3)).review_research_evidence_v11(
        context=support.context
    )
    assert response.review.evidence_relation.value == "ALIGNED"
    insufficient = calibration_cases()[-1]
    response = _ai(_v11_output(insufficient, confidence=0.3)).review_research_evidence_v11(
        context=insufficient.context
    )
    assert response.review.evidence_relation.value == "INSUFFICIENT_DATA"


def test_v10_aggregate_schema_and_values_remain_frozen() -> None:
    first, second = calibration_cases()[:2]
    runs = tuple(
        calibration_run(
            case,
            _ai(_output(case, confidence=confidence)).review_research_evidence(
                context=case.context
            ),
        )
        for case, confidence in ((first, 0.0), (second, 0.4))
    )
    aggregate = aggregate_calibration_scores(runs)
    assert V10_AGGREGATE_SCORE_KEYS == (
        "ZERO_CONFIDENCE_RATE",
        "CONFIDENCE_REVISION_DELTA",
        "EVIDENCE_ASSESSMENT_CONSISTENCY",
        "RATIONALIZATION_RATE",
    )
    assert aggregate["CONTRADICTION"] == {
        "CASE_COUNT": 2.0,
        "ZERO_CONFIDENCE_RATE": 0.5,
        "MEAN_CONFIDENCE_REVISION_DELTA": pytest.approx(-0.5),
        "EVIDENCE_ASSESSMENT_CONSISTENCY": 1.0,
        "RATIONALIZATION_RATE": 0.0,
    }


def test_v11_partial_aggregate_groups_by_relation_and_resume_needs_no_provider(
    tmp_path: Path,
) -> None:
    selected = calibration_cases()[:4]
    runs = tuple(
        calibration_v11_run(
            case,
            _ai(
                _v11_output(
                    case, confidence=0.3 if case.family != CalibrationFamilyV1.SUPPORT else 0.7
                )
            ).review_research_evidence_v11(context=case.context),
        )
        for case in selected
    )
    aggregate = aggregate_calibration_scores(runs)
    assert set(aggregate) == {"OPPOSED", "ALIGNED"}
    assert aggregate["OPPOSED"]["CASE_COUNT"] == 3
    assert aggregate["ALIGNED"]["CASE_COUNT"] == 1
    assert set(aggregate["OPPOSED"]) == {
        "CASE_COUNT",
        "MEAN_CONFIDENCE_REVISION_DELTA",
        *V11_AGGREGATE_SCORE_KEYS,
    } - {"CONFIDENCE_REVISION_DELTA"}
    path = write_v11_run(tmp_path, runs[0])
    assert (
        resumable_v11_run(tmp_path, selected[0], provider=runs[0].provider, model=runs[0].model)
        == path
    )


def test_mixed_v10_and_v11_aggregation_fails_closed() -> None:
    case = calibration_cases()[0]
    v10 = calibration_run(case, _ai(_output(case)).review_research_evidence(context=case.context))
    v11 = calibration_v11_run(
        case,
        _ai(_v11_output(case, confidence=0.4)).review_research_evidence_v11(context=case.context),
    )
    with pytest.raises(ValueError, match="incompatible calibration contract"):
        aggregate_calibration_scores((v10, v11))
