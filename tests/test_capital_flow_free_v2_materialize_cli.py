from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.research.capital_flow.free_v2_formulas import FEATURE_TIMELINE_MINUTES
from app.research.capital_flow.free_v2_streaming_runner import (
    ChunkStatusV1,
    MaterializationStatusV1,
    TimelineChunkV1,
)
from app.tools import capital_flow_free_v2_materialize as cli

START = datetime(2024, 1, 1, tzinfo=UTC)
CHUNK = TimelineChunkV1(START, START + timedelta(minutes=1))


def _status(dataset_status: str, chunk_status: str = "PENDING") -> MaterializationStatusV1:
    processed = 1 if chunk_status == "VERIFIED" else 0
    return MaterializationStatusV1(
        dataset_status=dataset_status,
        contract_compatible=dataset_status != "CORRUPT",
        source_authority_status="VALIDATED",
        chunks=(ChunkStatusV1(CHUNK, chunk_status),),
        processed_minutes=processed,
        total_minutes=FEATURE_TIMELINE_MINUTES,
        final_manifest_present=dataset_status == "COMPLETE",
        active_run_state="INACTIVE",
        invalid_chunks=(CHUNK.identity,) if chunk_status == "INVALID" else (),
        causal_violations=0 if dataset_status == "COMPLETE" else None,
    )


@pytest.mark.parametrize(
    ("dataset_status", "chunk_status"),
    [
        ("ABSENT", "PENDING"),
        ("INCOMPLETE_RESUMABLE", "VERIFIED"),
        ("COMPLETE", "VERIFIED"),
        ("CORRUPT", "INVALID"),
    ],
)
def test_status_is_read_only_and_reports_all_dataset_states(
    monkeypatch, capsys, dataset_status: str, chunk_status: str
) -> None:
    calls = {"inspect": 0, "run": 0}

    def inspect(*, run_id: str):
        calls["inspect"] += 1
        assert run_id == cli.PRODUCTION_RUN_ID
        return _status(dataset_status, chunk_status)

    def run(**_kwargs):
        calls["run"] += 1
        raise AssertionError("status must not materialize")

    monkeypatch.setattr(cli, "inspect_streaming_materialization", inspect)
    monkeypatch.setattr(cli, "run_streaming_materialization", run)
    assert cli.main(["--status"]) == 0
    output = capsys.readouterr().out
    assert f"dataset status: {dataset_status}" in output
    assert "source authority status: VALIDATED" in output
    if dataset_status == "CORRUPT":
        assert "invalid item:" in output
        assert "safe action:" in output
    assert calls == {"inspect": 1, "run": 0}


def test_default_invokes_released_runner_and_prints_verified_success(monkeypatch, capsys) -> None:
    calls = []
    monkeypatch.setattr(cli, "active_run_state", lambda _root: "INACTIVE")

    def run(*, run_id: str, phase_hook):
        calls.append(run_id)
        phase_hook("final_artifact_verified")
        return SimpleNamespace(row_count=FEATURE_TIMELINE_MINUTES * 15)

    monkeypatch.setattr(cli, "run_streaming_materialization", run)
    monkeypatch.setattr(cli, "_final_causal_violations", lambda: 0)
    assert cli.main([]) == 0
    output = capsys.readouterr().out
    assert cli.SUCCESS_CLASSIFICATION in output
    assert "manifest verification: PASS" in output
    assert calls == [cli.PRODUCTION_RUN_ID]


@pytest.mark.parametrize(
    ("state", "category"),
    [
        ("ACTIVE", "CAPITAL_FLOW_FREE_V2_RUN_ALREADY_ACTIVE"),
        ("STALE_REQUIRES_MANUAL_INTERVENTION", "CAPITAL_FLOW_FREE_V2_STALE_RUN_MARKER"),
    ],
)
def test_active_and_stale_run_markers_fail_without_starting(
    monkeypatch, capsys, state: str, category: str
) -> None:
    monkeypatch.setattr(cli, "active_run_state", lambda _root: state)
    monkeypatch.setattr(
        cli,
        "run_streaming_materialization",
        lambda **_kwargs: pytest.fail("runner must not start"),
    )
    assert cli.main([]) == 1
    assert category in capsys.readouterr().out


def test_runner_domain_failure_is_not_masked(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "active_run_state", lambda _root: "INACTIVE")

    def fail(**_kwargs):
        raise ValueError("corrupt chunk 2024-03")

    monkeypatch.setattr(cli, "run_streaming_materialization", fail)
    assert cli.main([]) == 1
    output = capsys.readouterr().out
    assert "CAPITAL_FLOW_FREE_V2_MATERIALIZATION_FAILED" in output
    assert "corrupt chunk 2024-03" in output


@pytest.mark.parametrize(
    "flag", ["--start", "--end", "--range", "--features", "--force", "--overwrite"]
)
def test_range_force_and_overwrite_flags_are_not_available(flag: str) -> None:
    with pytest.raises(SystemExit) as error:
        cli.main([flag, "value"])
    assert error.value.code == 2


def test_status_failure_has_domain_exit_code(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        cli,
        "inspect_streaming_materialization",
        lambda **_kwargs: (_ for _ in ()).throw(ValueError("source mismatch")),
    )
    assert cli.main(["--status"]) == 1
    assert "CAPITAL_FLOW_FREE_V2_STATUS_FAILED" in capsys.readouterr().out
