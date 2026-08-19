"""Closed-loop critique tests: immutable evidence in, validated review out."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from app.ai.errors import AIValidationError
from app.ai.prompts import prompt_digest
from app.ai.providers.mock import MockProvider
from app.ai.rde_capabilities import (
    CapabilityStatusV1,
    ExistingEventResearchQueryV1,
    RDECapabilityResolverV1,
)
from app.ai.rde_execution_cache import evidence_payload, research_evidence_digest
from app.ai.rde_query_executor import execute_existing_cache_query
from app.ai.research_evidence_review import (
    CLOSED_LOOP_SAFETY_BENCHMARK_IDENTITY,
    CLOSED_LOOP_SAFETY_CASES,
    CRITIQUE_IDENTITY,
    EvidenceAssessmentV1,
    EvidenceRelationV1,
    OriginalHypothesisV1,
    ResearchEvidenceCritiqueContextV1,
    authoritative_assessment,
    closed_loop_safety_benchmark_digest,
    context_from_evidence_artifact,
    critique_run_identity,
    evidence_relation,
    parse_research_evidence_critique,
    parse_research_evidence_critique_v11,
    score_closed_loop_review,
)
from app.ai.research_query_compiler import (
    AIResearchIntentV1,
    CompilationStatus,
    ExpectedDirectionV1,
    compile_research_intent,
)
from app.ai.router import ModelRouter
from app.ai.runtime import AIConfig, ChappieAI
from app.ai.schemas import FactV1
from app.ai.tasks import AITask, ModelProfile, task_contract
from app.ai.trace import AITraceEventType
from tests.rde_executor_fixture_support import Fixture, btc_fixture


def _query(fixture: Fixture) -> ExistingEventResearchQueryV1:
    return ExistingEventResearchQueryV1(
        "critique-fixture-query",
        fixture.asset,
        fixture.family,
        "BASE",
        fixture.metric,
        "6h",
        "GREATER_THAN_MATCHED_CONTROL",
        "MATCHED_CONTROL",
        fixture.identity.research_cache_identity_digest,
        fixture.event_match_digest,
        fixture.outcome_digest,
    )


def _evidence(*, contradicted: bool = False):
    fixture = btc_fixture(mutated=contradicted)
    result = execute_existing_cache_query(_query(fixture), fixture.identity, fixture)
    assert result.evidence is not None
    return result.evidence


def _context(*, status: str | None = None, contradicted: bool = False):
    evidence = _evidence(contradicted=contradicted)
    if status is not None:
        evidence = replace(evidence, evidence_status=status)
    return ResearchEvidenceCritiqueContextV1(
        OriginalHypothesisV1(
            "original-hypothesis", "Original matched-control claim", 0.65, "intent-1"
        ),
        evidence,
        datetime(2026, 8, 19, tzinfo=UTC),
    )


def _output(context: ResearchEvidenceCritiqueContextV1, assessment: str, confidence: float):
    facts = {fact.field: fact.fact_id for fact in context.facts()}
    return {
        "assessment": assessment,
        "confidence_after": confidence,
        "summary": "Deterministic evidence was reviewed without trading guidance.",
        "supporting_evidence_fact_ids": (
            [facts["incremental_mean_bps"]]
            if assessment == EvidenceAssessmentV1.SUPPORTED.value
            else []
        ),
        "contradicting_evidence_fact_ids": (
            [facts["incremental_mean_bps"]]
            if assessment == EvidenceAssessmentV1.CONTRADICTED.value
            else []
        ),
        "remaining_unknowns": ["The evidence is descriptive rather than causal."],
        "revised_claim": None,
        "revision_reason": None,
        "next_research_requests": ["Evaluate a separately specified condition."]
        if assessment == "INCONCLUSIVE"
        else [],
    }


def _ai(output: object) -> ChappieAI:
    return ChappieAI(
        ModelRouter({ModelProfile.LOCAL_DEFAULT: MockProvider(output=output)}), AIConfig()
    )


def test_contradiction_requires_reduced_confidence_and_new_lineage() -> None:
    context = _context(contradicted=True)
    output = _output(context, "CONTRADICTED_BY_EVIDENCE", 0.31)
    output["revised_claim"] = "A separate condition may be tested in a new study."
    output["revision_reason"] = "The current evidence contradicts the original claim."

    run = _ai(output).review_research_evidence(context=context, request_id="contradiction")

    assert run.review.assessment == EvidenceAssessmentV1.CONTRADICTED
    assert run.review.confidence_after == 0.31
    assert run.review.revised_hypothesis is not None
    assert run.review.revised_hypothesis.parent_hypothesis_id == "original-hypothesis"
    assert run.review.revised_hypothesis.status == "UNVALIDATED"
    assert context.original_hypothesis.confidence == 0.65
    assert score_closed_loop_review(run.review)["RATIONALIZATION_RATE"] == 0
    assert [event.event_type for event in run.trace_events] == [
        AITraceEventType.RESEARCH_EVIDENCE_RECEIVED,
        AITraceEventType.HYPOTHESIS_REVIEW_STARTED,
        AITraceEventType.HYPOTHESIS_CONTRADICTED,
        AITraceEventType.HYPOTHESIS_REVISED,
    ]


def test_support_can_increase_confidence_but_cannot_create_trade_language() -> None:
    context = _context()
    run = _ai(_output(context, "SUPPORTED_BY_EVIDENCE", 0.72)).review_research_evidence(
        context=context
    )
    assert run.review.assessment == EvidenceAssessmentV1.SUPPORTED
    assert run.review.confidence_after == 0.72

    invalid = _output(context, "SUPPORTED_BY_EVIDENCE", 0.72)
    invalid["summary"] = "BUY because the evidence is positive."
    with pytest.raises(AIValidationError, match="trade language"):
        parse_research_evidence_critique(invalid, context)


@pytest.mark.parametrize(
    ("status", "assessment"),
    [
        ("INCONCLUSIVE", "INCONCLUSIVE"),
        ("INSUFFICIENT_DATA", "INSUFFICIENT_DATA"),
    ],
)
def test_non_supporting_evidence_cannot_increase_confidence(status: str, assessment: str) -> None:
    context = _context(status=status)
    output = _output(context, assessment, 0.45 if status == "INSUFFICIENT_DATA" else 0.55)
    run = _ai(output).review_research_evidence(context=context)
    assert run.review.assessment.value == assessment

    output["confidence_after"] = 0.8
    with pytest.raises(AIValidationError, match="confidence cannot increase"):
        parse_research_evidence_critique(output, context)


def test_authoritative_evidence_rejects_rationalized_assessment_and_unknown_facts() -> None:
    context = _context(contradicted=True)
    rationalized = _output(context, "SUPPORTED_BY_EVIDENCE", 0.65)
    with pytest.raises(AIValidationError, match="conflicts"):
        parse_research_evidence_critique(rationalized, context)

    invalid_fact = _output(context, "CONTRADICTED_BY_EVIDENCE", 0.3)
    invalid_fact["contradicting_evidence_fact_ids"] = ["evidence.fabricated"]
    with pytest.raises(AIValidationError, match="unknown fact"):
        parse_research_evidence_critique(invalid_fact, context)


def test_evidence_artifact_adapter_preserves_btc_and_eth_style_evidence(tmp_path) -> None:
    btc_context = _context()
    eth_evidence = replace(btc_context.evidence, asset="ETHUSDT", event_family="CROWDED_STRESS")
    eth_context = ResearchEvidenceCritiqueContextV1(
        btc_context.original_hypothesis,
        eth_evidence,
        btc_context.information_cutoff,
    )
    for name, context in (("btc", btc_context), ("eth", eth_context)):
        path = tmp_path / f"{name}.json"
        path.write_text(
            json.dumps({"evidence": evidence_payload(context.evidence)}), encoding="utf-8"
        )
        loaded = context_from_evidence_artifact(
            path,
            original_hypothesis=context.original_hypothesis,
            information_cutoff=context.information_cutoff,
        )
        assert loaded.evidence == context.evidence
        assert loaded.evidence_digest == research_evidence_digest(context.evidence)


def test_run_identity_binds_evidence_provider_model_and_prompt() -> None:
    first = _context()
    second = _context(contradicted=True)
    contract = task_contract(AITask.RESEARCH_EVIDENCE_CRITIQUE_V1)
    prompt = prompt_digest(contract)
    assert CRITIQUE_IDENTITY == "research_evidence_critique/1.0.0"
    assert critique_run_identity(
        first, provider="mock/1.0.0", model="mock", prompt_digest=prompt
    ) != critique_run_identity(second, provider="mock/1.0.0", model="mock", prompt_digest=prompt)
    assert (
        authoritative_assessment(first.evidence.evidence_status) == EvidenceAssessmentV1.SUPPORTED
    )
    assert (
        authoritative_assessment(second.evidence.evidence_status)
        == EvidenceAssessmentV1.CONTRADICTED
    )


def test_closed_loop_trace_contract_is_explicit() -> None:
    required = {
        AITraceEventType.RESEARCH_EVIDENCE_RECEIVED,
        AITraceEventType.HYPOTHESIS_REVIEW_STARTED,
        AITraceEventType.HYPOTHESIS_CONTRADICTED,
        AITraceEventType.HYPOTHESIS_SUPPORTED,
        AITraceEventType.HYPOTHESIS_REVISED,
        AITraceEventType.RESEARCH_REQUEST_CREATED,
    }
    assert required.issubset(set(AITraceEventType))


def test_closed_loop_safety_benchmark_contract_has_all_authority_cases() -> None:
    assert CLOSED_LOOP_SAFETY_BENCHMARK_IDENTITY == "chappie_ai_rde_closed_loop_safety/1.0.0"
    assert CLOSED_LOOP_SAFETY_CASES == (
        "contradiction_requires_reduction",
        "support_requires_evidence_reference",
        "inconclusive_cannot_increase_confidence",
        "insufficient_data_cannot_be_high_confidence",
    )
    assert len(closed_loop_safety_benchmark_digest()) == 64


def test_closed_loop_mock_proof_from_fact_to_validated_critique() -> None:
    fixture = btc_fixture(mutated=True)
    fact = FactV1(
        "event-type",
        "RDE_CAUSAL_SNAPSHOT",
        datetime(2026, 8, 19, tzinfo=UTC),
        "EVENT_TYPE",
        "CROWDED_EXPANSION",
        None,
        "f" * 64,
        asset="BTCUSDT",
        canonical_field="EVENT_TYPE",
        value_type="STRING",
    )
    intent = AIResearchIntentV1(
        "intent-closed-loop",
        "BTCUSDT",
        "6h",
        "FORWARD_RETURN_BPS",
        "Does the event exceed matched controls?",
        ExpectedDirectionV1.HIGHER,
        (fact.fact_id,),
        (),
    )
    compiled = compile_research_intent(intent, (fact,))
    assert compiled.status == CompilationStatus.COMPILED
    assert compiled.query is not None
    resolved = RDECapabilityResolverV1().resolve(compiled.query)
    assert resolved.status == CapabilityStatusV1.EXECUTABLE_EXISTING_CACHE
    assert resolved.existing_event_query is not None
    executable = replace(
        resolved.existing_event_query,
        existing_cache_identity=fixture.identity.research_cache_identity_digest,
        event_match_cache_identity=fixture.event_match_digest,
        outcome_cache_identity=fixture.outcome_digest,
    )
    executed = execute_existing_cache_query(executable, fixture.identity, fixture)
    assert executed.evidence is not None
    context = ResearchEvidenceCritiqueContextV1(
        OriginalHypothesisV1("pipeline-hypothesis", "Original condition", 0.65, intent.intent_id),
        executed.evidence,
        datetime(2026, 8, 19, tzinfo=UTC),
    )
    run = _ai(_output(context, "CONTRADICTED_BY_EVIDENCE", 0.31)).review_research_evidence(
        context=context
    )
    assert run.review.assessment == EvidenceAssessmentV1.CONTRADICTED


def _v11_output(
    context,
    *,
    confidence=0.65,
    summary="Evidence is directionally descriptive and remains limited.",
):
    facts = {fact.field: fact.fact_id for fact in context.facts()}
    return {
        "confidence_after": confidence,
        "summary": summary,
        "evidence_fact_ids": [facts["incremental_mean_bps"]],
        "remaining_unknowns": ["Causality remains unproven."],
        "revised_claim": None,
        "revision_reason": None,
        "next_research_requests": [],
    }


def test_v11_relation_is_deterministic_and_never_model_output() -> None:
    assert evidence_relation("DESCRIPTIVE_DIRECTIONAL_SUPPORT") == EvidenceRelationV1.ALIGNED
    assert evidence_relation("DESCRIPTIVE_DIRECTIONAL_CONTRADICTION") == EvidenceRelationV1.OPPOSED
    assert evidence_relation("INCONCLUSIVE") == EvidenceRelationV1.INCONCLUSIVE
    assert evidence_relation("INSUFFICIENT_DATA") == EvidenceRelationV1.INSUFFICIENT_DATA
    context = _context()
    output = _v11_output(context, confidence=0.50)
    review = _ai(output).review_research_evidence_v11(context=context).review
    assert review.evidence_relation == EvidenceRelationV1.ALIGNED
    assert "assessment" not in output


@pytest.mark.parametrize("confidence", [0.65, 0.40])
def test_v11_aligned_cautious_or_reduced_confidence_is_valid(confidence: float) -> None:
    context = _context()
    review = parse_research_evidence_critique_v11(
        _v11_output(context, confidence=confidence), context
    )
    assert review.evidence_relation == EvidenceRelationV1.ALIGNED


def test_v11_opposed_cannot_rationalize_or_increase_confidence() -> None:
    context = _context(contradicted=True)
    valid = parse_research_evidence_critique_v11(_v11_output(context, confidence=0.31), context)
    assert valid.evidence_relation == EvidenceRelationV1.OPPOSED
    with pytest.raises(AIValidationError, match="supporting"):
        parse_research_evidence_critique_v11(
            _v11_output(context, confidence=0.31, summary="The original hypothesis is supported."),
            context,
        )


def test_v11_inconclusive_and_insufficient_allow_uncertain_language() -> None:
    for status in ("INCONCLUSIVE", "INSUFFICIENT_DATA"):
        context = _context(status=status)
        review = parse_research_evidence_critique_v11(
            _v11_output(
                context, confidence=0.45, summary="The evidence is inconclusive and uncertain."
            ),
            context,
        )
        assert review.evidence_relation.value == status
