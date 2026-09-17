"""Bounded, non-authoritative V2 runtime benchmark plan (execution intentionally separate)."""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from app.research.capital_flow.short_horizon_direction_v2_fit import (
    CAPITAL_FLOW_FEATURE_ORDER,
    MARKET_STRUCTURE_FEATURE_ORDER,
    TECHNICAL_FEATURE_ORDER,
    VOLUME_FEATURE_ORDER,
    V2ScientificFitBinding,
    build_v2_btc_only_binding,
    build_v2_logistic_binding,
    execute_v2_fit_unit,
    validate_v2_scientific_fit_binding,
)
from app.research.capital_flow.tsunami_fold_engine import validate_target_binding

IDENTITY = "CHAPPIE_SHORT_HORIZON_DIRECTION_V2_RUNTIME_BENCHMARK/1.0.0"
ROOT = Path("E:/projects/chappie/data/research/benchmarks/short_horizon_direction_v2")
CONFIRMATORY_FIT_SCENARIOS = (
    "FIT_BTC_ONLY_1H",
    "FIT_CAPITAL_FLOW_LOGISTIC_1H",
    "FIT_TECHNICAL_LOGISTIC_1H",
    "FIT_VOLUME_LOGISTIC_1H",
    "FIT_MARKET_STRUCTURE_LOGISTIC_1H",
    "FIT_FULL_LOGISTIC_1H",
    "FIT_FULL_LOGISTIC_4H",
)
SCENARIOS = CONFIRMATORY_FIT_SCENARIOS
MAX_REAL_FITS_PER_RUN = 7
LIMITS = {
    "fits": MAX_REAL_FITS_PER_RUN,
    "permutations": 3,
    "ablations": 3,
    "families": 2,
    "total": 15,
    "workers": 3,
}
RAM_BUDGET_BYTES = 10 * 1024**3
FORBIDDEN_RESULT_KEYS = frozenset(
    {
        "accuracy",
        "balanced_accuracy",
        "precision",
        "recall",
        "f1",
        "brier",
        "log_loss",
        "confusion_matrix",
        "calibration",
        "feature_importance",
        "winner",
    }
)
REQUIRED_TIMING_KEYS = frozenset(
    {
        "load_wall_seconds",
        "preprocessing_wall_seconds",
        "fit_wall_seconds",
        "predict_wall_seconds",
        "serialization_wall_seconds",
        "total_wall_seconds",
        "cpu_seconds",
    }
)
EVIDENCE_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class BenchmarkPlan:
    scenarios: tuple[str, ...]
    workers: int = 1
    identity: str = IDENTITY

    def __post_init__(self) -> None:
        if (
            not set(self.scenarios).issubset(SCENARIOS)
            or self.workers not in (1, 2, 3)
            or len(self.scenarios) > LIMITS["total"]
        ):
            raise ValueError("bounded benchmark plan violation")

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            json.dumps(asdict(self), default=str, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


INITIAL_CONFIRMATORY_FIT_PLAN = BenchmarkPlan(CONFIRMATORY_FIT_SCENARIOS)


@dataclass(frozen=True, slots=True)
class BenchmarkScenario:
    identity: str
    binding: V2ScientificFitBinding
    status: str = "AVAILABLE"
    reason: str | None = None

    @property
    def semantic_digest(self) -> str:
        return hashlib.sha256(
            json.dumps(asdict(self), default=str, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


_BENCHMARK_EXPERIMENT = "CHAPPIE_SHORT_HORIZON_MARKET_DIRECTION_V2/1.0.0"
CONFIRMATORY_MODEL_FAMILY = "logistic_baseline"
CONFIRMATORY_MODEL_CONFIGURATION_IDENTITY = "V2_LOGISTIC_BASELINE_CONFIRMATORY/1.0.0"
OUT_OF_CONFIRMATORY_V2 = frozenset({"regularized_logistic", "deterministic_tree_boosting"})
_UNSUPPORTED = {
    "FIT_FULL_REGULARIZED_LOGISTIC_1H": "OUT_OF_CONFIRMATORY_V2: regularized_logistic is excluded from the frozen confirmatory catalogue",
    "FIT_FULL_TREE_1H": "OUT_OF_CONFIRMATORY_V2: deterministic_tree_boosting is excluded from the frozen confirmatory catalogue",
}

FAMILY_COMBINATION_CATALOGUE = (
    ("CAPITAL_FLOW",),
    ("TECHNICAL",),
    ("VOLUME",),
    ("MARKET_STRUCTURE",),
    ("TECHNICAL", "VOLUME"),
    ("TECHNICAL", "MARKET_STRUCTURE"),
    ("VOLUME", "MARKET_STRUCTURE"),
    ("CAPITAL_FLOW", "TECHNICAL"),
    ("CAPITAL_FLOW", "TECHNICAL", "VOLUME"),
    ("FULL_V2",),
)
PRIMARY_COMPARISONS = (
    ("FIT_FULL_LOGISTIC_1H", "FIT_BTC_ONLY_1H", "balanced_accuracy"),
    ("FIT_FULL_LOGISTIC_1H", "FIT_CAPITAL_FLOW_LOGISTIC_1H", "balanced_accuracy"),
)


def _scenario(identity: str, binding: V2ScientificFitBinding) -> BenchmarkScenario:
    return BenchmarkScenario(identity, binding)


RESOLVED_FIT_SCENARIOS = {
    "FIT_BTC_ONLY_1H": _scenario(
        "FIT_BTC_ONLY_1H", build_v2_btc_only_binding(experiment_identity=_BENCHMARK_EXPERIMENT)
    ),
    "FIT_CAPITAL_FLOW_LOGISTIC_1H": _scenario(
        "FIT_CAPITAL_FLOW_LOGISTIC_1H",
        build_v2_logistic_binding(
            experiment_identity=_BENCHMARK_EXPERIMENT, feature_projection=CAPITAL_FLOW_FEATURE_ORDER
        ),
    ),
    "FIT_TECHNICAL_LOGISTIC_1H": _scenario(
        "FIT_TECHNICAL_LOGISTIC_1H",
        build_v2_logistic_binding(
            experiment_identity=_BENCHMARK_EXPERIMENT, feature_projection=TECHNICAL_FEATURE_ORDER
        ),
    ),
    "FIT_VOLUME_LOGISTIC_1H": _scenario(
        "FIT_VOLUME_LOGISTIC_1H",
        build_v2_logistic_binding(
            experiment_identity=_BENCHMARK_EXPERIMENT, feature_projection=VOLUME_FEATURE_ORDER
        ),
    ),
    "FIT_MARKET_STRUCTURE_LOGISTIC_1H": _scenario(
        "FIT_MARKET_STRUCTURE_LOGISTIC_1H",
        build_v2_logistic_binding(
            experiment_identity=_BENCHMARK_EXPERIMENT,
            feature_projection=MARKET_STRUCTURE_FEATURE_ORDER,
        ),
    ),
    "FIT_FULL_LOGISTIC_1H": _scenario(
        "FIT_FULL_LOGISTIC_1H", build_v2_logistic_binding(experiment_identity=_BENCHMARK_EXPERIMENT)
    ),
    "FIT_FULL_LOGISTIC_4H": _scenario(
        "FIT_FULL_LOGISTIC_4H",
        build_v2_logistic_binding(experiment_identity=_BENCHMARK_EXPERIMENT, horizon="4h"),
    ),
}


def status(plan: BenchmarkPlan) -> dict[str, object]:
    return {
        "classification": "NON_AUTHORITATIVE_RUNTIME_BENCHMARK",
        "identity": IDENTITY,
        "root": str(ROOT),
        "plan_digest": plan.digest,
        "pending": list(plan.scenarios),
        "verified": [],
        "failed": [],
        "workers": plan.workers,
        "metrics_suppressed": True,
        "validation_accessed": False,
    }


def _unit_identity(plan: BenchmarkPlan, scenario: BenchmarkScenario) -> str:
    return hashlib.sha256(
        json.dumps(
            {"benchmark": IDENTITY, "plan": plan.digest, "scenario": scenario.semantic_digest},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _validate_result(payload: dict[str, object], *, legacy_ok: bool = True) -> None:
    if (
        payload.get("identity") != IDENTITY
        or payload.get("classification") != "NON_AUTHORITATIVE_RUNTIME_BENCHMARK"
        or FORBIDDEN_RESULT_KEYS.intersection(payload)
        or not REQUIRED_TIMING_KEYS.issubset(payload)
    ):
        raise ValueError("benchmark result contract mismatch")
    if "cache_bytes" not in payload:
        if not legacy_ok or payload.get("evidence_schema_version", 1) != 1:
            raise ValueError("benchmark result requires cache_bytes")
    elif payload["cache_bytes"] != "UNAVAILABLE" and (
        not isinstance(payload["cache_bytes"], int) or payload["cache_bytes"] < 0
    ):
        raise ValueError("benchmark cache_bytes must be nonnegative or UNAVAILABLE")
    if any(float(payload[key]) < 0 for key in REQUIRED_TIMING_KEYS if key != "cpu_seconds"):
        raise ValueError("benchmark timing must be nonnegative")
    if float(payload["total_wall_seconds"]) < max(
        float(payload[key]) for key in REQUIRED_TIMING_KEYS - {"total_wall_seconds", "cpu_seconds"}
    ):
        raise ValueError("benchmark total timing is smaller than a phase")


def persist_unit_result(root: Path, payload: dict[str, object]) -> str:
    """Create-only atomic publication of non-authoritative verified unit evidence."""
    _validate_result(payload, legacy_ok=False)
    root = root.resolve()
    if "benchmarks" not in root.parts:
        raise PermissionError("benchmark root isolation failure")
    units = root / "units"
    units.mkdir(parents=True, exist_ok=True)
    final = units / f"{payload['unit_identity']}.json"
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if final.exists():
        existing = json.loads(final.read_text(encoding="utf-8"))
        _validate_result(existing)
        if "cache_bytes" not in existing:
            existing["cache_bytes"] = "UNAVAILABLE"
        comparable = dict(payload)
        comparable.pop("evidence_schema_version", None)
        if existing != comparable:
            raise FileExistsError(
                "benchmark unit identity already published with different evidence"
            )
        return "REUSED"
    temporary = units / f".{payload['unit_identity']}.tmp"
    temporary.write_text(canonical, encoding="utf-8")
    reloaded = json.loads(temporary.read_text(encoding="utf-8"))
    _validate_result(reloaded)
    os.replace(temporary, final)
    return "VERIFIED"


def load_verified_unit(
    root: Path, plan: BenchmarkPlan, scenario: BenchmarkScenario
) -> dict[str, object] | None:
    path = root.resolve() / "units" / f"{_unit_identity(plan, scenario)}.json"
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    _validate_result(payload)
    payload.setdefault("cache_bytes", "UNAVAILABLE")
    payload["publication_status"] = "REUSED"
    return payload


def preflight(plan: BenchmarkPlan, *, root: Path = ROOT) -> dict[str, object]:
    """Bounded binding/persistence gate; it never loads folds or fits a model."""
    root = root.resolve()
    checks = {
        "root_isolation": "benchmarks" in root.parts,
        "scenario_count": len(plan.scenarios) == len(CONFIRMATORY_FIT_SCENARIOS),
        "unit_limit": len(plan.scenarios) <= MAX_REAL_FITS_PER_RUN,
        "scenario_digests": True,
        "dataset_binding": True,
        "manifest_binding": True,
        "fold_schedule": True,
        "target_bindings": True,
        "validation_firewall": True,
    }
    try:
        if plan.identity != IDENTITY or tuple(plan.scenarios) != CONFIRMATORY_FIT_SCENARIOS:
            raise ValueError("preflight plan mismatch")
        for identity in plan.scenarios:
            scenario = resolve_fit_scenario(identity)
            if RESOLVED_FIT_SCENARIOS.get(identity) != scenario:
                raise ValueError("preflight scenario digest mismatch")
            validate_v2_scientific_fit_binding(scenario.binding)
            validate_target_binding(scenario.binding.target_binding)
    except (OSError, PermissionError, ValueError):
        checks.update({key: False for key in checks if key != "root_isolation"})
    try:
        if not checks["root_isolation"]:
            raise PermissionError("production root selected")
        (root / "plans").mkdir(parents=True, exist_ok=True)
        checks["persistence_readiness"] = True
    except OSError:
        checks["persistence_readiness"] = False
    passed = all(checks.values())
    return {
        "status": "PASS" if passed else "FAIL",
        "benchmark_identity": IDENTITY,
        "plan_digest": plan.digest,
        "scenario_count": len(plan.scenarios),
        "fit_count": len(plan.scenarios),
        "root": str(root),
        **checks,
        "models_fitted": False,
        "metrics_produced": False,
        "validation_accessed": False,
        "blind_oos_accessed": False,
    }


@dataclass
class ResourceSampler:
    started: float = 0.0
    cpu: float = 0.0

    def __enter__(self):
        self.started = time.perf_counter()
        self.cpu = time.process_time()
        return self

    def __exit__(self, *_):
        pass

    def result(self) -> dict[str, object]:
        return {
            "wall_seconds": time.perf_counter() - self.started,
            "cpu_seconds": time.process_time() - self.cpu,
            "working_set_bytes": "UNAVAILABLE",
            "private_bytes": "UNAVAILABLE",
            "ram_classification": "UNAVAILABLE",
            "cache_bytes": "UNAVAILABLE",
        }


def resolve_fit_scenario(scenario: str) -> BenchmarkScenario:
    """Return a canonical immutable scenario or its explicit non-applicability reason."""
    if scenario in _UNSUPPORTED:
        raise ValueError(_UNSUPPORTED[scenario])
    try:
        return RESOLVED_FIT_SCENARIOS[scenario]
    except KeyError as exc:
        raise ValueError("unknown or unresolved benchmark fit scenario") from exc


def resolve_fit_binding(scenario: str) -> V2ScientificFitBinding:
    return resolve_fit_scenario(scenario).binding


def execute_fit_benchmark_unit(
    plan: BenchmarkPlan, scenario: BenchmarkScenario, *, root: Path = ROOT
) -> dict[str, object]:
    if (
        scenario.identity not in SCENARIOS
        or scenario.status != "AVAILABLE"
        or RESOLVED_FIT_SCENARIOS.get(scenario.identity) != scenario
        or sum(item.startswith("FIT_") for item in plan.scenarios) > LIMITS["fits"]
    ):
        raise ValueError("fit benchmark scenario/budget rejected")
    reused = load_verified_unit(root, plan, scenario)
    if reused is not None:
        return reused
    with ResourceSampler() as sample:
        payload = execute_v2_fit_unit(scenario.binding)
    if FORBIDDEN_RESULT_KEYS.intersection(payload):
        raise ValueError("predictive metric output forbidden")
    resources = sample.result()
    result = {
        "classification": "NON_AUTHORITATIVE_RUNTIME_BENCHMARK",
        "identity": IDENTITY,
        "scenario": scenario.identity,
        "scenario_digest": scenario.semantic_digest,
        "plan_digest": plan.digest,
        "unit_identity": _unit_identity(plan, scenario),
        "feature_projection": list(scenario.binding.feature_projection),
        "model_configuration": list(scenario.binding.model_configuration),
        "dataset_semantic_digest": scenario.binding.dataset_semantic_digest,
        "manifest_semantic_digest": scenario.binding.manifest_semantic_digest,
        "fold_schedule_identity": scenario.binding.fold_schedule_identity,
        "resources": resources,
        "cpu_seconds": resources["cpu_seconds"],
        "cache_bytes": resources["cache_bytes"],
        "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
        "structural_status": "PASS",
        **payload,
    }
    _validate_result(result)
    result["publication_status"] = persist_unit_result(root, result)
    return result
