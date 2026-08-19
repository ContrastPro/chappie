"""Chappie AI public API with dependency-light package initialization."""

from __future__ import annotations

from importlib import import_module
from typing import Any

from app.ai.errors import (
    AIError,
    AIProviderError,
    AIProviderTimeoutError,
    AIRoutingError,
    AIValidationError,
)
from app.ai.schemas import FactV1, ReasoningOutputV1
from app.ai.tasks import AITask, ModelProfile

_LAZY_EXPORTS = {
    "AIConfig": ("app.ai.runtime", "AIConfig"),
    "ChappieAI": ("app.ai.runtime", "ChappieAI"),
    "AIResearchIntentV1": ("app.ai.research_query_compiler", "AIResearchIntentV1"),
    "CanonicalRDEQueryV1": ("app.ai.research_query_compiler", "CanonicalRDEQueryV1"),
    "CanonicalResearchPredicateV1": (
        "app.ai.research_query_compiler",
        "CanonicalResearchPredicateV1",
    ),
    "CompilationStatus": ("app.ai.research_query_compiler", "CompilationStatus"),
    "PredicateCompilationResultV1": (
        "app.ai.research_query_compiler",
        "PredicateCompilationResultV1",
    ),
    "QueryCompilationResultV1": (
        "app.ai.research_query_compiler",
        "QueryCompilationResultV1",
    ),
    "ResearchEvidenceV1": ("app.ai.research_query_compiler", "ResearchEvidenceV1"),
    "compile_fact_to_predicate": ("app.ai.research_query_compiler", "compile_fact_to_predicate"),
    "compile_research_intent": ("app.ai.research_query_compiler", "compile_research_intent"),
    "RDEExecutabilityAssessmentV1": ("app.ai.research_reasoning", "RDEExecutabilityAssessmentV1"),
    "RDEExecutabilityStatus": ("app.ai.research_reasoning", "RDEExecutabilityStatus"),
    "RDEFalsificationSpecV1": ("app.ai.research_reasoning", "RDEFalsificationSpecV1"),
    "RDEHypothesisQueryV1": ("app.ai.research_reasoning", "RDEHypothesisQueryV1"),
    "RDEHypothesisQueryV111": ("app.ai.research_reasoning", "RDEHypothesisQueryV111"),
    "RDEQueryabilityLevel": ("app.ai.research_reasoning", "RDEQueryabilityLevel"),
    "ResearchReasoningOutputV11": ("app.ai.research_reasoning", "ResearchReasoningOutputV11"),
    "TestableHypothesisV11": ("app.ai.research_reasoning", "TestableHypothesisV11"),
    "assess_rde_executability_v111": ("app.ai.research_reasoning", "assess_rde_executability_v111"),
    "derive_rde_falsification_spec_v111": (
        "app.ai.research_reasoning",
        "derive_rde_falsification_spec_v111",
    ),
    "is_rde_queryable": ("app.ai.research_reasoning", "is_rde_queryable"),
    "is_rde_queryable_v111": ("app.ai.research_reasoning", "is_rde_queryable_v111"),
    "rde_query_from_hypothesis": ("app.ai.research_reasoning", "rde_query_from_hypothesis"),
    "rde_query_from_hypothesis_v111": (
        "app.ai.research_reasoning",
        "rde_query_from_hypothesis_v111",
    ),
    "facts_from_historical_event_context": (
        "app.ai.adapters",
        "facts_from_historical_event_context",
    ),
    "historical_context_metadata": ("app.ai.adapters", "historical_context_metadata"),
}


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute = _LAZY_EXPORTS[name]
    except KeyError as error:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from error
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value


__all__ = [
    "AIConfig",
    "AIError",
    "AIProviderError",
    "AIProviderTimeoutError",
    "AIResearchIntentV1",
    "AIRoutingError",
    "AITask",
    "AIValidationError",
    "CanonicalRDEQueryV1",
    "CanonicalResearchPredicateV1",
    "ChappieAI",
    "CompilationStatus",
    "FactV1",
    "ModelProfile",
    "PredicateCompilationResultV1",
    "QueryCompilationResultV1",
    "RDEExecutabilityAssessmentV1",
    "RDEExecutabilityStatus",
    "RDEFalsificationSpecV1",
    "RDEHypothesisQueryV1",
    "RDEHypothesisQueryV111",
    "RDEQueryabilityLevel",
    "ReasoningOutputV1",
    "ResearchEvidenceV1",
    "ResearchReasoningOutputV11",
    "TestableHypothesisV11",
    "assess_rde_executability_v111",
    "compile_fact_to_predicate",
    "compile_research_intent",
    "derive_rde_falsification_spec_v111",
    "facts_from_historical_event_context",
    "historical_context_metadata",
    "is_rde_queryable",
    "is_rde_queryable_v111",
    "rde_query_from_hypothesis",
    "rde_query_from_hypothesis_v111",
]
