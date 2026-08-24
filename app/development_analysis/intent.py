"""Bounded, model-free Development task-intent resolution."""

from __future__ import annotations

import re

from app.development_analysis.models import (
    DevelopmentAnalysisRequestV1,
    DevelopmentEvidencePackV1,
    DevelopmentTaskIntentKindV1,
    DevelopmentTaskIntentSourceV1,
    DevelopmentTaskIntentV1,
)

_QUESTION = re.compile(r"^(?:explain|what|why|how|where|which|who|when)\b|\?$", re.IGNORECASE)
_ANALYSIS = re.compile(r"^(?:analy[sz]e|assess|evaluate|review)\b", re.IGNORECASE)
_PLAN = re.compile(r"^(?:plan|outline|decompose|split)\b", re.IGNORECASE)


def resolve_development_task_intent(
    request: DevelopmentAnalysisRequestV1,
    evidence: DevelopmentEvidencePackV1,
    context_digest: str,
) -> DevelopmentTaskIntentV1:
    """Resolve intent from an explicit value or a deliberately small compatibility rule."""
    if request.requested_intent is not None:
        intent = request.requested_intent
        source = DevelopmentTaskIntentSourceV1.EXPLICIT
        rule_id = "EXPLICIT_REQUEST_INTENT"
    else:
        text = request.request.strip()
        if _QUESTION.search(text):
            intent, rule_id = DevelopmentTaskIntentKindV1.QUESTION, "CLEAR_QUESTION_V1"
        elif _ANALYSIS.search(text):
            intent, rule_id = DevelopmentTaskIntentKindV1.ANALYSIS, "CLEAR_ANALYSIS_V1"
        elif _PLAN.search(text):
            intent, rule_id = DevelopmentTaskIntentKindV1.PLAN, "CLEAR_PLAN_V1"
        else:
            intent, rule_id = DevelopmentTaskIntentKindV1.UNKNOWN, "AMBIGUOUS_V1"
        source = DevelopmentTaskIntentSourceV1.DETERMINISTIC_RULE
    return DevelopmentTaskIntentV1(
        intent,
        source,
        rule_id,
        evidence.evidence_digest,
        context_digest,
    )
