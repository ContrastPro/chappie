"""Deterministic grading checks for the local Development Copilot acceptance runner."""

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from app.control_plane import ProjectQAControlPlaneResponseV1, ProjectQAStateV1


def _runner_module():
    path = Path(__file__).parents[1] / "tools" / "acceptance" / "chappie_copilot_acceptance.py"
    spec = spec_from_file_location("chappie_copilot_acceptance", path)
    assert spec and spec.loader
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _response(answer: str, *, validation_status: str = "VALID"):
    return ProjectQAControlPlaneResponseV1(
        ProjectQAStateV1.OK, "question", answer, validation_status=validation_status
    )


def test_runner_rejects_prompt_echo_for_primary_and_session_hard_gate() -> None:
    runner = _runner_module()
    primary = next(case for case in runner.BASE_CASES if case.case_id == "ai_bypass_risk")
    session = next(case for case in runner.SESSION_CASES if case.policy_case_id == "ai_bypass_risk")
    for case in (primary, session):
        result = runner._record(case, _response(case.question), None, 1)
        assert result["result"] == "FAIL"
        assert result["failure_classification"] == "MODEL_PROMPT_ECHO"


def test_runner_requires_substantive_file_review_and_accepts_typed_unknown() -> None:
    runner = _runner_module()
    review = next(case for case in runner.BASE_CASES if case.case_id == "knowledge_file")
    assert runner._record(review, _response(review.question), None, 1)["result"] == "FAIL"
    substantive = (
        "This package owns canonical Knowledge storage; its boundary preserves provenance."
    )
    assert runner._record(review, _response(substantive), None, 1)["result"] == "PASS"
    unknown = next(case for case in runner.BASE_CASES if case.case_id == "unknown_news")
    result = runner._record(
        unknown,
        _response(
            "Current canonical Knowledge does not establish such an implementation.",
            validation_status="UNKNOWN_CURRENT_CAPABILITY",
        ),
        None,
        1,
    )
    assert result["result"] == "PASS"
