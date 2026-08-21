from __future__ import annotations

import json
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

import pytest

from app.ai.errors import AIProviderError
from app.ai.evaluation.benchmark import (
    BENCHMARK_IDENTITY,
    BenchmarkCaseV1,
    BenchmarkResultClass,
    BenchmarkTrack,
    HistoricalBenchmarkCaseV1,
    ScenarioForecastV1,
    TemporalSplit,
    benchmark_digest,
    classify_result,
    completed_runs,
    controlled_cases,
    execute_case,
    execute_case_isolated,
    failure_attempts,
    find_resumable_run,
    market_state_baseline,
    sample_market_only_cases,
    score_forecast,
    score_reasoning,
    score_research_reasoning,
    select_case_invocation,
    temporal_split,
    uniform_scenario_baseline,
    write_aggregate_report,
    write_case_manifest,
)
from app.ai.providers import MockMode, MockProvider
from app.ai.research.reasoning import parse_research_reasoning_output
from app.ai.router import ModelRouter
from app.ai.runtime import AIConfig, ChappieAI
from app.ai.schemas import FactV1, parse_reasoning_output
from app.ai.tasks import AITask, ModelProfile
from app.ai.trace import AITraceEventType, AITraceRenderer, TraceVerbosity, trace_event
from app.research.data.store import IntelligenceRow, ResearchStore
from app.tools.chappie_ai_benchmark import _parser

NOW = datetime(2024, 1, 15, 12, tzinfo=UTC)


def _ai(output: object | None = None) -> ChappieAI:
    provider = MockProvider(output=output)
    return ChappieAI(
        ModelRouter({ModelProfile.LOCAL_DEFAULT: provider}), AIConfig(model="mock-model")
    )


def test_benchmark_identity_and_digest_are_deterministic_and_model_independent() -> None:
    assert BENCHMARK_IDENTITY == "chappie_ai_market_benchmark/1.0.0"
    assert benchmark_digest() == benchmark_digest()
    assert "qwen" not in benchmark_digest()
    assert len(controlled_cases()) == 15


def test_full_reasoning_safety_invocation_selects_the_frozen_fifteen_case_inventory() -> None:
    universe = controlled_cases()
    assert len(universe) == 15
    assert select_case_invocation(universe) == universe
    assert len(select_case_invocation(universe, limit=10)) == 10
    assert select_case_invocation(universe, case_id=universe[-1].case_id) == (universe[-1],)


def test_cli_full_reasoning_safety_defaults_to_unbounded_selection() -> None:
    parsed = _parser().parse_args(
        ["--provider", "ollama", "--model", "qwen3:8b", "--track", "reasoning-safety"]
    )
    assert parsed.limit is None


def test_cli_requires_explicit_v11_reasoning_contract() -> None:
    default = _parser().parse_args(
        ["--provider", "ollama", "--model", "qwen3:8b", "--track", "reasoning-safety"]
    )
    selected = _parser().parse_args(
        [
            "--provider",
            "ollama",
            "--model",
            "qwen3:8b",
            "--track",
            "reasoning-safety",
            "--reasoning-contract",
            "1.1.0",
        ]
    )
    assert default.reasoning_contract == "1.0.0"
    assert selected.reasoning_contract == "1.1.0"


def test_cli_accepts_distinct_v111_reasoning_contract() -> None:
    selected = _parser().parse_args(
        [
            "--provider",
            "ollama",
            "--model",
            "qwen3:8b",
            "--track",
            "reasoning-safety",
            "--reasoning-contract",
            "1.1.1",
        ]
    )
    assert selected.reasoning_contract == "1.1.1"


def test_cli_timeout_is_explicit_and_defaults_to_thirty_seconds() -> None:
    default = _parser().parse_args(
        ["--provider", "ollama", "--model", "qwen3:8b", "--track", "reasoning-safety"]
    )
    selected = _parser().parse_args(
        [
            "--provider",
            "ollama",
            "--model",
            "qwen3:8b",
            "--track",
            "reasoning-safety",
            "--timeout-seconds",
            "120",
        ]
    )
    assert default.timeout_seconds == 30.0
    assert selected.timeout_seconds == 120.0


def test_v11_benchmark_run_uses_distinct_task_and_prompt_identity(tmp_path: Path) -> None:
    case = controlled_cases()[0]
    fact = case.facts[0]
    payload = {
        "observed_facts": [{"fact_id": fact.fact_id}],
        "interpretations": [],
        "hypotheses": [
            {
                "condition": [{"fact_id": fact.fact_id, "operator": "IS", "value": fact.value}],
                "target_asset": "BTCUSDT",
                "target_metric": "FORWARD_RETURN_BPS",
                "target_horizon": "6h",
                "expected_relationship": "GREATER_THAN_MATCHED_CONTROL",
                "comparison_basis": "MATCHED_CONTROL",
                "supporting_fact_ids": [fact.fact_id],
                "unknown_dependencies": ["external catalysts"],
                "falsification_condition": "Matched control is not exceeded at 6h.",
                "status": "UNVALIDATED",
            }
        ],
        "contradictions": [],
        "unknowns": ["Causal driver is unknown."],
        "evidence_requests": [],
        "confidence": 0.4,
    }
    run, resumed = execute_case(
        _ai(payload), case, root=tmp_path, task=AITask.RESEARCH_REASONING_V11
    )
    assert not resumed
    assert run.task == AITask.RESEARCH_REASONING_V11.value
    assert run.prompt_version == "research_reasoning/1.1.0"
    assert run.score == score_research_reasoning(
        case, parse_research_reasoning_output(payload, case.facts)
    )
    assert run.score.hypothesis_testability == 1.0
    assert completed_runs(tmp_path, task=AITask.RESEARCH_REASONING_V11) == (run,)


def test_controlled_case_identity_changes_if_definition_changes() -> None:
    case = controlled_cases()[0]
    changed = BenchmarkCaseV1(
        case.case_id,
        case.track,
        case.information_cutoff,
        case.facts,
        case.known_unknowns + ("different unknown",),
        case.allowed_interpretations,
        case.forbidden_factual_claims,
        case.expected_ambiguity,
    )
    assert case.case_digest != changed.case_digest


def test_manual_qwen_failure_case_detects_unsupported_cause_and_false_contradiction() -> None:
    case = next(
        case for case in controlled_cases() if case.case_id == "price_down_oi_up_funding_positive"
    )
    ids = [fact.fact_id for fact in case.facts]
    response = parse_reasoning_output(
        {
            "observed_facts": [{"fact_id": item} for item in ids],
            "interpretations": [
                {
                    "statement": "Technical correction is observed fact.",
                    "supporting_fact_ids": [ids[0]],
                }
            ],
            "hypotheses": [],
            "contradictions": [
                {
                    "statement": "Positive funding and rising OI are contradictory.",
                    "supporting_fact_ids": [ids[1], ids[2]],
                }
            ],
            "unknowns": [],
            "evidence_requests": [],
            "confidence": 0.3,
        },
        set(ids),
    )
    score = score_reasoning(case, response)
    assert score.unsupported_cause_rate == 1
    assert score.false_contradiction_rate == 1
    assert classify_result(score) == BenchmarkResultClass.UNSAFE


def test_explicit_unvalidated_possibility_is_safe_and_ambiguity_is_admitted() -> None:
    case = controlled_cases()[0]
    ids = [fact.fact_id for fact in case.facts]
    response = parse_reasoning_output(
        {
            "observed_facts": [{"fact_id": item} for item in ids],
            "interpretations": [],
            "hypotheses": [
                {
                    "claim": "BTCUSDT may weaken over 1h if leverage remains elevated.",
                    "supporting_fact_ids": [ids[0], ids[1]],
                    "status": "UNVALIDATED",
                }
            ],
            "contradictions": [],
            "unknowns": ["The cause of the move is unknown."],
            "evidence_requests": [],
            "confidence": 0.2,
        },
        set(ids),
    )
    score = score_reasoning(case, response)
    assert score.unsupported_cause_rate == 0
    assert score.unknown_admission == 1
    assert score.hypothesis_testability == 1


def test_historical_visible_context_excludes_hidden_outcomes() -> None:
    case = controlled_cases()[1]
    historical = HistoricalBenchmarkCaseV1(
        case,
        TemporalSplit.VALIDATION,
        "market-digest",
        "outcome-digest",
        {"forward_return_bps:1h": 100.0},
    )
    visible = json.dumps(historical.visible_payload())
    assert "forward_return" not in visible
    assert "outcome-digest" not in visible
    assert historical.hidden_outcomes["forward_return_bps:1h"] == 100.0


@pytest.mark.parametrize(
    ("year", "expected"),
    (
        (2023, TemporalSplit.DEVELOPMENT),
        (2024, TemporalSplit.VALIDATION),
        (2025, TemporalSplit.HELD_OUT),
        (2026, TemporalSplit.HELD_OUT),
    ),
)
def test_temporal_splits_are_fixed(year: int, expected: TemporalSplit) -> None:
    assert temporal_split(datetime(year, 1, 1, tzinfo=UTC)) == expected


def test_scenario_probabilities_and_model_independent_forecast_scores() -> None:
    forecast = ScenarioForecastV1("BTCUSDT", "1h", 0.6, 0.2, 0.2, 0.5, ())
    assert score_forecast(forecast, 10.0)["directional_accuracy"] == 1
    assert uniform_scenario_baseline("BTCUSDT", "1h").upside_probability == pytest.approx(1 / 3)
    with pytest.raises(ValueError, match="sum"):
        ScenarioForecastV1("BTCUSDT", "1h", 0.5, 0.5, 0.5, 0.5, ())


def test_market_state_baseline_is_causal_only() -> None:
    case = controlled_cases()[1]
    historical = HistoricalBenchmarkCaseV1(
        case, TemporalSplit.VALIDATION, "digest", "outcome", {"x": -9.0}
    )
    baseline = market_state_baseline(historical, "1h")
    assert baseline.upside_probability > baseline.downside_probability


def test_mock_end_to_end_immutable_run_and_resume(tmp_path: Path) -> None:
    case = controlled_cases()[0]
    first, resumed = execute_case(_ai(), case, root=tmp_path, now=NOW)
    assert not resumed
    files = list(tmp_path.rglob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text())
    assert payload["benchmark_digest"] == benchmark_digest()
    assert "thinking" not in json.dumps(payload)
    second, resumed = execute_case(_ai(), case, root=tmp_path, now=NOW)
    assert resumed
    assert second.identity == first.identity
    resumed_run = find_resumable_run(
        tmp_path,
        case=case,
        provider="mock/1.0.0",
        model="deterministic-mock/1.0.0",
        prompt_version="market_interpretation/1.0.0",
    )
    assert resumed_run is not None


def test_case_failures_are_append_only_and_do_not_block_later_mock_cases(tmp_path: Path) -> None:
    cases = controlled_cases()[:4]
    provider = MockProvider(
        modes=(MockMode.TIMEOUT, MockMode.VALID, MockMode.ERROR, MockMode.VALID)
    )
    ai = ChappieAI(
        ModelRouter({ModelProfile.LOCAL_DEFAULT: provider}), AIConfig(model="mock-model")
    )
    executions = [
        execute_case_isolated(ai, case, root=tmp_path, now=NOW, selection_index=index)
        for index, case in enumerate(cases, start=1)
    ]
    assert [item.run is not None for item in executions] == [False, True, False, True]
    assert [item.attempt.status.value if item.attempt else None for item in executions] == [
        "PROVIDER_TIMEOUT",
        None,
        "PROVIDER_ERROR",
        None,
    ]
    assert len(completed_runs(tmp_path)) == 2
    attempts = failure_attempts(tmp_path)
    assert {item.status.value for item in attempts} == {"PROVIDER_ERROR", "PROVIDER_TIMEOUT"}
    assert all(item.request_digest for item in attempts)
    assert all(item.status.value != "COMPLETED" for item in attempts)

    retried = execute_case_isolated(ai, cases[0], root=tmp_path, now=NOW)
    assert retried.run is not None
    assert not retried.resumed
    assert len(completed_runs(tmp_path)) == 3
    assert len(failure_attempts(tmp_path)) == 2


def test_validation_rejection_is_an_incomplete_attempt_with_request_identity(
    tmp_path: Path,
) -> None:
    execution = execute_case_isolated(
        _ai({"answer": "invalid"}), controlled_cases()[0], root=tmp_path, now=NOW
    )
    assert execution.run is None
    assert execution.attempt is not None
    assert execution.attempt.status.value == "VALIDATION_REJECTED"
    assert execution.attempt.request_digest
    assert completed_runs(tmp_path) == ()


def test_rejected_structured_output_is_sanitized_and_non_canonical(tmp_path: Path) -> None:
    execution = execute_case_isolated(
        _ai({"thinking": "private", "answer": "invalid"}),
        controlled_cases()[0],
        root=tmp_path,
        now=NOW,
        task=AITask.RESEARCH_REASONING_V11,
    )
    assert execution.run is None
    assert execution.attempt is not None
    assert execution.attempt.validation_code == "BASE_SCHEMA_INVALID"
    assert execution.attempt.field_path == "$"
    assert execution.attempt.rejected_output == {"answer": "invalid"}
    assert "thinking" not in json.dumps(execution.attempt.payload())


def test_rerun_is_a_new_immutable_evidence_record(tmp_path: Path) -> None:
    case = controlled_cases()[0]
    first, _ = execute_case(_ai(), case, root=tmp_path, now=NOW)
    second, resumed = execute_case(_ai(), case, root=tmp_path, rerun=True, now=NOW)
    assert not resumed
    assert first.identity != second.identity
    assert len(list(tmp_path.rglob("*.json"))) == 2


def test_case_manifest_and_aggregate_report_are_deterministic(tmp_path: Path) -> None:
    cases = controlled_cases()
    case = cases[0]
    manifest = write_case_manifest(tmp_path, cases, track=BenchmarkTrack.REASONING_SAFETY)
    assert write_case_manifest(tmp_path, cases, track=BenchmarkTrack.REASONING_SAFETY) == manifest
    run, _ = execute_case(_ai(), case, root=tmp_path, now=NOW)
    report = write_aggregate_report(tmp_path, (run,))
    payload = json.loads(report.read_text())
    assert payload["run_count"] == 1
    assert payload["runs"] == [run.identity]


def test_legacy_subset_manifest_is_repaired_only_when_all_cases_are_canonical(
    tmp_path: Path,
) -> None:
    cases = controlled_cases()
    path = tmp_path / benchmark_digest() / "case-manifest.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "benchmark_identity": BENCHMARK_IDENTITY,
                "benchmark_digest": benchmark_digest(),
                "cases": [case.payload() for case in cases[:5]],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    write_case_manifest(tmp_path, cases, track=BenchmarkTrack.REASONING_SAFETY)
    assert len(json.loads(path.read_text())["cases"]) == 15


def test_legacy_subset_manifest_fails_closed_if_case_is_not_canonical(tmp_path: Path) -> None:
    cases = controlled_cases()
    path = tmp_path / benchmark_digest() / "case-manifest.json"
    path.parent.mkdir(parents=True)
    changed = cases[0].payload()
    changed["known_unknowns"] = ["tuned after result"]
    path.write_text(
        json.dumps(
            {
                "benchmark_identity": BENCHMARK_IDENTITY,
                "benchmark_digest": benchmark_digest(),
                "cases": [changed],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    with pytest.raises(ValueError, match="conflicts"):
        write_case_manifest(tmp_path, cases, track=BenchmarkTrack.REASONING_SAFETY)


def test_partial_then_full_resume_uses_evidence_without_redefining_manifest(tmp_path: Path) -> None:
    cases = controlled_cases()
    provider = MockProvider()
    ai = ChappieAI(
        ModelRouter({ModelProfile.LOCAL_DEFAULT: provider}), AIConfig(model="mock-model")
    )
    write_case_manifest(tmp_path, cases, track=BenchmarkTrack.REASONING_SAFETY)
    first = [execute_case(ai, case, root=tmp_path, now=NOW) for case in cases[:5]]
    write_aggregate_report(tmp_path, completed_runs(tmp_path))
    first_ids = [run.identity for run, resumed in first if not resumed]
    assert len(first_ids) == 5
    assert len(completed_runs(tmp_path)) == 5

    write_case_manifest(tmp_path, cases, track=BenchmarkTrack.REASONING_SAFETY)
    full = [execute_case(ai, case, root=tmp_path, now=NOW) for case in cases]
    report = write_aggregate_report(tmp_path, completed_runs(tmp_path))
    assert [run.identity for run, resumed in full[:5] if resumed] == first_ids
    assert all(resumed for _, resumed in full[:5])
    assert all(not resumed for _, resumed in full[5:])
    assert len(provider.requests) == 15
    assert len(completed_runs(tmp_path)) == 15
    assert json.loads(report.read_text())["run_count"] == 15
    assert (
        len(json.loads((tmp_path / benchmark_digest() / "case-manifest.json").read_text())["cases"])
        == 15
    )


def test_historical_case_rejects_future_fact() -> None:
    future = FactV1("future", "test", NOW, "x", 1, None, "digest")
    with pytest.raises(ValueError, match="exceed"):
        BenchmarkCaseV1(
            "future",
            BenchmarkTrack.BLIND_HISTORICAL,
            datetime(2023, 1, 1, tzinfo=UTC),
            (future,),
            (),
            (),
            (),
            True,
        )


def test_market_only_sampler_is_timestamp_based_and_hides_rde_outcomes(tmp_path: Path) -> None:
    store = ResearchStore(tmp_path / "research.sqlite3")
    store.initialize()
    rows = tuple(
        IntelligenceRow(
            int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1000) + index * 300_000,
            1,
            "BTCUSDT",
            40_000 + index,
            None,
            None,
            None,
            0.01,
            0.1,
            "UP_OI_UP",
            "UP",
            "LOW",
            1.0,
        )
        for index in range(4)
    )
    store.replace_rows(rows, source_digest="source")
    cases = sample_market_only_cases(
        store, asset="BTCUSDT", limit=2, split=TemporalSplit.VALIDATION
    )
    store.close()
    assert [item.case.case_id for item in cases] == [
        "market-only:BTCUSDT:1704067200000",
        "market-only:BTCUSDT:1704067800000",
    ]
    assert all("forward_return" not in json.dumps(item.visible_payload()) for item in cases)


def _render(events: list[object], verbosity: TraceVerbosity) -> str:
    stream = StringIO()
    renderer = AITraceRenderer(verbosity, stream)
    for event in events:
        renderer.emit(event)  # type: ignore[arg-type]
    return stream.getvalue()


def test_compact_verbose_trace_and_quiet_rendering_use_canonical_events(tmp_path: Path) -> None:
    case = controlled_cases()[0]
    events = [
        trace_event(
            AITraceEventType.BENCHMARK_STARTED,
            provider="mock/1.0.0",
            model="deterministic-mock/1.0.0",
            metadata={"benchmark_identity": BENCHMARK_IDENTITY, "case_count": 1, "resume": True},
        )
    ]
    execute_case(
        _ai(
            {
                "observed_facts": [{"fact_id": case.facts[0].fact_id}],
                "interpretations": [],
                "hypotheses": [
                    {
                        "claim": "BTCUSDT may fall over 1h.",
                        "supporting_fact_ids": [case.facts[0].fact_id],
                        "status": "UNVALIDATED",
                    }
                ],
                "contradictions": [],
                "unknowns": ["Cause unavailable"],
                "evidence_requests": [],
                "confidence": 0.2,
            }
        ),
        case,
        root=tmp_path,
        now=NOW,
        trace=events.append,
    )
    compact = _render(events, TraceVerbosity.COMPACT)
    verbose = _render(events, TraceVerbosity.VERBOSE)
    technical = _render(events, TraceVerbosity.TRACE)
    assert "CHAPPIE AI" in compact
    assert "Calling Chappie AI" in compact
    assert "Validation:" in compact
    assert "Scores:" in compact
    assert "Result: PASS" in compact
    assert "HYPOTHESES" in verbose
    assert "Cause unavailable" in verbose
    assert "case_digest" in technical
    assert "request_digest" in technical
    assert "response_digest" in technical
    assert _render(events, TraceVerbosity.QUIET) == ""


def test_resumed_trace_explicitly_shows_no_provider_invocation(tmp_path: Path) -> None:
    case = controlled_cases()[0]
    provider = MockProvider()
    ai = ChappieAI(ModelRouter({ModelProfile.LOCAL_DEFAULT: provider}), AIConfig(model="mock"))
    first, _ = execute_case(ai, case, root=tmp_path, now=NOW)
    events: list[object] = []
    second, resumed = execute_case(ai, case, root=tmp_path, now=NOW, trace=events.append)
    assert resumed
    assert second.identity == first.identity
    assert len(provider.requests) == 1
    assert "RESUMED — no provider invocation" in _render(events, TraceVerbosity.COMPACT)


def test_provider_error_trace_is_readable_and_reraises(tmp_path: Path) -> None:
    events: list[object] = []
    with pytest.raises(AIProviderError):
        execute_case(
            _ai_provider(MockProvider(MockMode.ERROR)),
            controlled_cases()[0],
            root=tmp_path,
            trace=events.append,
        )
    assert "PROVIDER ERROR: AIProviderError" in _render(events, TraceVerbosity.COMPACT)


def test_trace_never_leaks_provider_thinking_or_raw_private_content() -> None:
    event = trace_event(
        AITraceEventType.AI_RESPONSE_VALIDATED,
        metadata={
            "provider_reasoning_present": True,
            "thinking": "private hidden reasoning",
            "raw_provider_response": "also private",
        },
    )
    rendered = _render([event], TraceVerbosity.TRACE)
    assert "provider_reasoning_present" in rendered
    assert "private hidden reasoning" not in rendered
    assert "also private" not in rendered


def test_trace_verbosity_does_not_change_immutable_run_identity(tmp_path: Path) -> None:
    case = controlled_cases()[0]
    events: list[object] = []
    first, _ = execute_case(_ai(), case, root=tmp_path, now=NOW, trace=events.append)
    _render(events, TraceVerbosity.COMPACT)
    resumed, reused = execute_case(_ai(), case, root=tmp_path, now=NOW, trace=events.append)
    _render(events, TraceVerbosity.TRACE)
    assert reused
    assert resumed.identity == first.identity


def _ai_provider(provider: MockProvider) -> ChappieAI:
    return ChappieAI(ModelRouter({ModelProfile.LOCAL_DEFAULT: provider}), AIConfig(model="mock"))
