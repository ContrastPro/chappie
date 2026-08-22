"""Deterministic tests for the local qwen3:14b diagnostic runner."""

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def _runner():
    path = Path(__file__).parents[1] / "tools" / "acceptance" / "run_qwen3_14b_diagnostic.py"
    spec = spec_from_file_location("qwen3_14b_diagnostic", path)
    assert spec and spec.loader
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _result(raw_status="OK", chappie_status="OK", after="QWEN3_4B", raw_ms=1):
    return {
        "tests": {
            "raw_14b": {"status": raw_status, "elapsed_ms": raw_ms},
            "chappie_14b": {"status": chappie_status},
            "default_after_14b": {"effective_model_id": after},
        }
    }


def test_diagnostic_classifies_timeout_and_raw_failure():
    runner = _runner()
    assert runner._classify(_result(chappie_status="TIMEOUT")) == ("CHAPPIE_14B_TIMEOUT", None)
    assert runner._classify(_result(raw_status="PROVIDER_ERROR")) == ("RAW_14B_FAILURE", None)
    assert runner._classify(_result(chappie_status="TIMEOUT", raw_ms=45_001)) == (
        "CHAPPIE_14B_TIMEOUT",
        "PROVEN_TIMEOUT_MISMATCH",
    )


def test_diagnostic_detects_default_state_leak_and_writes_artifacts(tmp_path: Path):
    runner = _runner()
    result = _result(after="QWEN3_14B")
    result.update(
        {
            "source_commit": "a" * 40,
            "preflight": {"status": "OK"},
            "default_model": "qwen3:4b",
            "timing_ms": {},
        }
    )
    assert runner._classify(result) == ("MULTI_MODEL_SELECTION_FAILURE", "MULTI_MODEL_STATE_LEAK")
    runner._write(tmp_path, result)
    assert (tmp_path / "result.json").exists()
    assert "# Chappie qwen3:14b diagnostic" in (tmp_path / "report.md").read_text()


def test_preflight_failure_has_a_durable_report(tmp_path: Path):
    runner = _runner()
    result = {
        "source_commit": "a" * 40,
        "preflight": {
            "status": "FAILED",
            "checks": [{"name": "qwen3:14b installed", "status": "FAIL"}],
            "failures": [
                {
                    "code": "MODEL_MISSING",
                    "message": "required local model is absent",
                    "path": "qwen3:14b",
                }
            ],
        },
        "default_model": None,
        "tests": {},
        "timing_ms": {},
        "diagnosis": "DIAGNOSTIC_ENVIRONMENT_FAILURE",
    }
    runner._write(tmp_path, result)
    report = (tmp_path / "report.md").read_text()
    assert "Preflight: FAILED" in report
    assert "MODEL_MISSING: required local model is absent (qwen3:14b)" in report


def test_diagnostic_tooling_is_allowed_but_production_dirty_path_fails_closed():
    runner = _runner()
    allowed, failures = runner._tree_check(set(runner.ALLOWED_DIRTY_PATHS))
    assert allowed["status"] == "PASS"
    assert failures == []
    rejected, failures = runner._tree_check({"app/control_plane/service.py"})
    assert rejected["status"] == "FAIL"
    assert failures == [
        {
            "code": "UNEXPECTED_DIRTY_PATH",
            "message": "production provenance may be stale",
            "path": "app/control_plane/service.py",
        }
    ]
