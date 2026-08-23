"""Exact, bounded repository inspection for missing implementation context.

This adapter accepts only explicit targets and an already-observed repository
snapshot.  It never invokes Git, a shell, a model, semantic retrieval, or any
write operation.  It reads at most the exact files named by the request.
"""

from __future__ import annotations

import ast
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath

from app.implementation_context import (
    ImplementationContextStateV1,
    ImplementationContextV1,
    ImplementationContractContextV1,
    ImplementationRelationKindV1,
    ImplementationRelationV1,
    ImplementationSymbolKindV1,
    ImplementationSymbolV1,
    ImplementationTestContextV1,
    MissingContextRequestKindV1,
    MissingContextRequestV1,
)
from app.knowledge.store import RepositorySnapshotV1

TARGETED_CONTEXT_EXPANSION_IDENTITY = "chappie_targeted_context_expansion/1.0.0"
MAX_REQUESTS = 12
MAX_FILES = 12
MAX_SYMBOLS = 24
MAX_CONTRACTS = 12
MAX_TESTS = 12
MAX_RELATIONS = 24
MAX_SHAPE_FIELDS = 16


class TargetedContextExpansionReasonV1(StrEnum):
    RESOLVED = "RESOLVED"
    NOT_FOUND = "NOT_FOUND"
    UNBOUNDED_REQUEST = "UNBOUNDED_REQUEST"
    SOURCE_COMMIT_MISMATCH = "SOURCE_COMMIT_MISMATCH"
    CONTEXT_DIGEST_MISMATCH = "CONTEXT_DIGEST_MISMATCH"
    REPOSITORY_NOT_CLEAN = "REPOSITORY_NOT_CLEAN"
    EXPANSION_BUDGET_EXCEEDED = "EXPANSION_BUDGET_EXCEEDED"
    UNSAFE_PATH = "UNSAFE_PATH"
    CONTRADICTORY_EVIDENCE = "CONTRADICTORY_EVIDENCE"


@dataclass(frozen=True, slots=True)
class TargetedContextResolvedItemV1:
    request: MissingContextRequestV1
    reason: TargetedContextExpansionReasonV1
    locator: str | None = None


@dataclass(frozen=True, slots=True)
class TargetedContextExpansionV1:
    source_context_digest: str
    source_commit: str
    requests: tuple[MissingContextRequestV1, ...]
    resolved: tuple[TargetedContextResolvedItemV1, ...]
    unresolved: tuple[TargetedContextResolvedItemV1, ...]
    symbols: tuple[ImplementationSymbolV1, ...] = ()
    contracts: tuple[ImplementationContractContextV1, ...] = ()
    tests: tuple[ImplementationTestContextV1, ...] = ()
    relations: tuple[ImplementationRelationV1, ...] = ()
    files: tuple[str, ...] = ()
    files_inspected: int = 0
    elapsed_ms: int = 0

    @property
    def identity(self) -> str:
        return TARGETED_CONTEXT_EXPANSION_IDENTITY


def resolve_missing_context(
    context: ImplementationContextV1,
    requests: tuple[MissingContextRequestV1, ...],
    repository_snapshot: RepositorySnapshotV1,
    repository_root: Path,
    *,
    request_file_hints: dict[str, str] | None = None,
) -> TargetedContextExpansionV1:
    """Resolve only supplied exact requests, fail-closed on provenance/budget errors."""
    started = time.monotonic_ns()
    ordered = tuple(
        sorted(
            {request.payload().__repr__(): request for request in requests}.values(),
            key=lambda item: (item.requested_kind.value, item.exact_target),
        )
    )
    if len(ordered) > MAX_REQUESTS:
        return _failed(
            context, ordered, TargetedContextExpansionReasonV1.EXPANSION_BUDGET_EXCEEDED, started
        )
    if not context.source_commit or context.source_commit != repository_snapshot.commit:
        return _failed(
            context, ordered, TargetedContextExpansionReasonV1.SOURCE_COMMIT_MISMATCH, started
        )
    if not context.repository_clean or not repository_snapshot.clean:
        return _failed(
            context, ordered, TargetedContextExpansionReasonV1.REPOSITORY_NOT_CLEAN, started
        )
    if any(request.requesting_context_digest != context.digest for request in ordered):
        return _failed(
            context, ordered, TargetedContextExpansionReasonV1.CONTEXT_DIGEST_MISMATCH, started
        )

    hints = request_file_hints or {}
    resolved: list[TargetedContextResolvedItemV1] = []
    unresolved: list[TargetedContextResolvedItemV1] = []
    symbols: list[ImplementationSymbolV1] = []
    contracts: list[ImplementationContractContextV1] = []
    tests: list[ImplementationTestContextV1] = []
    relations: list[ImplementationRelationV1] = []
    files: set[str] = set()
    for request in ordered:
        path = _requested_path(request, hints)
        if path is None:
            unresolved.append(
                TargetedContextResolvedItemV1(
                    request, TargetedContextExpansionReasonV1.UNBOUNDED_REQUEST
                )
            )
            continue
        safe = _safe_file(repository_root, path)
        if safe is None:
            unresolved.append(
                TargetedContextResolvedItemV1(request, TargetedContextExpansionReasonV1.UNSAFE_PATH)
            )
            continue
        if len(files) >= MAX_FILES and path not in files:
            return _failed(
                context,
                ordered,
                TargetedContextExpansionReasonV1.EXPANSION_BUDGET_EXCEEDED,
                started,
            )
        if not safe.is_file():
            unresolved.append(
                TargetedContextResolvedItemV1(
                    request, TargetedContextExpansionReasonV1.NOT_FOUND, path
                )
            )
            continue
        files.add(path)
        try:
            tree = ast.parse(safe.read_text(encoding="utf-8"), filename=path)
        except (OSError, UnicodeDecodeError, SyntaxError):
            unresolved.append(
                TargetedContextResolvedItemV1(
                    request, TargetedContextExpansionReasonV1.NOT_FOUND, path
                )
            )
            continue
        owner = _owner(path)
        result = _resolve_request(context, request, path, tree, owner)
        if result is None:
            unresolved.append(
                TargetedContextResolvedItemV1(
                    request, TargetedContextExpansionReasonV1.NOT_FOUND, path
                )
            )
            continue
        item, symbol, contract, test, relation = result
        resolved.append(item)
        if symbol:
            symbols.append(symbol)
        if contract:
            contracts.append(contract)
        if test:
            tests.append(test)
        if relation:
            relations.append(relation)
    if any(
        len(value) > limit
        for value, limit in (
            (symbols, MAX_SYMBOLS),
            (contracts, MAX_CONTRACTS),
            (tests, MAX_TESTS),
            (relations, MAX_RELATIONS),
        )
    ):
        return _failed(
            context, ordered, TargetedContextExpansionReasonV1.EXPANSION_BUDGET_EXCEEDED, started
        )
    return TargetedContextExpansionV1(
        context.digest,
        context.source_commit,
        ordered,
        tuple(resolved),
        tuple(unresolved),
        tuple(
            sorted(
                {item.qualified_identifier: item for item in symbols}.values(),
                key=lambda item: item.qualified_identifier,
            )
        ),
        tuple(
            sorted(
                {item.identity: item for item in contracts}.values(), key=lambda item: item.identity
            )
        ),
        tuple(
            sorted(
                {(item.module_path, item.target_ref): item for item in tests}.values(),
                key=lambda item: (item.module_path, item.target_ref),
            )
        ),
        tuple(
            sorted(
                {
                    (item.source_ref, item.relation.value, item.target_ref): item
                    for item in relations
                }.values(),
                key=lambda item: (item.source_ref, item.relation.value, item.target_ref),
            )
        ),
        tuple(sorted(files)),
        len(files),
        _elapsed(started),
    )


def apply_targeted_expansion(
    context: ImplementationContextV1, expansion: TargetedContextExpansionV1
) -> ImplementationContextV1:
    """Return a new context; resolved gaps are removed, all merge conflicts fail closed."""
    if expansion.source_context_digest != context.digest:
        raise ValueError("CONTEXT_DIGEST_MISMATCH")
    if expansion.source_commit != context.source_commit:
        raise ValueError("SOURCE_COMMIT_MISMATCH")
    resolved_keys = {
        (item.request.requested_kind, item.request.exact_target) for item in expansion.resolved
    }
    unknown = tuple(
        item
        for item in context.unknown_items
        if (item.missing_kind, item.identifier) not in resolved_keys
    )
    _no_conflict(context.symbols, expansion.symbols, lambda item: item.qualified_identifier)
    _no_conflict(context.contracts, expansion.contracts, lambda item: item.identity)
    _no_conflict(context.tests, expansion.tests, lambda item: (item.module_path, item.target_ref))
    _no_conflict(
        context.relations,
        expansion.relations,
        lambda item: (item.source_ref, item.relation, item.target_ref),
    )
    return ImplementationContextV1(
        goal=context.goal,
        source_commit=context.source_commit,
        repository_clean=context.repository_clean,
        knowledge_identity=context.knowledge_identity,
        knowledge_manifest_digest=context.knowledge_manifest_digest,
        knowledge_context_digest=context.knowledge_context_digest,
        evidence_digest=context.evidence_digest,
        canonical_owner=context.canonical_owner,
        current_items=context.current_items,
        proposed_items=context.proposed_items,
        unknown_items=unknown,
        direct_files=tuple(sorted(set(context.direct_files + expansion.files))),
        symbols=context.symbols + expansion.symbols,
        contracts=context.contracts + expansion.contracts,
        tests=context.tests + expansion.tests,
        relations=context.relations + expansion.relations,
        authority=context.authority,
        acceptance_criteria=context.acceptance_criteria,
        risks=context.risks,
        search_guidance=context.search_guidance,
    )


def _resolve_request(
    context: ImplementationContextV1,
    request: MissingContextRequestV1,
    path: str,
    tree: ast.Module,
    owner: str,
):
    names = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    if request.requested_kind is MissingContextRequestKindV1.FILE:
        symbol = None
        if owner not in {existing.qualified_identifier for existing in context.symbols}:
            symbol = ImplementationSymbolV1(
                ImplementationSymbolKindV1.MODULE,
                owner,
                owner,
                path,
                "Exact current file inspected.",
                ImplementationContextStateV1.CURRENT,
                source_commit=context.source_commit,
                evidence_refs=(context.evidence_digest or context.digest,),
            )
        return (
            TargetedContextResolvedItemV1(request, TargetedContextExpansionReasonV1.RESOLVED, path),
            symbol,
            None,
            None,
            None,
        )
    if request.requested_kind in {
        MissingContextRequestKindV1.SYMBOL,
        MissingContextRequestKindV1.CONTRACT,
    }:
        target = request.exact_target.rsplit(".", 1)[-1]
        node = names.get(target)
        if node is None:
            return None
        shape = _shape(node)
        symbol = ImplementationSymbolV1(
            _symbol_kind(node),
            request.exact_target,
            owner,
            path,
            shape,
            ImplementationContextStateV1.CURRENT,
            source_commit=context.source_commit,
            evidence_refs=(context.evidence_digest or context.digest,),
        )
        contract = None
        if request.requested_kind is MissingContextRequestKindV1.CONTRACT:
            contract = ImplementationContractContextV1(
                request.exact_target,
                owner,
                ImplementationContextStateV1.CURRENT,
                current_shape=(shape,),
                source_commit=context.source_commit,
                evidence_refs=(context.evidence_digest or context.digest,),
            )
        return (
            TargetedContextResolvedItemV1(request, TargetedContextExpansionReasonV1.RESOLVED, path),
            symbol,
            contract,
            None,
            None,
        )
    if request.requested_kind is MissingContextRequestKindV1.TEST:
        target = request.exact_target.split("::", 1)[-1]
        node = names.get(target)
        if node is None:
            return None
        test = ImplementationTestContextV1(
            path,
            ImplementationContextStateV1.CURRENT,
            target,
            "Exact test target exists.",
            "Current assertion behavior is defined by the bounded test.",
            "The exact test target fails.",
        )
        return (
            TargetedContextResolvedItemV1(request, TargetedContextExpansionReasonV1.RESOLVED, path),
            None,
            None,
            test,
            None,
        )
    if request.requested_kind is MissingContextRequestKindV1.RELATION:
        parts = request.exact_target.split(":", 2)
        if len(parts) != 3 or parts[1] not in ImplementationRelationKindV1.__members__:
            return None
        source, relation_name, target = parts
        refs = {symbol.qualified_identifier for symbol in context.symbols} | {
            contract.identity for contract in context.contracts
        }
        if source not in refs or target not in refs or source == target:
            return None
        relation = ImplementationRelationV1(
            source,
            ImplementationRelationKindV1[relation_name],
            target,
            path,
            context.evidence_digest or context.digest,
        )
        return (
            TargetedContextResolvedItemV1(request, TargetedContextExpansionReasonV1.RESOLVED, path),
            None,
            None,
            None,
            relation,
        )
    return None


def _requested_path(request: MissingContextRequestV1, hints: dict[str, str]) -> str | None:
    if request.requested_kind is MissingContextRequestKindV1.FILE:
        return request.exact_target
    if request.requested_kind is MissingContextRequestKindV1.TEST and "::" in request.exact_target:
        return request.exact_target.split("::", 1)[0]
    return hints.get(request.exact_target)


def _safe_file(root: Path, path: str) -> Path | None:
    candidate = PurePosixPath(path)
    if candidate.is_absolute() or ".." in candidate.parts or any(char in path for char in "*?["):
        return None
    absolute = (root / Path(*candidate.parts)).resolve()
    try:
        absolute.relative_to(root.resolve())
    except ValueError:
        return None
    return absolute


def _shape(node: ast.AST) -> str:
    if isinstance(node, ast.ClassDef):
        fields = [
            child.target.id
            for child in node.body
            if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name)
        ][:MAX_SHAPE_FIELDS]
        return f"class {node.name}" + (f" fields: {', '.join(fields)}" if fields else "")
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return f"function {node.name}({', '.join(arg.arg for arg in node.args.args[:MAX_SHAPE_FIELDS])})"
    return "module declaration"


def _symbol_kind(node: ast.AST) -> ImplementationSymbolKindV1:
    if isinstance(node, ast.ClassDef):
        return (
            ImplementationSymbolKindV1.DTO
            if node.name.endswith("V1")
            else ImplementationSymbolKindV1.CLASS
        )
    return ImplementationSymbolKindV1.FUNCTION


def _owner(path: str) -> str:
    parts = PurePosixPath(path).parts
    if parts and parts[0] == "app":
        return ".".join((*parts[:-1], PurePosixPath(parts[-1]).stem))
    return "tests"


def _no_conflict(existing, incoming, key) -> None:
    known = {key(item): item for item in existing}
    for item in incoming:
        if key(item) in known and known[key(item)] != item:
            raise ValueError("CONTRADICTORY_EVIDENCE")


def _failed(context, requests, reason, started) -> TargetedContextExpansionV1:
    return TargetedContextExpansionV1(
        context.digest,
        context.source_commit or "0" * 40,
        requests,
        (),
        tuple(TargetedContextResolvedItemV1(request, reason) for request in requests),
        elapsed_ms=_elapsed(started),
    )


def _elapsed(started: int) -> int:
    return max(0, (time.monotonic_ns() - started) // 1_000_000)
