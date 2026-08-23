from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from app.implementation_context import (
    ImplementationContextItemV1,
    ImplementationContextStateV1,
    ImplementationContextSufficiencyStateV1,
    MissingContextRequestKindV1,
    MissingContextRequestV1,
)
from app.knowledge.store import RepositorySnapshotV1
from app.targeted_context import ContextCompletionReasonV1, complete_implementation_context
from tests.test_targeted_context import _context, _repo


def test_sufficient_and_insufficient_contexts_return_without_inspection(tmp_path: Path) -> None:
    snapshot = _repo(tmp_path)
    sufficient = _context()
    result = complete_implementation_context(sufficient, (), snapshot, tmp_path)
    assert result.reason_code is ContextCompletionReasonV1.SUFFICIENT
    assert not result.rounds
    insufficient = replace(_context(), source_commit=None)
    result = complete_implementation_context(insufficient, (), snapshot, tmp_path)
    assert result.reason_code is ContextCompletionReasonV1.INSUFFICIENT


def test_case_a_completion_deduplicates_and_reaches_sufficient(tmp_path: Path) -> None:
    snapshot = _repo(tmp_path)
    unknown = ImplementationContextItemV1(
        "app/sample.py",
        ImplementationContextStateV1.UNKNOWN,
        "need file",
        missing_kind=MissingContextRequestKindV1.FILE,
        missing_reason="FILE_REQUIRED",
    )
    context = _context(unknown=(unknown,))
    request = MissingContextRequestV1(
        MissingContextRequestKindV1.FILE, "app/sample.py", "FILE_REQUIRED", context.digest
    )
    result = complete_implementation_context(context, (request, request), snapshot, tmp_path)
    assert (
        result.initial_sufficiency.state
        is ImplementationContextSufficiencyStateV1.TARGETED_EXPANSION_REQUIRED
    )
    assert (
        result.final_sufficiency.state
        is ImplementationContextSufficiencyStateV1.IMPLEMENTATION_CONTEXT_SUFFICIENT
    )
    assert len(result.requests_consumed) == 1
    assert len(result.rounds) == 1
    assert result.reason_code is ContextCompletionReasonV1.SUFFICIENT


def test_missing_exact_symbol_stops_after_one_attempt_and_preserves_unknown(tmp_path: Path) -> None:
    snapshot = _repo(tmp_path)
    unknown = ImplementationContextItemV1(
        "MissingSymbol",
        ImplementationContextStateV1.UNKNOWN,
        "missing symbol",
        missing_kind=MissingContextRequestKindV1.SYMBOL,
        missing_reason="SYMBOL_REQUIRED",
    )
    context = _context(unknown=(unknown,))
    request = MissingContextRequestV1(
        MissingContextRequestKindV1.SYMBOL, "MissingSymbol", "SYMBOL_REQUIRED", context.digest
    )
    result = complete_implementation_context(
        context,
        (request,),
        snapshot,
        tmp_path,
        request_file_hints={"MissingSymbol": "app/sample.py"},
    )
    assert result.reason_code is ContextCompletionReasonV1.NO_PROGRESS
    assert len(result.rounds) == 1
    assert result.remaining_missing_requests[0].exact_target == "MissingSymbol"
    assert result.final_context.unknown_items == context.unknown_items


def test_snapshot_and_digest_failures_stop_fail_closed(tmp_path: Path) -> None:
    _repo(tmp_path)
    unknown = ImplementationContextItemV1(
        "app/sample.py",
        ImplementationContextStateV1.UNKNOWN,
        "need file",
        missing_kind=MissingContextRequestKindV1.FILE,
        missing_reason="FILE_REQUIRED",
    )
    context = _context(unknown=(unknown,))
    request = MissingContextRequestV1(
        MissingContextRequestKindV1.FILE, "app/sample.py", "FILE_REQUIRED", context.digest
    )
    result = complete_implementation_context(
        context, (request,), RepositorySnapshotV1("c" * 40, True), tmp_path
    )
    assert result.reason_code is ContextCompletionReasonV1.NO_PROGRESS
    assert result.requests_unresolved
