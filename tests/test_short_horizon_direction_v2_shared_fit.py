from __future__ import annotations

import json
from dataclasses import replace
from inspect import signature
from types import SimpleNamespace

import pytest

from app.research.capital_flow import short_horizon_direction_v2_fit as v2_fit
from app.research.capital_flow import tsunami_research_execution as research_execution
from app.research.capital_flow.short_horizon_direction_v2_fit import (
    FULL_V2_FEATURE_ORDER,
    build_v2_logistic_binding,
    evaluate_v2_fit_predictive_evidence,
    validate_v2_scientific_fit_binding,
)
from app.research.capital_flow.tsunami_fold_engine import V2_DIRECTION_1H_TARGET_BINDING
from app.research.capital_flow.tsunami_research_execution import (
    V2_FULL_V2_LOGISTIC_1H_WF1,
    write_v2_fit_predictive_evaluation,
)
from app.tools import short_horizon_direction_v2_predictive_benchmark as benchmark
from app.tools import short_horizon_direction_v2_smoke_fit as smoke


def _evidence(binding):
    return {
        "fold": binding.fold_identity,
        "horizon": binding.horizon,
        "model": binding.model_family,
        "load_wall_seconds": 0.1,
        "preprocessing_wall_seconds": 0.1,
        "fit_wall_seconds": 0.1,
        "predict_wall_seconds": 0.1,
        "serialization_wall_seconds": 0.1,
        "total_wall_seconds": 0.2,
    }


def test_smoke_resolves_to_shared_adapter(monkeypatch) -> None:
    captured = []
    monkeypatch.setattr(
        smoke, "execute_v2_fit_unit", lambda binding: captured.append(binding) or _evidence(binding)
    )

    result = smoke.run()

    assert captured == [smoke.smoke_binding()]
    assert result["classification"] == "NON_AUTHORITATIVE_SMOKE_TEST"


def test_benchmark_fit_primitive_resolves_to_same_shared_adapter(monkeypatch, tmp_path) -> None:
    captured = []
    monkeypatch.setattr(
        benchmark,
        "execute_v2_fit_unit",
        lambda binding: captured.append(binding) or _evidence(binding),
    )
    plan = benchmark.BenchmarkPlan(("FIT_FULL_LOGISTIC_1H",))

    scenario = benchmark.resolve_fit_scenario("FIT_FULL_LOGISTIC_1H")
    result = benchmark.execute_fit_benchmark_unit(
        plan, scenario, root=tmp_path / "benchmarks" / "v2"
    )

    assert captured == [scenario.binding]
    assert result["structural_status"] == "PASS"


def test_equivalent_smoke_and_benchmark_binding_are_frozen_and_identical() -> None:
    first = smoke.smoke_binding()
    second = smoke.smoke_binding()
    benchmark_binding = benchmark.resolve_fit_binding("FIT_FULL_LOGISTIC_1H")

    assert first == second
    assert first == benchmark_binding
    assert first.feature_schema is benchmark_binding.feature_schema
    assert first.target_binding is benchmark_binding.target_binding
    assert first.fold_schedule_identity == benchmark_binding.fold_schedule_identity
    assert first.fold_identity == benchmark_binding.fold_identity
    assert first.horizon == benchmark_binding.horizon
    assert first.feature_projection == benchmark_binding.feature_projection == FULL_V2_FEATURE_ORDER
    assert first.model_configuration == benchmark_binding.model_configuration


def test_benchmark_cannot_inject_arbitrary_scientific_callback() -> None:
    assert tuple(signature(benchmark.execute_fit_benchmark_unit).parameters) == (
        "plan",
        "scenario",
        "root",
    )
    with pytest.raises(ValueError, match="unknown"):
        benchmark.resolve_fit_binding("FIT_UNKNOWN")


def test_validation_boundary_is_rejected_before_load() -> None:
    invalid = replace(
        build_v2_logistic_binding(experiment_identity="test"),
        target_binding=replace(
            V2_DIRECTION_1H_TARGET_BINDING,
            research_end_exclusive=V2_DIRECTION_1H_TARGET_BINDING.research_start,
        ),
    )
    with pytest.raises(ValueError, match="binding mismatch"):
        validate_v2_scientific_fit_binding(invalid)


def test_predictive_evidence_uses_fold_metrics_and_training_frequency_baseline(monkeypatch) -> None:
    binding = build_v2_logistic_binding(experiment_identity="test")
    dataset = SimpleNamespace()
    calls = []

    monkeypatch.setattr(v2_fit, "load_v2_fold_dataset", lambda *_args: dataset)

    def evaluate(train, assessment, model, **kwargs):
        calls.append((train, assessment, model, kwargs))
        metric = 0.6 if model == "logistic_baseline" else 0.4
        return SimpleNamespace(
            true_labels=("UP", "DOWN", "NO_MOVE", "UP"),
            class_order=("UP", "DOWN", "NO_MOVE"),
            metrics={"balanced_accuracy": metric, "direction_accuracy": metric},
        )

    monkeypatch.setattr(v2_fit, "evaluate_fold", evaluate)

    evidence = evaluate_v2_fit_predictive_evidence(
        binding, work_unit_identity="v2-authoritative-fit-" + "a" * 24
    )

    assert [item[2] for item in calls] == ["logistic_baseline", "historical_frequency_probability"]
    assert all(item[3]["schema"] is binding.feature_schema for item in calls)
    assert all(item[3]["target_binding"] is binding.target_binding for item in calls)
    assert evidence["class_support"] == {"UP": 2, "DOWN": 1, "NO_MOVE": 1}
    assert evidence["primary_metric_value"] == 0.6
    assert evidence["baseline_primary_metric_value"] == 0.4
    assert evidence["primary_metric_delta"] == pytest.approx(0.2)
    assert evidence["validation_accessed"] is False
    assert evidence["blind_oos_accessed"] is False


def test_predictive_evidence_writer_canonicalizes_nested_configuration_for_reload(
    monkeypatch, tmp_path
) -> None:
    """JSON turns frozen configuration tuples into lists; reload must compare canonical values."""
    unit = V2_FULL_V2_LOGISTIC_1H_WF1
    monkeypatch.setattr(
        research_execution,
        "verify_checkpoint",
        lambda *_args: SimpleNamespace(status="REUSED"),
    )
    evidence = {
        "schema": v2_fit.V2_FIT_PREDICTIVE_EVALUATION_IDENTITY,
        "work_unit_identity": unit.identity,
        "model_configuration": (("l2", 0.0), ("max_iter", 100)),
        "validation_accessed": False,
        "blind_oos_accessed": False,
    }

    path = write_v2_fit_predictive_evaluation(tmp_path, unit, evidence)

    assert json.loads(path.read_text(encoding="utf-8"))["model_configuration"] == [
        ["l2", 0.0],
        ["max_iter", 100],
    ]
    assert write_v2_fit_predictive_evaluation(tmp_path, unit, evidence) == path
