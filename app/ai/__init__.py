"""Chappie AI: research reasoning with no trading authority."""

from app.ai.adapters import facts_from_historical_event_context, historical_context_metadata
from app.ai.errors import (
    AIError,
    AIProviderError,
    AIProviderTimeoutError,
    AIRoutingError,
    AIValidationError,
)
from app.ai.research_query_compiler import (
    AIResearchIntentV1,
    CanonicalRDEQueryV1,
    CanonicalResearchPredicateV1,
    CompilationStatus,
    PredicateCompilationResultV1,
    QueryCompilationResultV1,
    ResearchEvidenceV1,
    compile_fact_to_predicate,
    compile_research_intent,
)
from app.ai.research_reasoning import (
    RDEExecutabilityAssessmentV1,
    RDEExecutabilityStatus,
    RDEFalsificationSpecV1,
    RDEHypothesisQueryV1,
    RDEHypothesisQueryV111,
    RDEQueryabilityLevel,
    ResearchReasoningOutputV11,
    TestableHypothesisV11,
    assess_rde_executability_v111,
    derive_rde_falsification_spec_v111,
    is_rde_queryable,
    is_rde_queryable_v111,
    rde_query_from_hypothesis,
    rde_query_from_hypothesis_v111,
)
from app.ai.runtime import AIConfig, ChappieAI
from app.ai.schemas import FactV1, ReasoningOutputV1
from app.ai.tasks import AITask, ModelProfile

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
