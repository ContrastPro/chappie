from dataclasses import replace

import pytest

from app.ai.errors import AIValidationError
from app.ai.model_policy import DevelopmentModelRoleV1
from app.development_analysis.context import build_development_context_map
from app.development_analysis.models import (
    DevelopmentAnalysisReadinessV1,
    DevelopmentAnalysisStateV1,
    DevelopmentAnalysisV1,
    DevelopmentEvidencePackV1,
    evidence_digest,
)
from app.development_analysis.service import _development_retrieval_request
from app.development_analysis.synthesis import parse_development_synthesis
from app.development_analysis.task import CodexTaskPreparationRequestV1, prepare_codex_task
from app.knowledge import RepositorySnapshotV1, initial_records, manifest
from app.knowledge.retrieval import retrieve


def _inputs():
    snapshot = RepositorySnapshotV1("a" * 40, True)
    records = initial_records(snapshot=snapshot)
    knowledge_manifest = manifest(records, snapshot)
    _result, context = retrieve(
        records,
        knowledge_manifest,
        _development_retrieval_request(
            records,
            "Design a bounded diff review using DevelopmentAnalysisV1 and CodexTaskV1.",
            None,
        ),
    )
    raw = {
        "source_commit": snapshot.commit,
        "repository_clean": True,
        "knowledge_identity": "chappie_knowledge/1.0.0",
        "knowledge_manifest_digest": knowledge_manifest.manifest_digest,
        "knowledge_context_digest": context.context_digest,
        "workspace_context_digest": None,
        "requested_role": DevelopmentModelRoleV1.CODE,
        "requested_model": "qwen3-coder:30b",
        "effective_model": "qwen3-coder:30b",
        "profile_identity": "CODE_ADVISORY",
        "capability_identity": "development_model_role/CODE_ADVISORY",
        "record_ids": tuple(record.knowledge_id for record in context.records),
        "record_digests": tuple(record.record_digest for record in context.records),
        "allowed_fact_ids": tuple(
            sorted(f"knowledge.{record.knowledge_id}.statement" for record in context.records)
        ),
    }
    evidence = DevelopmentEvidencePackV1(**raw, evidence_digest=evidence_digest(raw))
    return context, evidence, build_development_context_map(context, evidence)


def _payload(context_map, **changes):
    owner = context_map.owner_candidates[0]
    payload = {
        "current_evidence_handles": ["E1", "E2"],
        "inferences": ["The supplied development evidence supports a bounded proposal."],
        "unknowns": ["The final transport shape cannot be established from current evidence."],
        "proposed_scope": {
            "proposed_owner": owner,
            "proposed_modified_files": ["app/development_analysis/task.py"],
            "proposed_new_files": ["app/development_analysis/diff_review.py"],
            "proposed_contract_changes": ["Add an advisory diff-review DTO."],
            "proposed_components": ["Bounded diff review"],
            "proposed_behavior": ["Compare bounded change evidence without mutation."],
            "rationale": ["The request is a future design proposal."],
        },
        "risks": ["Diff review must retain the advisory authority boundary."],
        "required_tests": ["tests/test_development_analysis.py covers diff-review validation."],
        "non_goals": ["No file, Git, shell, Knowledge, trading, Risk, or execution mutation."],
        "acceptance_criteria": ["The proposed diff review remains evidence-bound and advisory."],
        "confidence": 0.5,
    }
    payload.update(changes)
    return payload


def test_evidence_handles_are_deterministic_and_preserve_canonical_provenance() -> None:
    context, evidence, first = _inputs()
    second = build_development_context_map(context, evidence)
    assert first == second
    assert first.context_digest == second.context_digest
    assert [item.handle for item in first.evidence_handles] == [
        f"E{index}" for index in range(1, len(first.evidence_handles) + 1)
    ]
    assert all(
        item.fact_id.startswith("knowledge.") and item.record_digest
        for item in first.evidence_handles
    )


def test_owner_candidates_are_request_local_canonical_package_identities() -> None:
    context, evidence, first = _inputs()
    assert first.owner_candidates == ("app.development_analysis",)
    assert "MODULE_RESPONSIBILITY" not in first.owner_candidates
    assert "CONTRACT" not in first.owner_candidates

    ownership_record = next(
        record
        for record in context.records
        if record.title == "Package ownership: Development Intelligence"
    )
    duplicate_context = replace(context, records=(*context.records, ownership_record))
    duplicate = build_development_context_map(duplicate_context, evidence)
    assert duplicate.owner_candidates == first.owner_candidates

    without_ownership = replace(
        context,
        records=tuple(
            record
            for record in context.records
            if record.knowledge_type.value != "MODULE_RESPONSIBILITY"
        ),
    )
    assert build_development_context_map(without_ownership, evidence).owner_candidates == ()


def test_current_handles_are_strict_but_proposed_new_files_are_not_existing_facts() -> None:
    context, _evidence, context_map = _inputs()
    output = parse_development_synthesis(_payload(context_map), context, context_map=context_map)
    assert output.proposed_scope.proposed_new_files == ("app/development_analysis/diff_review.py",)
    with pytest.raises(AIValidationError, match="unsupported Knowledge"):
        parse_development_synthesis(
            _payload(context_map, current_evidence_handles=["E999"]),
            context,
            context_map=context_map,
        )


@pytest.mark.parametrize("path", ("../escape.py", "data/runtime.py", "C:/unsafe.py"))
def test_proposed_scope_paths_fail_closed(path: str) -> None:
    context, _evidence, context_map = _inputs()
    payload = _payload(context_map)
    payload["proposed_scope"]["proposed_new_files"] = [path]
    with pytest.raises(AIValidationError, match="proposed path"):
        parse_development_synthesis(payload, context, context_map=context_map)


def test_proposed_owner_must_be_an_architecture_candidate() -> None:
    context, _evidence, context_map = _inputs()
    payload = _payload(context_map)
    payload["proposed_scope"]["proposed_owner"] = "INVENTED_OWNER"
    with pytest.raises(AIValidationError, match="unsupported Knowledge"):
        parse_development_synthesis(payload, context, context_map=context_map)


def test_ready_analysis_emits_deterministic_bounded_codex_handoff() -> None:
    context, evidence, context_map = _inputs()
    output = parse_development_synthesis(_payload(context_map), context, context_map=context_map)
    facts = tuple(
        item.statement
        for item in context_map.evidence_handles
        if item.handle in output.current_evidence_handles
    )
    analysis = DevelopmentAnalysisV1(
        DevelopmentAnalysisStateV1.OK,
        "Design a bounded diff review for DevelopmentAnalysisV1.",
        evidence=evidence,
        facts=facts,
        inferences=output.inferences,
        unknowns=output.unknowns,
        architecture_owners=tuple(
            sorted(
                item.category
                for item in context_map.evidence_handles
                if item.handle in output.current_evidence_handles
            )
        ),
        affected_contracts=context_map.existing_contracts,
        likely_files=context_map.existing_files,
        risks=output.risks,
        required_tests=output.required_tests,
        non_goals=output.non_goals,
        acceptance_criteria=output.acceptance_criteria,
        validation_status="VALID",
        readiness=DevelopmentAnalysisReadinessV1.READY_FOR_TASK,
        context_map=context_map,
        current_evidence_handles=output.current_evidence_handles,
        proposed_scope=output.proposed_scope,
    )
    first = prepare_codex_task(CodexTaskPreparationRequestV1(analysis))
    second = prepare_codex_task(CodexTaskPreparationRequestV1(analysis))
    assert first.state.value == "ADVISORY"
    assert first.payload() == second.payload()
    assert first.context_digest == context_map.context_digest
    assert "Begin from this bounded evidence pack" in first.projection()
    assert "app/development_analysis/diff_review.py" in first.projection()
    blocked = prepare_codex_task(
        CodexTaskPreparationRequestV1(
            replace(analysis, readiness=DevelopmentAnalysisReadinessV1.NEEDS_REVIEW)
        )
    )
    assert blocked.reason_code == "SOURCE_ANALYSIS_NOT_READY"
