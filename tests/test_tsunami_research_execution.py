from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.research.capital_flow.tsunami_predictive_research import (
    ANALYTICAL_SEMANTIC_DIGEST,
    RESEARCH_IDENTITY,
    TARGET_MANIFEST_SEMANTIC_DIGEST,
    TARGET_SEMANTIC_DIGEST,
)
from app.research.capital_flow.tsunami_research_execution import (
    BOOTSTRAP_BATCH_SIZE,
    CHECKPOINT_ROOT_NAME,
    EXECUTION_BINDING_DIGEST,
    EXECUTION_GRAPH_IDENTITY,
    EXECUTION_IDENTITY,
    HEARTBEAT_INTERVAL_SECONDS,
    LOCK_NAME,
    PHASES,
    CheckpointError,
    ExecutionPhase,
    GraphWorkUnitV2,
    ProductionGraphV2,
    ProductionLock,
    WorkUnitV1,
    _classification,
    _collect_bootstrap_scopes,
    _normalize_method,
    _scoped_bootstrap_summaries,
    _scoped_contribution_sections,
    _scoped_multiple_testing,
    assemble_from_checkpoints,
    candidate_aggregate_unit,
    checkpoint_inventory,
    execute,
    execute_phased_v2,
    fold_candidate_unit,
    format_progress,
    model_selection_unit,
    phased_status_v2,
    production_status_v2,
    production_work_units,
    publish_final_from_checkpoints,
    publish_final_v2,
    status,
    verify_checkpoint,
    write_verified_checkpoint,
)


def _unit(name: str = "24h") -> WorkUnitV1:
    return WorkUnitV1(
        ExecutionPhase.CANDIDATE_EVALUATION,
        horizon=name,
        task="TSUNAMI",
        fold="WF1",
        model="regularized_logistic",
        seed=20240101,
    )


def _calculator(unit: WorkUnitV1, heartbeat) -> dict[str, object]:
    heartbeat()
    return {"unit": unit.identity, "row_count": 10}


def test_execution_identity_phases_and_bindings_are_frozen() -> None:
    assert EXECUTION_IDENTITY == "CHAPPIE_CAPITAL_FLOW_TSUNAMI_RESEARCH_EXECUTION/1.0.0"
    assert RESEARCH_IDENTITY == "CHAPPIE_CAPITAL_FLOW_TSUNAMI_PREDICTIVE_RESEARCH/1.0.0"
    assert EXECUTION_GRAPH_IDENTITY == "CHAPPIE_CAPITAL_FLOW_TSUNAMI_RESEARCH_EXECUTION_GRAPH/2.0.0"
    assert len(EXECUTION_BINDING_DIGEST) == 64
    assert (
        ANALYTICAL_SEMANTIC_DIGEST
        == "46b7762bbee33d6831ab77d6525fc1f539f0db5505e9af13d1c3fb5fda8b7243"
    )
    assert (
        TARGET_SEMANTIC_DIGEST == "11d4323fecc384665cfff30c619598f95337252f155f703af9a9d1b28888eee8"
    )
    assert (
        TARGET_MANIFEST_SEMANTIC_DIGEST
        == "21f2a3e914528c13b2dd9ef28972969d56ad74b145622566e95f35dfb0a1c682"
    )
    assert tuple(item.value for item in PHASES) == tuple(item.value for item in ExecutionPhase)


def test_work_unit_identity_binds_every_dimension() -> None:
    original = _unit()
    assert original.identity == _unit().identity
    assert original.identity != _unit("12h").identity
    assert (
        original.identity
        != WorkUnitV1(
            ExecutionPhase.CANDIDATE_EVALUATION,
            horizon="24h",
            task="TSUNAMI",
            fold="WF2",
            model="regularized_logistic",
            seed=20240101,
        ).identity
    )


def test_v2_fold_aggregate_selection_and_downstream_identities_are_unambiguous() -> None:
    fold = fold_candidate_unit(
        horizon="24h",
        task="TSUNAMI",
        fold="WF1",
        model="regularized_logistic",
        hyperparameters=(("l2", "0.1"),),
        preprocessing_identity="prep/1",
        calibration_identity="cal/1",
        seed=20240101,
    )
    digests = tuple(str(index) * 64 for index in range(1, 7))
    aggregate = candidate_aggregate_unit(
        horizon="24h",
        task="TSUNAMI",
        model="regularized_logistic",
        hyperparameters=(("l2", "0.1"),),
        preprocessing_identity="prep/1",
        calibration_identity="cal/1",
        fold_evidence_digests=digests,
    )
    selection = model_selection_unit(
        horizon="24h", task="TSUNAMI", candidate_aggregate_digests=(aggregate.identity,)
    )
    direction = model_selection_unit(
        horizon="24h", task="DIRECTION", candidate_aggregate_digests=(aggregate.identity,)
    )
    assert fold.fold == "WF1" and aggregate.fold is None
    assert selection.identity != direction.identity
    downstream = (
        GraphWorkUnitV2(
            ExecutionPhase.ABLATION,
            horizon="24h",
            task="TSUNAMI",
            selected_configuration_digest=selection.identity,
            feature_exclusion="breadth.crypto_positive_share_60m",
        ),
        GraphWorkUnitV2(
            ExecutionPhase.PERMUTATION_IMPORTANCE,
            horizon="24h",
            task="TSUNAMI",
            selected_configuration_digest=selection.identity,
            feature_exclusion="breadth.crypto_positive_share_60m",
            permutation_index=0,
            held_out_evidence_digest="a" * 64,
        ),
        GraphWorkUnitV2(
            ExecutionPhase.FAMILY_CONTRIBUTION,
            horizon="24h",
            task="TSUNAMI",
            selected_configuration_digest=selection.identity,
            family_exclusion="CRYPTO_PARTICIPATION",
            family_operation="LEAVE_FAMILY_OUT",
        ),
        GraphWorkUnitV2(
            ExecutionPhase.INTERACTION_EVALUATION,
            horizon="24h",
            task="TSUNAMI",
            additive_configuration_digest="b" * 64,
            interaction_configuration_digest="c" * 64,
        ),
        GraphWorkUnitV2(
            ExecutionPhase.BOOTSTRAP,
            horizon="24h",
            task="TSUNAMI",
            bootstrap_start=0,
            bootstrap_end_exclusive=100,
            prediction_evidence_digest="d" * 64,
            seed=20240601,
        ),
    )
    assert len({item.identity for item in downstream}) == len(downstream)


def test_production_plan_has_independent_expensive_units() -> None:
    units = production_work_units()
    assert len({item.identity for item in units}) == len(units)
    baseline_units = [item for item in units if item.phase is ExecutionPhase.BASELINES]
    assert baseline_units and all(item.fold for item in baseline_units)
    assert sum(item.phase is ExecutionPhase.ABLATION for item in units) == 15
    assert sum(item.phase is ExecutionPhase.PERMUTATION_IMPORTANCE for item in units) == 15 * 20
    assert sum(item.phase is ExecutionPhase.FAMILY_CONTRIBUTION for item in units) == 8
    assert sum(item.phase is ExecutionPhase.BOOTSTRAP for item in units) == 20
    assert BOOTSTRAP_BATCH_SIZE == 100


def test_checkpoint_write_reload_and_verified_reuse(tmp_path: Path) -> None:
    unit = _unit()
    state = write_verified_checkpoint(tmp_path, unit, {"answer": 1, "row_count": 2})
    assert state.status == "CALCULATED"
    assert verify_checkpoint(tmp_path, unit).status == "REUSED"
    assert write_verified_checkpoint(tmp_path, unit, {"different": True}).status == "REUSED"


def test_partial_and_corrupt_checkpoint_fail_closed_without_deletion(tmp_path: Path) -> None:
    unit = _unit()
    artifact = (
        tmp_path / CHECKPOINT_ROOT_NAME / unit.phase.value.lower() / f"{unit.identity}.result.json"
    )
    artifact.parent.mkdir(parents=True)
    artifact.write_text("partial", encoding="utf-8")
    state = verify_checkpoint(tmp_path, unit)
    assert state.status == "INVALID"
    with pytest.raises(CheckpointError, match="recover manually"):
        write_verified_checkpoint(tmp_path, unit, {"answer": 1})
    assert artifact.read_text(encoding="utf-8") == "partial"


def test_wrong_identity_source_digest_and_work_unit_tampering_are_invalid(tmp_path: Path) -> None:
    unit = _unit()
    write_verified_checkpoint(tmp_path, unit, {"answer": 1})
    checkpoint = next(tmp_path.rglob("*.checkpoint.json"))
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    for key, value in (
        ("execution_binding_digest", "0" * 64),
        ("work_unit_identity", "wrong"),
        ("result_semantic_digest", "1" * 64),
    ):
        changed = dict(payload)
        changed[key] = value
        checkpoint.write_text(json.dumps(changed), encoding="utf-8")
        assert verify_checkpoint(tmp_path, unit).status == "INVALID"
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")
    assert verify_checkpoint(tmp_path, unit).status == "REUSED"


def test_live_or_stale_lock_rejects_second_writer_and_requires_manual_action(
    tmp_path: Path,
) -> None:
    with ProductionLock(tmp_path):
        assert (tmp_path / LOCK_NAME).exists()
        with (
            pytest.raises(CheckpointError, match="remove manually only if stale"),
            ProductionLock(tmp_path),
        ):
            pass
    assert not (tmp_path / LOCK_NAME).exists()
    lock = tmp_path / LOCK_NAME
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("stale", encoding="utf-8")
    with pytest.raises(CheckpointError, match="manually"), ProductionLock(tmp_path):
        pass
    assert lock.exists()


def test_cold_run_resume_exception_and_keyboard_interrupt_preserve_verified_units(
    tmp_path: Path,
) -> None:
    units = (_unit("1h"), _unit("4h"), _unit("12h"))
    calls: list[str] = []

    def fail_second(unit: WorkUnitV1, heartbeat) -> dict[str, object]:
        calls.append(unit.identity)
        if len(calls) == 2:
            raise RuntimeError("boom")
        return _calculator(unit, heartbeat)

    with pytest.raises(RuntimeError, match="boom"):
        execute(tmp_path, fail_second, units=units, emit=lambda _: None)
    assert checkpoint_inventory(tmp_path, units)[0].status == "REUSED"
    execute(tmp_path, _calculator, units=units, emit=lambda _: None)
    assert all(item.status == "REUSED" for item in checkpoint_inventory(tmp_path, units))

    interrupt_root = tmp_path / "interrupt"

    def interrupt(unit: WorkUnitV1, heartbeat):
        if unit == units[1]:
            raise KeyboardInterrupt
        return _calculator(unit, heartbeat)

    with pytest.raises(KeyboardInterrupt):
        execute(interrupt_root, interrupt, units=units, emit=lambda _: None)
    assert verify_checkpoint(interrupt_root, units[0]).status == "REUSED"


def test_progress_heartbeat_and_eta_are_structural_only(tmp_path: Path) -> None:
    messages: list[str] = []
    clock = iter((0.0, 0.0, 61.0, 62.0, 63.0, 64.0, 65.0, 66.0))

    def calculate(unit: WorkUnitV1, heartbeat):
        heartbeat()
        return {"complete": True}

    execute(
        tmp_path,
        calculate,
        units=(_unit("1h"), _unit("4h")),
        emit=messages.append,
        monotonic=lambda: next(clock),
    )
    assert any("status=HEARTBEAT" in item for item in messages)
    assert any(
        "REAL_RESEARCH_THROUGHPUT" in item and "REAL_RESEARCH_ETA" in item for item in messages
    )
    assert "phase=" in format_progress(_unit(), "CALCULATED", 1, 2, 5, None)
    assert HEARTBEAT_INTERVAL_SECONDS == 60


def test_status_is_read_only_and_reports_pending_verified_invalid_lock_and_manifest(
    tmp_path: Path,
) -> None:
    units = (_unit("1h"), _unit("4h"))
    write_verified_checkpoint(tmp_path, units[0], {"ok": True})
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    result = status(tmp_path, units)
    after = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    assert before == after
    assert result["verified_work_units"] == 1
    assert result["pending_work_units"] == 1
    assert result["result_state"] == "INCOMPLETE"


def test_final_assembly_requires_all_verified_and_create_only_manifest_guard(
    tmp_path: Path,
) -> None:
    units = (_unit("1h"), _unit("4h"))
    write_verified_checkpoint(tmp_path, units[0], {"ok": True})
    with pytest.raises(CheckpointError, match="every work unit"):
        assemble_from_checkpoints(tmp_path, units)
    write_verified_checkpoint(tmp_path, units[1], {"ok": True})
    assembled = assemble_from_checkpoints(tmp_path, units)
    assert len(assembled["checkpoint_evidence"]) == 2
    assert assembled["validation_status"] == "NOT_ACCESSED"
    (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(CheckpointError, match="create-only"):
        execute(tmp_path, _calculator, units=units, emit=lambda _: None)


def test_manifest_is_last_and_freeze_comes_only_from_verified_selection(tmp_path: Path) -> None:
    selection_units = tuple(
        WorkUnitV1(ExecutionPhase.MODEL_SELECTION, horizon=horizon, task=task)
        for horizon in ("1h", "4h", "12h", "24h", "48h", "72h")
        for task in ("DIRECTION", "MOVE", "STRESS", "TSUNAMI", "UP_TSUNAMI", "DOWN_TSUNAMI")
    )
    units = (*selection_units, WorkUnitV1(ExecutionPhase.FINAL_VERIFY))
    with pytest.raises(CheckpointError, match="every work unit"):
        publish_final_from_checkpoints(tmp_path, units)
    write_verified_checkpoint(
        tmp_path,
        selection_units[0],
        {"selected_model": "regularized_logistic", "selection_evidence_digest": "a" * 64},
    )
    assert not (tmp_path / "manifest.json").exists()
    for unit in selection_units[1:]:
        write_verified_checkpoint(
            tmp_path,
            unit,
            {"selected_model": "regularized_logistic", "selection_evidence_digest": unit.identity},
        )
    write_verified_checkpoint(tmp_path, units[-1], {"verified": True})
    manifest = publish_final_from_checkpoints(tmp_path, units)
    assert manifest["files"] == ["result.json", "validation_candidate_freeze.json"]
    freeze = json.loads((tmp_path / "validation_candidate_freeze.json").read_text(encoding="utf-8"))
    assert len(freeze["selection_evidence"]) == 36
    assert freeze["selection_evidence"][0]["result"]["selected_model"] == "regularized_logistic"
    assert (tmp_path / "manifest.json").exists()
    with pytest.raises(CheckpointError, match="create-only"):
        publish_final_from_checkpoints(tmp_path, units)


def _phased_resolvers():
    folds = tuple(
        fold_candidate_unit(
            horizon="24h",
            task="TSUNAMI",
            fold=f"WF{index}",
            model="regularized_logistic",
            hyperparameters=(("l2", "0.1"),),
            preprocessing_identity="prep/1",
            calibration_identity="cal/1",
            seed=20240101,
        )
        for index in range(1, 7)
    )

    def fold_phase(_evidence):
        return folds

    def aggregate_phase(evidence):
        digests = tuple(evidence[item.identity] for item in folds)
        return (
            candidate_aggregate_unit(
                horizon="24h",
                task="TSUNAMI",
                model="regularized_logistic",
                hyperparameters=(("l2", "0.1"),),
                preprocessing_identity="prep/1",
                calibration_identity="cal/1",
                fold_evidence_digests=digests,
            ),
        )

    def selection_phase(evidence):
        aggregate = aggregate_phase(evidence)[0]
        return (
            model_selection_unit(
                horizon="24h",
                task="TSUNAMI",
                candidate_aggregate_digests=(evidence[aggregate.identity],),
            ),
        )

    def downstream_phase(evidence):
        selection = selection_phase(evidence)[0]
        return (
            GraphWorkUnitV2(
                ExecutionPhase.ABLATION,
                horizon="24h",
                task="TSUNAMI",
                selected_configuration_digest=evidence[selection.identity],
                feature_exclusion="breadth.crypto_positive_share_60m",
            ),
        )

    def bootstrap_phase(evidence):
        downstream = downstream_phase(evidence)[0]
        return (
            GraphWorkUnitV2(
                ExecutionPhase.BOOTSTRAP,
                horizon="24h",
                task="TSUNAMI",
                bootstrap_start=0,
                bootstrap_end_exclusive=100,
                prediction_evidence_digest=evidence[downstream.identity],
            ),
        )

    def multiple_testing_phase(evidence):
        bootstrap = bootstrap_phase(evidence)[0]
        return (
            GraphWorkUnitV2(
                ExecutionPhase.MULTIPLE_TESTING,
                horizon="24h",
                task="TSUNAMI",
                evidence_set_digest=evidence[bootstrap.identity],
            ),
        )

    def freeze_phase(evidence):
        multiple_testing = multiple_testing_phase(evidence)[0]
        return (
            GraphWorkUnitV2(
                ExecutionPhase.VALIDATION_CANDIDATE_FREEZE,
                evidence_set_digest=evidence[multiple_testing.identity],
            ),
        )

    def final_verify_phase(evidence):
        freeze = freeze_phase(evidence)[0]
        return (
            GraphWorkUnitV2(
                ExecutionPhase.FINAL_VERIFY,
                evidence_set_digest=evidence[freeze.identity],
            ),
        )

    def manifest_phase(evidence):
        final_verify = final_verify_phase(evidence)[0]
        return (
            GraphWorkUnitV2(
                ExecutionPhase.MANIFEST,
                evidence_set_digest=evidence[final_verify.identity],
            ),
        )

    return (
        (ExecutionPhase.BASE_MODEL_FITS, fold_phase),
        (ExecutionPhase.CANDIDATE_EVALUATION, aggregate_phase),
        (ExecutionPhase.MODEL_SELECTION, selection_phase),
        (ExecutionPhase.ABLATION, downstream_phase),
        (ExecutionPhase.BOOTSTRAP, bootstrap_phase),
        (ExecutionPhase.MULTIPLE_TESTING, multiple_testing_phase),
        (ExecutionPhase.VALIDATION_CANDIDATE_FREEZE, freeze_phase),
        (ExecutionPhase.FINAL_VERIFY, final_verify_phase),
        (ExecutionPhase.MANIFEST, manifest_phase),
    )


def test_phased_v2_cold_start_dynamic_expansion_resume_and_status(tmp_path: Path) -> None:
    resolvers = _phased_resolvers()
    calls: list[str] = []

    def calculator(unit, heartbeat):
        calls.append(unit.identity)
        return {"genuine_evidence": unit.identity, "semantic": "bounded"}

    result = execute_phased_v2(
        tmp_path, resolvers, calculator, emit=lambda _: None, estimated_total_units=14
    )
    assert result["completed_phases"] == [item[0].value for item in resolvers]
    assert len(calls) == 14
    first_calls = tuple(calls)
    calls.clear()
    execute_phased_v2(tmp_path, resolvers, calculator, emit=lambda _: None)
    assert calls == []
    assert phased_status_v2(tmp_path, resolvers)["completed_phases"] == [
        item[0].value for item in resolvers
    ]
    assert first_calls[-1].startswith("v2-manifest-")


def test_phased_v2_interrupt_resume_and_result_dependent_identity(tmp_path: Path) -> None:
    resolvers = _phased_resolvers()
    count = 0

    def interrupt(unit, heartbeat):
        nonlocal count
        count += 1
        if count == 7:
            raise KeyboardInterrupt
        return {"genuine_evidence": unit.identity}

    with pytest.raises(KeyboardInterrupt):
        execute_phased_v2(tmp_path, resolvers, interrupt, emit=lambda _: None)
    status_after = phased_status_v2(tmp_path, resolvers)
    assert status_after["completed_phases"] == [ExecutionPhase.BASE_MODEL_FITS.value]
    executed_on_resume: list[str] = []
    execute_phased_v2(
        tmp_path,
        resolvers,
        lambda unit, heartbeat: (
            executed_on_resume.append(unit.identity) or {"genuine_evidence": unit.identity}
        ),
        emit=lambda _: None,
    )
    assert len(executed_on_resume) == 8

    original_folds = resolvers[0][1]({})
    original_evidence = {item.identity: "a" * 64 for item in original_folds}
    changed_evidence = dict(original_evidence)
    changed_evidence[original_folds[0].identity] = "b" * 64
    assert (
        resolvers[1][1](original_evidence)[0].identity
        != resolvers[1][1](changed_evidence)[0].identity
    )


def test_phased_v2_interrupt_during_fold_phase_reuses_verified_fold(tmp_path: Path) -> None:
    resolvers = _phased_resolvers()
    attempted = 0

    def interrupt_fold(unit, heartbeat):
        nonlocal attempted
        attempted += 1
        if attempted == 2:
            raise KeyboardInterrupt
        return {"genuine_evidence": unit.identity}

    with pytest.raises(KeyboardInterrupt):
        execute_phased_v2(tmp_path, resolvers, interrupt_fold, emit=lambda _: None)
    assert phased_status_v2(tmp_path, resolvers)["current_phase_counts"]["verified"] == 1

    calculated_on_resume: list[str] = []
    execute_phased_v2(
        tmp_path,
        resolvers,
        lambda unit, heartbeat: (
            calculated_on_resume.append(unit.identity) or {"genuine_evidence": unit.identity}
        ),
        emit=lambda _: None,
    )
    assert len(calculated_on_resume) == 13


def test_phased_v2_invalid_checkpoint_fails_closed(tmp_path: Path) -> None:
    resolvers = _phased_resolvers()
    first = resolvers[0][1]({})[0]
    write_verified_checkpoint(tmp_path, first, {"genuine": True})
    checkpoint = next(tmp_path.rglob("*.checkpoint.json"))
    checkpoint.write_text("{}", encoding="utf-8")
    with pytest.raises(CheckpointError, match="invalid checkpoint"):
        execute_phased_v2(
            tmp_path,
            resolvers,
            lambda unit, heartbeat: {"genuine": True},
            emit=lambda _: None,
        )


def test_production_v2_planner_is_internal_complete_and_result_dependent(tmp_path: Path) -> None:
    graph = ProductionGraphV2(tmp_path, tmp_path / "analytical", tmp_path / "targets")
    resolvers = graph.resolvers()
    assert [phase for phase, _resolver in resolvers] == [
        ExecutionPhase.INPUT_VERIFY,
        ExecutionPhase.BASE_MODEL_FITS,
        ExecutionPhase.CANDIDATE_EVALUATION,
        ExecutionPhase.MODEL_SELECTION,
        ExecutionPhase.ABLATION,
        ExecutionPhase.PERMUTATION_IMPORTANCE,
        ExecutionPhase.FAMILY_CONTRIBUTION,
        ExecutionPhase.INTERACTION_EVALUATION,
        ExecutionPhase.BOOTSTRAP,
        ExecutionPhase.MULTIPLE_TESTING,
        ExecutionPhase.VALIDATION_CANDIDATE_FREEZE,
        ExecutionPhase.FINAL_VERIFY,
        ExecutionPhase.MANIFEST,
    ]
    input_unit = resolvers[0][1]({})[0]
    folds = resolvers[1][1]({input_unit.identity: "a" * 64})
    assert len(folds) == 6 * 6 * 6 * 10
    with pytest.raises(CheckpointError, match="six verified folds"):
        resolvers[2][1]({})
    status_result = production_status_v2(tmp_path, graph.analytical_root, graph.target_root)
    assert status_result["current_phase"] == ExecutionPhase.INPUT_VERIFY.value
    assert status_result["execution_compatible"] is True


def test_cli_execute_and_status_use_internal_v2_binding(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    from app.tools import tsunami_predictive_research as cli

    calls = []
    monkeypatch.setattr(cli, "verify_bound_manifests", lambda analytical, target: ({}, {}))
    monkeypatch.setattr(
        cli,
        "execute_production_v2",
        lambda output, analytical, target: (
            calls.append((output, analytical, target))
            or {"execution": "genuine-v2", "manifest": "last"}
        ),
    )
    monkeypatch.setattr(
        cli,
        "production_status_v2",
        lambda output, analytical, target: {"current_phase": "INPUT_VERIFY"},
    )
    roots = [
        "--analytical-root",
        str(tmp_path / "analytical"),
        "--target-root",
        str(tmp_path / "targets"),
        "--output-root",
        str(tmp_path / "output"),
    ]
    assert cli.main([*roots, "--execute"]) == 0
    assert calls and json.loads(capsys.readouterr().out)["execution"] == "genuine-v2"
    assert cli.main([*roots, "--status"]) == 0
    assert json.loads(capsys.readouterr().out)["current_phase"] == "INPUT_VERIFY"


def test_v2_publication_uses_verified_graph_checkpoints_and_manifest_last(
    tmp_path: Path, monkeypatch
) -> None:
    selection = model_selection_unit(
        horizon="24h", task="TSUNAMI", candidate_aggregate_digests=("a" * 64,)
    )
    bootstrap = GraphWorkUnitV2(
        ExecutionPhase.BOOTSTRAP,
        horizon="24h",
        task="TSUNAMI",
        bootstrap_start=0,
        bootstrap_end_exclusive=100,
        prediction_evidence_digest="b" * 64,
    )
    manifest_unit = GraphWorkUnitV2(ExecutionPhase.MANIFEST, evidence_set_digest="c" * 64)
    write_verified_checkpoint(
        tmp_path,
        selection,
        {
            "identity": "SELECTED_RESEARCH_CONFIGURATION",
            "model": "majority_class",
            "selected_prediction_evidence_digest": "b" * 64,
        },
    )
    write_verified_checkpoint(
        tmp_path, bootstrap, {"kind": "BOOTSTRAP_BATCH", "replicates": [0.0] * 100}
    )
    write_verified_checkpoint(tmp_path, manifest_unit, {"kind": "MANIFEST"})

    class BoundedGraph:
        root = tmp_path

        def resolvers(self):
            return (
                (ExecutionPhase.MODEL_SELECTION, lambda evidence: (selection,)),
                (ExecutionPhase.BOOTSTRAP, lambda evidence: (bootstrap,)),
                (ExecutionPhase.MANIFEST, lambda evidence: (manifest_unit,)),
            )

        def _artifact(self, digest):
            for path in tmp_path.rglob("*.result.json"):
                payload = json.loads(path.read_text(encoding="utf-8"))
                canonical = json.dumps(
                    payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
                ).encode()
                if hashlib.sha256(canonical).hexdigest() == digest:
                    return payload
            raise AssertionError("missing test artifact")

    from app.research.capital_flow import tsunami_research_execution as execution_module

    complete_methods = {
        "permutation_raw_delta": 1.0,
        "permutation_sign": "POSITIVE",
        "permutation_normalization_eligible": True,
        "permutation_normalized_contribution_pct": 100.0,
        "ablation_raw_delta": 0.0,
        "ablation_sign": "NEUTRAL",
        "ablation_normalization_eligible": False,
        "ablation_normalized_contribution_pct": None,
    }
    contributions = {
        "individual_feature_contribution": [complete_methods] * (6 * 6 * 15),
        "family_contribution": [complete_methods] * (6 * 6 * 8),
        "interaction_contribution": [{}] * (6 * 6),
    }
    monkeypatch.setattr(
        execution_module, "_scoped_contribution_sections", lambda root: contributions
    )
    monkeypatch.setattr(
        execution_module,
        "_collect_bootstrap_scopes",
        lambda root: {("scope", "task", "metric", "d" * 64): (0.0,) * 2_000},
    )
    monkeypatch.setattr(
        execution_module,
        "_scoped_bootstrap_summaries",
        lambda root: [
            {"horizon": horizon, "task": task}
            for horizon in ("1h", "4h", "12h", "24h", "48h", "72h")
            for task in ("DIRECTION", "MOVE", "STRESS", "TSUNAMI", "UP_TSUNAMI", "DOWN_TSUNAMI")
        ],
    )
    monkeypatch.setattr(
        execution_module,
        "_scoped_multiple_testing",
        lambda scopes: ({"family": "CONFIRMATORY_HOLM"}, {"family": "EXPLORATORY_BH"}),
    )
    published = publish_final_v2(BoundedGraph())
    assert published["manifest_last"] is True
    assert (tmp_path / "result.json").exists()
    assert (tmp_path / "validation_candidate_freeze.json").exists()
    assert (tmp_path / "manifest.json").exists()


def _write_aggregation_result(
    root: Path, phase: ExecutionPhase, name: str, result: dict[str, object]
) -> None:
    path = root / CHECKPOINT_ROOT_NAME / phase.value.lower() / f"{name}.result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"result": result}), encoding="utf-8")


def test_scoped_contribution_classification_and_normalization_are_independent(
    tmp_path: Path,
) -> None:
    assert [_classification(value) for value in (1.0, 0.0, -1.0)] == [
        "POSITIVE",
        "NEUTRAL",
        "HARMFUL",
    ]
    normalized = _normalize_method(
        {
            ("24h", "TSUNAMI", "positive"): 3.0,
            ("24h", "TSUNAMI", "harmful"): -1.0,
            ("24h", "DIRECTION", "other_task"): 7.0,
            ("1h", "TSUNAMI", "other_horizon"): 9.0,
        },
        "PERMUTATION",
    )
    assert (
        normalized[("24h", "TSUNAMI", "positive")]["permutation_normalized_contribution_pct"]
        == 100.0
    )
    for key in (("24h", "TSUNAMI", "harmful"),):
        assert normalized[key]["permutation_normalization_eligible"] is False
        assert normalized[key]["permutation_normalized_contribution_pct"] is None

    for index in range(20):
        _write_aggregation_result(
            tmp_path,
            ExecutionPhase.PERMUTATION_IMPORTANCE,
            f"feature-{index}",
            {
                "kind": "PERMUTATION_IMPORTANCE",
                "horizon": "24h",
                "task": "TSUNAMI",
                "feature": "sensor",
                "family": None,
                "raw_delta": 2.0,
            },
        )
        _write_aggregation_result(
            tmp_path,
            ExecutionPhase.PERMUTATION_IMPORTANCE,
            f"family-{index}",
            {
                "kind": "PERMUTATION_IMPORTANCE",
                "horizon": "24h",
                "task": "TSUNAMI",
                "feature": None,
                "family": "family",
                "raw_delta": 8.0,
            },
        )
    _write_aggregation_result(
        tmp_path,
        ExecutionPhase.ABLATION,
        "feature-ablation",
        {
            "horizon": "24h",
            "task": "TSUNAMI",
            "feature": "sensor",
            "raw_delta": -3.0,
        },
    )
    _write_aggregation_result(
        tmp_path,
        ExecutionPhase.FAMILY_CONTRIBUTION,
        "family-ablation",
        {
            "horizon": "24h",
            "task": "TSUNAMI",
            "family": "family",
            "raw_delta": 4.0,
        },
    )
    _write_aggregation_result(
        tmp_path,
        ExecutionPhase.INTERACTION_EVALUATION,
        "interaction",
        {
            "horizon": "24h",
            "task": "TSUNAMI",
            "metric": "pr_auc",
            "additive_metric": 0.1,
            "interaction_metric": 0.2,
            "raw_delta": 0.1,
            "evidence_digest": "e" * 64,
        },
    )
    sections = _scoped_contribution_sections(tmp_path)
    feature = sections["individual_feature_contribution"][0]
    family = sections["family_contribution"][0]
    assert feature["permutation_raw_delta"] == 2.0
    assert feature["ablation_raw_delta"] == -3.0
    assert feature["ablation_sign"] == "HARMFUL"
    assert family["permutation_raw_delta"] == 8.0
    assert sections["interaction_contribution"][0]["incremental_raw_delta"] == 0.1


def test_bootstrap_and_multiple_testing_are_strictly_scoped(tmp_path: Path) -> None:
    scopes = (
        ("24h", "TSUNAMI", "pr_auc", "a" * 64, 1.0),
        ("24h", "DIRECTION", "balanced_accuracy", "b" * 64, 2.0),
        ("1h", "TSUNAMI", "pr_auc", "c" * 64, 3.0),
    )
    for scope_index, (horizon, task, metric, digest, value) in enumerate(scopes):
        for start in range(0, 2_000, 100):
            _write_aggregation_result(
                tmp_path,
                ExecutionPhase.BOOTSTRAP,
                f"{scope_index}-{start}",
                {
                    "horizon": horizon,
                    "task": task,
                    "metric": metric,
                    "prediction_evidence_digest": digest,
                    "batch_start": start,
                    "batch_end_exclusive": start + 100,
                    "replicates": [value] * 100,
                },
            )
    collected = _collect_bootstrap_scopes(tmp_path)
    summaries = _scoped_bootstrap_summaries(tmp_path)
    assert {item["point_estimate"] for item in summaries} == {1.0, 2.0, 3.0}
    assert all(item["replicate_count"] == 2_000 for item in summaries)
    confirmatory, exploratory = _scoped_multiple_testing(collected)
    assert len(confirmatory["hypotheses"]) == 2
    assert {item["task"] for item in confirmatory["hypotheses"]} == {
        "TSUNAMI",
        "DIRECTION",
    }
    assert all(item["horizon"] == "24h" for item in confirmatory["hypotheses"])
    assert len(exploratory["hypotheses"]) == 1
    assert exploratory["hypotheses"][0]["horizon"] == "1h"
    assert {item["testing_family"] for item in confirmatory["hypotheses"]} == {"CONFIRMATORY_HOLM"}
    assert {item["testing_family"] for item in exploratory["hypotheses"]} == {"EXPLORATORY_BH"}

    missing_root = tmp_path / "missing"
    for start in range(0, 1_900, 100):
        _write_aggregation_result(
            missing_root,
            ExecutionPhase.BOOTSTRAP,
            str(start),
            {
                "horizon": "24h",
                "task": "TSUNAMI",
                "metric": "pr_auc",
                "prediction_evidence_digest": "d" * 64,
                "batch_start": start,
                "batch_end_exclusive": start + 100,
                "replicates": [0.0] * 100,
            },
        )
    with pytest.raises(CheckpointError, match="missing or unexpected"):
        _collect_bootstrap_scopes(missing_root)

    wrong_metric_root = tmp_path / "wrong-metric"
    for start in range(0, 2_000, 100):
        _write_aggregation_result(
            wrong_metric_root,
            ExecutionPhase.BOOTSTRAP,
            str(start),
            {
                "horizon": "1h",
                "task": "TSUNAMI",
                "metric": "balanced_accuracy",
                "prediction_evidence_digest": "f" * 64,
                "batch_start": start,
                "batch_end_exclusive": start + 100,
                "replicates": [0.0] * 100,
            },
        )
    with pytest.raises(CheckpointError, match="wrong horizon, task, or metric"):
        _collect_bootstrap_scopes(wrong_metric_root)
