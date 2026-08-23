from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest

from app.implementation_context import (
    IMPLEMENTATION_CONTEXT_IDENTITY,
    AuthorityBoundaryV1,
    ImplementationContextItemV1,
    ImplementationContextStateV1,
    ImplementationContextSufficiencyStateV1,
    ImplementationContextV1,
    ImplementationContractContextV1,
    ImplementationRelationKindV1,
    ImplementationRelationV1,
    ImplementationSymbolKindV1,
    ImplementationSymbolV1,
    ImplementationTestContextV1,
    MissingContextRequestKindV1,
    MissingContextRequestV1,
    assess_implementation_context_sufficiency,
)

COMMIT = "a" * 64
DIGEST = "b" * 64


def _authority() -> AuthorityBoundaryV1:
    return AuthorityBoundaryV1(
        ("prepare bounded implementation context",),
        ("file mutation", "Git push", "order execution"),
        "HUMAN_APPROVAL_REQUIRED",
    )


def _symbol(
    name: str = "app.implementation_context.models.ImplementationContextV1",
) -> ImplementationSymbolV1:
    return ImplementationSymbolV1(
        ImplementationSymbolKindV1.DTO,
        name,
        "app.implementation_context",
        "app/implementation_context/models.py",
        "immutable context DTO",
        ImplementationContextStateV1.CURRENT,
        source_commit=COMMIT,
        evidence_refs=("knowledge.implementation_context",),
    )


def _contract() -> ImplementationContractContextV1:
    return ImplementationContractContextV1(
        "chappie_implementation_context/1.0.0",
        "app.implementation_context",
        ImplementationContextStateV1.CURRENT,
        current_shape=("goal", "source_commit", "authority"),
        version="1.0.0",
        frozen=True,
        versioned=True,
        source_commit=COMMIT,
        evidence_refs=("knowledge.implementation_context.contract",),
    )


def _test() -> ImplementationTestContextV1:
    return ImplementationTestContextV1(
        "tests/test_implementation_context.py",
        ImplementationContextStateV1.PROPOSED,
        "app.implementation_context.models.ImplementationContextV1",
        "normalizes unordered records before digesting",
        "equivalent contexts share one digest",
        "different input order changes no semantic payload",
    )


def _context(**changes: object) -> ImplementationContextV1:
    values: dict[str, object] = {
        "goal": "Prepare a bounded implementation context contract.",
        "source_commit": COMMIT,
        "repository_clean": True,
        "knowledge_identity": "chappie_knowledge/1.0.0",
        "knowledge_manifest_digest": DIGEST,
        "knowledge_context_digest": DIGEST,
        "evidence_digest": DIGEST,
        "canonical_owner": "app.implementation_context",
        "current_items": (
            ImplementationContextItemV1(
                "app.implementation_context",
                ImplementationContextStateV1.CURRENT,
                "neutral implementation contract package",
                current_locator="app/implementation_context/models.py",
                source_commit=COMMIT,
                evidence_refs=("knowledge.implementation_context",),
            ),
        ),
        "direct_files": ("app/implementation_context/models.py",),
        "symbols": (_symbol(),),
        "contracts": (_contract(),),
        "tests": (_test(),),
        "authority": _authority(),
        "acceptance_criteria": ("digest is deterministic",),
    }
    values.update(changes)
    return ImplementationContextV1(**values)  # type: ignore[arg-type]


def test_valid_minimal_context_has_stable_identity_digest_and_sufficiency() -> None:
    context = _context()
    assert context.stable_payload()["identity"] == IMPLEMENTATION_CONTEXT_IDENTITY
    assert len(context.digest) == 64
    assert assess_implementation_context_sufficiency(context).state is (
        ImplementationContextSufficiencyStateV1.IMPLEMENTATION_CONTEXT_SUFFICIENT
    )


def test_unordered_and_duplicate_semantic_input_normalizes_to_one_digest() -> None:
    first = _context(
        acceptance_criteria=("z", "a", "z"),
        direct_files=(
            "tests/test_implementation_context.py",
            "app/implementation_context/models.py",
        ),
    )
    second = _context(
        acceptance_criteria=("a", "z"),
        direct_files=(
            "app/implementation_context/models.py",
            "tests/test_implementation_context.py",
        ),
    )
    assert first.acceptance_criteria == ("a", "z")
    assert first.digest == second.digest


def test_current_proposed_unknown_contracts_are_strict_and_contradictions_reject() -> None:
    with pytest.raises(ValueError, match="CURRENT item requires"):
        ImplementationContextItemV1("current", ImplementationContextStateV1.CURRENT, "x")
    proposed = ImplementationContextItemV1(
        "future.contract",
        ImplementationContextStateV1.PROPOSED,
        "future scope",
        canonical_owner="app.implementation_context",
        proposed_locator="app/implementation_context/future.py",
    )
    assert proposed.current_locator is None
    unknown = ImplementationContextItemV1(
        "app.implementation_context.future_symbol",
        ImplementationContextStateV1.UNKNOWN,
        "required future symbol is unknown",
        missing_kind=MissingContextRequestKindV1.SYMBOL,
        missing_reason="DIRECT_SYMBOL_REQUIRED",
    )
    assert unknown.state is ImplementationContextStateV1.UNKNOWN
    duplicate = _context().current_items[0]
    with pytest.raises(ValueError, match="contradictory"):
        _context(
            proposed_items=(
                ImplementationContextItemV1(
                    duplicate.identifier,
                    ImplementationContextStateV1.PROPOSED,
                    "conflicting future item",
                    canonical_owner="app.implementation_context",
                ),
            )
        )


def test_symbol_contract_test_and_relation_validation() -> None:
    with pytest.raises(ValueError, match="safe repository-relative"):
        ImplementationSymbolV1(
            ImplementationSymbolKindV1.DTO,
            "unsafe",
            "app.implementation_context",
            "../models.py",
            "x",
            ImplementationContextStateV1.PROPOSED,
        )
    proposed = ImplementationContractContextV1(
        "future.contract",
        "app.implementation_context",
        ImplementationContextStateV1.PROPOSED,
        proposed_delta=("add bounded field",),
    )
    assert proposed.current_shape == ()
    with pytest.raises(ValueError, match="too generic"):
        ImplementationTestContextV1(
            "tests/test_implementation_context.py",
            ImplementationContextStateV1.PROPOSED,
            "future.contract",
            "add tests",
            "passes",
            "fails",
        )
    relation = ImplementationRelationV1(
        _symbol().qualified_identifier,
        ImplementationRelationKindV1.PRODUCES,
        _contract().identity,
        "app/implementation_context/models.py",
        "knowledge.implementation_context",
    )
    assert relation.relation is ImplementationRelationKindV1.PRODUCES
    with pytest.raises(ValueError, match="not present"):
        _context(
            relations=(
                ImplementationRelationV1(
                    "unknown.source", ImplementationRelationKindV1.CALLS, "unknown.target"
                ),
            )
        )


def test_owner_authority_and_mutation_authority_are_required_and_strict() -> None:
    assert assess_implementation_context_sufficiency(_context(canonical_owner=None)).state is (
        ImplementationContextSufficiencyStateV1.INSUFFICIENT
    )
    assert assess_implementation_context_sufficiency(_context(authority=None)).state is (
        ImplementationContextSufficiencyStateV1.INSUFFICIENT
    )
    with pytest.raises(ValueError, match="mutation authority"):
        AuthorityBoundaryV1(("write files",), ("order execution",), "HUMAN_APPROVAL_REQUIRED")


def test_exact_unknown_produces_targeted_expansion_but_missing_provenance_is_insufficient() -> None:
    unknown = ImplementationContextItemV1(
        "app.implementation_context.adapters.FutureAdapter",
        ImplementationContextStateV1.UNKNOWN,
        "adapter interface is not yet selected",
        missing_kind=MissingContextRequestKindV1.SYMBOL,
        missing_reason="DIRECT_ADAPTER_SYMBOL_REQUIRED",
    )
    assessment = assess_implementation_context_sufficiency(_context(unknown_items=(unknown,)))
    assert assessment.state is ImplementationContextSufficiencyStateV1.TARGETED_EXPANSION_REQUIRED
    assert assessment.missing_context[0].exact_target == unknown.identifier
    assert assess_implementation_context_sufficiency(_context(source_commit=None)).state is (
        ImplementationContextSufficiencyStateV1.INSUFFICIENT
    )
    request = MissingContextRequestV1(
        MissingContextRequestKindV1.SYMBOL,
        "app.implementation_context.adapters.FutureAdapter",
        "adapter boundary needed",
        _context().digest,
    )
    assert request.response_budget == 1
    with pytest.raises(ValueError):
        MissingContextRequestV1(
            MissingContextRequestKindV1.SYMBOL, "*", "all symbols", _context().digest
        )


def test_core_is_consumer_neutral_and_existing_development_contracts_remain_separate() -> None:
    names = {item.name.lower() for item in fields(ImplementationContextV1)}
    assert not {"codex", "claude"} & names
    from app.development_analysis import CODEX_TASK_IDENTITY, DEVELOPMENT_ANALYSIS_IDENTITY

    assert CODEX_TASK_IDENTITY == "chappie_codex_handoff/1.0.0"
    assert DEVELOPMENT_ANALYSIS_IDENTITY == "chappie_development_analysis/2.0.0"


def test_core_has_no_graph_memory_dependency() -> None:
    source = Path("app/implementation_context/models.py").read_text(encoding="utf-8").lower()
    assert "networkx" not in source
    assert "neo4j" not in source
    assert "graph database" not in source
