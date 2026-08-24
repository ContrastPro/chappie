"""Regression coverage for exact acceptance-only authorization persistence."""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from app.ai.model_policy import DevelopmentModelRoleV1
from app.development_analysis.context import DevelopmentContextMapV1, build_context_digest
from app.development_analysis.models import (
    DevelopmentAnalysisReadinessV1,
    DevelopmentAnalysisStateV1,
    DevelopmentAnalysisV1,
    DevelopmentEvidencePackV1,
    DevelopmentProposedScopeV1,
    evidence_digest,
)
from app.development_analysis.task import CodexTaskPreparationRequestV1, prepare_codex_handoff
from tools.acceptance.development_authorization_pair import (
    AUTHORIZATION_PAIR_FILENAME,
    AUTHORIZATION_PAIR_IDENTITY,
    authorization_pair_payload,
    load_authorization_pair,
    persist_authorization_pair,
)


def _analysis() -> DevelopmentAnalysisV1:
    raw = {
        "source_commit": "a" * 40,
        "repository_clean": True,
        "knowledge_identity": "chappie_knowledge/1.0.0",
        "knowledge_manifest_digest": "b" * 64,
        "knowledge_context_digest": "c" * 64,
        "workspace_context_digest": None,
        "requested_role": DevelopmentModelRoleV1.CODE,
        "requested_model": "qwen3-coder:30b",
        "effective_model": "qwen3-coder:30b",
        "profile_identity": "CODE_ADVISORY",
        "capability_identity": "development_model_role/CODE_ADVISORY",
        "record_ids": ("record",),
        "record_digests": ("d" * 64,),
        "allowed_fact_ids": ("knowledge.record.statement",),
    }
    evidence = DevelopmentEvidencePackV1(**raw, evidence_digest=evidence_digest(raw))
    context = DevelopmentContextMapV1(
        "a" * 40,
        "chappie_knowledge/1.0.0",
        "b" * 64,
        "c" * 64,
        evidence.evidence_digest,
        "CODE",
        "qwen3-coder:30b",
        (),
        ("app.development_analysis",),
        ("DevelopmentAnalysisV1",),
        ("app/development_analysis/task.py",),
        ("tests/test_development_authorization_pair.py",),
        ("Advisory only.",),
        "",
    )
    context = replace(context, context_digest=build_context_digest(context))
    return DevelopmentAnalysisV1(
        DevelopmentAnalysisStateV1.OK,
        "Implement bounded authorization-pair persistence.",
        evidence=evidence,
        facts=("The evidence pack is immutable.",),
        inferences=("A bounded task can be prepared.",),
        unknowns=("Human approval remains required.",),
        architecture_owners=("MODULE_RESPONSIBILITY",),
        affected_contracts=("DevelopmentAnalysisV1",),
        likely_files=("app/development_analysis/task.py",),
        risks=("Risk of missing test coverage.",),
        required_tests=("tests/test_development_authorization_pair.py",),
        non_goals=("No mutation.",),
        acceptance_criteria=("Task remains advisory.",),
        validation_status="VALID",
        readiness=DevelopmentAnalysisReadinessV1.READY_FOR_TASK,
        context_map=context,
        current_evidence_handles=("E1",),
        proposed_scope=DevelopmentProposedScopeV1(
            "app.development_analysis",
            ("app/development_analysis/task.py",),
            ("app/development_analysis/authorization_pair.py",),
            ("Persist an exact authorization pair.",),
            ("Authorization pair",),
            ("Remain advisory.",),
            ("Bounded source DTOs.",),
        ),
    )


def _pair():
    analysis = _analysis()
    handoff = prepare_codex_handoff(CodexTaskPreparationRequestV1(analysis))
    assert handoff.validation_status == "VALID"
    return analysis, handoff


def test_authorization_pair_round_trips_exact_public_dtos(tmp_path: Path) -> None:
    analysis, handoff = _pair()
    artifact = tmp_path / AUTHORIZATION_PAIR_FILENAME
    persisted = persist_authorization_pair(artifact, analysis, handoff)
    loaded = load_authorization_pair(artifact)
    raw = json.loads(artifact.read_text(encoding="utf-8"))

    assert raw["identity"] == AUTHORIZATION_PAIR_IDENTITY
    assert raw["analysis"] == analysis.payload()
    assert raw["handoff"] == handoff.payload()
    assert loaded.analysis.payload() == analysis.payload()
    assert loaded.handoff.payload() == handoff.payload()
    assert loaded.authorization_digest == persisted.authorization_digest
    assert raw["source_commit"] == analysis.evidence.source_commit
    assert raw["analysis_evidence_digest"] == analysis.evidence.evidence_digest
    assert raw["analysis_context_digest"] == analysis.context_map.context_digest
    assert raw["handoff_evidence_digest"] == handoff.evidence_digest
    assert raw["handoff_context_digest"] == handoff.context_digest
    assert raw["approval_status"] == "HUMAN_APPROVAL_REQUIRED"


def test_authorization_pair_digest_is_deterministic() -> None:
    analysis, handoff = _pair()
    first = authorization_pair_payload(analysis, handoff)
    second = authorization_pair_payload(analysis, handoff)
    assert first == second
    assert first["authorization_digest"] == second["authorization_digest"]


@pytest.mark.parametrize("section", ("analysis", "handoff"))
def test_reduced_or_synthetic_payload_is_rejected(tmp_path: Path, section: str) -> None:
    analysis, handoff = _pair()
    artifact = tmp_path / AUTHORIZATION_PAIR_FILENAME
    persist_authorization_pair(artifact, analysis, handoff)
    raw = json.loads(artifact.read_text(encoding="utf-8"))
    raw[section].pop("evidence" if section == "analysis" else "task_id")
    artifact.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError):
        load_authorization_pair(artifact)


def test_non_ready_or_non_advisory_pair_cannot_be_persisted(tmp_path: Path) -> None:
    analysis, handoff = _pair()
    invalid = replace(analysis, readiness=DevelopmentAnalysisReadinessV1.NEEDS_REVIEW)

    with pytest.raises(ValueError):
        persist_authorization_pair(tmp_path / AUTHORIZATION_PAIR_FILENAME, invalid, handoff)


def test_artifact_contains_only_exact_bounded_public_payloads(tmp_path: Path) -> None:
    analysis, handoff = _pair()
    artifact = tmp_path / AUTHORIZATION_PAIR_FILENAME
    persist_authorization_pair(artifact, analysis, handoff)
    serialized = artifact.read_text(encoding="utf-8")

    assert '"prompt"' not in serialized
    assert '"raw_provider_payload"' not in serialized
    assert '"hidden_reasoning"' not in serialized
    assert '"source_body"' not in serialized
    assert '"secret"' not in serialized


def test_dogfood_persists_pair_only_after_valid_ready_handoff(tmp_path: Path) -> None:
    runner_path = Path("tools/acceptance/run_development_dogfood.py")
    spec = importlib.util.spec_from_file_location(
        "development_dogfood_authorization_pair", runner_path
    )
    assert spec and spec.loader
    runner = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = runner
    spec.loader.exec_module(runner)
    analysis = _analysis()
    result = runner.run_dogfood(
        root=tmp_path,
        model="qwen3-coder:30b",
        case="diff_review",
        run_id="authorization-pair",
        preflight_fn=lambda _paths, _model: runner.Preflight(
            "PASS", (), "a" * 40, {"durable_default_model": "qwen3:4b"}
        ),
        analyze_fn=lambda _paths, _question, _role: analysis,
    )

    artifact = runner.output_path(tmp_path, "qwen3-coder:30b", "authorization-pair")
    assert result["status"] == "COMPLETED"
    assert result["authorization_pair"]["identity"] == AUTHORIZATION_PAIR_IDENTITY
    assert load_authorization_pair(artifact / AUTHORIZATION_PAIR_FILENAME).analysis.payload() == (
        analysis.payload()
    )
