from __future__ import annotations

from pathlib import Path

import pytest

from app.implementation_context import (
    AuthorityBoundaryV1,
    ImplementationContextItemV1,
    ImplementationContextStateV1,
    ImplementationContextSufficiencyStateV1,
    ImplementationContextV1,
    ImplementationContractContextV1,
    ImplementationSymbolKindV1,
    ImplementationSymbolV1,
    ImplementationTestContextV1,
    MissingContextRequestKindV1,
    MissingContextRequestV1,
    assess_implementation_context_sufficiency,
)
from app.knowledge.store import RepositorySnapshotV1
from app.targeted_context import (
    TargetedContextExpansionReasonV1,
    apply_targeted_expansion,
    resolve_missing_context,
)

COMMIT = "a" * 40
DIGEST = "b" * 64


def _context(*, unknown=()):
    return ImplementationContextV1(
        "Resolve an exact bounded context gap.",
        COMMIT,
        True,
        "chappie_knowledge/1.0.0",
        DIGEST,
        DIGEST,
        DIGEST,
        "app.sample",
        unknown_items=unknown,
        direct_files=("app/sample.py",),
        symbols=(
            ImplementationSymbolV1(
                ImplementationSymbolKindV1.MODULE,
                "app.sample",
                "app.sample",
                "app/sample.py",
                "current module",
                ImplementationContextStateV1.CURRENT,
                source_commit=COMMIT,
                evidence_refs=("knowledge.sample",),
            ),
        ),
        contracts=(
            ImplementationContractContextV1(
                "sample.contract",
                "app.sample",
                ImplementationContextStateV1.CURRENT,
                current_shape=("current shape",),
                source_commit=COMMIT,
                evidence_refs=("knowledge.sample",),
            ),
        ),
        tests=(
            ImplementationTestContextV1(
                "tests/test_sample.py",
                ImplementationContextStateV1.CURRENT,
                "sample.contract",
                "exact test exists",
                "current behavior is asserted",
                "test fails",
            ),
        ),
        authority=AuthorityBoundaryV1(
            ("read exact files",), ("file mutation",), "HUMAN_APPROVAL_REQUIRED"
        ),
        acceptance_criteria=("exact resolution is bounded",),
    )


def _request(context, kind, target):
    return MissingContextRequestV1(kind, target, "exact context required", context.digest)


def _repo(tmp_path: Path):
    (tmp_path / "app").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "app" / "sample.py").write_text(
        "class SampleContractV1:\n    field: str\n\ndef sample_function(value: str) -> str:\n    return value\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_sample.py").write_text(
        "def test_sample_target():\n    assert True\n", encoding="utf-8"
    )
    return RepositorySnapshotV1(COMMIT, True)


def test_exact_file_symbol_contract_and_test_requests_resolve_only_hinted_files(
    tmp_path: Path,
) -> None:
    snapshot = _repo(tmp_path)
    context = _context()
    requests = (
        _request(context, MissingContextRequestKindV1.FILE, "app/sample.py"),
        _request(context, MissingContextRequestKindV1.SYMBOL, "SampleContractV1"),
        _request(context, MissingContextRequestKindV1.CONTRACT, "SampleContractV1"),
        _request(
            context, MissingContextRequestKindV1.TEST, "tests/test_sample.py::test_sample_target"
        ),
    )
    result = resolve_missing_context(
        context,
        requests,
        snapshot,
        tmp_path,
        request_file_hints={"SampleContractV1": "app/sample.py"},
    )
    assert len(result.resolved) == 4
    assert result.files == ("app/sample.py", "tests/test_sample.py")
    assert {item.qualified_identifier for item in result.symbols} == {"SampleContractV1"}
    assert result.contracts[0].current_shape[0].startswith("class SampleContractV1")
    assert result.tests[0].target_ref == "test_sample_target"


def test_missing_unsafe_glob_and_unbounded_requests_fail_closed(tmp_path: Path) -> None:
    snapshot = _repo(tmp_path)
    context = _context()
    missing = _request(context, MissingContextRequestKindV1.SYMBOL, "MissingSymbol")
    result = resolve_missing_context(
        context,
        (missing,),
        snapshot,
        tmp_path,
        request_file_hints={"MissingSymbol": "app/sample.py"},
    )
    assert result.unresolved[0].reason is TargetedContextExpansionReasonV1.NOT_FOUND
    with pytest.raises(ValueError):
        MissingContextRequestV1(
            MissingContextRequestKindV1.FILE, "../secrets.py", "bad", context.digest
        )
    glob = _request(context, MissingContextRequestKindV1.FILE, "app/*.py")
    result = resolve_missing_context(context, (glob,), snapshot, tmp_path)
    assert result.unresolved[0].reason is TargetedContextExpansionReasonV1.UNSAFE_PATH


def test_provenance_budget_and_relation_are_bounded(tmp_path: Path) -> None:
    snapshot = _repo(tmp_path)
    context = _context()
    relation = _request(
        context, MissingContextRequestKindV1.RELATION, "app.sample:OWNS:sample.contract"
    )
    result = resolve_missing_context(
        context,
        (relation,),
        snapshot,
        tmp_path,
        request_file_hints={relation.exact_target: "app/sample.py"},
    )
    assert result.resolved, result
    assert result.resolved[0].reason is TargetedContextExpansionReasonV1.RESOLVED
    bad_digest = MissingContextRequestV1(
        MissingContextRequestKindV1.FILE, "app/sample.py", "bad digest", DIGEST
    )
    result = resolve_missing_context(context, (bad_digest,), snapshot, tmp_path)
    assert result.unresolved[0].reason is TargetedContextExpansionReasonV1.CONTEXT_DIGEST_MISMATCH
    result = resolve_missing_context(
        context,
        tuple(
            _request(context, MissingContextRequestKindV1.FILE, f"app/sample_{index}.py")
            for index in range(13)
        ),
        snapshot,
        tmp_path,
    )
    assert result.unresolved[0].reason is TargetedContextExpansionReasonV1.EXPANSION_BUDGET_EXCEEDED
    result = resolve_missing_context(
        context,
        (_request(context, MissingContextRequestKindV1.FILE, "app/sample.py"),),
        RepositorySnapshotV1("c" * 40, True),
        tmp_path,
    )
    assert result.unresolved[0].reason is TargetedContextExpansionReasonV1.SOURCE_COMMIT_MISMATCH


def test_apply_expansion_removes_resolved_unknown_and_keeps_unresolved_and_proposed(
    tmp_path: Path,
) -> None:
    snapshot = _repo(tmp_path)
    provisional = _context()
    request = _request(provisional, MissingContextRequestKindV1.FILE, "app/sample.py")
    unknown = ImplementationContextItemV1(
        "app/sample.py",
        ImplementationContextStateV1.UNKNOWN,
        "need exact file",
        missing_kind=MissingContextRequestKindV1.FILE,
        missing_reason="FILE_REQUIRED",
    )
    context = _context(unknown=(unknown,))
    request = _request(context, MissingContextRequestKindV1.FILE, "app/sample.py")
    expansion = resolve_missing_context(context, (request,), snapshot, tmp_path)
    merged = apply_targeted_expansion(context, expansion)
    assert not merged.unknown_items
    assert merged.digest != context.digest
    with pytest.raises(ValueError, match="CONTEXT_DIGEST_MISMATCH"):
        apply_targeted_expansion(_context(), expansion)


def test_case_a_can_reach_sufficiency_after_exact_resolution(tmp_path: Path) -> None:
    snapshot = _repo(tmp_path)
    unknown = ImplementationContextItemV1(
        "app/sample.py",
        ImplementationContextStateV1.UNKNOWN,
        "need exact file",
        missing_kind=MissingContextRequestKindV1.FILE,
        missing_reason="FILE_REQUIRED",
    )
    context = _context(unknown=(unknown,))
    before = assess_implementation_context_sufficiency(context)
    assert before.state is ImplementationContextSufficiencyStateV1.TARGETED_EXPANSION_REQUIRED
    expansion = resolve_missing_context(context, before.missing_context, snapshot, tmp_path)
    merged = apply_targeted_expansion(context, expansion)
    assert not merged.unknown_items
