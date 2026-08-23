"""Bounded, read-only resolution of explicit implementation-context gaps."""

from app.targeted_context.resolver import (
    TARGETED_CONTEXT_EXPANSION_IDENTITY,
    TargetedContextExpansionReasonV1,
    TargetedContextExpansionV1,
    apply_targeted_expansion,
    resolve_missing_context,
)

__all__ = (
    "TARGETED_CONTEXT_EXPANSION_IDENTITY",
    "TargetedContextExpansionReasonV1",
    "TargetedContextExpansionV1",
    "apply_targeted_expansion",
    "resolve_missing_context",
)
