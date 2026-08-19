from __future__ import annotations

from datetime import UTC, datetime

from app.ai.rde_capabilities import CapabilityStatusV1, RDECapabilityResolverV1
from app.ai.research_query_compiler import (
    COMPILER_IDENTITY,
    FIELD_REGISTRY_V1,
    AIResearchIntentV1,
    CompatibilityStatusV1,
    CompilationStatus,
    ExpectedDirectionV1,
    RDEQueryExecutor,
    assess_v11_compatibility,
    compile_fact_to_predicate,
    compile_research_intent,
    compiler_digest,
)
from app.ai.research_reasoning import parse_research_reasoning_output_v111
from app.ai.schemas import FactV1

NOW = datetime(2026, 8, 19, 12, tzinfo=UTC)


def _fact(
    fact_id: str,
    field: str,
    value: str | float | bool | None,
    *,
    asset: str = "BTCUSDT",
    unit: str | None = None,
    availability: str = "AVAILABLE",
) -> FactV1:
    value_type = (
        "NULL"
        if value is None
        else "BOOLEAN"
        if isinstance(value, bool)
        else "INTEGER"
        if isinstance(value, int)
        else "FLOAT"
        if isinstance(value, float)
        else "STRING"
    )
    return FactV1(
        fact_id,
        "RDE_CAUSAL_SNAPSHOT",
        NOW,
        field,
        value,
        unit,
        "a" * 64,
        asset=asset,
        canonical_field=field,
        value_type=value_type,
        availability=availability,
    )


def _intent(
    *fact_ids: str, direction: ExpectedDirectionV1 = ExpectedDirectionV1.HIGHER
) -> AIResearchIntentV1:
    return AIResearchIntentV1(
        "intent-price-oi",
        "BTCUSDT",
        "6h",
        "FORWARD_RETURN_BPS",
        "Does price-plus-OI state differ from matched controls?",
        direction,
        fact_ids,
        ("external catalyst",),
    )


def test_compiler_identity_registry_and_digest_are_deterministic() -> None:
    assert COMPILER_IDENTITY == "research_query_compiler/1.0.0"
    assert compiler_digest() == compiler_digest()
    assert set(FIELD_REGISTRY_V1) == {
        "RETURN",
        "OI_STATE",
        "FUNDING_STATE",
        "POSITIVE_BREADTH",
        "RELATIVE_STRENGTH",
        "TAKER_PRESSURE",
        "EVENT_TYPE",
        "EVENT_PRESENT",
    }


def test_fact_metadata_and_typed_predicate_preserve_numeric_value() -> None:
    fact = _fact("return", "RETURN", -1.2, unit="PERCENT")
    result = compile_fact_to_predicate(fact)
    assert result.status == CompilationStatus.COMPILED
    assert result.predicate is not None
    predicate = result.predicate
    assert predicate.typed_value == -1.2
    assert type(predicate.typed_value) is float
    assert predicate.source_fact_id == "return"


def test_null_or_unavailable_fact_fails_closed_without_string_substitution() -> None:
    unavailable = _fact("funding", "FUNDING_STATE", None, availability="UNAVAILABLE")
    result = compile_fact_to_predicate(unavailable)
    assert result.status == CompilationStatus.UNSUPPORTED_PREDICATE
    assert result.reason_codes == ("UNAVAILABLE_FACT_NOT_PREDICTIVE",)


def test_intent_compiles_price_up_and_oi_up_to_same_asset_matched_control_query() -> None:
    facts = (_fact("price", "RETURN", 1.2, unit="PERCENT"), _fact("oi", "OI_STATE", "UP"))
    result = compile_research_intent(_intent("price", "oi"), facts)
    assert result.status == CompilationStatus.COMPILED
    assert result.query is not None
    assert [item.canonical_field for item in result.query.predicates] == ["RETURN", "OI_STATE"]
    assert result.query.target_asset == "BTCUSDT"
    assert result.query.comparison_basis == "MATCHED_CONTROL"
    assert result.query.target_metric == "FORWARD_RETURN_BPS"
    assert result.query.query_digest == result.query.query_digest


def test_unknown_fact_and_ambiguous_intent_fail_closed() -> None:
    facts = (_fact("price", "RETURN", 1.2, unit="PERCENT"),)
    unknown = compile_research_intent(_intent("missing"), facts)
    assert unknown.status == CompilationStatus.UNKNOWN_FACT
    ambiguous = compile_research_intent(
        _intent("price", direction=ExpectedDirectionV1.UNCERTAIN), facts
    )
    assert ambiguous.status == CompilationStatus.AMBIGUOUS_INTENT


def test_unsupported_breadth_and_event_occurrence_are_never_causal_claims() -> None:
    breadth = _fact("breadth", "POSITIVE_BREADTH", "2/8", asset="GLOBAL", unit="RATIO")
    assert compile_fact_to_predicate(breadth).status == CompilationStatus.UNSUPPORTED_PREDICATE
    event = _fact("event", "EVENT_PRESENT", True, asset="GLOBAL")
    result = compile_fact_to_predicate(event)
    assert result.status == CompilationStatus.COMPILED
    assert result.predicate is not None
    assert result.predicate.canonical_field == "EVENT_PRESENT"
    assert "caus" not in result.predicate.operator.casefold()


def test_v11_compatibility_is_read_only_and_reports_missing_legacy_metadata_as_partial() -> None:
    legacy = FactV1("funding", "BENCHMARK", NOW, "BTC.funding", "NEGATIVE", None, "b" * 64)
    payload = {
        "observed_facts": [{"fact_id": "funding"}],
        "interpretations": [],
        "hypotheses": [
            {
                "condition": [{"fact_id": "funding", "operator": "IS", "value": "NEGATIVE"}],
                "target_asset": "BTCUSDT",
                "target_metric": "FORWARD_RETURN_BPS",
                "target_horizon": "6h",
                "expected_relationship": "GREATER_THAN_MATCHED_CONTROL",
                "comparison_basis": "MATCHED_CONTROL",
                "supporting_fact_ids": ["funding"],
                "unknown_dependencies": ["external catalyst"],
                "falsification_condition": "Free text is explanatory.",
                "status": "UNVALIDATED",
            }
        ],
        "contradictions": [],
        "unknowns": ["unknown"],
        "evidence_requests": [],
        "confidence": 0.2,
    }
    hypothesis = parse_research_reasoning_output_v111(payload, (legacy,)).hypotheses[0]
    assessment = assess_v11_compatibility(hypothesis, (legacy,))
    assert assessment.status == CompatibilityStatusV1.PARTIAL
    assert assessment.intent is not None


def test_executor_is_an_interface_not_an_implemented_executor() -> None:
    assert getattr(RDEQueryExecutor, "_is_protocol", False)


def test_capability_resolver_refuses_generic_oi_without_new_methodology() -> None:
    result = compile_research_intent(_intent("oi"), (_fact("oi", "OI_STATE", "UP"),))
    assert result.query is not None
    assessment = RDECapabilityResolverV1().resolve(result.query)
    assert assessment.status == CapabilityStatusV1.METHODOLOGY_EXTENSION_REQUIRED


def test_capability_resolver_accepts_only_exact_existing_event_family() -> None:
    result = compile_research_intent(
        _intent("event"), (_fact("event", "EVENT_TYPE", "CROWDED_EXPANSION"),)
    )
    assert result.query is not None
    assessment = RDECapabilityResolverV1().resolve(result.query)
    assert assessment.status == CapabilityStatusV1.EXECUTABLE_EXISTING_CACHE
    assert assessment.existing_event_query is not None
    assert assessment.existing_event_query.event_family == "CROWDED_EXPANSION"
