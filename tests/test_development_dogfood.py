"""Deterministic tests for the local-only Development Reasoning dogfood harness."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.ai.model_policy import DevelopmentModelRoleV1, resolve_development_model_role
from app.development_analysis import DevelopmentAnalysisReadinessV1, DevelopmentAnalysisStateV1


@pytest.fixture(scope="module")
def harness():
    path = Path("tools/acceptance/run_development_dogfood.py")
    spec = importlib.util.spec_from_file_location("development_dogfood", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _preflight(harness, status="PASS", failures=()):
    return lambda _paths, _model: harness.Preflight(
        status,
        tuple(failures),
        "a" * 40,
        {"knowledge_identity": "knowledge/test", "durable_default_model": "qwen3:4b"},
    )


def _analysis(*, state=DevelopmentAnalysisStateV1.OK, reason=None, effective="qwen3-coder:30b"):
    def event(stage, status, reason_code=None):
        return SimpleNamespace(
            stage=SimpleNamespace(value=stage),
            status=SimpleNamespace(value=status),
            elapsed_ms=12,
            reason_code=reason_code,
        )

    evidence = SimpleNamespace(effective_model=effective, record_ids=("development.analysis",))
    context_map = SimpleNamespace(
        context_digest="context",
        knowledge_identity="chappie_knowledge/1.0.0",
        knowledge_manifest_digest="m" * 64,
        knowledge_context_digest="c" * 64,
        evidence_handles=(),
    )
    return SimpleNamespace(
        state=state,
        reason_code=reason,
        evidence=evidence,
        context_map=context_map,
        lifecycle=SimpleNamespace(
            events=(
                event(
                    "GROUNDING_SYNTHESIS",
                    "COMPLETED" if state == DevelopmentAnalysisStateV1.OK else "FAILED",
                    reason,
                ),
                event(
                    "GROUNDING_PARSE",
                    "COMPLETED" if state == DevelopmentAnalysisStateV1.OK else "FAILED",
                    reason,
                ),
                event(
                    "PROPOSAL_SYNTHESIS",
                    "COMPLETED" if state == DevelopmentAnalysisStateV1.OK else "FAILED",
                    reason,
                ),
                event(
                    "PROPOSAL_PARSE",
                    "COMPLETED" if state == DevelopmentAnalysisStateV1.OK else "FAILED",
                    reason,
                ),
                event(
                    "REVIEW_SYNTHESIS",
                    "COMPLETED" if state == DevelopmentAnalysisStateV1.OK else "FAILED",
                    reason,
                ),
                event(
                    "REVIEW_PARSE",
                    "COMPLETED" if state == DevelopmentAnalysisStateV1.OK else "FAILED",
                    reason,
                ),
                event(
                    "STRUCTURAL_VALIDATION",
                    "COMPLETED" if state == DevelopmentAnalysisStateV1.OK else "FAILED",
                    reason,
                ),
                event(
                    "READINESS_EVALUATION",
                    "COMPLETED" if state == DevelopmentAnalysisStateV1.OK else "FAILED",
                    reason,
                ),
            )
        ),
        readiness=DevelopmentAnalysisReadinessV1.INSUFFICIENT_EVIDENCE,
        architecture_owners=(),
        current_evidence_handles=(),
        affected_contracts=(),
        likely_files=(),
        proposed_scope=None,
        unknowns=(),
        required_tests=(),
        acceptance_criteria=(),
        payload=lambda: {
            "state": state.value,
            "facts": [],
            "inferences": [],
            "unknowns": [],
            "architecture_owners": [],
            "affected_contracts": [],
            "likely_files": [],
            "risks": [],
            "required_tests": [],
            "non_goals": [],
            "acceptance_criteria": [],
            "confidence": None,
            "validation_status": "VALID" if state == DevelopmentAnalysisStateV1.OK else "INVALID",
            "readiness": "INSUFFICIENT_EVIDENCE",
            "reason_code": reason,
            "current_evidence_handles": [],
            "proposed_scope": None,
        },
    )


def test_preflight_failure_writes_inspectable_checkpoint(tmp_path, harness):
    invoked = False

    def analyze(*_args):
        nonlocal invoked
        invoked = True
        return _analysis()

    result = harness.run_dogfood(
        root=tmp_path,
        model="qwen3-coder:30b",
        case="diff_review",
        run_id="preflight",
        preflight_fn=_preflight(harness, "FAIL", ("KNOWLEDGE_STALE",)),
        analyze_fn=analyze,
    )

    persisted = json.loads(
        (harness.output_path(tmp_path, "qwen3-coder:30b", "preflight") / "result.json").read_text()
    )
    assert result["status"] == "PREFLIGHT_FAILED"
    assert persisted["stages"]["PREFLIGHT"]["reason_code"] == "KNOWLEDGE_STALE"
    assert invoked is False


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        (DevelopmentAnalysisStateV1.TIMEOUT, "AI_PROVIDER_TIMEOUT"),
        (DevelopmentAnalysisStateV1.ANALYSIS_INVALID, "DEVELOPMENT_SCHEMA_INVALID"),
        (DevelopmentAnalysisStateV1.ANALYSIS_INVALID, "UNSUPPORTED_KNOWLEDGE_CITATION"),
    ],
)
def test_failed_analysis_preserves_reason_and_stages(tmp_path, harness, state, reason):
    result = harness.run_dogfood(
        root=tmp_path,
        model="qwen3-coder:30b",
        case="diff_review",
        run_id=reason.lower(),
        preflight_fn=_preflight(harness),
        analyze_fn=lambda *_args: _analysis(state=state, reason=reason),
    )

    assert result["status"] == "FAILED"
    assert result["reason_code"] == reason
    assert result["stages"]["HANDOFF"]["status"] == "NOT_REACHED"
    assert (
        harness.output_path(tmp_path, "qwen3-coder:30b", reason.lower()) / "result.json"
    ).is_file()


def test_requested_effective_model_mismatch_fails_closed(tmp_path, harness):
    result = harness.run_dogfood(
        root=tmp_path,
        model="qwen3-coder:30b",
        case="diff_review",
        run_id="mismatch",
        preflight_fn=_preflight(harness),
        analyze_fn=lambda *_args: _analysis(effective="qwen3:14b"),
    )

    assert result["status"] == "FAILED"
    assert result["reason_code"] == "REQUESTED_EFFECTIVE_MODEL_MISMATCH"


def test_success_records_only_public_analysis_and_metrics(tmp_path, harness):
    result = harness.run_dogfood(
        root=tmp_path,
        model="qwen3-coder:30b",
        case="diff_review",
        run_id="success",
        preflight_fn=_preflight(harness),
        analyze_fn=lambda _paths, _question, role: (
            _analysis() if role is DevelopmentModelRoleV1.CODE else pytest.fail("wrong role")
        ),
    )

    assert result["status"] == "COMPLETED"
    assert result["stages"]["CONTEXT_MAP"]["status"] == "PASS"
    assert result["stages"]["EVIDENCE_HANDLES"]["status"] == "PASS"
    assert result["stages"]["GROUNDING_SYNTHESIS"]["status"] == "PASS"
    assert result["provider_metrics"]["total_inference_elapsed_ms"] == 36
    assert "prompt" not in result["analysis"]
    assert (harness.output_path(tmp_path, "qwen3-coder:30b", "success") / "report.md").is_file()


def test_existing_evidence_never_overwritten(tmp_path, harness):
    destination = harness.output_path(tmp_path, "qwen3-coder:30b", "existing")
    destination.mkdir(parents=True)
    (destination / "result.json").write_text('{"preserve": true}\n', encoding="utf-8")

    with pytest.raises(FileExistsError):
        harness.run_dogfood(
            root=tmp_path,
            model="qwen3-coder:30b",
            case="diff_review",
            run_id="existing",
            preflight_fn=_preflight(harness),
            analyze_fn=lambda *_args: _analysis(),
        )
    assert (destination / "result.json").read_text(encoding="utf-8") == '{"preserve": true}\n'


def test_model_selection_is_request_local_and_does_not_mutate_policy(tmp_path, harness):
    before = resolve_development_model_role(DevelopmentModelRoleV1.CODE)[1].concrete_model
    harness.run_dogfood(
        root=tmp_path,
        model="qwen3:14b",
        case="diff_review",
        run_id="deep",
        preflight_fn=_preflight(harness),
        analyze_fn=lambda *_args: _analysis(effective="qwen3:14b"),
    )
    assert before == "qwen3-coder:30b"
    assert resolve_development_model_role(DevelopmentModelRoleV1.CODE)[1].concrete_model == before
