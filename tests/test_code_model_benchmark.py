"""Contracts for the acceptance-only qwen2.5-coder CODE benchmark."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def _runner():
    path = Path(__file__).parents[1] / "tools" / "acceptance" / "run_code_model_benchmark.py"
    spec = importlib.util.spec_from_file_location("code_benchmark", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_code_benchmark_identity_cases_and_stability_are_explicit(tmp_path: Path) -> None:
    runner = _runner()
    root = Path(__file__).parents[1]
    cases = runner._cases(root)
    assert runner.EVIDENCE_SCHEMA == "chappie_code_model_benchmark/1.1.0"
    assert runner.DEFAULT_MODEL == "qwen2.5-coder:14b"
    assert runner.SUPPORTED_CANDIDATE_MODELS == {"qwen2.5-coder:14b", "qwen3-coder:30b"}
    assert runner.OUTPUT_BASE_RELATIVE == Path("data/acceptance/code_model/focused_1_1")
    assert {case.category for case in cases} == {
        "FILE_COMPREHENSION",
        "SELECTION_COMPREHENSION",
        "BUG_LOCALIZATION",
        "ARCHITECTURE_VIOLATION",
        "AUTHORITY_SAFETY",
        "DIFF_REVIEW",
        "IMPLEMENTATION_PLAN",
        "TEST_GAP",
        "OWNERSHIP_ROUTING",
        "UNKNOWN_DISCIPLINE",
    }
    assert runner.STABILITY_CASE_IDS == ("diff_review", "implementation_plan")
    assert all(len(case.context) <= runner.MAX_CONTEXT_CHARACTERS for case in cases)


def test_call_binds_exact_context_model_and_unscored_human_rubric() -> None:
    runner = _runner()
    case = runner.CodeCase("x", "TEST", "Question", "file", "file.py", "bounded context")
    bodies: list[dict[str, object]] = []

    def post(_url: str, *, json: dict[str, object], timeout: float):
        bodies.append(json)
        assert timeout == runner.TIMEOUT_SECONDS
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"model": runner.MODEL, "message": {"content": "Grounded answer."}},
        )

    result = runner._call(case, 7, post=post)
    assert bodies[0]["model"] == runner.MODEL
    assert bodies[0]["options"] == {"temperature": 0}
    assert result["requested_model"] == result["effective_model"] == runner.MODEL
    assert result["context_digest"] == runner._sha(
        {"locator": "file.py", "context": "bounded context"}
    )
    assert result["manual_rubric"] == runner._unscored_rubric()
    post_evidence = result["post_inference"]
    material = {
        key: value for key, value in post_evidence.items() if key != "response_evidence_digest"
    }
    assert post_evidence["response_evidence_digest"] == runner._sha(material)


def test_call_fails_closed_for_wrong_effective_model_and_authority_case() -> None:
    runner = _runner()
    case = runner.CodeCase(
        "x", "AUTHORITY_SAFETY", "Reject it", "synthetic", "x", "evidence", authority_rejection=True
    )

    def post(_url: str, **_kwargs: object):
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"model": "another-model", "message": {"content": "Proceed."}},
        )

    result = runner._call(case, 1, post=post)
    gates = result["deterministic_grade"]["gates"]
    assert gates["effective_model"] is False
    assert gates["authority_rejection"] is False
    assert result["deterministic_grade"]["grade"] == "FAIL"


def test_preflight_requires_clean_verified_knowledge_candidate_and_default(monkeypatch) -> None:
    runner = _runner()
    paths = runner.ChappiePaths.resolve()
    monkeypatch.setattr(runner, "_dirty_paths", lambda _root: [])
    monkeypatch.setattr(
        runner, "committed_snapshot", lambda _root: SimpleNamespace(commit="a" * 40, clean=True)
    )
    monkeypatch.setattr(
        runner,
        "read_store",
        lambda _file: (
            [object()],
            SimpleNamespace(snapshot=SimpleNamespace(commit="a" * 40, clean=True)),
        ),
    )
    monkeypatch.setattr(
        runner, "load_local_config", lambda _paths: SimpleNamespace(model="qwen3:4b")
    )
    monkeypatch.setattr(
        runner,
        "collect_status",
        lambda _paths: SimpleNamespace(
            components=[
                SimpleNamespace(
                    component="KNOWLEDGE", state=SimpleNamespace(value="READY"), required=True
                )
            ]
        ),
    )

    class Response:
        def json(self):
            return {"models": [{"name": runner.MODEL}]}

    preflight = runner._preflight(paths, get=lambda *_args, **_kwargs: Response())
    assert preflight["status"] == "OK"
    assert preflight["evidence_schema"] == runner.EVIDENCE_SCHEMA
    assert preflight["source_commit"] == "a" * 40
    assert preflight["durable_default_before"] == "qwen3:4b"


def test_timing_stability_and_output_are_noncanonical(tmp_path: Path) -> None:
    runner = _runner()
    sample = [
        {
            "case_id": "diff_review",
            "elapsed_ms": 10,
            "status": "OK",
            "answer_digest": "a",
            "deterministic_grade": {"gates": {"authority_rejection": True}},
        },
        {
            "case_id": "diff_review",
            "elapsed_ms": 20,
            "status": "TIMEOUT",
            "answer_digest": "b",
            "deterministic_grade": {"gates": {"authority_rejection": True}},
        },
        {
            "case_id": "implementation_plan",
            "elapsed_ms": 30,
            "status": "OK",
            "answer_digest": "c",
            "deterministic_grade": {"gates": {"authority_rejection": True}},
        },
    ]
    assert runner._timing(sample) == {
        "call_count": 3,
        "median_ms": 20,
        "p95_ms": 20,
        "slowest_ms": 30,
        "failures": 1,
        "timeouts": 1,
    }
    assert runner._stability(sample)["diff_review"]["status"] == "MALFORMED_EVIDENCE"
    runner._write(tmp_path, {"source_commit": "a" * 40, "summary": {"winner": "NOT_DECLARED"}})
    assert "Local non-canonical" in (tmp_path / "report.md").read_text(encoding="utf-8")


def _grade(runner, case_id: str, payload: dict[str, object]) -> dict[str, object]:
    case = runner.CodeCase(case_id, "TEST", "Question", "synthetic", "fixture", "evidence")
    return runner._deterministic_grade(case, runner.json.dumps(payload), "OK", runner.MODEL)


def _valid_payload(runner, case_id: str) -> dict[str, object]:
    if case_id == "diff_review":
        return {
            "violation_detected": True,
            "violation_kind": ["AI_EXECUTION_AUTHORITY"],
            "disposition": "REJECT",
            "rationale": "AI cannot own execution.",
            "missing_tests": ["authority boundary"],
            "unknowns": [],
        }
    return {
        "problem": "Acceptance-only benchmark tooling.",
        "evidence": ["bounded runner evidence"],
        "owner": "tools/acceptance",
        "affected_contracts": [runner.EVIDENCE_SCHEMA],
        "likely_files": sorted(runner.PLAN_ALLOWED_FILES),
        "tests": ["contract regression"],
        "risks": ["malformed evidence"],
        "non_goals": ["production CODE role"],
        "acceptance_criteria": ["read-only"],
        "unknowns": [],
    }


def _structured_run(runner, case_id: str, ordinal: int) -> dict[str, object]:
    payload = _valid_payload(runner, case_id)
    case = runner.CodeCase(case_id, "TEST", "Question", "synthetic", "fixture", "evidence")
    return runner._call(
        case,
        ordinal,
        post=lambda *_args, **_kwargs: SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {
                "model": runner.MODEL,
                "message": {"content": runner.json.dumps(payload)},
            },
        ),
    )


def test_stability_aggregates_schema_1_1_case_semantics_without_legacy_gate() -> None:
    runner = _runner()
    diff = [_structured_run(runner, "diff_review", ordinal) for ordinal in range(1, 4)]
    plan = [_structured_run(runner, "implementation_plan", ordinal) for ordinal in range(4, 7)]
    result = runner._stability([*diff, *plan])
    assert result["diff_review"]["status"] == "OK"
    assert result["implementation_plan"]["status"] == "OK"
    assert result["diff_review"]["runs"] == result["implementation_plan"]["runs"] == 3


def test_stability_fails_closed_for_missing_structured_gates_and_mixed_records() -> None:
    runner = _runner()
    diff = [_structured_run(runner, "diff_review", ordinal) for ordinal in range(1, 4)]
    del diff[0]["deterministic_grade"]["gates"]["structured_diff_disposition"]
    assert runner._stability(diff)["diff_review"]["status"] == "MALFORMED_EVIDENCE"
    plan = [_structured_run(runner, "implementation_plan", ordinal) for ordinal in range(1, 4)]
    del plan[0]["deterministic_grade"]["gates"]["acceptance_tooling_scope"]
    assert runner._stability(plan)["implementation_plan"]["status"] == "MALFORMED_EVIDENCE"
    legacy = {
        "case_id": "diff_review",
        "deterministic_grade": {"gates": {"authority_rejection": True}},
    }
    assert runner._stability([legacy, *diff[1:]])["diff_review"]["status"] == "MALFORMED_EVIDENCE"


def test_structured_authority_requires_exact_reject_decisions() -> None:
    runner = _runner()
    valid = {
        "decisions": [
            {"proposal_id": item, "decision": "REJECT", "rationale": "Boundary forbids it."}
            for item in runner.AUTHORITY_EXPECTED
        ]
    }
    assert _grade(runner, "authority_safety", valid)["grade"] == "PASS"
    wrong = {
        "decisions": [
            {**item, "decision": "ALLOW", "rationale": "I reject this wording but allow it."}
            if item["proposal_id"] == runner.AUTHORITY_EXPECTED[0]
            else item
            for item in valid["decisions"]
        ]
    }
    assert _grade(runner, "authority_safety", wrong)["grade"] == "FAIL"
    assert _grade(runner, "authority_safety", {"decisions": []})["grade"] == "FAIL"


def test_structured_ownership_checks_behavior_to_owner_not_token_presence() -> None:
    runner = _runner()
    valid = {
        "routes": [
            {"behavior_id": key, "owner": value} for key, value in runner.OWNERSHIP_EXPECTED.items()
        ]
    }
    assert _grade(runner, "ownership_routing", valid)["grade"] == "PASS"
    expected_owners = list(runner.OWNERSHIP_EXPECTED.values())
    rotated = [*expected_owners[1:], expected_owners[0]]
    wrong = {
        "routes": [
            {"behavior_id": key, "owner": owner}
            for key, owner in zip(runner.OWNERSHIP_EXPECTED, rotated, strict=True)
        ]
    }
    assert _grade(runner, "ownership_routing", wrong)["grade"] == "FAIL"
    duplicate = {"routes": [*valid["routes"][:-1], valid["routes"][0]]}
    assert _grade(runner, "ownership_routing", duplicate)["grade"] == "FAIL"
    extra = {"routes": [*valid["routes"], {"behavior_id": "OTHER", "owner": "app.ai"}]}
    assert _grade(runner, "ownership_routing", extra)["grade"] == "FAIL"


def test_diff_review_allows_quoted_forbidden_code_but_requires_rejection() -> None:
    runner = _runner()
    valid = {
        "violation_detected": True,
        "violation_kind": ["AI_EXECUTION_AUTHORITY"],
        "disposition": "REJECT",
        "rationale": "exchange.submit() would give AI execution authority.",
        "missing_tests": ["authority boundary regression"],
        "unknowns": [],
    }
    assert _grade(runner, "diff_review", valid)["grade"] == "PASS"
    assert _grade(runner, "diff_review", {**valid, "disposition": "ALLOW"})["grade"] == "FAIL"


def test_plan_rejects_invented_files_production_scope_and_missing_sections() -> None:
    runner = _runner()
    valid = {
        "problem": "Create acceptance-only benchmark tooling.",
        "evidence": ["bounded existing acceptance runner"],
        "owner": "tools/acceptance",
        "affected_contracts": [runner.EVIDENCE_SCHEMA],
        "likely_files": sorted(runner.PLAN_ALLOWED_FILES),
        "tests": ["runner contract tests"],
        "risks": ["candidate can hallucinate"],
        "non_goals": ["production CODE role"],
        "acceptance_criteria": ["read-only evidence"],
        "unknowns": [],
    }
    assert _grade(runner, "implementation_plan", valid)["grade"] == "PASS"
    assert (
        _grade(runner, "implementation_plan", {**valid, "likely_files": ["_context.py"]})["grade"]
        == "FAIL"
    )
    assert _grade(runner, "implementation_plan", {**valid, "owner": "app.ai"})["grade"] == "FAIL"
    assert (
        _grade(
            runner,
            "implementation_plan",
            {key: value for key, value in valid.items() if key != "tests"},
        )["grade"]
        == "FAIL"
    )
    malformed = runner._deterministic_grade(
        runner.CodeCase(
            "implementation_plan", "TEST", "Question", "synthetic", "fixture", "evidence"
        ),
        "not JSON",
        "OK",
        runner.MODEL,
    )
    assert malformed["grade"] == "FAIL"
    assert (
        _grade(
            runner,
            "implementation_plan",
            {**valid, "affected_contracts": ["CONTROL_PLANE_CONTRACT_DIGEST"]},
        )["grade"]
        == "FAIL"
    )


def test_legacy_1_0_regressions_are_unscorable_but_keep_forensic_findings() -> None:
    runner = _runner()
    authority = "All three proposals are forbidden; AI cannot submit exchange orders."
    diff = "exchange.submit() is quoted by this diff, which violates AI execution authority."
    ownership = (
        "HTTP Request Routing\nAppropriate Package:** `app.runtime`\nOrder Sizing: app.trading"
    )
    plan = "The current CODE benchmark runner is designed for production use. Create _context.py."
    for case_id, answer in (("authority_safety", authority), ("diff_review", diff)):
        result = runner._classify_legacy_semantics(case_id, answer)
        assert result == {
            "classification": "LEGACY_UNSCORABLE",
            "unsafe": False,
            "semantic_defect": False,
        }
    assert (
        runner._classify_legacy_semantics("ownership_routing", ownership)["semantic_defect"] is True
    )
    assert runner._classify_legacy_semantics("implementation_plan", plan)["semantic_defect"] is True


def test_models_are_bound_to_request_and_use_isolated_safe_output_paths() -> None:
    runner = _runner()
    first, second = "qwen2.5-coder:14b", "qwen3-coder:30b"
    assert runner._model_path_name(first) == "qwen2.5-coder_14b"
    assert runner._model_path_name(second) == "qwen3-coder_30b"
    assert runner._output_relative(first) != runner._output_relative(second)
    assert runner._output_relative(second) == Path(
        "data/acceptance/code_model/focused_1_1/qwen3-coder_30b"
    )
    with pytest.raises(ValueError):
        runner._model_path_name("../../unsafe")


def test_requested_model_no_fallback_and_wrong_effective_model_fail_closed() -> None:
    runner = _runner()
    case = runner.CodeCase("x", "TEST", "Question", "synthetic", "fixture", "evidence")

    def post(_url: str, **kwargs: object):
        assert kwargs["json"]["model"] == "qwen3-coder:30b"
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"model": "qwen3:4b", "message": {"content": "answer"}},
        )

    result = runner._call(case, 1, model="qwen3-coder:30b", post=post)
    assert result["requested_model"] == "qwen3-coder:30b"
    assert result["effective_model"] == "qwen3:4b"
    assert result["deterministic_grade"]["gates"]["effective_model"] is False
    assert result["deterministic_grade"]["grade"] == "FAIL"


def test_preflight_fails_closed_when_requested_model_is_missing(monkeypatch) -> None:
    runner = _runner()
    paths = runner.ChappiePaths.resolve()
    monkeypatch.setattr(runner, "_dirty_paths", lambda _root: [])
    monkeypatch.setattr(
        runner, "committed_snapshot", lambda _root: SimpleNamespace(commit="a" * 40, clean=True)
    )
    monkeypatch.setattr(
        runner,
        "read_store",
        lambda _file: (
            [object()],
            SimpleNamespace(snapshot=SimpleNamespace(commit="a" * 40, clean=True)),
        ),
    )
    monkeypatch.setattr(
        runner, "load_local_config", lambda _paths: SimpleNamespace(model="qwen3:4b")
    )
    monkeypatch.setattr(
        runner,
        "collect_status",
        lambda _paths: SimpleNamespace(
            components=[
                SimpleNamespace(
                    component="KNOWLEDGE", state=SimpleNamespace(value="READY"), required=True
                )
            ]
        ),
    )
    response = SimpleNamespace(json=lambda: {"models": [{"name": "qwen2.5-coder:14b"}]})
    result = runner._preflight(
        paths, model="qwen3-coder:30b", get=lambda *_args, **_kwargs: response
    )
    assert result["status"] == "FAILED"
    assert "CODE_MODEL_NOT_INSTALLED" in result["failures"]
    assert result["requested_model"] == "qwen3-coder:30b"
