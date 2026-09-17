"""Focused durable-status coverage for the one-shot Validation worker."""

from __future__ import annotations

import json

import pytest

from app.research.capital_flow import short_horizon_direction_v2_validation_execution as execution

BOUNDARY_DIGEST = "b" * 64


@pytest.fixture
def status_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        execution,
        "verify_validation_boundary",
        lambda: {"boundary_semantic_digest": BOUNDARY_DIGEST, "identity": "boundary"},
    )
    from app.research.capital_flow import short_horizon_direction_v2_validation_freeze as freeze

    monkeypatch.setattr(freeze, "candidate_freeze_payload", lambda boundary: {"candidate": "FULL_V2/1h"})


def test_status_runner_persists_terminal_metrics(tmp_path, monkeypatch: pytest.MonkeyPatch, status_environment) -> None:
    def execute(candidate, *, boundary_digest, stage_callback):
        stage_callback("RESEARCH_DATA_LOADED", {"research_training_boundary_digest": "research"})
        stage_callback("MODEL_FIT_COMPLETE")
        metrics = {
            "candidate": candidate,
            "scored_samples": 2,
            "balanced_accuracy": 0.5,
            "model_configuration": [("iterations", "100"), ("l2", "0.0")],
        }
        stage_callback("METRICS_COMPLETE", metrics)
        return metrics

    monkeypatch.setattr(execution, "execute_v2_validation_candidate", execute)
    path = tmp_path / "status.json"
    result = execution.run_v2_validation_candidate_with_status("FULL_V2/1h", boundary_digest=BOUNDARY_DIGEST, path=path)

    assert result["scored_samples"] == 2
    assert json.loads(path.read_text(encoding="utf-8"))["stage"] == "TERMINAL_SUCCESS"
    assert json.loads(path.read_text(encoding="utf-8"))["metrics"]["balanced_accuracy"] == 0.5
    assert json.loads(path.read_text(encoding="utf-8"))["metrics"]["model_configuration"] == [
        ["iterations", "100"],
        ["l2", "0.0"],
    ]


def test_status_runner_persists_failure_traceback(tmp_path, monkeypatch: pytest.MonkeyPatch, status_environment) -> None:
    def execute(candidate, *, boundary_digest, stage_callback):
        stage_callback("RESEARCH_DATA_LOADED", {"research_training_boundary_digest": "research"})
        raise RuntimeError("injected worker failure")

    monkeypatch.setattr(execution, "execute_v2_validation_candidate", execute)
    path = tmp_path / "status.json"
    with pytest.raises(RuntimeError, match="injected worker failure"):
        execution.run_v2_validation_candidate_with_status("FULL_V2/1h", boundary_digest=BOUNDARY_DIGEST, path=path)

    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["stage"] == "TERMINAL_FAILURE"
    assert report["last_completed_stage"] == "RESEARCH_DATA_LOADED"
    assert report["exception_type"] == "RuntimeError"
    assert "injected worker failure" in report["traceback"]


def test_status_runner_preserves_metrics_after_later_failure(
    tmp_path, monkeypatch: pytest.MonkeyPatch, status_environment
) -> None:
    metrics = {
        "candidate": "FULL_V2/1h",
        "scored_samples": 525_541,
        "model_configuration": [("solver", "deterministic_batch_gradient")],
    }

    def execute(candidate, *, boundary_digest, stage_callback):
        stage_callback("RESEARCH_DATA_LOADED", {"research_training_boundary_digest": "research"})
        stage_callback("METRICS_COMPLETE", metrics)
        raise RuntimeError("terminal reporting failure")

    monkeypatch.setattr(execution, "execute_v2_validation_candidate", execute)
    path = tmp_path / "status.json"
    with pytest.raises(RuntimeError, match="terminal reporting failure"):
        execution.run_v2_validation_candidate_with_status("FULL_V2/1h", boundary_digest=BOUNDARY_DIGEST, path=path)

    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["stage"] == "TERMINAL_FAILURE"
    assert report["last_completed_stage"] == "METRICS_COMPLETE"
    assert report["metrics"]["scored_samples"] == 525_541
    assert report["metrics"]["model_configuration"] == [["solver", "deterministic_batch_gradient"]]
