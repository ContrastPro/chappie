from types import SimpleNamespace

import pytest

from app.ai.model_policy import DevelopmentModelRoleV1
from app.development_analysis.intent import resolve_development_task_intent
from app.development_analysis.models import (
    DevelopmentAnalysisRequestV1,
    DevelopmentTaskIntentKindV1,
    DevelopmentTaskIntentSourceV1,
)
from app.development_analysis.service import DevelopmentAnalysisServiceV1
from app.development_analysis.synthesis import DevelopmentIntentSynthesisOutputV1
from app.knowledge import RepositorySnapshotV1, initial_records, manifest
from app.platform.bootstrap import ChappiePaths


def _binding():
    return SimpleNamespace(evidence_digest="e" * 64)


@pytest.mark.parametrize(
    ("text", "expected", "rule"),
    (
        (
            "Explain the current Development architecture.",
            DevelopmentTaskIntentKindV1.QUESTION,
            "CLEAR_QUESTION_V1",
        ),
        (
            "Analyze the current Development architecture and identify its main risks.",
            DevelopmentTaskIntentKindV1.ANALYSIS,
            "CLEAR_ANALYSIS_V1",
        ),
        (
            "Split repository cleanup into a small set of bounded tasks.",
            DevelopmentTaskIntentKindV1.PLAN,
            "CLEAR_PLAN_V1",
        ),
        (
            "Implement the approved bounded change.",
            DevelopmentTaskIntentKindV1.UNKNOWN,
            "AMBIGUOUS_V1",
        ),
    ),
)
def test_missing_intent_uses_conservative_bounded_rules(text, expected, rule) -> None:
    resolved = resolve_development_task_intent(
        DevelopmentAnalysisRequestV1(text, DevelopmentModelRoleV1.CODE),
        _binding(),
        "c" * 64,
    )
    assert resolved.intent == expected
    assert resolved.source == DevelopmentTaskIntentSourceV1.DETERMINISTIC_RULE
    assert resolved.rule_id == rule


@pytest.mark.parametrize("intent", tuple(DevelopmentTaskIntentKindV1))
def test_explicit_intent_is_authoritative(intent) -> None:
    resolved = resolve_development_task_intent(
        DevelopmentAnalysisRequestV1(
            "ambiguous", DevelopmentModelRoleV1.CODE, requested_intent=intent
        ),
        _binding(),
        "c" * 64,
    )
    assert resolved.intent == intent
    assert resolved.source == DevelopmentTaskIntentSourceV1.EXPLICIT


def _service(tmp_path, monkeypatch, runner):
    from app.development_analysis import service

    snapshot = RepositorySnapshotV1("a" * 40, True)
    records = initial_records(snapshot=snapshot)
    monkeypatch.setattr(service, "committed_snapshot", lambda _root: snapshot)
    monkeypatch.setattr(service, "read_store", lambda _path: (records, manifest(records, snapshot)))
    root = tmp_path / "project"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    return DevelopmentAnalysisServiceV1(
        paths=ChappiePaths.resolve(root), runner=runner, role_available=lambda _role: True
    )


@pytest.mark.parametrize(
    ("intent", "text"),
    (
        (DevelopmentTaskIntentKindV1.QUESTION, "Explain the current Development architecture."),
        (
            DevelopmentTaskIntentKindV1.ANALYSIS,
            "Analyze the current Development architecture and identify its main risks.",
        ),
        (
            DevelopmentTaskIntentKindV1.PLAN,
            "Split repository cleanup into a small set of bounded tasks.",
        ),
    ),
)
def test_non_implementation_routes_call_runner_once_and_bypass_implementation_gates(
    tmp_path, monkeypatch, intent, text
) -> None:
    from app.development_analysis import service

    def forbidden_gate(*_args, **_kwargs):
        raise AssertionError("implementation gate was reached")

    monkeypatch.setattr(service, "validate_development_analysis", forbidden_gate)
    monkeypatch.setattr(service, "development_readiness", forbidden_gate)
    calls = []

    def runner(request, _context, _evidence):
        calls.append(request.requested_intent)
        return DevelopmentIntentSynthesisOutputV1(
            intent,
            ("E1",),
            "Bounded grounded response.",
            ("Unresolved detail remains unknown.",),
            inferences=("Bounded inference.",)
            if intent == DevelopmentTaskIntentKindV1.ANALYSIS
            else (),
            risks=("Bounded risk.",) if intent != DevelopmentTaskIntentKindV1.QUESTION else (),
            plan_items=("Verify the bounded proposal.",)
            if intent == DevelopmentTaskIntentKindV1.PLAN
            else (),
            non_goals=("No mutation.",) if intent == DevelopmentTaskIntentKindV1.PLAN else (),
        )

    result = _service(tmp_path, monkeypatch, runner).analyze(
        DevelopmentAnalysisRequestV1(text, DevelopmentModelRoleV1.CODE, requested_intent=intent)
    )
    assert calls == [intent]
    assert result.state.value == "OK"
    assert result.task_intent is not None and result.task_intent.intent == intent
    assert result.answer == "Bounded grounded response."
    assert result.proposed_scope is None
    assert result.readiness is None
    assert result.staged_results is None


def test_unknown_returns_clarification_without_runner(tmp_path, monkeypatch) -> None:
    calls = []
    result = _service(tmp_path, monkeypatch, lambda *_args: calls.append(True)).analyze(
        DevelopmentAnalysisRequestV1("Help with this", DevelopmentModelRoleV1.CODE)
    )
    assert calls == []
    assert result.task_intent is not None
    assert result.task_intent.intent == DevelopmentTaskIntentKindV1.UNKNOWN
    assert "Choose the task intent" in result.answer
    assert result.proposed_scope is None
    assert result.readiness is None
