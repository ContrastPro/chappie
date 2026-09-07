"""Resumable, fail-closed execution orchestration for Tsunami predictive Research."""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Self

from .free_v2_materializer import FEATURE_ORDER
from .tsunami_forecast_contract import FEATURE_FAMILY_MEMBERSHIP, WALK_FORWARD_FOLDS
from .tsunami_predictive_research import (
    ANALYTICAL_SEMANTIC_DIGEST,
    BOOTSTRAP_REPLICATES,
    HORIZON_NAMES,
    METRIC_CATALOGUE,
    MODEL_CATALOGUE,
    PERMUTATION_REPLICATES,
    PRODUCTION_CONTRACT,
    RANDOM_SEEDS,
    RESEARCH_CONTRACT_DIGEST,
    RESEARCH_IDENTITY,
    RESULT_IDENTITY,
    TARGET_MANIFEST_SEMANTIC_DIGEST,
    TARGET_SEMANTIC_DIGEST,
    TASKS,
    VALIDATION_FREEZE_IDENTITY,
)

EXECUTION_IDENTITY = "CHAPPIE_CAPITAL_FLOW_TSUNAMI_RESEARCH_EXECUTION/1.0.0"
EXECUTION_GRAPH_IDENTITY = "CHAPPIE_CAPITAL_FLOW_TSUNAMI_RESEARCH_EXECUTION_GRAPH/2.0.0"
SELECTION_RULE_IDENTITY = "SIX_FOLD_PRIMARY_BRIER_COMPLEXITY_LEXICAL/1.0.0"
SEED_SCHEDULE_IDENTITY = "TSUNAMI_RESEARCH_DETERMINISTIC_SEEDS/1.0.0"
CHECKPOINT_ROOT_NAME = "staging/checkpoints"
LOCK_NAME = "staging/execution.lock"
HEARTBEAT_INTERVAL_SECONDS = 60
BOOTSTRAP_BATCH_SIZE = 100
CHECKPOINT_SCHEMA = "CHAPPIE_RESEARCH_WORK_CHECKPOINT/1.0.0"
ARTIFACT_SCHEMA = "CHAPPIE_RESEARCH_WORK_RESULT/1.0.0"


class ExecutionPhase(StrEnum):
    INPUT_VERIFY = "INPUT_VERIFY"
    BASELINES = "BASELINES"
    BASE_MODEL_FITS = "BASE_MODEL_FITS"
    CANDIDATE_EVALUATION = "CANDIDATE_EVALUATION"
    MODEL_SELECTION = "MODEL_SELECTION"
    ABLATION = "ABLATION"
    PERMUTATION_IMPORTANCE = "PERMUTATION_IMPORTANCE"
    FAMILY_CONTRIBUTION = "FAMILY_CONTRIBUTION"
    INTERACTION_EVALUATION = "INTERACTION_EVALUATION"
    BOOTSTRAP = "BOOTSTRAP"
    MULTIPLE_TESTING = "MULTIPLE_TESTING"
    VALIDATION_CANDIDATE_FREEZE = "VALIDATION_CANDIDATE_FREEZE"
    FINAL_VERIFY = "FINAL_VERIFY"
    MANIFEST = "MANIFEST"


PHASES = tuple(ExecutionPhase)


def _canonical(value: object, *, pretty: bool = False) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=None if pretty else (",", ":"),
        indent=2 if pretty else None,
        ensure_ascii=True,
    ).encode()


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


EXECUTION_BINDING = {
    "execution_identity": EXECUTION_IDENTITY,
    "research_identity": RESEARCH_IDENTITY,
    "research_contract_digest": RESEARCH_CONTRACT_DIGEST,
    "experiment_identity": PRODUCTION_CONTRACT.experiment_identity,
    "analytical_semantic_digest": ANALYTICAL_SEMANTIC_DIGEST,
    "target_semantic_digest": TARGET_SEMANTIC_DIGEST,
    "target_manifest_semantic_digest": TARGET_MANIFEST_SEMANTIC_DIGEST,
    "feature_order": list(FEATURE_ORDER),
    "horizons": list(HORIZON_NAMES),
    "tasks": [item.value for item in TASKS],
    "folds": [item.identity for item in WALK_FORWARD_FOLDS],
    "models": [item.value for item in MODEL_CATALOGUE],
    "seeds": list(RANDOM_SEEDS),
    "metrics": list(METRIC_CATALOGUE),
}
EXECUTION_BINDING_DIGEST = _digest(EXECUTION_BINDING)


@dataclass(frozen=True, slots=True)
class WorkUnitV1:
    phase: ExecutionPhase
    horizon: str | None = None
    task: str | None = None
    fold: str | None = None
    model: str | None = None
    hyperparameters: tuple[tuple[str, str], ...] = ()
    feature_exclusion: str | None = None
    family_exclusion: str | None = None
    permutation_index: int | None = None
    bootstrap_start: int | None = None
    bootstrap_end_exclusive: int | None = None
    seed: int | None = None

    def payload(self) -> dict[str, object]:
        return {key: value for key, value in asdict(self).items() if value not in (None, (), [])}

    @property
    def identity(self) -> str:
        return f"{self.phase.value.lower()}-{_digest(self.payload())[:24]}"


@dataclass(frozen=True, slots=True)
class GraphWorkUnitV2:
    """Evidence-bound V2 identity used by genuine production dispatch."""

    phase: ExecutionPhase
    horizon: str | None = None
    task: str | None = None
    fold: str | None = None
    model: str | None = None
    hyperparameters: tuple[tuple[str, str], ...] = ()
    feature_set: tuple[str, ...] = FEATURE_ORDER
    preprocessing_identity: str | None = None
    calibration_identity: str | None = None
    seed: int | None = None
    fold_evidence_digests: tuple[str, ...] = ()
    candidate_aggregate_digests: tuple[str, ...] = ()
    selected_configuration_digest: str | None = None
    feature_exclusion: str | None = None
    family_exclusion: str | None = None
    family_operation: str | None = None
    permutation_index: int | None = None
    held_out_evidence_digest: str | None = None
    additive_configuration_digest: str | None = None
    interaction_configuration_digest: str | None = None
    bootstrap_start: int | None = None
    bootstrap_end_exclusive: int | None = None
    prediction_evidence_digest: str | None = None
    evidence_set_digest: str | None = None

    def __post_init__(self) -> None:
        if self.horizon is not None and self.horizon not in HORIZON_NAMES:
            raise ValueError("V2 work unit horizon is not frozen")
        if self.task is not None and self.task not in {item.value for item in TASKS}:
            raise ValueError("V2 work unit task is not frozen")
        if self.fold is not None and self.fold not in {
            item.identity for item in WALK_FORWARD_FOLDS
        }:
            raise ValueError("V2 work unit fold is not frozen")
        if self.feature_set and any(item not in FEATURE_ORDER for item in self.feature_set):
            raise ValueError("V2 work unit feature set is not frozen")
        if self.phase is ExecutionPhase.CANDIDATE_EVALUATION and (
            self.fold is not None or len(self.fold_evidence_digests) != 6
        ):
            raise ValueError("candidate aggregate requires exactly six ordered fold evidences")
        if self.phase is ExecutionPhase.MODEL_SELECTION and (
            not self.horizon or not self.task or not self.candidate_aggregate_digests
        ):
            raise ValueError("model selection must be task/horizon scoped with candidate evidence")
        if (
            self.phase
            in {
                ExecutionPhase.ABLATION,
                ExecutionPhase.PERMUTATION_IMPORTANCE,
                ExecutionPhase.FAMILY_CONTRIBUTION,
            }
            and not self.selected_configuration_digest
        ):
            raise ValueError("downstream contribution unit requires selected configuration")
        if self.phase is ExecutionPhase.BOOTSTRAP and (
            self.bootstrap_start is None
            or self.bootstrap_end_exclusive is None
            or self.bootstrap_end_exclusive - self.bootstrap_start != BOOTSTRAP_BATCH_SIZE
            or not self.prediction_evidence_digest
        ):
            raise ValueError("bootstrap requires exact batch and selected prediction evidence")

    def payload(self) -> dict[str, object]:
        payload = {
            "graph_identity": EXECUTION_GRAPH_IDENTITY,
            "execution_binding_digest": EXECUTION_BINDING_DIGEST,
            **{key: value for key, value in asdict(self).items() if value not in (None, (), [])},
        }
        return json.loads(_canonical(payload))

    @property
    def identity(self) -> str:
        return f"v2-{self.phase.value.lower()}-{_digest(self.payload())[:32]}"


def fold_candidate_unit(
    *,
    horizon: str,
    task: str,
    fold: str,
    model: str,
    hyperparameters: tuple[tuple[str, str], ...],
    preprocessing_identity: str,
    calibration_identity: str,
    seed: int,
    feature_set: tuple[str, ...] = FEATURE_ORDER,
) -> GraphWorkUnitV2:
    return GraphWorkUnitV2(
        ExecutionPhase.BASE_MODEL_FITS,
        horizon,
        task,
        fold,
        model,
        hyperparameters,
        feature_set,
        preprocessing_identity,
        calibration_identity,
        seed,
    )


def candidate_aggregate_unit(
    *,
    horizon: str,
    task: str,
    model: str,
    hyperparameters: tuple[tuple[str, str], ...],
    preprocessing_identity: str,
    calibration_identity: str,
    fold_evidence_digests: tuple[str, ...],
    feature_set: tuple[str, ...] = FEATURE_ORDER,
) -> GraphWorkUnitV2:
    return GraphWorkUnitV2(
        ExecutionPhase.CANDIDATE_EVALUATION,
        horizon=horizon,
        task=task,
        model=model,
        hyperparameters=hyperparameters,
        feature_set=feature_set,
        preprocessing_identity=preprocessing_identity,
        calibration_identity=calibration_identity,
        fold_evidence_digests=fold_evidence_digests,
    )


def model_selection_unit(
    *, horizon: str, task: str, candidate_aggregate_digests: tuple[str, ...]
) -> GraphWorkUnitV2:
    return GraphWorkUnitV2(
        ExecutionPhase.MODEL_SELECTION,
        horizon=horizon,
        task=task,
        candidate_aggregate_digests=candidate_aggregate_digests,
        evidence_set_digest=_digest(
            {"selection_rule": SELECTION_RULE_IDENTITY, "candidates": candidate_aggregate_digests}
        ),
    )


@dataclass(slots=True)
class ProductionGraphV2:
    """Deterministic production planner backed only by verified V2 artifacts."""

    root: Path
    analytical_root: Path
    target_root: Path
    _artifact_index: ArtifactIndexV1 | None = field(default=None, init=False, repr=False)
    _fold_cache: dict[tuple[object, ...], object] = field(
        default_factory=dict, init=False, repr=False
    )
    _cache_lock: RLock = field(default_factory=RLock, init=False, repr=False)

    def _candidate_specs(self) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
        specs: list[tuple[str, tuple[tuple[str, str], ...]]] = []
        for model in MODEL_CATALOGUE:
            if model.value == "regularized_logistic":
                specs.extend((model.value, (("l2", str(value)),)) for value in (0.01, 0.1, 1.0))
            else:
                specs.append((model.value, ()))
        return tuple(specs)

    def _fold_units(self) -> tuple[GraphWorkUnitV2, ...]:
        from .tsunami_fold_engine import CALIBRATION_IDENTITY, PREPROCESSING_IDENTITY

        return tuple(
            fold_candidate_unit(
                horizon=horizon,
                task=task.value,
                fold=fold.identity,
                model=model,
                hyperparameters=parameters,
                preprocessing_identity=PREPROCESSING_IDENTITY,
                calibration_identity=CALIBRATION_IDENTITY,
                seed=RANDOM_SEEDS[0],
            )
            for horizon in HORIZON_NAMES
            for task in TASKS
            for model, parameters in self._candidate_specs()
            for fold in WALK_FORWARD_FOLDS
        )

    def _artifact(self, digest: str) -> dict[str, object]:
        with self._cache_lock:
            if self._artifact_index is None:
                self._artifact_index = ArtifactIndexV1.build(self.root)
            payload = self._artifact_index.artifact(digest)
            if payload is not None:
                return payload
            # A phase can have just published dependencies after the index was built.
            # Refresh once on a miss; steady-state lookups remain O(1).
            self._artifact_index = ArtifactIndexV1.build(self.root)
            payload = self._artifact_index.artifact(digest)
            if payload is not None:
                return payload
        raise CheckpointError(f"verified dependency artifact not found: {digest}")

    def _fold_dataset(self, fold: str, phase: str, horizon: str, task: str) -> object:
        """Cache immutable raw joined fold data; fitted state is never shared."""
        key = (
            fold,
            phase,
            horizon,
            task,
            ANALYTICAL_SEMANTIC_DIGEST,
            TARGET_SEMANTIC_DIGEST,
            TARGET_MANIFEST_SEMANTIC_DIGEST,
            RESEARCH_CONTRACT_DIGEST,
            _digest((FEATURE_ORDER, CHECKPOINT_SCHEMA)),
        )
        with self._cache_lock:
            cached = self._fold_cache.get(key)
            if cached is not None:
                return cached
            from .tsunami_fold_engine import load_fold_dataset

            loaded = load_fold_dataset(
                fold,
                phase,  # type: ignore[arg-type]
                horizon,
                task,
                analytical_root=self.analytical_root,
                target_root=self.target_root,
            )
            self._fold_cache[key] = loaded
            return loaded

    def clear_fold_cache(self) -> None:
        """Release immutable raw fold data between memory-intensive refits."""
        with self._cache_lock:
            self._fold_cache.clear()

    def _selected_units(self, evidence: Mapping[str, str]) -> tuple[GraphWorkUnitV2, ...]:
        return tuple(
            unit
            for unit in self.resolvers()[3][1](evidence)
            if unit.phase is ExecutionPhase.MODEL_SELECTION
        )

    def resolvers(self) -> tuple[tuple[ExecutionPhase, PhaseResolverV2], ...]:
        input_unit = GraphWorkUnitV2(ExecutionPhase.INPUT_VERIFY)
        folds = self._fold_units()

        def input_phase(_evidence: Mapping[str, str]) -> tuple[GraphWorkUnitV2, ...]:
            return (input_unit,)

        def fold_phase(evidence: Mapping[str, str]) -> tuple[GraphWorkUnitV2, ...]:
            if input_unit.identity not in evidence:
                raise CheckpointError("fold phase requires verified input binding")
            return folds

        def aggregate_phase(evidence: Mapping[str, str]) -> tuple[GraphWorkUnitV2, ...]:
            units = []
            for horizon in HORIZON_NAMES:
                for task in TASKS:
                    for model, parameters in self._candidate_specs():
                        group = tuple(
                            unit
                            for unit in folds
                            if (unit.horizon, unit.task, unit.model, unit.hyperparameters)
                            == (horizon, task.value, model, parameters)
                        )
                        try:
                            digests = tuple(evidence[unit.identity] for unit in group)
                        except KeyError as exc:
                            raise CheckpointError("aggregate requires six verified folds") from exc
                        units.append(
                            candidate_aggregate_unit(
                                horizon=horizon,
                                task=task.value,
                                model=model,
                                hyperparameters=parameters,
                                preprocessing_identity=group[0].preprocessing_identity or "",
                                calibration_identity=group[0].calibration_identity or "",
                                fold_evidence_digests=digests,
                            )
                        )
            return tuple(units)

        def selection_phase(evidence: Mapping[str, str]) -> tuple[GraphWorkUnitV2, ...]:
            aggregates = aggregate_phase(evidence)
            units = []
            for horizon in HORIZON_NAMES:
                for task in TASKS:
                    group = tuple(
                        unit
                        for unit in aggregates
                        if (unit.horizon, unit.task) == (horizon, task.value)
                    )
                    try:
                        digests = tuple(evidence[unit.identity] for unit in group)
                    except KeyError as exc:
                        raise CheckpointError(
                            "selection requires verified candidate aggregates"
                        ) from exc
                    units.append(
                        model_selection_unit(
                            horizon=horizon,
                            task=task.value,
                            candidate_aggregate_digests=digests,
                        )
                    )
            return tuple(units)

        def selected_digest(evidence: Mapping[str, str], horizon: str, task: str) -> str:
            unit = next(
                item
                for item in selection_phase(evidence)
                if (item.horizon, item.task) == (horizon, task)
            )
            try:
                return evidence[unit.identity]
            except KeyError as exc:
                raise CheckpointError("downstream phase requires verified selection") from exc

        def ablation_phase(evidence: Mapping[str, str]) -> tuple[GraphWorkUnitV2, ...]:
            return tuple(
                GraphWorkUnitV2(
                    ExecutionPhase.ABLATION,
                    horizon=horizon,
                    task=task.value,
                    selected_configuration_digest=selected_digest(evidence, horizon, task.value),
                    feature_exclusion=feature,
                )
                for horizon in HORIZON_NAMES
                for task in TASKS
                for feature in FEATURE_ORDER
            )

        def permutation_phase(evidence: Mapping[str, str]) -> tuple[GraphWorkUnitV2, ...]:
            units = []
            for horizon in HORIZON_NAMES:
                for task in TASKS:
                    selected = selected_digest(evidence, horizon, task.value)
                    selected_payload = self._artifact(selected)["result"]
                    prediction_digest = str(selected_payload["selected_prediction_evidence_digest"])
                    for feature in FEATURE_ORDER:
                        for index in range(PERMUTATION_REPLICATES):
                            units.append(
                                GraphWorkUnitV2(
                                    ExecutionPhase.PERMUTATION_IMPORTANCE,
                                    horizon=horizon,
                                    task=task.value,
                                    selected_configuration_digest=selected,
                                    feature_exclusion=feature,
                                    permutation_index=index,
                                    held_out_evidence_digest=prediction_digest,
                                )
                            )
                    for family, _members in FEATURE_FAMILY_MEMBERSHIP:
                        for index in range(PERMUTATION_REPLICATES):
                            units.append(
                                GraphWorkUnitV2(
                                    ExecutionPhase.PERMUTATION_IMPORTANCE,
                                    horizon=horizon,
                                    task=task.value,
                                    selected_configuration_digest=selected,
                                    family_exclusion=family,
                                    family_operation="COHERENT_HELD_OUT_PERMUTATION",
                                    permutation_index=index,
                                    held_out_evidence_digest=prediction_digest,
                                )
                            )
            return tuple(units)

        def family_phase(evidence: Mapping[str, str]) -> tuple[GraphWorkUnitV2, ...]:
            return tuple(
                GraphWorkUnitV2(
                    ExecutionPhase.FAMILY_CONTRIBUTION,
                    horizon=horizon,
                    task=task.value,
                    selected_configuration_digest=selected_digest(evidence, horizon, task.value),
                    family_exclusion=family,
                    family_operation="LEAVE_FAMILY_OUT",
                )
                for horizon in HORIZON_NAMES
                for task in TASKS
                for family, _members in FEATURE_FAMILY_MEMBERSHIP
            )

        def interaction_phase(evidence: Mapping[str, str]) -> tuple[GraphWorkUnitV2, ...]:
            return tuple(
                GraphWorkUnitV2(
                    ExecutionPhase.INTERACTION_EVALUATION,
                    horizon=horizon,
                    task=task.value,
                    additive_configuration_digest=selected_digest(evidence, horizon, task.value),
                    interaction_configuration_digest=_digest(
                        {
                            "selected": selected_digest(evidence, horizon, task.value),
                            "kind": "interaction",
                        }
                    ),
                )
                for horizon in HORIZON_NAMES
                for task in TASKS
            )

        def bootstrap_phase(evidence: Mapping[str, str]) -> tuple[GraphWorkUnitV2, ...]:
            units = []
            for horizon in HORIZON_NAMES:
                for task in TASKS:
                    selected = selected_digest(evidence, horizon, task.value)
                    selected_payload = self._artifact(selected)["result"]
                    prediction_digest = str(selected_payload["selected_prediction_evidence_digest"])
                    for start in range(0, BOOTSTRAP_REPLICATES, BOOTSTRAP_BATCH_SIZE):
                        units.append(
                            GraphWorkUnitV2(
                                ExecutionPhase.BOOTSTRAP,
                                horizon=horizon,
                                task=task.value,
                                bootstrap_start=start,
                                bootstrap_end_exclusive=start + BOOTSTRAP_BATCH_SIZE,
                                prediction_evidence_digest=prediction_digest,
                                seed=RANDOM_SEEDS[1],
                            )
                        )
            return tuple(units)

        phases: list[tuple[ExecutionPhase, PhaseResolverV2]] = [
            (ExecutionPhase.INPUT_VERIFY, input_phase),
            (ExecutionPhase.BASE_MODEL_FITS, fold_phase),
            (ExecutionPhase.CANDIDATE_EVALUATION, aggregate_phase),
            (ExecutionPhase.MODEL_SELECTION, selection_phase),
            (ExecutionPhase.ABLATION, ablation_phase),
            (ExecutionPhase.PERMUTATION_IMPORTANCE, permutation_phase),
            (ExecutionPhase.FAMILY_CONTRIBUTION, family_phase),
            (ExecutionPhase.INTERACTION_EVALUATION, interaction_phase),
            (ExecutionPhase.BOOTSTRAP, bootstrap_phase),
        ]

        def terminal(phase: ExecutionPhase, required_phases: tuple[ExecutionPhase, ...]):
            def resolve(evidence: Mapping[str, str]) -> tuple[GraphWorkUnitV2, ...]:
                required: list[str] = []
                for required_phase, resolver in phases:
                    if required_phase in required_phases:
                        for unit in resolver(evidence):
                            try:
                                required.append(evidence[unit.identity])
                            except KeyError as exc:
                                raise CheckpointError(
                                    f"{phase.value} requires complete verified evidence"
                                ) from exc
                return (GraphWorkUnitV2(phase, evidence_set_digest=_digest(required)),)

            return resolve

        statistical = (
            ExecutionPhase.ABLATION,
            ExecutionPhase.PERMUTATION_IMPORTANCE,
            ExecutionPhase.FAMILY_CONTRIBUTION,
            ExecutionPhase.INTERACTION_EVALUATION,
            ExecutionPhase.BOOTSTRAP,
        )
        phases.append(
            (
                ExecutionPhase.MULTIPLE_TESTING,
                terminal(ExecutionPhase.MULTIPLE_TESTING, statistical),
            )
        )
        phases.append(
            (
                ExecutionPhase.VALIDATION_CANDIDATE_FREEZE,
                terminal(
                    ExecutionPhase.VALIDATION_CANDIDATE_FREEZE,
                    statistical + (ExecutionPhase.MULTIPLE_TESTING,),
                ),
            )
        )
        phases.append(
            (
                ExecutionPhase.FINAL_VERIFY,
                terminal(
                    ExecutionPhase.FINAL_VERIFY, (ExecutionPhase.VALIDATION_CANDIDATE_FREEZE,)
                ),
            )
        )
        phases.append(
            (
                ExecutionPhase.MANIFEST,
                terminal(ExecutionPhase.MANIFEST, (ExecutionPhase.FINAL_VERIFY,)),
            )
        )
        return tuple(phases)

    def _selected(self, digest: str) -> dict[str, object]:
        payload = self._artifact(digest)["result"]
        if (
            not isinstance(payload, dict)
            or payload.get("identity") != "SELECTED_RESEARCH_CONFIGURATION"
        ):
            raise CheckpointError("selected configuration dependency is invalid")
        return payload

    def _fold_result(self, digest: str) -> dict[str, object]:
        payload = self._artifact(digest)["result"]
        if not isinstance(payload, dict) or payload.get("kind") != "FOLD_EVALUATION":
            raise CheckpointError("fold evidence dependency is invalid")
        return payload

    def dispatch(self, unit: V2Unit, heartbeat: Callable[[], None]) -> Mapping[str, object]:
        """Dispatch one genuine V2 unit; every VERIFIED result contains real evidence."""
        if not isinstance(unit, GraphWorkUnitV2):
            raise CheckpointError("production V2 dispatcher rejects legacy work units")
        heartbeat()
        if unit.phase is ExecutionPhase.INPUT_VERIFY:
            from .tsunami_predictive_research import verify_bound_manifests

            analytical, target = verify_bound_manifests(self.analytical_root, self.target_root)
            return {
                "kind": "INPUT_VERIFY",
                "analytical_manifest_digest": _digest(analytical),
                "target_manifest_digest": _digest(target),
                "validation_accessed": False,
            }
        if unit.phase is ExecutionPhase.BASE_MODEL_FITS:
            from .tsunami_fold_engine import (
                checkpoint_evidence_bindings,
                evaluate_fold,
            )

            train = self._fold_dataset(
                unit.fold or "", "TRAIN", unit.horizon or "", unit.task or ""
            )
            assessment = self._fold_dataset(
                unit.fold or "", "ASSESSMENT", unit.horizon or "", unit.task or ""
            )
            parameters = dict(unit.hyperparameters)
            result = evaluate_fold(
                train,
                assessment,
                unit.model or "",
                l2=float(parameters.get("l2", "0")),
                interactions=unit.model == "explicit_interaction_candidate",
            )
            return {
                "kind": "FOLD_EVALUATION",
                "evidence": asdict(result),
                "bindings": checkpoint_evidence_bindings(result),
            }
        if unit.phase is ExecutionPhase.CANDIDATE_EVALUATION:
            folds = tuple(self._fold_result(item) for item in unit.fold_evidence_digests)
            if len(folds) != 6:
                raise CheckpointError("candidate aggregation requires six fold results")
            metric_name = "balanced_accuracy" if unit.task == "DIRECTION" else "pr_auc"
            primary = [item["evidence"]["metrics"].get(metric_name) for item in folds]
            briers = [item["evidence"]["metrics"].get("brier") for item in folds]
            valid_primary = [float(item) for item in primary if isinstance(item, (int, float))]
            valid_briers = [float(item) for item in briers if isinstance(item, (int, float))]
            fold_metric_states = []
            for fold in folds:
                evidence = fold["evidence"]
                metric = evidence["metrics"].get(metric_name)
                if isinstance(metric, (int, float)):
                    state, reason = "AVAILABLE", None
                else:
                    state = "PRIMARY_METRIC_UNAVAILABLE"
                    reason = str(evidence["metrics"].get("reason") or metric or "UNDEFINED")
                fold_metric_states.append(
                    {"fold": evidence["fold"], "state": state, "reason": reason}
                )
            prediction_digest = _digest(
                [item["bindings"]["held_out_prediction_artifact_digest"] for item in folds]
            )
            if not valid_primary:
                reasons = sorted(
                    {
                        str(item["reason"])
                        for item in fold_metric_states
                        if item["reason"] is not None
                    }
                )
                return {
                    "kind": "CANDIDATE_AGGREGATE",
                    "model": unit.model,
                    "hyperparameters": list(unit.hyperparameters),
                    "fold_artifact_digests": list(unit.fold_evidence_digests),
                    "fold_metric_states": fold_metric_states,
                    "primary_metric_state": "PRIMARY_METRIC_UNAVAILABLE",
                    "primary_metric_unavailable_reasons": reasons,
                    "primary_metric": None,
                    "brier": sum(valid_briers) / len(valid_briers) if valid_briers else None,
                    "selection_eligible": False,
                    "prediction_evidence_digest": prediction_digest,
                }
            return {
                "kind": "CANDIDATE_AGGREGATE",
                "model": unit.model,
                "hyperparameters": list(unit.hyperparameters),
                "fold_artifact_digests": list(unit.fold_evidence_digests),
                "fold_metric_states": fold_metric_states,
                "primary_metric_state": "PRIMARY_METRIC_AVAILABLE",
                "primary_metric": sum(valid_primary) / len(valid_primary),
                "brier": sum(valid_briers) / len(valid_briers) if valid_briers else 1.0,
                "selection_eligible": True,
                "prediction_evidence_digest": prediction_digest,
            }
        if unit.phase is ExecutionPhase.MODEL_SELECTION:
            from .tsunami_predictive_research import CandidateEvidenceV1, select_candidate

            aggregates = [
                self._artifact(item)["result"] for item in unit.candidate_aggregate_digests
            ]
            candidates = [
                CandidateEvidenceV1(
                    str(item["model"]),
                    float(item["primary_metric"])
                    if isinstance(item.get("primary_metric"), (int, float))
                    else None,
                    float(item["brier"]) if isinstance(item.get("brier"), (int, float)) else None,
                    tuple(tuple(pair) for pair in item["hyperparameters"]),
                    bool(
                        item.get(
                            "selection_eligible",
                            isinstance(item.get("primary_metric"), (int, float)),
                        )
                    ),
                )
                for item in aggregates
            ]
            selected = select_candidate(candidates)
            if selected is None:
                return {
                    "identity": "NO_SELECTABLE_CANDIDATE",
                    "horizon": unit.horizon,
                    "task": unit.task,
                    "selection_state": "NO_SELECTABLE_CANDIDATE",
                    "selection_eligible": False,
                    "candidate_aggregate_digests": list(unit.candidate_aggregate_digests),
                    "validation_accessed": False,
                }
            selected_index = candidates.index(selected)
            aggregate = aggregates[selected_index]
            return {
                "identity": "SELECTED_RESEARCH_CONFIGURATION",
                "horizon": unit.horizon,
                "task": unit.task,
                "model": selected.model,
                "hyperparameters": list(selected.hyperparameters),
                "selected_aggregate_digest": unit.candidate_aggregate_digests[selected_index],
                "selected_fold_artifact_digests": aggregate["fold_artifact_digests"],
                "selected_prediction_evidence_digest": aggregate["prediction_evidence_digest"],
                "selection_state": "SELECTED",
                "selection_eligible": True,
            }
        if unit.phase in {ExecutionPhase.ABLATION, ExecutionPhase.FAMILY_CONTRIBUTION}:
            from .tsunami_fold_engine import (
                ablation_evaluation,
                family_ablation_evaluation,
            )

            selected = self._selected(unit.selected_configuration_digest or "")
            values = []
            for fold in WALK_FORWARD_FOLDS:
                train = self._fold_dataset(
                    fold.identity, "TRAIN", unit.horizon or "", unit.task or ""
                )
                assessment = self._fold_dataset(
                    fold.identity, "ASSESSMENT", unit.horizon or "", unit.task or ""
                )
                l2 = float(dict(selected["hyperparameters"]).get("l2", "0"))
                result = (
                    ablation_evaluation(
                        train,
                        assessment,
                        str(selected["model"]),
                        unit.feature_exclusion or "",
                        l2=l2,
                    )
                    if unit.phase is ExecutionPhase.ABLATION
                    else family_ablation_evaluation(
                        train,
                        assessment,
                        str(selected["model"]),
                        unit.family_exclusion or "",
                        l2=l2,
                    )
                )
                metric = result.metrics.get(
                    "balanced_accuracy" if unit.task == "DIRECTION" else "pr_auc"
                )
                if isinstance(metric, (int, float)):
                    values.append(float(metric))
            if not values:
                raise CheckpointError("downstream refit produced no valid evidence")
            aggregate = self._artifact(str(selected["selected_aggregate_digest"]))["result"]
            raw_metric = sum(values) / len(values)
            return {
                "kind": unit.phase.value,
                "raw_metric": raw_metric,
                "raw_delta": float(aggregate["primary_metric"]) - raw_metric,
                "horizon": unit.horizon,
                "task": unit.task,
                "feature": unit.feature_exclusion,
                "family": unit.family_exclusion,
            }
        if unit.phase is ExecutionPhase.PERMUTATION_IMPORTANCE:
            return self._dispatch_permutation(unit)
        if unit.phase is ExecutionPhase.INTERACTION_EVALUATION:
            from .tsunami_fold_engine import evaluate_fold

            selected = self._selected(unit.additive_configuration_digest or "")
            metrics = []
            for fold in WALK_FORWARD_FOLDS:
                train = self._fold_dataset(
                    fold.identity, "TRAIN", unit.horizon or "", unit.task or ""
                )
                assessment = self._fold_dataset(
                    fold.identity, "ASSESSMENT", unit.horizon or "", unit.task or ""
                )
                result = evaluate_fold(
                    train, assessment, "explicit_interaction_candidate", interactions=True
                )
                metric = result.metrics.get(
                    "balanced_accuracy" if unit.task == "DIRECTION" else "pr_auc"
                )
                if isinstance(metric, (int, float)):
                    metrics.append(float(metric))
            if not metrics:
                raise CheckpointError("interaction comparison produced no valid evidence")
            additive = self._artifact(str(selected["selected_aggregate_digest"]))["result"]
            interaction_metric = sum(metrics) / len(metrics)
            return {
                "kind": "INTERACTION_EVALUATION",
                "horizon": unit.horizon,
                "task": unit.task,
                "metric": "balanced_accuracy" if unit.task == "DIRECTION" else "pr_auc",
                "selected_model": selected["model"],
                "comparison_binding": unit.interaction_configuration_digest,
                "additive_metric": float(additive["primary_metric"]),
                "interaction_metric": interaction_metric,
                "raw_delta": interaction_metric - float(additive["primary_metric"]),
                "evidence_digest": _digest(
                    {
                        "additive": unit.additive_configuration_digest,
                        "interaction": unit.interaction_configuration_digest,
                    }
                ),
            }
        if unit.phase is ExecutionPhase.BOOTSTRAP:
            from .tsunami_fold_engine import bootstrap_batch

            values = self._prediction_values(unit.prediction_evidence_digest or "")
            replicates = bootstrap_batch(
                values, unit.bootstrap_start or 0, unit.bootstrap_end_exclusive or 0
            )
            return {
                "kind": "BOOTSTRAP_BATCH",
                "horizon": unit.horizon,
                "task": unit.task,
                "metric": "balanced_accuracy" if unit.task == "DIRECTION" else "pr_auc",
                "prediction_evidence_digest": unit.prediction_evidence_digest,
                "batch_start": unit.bootstrap_start,
                "batch_end_exclusive": unit.bootstrap_end_exclusive,
                "replicates": list(replicates),
            }
        if unit.phase is ExecutionPhase.MULTIPLE_TESTING:
            confirmatory, exploratory = self._multiple_testing_sections()
            return {
                "kind": "MULTIPLE_TESTING",
                "confirmatory_multiple_testing": confirmatory,
                "exploratory_multiple_testing": exploratory,
                "contribution_aggregation": self._contribution_summary(),
                "bootstrap_aggregation": self._bootstrap_summary(),
                "evidence_set_digest": unit.evidence_set_digest,
            }
        if unit.phase in {
            ExecutionPhase.VALIDATION_CANDIDATE_FREEZE,
            ExecutionPhase.FINAL_VERIFY,
            ExecutionPhase.MANIFEST,
        }:
            return {
                "kind": unit.phase.value,
                "evidence_set_digest": unit.evidence_set_digest,
                "validation_accessed": False,
            }
        raise CheckpointError(f"unsupported production V2 phase: {unit.phase.value}")

    def _prediction_values(self, prediction_digest: str) -> tuple[float, ...]:
        values: list[float] = []
        selection = None
        for path in (self.root / CHECKPOINT_ROOT_NAME).glob("model_selection/*.result.json"):
            candidate = json.loads(path.read_text(encoding="utf-8"))["result"]
            if candidate.get("selected_prediction_evidence_digest") == prediction_digest:
                selection = candidate
                break
        if selection is None:
            raise CheckpointError("selected prediction binding is unavailable")
        for digest in selection["selected_fold_artifact_digests"]:
            evidence = self._fold_result(str(digest))["evidence"]
            if evidence["task"] == "DIRECTION":
                for label, row in zip(
                    evidence["true_labels"], evidence["probabilities"], strict=True
                ):
                    predicted = evidence["class_order"][max(range(len(row)), key=row.__getitem__)]
                    values.append(float(predicted == str(label)))
            else:
                for label, row in zip(
                    evidence["true_labels"], evidence["probabilities"], strict=True
                ):
                    values.append(-((float(row[1]) - int(label)) ** 2))
        if not values or len(prediction_digest) != 64:
            raise CheckpointError("selected held-out prediction evidence is unavailable")
        return tuple(values)

    def _dispatch_permutation(self, unit: GraphWorkUnitV2) -> Mapping[str, object]:
        import numpy as np

        from .tsunami_fold_engine import (
            BtcBaselineModelV2,
            PreprocessingV1,
            load_fitted_model,
            permute_held_out_family,
            permute_held_out_sensor,
            predict_from_fitted_model,
        )
        from .tsunami_predictive_research import interaction_row, pr_auc

        selected = self._selected(unit.selected_configuration_digest or "")
        operation = "family" if unit.family_exclusion else "sensor"
        primitive = permute_held_out_family if operation == "family" else permute_held_out_sensor
        scores: list[float] = []
        references: list[float] = []
        for digest in selected["selected_fold_artifact_digests"]:
            fold_result = self._fold_result(str(digest))
            evidence = fold_result["evidence"]
            assessment = self._fold_dataset(
                str(evidence["fold"]), "ASSESSMENT", unit.horizon or "", unit.task or ""
            )
            permuted = (
                primitive(assessment, unit.family_exclusion or "", unit.permutation_index or 0)
                if operation == "family"
                else primitive(
                    assessment, unit.feature_exclusion or "", unit.permutation_index or 0
                )
            )
            model = load_fitted_model(
                evidence["fitted_model_artifact"], expected_fold_identity=str(evidence["fold"])
            )
            if isinstance(model, BtcBaselineModelV2):
                matrix = permuted.btc_inputs
            elif evidence["preprocessing"] is None:
                matrix = ((0.0,),) * len(permuted.timestamps)
            else:
                preprocessing = PreprocessingV1(**evidence["preprocessing"])
                indexes = tuple(int(item) for item in model.feature_indexes)
                matrix = preprocessing.transform(permuted, indexes)
                if selected["model"] == "explicit_interaction_candidate":
                    interactions = np.asarray(
                        [
                            interaction_row(row[: len(FEATURE_ORDER)])[len(FEATURE_ORDER) :]
                            for row in matrix
                        ]
                    )
                    matrix = np.concatenate((matrix, interactions), axis=1)
            probabilities = predict_from_fitted_model(model, matrix)
            if unit.task == "DIRECTION":
                actual = [evidence["class_order"].index(str(item)) for item in permuted.labels]
                actual_array = np.asarray(actual)
                predicted = np.asarray(probabilities).argmax(axis=1)
                recalls = [
                    float(np.mean(predicted[actual_array == index] == index))
                    for index in range(len(evidence["class_order"]))
                    if np.any(actual_array == index)
                ]
                scores.append(sum(recalls) / len(recalls))
                reference = evidence["metrics"].get("balanced_accuracy")
            else:
                scores.append(
                    float(
                        pr_auc(
                            tuple(int(item) for item in permuted.labels),
                            tuple(float(row[1]) for row in probabilities),
                        )
                        or 0.0
                    )
                )
                reference = evidence["metrics"].get("pr_auc")
            if isinstance(reference, (int, float)):
                references.append(float(reference))
        if not scores or not references:
            raise CheckpointError("permutation produced no valid same-model evidence")
        raw_delta = sum(references) / len(references) - sum(scores) / len(scores)
        return {
            "kind": "PERMUTATION_IMPORTANCE",
            "operation": operation,
            "primitive": primitive.__name__,
            "selected_model": selected["model"],
            "permutation_index": unit.permutation_index,
            "held_out_evidence_digest": unit.held_out_evidence_digest,
            "refit": False,
            "raw_metric": sum(scores) / len(scores),
            "raw_delta": raw_delta,
            "horizon": unit.horizon,
            "task": unit.task,
            "feature": unit.feature_exclusion,
            "family": unit.family_exclusion,
        }

    def _contribution_summary(self) -> dict[str, object]:
        return _scoped_contribution_sections(self.root)

    def _bootstrap_summary(self) -> dict[str, object]:
        return {"scopes": _scoped_bootstrap_summaries(self.root)}

    def _multiple_testing_sections(self) -> tuple[dict[str, object], dict[str, object]]:
        return _scoped_multiple_testing(_collect_bootstrap_scopes(self.root))


def _result_payloads(root: Path, phase: ExecutionPhase) -> list[dict[str, object]]:
    return [
        json.loads(path.read_text(encoding="utf-8"))["result"]
        for path in sorted(
            (root / CHECKPOINT_ROOT_NAME / phase.value.lower()).glob("*.result.json")
        )
    ]


def _classification(delta: float) -> str:
    from .tsunami_predictive_research import classify_ablation

    return classify_ablation(delta)


def _method_deltas(
    root: Path, *, entity_field: str, method: str
) -> dict[tuple[str, str, str], float]:
    phase = (
        ExecutionPhase.ABLATION
        if entity_field == "feature" and method == "ABLATION"
        else ExecutionPhase.FAMILY_CONTRIBUTION
        if entity_field == "family" and method == "ABLATION"
        else ExecutionPhase.PERMUTATION_IMPORTANCE
    )
    grouped: dict[tuple[str, str, str], list[float]] = {}
    for result in _result_payloads(root, phase):
        entity = result.get(entity_field)
        if entity is None:
            continue
        if method == "PERMUTATION" and result.get("kind") != "PERMUTATION_IMPORTANCE":
            continue
        key = (str(result["horizon"]), str(result["task"]), str(entity))
        grouped.setdefault(key, []).append(float(result["raw_delta"]))
    if method == "PERMUTATION" and any(
        len(values) != PERMUTATION_REPLICATES for values in grouped.values()
    ):
        raise CheckpointError("contribution aggregation requires exact 20-permutation scopes")
    if method == "ABLATION" and any(len(values) != 1 for values in grouped.values()):
        raise CheckpointError("contribution aggregation requires one ablation per scope")
    return {key: sum(values) / len(values) for key, values in grouped.items()}


def _normalize_method(
    deltas: Mapping[tuple[str, str, str], float], method: str
) -> dict[tuple[str, str, str], dict[str, object]]:
    totals: dict[tuple[str, str], float] = {}
    for (horizon, task, _entity), delta in deltas.items():
        if _classification(delta) == "POSITIVE":
            totals[(horizon, task)] = totals.get((horizon, task), 0.0) + delta
    normalized = {}
    prefix = method.lower()
    for key, delta in deltas.items():
        total = totals.get(key[:2], 0.0)
        eligible = _classification(delta) == "POSITIVE" and total > 0
        normalized[key] = {
            f"{prefix}_raw_delta": delta,
            f"{prefix}_sign": _classification(delta),
            f"{prefix}_normalization_eligible": eligible,
            f"{prefix}_normalized_contribution_pct": 100 * delta / total if eligible else None,
        }
    return normalized


def _entity_contributions(root: Path, entity_field: str) -> list[dict[str, object]]:
    ablation = _normalize_method(
        _method_deltas(root, entity_field=entity_field, method="ABLATION"), "ABLATION"
    )
    permutation = _normalize_method(
        _method_deltas(root, entity_field=entity_field, method="PERMUTATION"), "PERMUTATION"
    )
    keys = sorted(set(ablation) | set(permutation))
    return [
        {
            "horizon": key[0],
            "task": key[1],
            entity_field: key[2],
            **ablation.get(key, {}),
            **permutation.get(key, {}),
        }
        for key in keys
    ]


def _interaction_contributions(root: Path) -> list[dict[str, object]]:
    result = []
    for item in _result_payloads(root, ExecutionPhase.INTERACTION_EVALUATION):
        delta = float(item["raw_delta"])
        result.append(
            {
                "horizon": item["horizon"],
                "task": item["task"],
                "metric": item["metric"],
                "additive_metric": item["additive_metric"],
                "interaction_metric": item["interaction_metric"],
                "incremental_raw_delta": delta,
                "sign": _classification(delta),
                "evidence_digest": item["evidence_digest"],
            }
        )
    return sorted(result, key=lambda item: (str(item["horizon"]), str(item["task"])))


def _scoped_contribution_sections(root: Path) -> dict[str, object]:
    return {
        "individual_feature_contribution": _entity_contributions(root, "feature"),
        "family_contribution": _entity_contributions(root, "family"),
        "interaction_contribution": _interaction_contributions(root),
    }


def _collect_bootstrap_scopes(
    root: Path,
) -> dict[tuple[str, str, str, str], tuple[float, ...]]:
    frozen_tasks = {item.value for item in TASKS}
    selected_bindings = {
        (str(item["horizon"]), str(item["task"])): str(item["selected_prediction_evidence_digest"])
        for item in _result_payloads(root, ExecutionPhase.MODEL_SELECTION)
        if item.get("identity") == "SELECTED_RESEARCH_CONFIGURATION"
    }
    batches: dict[tuple[str, str, str, str], dict[int, tuple[float, ...]]] = {}
    for item in _result_payloads(root, ExecutionPhase.BOOTSTRAP):
        horizon = str(item["horizon"])
        task = str(item["task"])
        metric = str(item["metric"])
        evidence_digest = str(item["prediction_evidence_digest"])
        expected_metric = "balanced_accuracy" if task == "DIRECTION" else "pr_auc"
        if horizon not in HORIZON_NAMES or task not in frozen_tasks or metric != expected_metric:
            raise CheckpointError("bootstrap batch has a wrong horizon, task, or metric scope")
        if len(evidence_digest) != 64:
            raise CheckpointError("bootstrap batch has an invalid evidence digest")
        selected_digest = selected_bindings.get((horizon, task))
        if selected_bindings and selected_digest != evidence_digest:
            raise CheckpointError("bootstrap batch does not bind selected prediction evidence")
        key = (
            horizon,
            task,
            metric,
            evidence_digest,
        )
        start = int(item["batch_start"])
        end = int(item["batch_end_exclusive"])
        values = tuple(float(value) for value in item["replicates"])
        if end - start != BOOTSTRAP_BATCH_SIZE or len(values) != BOOTSTRAP_BATCH_SIZE:
            raise CheckpointError("bootstrap scope contains a malformed 100-replicate batch")
        if start in batches.setdefault(key, {}):
            raise CheckpointError("bootstrap scope contains a duplicate replicate batch")
        batches[key][start] = values
    expected_starts = set(range(0, BOOTSTRAP_REPLICATES, BOOTSTRAP_BATCH_SIZE))
    scopes = {}
    for key, scoped_batches in batches.items():
        if set(scoped_batches) != expected_starts:
            raise CheckpointError("bootstrap scope has missing or unexpected replicate indexes")
        values = tuple(value for start in sorted(scoped_batches) for value in scoped_batches[start])
        if len(values) != BOOTSTRAP_REPLICATES:
            raise CheckpointError("bootstrap scope requires exactly 2,000 replicates")
        scopes[key] = values
    if not scopes:
        raise CheckpointError("bootstrap aggregation requires verified scoped evidence")
    return scopes


def _scoped_bootstrap_summaries(root: Path) -> list[dict[str, object]]:
    summaries = []
    for key, values in sorted(_collect_bootstrap_scopes(root).items()):
        ordered = sorted(values)
        summaries.append(
            {
                "horizon": key[0],
                "task": key[1],
                "metric": key[2],
                "evidence_digest": key[3],
                "point_estimate": sum(values) / len(values),
                "lower_95": ordered[int(0.025 * (len(ordered) - 1))],
                "upper_95": ordered[int(0.975 * (len(ordered) - 1))],
                "replicate_count": len(values),
            }
        )
    return summaries


def _adjusted_hypotheses(
    hypotheses: list[dict[str, object]], family: str
) -> list[dict[str, object]]:
    ordered = sorted(
        hypotheses, key=lambda item: (float(item["raw_p_value"]), str(item["hypothesis_id"]))
    )
    count = len(ordered)
    adjusted: dict[str, float] = {}
    if family == "CONFIRMATORY_HOLM":
        running = 0.0
        for rank, item in enumerate(ordered, 1):
            running = max(running, min(1.0, (count - rank + 1) * float(item["raw_p_value"])))
            adjusted[str(item["hypothesis_id"])] = running
        threshold = 0.05
    else:
        running = 1.0
        for rank, item in reversed(list(enumerate(ordered, 1))):
            running = min(running, min(1.0, count * float(item["raw_p_value"]) / rank))
            adjusted[str(item["hypothesis_id"])] = running
        threshold = 0.10
    return [
        {
            **item,
            "testing_family": family,
            "adjusted_p_value": adjusted[str(item["hypothesis_id"])],
            "rejected": adjusted[str(item["hypothesis_id"])] <= threshold,
            "alpha" if family == "CONFIRMATORY_HOLM" else "q": threshold,
        }
        for item in sorted(hypotheses, key=lambda item: str(item["hypothesis_id"]))
    ]


def _scoped_multiple_testing(
    scopes: Mapping[tuple[str, str, str, str], tuple[float, ...]],
) -> tuple[dict[str, object], dict[str, object]]:
    hypotheses = []
    for (horizon, task, metric, evidence_digest), values in sorted(scopes.items()):
        hypothesis_id = f"{horizon}/{task}/{metric}/{evidence_digest}"
        hypotheses.append(
            {
                "hypothesis_id": hypothesis_id,
                "scope": f"{horizon}/{task}",
                "horizon": horizon,
                "task": task,
                "metric": metric,
                "raw_p_value": (sum(value <= 0 for value in values) + 1) / (len(values) + 1),
                "evidence_digest": evidence_digest,
            }
        )
    confirmatory = [
        item
        for item in hypotheses
        if (item["horizon"], item["task"], item["metric"])
        in {("24h", "TSUNAMI", "pr_auc"), ("24h", "DIRECTION", "balanced_accuracy")}
    ]
    expected_confirmatory = {
        ("24h", "TSUNAMI", "pr_auc"),
        ("24h", "DIRECTION", "balanced_accuracy"),
    }
    if (
        len(confirmatory) != 2
        or {(str(item["horizon"]), str(item["task"]), str(item["metric"])) for item in confirmatory}
        != expected_confirmatory
    ):
        raise CheckpointError("Holm family requires exactly two frozen 24h hypotheses")
    confirmatory_ids = {str(item["hypothesis_id"]) for item in confirmatory}
    exploratory = [
        item for item in hypotheses if str(item["hypothesis_id"]) not in confirmatory_ids
    ]
    return (
        {
            "family": "CONFIRMATORY_HOLM",
            "alpha": 0.05,
            "hypotheses": _adjusted_hypotheses(confirmatory, "CONFIRMATORY_HOLM"),
        },
        {
            "family": "EXPLORATORY_BH",
            "q": 0.10,
            "hypotheses": _adjusted_hypotheses(exploratory, "EXPLORATORY_BH"),
        },
    )


def _verified_v2_evidence(
    root: Path, resolvers: Sequence[tuple[ExecutionPhase, PhaseResolverV2]]
) -> tuple[dict[str, str], tuple[GraphWorkUnitV2, ...]]:
    evidence: dict[str, str] = {}
    units: list[GraphWorkUnitV2] = []
    for phase, resolver in resolvers:
        resolved = tuple(resolver(evidence))
        if any(
            not isinstance(item, GraphWorkUnitV2) or item.phase is not phase for item in resolved
        ):
            raise CheckpointError("invalid production V2 phase resolution")
        inventory = checkpoint_inventory(root, resolved)  # type: ignore[arg-type]
        if any(item.status != "REUSED" for item in inventory):
            raise CheckpointError(f"{phase.value} is not completely VERIFIED")
        for unit in resolved:
            evidence[unit.identity] = _artifact_semantic_digest(root, unit)
            units.append(unit)
    return evidence, tuple(units)


def publish_final_v2(graph: ProductionGraphV2) -> dict[str, object]:
    """Publish verified V2 Research result and freeze, then manifest last."""
    root = graph.root
    manifest_path = root / "manifest.json"
    if manifest_path.exists():
        raise CheckpointError(
            "authoritative Research manifest already exists; publication is create-only"
        )
    evidence, units = _verified_v2_evidence(root, graph.resolvers())
    selections = []
    for unit in units:
        if unit.phase is ExecutionPhase.MODEL_SELECTION:
            selections.append(graph._artifact(evidence[unit.identity])["result"])
    contributions = _scoped_contribution_sections(root)
    if len(contributions["individual_feature_contribution"]) != len(HORIZON_NAMES) * len(
        TASKS
    ) * len(FEATURE_ORDER):
        raise CheckpointError("V2 final assembly requires every scoped feature contribution")
    if any(
        not {
            "permutation_raw_delta",
            "permutation_sign",
            "permutation_normalization_eligible",
            "permutation_normalized_contribution_pct",
            "ablation_raw_delta",
            "ablation_sign",
            "ablation_normalization_eligible",
            "ablation_normalized_contribution_pct",
        }.issubset(item)
        for item in contributions["individual_feature_contribution"]
    ):
        raise CheckpointError("V2 feature contribution is missing a frozen method")
    if len(contributions["family_contribution"]) != len(HORIZON_NAMES) * len(TASKS) * len(
        FEATURE_FAMILY_MEMBERSHIP
    ):
        raise CheckpointError("V2 final assembly requires every scoped family contribution")
    if any(
        not {
            "permutation_raw_delta",
            "permutation_sign",
            "permutation_normalization_eligible",
            "permutation_normalized_contribution_pct",
            "ablation_raw_delta",
            "ablation_sign",
            "ablation_normalization_eligible",
            "ablation_normalized_contribution_pct",
        }.issubset(item)
        for item in contributions["family_contribution"]
    ):
        raise CheckpointError("V2 family contribution is missing a frozen method")
    if len(contributions["interaction_contribution"]) != len(HORIZON_NAMES) * len(TASKS):
        raise CheckpointError("V2 final assembly requires every scoped interaction contribution")
    bootstrap_scopes = _collect_bootstrap_scopes(root)
    uncertainty = _scoped_bootstrap_summaries(root)
    if len(uncertainty) != len(HORIZON_NAMES) * len(TASKS):
        raise CheckpointError("V2 final assembly requires every scoped bootstrap outcome")
    expected_outcomes = {(horizon, task.value) for horizon in HORIZON_NAMES for task in TASKS}
    if {(str(item["horizon"]), str(item["task"])) for item in uncertainty} != expected_outcomes:
        raise CheckpointError("V2 bootstrap scopes do not match the frozen outcome catalogue")
    confirmatory, exploratory = _scoped_multiple_testing(bootstrap_scopes)
    assembled = {
        "identity": RESULT_IDENTITY,
        "research_identity": RESEARCH_IDENTITY,
        "execution_graph_identity": EXECUTION_GRAPH_IDENTITY,
        "execution_binding_digest": EXECUTION_BINDING_DIGEST,
        "selection_evidence": selections,
        **contributions,
        "bootstrap_uncertainty": uncertainty,
        "confirmatory_multiple_testing": confirmatory,
        "exploratory_multiple_testing": exploratory,
        "verified_work_unit_count": len(units),
        "verified_evidence_set_digest": _digest(evidence),
        "validation_accessed": False,
    }
    assembled["result_semantic_digest"] = _digest(assembled)
    result_path = root / "result.json"
    _atomic_write(result_path, assembled)
    if json.loads(result_path.read_text(encoding="utf-8")) != assembled:
        raise CheckpointError("V2 Research result reload verification failed")
    freeze = {
        "identity": VALIDATION_FREEZE_IDENTITY,
        "research_identity": RESEARCH_IDENTITY,
        "execution_graph_identity": EXECUTION_GRAPH_IDENTITY,
        "result_semantic_digest": assembled["result_semantic_digest"],
        "selected_configurations": selections,
        "validation_accessed": False,
    }
    freeze["freeze_semantic_digest"] = _digest(freeze)
    freeze_path = root / "validation_candidate_freeze.json"
    _atomic_write(freeze_path, freeze)
    if json.loads(freeze_path.read_text(encoding="utf-8")) != freeze:
        raise CheckpointError("V2 Validation Candidate Freeze reload verification failed")
    manifest = {
        "identity": RESULT_IDENTITY,
        "research_identity": RESEARCH_IDENTITY,
        "execution_graph_identity": EXECUTION_GRAPH_IDENTITY,
        "result_semantic_digest": assembled["result_semantic_digest"],
        "freeze_semantic_digest": freeze["freeze_semantic_digest"],
        "manifest_last": True,
    }
    manifest["manifest_semantic_digest"] = _digest(manifest)
    _atomic_write(manifest_path, manifest)
    if json.loads(manifest_path.read_text(encoding="utf-8")) != manifest:
        raise CheckpointError("V2 authoritative manifest reload verification failed")
    return manifest


def execute_production_v2(
    root: Path,
    analytical_root: Path,
    target_root: Path,
    *,
    emit: Callable[[str], None] = print,
    max_workers: int = 1,
) -> dict[str, object]:
    import gc

    graph = ProductionGraphV2(root, analytical_root, target_root)

    def memory_bounded_dispatch(
        unit: V2Unit, heartbeat: Callable[[], None]
    ) -> Mapping[str, object]:
        try:
            return graph.dispatch(unit, heartbeat)
        finally:
            if unit.phase in {
                ExecutionPhase.ABLATION,
                ExecutionPhase.FAMILY_CONTRIBUTION,
                ExecutionPhase.INTERACTION_EVALUATION,
            }:
                graph.clear_fold_cache()
                gc.collect()

    def workers_for_phase(phase: ExecutionPhase, workers: int) -> int:
        return (
            1
            if phase
            in {
                ExecutionPhase.ABLATION,
                ExecutionPhase.FAMILY_CONTRIBUTION,
                ExecutionPhase.INTERACTION_EVALUATION,
            }
            else workers
        )

    execution = execute_phased_v2(
        root,
        graph.resolvers(),
        memory_bounded_dispatch,
        emit=emit,
        max_workers=max_workers,
        workers_for_phase=workers_for_phase,
    )
    manifest = publish_final_v2(graph)
    return {"execution": execution, "manifest": manifest}


def production_status_v2(root: Path, analytical_root: Path, target_root: Path) -> dict[str, object]:
    graph = ProductionGraphV2(root, analytical_root, target_root)
    result = phased_status_v2(root, graph.resolvers())
    result["execution_compatible"] = True
    return result


def production_work_units() -> tuple[WorkUnitV1, ...]:
    """Return the immutable unit graph; order is the execution order."""
    units: list[WorkUnitV1] = [WorkUnitV1(ExecutionPhase.INPUT_VERIFY)]
    for horizon in HORIZON_NAMES:
        for task in TASKS:
            for fold in WALK_FORWARD_FOLDS:
                for baseline in MODEL_CATALOGUE[:4]:
                    units.append(
                        WorkUnitV1(
                            ExecutionPhase.BASELINES,
                            horizon=horizon,
                            task=task.value,
                            fold=fold.identity,
                            model=baseline.value,
                            seed=RANDOM_SEEDS[0],
                        )
                    )
                for model in MODEL_CATALOGUE:
                    units.append(
                        WorkUnitV1(
                            ExecutionPhase.BASE_MODEL_FITS,
                            horizon=horizon,
                            task=task.value,
                            fold=fold.identity,
                            model=model.value,
                            seed=RANDOM_SEEDS[0],
                        )
                    )
                    units.append(
                        WorkUnitV1(
                            ExecutionPhase.CANDIDATE_EVALUATION,
                            horizon=horizon,
                            task=task.value,
                            fold=fold.identity,
                            model=model.value,
                            seed=RANDOM_SEEDS[0],
                        )
                    )
    units.append(WorkUnitV1(ExecutionPhase.MODEL_SELECTION, horizon="24h"))
    for feature in FEATURE_ORDER:
        units.append(WorkUnitV1(ExecutionPhase.ABLATION, horizon="24h", feature_exclusion=feature))
        for permutation in range(PERMUTATION_REPLICATES):
            units.append(
                WorkUnitV1(
                    ExecutionPhase.PERMUTATION_IMPORTANCE,
                    horizon="24h",
                    feature_exclusion=feature,
                    permutation_index=permutation,
                    seed=RANDOM_SEEDS[0] + permutation,
                )
            )
    for family, _ in FEATURE_FAMILY_MEMBERSHIP:
        units.append(
            WorkUnitV1(
                ExecutionPhase.FAMILY_CONTRIBUTION,
                horizon="24h",
                family_exclusion=family,
            )
        )
    units.append(WorkUnitV1(ExecutionPhase.INTERACTION_EVALUATION, horizon="24h"))
    for start in range(0, BOOTSTRAP_REPLICATES, BOOTSTRAP_BATCH_SIZE):
        units.append(
            WorkUnitV1(
                ExecutionPhase.BOOTSTRAP,
                horizon="24h",
                bootstrap_start=start,
                bootstrap_end_exclusive=min(start + BOOTSTRAP_BATCH_SIZE, BOOTSTRAP_REPLICATES),
                seed=RANDOM_SEEDS[1],
            )
        )
    units.extend(
        (
            WorkUnitV1(ExecutionPhase.MULTIPLE_TESTING, horizon="24h"),
            WorkUnitV1(ExecutionPhase.VALIDATION_CANDIDATE_FREEZE),
            WorkUnitV1(ExecutionPhase.FINAL_VERIFY),
            WorkUnitV1(ExecutionPhase.MANIFEST),
        )
    )
    identities = [item.identity for item in units]
    if len(identities) != len(set(identities)):
        raise RuntimeError("duplicate production work-unit identity")
    return tuple(units)


@dataclass(frozen=True, slots=True)
class CheckpointStateV1:
    unit: WorkUnitV1
    status: str
    path: Path
    reason: str | None = None


class CheckpointError(RuntimeError):
    pass


@dataclass(slots=True)
class ArtifactIndexV1:
    """Read-only, process-local lookup index for verified result artifacts."""

    root: Path
    by_digest: dict[str, Path] = field(default_factory=dict)
    by_unit_identity: dict[str, Path] = field(default_factory=dict)
    _payload_cache: OrderedDict[str, dict[str, object]] = field(default_factory=OrderedDict)
    max_cached_payloads: int = 12

    @classmethod
    def build(cls, root: Path) -> Self:
        index = cls(root)
        for path in sorted((root / CHECKPOINT_ROOT_NAME).glob("*/*.checkpoint.json")):
            checkpoint = json.loads(path.read_text(encoding="utf-8"))
            if checkpoint.get("completion_state") != "VERIFIED":
                continue
            digest = checkpoint.get("result_semantic_digest")
            identity = checkpoint.get("work_unit_identity")
            relative = checkpoint.get("artifact_relative_path")
            if (
                not isinstance(digest, str)
                or not isinstance(identity, str)
                or not isinstance(relative, str)
            ):
                continue
            artifact = root / Path(relative)
            index.by_digest[digest] = artifact
            index.by_unit_identity[identity] = artifact
        return index

    def artifact(self, digest: str) -> dict[str, object] | None:
        cached = self._payload_cache.get(digest)
        if cached is not None:
            self._payload_cache.move_to_end(digest)
            return cached
        path = self.by_digest.get(digest)
        if path is None:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if _digest(payload) != digest:
            raise CheckpointError(f"indexed artifact semantic digest mismatch at {path}")
        self._payload_cache[digest] = payload
        if len(self._payload_cache) > self.max_cached_payloads:
            self._payload_cache.popitem(last=False)
        return payload


class ProductionLock:
    def __init__(self, root: Path) -> None:
        self.path = root / LOCK_NAME
        self._held = False

    def __enter__(self) -> Self:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise CheckpointError(
                f"production lock exists at {self.path}; inspect owner and remove manually only if stale"
            ) from exc
        try:
            os.write(
                descriptor,
                _canonical(
                    {
                        "execution_identity": EXECUTION_IDENTITY,
                        "pid": os.getpid(),
                        "semantic_note": "pid is operational metadata, not semantic identity",
                    },
                    pretty=True,
                ),
            )
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self._held = True
        return self

    def __exit__(self, *_: object) -> None:
        if self._held:
            self.path.unlink()
            self._held = False


def _paths(root: Path, unit: WorkUnitV1) -> tuple[Path, Path, Path]:
    directory = root / CHECKPOINT_ROOT_NAME / unit.phase.value.lower()
    return (
        directory / f"{unit.identity}.result.json",
        directory / f"{unit.identity}.checkpoint.json",
        directory / f"{unit.identity}.pending",
    )


def _atomic_write(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(path.suffix + ".pending")
    with pending.open("wb") as handle:
        handle.write(_canonical(payload, pretty=True))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(pending, path)


def verify_checkpoint(root: Path, unit: WorkUnitV1) -> CheckpointStateV1:
    artifact, checkpoint, pending = _paths(root, unit)
    if pending.exists() or artifact.with_suffix(artifact.suffix + ".pending").exists():
        return CheckpointStateV1(unit, "INVALID", pending, "non-authoritative partial artifact")
    if not checkpoint.exists() and not artifact.exists():
        return CheckpointStateV1(unit, "PENDING", checkpoint)
    if not checkpoint.exists() or not artifact.exists():
        return CheckpointStateV1(unit, "INVALID", checkpoint, "checkpoint/artifact pair incomplete")
    try:
        result = json.loads(artifact.read_text(encoding="utf-8"))
        saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return CheckpointStateV1(unit, "INVALID", checkpoint, str(exc))
    expected = {
        "schema": CHECKPOINT_SCHEMA,
        "execution_binding_digest": EXECUTION_BINDING_DIGEST,
        "work_unit": unit.payload(),
        "work_unit_identity": unit.identity,
        "artifact_relative_path": artifact.relative_to(root).as_posix(),
        "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "result_semantic_digest": _digest(result),
        "completion_state": "VERIFIED",
    }
    if saved != expected or result.get("schema") != ARTIFACT_SCHEMA:
        return CheckpointStateV1(unit, "INVALID", checkpoint, "binding or digest mismatch")
    return CheckpointStateV1(unit, "REUSED", checkpoint)


def write_verified_checkpoint(
    root: Path, unit: WorkUnitV1, result: Mapping[str, object]
) -> CheckpointStateV1:
    existing = verify_checkpoint(root, unit)
    if existing.status == "REUSED":
        return existing
    if existing.status == "INVALID":
        raise CheckpointError(
            f"invalid checkpoint for {unit.identity} at {existing.path}; preserve it and recover manually"
        )
    artifact, checkpoint, _ = _paths(root, unit)
    payload = {
        "schema": ARTIFACT_SCHEMA,
        "execution_binding_digest": EXECUTION_BINDING_DIGEST,
        "work_unit": unit.payload(),
        "result": dict(result),
    }
    if isinstance(unit, GraphWorkUnitV2):
        payload = json.loads(_canonical(payload))
    _atomic_write(artifact, payload)
    reloaded = json.loads(artifact.read_text(encoding="utf-8"))
    if reloaded != payload:
        raise CheckpointError(f"artifact reload verification failed for {unit.identity}")
    checkpoint_payload = {
        "schema": CHECKPOINT_SCHEMA,
        "execution_binding_digest": EXECUTION_BINDING_DIGEST,
        "work_unit": unit.payload(),
        "work_unit_identity": unit.identity,
        "artifact_relative_path": artifact.relative_to(root).as_posix(),
        "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "result_semantic_digest": _digest(payload),
        "completion_state": "VERIFIED",
    }
    _atomic_write(checkpoint, checkpoint_payload)
    verified = verify_checkpoint(root, unit)
    if verified.status != "REUSED":
        raise CheckpointError(f"checkpoint reload verification failed for {unit.identity}")
    return CheckpointStateV1(unit, "CALCULATED", checkpoint)


def checkpoint_inventory(root: Path, units: Sequence[WorkUnitV1]) -> tuple[CheckpointStateV1, ...]:
    return tuple(verify_checkpoint(root, unit) for unit in units)


def _duration(seconds: float) -> str:
    return str(timedelta(seconds=int(seconds)))


def format_progress(
    unit: WorkUnitV1,
    status: str,
    completed: int,
    total: int,
    elapsed: float,
    throughput: float | None,
) -> str:
    fields: dict[str, object] = {
        "phase": unit.phase.value,
        "horizon": unit.horizon or "-",
        "task": unit.task or "-",
        "fold": unit.fold or "-",
        "model": unit.model or "-",
        "status": status,
        "units": f"{completed}/{total}",
        "percentage": f"{100 * completed / total:.3f}%",
        "elapsed": _duration(elapsed),
    }
    if throughput is not None:
        fields["REAL_RESEARCH_THROUGHPUT"] = f"{throughput:.6f}_units_per_second"
        fields["REAL_RESEARCH_ETA"] = f"approx_{_duration((total - completed) / throughput)}"
    return " ".join(f"{key}={value}" for key, value in fields.items())


Calculator = Callable[[WorkUnitV1, Callable[[], None]], Mapping[str, object]]
V2Unit = WorkUnitV1 | GraphWorkUnitV2
V2Calculator = Callable[[V2Unit, Callable[[], None]], Mapping[str, object]]
PhaseResolverV2 = Callable[[Mapping[str, str]], Sequence[V2Unit]]
PHASE_STATE_NAME = "staging/phase_state.json"


def _artifact_semantic_digest(root: Path, unit: V2Unit) -> str:
    artifact, _, _ = _paths(root, unit)  # type: ignore[arg-type]
    return _digest(json.loads(artifact.read_text(encoding="utf-8")))


def _execute_resolved_phase(
    root: Path,
    units: Sequence[V2Unit],
    calculator: V2Calculator,
    *,
    completed_before: int,
    estimated_total: int | None,
    started: float,
    emit: Callable[[str], None],
    monotonic: Callable[[], float],
    max_workers: int = 1,
) -> int:
    if not units:
        raise CheckpointError("resolved phase must contain at least one work unit")
    if len({item.identity for item in units}) != len(units):
        raise CheckpointError("resolved phase contains duplicate work-unit identities")
    inventory = checkpoint_inventory(root, units)  # type: ignore[arg-type]
    invalid = [item for item in inventory if item.status == "INVALID"]
    if invalid:
        item = invalid[0]
        raise CheckpointError(
            f"invalid checkpoint for {item.unit.identity} at {item.path}; manual recovery required"
        )
    if max_workers < 1:
        raise ValueError("max_workers must be positive")
    completed = completed_before
    phase_total = len(units)
    pending: list[tuple[int, V2Unit]] = []
    for phase_index, (unit, state) in enumerate(zip(units, inventory, strict=True), 1):
        if state.status == "REUSED":
            completed += 1
            emit(
                format_progress(
                    unit,  # type: ignore[arg-type]
                    "REUSED",
                    phase_index,
                    phase_total,
                    monotonic() - started,
                    None,
                )
            )
            continue
        pending.append((phase_index, unit))

    if max_workers == 1:
        for phase_index, unit in pending:
            last_heartbeat = monotonic()

            def heartbeat(
                current_unit: V2Unit = unit, current_phase_index: int = phase_index
            ) -> None:
                nonlocal last_heartbeat
                now = monotonic()
                if now - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
                    emit(
                        format_progress(
                            current_unit,  # type: ignore[arg-type]
                            "HEARTBEAT",
                            current_phase_index - 1,
                            phase_total,
                            now - started,
                            None,
                        )
                    )
                    last_heartbeat = now

            result = calculator(unit, heartbeat)
            if not result:
                raise CheckpointError(f"calculator returned empty evidence for {unit.identity}")
            write_verified_checkpoint(root, unit, result)  # type: ignore[arg-type]
            completed += 1
            elapsed = monotonic() - started
            throughput = completed / elapsed if completed >= 2 and elapsed > 0 else None
            emit(
                format_progress(
                    unit,  # type: ignore[arg-type]
                    "CALCULATED",
                    phase_index,
                    phase_total,
                    elapsed,
                    throughput if estimated_total is not None else None,
                )
            )
        return completed
    else:
        # Results are committed atomically as workers finish so an interrupted
        # batch retains every completed unit. Progress output is deferred to the
        # frozen order; artifact bytes are independent of completion order.
        calculated: dict[int, Mapping[str, object]] = {}

        def calculate(unit: V2Unit) -> Mapping[str, object]:
            return calculator(unit, lambda: None)

        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="tsunami") as pool:
            futures = {
                pool.submit(calculate, unit): (phase_index, unit) for phase_index, unit in pending
            }
            for future in as_completed(futures):
                phase_index, unit = futures[future]
                result = future.result()
                if not result:
                    raise CheckpointError(f"calculator returned empty evidence for {unit.identity}")
                write_verified_checkpoint(root, unit, result)  # type: ignore[arg-type]
                calculated[phase_index] = result

    for phase_index, unit in pending:
        result = calculated[phase_index]
        if not result:
            raise CheckpointError(f"calculator returned empty evidence for {unit.identity}")
        completed += 1
        elapsed = monotonic() - started
        throughput = completed / elapsed if completed >= 2 and elapsed > 0 else None
        emit(
            format_progress(
                unit,  # type: ignore[arg-type]
                "CALCULATED",
                phase_index,
                phase_total,
                elapsed,
                throughput if estimated_total is not None else None,
            )
        )
    return completed


def execute_phased_v2(
    root: Path,
    phase_resolvers: Sequence[tuple[ExecutionPhase, PhaseResolverV2]],
    calculator: V2Calculator,
    *,
    estimated_total_units: int | None = None,
    emit: Callable[[str], None] = print,
    monotonic: Callable[[], float] = time.monotonic,
    max_workers: int = 1,
    workers_for_phase: Callable[[ExecutionPhase, int], int] | None = None,
) -> dict[str, object]:
    """Resolve and execute V2 phases only after actual upstream evidence verifies."""
    if (root / "manifest.json").exists():
        raise CheckpointError(
            "authoritative Research manifest already exists; execution is create-only"
        )
    if not phase_resolvers:
        raise ValueError("phased execution requires resolvers")
    started = monotonic()
    evidence: dict[str, str] = {}
    completed = 0
    completed_phases: list[str] = []
    with ProductionLock(root):
        for expected_phase, resolver in phase_resolvers:
            units = tuple(resolver(dict(evidence)))
            if any(unit.phase is not expected_phase for unit in units):
                raise CheckpointError("phase resolver returned a unit from the wrong phase")
            completed = _execute_resolved_phase(
                root,
                units,
                calculator,
                completed_before=completed,
                estimated_total=estimated_total_units,
                started=started,
                emit=emit,
                monotonic=monotonic,
                max_workers=(
                    workers_for_phase(expected_phase, max_workers)
                    if workers_for_phase is not None
                    else max_workers
                ),
            )
            phase_inventory = checkpoint_inventory(root, units)  # type: ignore[arg-type]
            if any(item.status != "REUSED" for item in phase_inventory):
                raise CheckpointError("phase may advance only after every unit is VERIFIED")
            for unit in units:
                evidence[unit.identity] = _artifact_semantic_digest(root, unit)
            completed_phases.append(expected_phase.value)
            _atomic_write(
                root / PHASE_STATE_NAME,
                {
                    "execution_graph_identity": EXECUTION_GRAPH_IDENTITY,
                    "execution_binding_digest": EXECUTION_BINDING_DIGEST,
                    "last_completed_phase": expected_phase.value,
                    "completed_phases": completed_phases,
                    "verified_evidence_count": len(evidence),
                },
            )
    return {
        "execution_graph_identity": EXECUTION_GRAPH_IDENTITY,
        "completed_phases": completed_phases,
        "verified_work_units": completed,
        "state": "PHASES_VERIFIED",
    }


def phased_status_v2(
    root: Path, phase_resolvers: Sequence[tuple[ExecutionPhase, PhaseResolverV2]]
) -> dict[str, object]:
    """Resolve phased status from verified evidence without locks or calculations."""
    evidence: dict[str, str] = {}
    completed_phases: list[str] = []
    current_phase: str | None = None
    current_counts = {"verified": 0, "pending": 0, "invalid": 0}
    for expected_phase, resolver in phase_resolvers:
        try:
            units = tuple(resolver(dict(evidence)))
        except CheckpointError:
            current_phase = expected_phase.value
            break
        if not units or any(unit.phase is not expected_phase for unit in units):
            current_phase = expected_phase.value
            break
        inventory = checkpoint_inventory(root, units)  # type: ignore[arg-type]
        current_counts = {
            "verified": sum(item.status == "REUSED" for item in inventory),
            "pending": sum(item.status == "PENDING" for item in inventory),
            "invalid": sum(item.status == "INVALID" for item in inventory),
        }
        if current_counts["invalid"] or current_counts["pending"]:
            current_phase = expected_phase.value
            break
        completed_phases.append(expected_phase.value)
        for unit in units:
            evidence[unit.identity] = _artifact_semantic_digest(root, unit)
    manifest = root / "manifest.json"
    return {
        "execution_graph_identity": EXECUTION_GRAPH_IDENTITY,
        "completed_phases": completed_phases,
        "current_phase": current_phase,
        "current_phase_counts": current_counts,
        "verified_evidence_count": len(evidence),
        "lock": "PRESENT_ACTIVE_OR_STALE_REQUIRES_MANUAL_INSPECTION"
        if (root / LOCK_NAME).exists()
        else "ABSENT",
        "final_manifest_present": manifest.exists(),
        "result_state": "AUTHORITATIVE" if manifest.exists() else "INCOMPLETE",
    }


def execute(
    root: Path,
    calculator: Calculator,
    *,
    units: Sequence[WorkUnitV1] | None = None,
    emit: Callable[[str], None] = print,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, object]:
    """Execute or resume immutable units, reusing verified evidence only."""
    plan = tuple(units or production_work_units())
    if (root / "manifest.json").exists():
        raise CheckpointError(
            "authoritative Research manifest already exists; execution is create-only"
        )
    started = monotonic()
    completed = 0
    with ProductionLock(root):
        inventory = checkpoint_inventory(root, plan)
        invalid = [item for item in inventory if item.status == "INVALID"]
        if invalid:
            item = invalid[0]
            raise CheckpointError(
                f"invalid checkpoint for {item.unit.identity} at {item.path}; manual recovery required"
            )
        for unit, state in zip(plan, inventory, strict=True):
            if state.status == "REUSED":
                completed += 1
                elapsed = monotonic() - started
                throughput = completed / elapsed if completed >= 2 and elapsed > 0 else None
                emit(format_progress(unit, "REUSED", completed, len(plan), elapsed, throughput))
                continue
            last_heartbeat = monotonic()

            def heartbeat(
                current_unit: WorkUnitV1 = unit, completed_before: int = completed
            ) -> None:
                nonlocal last_heartbeat
                now = monotonic()
                if now - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
                    emit(
                        format_progress(
                            current_unit,
                            "HEARTBEAT",
                            completed_before,
                            len(plan),
                            now - started,
                            None,
                        )
                    )
                    last_heartbeat = now

            result = calculator(unit, heartbeat)
            write_verified_checkpoint(root, unit, result)
            completed += 1
            elapsed = monotonic() - started
            throughput = completed / elapsed if completed >= 2 and elapsed > 0 else None
            emit(format_progress(unit, "CALCULATED", completed, len(plan), elapsed, throughput))
    return {
        "execution_identity": EXECUTION_IDENTITY,
        "completed_units": completed,
        "total_units": len(plan),
        "state": "CHECKPOINTS_VERIFIED",
    }


def status(root: Path, units: Sequence[WorkUnitV1] | None = None) -> dict[str, object]:
    """Inspect execution state without acquiring a writer lock or calculating Research."""
    plan = tuple(units or production_work_units())
    inventory = checkpoint_inventory(root, plan)
    counts = {
        name: sum(item.status == name for item in inventory)
        for name in ("REUSED", "PENDING", "INVALID")
    }
    phases = {
        phase.value: {
            name.lower(): sum(
                item.unit.phase is phase and item.status == name for item in inventory
            )
            for name in counts
        }
        for phase in PHASES
    }
    lock = root / LOCK_NAME
    manifest = root / "manifest.json"
    return {
        "execution_identity": EXECUTION_IDENTITY,
        "execution_binding_digest": EXECUTION_BINDING_DIGEST,
        "compatible": True,
        "lock": "PRESENT_ACTIVE_OR_STALE_REQUIRES_MANUAL_INSPECTION" if lock.exists() else "ABSENT",
        "phase_states": phases,
        "verified_work_units": counts["REUSED"],
        "pending_work_units": counts["PENDING"],
        "invalid_work_units": counts["INVALID"],
        "percentage": 100 * counts["REUSED"] / len(plan),
        "final_manifest_present": manifest.exists(),
        "result_state": "AUTHORITATIVE" if manifest.exists() else "INCOMPLETE",
    }


def assemble_from_checkpoints(
    root: Path, units: Sequence[WorkUnitV1] | None = None
) -> dict[str, object]:
    """Assemble deterministic evidence only after every required unit verifies."""
    plan = tuple(units or production_work_units())
    inventory = checkpoint_inventory(root, plan)
    if any(item.status != "REUSED" for item in inventory):
        raise CheckpointError("final assembly requires every work unit to be VERIFIED")
    evidence = []
    for unit in plan:
        artifact, _, _ = _paths(root, unit)
        evidence.append(
            {
                "work_unit_identity": unit.identity,
                "result_semantic_digest": _digest(json.loads(artifact.read_text(encoding="utf-8"))),
            }
        )
    return {
        "identity": RESULT_IDENTITY,
        "execution_identity": EXECUTION_IDENTITY,
        "execution_binding_digest": EXECUTION_BINDING_DIGEST,
        "checkpoint_evidence": evidence,
        "validation_freeze_identity": VALIDATION_FREEZE_IDENTITY,
        "validation_status": "NOT_ACCESSED",
    }


def publish_final_from_checkpoints(
    root: Path, units: Sequence[WorkUnitV1] | None = None
) -> dict[str, object]:
    """Publish result, then verified freeze, then authoritative manifest last."""
    manifest_path = root / "manifest.json"
    if manifest_path.exists():
        raise CheckpointError(
            "authoritative Research manifest already exists; publication is create-only"
        )
    plan = tuple(units or production_work_units())
    assembled = assemble_from_checkpoints(root, plan)
    selection_units = [item for item in plan if item.phase is ExecutionPhase.MODEL_SELECTION]
    required_keys = {(horizon, task.value) for horizon in HORIZON_NAMES for task in TASKS}
    actual_keys = {(item.horizon, item.task) for item in selection_units}
    if actual_keys != required_keys:
        raise CheckpointError("final publication requires the complete horizon/task selection set")
    selection_payloads = []
    for selection_unit in sorted(
        selection_units, key=lambda item: (item.horizon or "", item.task or "")
    ):
        selection_artifact, _, _ = _paths(root, selection_unit)
        selection_payloads.append(json.loads(selection_artifact.read_text(encoding="utf-8")))
    result_path = root / "result.json"
    freeze_path = root / "validation_candidate_freeze.json"
    if result_path.exists() or freeze_path.exists():
        raise CheckpointError("partial final publication exists; preserve and recover manually")
    result_payload = {
        **assembled,
        "research_contract_digest": RESEARCH_CONTRACT_DIGEST,
        "result_semantic_digest": _digest(assembled),
    }
    _atomic_write(result_path, result_payload)
    if json.loads(result_path.read_text(encoding="utf-8")) != result_payload:
        raise CheckpointError("final Research result reload verification failed")
    freeze_payload = {
        "identity": VALIDATION_FREEZE_IDENTITY,
        "research_identity": RESEARCH_IDENTITY,
        "execution_identity": EXECUTION_IDENTITY,
        "feature_order": list(FEATURE_ORDER),
        "horizons": list(HORIZON_NAMES),
        "analytical_semantic_digest": ANALYTICAL_SEMANTIC_DIGEST,
        "target_semantic_digest": TARGET_SEMANTIC_DIGEST,
        "target_manifest_semantic_digest": TARGET_MANIFEST_SEMANTIC_DIGEST,
        "selection_evidence": selection_payloads,
    }
    freeze_payload["freeze_semantic_digest"] = _digest(freeze_payload)
    _atomic_write(freeze_path, freeze_payload)
    if json.loads(freeze_path.read_text(encoding="utf-8")) != freeze_payload:
        raise CheckpointError("Validation candidate freeze reload verification failed")
    manifest = {
        "identity": RESULT_IDENTITY,
        "execution_identity": EXECUTION_IDENTITY,
        "execution_binding_digest": EXECUTION_BINDING_DIGEST,
        "result_sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
        "freeze_sha256": hashlib.sha256(freeze_path.read_bytes()).hexdigest(),
        "files": ["result.json", "validation_candidate_freeze.json"],
    }
    manifest["manifest_semantic_digest"] = _digest(manifest)
    _atomic_write(manifest_path, manifest)
    reloaded = json.loads(manifest_path.read_text(encoding="utf-8"))
    if reloaded != manifest:
        raise CheckpointError("authoritative manifest reload verification failed")
    return manifest
