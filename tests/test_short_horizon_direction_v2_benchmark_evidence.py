from __future__ import annotations

import json

import pytest

from app.tools import short_horizon_direction_v2_predictive_benchmark as benchmark


def _result() -> dict[str, object]:
    scenario = benchmark.resolve_fit_scenario("FIT_FULL_LOGISTIC_1H")
    plan = benchmark.INITIAL_CONFIRMATORY_FIT_PLAN
    return {
        "identity": benchmark.IDENTITY,
        "classification": "NON_AUTHORITATIVE_RUNTIME_BENCHMARK",
        "plan_digest": plan.digest,
        "scenario": scenario.identity,
        "scenario_digest": scenario.semantic_digest,
        "unit_identity": benchmark._unit_identity(plan, scenario),
        "load_wall_seconds": 0.1,
        "preprocessing_wall_seconds": 0.2,
        "fit_wall_seconds": 0.3,
        "predict_wall_seconds": 0.1,
        "serialization_wall_seconds": 0.1,
        "total_wall_seconds": 0.8,
        "cpu_seconds": 0.7,
        "cache_bytes": "UNAVAILABLE",
    }


def test_result_timing_contract_and_atomic_create_only_publish(tmp_path) -> None:
    root = tmp_path / "benchmarks" / "short_horizon_direction_v2"
    payload = _result()
    assert benchmark.persist_unit_result(root, payload) == "VERIFIED"
    final = root / "units" / f"{payload['unit_identity']}.json"
    assert json.loads(final.read_text(encoding="utf-8")) == payload
    assert not list((root / "units").glob("*.tmp"))
    assert benchmark.persist_unit_result(root, payload) == "REUSED"


def test_persistence_rejects_production_root_malformed_and_metrics(tmp_path) -> None:
    payload = _result()
    with pytest.raises(PermissionError):
        benchmark.persist_unit_result(tmp_path / "production", payload)
    with pytest.raises(ValueError):
        benchmark.persist_unit_result(tmp_path / "benchmarks" / "x", {"identity": "bad"})
    with pytest.raises(ValueError):
        benchmark.persist_unit_result(tmp_path / "benchmarks" / "x", {**payload, "accuracy": 1.0})


def test_preflight_is_terminal_and_never_calls_fit(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(benchmark, "validate_target_binding", lambda _: None)
    monkeypatch.setattr(benchmark, "execute_v2_fit_unit", lambda _: pytest.fail("must not fit"))
    result = benchmark.preflight(
        benchmark.INITIAL_CONFIRMATORY_FIT_PLAN, root=tmp_path / "benchmarks" / "v2"
    )
    assert result["status"] == "PASS"
    assert result["models_fitted"] is False
    assert result["metrics_produced"] is False
    assert not benchmark.FORBIDDEN_RESULT_KEYS.intersection(result)


def test_legacy_unit_without_cache_is_reused_without_fit(monkeypatch, tmp_path) -> None:
    root = tmp_path / "benchmarks" / "v2"
    scenario = benchmark.resolve_fit_scenario("FIT_FULL_LOGISTIC_1H")
    plan = benchmark.INITIAL_CONFIRMATORY_FIT_PLAN
    legacy = _result()
    legacy.pop("cache_bytes")
    unit = root / "units" / f"{legacy['unit_identity']}.json"
    unit.parent.mkdir(parents=True)
    unit.write_text(json.dumps(legacy), encoding="utf-8")
    monkeypatch.setattr(benchmark, "execute_v2_fit_unit", lambda _: pytest.fail("must not refit"))
    loaded = benchmark.execute_fit_benchmark_unit(plan, scenario, root=root)
    assert loaded["publication_status"] == "REUSED"
    assert loaded["cache_bytes"] == "UNAVAILABLE"


def test_preflight_rejects_wrong_plan_and_production_root(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(benchmark, "validate_target_binding", lambda _: None)
    bad = benchmark.BenchmarkPlan(benchmark.CONFIRMATORY_FIT_SCENARIOS[:-1])
    assert benchmark.preflight(bad, root=tmp_path / "benchmarks" / "v2")["status"] == "FAIL"
    assert (
        benchmark.preflight(benchmark.INITIAL_CONFIRMATORY_FIT_PLAN, root=tmp_path / "production")[
            "status"
        ]
        == "FAIL"
    )
