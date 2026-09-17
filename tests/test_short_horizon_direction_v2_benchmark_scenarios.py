from __future__ import annotations

from dataclasses import replace

import pytest

from app.research.capital_flow.short_horizon_direction_v2_fit import (
    BTC_ONLY_CAUSAL_INPUT_ORDER,
    CAPITAL_FLOW_FEATURE_ORDER,
    FULL_V2_FEATURE_ORDER,
    LOGISTIC_BASELINE_CONFIGURATION,
    MARKET_STRUCTURE_FEATURE_ORDER,
    TECHNICAL_FEATURE_ORDER,
    VOLUME_FEATURE_ORDER,
)
from app.research.capital_flow.tsunami_fold_engine import (
    V2_DIRECTION_1H_TARGET_BINDING,
    V2_DIRECTION_4H_TARGET_BINDING,
)
from app.tools import short_horizon_direction_v2_predictive_benchmark as benchmark


@pytest.mark.parametrize(
    ("identity", "projection", "model", "horizon"),
    (
        ("FIT_BTC_ONLY_1H", BTC_ONLY_CAUSAL_INPUT_ORDER, "btc_only_causal_price_volatility", "1h"),
        ("FIT_CAPITAL_FLOW_LOGISTIC_1H", CAPITAL_FLOW_FEATURE_ORDER, "logistic_baseline", "1h"),
        ("FIT_TECHNICAL_LOGISTIC_1H", TECHNICAL_FEATURE_ORDER, "logistic_baseline", "1h"),
        ("FIT_VOLUME_LOGISTIC_1H", VOLUME_FEATURE_ORDER, "logistic_baseline", "1h"),
        (
            "FIT_MARKET_STRUCTURE_LOGISTIC_1H",
            MARKET_STRUCTURE_FEATURE_ORDER,
            "logistic_baseline",
            "1h",
        ),
        ("FIT_FULL_LOGISTIC_1H", FULL_V2_FEATURE_ORDER, "logistic_baseline", "1h"),
        ("FIT_FULL_LOGISTIC_4H", FULL_V2_FEATURE_ORDER, "logistic_baseline", "4h"),
    ),
)
def test_resolved_scenario_scientific_bindings(identity, projection, model, horizon) -> None:
    scenario = benchmark.resolve_fit_scenario(identity)
    assert scenario.binding.fold_identity == "V2_WF1"
    assert scenario.binding.feature_projection == projection
    assert scenario.binding.model_family == model
    assert scenario.binding.horizon == horizon
    assert scenario.binding.target_binding is (
        V2_DIRECTION_1H_TARGET_BINDING if horizon == "1h" else V2_DIRECTION_4H_TARGET_BINDING
    )


def test_feature_family_orders_and_btc_representation_are_exact() -> None:
    assert len(CAPITAL_FLOW_FEATURE_ORDER) == 15
    assert len(TECHNICAL_FEATURE_ORDER) == 21
    assert len(VOLUME_FEATURE_ORDER) == 9
    assert len(MARKET_STRUCTURE_FEATURE_ORDER) == 11
    assert len(FULL_V2_FEATURE_ORDER) == 60
    assert len([name for name in FULL_V2_FEATURE_ORDER if name.startswith("mtf.")]) == 4
    assert BTC_ONLY_CAUSAL_INPUT_ORDER == ("entry_open", "sigma_h")
    assert BTC_ONLY_CAUSAL_INPUT_ORDER != FULL_V2_FEATURE_ORDER[:2]


def test_regularized_and_tree_are_explicitly_not_applicable_without_v2_contract() -> None:
    for identity in ("FIT_FULL_REGULARIZED_LOGISTIC_1H", "FIT_FULL_TREE_1H"):
        with pytest.raises(ValueError, match="OUT_OF_CONFIRMATORY_V2"):
            benchmark.resolve_fit_scenario(identity)


def test_scenario_digests_are_deterministic_and_bind_science() -> None:
    scenario = benchmark.resolve_fit_scenario("FIT_FULL_LOGISTIC_1H")
    assert (
        scenario.semantic_digest
        == benchmark.resolve_fit_scenario("FIT_FULL_LOGISTIC_1H").semantic_digest
    )
    assert (
        replace(
            scenario,
            binding=replace(scenario.binding, feature_projection=CAPITAL_FLOW_FEATURE_ORDER),
        ).semantic_digest
        != scenario.semantic_digest
    )
    assert (
        replace(scenario, binding=replace(scenario.binding, horizon="4h")).semantic_digest
        != scenario.semantic_digest
    )
    assert (
        replace(
            scenario, binding=replace(scenario.binding, model_configuration=(("l2", "1.0"),))
        ).semantic_digest
        != scenario.semantic_digest
    )
    assert scenario.binding.model_configuration == LOGISTIC_BASELINE_CONFIGURATION


def test_modified_scenario_is_rejected_by_execution_gate(monkeypatch) -> None:
    scenario = benchmark.resolve_fit_scenario("FIT_FULL_LOGISTIC_1H")
    plan = benchmark.BenchmarkPlan((scenario.identity,))
    monkeypatch.setattr(benchmark, "execute_v2_fit_unit", lambda _: {})
    with pytest.raises(ValueError, match="rejected"):
        benchmark.execute_fit_benchmark_unit(
            plan, replace(scenario, binding=replace(scenario.binding, horizon="4h"))
        )


def test_confirmatory_catalogue_freezes_one_model_and_primary_comparisons() -> None:
    assert tuple(benchmark.RESOLVED_FIT_SCENARIOS) == benchmark.CONFIRMATORY_FIT_SCENARIOS
    assert len(benchmark.CONFIRMATORY_FIT_SCENARIOS) == 7
    assert benchmark.MAX_REAL_FITS_PER_RUN == 7
    assert benchmark.INITIAL_CONFIRMATORY_FIT_PLAN.scenarios == benchmark.CONFIRMATORY_FIT_SCENARIOS
    assert benchmark.OUT_OF_CONFIRMATORY_V2 == {
        "regularized_logistic",
        "deterministic_tree_boosting",
    }
    assert all(
        scenario.binding.model_family == "logistic_baseline"
        for identity, scenario in benchmark.RESOLVED_FIT_SCENARIOS.items()
        if identity != "FIT_BTC_ONLY_1H"
    )
    assert all(
        scenario.binding.model_configuration == LOGISTIC_BASELINE_CONFIGURATION
        for identity, scenario in benchmark.RESOLVED_FIT_SCENARIOS.items()
        if identity != "FIT_BTC_ONLY_1H"
    )
    assert benchmark.PRIMARY_COMPARISONS == (
        ("FIT_FULL_LOGISTIC_1H", "FIT_BTC_ONLY_1H", "balanced_accuracy"),
        ("FIT_FULL_LOGISTIC_1H", "FIT_CAPITAL_FLOW_LOGISTIC_1H", "balanced_accuracy"),
    )
