"""Contracts for the acceptance-only qwen2.5-coder CODE benchmark."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace


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
    assert runner.EVIDENCE_SCHEMA == "chappie_code_model_benchmark/1.0.0"
    assert runner.MODEL == "qwen2.5-coder:14b"
    assert runner.OUTPUT_RELATIVE == Path("data/acceptance/code_model/qwen2_5_coder_14b")
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
    assert runner._stability(sample)["diff_review"]["runs"] == 2
    runner._write(tmp_path, {"source_commit": "a" * 40, "summary": {"winner": "NOT_DECLARED"}})
    assert "Local non-canonical" in (tmp_path / "report.md").read_text(encoding="utf-8")
