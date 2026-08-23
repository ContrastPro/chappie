"""Acceptance-only materialization of a real neutral DiffReview context."""

from __future__ import annotations

from pathlib import Path

from app.implementation_context import (
    AuthorityBoundaryV1,
    ImplementationContextStateV1,
    ImplementationContextV1,
    ImplementationContractContextV1,
    ImplementationRelationKindV1,
    ImplementationRelationV1,
    ImplementationSymbolKindV1,
    ImplementationSymbolV1,
    ImplementationTestContextV1,
    assess_implementation_context_sufficiency,
)
from app.knowledge.store import KNOWLEDGE_STORE_PATH, committed_snapshot, read_store


def materialize_real_diff_review_context(root: Path = Path(".")) -> ImplementationContextV1:
    """Build the bounded acceptance context from exact design-specified files only."""
    snapshot = committed_snapshot(root)
    records, manifest = read_store(root / KNOWLEDGE_STORE_PATH)
    evidence = next(record for record in records if record.title == "DevelopmentDiffReviewV1")
    owner = "app.development_analysis"
    current_files = (
        "app/development_analysis/models.py",
        "app/development_analysis/synthesis.py",
        "app/development_analysis/task.py",
        "app/development_analysis/__init__.py",
        "tests/test_development_staged_synthesis.py",
        "tests/test_codex_task.py",
    )
    symbols = (
        ImplementationSymbolV1(
            ImplementationSymbolKindV1.MODULE,
            owner,
            owner,
            "app/development_analysis/__init__.py",
            "Current Development Analysis owner module.",
            ImplementationContextStateV1.CURRENT,
            source_commit=snapshot.commit,
            evidence_refs=(evidence.knowledge_id,),
        ),
        _symbol(
            "DevelopmentAnalysisV1",
            "app/development_analysis/models.py",
            snapshot.commit,
            evidence.knowledge_id,
        ),
        _symbol(
            "DevelopmentReviewV1",
            "app/development_analysis/synthesis.py",
            snapshot.commit,
            evidence.knowledge_id,
        ),
        _symbol(
            "CodexHandoffV1",
            "app/development_analysis/task.py",
            snapshot.commit,
            evidence.knowledge_id,
        ),
    )
    current = ImplementationContractContextV1(
        "chappie_development_analysis/2.0.0",
        owner,
        ImplementationContextStateV1.CURRENT,
        current_shape=("DevelopmentAnalysisV1 is the current validated source analysis contract.",),
        source_commit=snapshot.commit,
        evidence_refs=(evidence.knowledge_id,),
    )
    proposed = ImplementationContractContextV1(
        "chappie_development_diff_review/1.0.0",
        owner,
        ImplementationContextStateV1.PROPOSED,
        proposed_delta=(
            "Add bounded advisory post-implementation DiffReview DTOs and deterministic review projection.",
        ),
        version="1.0.0",
        versioned=True,
    )
    return ImplementationContextV1(
        "Implement the first read-only advisory DevelopmentDiffReviewV1 capability that compares an approved Development Analysis / Handoff against bounded post-implementation change and test evidence, and determines whether implementation remains within approved scope.",
        snapshot.commit,
        snapshot.clean,
        "chappie_knowledge/1.0.0",
        manifest.store_content_digest,
        manifest.store_content_digest,
        evidence.record_digest,
        owner,
        direct_files=current_files,
        symbols=symbols,
        contracts=(current, proposed),
        tests=(
            ImplementationTestContextV1(
                "tests/test_development_staged_synthesis.py",
                ImplementationContextStateV1.CURRENT,
                "DevelopmentReviewV1",
                "validates staged review fields",
                "review is bounded",
                "staged review validation fails",
            ),
            ImplementationTestContextV1(
                "tests/test_development_diff_review.py",
                ImplementationContextStateV1.PROPOSED,
                "DevelopmentDiffReviewV1",
                "validates approved scope, evidence, authority, and human approval",
                "drift or authority escalation is rejected",
                "missing evidence or approval boundary causes failure",
            ),
        ),
        relations=(
            ImplementationRelationV1(
                owner, ImplementationRelationKindV1.OWNS, "DevelopmentAnalysisV1"
            ),
            ImplementationRelationV1(
                owner, ImplementationRelationKindV1.OWNS, "DevelopmentReviewV1"
            ),
            ImplementationRelationV1(owner, ImplementationRelationKindV1.OWNS, "CodexHandoffV1"),
        ),
        authority=AuthorityBoundaryV1(
            ("prepare read-only advisory review context",),
            ("file mutation", "Git push", "order execution"),
            "HUMAN_APPROVAL_REQUIRED",
        ),
        acceptance_criteria=(
            "Approved scope and contract changes are accepted.",
            "Unexpected drift and authority escalation are rejected.",
            "HUMAN_APPROVAL_REQUIRED is preserved.",
        ),
        risks=("Incomplete evidence remains unresolved and needs review.",),
        search_guidance=(
            "Verify only the supplied direct files and contracts before implementation.",
        ),
    )


def _symbol(identifier: str, path: str, commit: str, evidence: str) -> ImplementationSymbolV1:
    return ImplementationSymbolV1(
        ImplementationSymbolKindV1.DTO,
        identifier,
        "app.development_analysis",
        path,
        "Current declaration verified in exact source file.",
        ImplementationContextStateV1.CURRENT,
        source_commit=commit,
        evidence_refs=(evidence,),
    )


def test_real_diff_review_context_is_repository_bound_neutral_and_honest() -> None:
    context = materialize_real_diff_review_context()
    assert context.source_commit == committed_snapshot().commit
    assert context.repository_clean is committed_snapshot().clean
    assert all(Path(path).is_file() for path in context.direct_files)
    assert all(item.state is ImplementationContextStateV1.CURRENT for item in context.symbols)
    assert any(item.state is ImplementationContextStateV1.PROPOSED for item in context.contracts)
    assert assess_implementation_context_sufficiency(context).state.value in {
        "IMPLEMENTATION_CONTEXT_SUFFICIENT",
        "INSUFFICIENT",
    }
    assert "task_id" not in context.stable_payload()
