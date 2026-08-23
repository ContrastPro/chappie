"""Bounded orchestration of already-declared targeted context requests.

Context Completion may resolve bounded declared gaps; it may not autonomously
explore the repository.  Repository access remains exclusively delegated to the
exact-request resolver in this package.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from app.implementation_context import (
    ImplementationContextSufficiencyStateV1,
    ImplementationContextSufficiencyV1,
    ImplementationContextV1,
    MissingContextRequestV1,
    assess_implementation_context_sufficiency,
)
from app.knowledge.store import RepositorySnapshotV1
from app.targeted_context.resolver import (
    TargetedContextExpansionV1,
    apply_targeted_expansion,
    resolve_missing_context,
)

CONTEXT_COMPLETION_IDENTITY = "chappie_context_completion/1.0.0"
MAX_COMPLETION_ROUNDS = 2


class ContextCompletionReasonV1(StrEnum):
    SUFFICIENT = "SUFFICIENT"
    INSUFFICIENT = "INSUFFICIENT"
    NO_PROGRESS = "NO_PROGRESS"
    ROUND_LIMIT_REACHED = "ROUND_LIMIT_REACHED"
    EXPANSION_FAILED = "EXPANSION_FAILED"


@dataclass(frozen=True, slots=True)
class ContextCompletionRoundV1:
    round_number: int
    context_digest_before: str
    expansion_context_digest: str
    context_digest_after: str
    requests: tuple[MissingContextRequestV1, ...]
    resolved_count: int
    unresolved_count: int
    progress: bool


@dataclass(frozen=True, slots=True)
class ContextCompletionResultV1:
    initial_context_digest: str
    final_context_digest: str
    source_commit: str | None
    initial_sufficiency: ImplementationContextSufficiencyV1
    final_sufficiency: ImplementationContextSufficiencyV1
    rounds: tuple[ContextCompletionRoundV1, ...]
    requests_consumed: tuple[MissingContextRequestV1, ...]
    requests_resolved: tuple[MissingContextRequestV1, ...]
    requests_unresolved: tuple[MissingContextRequestV1, ...]
    expansions: tuple[TargetedContextExpansionV1, ...]
    final_context: ImplementationContextV1
    remaining_missing_requests: tuple[MissingContextRequestV1, ...]
    reason_code: ContextCompletionReasonV1

    @property
    def identity(self) -> str:
        return CONTEXT_COMPLETION_IDENTITY


def complete_implementation_context(
    context: ImplementationContextV1,
    missing_requests: tuple[MissingContextRequestV1, ...],
    repository_snapshot: RepositorySnapshotV1,
    repository_root: Path,
    *,
    request_file_hints: dict[str, str] | None = None,
) -> ContextCompletionResultV1:
    """Resolve a maximum of two rounds of caller-supplied exact requests."""
    initial = assess_implementation_context_sufficiency(context)
    if initial.state is not ImplementationContextSufficiencyStateV1.TARGETED_EXPANSION_REQUIRED:
        return _result(context, initial, initial, (), (), (), (), (), _terminal_reason(initial))
    current = context
    queued = _normalize_requests(missing_requests)
    rounds: list[ContextCompletionRoundV1] = []
    expansions: list[TargetedContextExpansionV1] = []
    consumed: list[MissingContextRequestV1] = []
    resolved: list[MissingContextRequestV1] = []
    unresolved: list[MissingContextRequestV1] = []
    for number in range(1, MAX_COMPLETION_ROUNDS + 1):
        if not queued:
            break
        expansion = resolve_missing_context(
            current,
            queued,
            repository_snapshot,
            repository_root,
            request_file_hints=request_file_hints,
        )
        expansions.append(expansion)
        consumed.extend(expansion.requests)
        resolved_now = tuple(item.request for item in expansion.resolved)
        unresolved_now = tuple(item.request for item in expansion.unresolved)
        resolved.extend(resolved_now)
        unresolved.extend(unresolved_now)
        try:
            merged = apply_targeted_expansion(current, expansion)
        except ValueError:
            final = assess_implementation_context_sufficiency(current)
            return _result(
                current,
                initial,
                final,
                tuple(rounds),
                tuple(consumed),
                tuple(resolved),
                tuple(unresolved),
                tuple(expansions),
                ContextCompletionReasonV1.EXPANSION_FAILED,
            )
        final = assess_implementation_context_sufficiency(merged)
        progress = bool(resolved_now) and merged.digest != current.digest
        rounds.append(
            ContextCompletionRoundV1(
                number,
                current.digest,
                expansion.source_context_digest,
                merged.digest,
                expansion.requests,
                len(resolved_now),
                len(unresolved_now),
                progress,
            )
        )
        current = merged
        if final.state is not ImplementationContextSufficiencyStateV1.TARGETED_EXPANSION_REQUIRED:
            return _result(
                current,
                initial,
                final,
                tuple(rounds),
                tuple(consumed),
                tuple(resolved),
                tuple(unresolved),
                tuple(expansions),
                _terminal_reason(final),
            )
        if not progress:
            return _result(
                current,
                initial,
                final,
                tuple(rounds),
                tuple(consumed),
                tuple(resolved),
                tuple(unresolved),
                tuple(expansions),
                ContextCompletionReasonV1.NO_PROGRESS,
            )
        # A second round may only consume newly reported exact requests, never prose-derived requests.
        queued = _normalize_requests(final.missing_context)
    final = assess_implementation_context_sufficiency(current)
    return _result(
        current,
        initial,
        final,
        tuple(rounds),
        tuple(consumed),
        tuple(resolved),
        tuple(unresolved),
        tuple(expansions),
        ContextCompletionReasonV1.ROUND_LIMIT_REACHED,
    )


def _normalize_requests(
    requests: tuple[MissingContextRequestV1, ...],
) -> tuple[MissingContextRequestV1, ...]:
    return tuple(
        sorted(
            {
                (
                    item.requested_kind,
                    item.exact_target,
                    item.reason,
                    item.requesting_context_digest,
                ): item
                for item in requests
            }.values(),
            key=lambda item: (item.requested_kind.value, item.exact_target, item.reason),
        )
    )


def _terminal_reason(value: ImplementationContextSufficiencyV1) -> ContextCompletionReasonV1:
    if value.state is ImplementationContextSufficiencyStateV1.IMPLEMENTATION_CONTEXT_SUFFICIENT:
        return ContextCompletionReasonV1.SUFFICIENT
    return ContextCompletionReasonV1.INSUFFICIENT


def _result(context, initial, final, rounds, consumed, resolved, unresolved, expansions, reason):
    remaining = (
        final.missing_context
        if final.state is ImplementationContextSufficiencyStateV1.TARGETED_EXPANSION_REQUIRED
        else ()
    )
    return ContextCompletionResultV1(
        initial_context_digest=initial.missing_context[0].requesting_context_digest
        if initial.missing_context
        else context.digest,
        final_context_digest=context.digest,
        source_commit=context.source_commit,
        initial_sufficiency=initial,
        final_sufficiency=final,
        rounds=rounds,
        requests_consumed=_normalize_requests(consumed),
        requests_resolved=_normalize_requests(resolved),
        requests_unresolved=_normalize_requests(unresolved),
        expansions=expansions,
        final_context=context,
        remaining_missing_requests=remaining,
        reason_code=reason,
    )
