from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

import pytest

from app.research.capital_flow import free_v2_materializer as materializer
from app.research.capital_flow import free_v2_streaming_runner as runner
from app.research.capital_flow.free_v2_formulas import SOURCE_UNAVAILABLE
from app.research.capital_flow.free_v2_materializer import (
    FEATURE_ORDER,
    VALIDATED_AUTHORITY_BINDINGS,
    SourceAuthorityBindingV1,
)
from app.research.capital_flow.free_v2_publication import (
    _bytes,
    _record,
    verify_feature_artifact,
    verify_feature_dataset,
    write_feature_rows,
)
from app.research.capital_flow.free_v2_streaming_runner import (
    TimelineChunkV1,
    calendar_month_chunks,
    iter_minutes,
    run_streaming_materialization,
)

AUTH = tuple(SourceAuthorityBindingV1(*item) for item in VALIDATED_AUTHORITY_BINDINGS)
START = datetime(2024, 1, 1, tzinfo=UTC)


def _rows(timestamp: datetime):
    return tuple(
        materializer._make(
            name,
            timestamp,
            AUTH,
            (),
            "STREAMING_FIXTURE",
            None,
            state=SOURCE_UNAVAILABLE,
        )
        for name in FEATURE_ORDER
    )


def _flat(start: datetime, minutes: int):
    for timestamp in iter_minutes(start, start + timedelta(minutes=minutes)):
        yield from _rows(timestamp)


def test_exact_timeline_iterator_and_calendar_month_chunks() -> None:
    end = datetime(2024, 3, 2, tzinfo=UTC)
    chunks = calendar_month_chunks(START, end)
    assert chunks == (
        TimelineChunkV1(START, datetime(2024, 2, 1, tzinfo=UTC)),
        TimelineChunkV1(datetime(2024, 2, 1, tzinfo=UTC), datetime(2024, 3, 1, tzinfo=UTC)),
        TimelineChunkV1(datetime(2024, 3, 1, tzinfo=UTC), end),
    )
    assert len(tuple(iter_minutes(START, START + timedelta(hours=1)))) == 60
    assert chunks[0].identity == chunks[0].identity


def test_incremental_serialization_matches_reference_and_is_deterministic() -> None:
    rows = tuple(_flat(START, 3))
    reference = b"".join(_bytes(_record(row)) for row in rows)
    first = BytesIO()
    evidence = write_feature_rows(iter(rows), first, AUTH)
    second = BytesIO()
    write_feature_rows(iter(rows), second, AUTH)
    assert first.getvalue() == second.getvalue() == reference
    assert evidence.digest.hexdigest() == hashlib.sha256(reference).hexdigest()  # type: ignore[attr-defined]
    assert evidence.row_count == 45 and evidence.minute_count == 3
    assert all(item["source_unavailable_count"] == 3 for item in evidence.quality.payload())


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "order", "extra"])
def test_stream_rejects_timeline_and_feature_contract_violations(mutation: str) -> None:
    rows = list(_flat(START, 3))
    if mutation == "missing":
        del rows[15:30]
    elif mutation == "duplicate":
        rows[30:30] = rows[15:30]
    elif mutation == "order":
        rows[0], rows[1] = rows[1], rows[0]
    else:
        rows.extend(_rows(START + timedelta(minutes=4)))
    with pytest.raises(ValueError):
        write_feature_rows(rows, BytesIO(), AUTH)


def test_streaming_runner_resume_is_byte_identical_and_manifest_last(tmp_path: Path) -> None:
    end = START + timedelta(minutes=4)
    staging = tmp_path / "resume-staging"
    resumed_final = tmp_path / "resumed"

    def fail_before_manifest(phase: str) -> None:
        if phase == "final_artifact_published":
            raise RuntimeError("injected before manifest")

    with pytest.raises(RuntimeError, match="injected"):
        run_streaming_materialization(
            run_id="fixture-resume",
            staging_root=staging,
            final_root=resumed_final,
            start=START,
            end_exclusive=end,
            authorities=AUTH,
            materialize=_rows,
            phase_hook=fail_before_manifest,
        )
    assert not resumed_final.exists()
    assert tuple((staging / "chunks").glob("*.json"))

    resumed = run_streaming_materialization(
        run_id="fixture-resume",
        staging_root=staging,
        final_root=resumed_final,
        start=START,
        end_exclusive=end,
        authorities=AUTH,
        materialize=_rows,
    )
    cold_final = tmp_path / "cold"
    cold = run_streaming_materialization(
        run_id="fixture-cold",
        staging_root=tmp_path / "cold-staging",
        final_root=cold_final,
        start=START,
        end_exclusive=end,
        authorities=AUTH,
        materialize=_rows,
    )
    assert resumed == verify_feature_dataset(resumed_final, AUTH)
    assert resumed.artifact_digest == cold.artifact_digest
    assert resumed.dataset_semantic_digest == cold.dataset_semantic_digest
    assert resumed.manifest_semantic_digest == cold.manifest_semantic_digest
    assert (resumed_final / "features.ndjson").read_bytes() == (
        cold_final / "features.ndjson"
    ).read_bytes()


def test_corrupt_and_partial_checkpoints_fail_closed(tmp_path: Path) -> None:
    end = START + timedelta(minutes=2)
    staging = tmp_path / "staging"
    final = tmp_path / "final"
    run_streaming_materialization(
        run_id="first",
        staging_root=staging,
        final_root=final,
        start=START,
        end_exclusive=end,
        authorities=AUTH,
        materialize=_rows,
    )
    final.rename(tmp_path / "published")
    checkpoint = next((staging / "chunks").glob("*.json"))
    checkpoint.write_text(json.dumps({"corrupt": True}))
    with pytest.raises(ValueError, match="corrupt"):
        run_streaming_materialization(
            run_id="first",
            staging_root=staging,
            final_root=final,
            start=START,
            end_exclusive=end,
            authorities=AUTH,
            materialize=_rows,
        )


def test_execution_and_concurrency_guards(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "active.lock").write_text("active")
    with pytest.raises(RuntimeError, match="active"):
        run_streaming_materialization(
            run_id="one",
            staging_root=staging,
            final_root=tmp_path / "final",
            start=START,
            end_exclusive=START + timedelta(minutes=1),
            authorities=AUTH,
            materialize=_rows,
        )
    (staging / "active.lock").unlink()
    run_streaming_materialization(
        run_id="one",
        staging_root=staging,
        final_root=tmp_path / "final",
        start=START,
        end_exclusive=START + timedelta(minutes=1),
        authorities=AUTH,
        materialize=_rows,
    )
    (tmp_path / "final").rename(tmp_path / "published")
    with pytest.raises(RuntimeError, match="execution-count"):
        run_streaming_materialization(
            run_id="two",
            staging_root=staging,
            final_root=tmp_path / "final",
            start=START,
            end_exclusive=START + timedelta(minutes=1),
            authorities=AUTH,
            materialize=_rows,
        )


def test_streaming_artifact_verifier_detects_tampering(tmp_path: Path) -> None:
    artifact = tmp_path / "features.ndjson"
    with artifact.open("wb") as handle:
        write_feature_rows(_flat(START, 2), handle, AUTH)
    verified = verify_feature_artifact(artifact, AUTH)
    assert verified.row_count == 30
    lines = artifact.read_text().splitlines()
    artifact.write_text("\n".join((lines[0], *lines)) + "\n")
    with pytest.raises(ValueError):
        verify_feature_artifact(artifact, AUTH)


def test_row_authority_and_frozen_metadata_fail_closed(tmp_path: Path) -> None:
    artifact = tmp_path / "features.ndjson"
    with artifact.open("wb") as handle:
        write_feature_rows(_flat(START, 1), handle, AUTH)
    records = [json.loads(line) for line in artifact.read_text().splitlines()]
    records[0]["source_authorities"][0][1] = "0" * 64
    artifact.write_text("\n".join(json.dumps(item) for item in records) + "\n")
    with pytest.raises(ValueError, match="row authority"):
        verify_feature_artifact(artifact, AUTH)


def test_sources_are_verified_once_and_chunk_factory_is_once_per_chunk(
    tmp_path: Path, monkeypatch
) -> None:
    calls = {"verify": 0, "chunks": 0}

    def verify():
        calls["verify"] += 1
        return AUTH

    def factory(_chunk, bindings):
        calls["chunks"] += 1
        assert bindings == AUTH
        return _rows

    monkeypatch.setattr(runner, "verify_real_source_authorities", verify)
    boundary_start = datetime(2024, 1, 31, 23, 59, tzinfo=UTC)
    run_streaming_materialization(
        run_id="verify-once",
        staging_root=tmp_path / "staging",
        final_root=tmp_path / "final",
        start=boundary_start,
        end_exclusive=boundary_start + timedelta(minutes=2),
        chunk_materializer_factory=factory,
    )
    assert calls == {"verify": 1, "chunks": 2}


def test_production_timeline_rejects_test_source_overrides(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="production source"):
        run_streaming_materialization(
            run_id="forbidden-production-override",
            staging_root=tmp_path / "staging",
            final_root=tmp_path / "final",
            authorities=AUTH,
            materialize=_rows,
        )


def test_mid_chunk_failure_leaves_no_reusable_partial_or_manifest(tmp_path: Path) -> None:
    def fail(timestamp: datetime):
        if timestamp == START + timedelta(minutes=1):
            raise RuntimeError("mid-chunk")
        return _rows(timestamp)

    staging = tmp_path / "staging"
    final = tmp_path / "final"
    with pytest.raises(RuntimeError, match="mid-chunk"):
        run_streaming_materialization(
            run_id="mid-chunk",
            staging_root=staging,
            final_root=final,
            start=START,
            end_exclusive=START + timedelta(minutes=3),
            authorities=AUTH,
            materialize=fail,
        )
    assert not final.exists()
    assert not tuple((staging / "chunks").glob("*.ndjson"))
    assert not tuple((staging / "chunks").glob("*.json"))


def test_completed_checkpoint_survives_failure_before_finalization(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    final = tmp_path / "final"

    def fail_after_chunk(phase: str) -> None:
        if phase.startswith("chunk_completed:"):
            raise RuntimeError("after-checkpoint")

    with pytest.raises(RuntimeError, match="after-checkpoint"):
        run_streaming_materialization(
            run_id="after-checkpoint",
            staging_root=staging,
            final_root=final,
            start=START,
            end_exclusive=START + timedelta(minutes=2),
            authorities=AUTH,
            materialize=_rows,
            phase_hook=fail_after_chunk,
        )
    assert not final.exists()
    assert len(tuple((staging / "chunks").glob("*.ndjson"))) == 1
    assert len(tuple((staging / "chunks").glob("*.json"))) == 1


def test_final_verification_failure_removes_authoritative_outputs(
    tmp_path: Path, monkeypatch
) -> None:
    staging = tmp_path / "staging"
    final = tmp_path / "final"

    def fail_verify(*_args, **_kwargs):
        raise RuntimeError("final-verification")

    monkeypatch.setattr(runner, "verify_feature_dataset", fail_verify)
    with pytest.raises(RuntimeError, match="final-verification"):
        run_streaming_materialization(
            run_id="final-verification",
            staging_root=staging,
            final_root=final,
            start=START,
            end_exclusive=START + timedelta(minutes=2),
            authorities=AUTH,
            materialize=_rows,
        )
    assert not final.exists()
    assert len(tuple((staging / "chunks").glob("*.json"))) == 1


def test_read_only_status_cold_verified_and_corrupt(tmp_path: Path, monkeypatch) -> None:
    staging = tmp_path / "staging"
    final = tmp_path / "final"
    cold = runner.inspect_streaming_materialization(
        run_id="status-run", staging_root=staging, final_root=final, authorities=AUTH
    )
    assert cold.dataset_status == "ABSENT"
    assert cold.processed_minutes == 0
    assert not staging.exists() and not final.exists()

    chunk = TimelineChunkV1(START, START + timedelta(minutes=2))
    monkeypatch.setattr(runner, "calendar_month_chunks", lambda _start, _end: (chunk,))
    staging.mkdir()
    runner._canonical_json(
        staging / "run.json",
        runner._run_contract(
            "status-run", runner.FEATURE_TIMELINE_START, runner.FEATURE_TIMELINE_END_EXCLUSIVE, AUTH
        ),
    )
    runner._write_checkpoint(chunk, staging, AUTH, _rows)
    partial = runner.inspect_streaming_materialization(
        run_id="status-run", staging_root=staging, final_root=final, authorities=AUTH
    )
    assert partial.dataset_status == "INCOMPLETE_RESUMABLE"
    assert partial.chunks[0].status == "VERIFIED" and partial.processed_minutes == 2

    checkpoint = next((staging / "chunks").glob("*.json"))
    checkpoint.write_text("{}")
    corrupt = runner.inspect_streaming_materialization(
        run_id="status-run", staging_root=staging, final_root=final, authorities=AUTH
    )
    assert corrupt.dataset_status == "CORRUPT"
    assert corrupt.chunks[0].status == "INVALID" and corrupt.invalid_chunks


def test_read_only_active_run_state_distinguishes_live_and_stale(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    lock = staging / "active.lock"
    assert runner.active_run_state(staging) == "INACTIVE"
    lock.write_text(f"pid={os.getpid()}\n")
    assert runner.active_run_state(staging) == "ACTIVE"
    lock.write_text("pid=999999999\n")
    assert runner.active_run_state(staging) == "STALE_REQUIRES_MANUAL_INTERVENTION"


def test_crash_left_pending_file_is_reported_and_resume_rejected(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    chunks = staging / "chunks"
    chunks.mkdir(parents=True)
    partial = chunks / ".20240101T0000-20240201T0000.crash.pending"
    partial.write_bytes(b"partial")
    status = runner.inspect_streaming_materialization(
        run_id="pending-run", staging_root=staging, final_root=tmp_path / "final", authorities=AUTH
    )
    assert status.dataset_status == "CORRUPT"
    assert status.chunks[0].status == "INVALID"
    assert str(partial) in (status.chunks[0].error or "")
    assert str(partial) in status.invalid_chunks
    with pytest.raises(ValueError, match="partial temporary artifacts"):
        run_streaming_materialization(
            run_id="pending-run",
            staging_root=staging,
            final_root=tmp_path / "final",
            start=START,
            end_exclusive=START + timedelta(minutes=1),
            authorities=AUTH,
            materialize=_rows,
        )
    assert partial.read_bytes() == b"partial"


def test_read_only_status_classifies_verified_final_dataset_complete(
    tmp_path: Path, monkeypatch
) -> None:
    staging = tmp_path / "staging"
    final = tmp_path / "final"
    run_streaming_materialization(
        run_id="bounded-complete",
        staging_root=staging,
        final_root=final,
        start=START,
        end_exclusive=START + timedelta(minutes=1),
        authorities=AUTH,
        materialize=_rows,
    )
    before = {path: path.read_bytes() for path in (*staging.rglob("*.*"), *final.rglob("*.*"))}
    monkeypatch.setattr(runner, "verify_feature_dataset", lambda *_args, **_kwargs: object())
    status = runner.inspect_streaming_materialization(
        run_id="bounded-complete",
        staging_root=staging,
        final_root=final,
        authorities=AUTH,
    )
    after = {path: path.read_bytes() for path in before}
    assert status.dataset_status == "COMPLETE" and status.final_manifest_present
    assert before == after
