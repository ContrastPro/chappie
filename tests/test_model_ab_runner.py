"""Deterministic contracts for the focused model A/B acceptance runner."""

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace


def _runner():
    path = Path(__file__).parents[1] / "tools" / "acceptance" / "run_model_ab.py"
    sys.path.insert(0, str(path.parent))
    spec = spec_from_file_location("model_ab_runner", path)
    assert spec and spec.loader
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_focused_matrix_is_explicit_and_has_required_sizes():
    runner = _runner()
    assert len(runner._cases()) == 19
    assert runner.MODEL_IDS == ("QWEN3_4B", "QWEN3_14B")
    assert runner.STABILITY_IDS == ("knowledge_file", "selection_control_plane")


def test_pair_gates_context_retrieval_and_effective_model():
    runner = _runner()
    left = {
        "case_id": "x",
        "question": "q",
        "context_kind": "file",
        "locator": "a",
        "context_digest": "d",
        "context_character_count": 1,
        "sources": ["r"],
        "effective_model_id": "QWEN3_4B",
    }
    right = {**left, "effective_model_id": "QWEN3_14B"}
    assert runner._compare(left, right)["valid"] is True
    assert runner._compare(left, {**right, "sources": ["other"]})["retrieval_equal"] is False
    assert (
        runner._compare(left, {**right, "effective_model_id": "QWEN3_4B"})["effective_model_gate"]
        is False
    )


def test_timing_records_timeouts_and_report_is_serializable(tmp_path: Path):
    runner = _runner()
    assert (
        runner._timing(
            [{"elapsed_ms": 2, "status": "OK"}, {"elapsed_ms": 5, "status": "OUTER_TIMEOUT"}]
        )["timeouts"]
        == 1
    )
    payload = {
        "source_commit": "a" * 40,
        "preflight": {"status": "OK"},
        "default_model": "qwen3:4b",
        "summary": {},
        "stability": {},
        "timing_summary": {},
        "timeout_behavior": {},
        "state_isolation": {},
    }
    runner._write(tmp_path, payload)
    assert (tmp_path / "result.json").exists() and "# Chappie focused model A/B" in (
        tmp_path / "report.md"
    ).read_text()


def test_runner_forwards_only_explicit_model_selection() -> None:
    runner = _runner()
    bodies: list[dict[str, object]] = []

    class Client:
        def post(self, _path: str, *, json: dict[str, object], timeout: float):
            bodies.append(json)
            assert timeout == runner.CHAPPIE_TIMEOUT_SECONDS
            return SimpleNamespace(json=lambda: {"data": {"state": "OK", "answer": "Grounded."}})

    case = runner._cases()[0]
    runner._call(Client(), case, "QWEN3_14B", None)
    runner._call(Client(), case, None, None)

    assert bodies[0]["requested_model_id"] == "QWEN3_14B"
    assert "requested_model_id" not in bodies[1]
