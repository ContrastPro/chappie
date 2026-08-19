from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.ai import AIConfig, AITask, AIValidationError, ChappieAI, FactV1, ModelProfile
from app.ai.market_semantics import MARKET_SEMANTICS_IDENTITY, market_semantics_digest
from app.ai.providers import MockProvider
from app.ai.research_reasoning import (
    RELATIONSHIPS,
    RESEARCH_REASONING_IDENTITY,
    RESEARCH_REASONING_JSON_SCHEMA,
    RESEARCH_REASONING_V111_IDENTITY,
    TARGET_HORIZONS,
    TARGET_METRICS,
    RDEExecutabilityStatus,
    RDEQueryabilityLevel,
    ResearchReasoningOutputV11,
    assess_rde_executability_v111,
    derive_rde_falsification_spec_v111,
    is_rde_queryable,
    is_rde_queryable_v111,
    parse_research_reasoning_output,
    parse_research_reasoning_output_v111,
    rde_query_from_hypothesis,
    rde_query_from_hypothesis_v111,
)
from app.ai.router import ModelRouter
from app.ai.tasks import task_contract

NOW = datetime(2026, 8, 19, 12, tzinfo=UTC)


def _facts() -> tuple[FactV1, ...]:
    return (
        FactV1("funding", "RDE", NOW, "BTC.funding", "NEGATIVE", None, "a" * 64),
        FactV1("oi", "RDE", NOW, "BTC.oi_change", "UP", None, "b" * 64),
        FactV1("btc-return", "RDE", NOW, "BTC.return", -120.0, "bps", "c" * 64),
    )


def _payload(
    *, statement: str = "Negative funding is supplied; future direction is unknown."
) -> dict[str, object]:
    return {
        "observed_facts": [{"fact_id": "funding"}, {"fact_id": "oi"}],
        "interpretations": [{"statement": statement, "supporting_fact_ids": ["funding"]}],
        "hypotheses": [
            {
                "condition": [{"fact_id": "funding", "operator": "IS", "value": "NEGATIVE"}],
                "target_asset": "BTCUSDT",
                "target_metric": "FORWARD_RETURN_BPS",
                "target_horizon": "6h",
                "expected_relationship": "GREATER_THAN_MATCHED_CONTROL",
                "comparison_basis": "MATCHED_CONTROL",
                "supporting_fact_ids": ["funding"],
                "unknown_dependencies": ["external catalysts"],
                "falsification_condition": "The matched-control estimate is not exceeded at 6h.",
                "status": "UNVALIDATED",
            }
        ],
        "contradictions": [],
        "unknowns": ["Participant identity is not supplied."],
        "evidence_requests": ["Evaluate the defined RDE outcome against matched controls."],
        "confidence": 0.4,
    }


def _ai(provider: MockProvider) -> ChappieAI:
    return ChappieAI(ModelRouter({ModelProfile.LOCAL_DEFAULT: provider}), AIConfig())


def test_market_semantics_identity_and_digest_are_frozen_and_deterministic() -> None:
    assert MARKET_SEMANTICS_IDENTITY == "market_semantics/1.0.0"
    assert market_semantics_digest() == market_semantics_digest()
    assert RESEARCH_REASONING_IDENTITY == "research_reasoning/1.1.0"
    assert RESEARCH_REASONING_V111_IDENTITY == "research_reasoning/1.1.1"
    assert RESEARCH_REASONING_V111_IDENTITY != RESEARCH_REASONING_IDENTITY


def test_provider_schema_recursively_matches_the_frozen_v11_contract() -> None:
    hypothesis = RESEARCH_REASONING_JSON_SCHEMA["properties"]["hypotheses"]["items"]
    assert hypothesis["additionalProperties"] is False
    assert set(hypothesis["required"]) == {
        "condition",
        "target_asset",
        "target_metric",
        "target_horizon",
        "expected_relationship",
        "comparison_basis",
        "supporting_fact_ids",
        "unknown_dependencies",
        "falsification_condition",
        "status",
    }
    assert hypothesis["properties"]["target_metric"]["enum"] == sorted(TARGET_METRICS)
    assert hypothesis["properties"]["target_horizon"]["enum"] == sorted(TARGET_HORIZONS)
    assert hypothesis["properties"]["expected_relationship"]["enum"] == sorted(RELATIONSHIPS)
    assert hypothesis["properties"]["comparison_basis"]["enum"] == ["MATCHED_CONTROL"]
    assert hypothesis["properties"]["status"]["enum"] == ["UNVALIDATED"]
    condition = hypothesis["properties"]["condition"]["items"]
    assert condition["additionalProperties"] is False
    assert set(condition["required"]) == {"fact_id", "operator", "value"}


def test_v11_hypothesis_is_strictly_rde_queryable_and_has_deterministic_identity() -> None:
    output = parse_research_reasoning_output(_payload(), _facts())
    hypothesis = output.hypotheses[0]
    assert is_rde_queryable(hypothesis, _facts(), information_cutoff=NOW)
    assert (
        hypothesis.candidate_id
        == parse_research_reasoning_output(_payload(), _facts()).hypotheses[0].candidate_id
    )
    query = rde_query_from_hypothesis(hypothesis)
    assert query.target_metric == "FORWARD_RETURN_BPS"
    assert query.comparison_basis == "MATCHED_CONTROL"


def test_rde_queryability_fails_closed_for_future_supporting_fact() -> None:
    facts = (
        *_facts(),
        FactV1("future", "RDE", NOW + timedelta(seconds=1), "BTC.x", 1, None, "d" * 64),
    )
    payload = _payload()
    hypothesis = payload["hypotheses"][0]
    assert isinstance(hypothesis, dict)
    hypothesis["condition"] = [{"fact_id": "future", "operator": "IS", "value": 1}]
    hypothesis["supporting_fact_ids"] = ["future"]
    parsed = parse_research_reasoning_output(payload, facts)
    assert not is_rde_queryable(parsed.hypotheses[0], facts, information_cutoff=NOW)


@pytest.mark.parametrize(
    "statement",
    (
        "Rising OI is bullish.",
        "Positive funding proves institutional demand.",
        "Negative funding proves a short squeeze.",
        "BTC is high beta.",
        "Relative strength proves positive absolute return.",
        "Funding is unavailable due to a technical issue.",
        "FOMC caused BTC to decline.",
    ),
)
def test_market_semantics_reject_unsupported_interpretations(statement: str) -> None:
    with pytest.raises(AIValidationError, match="semantic violation"):
        parse_research_reasoning_output(_payload(statement=statement), _facts())


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("target_metric", "RETURN"),
        ("target_horizon", "5m"),
        ("expected_relationship", "HIGHER"),
        ("comparison_basis", "BASELINE"),
        ("status", "SUPPORTED"),
    ),
)
def test_v11_hypothesis_schema_rejects_non_queryable_values(field: str, value: str) -> None:
    payload = _payload()
    hypothesis = payload["hypotheses"][0]
    assert isinstance(hypothesis, dict)
    hypothesis[field] = value
    with pytest.raises(AIValidationError):
        parse_research_reasoning_output(payload, _facts())


def test_invalid_metric_exposes_typed_sanitized_diagnostic_fields() -> None:
    payload = _payload()
    hypothesis = payload["hypotheses"][0]
    assert isinstance(hypothesis, dict)
    hypothesis["target_metric"] = "BTC price"
    with pytest.raises(AIValidationError) as captured:
        parse_research_reasoning_output(payload, _facts())
    assert captured.value.code == "HYPOTHESIS_TARGET_METRIC_INVALID"
    assert captured.value.field_path == "hypotheses[0]"
    assert captured.value.received == "BTC price"
    assert "FORWARD_RETURN_BPS" in (captured.value.expected or "")


def test_v11_rejects_condition_not_equal_to_supplied_fact() -> None:
    payload = _payload()
    hypothesis = payload["hypotheses"][0]
    assert isinstance(hypothesis, dict)
    condition = hypothesis["condition"]
    assert isinstance(condition, list) and isinstance(condition[0], dict)
    condition[0]["value"] = "POSITIVE"
    with pytest.raises(AIValidationError, match="condition value"):
        parse_research_reasoning_output(payload, _facts())


def test_v11_mock_end_to_end_adds_system_semantics_without_turning_them_into_facts() -> None:
    provider = MockProvider(output=_payload())
    response = _ai(provider).generate(
        task=AITask.RESEARCH_REASONING_V11,
        facts=_facts(),
        information_cutoff=NOW,
        created_at=NOW,
    )
    assert isinstance(response.structured_output, ResearchReasoningOutputV11)
    assert provider.requests[0].context["market_semantics"]["identity"] == MARKET_SEMANTICS_IDENTITY
    assert {fact.fact_id for fact in provider.requests[0].facts} == {"funding", "oi", "btc-return"}
    assert response.prompt_version == "research_reasoning/1.1.0"


def test_v111_mock_end_to_end_has_distinct_contract_identity() -> None:
    provider = MockProvider(output=_payload())
    response = _ai(provider).generate(
        task=AITask.RESEARCH_REASONING_V111,
        facts=_facts(),
        information_cutoff=NOW,
        created_at=NOW,
    )
    assert isinstance(response.structured_output, ResearchReasoningOutputV11)
    assert response.prompt_version == RESEARCH_REASONING_V111_IDENTITY
    assert provider.requests[0].context["market_semantics"]["identity"] == MARKET_SEMANTICS_IDENTITY


def test_v1_prompt_contract_remains_distinct_and_unchanged() -> None:
    assert (
        task_contract(AITask.MARKET_INTERPRETATION).prompt_identity == "market_interpretation/1.0.0"
    )
    assert (
        task_contract(AITask.RESEARCH_REASONING_V11).prompt_identity == RESEARCH_REASONING_IDENTITY
    )


def test_v111_condition_facts_must_be_explicitly_supported() -> None:
    payload = _payload()
    hypothesis = payload["hypotheses"][0]
    assert isinstance(hypothesis, dict)
    hypothesis["supporting_fact_ids"] = ["oi"]
    # Frozen v1.1.0 evidence retains its original acceptance semantics.
    old = parse_research_reasoning_output(payload, _facts()).hypotheses[0]
    assert is_rde_queryable(old, _facts(), information_cutoff=NOW)
    with pytest.raises(AIValidationError) as captured:
        parse_research_reasoning_output_v111(payload, _facts())
    assert captured.value.code == "CONDITION_FACT_NOT_SUPPORTED"
    assert captured.value.received == ["funding"]


def test_v111_requires_exact_condition_value_type_and_never_coerces_nulls() -> None:
    facts = (
        *_facts(),
        FactV1("missing-basis", "RDE", NOW, "BTC.basis", None, None, "d" * 64),
        FactV1("integer", "RDE", NOW, "BTC.count", 1, None, "e" * 64),
    )
    payload = _payload()
    hypothesis = payload["hypotheses"][0]
    assert isinstance(hypothesis, dict)
    hypothesis["condition"] = [{"fact_id": "integer", "operator": "IS", "value": 1.0}]
    hypothesis["supporting_fact_ids"] = ["integer"]
    with pytest.raises(AIValidationError) as captured:
        parse_research_reasoning_output_v111(payload, facts)
    assert captured.value.code == "CONDITION_VALUE_INVALID"
    hypothesis["condition"] = [{"fact_id": "missing-basis", "operator": "IS", "value": "NULL"}]
    hypothesis["supporting_fact_ids"] = ["missing-basis"]
    with pytest.raises(AIValidationError) as captured:
        parse_research_reasoning_output_v111(payload, facts)
    assert captured.value.code == "CONDITION_VALUE_INVALID"
    hypothesis["condition"] = [{"fact_id": "missing-basis", "operator": "IS", "value": None}]
    parsed = parse_research_reasoning_output_v111(payload, facts)
    assert parsed.hypotheses[0].condition[0].value is None


def test_v111_derived_falsification_is_machine_authoritative_and_same_asset() -> None:
    hypothesis = parse_research_reasoning_output_v111(_payload(), _facts()).hypotheses[0]
    spec = derive_rde_falsification_spec_v111(hypothesis)
    query = rde_query_from_hypothesis_v111(hypothesis)
    assert spec.target_asset == "BTCUSDT"
    assert spec.target_metric == "FORWARD_RETURN_BPS"
    assert spec.comparison_basis == "MATCHED_CONTROL"
    assert spec.required_relationship == "GREATER_THAN_MATCHED_CONTROL"
    assert spec.failure_relationship == "NOT_GREATER_THAN_MATCHED_CONTROL"
    assert query.falsification_spec == spec
    changed_prose = type(hypothesis)(
        hypothesis.condition,
        hypothesis.target_asset,
        hypothesis.target_metric,
        hypothesis.target_horizon,
        hypothesis.expected_relationship,
        hypothesis.comparison_basis,
        hypothesis.supporting_fact_ids,
        hypothesis.unknown_dependencies,
        "This prose is explanatory only.",
    )
    assert derive_rde_falsification_spec_v111(changed_prose) == spec


def test_v111_distinguishes_queryability_from_future_executability() -> None:
    hypothesis = parse_research_reasoning_output_v111(_payload(), _facts()).hypotheses[0]
    assert is_rde_queryable_v111(hypothesis, _facts(), information_cutoff=NOW)
    assessment = assess_rde_executability_v111(hypothesis, _facts(), information_cutoff=NOW)
    assert assessment.level == "RDE_QUERYABLE"
    assert assessment.status == RDEExecutabilityStatus.QUERYABLE_NOT_EXECUTABLE
    assert assessment.reason_codes == ("NO_CANONICAL_FACT_TO_RDE_PREDICATE_MAPPING",)
    assert tuple(RDEQueryabilityLevel) == (
        RDEQueryabilityLevel.STRUCTURALLY_VALID,
        RDEQueryabilityLevel.RDE_QUERYABLE,
        RDEQueryabilityLevel.RDE_EXECUTABLE,
    )
