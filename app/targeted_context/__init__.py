"""Bounded, read-only resolution of explicit implementation-context gaps."""

from app.targeted_context.completion import (
    CONTEXT_COMPLETION_IDENTITY,
    ContextCompletionReasonV1,
    ContextCompletionResultV1,
    complete_implementation_context,
)
from app.targeted_context.resolver import (
    TARGETED_CONTEXT_EXPANSION_IDENTITY,
    TargetedContextExpansionReasonV1,
    TargetedContextExpansionV1,
    apply_targeted_expansion,
    resolve_missing_context,
)

__all__ = (
    "CONTEXT_COMPLETION_IDENTITY",
    "TARGETED_CONTEXT_EXPANSION_IDENTITY",
    "ContextCompletionReasonV1",
    "ContextCompletionResultV1",
    "TargetedContextExpansionReasonV1",
    "TargetedContextExpansionV1",
    "apply_targeted_expansion",
    "complete_implementation_context",
    "resolve_missing_context",
)
