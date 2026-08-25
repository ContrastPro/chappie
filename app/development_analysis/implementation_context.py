"""Pure projection of validated Development artifacts into neutral context.

This producer-side adapter deliberately has no retrieval, model, filesystem,
Git, shell, or runtime authority.  It does not replace Development Analysis or
the advisory handoff contract.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.ai.schemas import digest
from app.development_analysis.context import DevelopmentContextMapV1
from app.development_analysis.models import (
    DevelopmentAnalysisStateV1,
    DevelopmentAnalysisV1,
)
from app.development_analysis.synthesis import (
    DEVELOPMENT_GROUNDING_IDENTITY,
    DEVELOPMENT_PROPOSAL_IDENTITY,
)
from app.development_analysis.task import CodexTaskStateV1, CodexTaskV1
from app.implementation_context import (
    AuthorityBoundaryV1,
    ImplementationContextItemV1,
    ImplementationContextStateV1,
    ImplementationContextV1,
    ImplementationContractContextV1,
    ImplementationRelationKindV1,
    ImplementationRelationV1,
    ImplementationSymbolKindV1,
    ImplementationSymbolV1,
    ImplementationTestContextV1,
    MissingContextRequestKindV1,
)

_CURRENT_CONTRACT_IDENTITIES = {
    "DevelopmentAnalysisV1": "chappie_development_analysis/2.0.0",
    "CodexHandoffV1": "chappie_codex_handoff/1.0.0",
    "CodexTaskV1": "chappie_codex_handoff/1.0.0",
}
_DIFF_REVIEW_IDENTITY = "chappie_development_diff_review/1.0.0"
_DIFF_REVIEW_NAME = "DevelopmentDiffReviewV1"


@dataclass(frozen=True, slots=True)
class DevelopmentProjectionComparisonV1:
    """Test/documentation-only summary of deterministic projection coverage."""

    preserved: tuple[str, ...]
    adapted: tuple[str, ...]
    intentionally_consumer_specific: tuple[str, ...]
    missing: tuple[str, ...]


def build_implementation_context_from_development(
    context_map: DevelopmentContextMapV1,
    analysis: DevelopmentAnalysisV1,
    handoff: CodexTaskV1 | None = None,
) -> ImplementationContextV1:
    """Transform already-validated Development DTOs without accessing external state."""

    if analysis.state is not DevelopmentAnalysisStateV1.OK or analysis.evidence is None:
        raise ValueError("development analysis must be validated before projection")
    return _build_implementation_context(context_map, analysis, handoff)


def build_partial_implementation_context_from_development(
    context_map: DevelopmentContextMapV1,
    analysis: DevelopmentAnalysisV1,
) -> ImplementationContextV1:
    """Project only a preserved, validated Grounding+Proposal partial for discovery."""

    if not is_discovery_safe_partial_development_analysis(analysis):
        raise ValueError("development partial is not safe for discovery projection")
    return _build_implementation_context(context_map, analysis, None)


def is_discovery_safe_partial_development_analysis(analysis: DevelopmentAnalysisV1) -> bool:
    """Recognize only the internal late-stage partial shape; never trust Review output."""

    staged = analysis.staged_results
    grounding = staged.get("grounding") if isinstance(staged, dict) else None
    proposal = staged.get("proposal") if isinstance(staged, dict) else None
    known_handles = (
        {handle.handle for handle in analysis.context_map.evidence_handles}
        if analysis.context_map
        else set()
    )
    return bool(
        analysis.state is DevelopmentAnalysisStateV1.ANALYSIS_INVALID
        and analysis.validation_status == "PARTIAL"
        and analysis.evidence is not None
        and analysis.context_map is not None
        and analysis.evidence.repository_clean
        and analysis.current_evidence_handles
        and analysis.proposed_scope is not None
        and analysis.proposed_scope.proposed_owner is not None
        and isinstance(grounding, dict)
        and grounding.get("identity") == DEVELOPMENT_GROUNDING_IDENTITY
        and isinstance(proposal, dict)
        and proposal.get("identity") == DEVELOPMENT_PROPOSAL_IDENTITY
        and staged.get("proposal_status") == "PROPOSED_IMPLEMENTATION_NOT_AUTHORIZED"
        and len(set(analysis.current_evidence_handles)) == len(analysis.current_evidence_handles)
        and set(analysis.current_evidence_handles).issubset(known_handles)
        and all(
            _safe_repository_relative_path(path)
            for path in (
                *analysis.proposed_scope.proposed_modified_files,
                *analysis.proposed_scope.proposed_new_files,
            )
        )
    )


def _safe_repository_relative_path(value: str) -> bool:
    normalized = value.replace("\\", "/")
    return (
        bool(value)
        and not normalized.startswith(("/", "../"))
        and ":" not in value
        and ".." not in normalized.split("/")
    )


def _build_implementation_context(
    context_map: DevelopmentContextMapV1,
    analysis: DevelopmentAnalysisV1,
    handoff: CodexTaskV1 | None,
) -> ImplementationContextV1:
    """Build neutral context after the caller selects a fully-valid or safe-partial gate."""

    if analysis.context_map is not None and analysis.context_map != context_map:
        raise ValueError("development context map does not match analysis")
    if not analysis.evidence.repository_clean:
        raise ValueError("development evidence must bind a clean repository")
    if (
        context_map.source_commit != analysis.evidence.source_commit
        or context_map.evidence_digest != analysis.evidence.evidence_digest
        or context_map.knowledge_identity != analysis.evidence.knowledge_identity
        or context_map.knowledge_manifest_digest != analysis.evidence.knowledge_manifest_digest
        or context_map.knowledge_context_digest != analysis.evidence.knowledge_context_digest
    ):
        raise ValueError("development context provenance does not match analysis evidence")
    if not analysis.proposed_scope or not analysis.proposed_scope.proposed_owner:
        raise ValueError("development projection requires a canonical proposed owner")
    if analysis.proposed_scope.proposed_owner not in context_map.owner_candidates:
        raise ValueError("development projection owner is not request-local")
    _validate_handoff(handoff, analysis, context_map)

    direct_files = tuple(sorted(set(context_map.existing_files)))
    if not direct_files:
        raise ValueError("development projection requires bounded source locators")
    owner = analysis.proposed_scope.proposed_owner
    evidence_refs = tuple(sorted({handle.fact_id for handle in context_map.evidence_handles})) or (
        context_map.context_digest,
    )
    current_items = tuple(
        ImplementationContextItemV1(
            f"current_file.{_short_digest(path)}",
            ImplementationContextStateV1.CURRENT,
            "Current direct supporting file from Development context.",
            canonical_owner=owner,
            current_locator=path,
            source_commit=context_map.source_commit,
            evidence_refs=(context_map.context_digest,),
        )
        for path in direct_files
    ) + tuple(
        ImplementationContextItemV1(
            f"evidence.{handle.handle}",
            ImplementationContextStateV1.CURRENT,
            handle.statement,
            canonical_owner=owner,
            current_locator=direct_files[0],
            source_commit=context_map.source_commit,
            evidence_refs=(handle.fact_id, handle.record_digest),
        )
        for handle in context_map.evidence_handles
    )
    proposed_items = tuple(
        ImplementationContextItemV1(
            f"proposed_file.{_short_digest(path)}",
            ImplementationContextStateV1.PROPOSED,
            "Proposed direct implementation file from validated Development scope.",
            canonical_owner=owner,
            proposed_locator=path,
        )
        for path in tuple(
            sorted(
                set(
                    analysis.proposed_scope.proposed_modified_files
                    + analysis.proposed_scope.proposed_new_files
                )
            )
        )
    )
    unknown_items = _unknown_items(analysis, context_map)
    symbols = _symbols(context_map, analysis, handoff, owner, direct_files, evidence_refs)
    contracts = _contracts(context_map, analysis, owner)
    tests, test_unknowns = _tests(analysis, context_map)
    unknown_items = tuple(
        sorted(
            {item.identifier: item for item in (*unknown_items, *test_unknowns)}.values(),
            key=lambda item: item.identifier,
        )
    )
    relations = _relations(symbols, contracts, analysis, handoff)
    return ImplementationContextV1(
        goal=analysis.request,
        source_commit=context_map.source_commit,
        repository_clean=True,
        knowledge_identity=context_map.knowledge_identity,
        knowledge_manifest_digest=context_map.knowledge_manifest_digest,
        knowledge_context_digest=context_map.knowledge_context_digest,
        evidence_digest=context_map.evidence_digest,
        canonical_owner=owner,
        current_items=current_items,
        proposed_items=proposed_items,
        unknown_items=unknown_items,
        direct_files=direct_files,
        symbols=symbols,
        contracts=contracts,
        tests=tests,
        relations=relations,
        authority=AuthorityBoundaryV1(
            ("prepare read-only advisory implementation context",),
            tuple(sorted(set(context_map.authority_constraints)))
            or ("No operational mutation authority.",),
            "HUMAN_APPROVAL_REQUIRED",
        ),
        acceptance_criteria=analysis.acceptance_criteria,
        risks=analysis.risks,
        search_guidance=(
            "Verify only supplied direct files, symbols, and contracts before requesting expansion.",
        ),
    )


def compare_development_projection(
    context: ImplementationContextV1,
    analysis: DevelopmentAnalysisV1,
    handoff: CodexTaskV1 | None = None,
) -> DevelopmentProjectionComparisonV1:
    """Provide deterministic test/documentation coverage without consumer prose."""

    preserved = ["goal", "source_commit", "knowledge_bindings", "evidence_digest", "owner"]
    if analysis.facts:
        preserved.append("current_facts")
    if analysis.proposed_scope:
        preserved.append("proposed_scope")
    if analysis.unknowns:
        preserved.append("unknowns")
    if context.tests:
        preserved.append("direct_tests")
    adapted = ("bounded_current_items", "generic_search_guidance")
    consumer_specific = ("handoff_task_id", "handoff_projection") if handoff else ()
    missing = tuple(item.identifier for item in context.unknown_items)
    return DevelopmentProjectionComparisonV1(
        tuple(sorted(preserved)), adapted, consumer_specific, missing
    )


def _validate_handoff(
    handoff: CodexTaskV1 | None,
    analysis: DevelopmentAnalysisV1,
    context_map: DevelopmentContextMapV1,
) -> None:
    if handoff is None:
        return
    if (
        handoff.state is not CodexTaskStateV1.ADVISORY
        or handoff.source_commit != context_map.source_commit
        or handoff.evidence_digest != context_map.evidence_digest
        or handoff.context_digest != context_map.context_digest
        or handoff.problem != analysis.request
        or handoff.approval_status != "HUMAN_APPROVAL_REQUIRED"
    ):
        raise ValueError("development handoff is not bound to the supplied analysis")


def _unknown_items(
    analysis: DevelopmentAnalysisV1, context_map: DevelopmentContextMapV1
) -> tuple[ImplementationContextItemV1, ...]:
    items = [
        ImplementationContextItemV1(
            f"unknown.{_short_digest(value)}",
            ImplementationContextStateV1.UNKNOWN,
            value,
            missing_kind=MissingContextRequestKindV1.RELATION,
            missing_reason="DEVELOPMENT_ANALYSIS_UNKNOWN",
        )
        for value in analysis.unknowns
    ]
    if _DIFF_REVIEW_NAME in analysis.affected_contracts:
        items.extend(
            (
                ImplementationContextItemV1(
                    "app.development_analysis.models.DevelopmentDiffReviewV1",
                    ImplementationContextStateV1.UNKNOWN,
                    "Exact proposed DiffReview DTO shape remains unimplemented.",
                    missing_kind=MissingContextRequestKindV1.SYMBOL,
                    missing_reason="PLANNED_DIFF_REVIEW_SYMBOL_SHAPE_REQUIRED",
                ),
                ImplementationContextItemV1(
                    "tests.test_development_diff_review.direct_targets",
                    ImplementationContextStateV1.UNKNOWN,
                    "Exact direct DiffReview test targets remain planned.",
                    missing_kind=MissingContextRequestKindV1.TEST,
                    missing_reason="PLANNED_DIFF_REVIEW_TEST_TARGET_REQUIRED",
                ),
            )
        )
    return tuple(items)


def _symbols(
    context_map: DevelopmentContextMapV1,
    analysis: DevelopmentAnalysisV1,
    handoff: CodexTaskV1 | None,
    owner: str,
    direct_files: tuple[str, ...],
    evidence_refs: tuple[str, ...],
) -> tuple[ImplementationSymbolV1, ...]:
    source_file = direct_files[0]
    symbols = [
        _current_symbol(
            ImplementationSymbolKindV1.MODULE,
            owner,
            owner,
            source_file,
            "Current owner module identified by Development context.",
            context_map.source_commit,
            evidence_refs,
        ),
        _current_symbol(
            ImplementationSymbolKindV1.DTO,
            "DevelopmentAnalysisV1",
            owner,
            "app/development_analysis/models.py",
            "Current bounded Development analysis DTO; no signature is projected.",
            context_map.source_commit,
            evidence_refs,
        ),
    ]
    if handoff is not None:
        symbols.append(
            _current_symbol(
                ImplementationSymbolKindV1.DTO,
                "CodexHandoffV1",
                owner,
                "app/development_analysis/task.py",
                "Current advisory handoff DTO; consumer presentation is excluded.",
                context_map.source_commit,
                evidence_refs,
            )
        )
    if any("POST /v1/development/analyze" in value for value in analysis.facts):
        symbols.append(
            _current_symbol(
                ImplementationSymbolKindV1.ENDPOINT,
                "app.control_plane.transport.development_analyze_endpoint",
                "app.control_plane",
                "app/control_plane/transport.py",
                "Current read-only Development analysis endpoint.",
                context_map.source_commit,
                evidence_refs,
            )
        )
    if any("POST /v1/development/task" in value for value in analysis.facts):
        symbols.append(
            _current_symbol(
                ImplementationSymbolKindV1.ENDPOINT,
                "app.control_plane.transport.development_task_endpoint",
                "app.control_plane",
                "app/control_plane/transport.py",
                "Current read-only advisory task endpoint.",
                context_map.source_commit,
                evidence_refs,
            )
        )
    if "tests/test_codex_task.py" in context_map.relevant_tests:
        symbols.append(
            _current_symbol(
                ImplementationSymbolKindV1.TEST,
                "tests.test_codex_task",
                owner,
                "tests/test_codex_task.py",
                "Current direct advisory handoff test module.",
                context_map.source_commit,
                evidence_refs,
            )
        )
    if _DIFF_REVIEW_NAME in analysis.affected_contracts:
        symbols.append(
            ImplementationSymbolV1(
                ImplementationSymbolKindV1.DTO,
                _DIFF_REVIEW_NAME,
                owner,
                "app/development_analysis/models.py",
                "Proposed DiffReview DTO; exact shape is intentionally unknown.",
                ImplementationContextStateV1.PROPOSED,
            )
        )
    return tuple(symbols)


def _current_symbol(
    kind: ImplementationSymbolKindV1,
    identifier: str,
    owner: str,
    path: str,
    summary: str,
    source_commit: str,
    evidence_refs: tuple[str, ...],
) -> ImplementationSymbolV1:
    return ImplementationSymbolV1(
        kind,
        identifier,
        owner,
        path,
        summary,
        ImplementationContextStateV1.CURRENT,
        source_commit=source_commit,
        evidence_refs=evidence_refs,
    )


def _contracts(
    context_map: DevelopmentContextMapV1,
    analysis: DevelopmentAnalysisV1,
    owner: str,
) -> tuple[ImplementationContractContextV1, ...]:
    names = tuple(sorted({*context_map.existing_contracts, *analysis.affected_contracts}))
    contracts = []
    for name in names:
        if name == _DIFF_REVIEW_NAME:
            contracts.append(
                ImplementationContractContextV1(
                    _DIFF_REVIEW_IDENTITY,
                    owner,
                    ImplementationContextStateV1.PROPOSED,
                    proposed_delta=(
                        "Add the planned bounded DiffReview input/output contract without runtime evidence.",
                    ),
                    version="1.0.0",
                    versioned=True,
                )
            )
            continue
        contracts.append(
            ImplementationContractContextV1(
                _CURRENT_CONTRACT_IDENTITIES.get(
                    name, f"development_contract.{_short_digest(name)}"
                ),
                owner,
                ImplementationContextStateV1.CURRENT,
                current_shape=("Current contract referenced by bounded Development evidence.",),
                source_commit=context_map.source_commit,
                evidence_refs=(context_map.context_digest,),
            )
        )
    return tuple(contracts)


def _tests(
    analysis: DevelopmentAnalysisV1, context_map: DevelopmentContextMapV1
) -> tuple[tuple[ImplementationTestContextV1, ...], tuple[ImplementationContextItemV1, ...]]:
    tests: list[ImplementationTestContextV1] = []
    unknowns: list[ImplementationContextItemV1] = []
    for value in analysis.required_tests:
        parts = tuple(item.strip() for item in value.split(" | "))
        if len(parts) == 4 and "::" in parts[0]:
            module_path, target = parts[0].split("::", 1)
            tests.append(
                ImplementationTestContextV1(
                    module_path,
                    ImplementationContextStateV1.PROPOSED,
                    target,
                    parts[1],
                    parts[2],
                    parts[3],
                )
            )
            continue
        module = (
            context_map.relevant_tests[0] if context_map.relevant_tests else "tests.test_target"
        )
        unknowns.append(
            ImplementationContextItemV1(
                f"test_detail.{_short_digest(value)}",
                ImplementationContextStateV1.UNKNOWN,
                value,
                missing_kind=MissingContextRequestKindV1.TEST,
                missing_reason=f"DIRECT_TEST_TARGET_REQUIRED:{module}",
            )
        )
    return tuple(tests), tuple(unknowns)


def _relations(
    symbols: tuple[ImplementationSymbolV1, ...],
    contracts: tuple[ImplementationContractContextV1, ...],
    analysis: DevelopmentAnalysisV1,
    handoff: CodexTaskV1 | None,
) -> tuple[ImplementationRelationV1, ...]:
    refs = {item.qualified_identifier for item in symbols} | {item.identity for item in contracts}
    owner = analysis.proposed_scope.proposed_owner if analysis.proposed_scope else None
    relations: list[ImplementationRelationV1] = []
    if owner and owner in refs and "DevelopmentAnalysisV1" in refs:
        relations.append(
            ImplementationRelationV1(
                owner, ImplementationRelationKindV1.OWNS, "DevelopmentAnalysisV1"
            )
        )
    if handoff and owner and owner in refs and "CodexHandoffV1" in refs:
        relations.append(
            ImplementationRelationV1(owner, ImplementationRelationKindV1.OWNS, "CodexHandoffV1")
        )
    if (
        "app.control_plane.transport.development_analyze_endpoint" in refs
        and "DevelopmentAnalysisV1" in refs
    ):
        relations.append(
            ImplementationRelationV1(
                "app.control_plane.transport.development_analyze_endpoint",
                ImplementationRelationKindV1.EXPOSES,
                "DevelopmentAnalysisV1",
            )
        )
    if "app.control_plane.transport.development_task_endpoint" in refs and "CodexHandoffV1" in refs:
        relations.append(
            ImplementationRelationV1(
                "app.control_plane.transport.development_task_endpoint",
                ImplementationRelationKindV1.PRODUCES,
                "CodexHandoffV1",
            )
        )
    if "tests.test_codex_task" in refs and "CodexHandoffV1" in refs:
        relations.append(
            ImplementationRelationV1(
                "tests.test_codex_task", ImplementationRelationKindV1.VALIDATES, "CodexHandoffV1"
            )
        )
    if owner and owner in refs and _DIFF_REVIEW_NAME in refs:
        relations.append(
            ImplementationRelationV1(owner, ImplementationRelationKindV1.OWNS, _DIFF_REVIEW_NAME)
        )
    return tuple(relations)


def _short_digest(value: str) -> str:
    return digest(value)[:16]
