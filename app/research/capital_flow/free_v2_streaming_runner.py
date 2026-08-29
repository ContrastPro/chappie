"""Resumable bounded-memory execution for the Capital Flow Free V2 dataset."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .free_v2_formulas import (
    FEATURE_TIMELINE_END_EXCLUSIVE,
    FEATURE_TIMELINE_MINUTES,
    FEATURE_TIMELINE_START,
)
from .free_v2_materializer import (
    FEATURE_ORDER,
    CapitalFlowFeatureValueV2,
    SourceAuthorityBindingV1,
    dataset_contract_payload,
    load_real_materializer_chunk_inputs,
    materialize_timestamp,
    verify_real_source_authorities,
)
from .free_v2_publication import (
    FREE_V2_PUBLICATION_ROOT,
    PublishedFeatureDatasetV1,
    _bytes,
    _manifest,
    verify_feature_artifact,
    verify_feature_dataset,
    write_feature_rows,
)

FREE_V2_STAGING_ROOT = Path("data/research/staging/capital_flow/free_v2")


@dataclass(frozen=True, slots=True)
class TimelineChunkV1:
    start: datetime
    end_exclusive: datetime

    @property
    def identity(self) -> str:
        raw = f"{self.start.isoformat()}|{self.end_exclusive.isoformat()}".encode()
        return hashlib.sha256(raw).hexdigest()

    @property
    def minute_count(self) -> int:
        return int((self.end_exclusive - self.start).total_seconds() // 60)


@dataclass(frozen=True, slots=True)
class ChunkStatusV1:
    chunk: TimelineChunkV1
    status: str
    error: str | None = None


@dataclass(frozen=True, slots=True)
class MaterializationStatusV1:
    dataset_status: str
    contract_compatible: bool
    source_authority_status: str
    chunks: tuple[ChunkStatusV1, ...]
    processed_minutes: int
    total_minutes: int
    final_manifest_present: bool
    active_run_state: str
    invalid_chunks: tuple[str, ...]
    causal_violations: int | None = None


def iter_minutes(start: datetime, end_exclusive: datetime) -> Iterator[datetime]:
    if start.tzinfo != UTC or end_exclusive.tzinfo != UTC or start >= end_exclusive:
        raise ValueError("invalid exact UTC timeline")
    current = start
    while current < end_exclusive:
        yield current
        current += timedelta(minutes=1)


def calendar_month_chunks(start: datetime, end_exclusive: datetime) -> tuple[TimelineChunkV1, ...]:
    chunks = []
    current = start
    while current < end_exclusive:
        if current.month == 12:
            boundary = current.replace(year=current.year + 1, month=1, day=1, hour=0, minute=0)
        else:
            boundary = current.replace(month=current.month + 1, day=1, hour=0, minute=0)
        finish = min(boundary, end_exclusive)
        chunks.append(TimelineChunkV1(current, finish))
        current = finish
    return tuple(chunks)


def _canonical_json(path: Path, value: object) -> None:
    descriptor, name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".pending", dir=path.parent)
    pending = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_bytes(value, pretty=True))
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            raise FileExistsError(path)
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)


@contextmanager
def _exclusive_run_lock(staging: Path) -> Iterator[None]:
    staging.mkdir(parents=True, exist_ok=True)
    lock = staging / "active.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as error:
        raise RuntimeError("another materialization runner is active") from error
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(f"pid={os.getpid()}\n")
            handle.flush()
            os.fsync(handle.fileno())
        yield
    finally:
        lock.unlink(missing_ok=True)


def _run_contract(
    run_id: str,
    start: datetime,
    end_exclusive: datetime,
    authorities: tuple[SourceAuthorityBindingV1, ...],
) -> dict[str, object]:
    return {
        "classification": "NON_AUTHORITATIVE_STAGING",
        "run_id": run_id,
        "start": start.isoformat(),
        "end_exclusive": end_exclusive.isoformat(),
        "feature_order": list(FEATURE_ORDER),
        **dataset_contract_payload(authorities),
    }


def _active_run_state(staging: Path) -> str:
    lock = staging / "active.lock"
    if not lock.exists():
        return "INACTIVE"
    try:
        text = lock.read_text(encoding="utf-8").strip()
        pid = int(text.removeprefix("pid="))
        os.kill(pid, 0)
    except (OSError, ValueError):
        return "STALE_REQUIRES_MANUAL_INTERVENTION"
    return "ACTIVE"


def active_run_state(staging: Path = FREE_V2_STAGING_ROOT) -> str:
    """Return active/stale/inactive lock state without modifying execution state."""
    return _active_run_state(staging)


def _pending_files(staging: Path) -> tuple[Path, ...]:
    if not staging.exists():
        return ()
    return tuple(sorted(staging.rglob("*.pending")))


def inspect_streaming_materialization(
    *,
    run_id: str,
    staging_root: Path = FREE_V2_STAGING_ROOT,
    final_root: Path = FREE_V2_PUBLICATION_ROOT,
    authorities: tuple[SourceAuthorityBindingV1, ...] | None = None,
) -> MaterializationStatusV1:
    """Inspect production progress without writing, materializing, or finalizing."""
    verified = authorities or verify_real_source_authorities()
    chunks = calendar_month_chunks(FEATURE_TIMELINE_START, FEATURE_TIMELINE_END_EXCLUSIVE)
    expected_contract = json.loads(
        _bytes(
            _run_contract(run_id, FEATURE_TIMELINE_START, FEATURE_TIMELINE_END_EXCLUSIVE, verified)
        )
    )
    marker = staging_root / "run.json"
    compatible = True
    marker_present = marker.exists()
    if marker_present:
        try:
            compatible = json.loads(marker.read_text(encoding="utf-8")) == expected_contract
        except Exception:  # noqa: BLE001 - malformed marker is incompatible
            compatible = False
    statuses: list[ChunkStatusV1] = []
    invalid: list[str] = []
    processed = 0
    pending_files = _pending_files(staging_root)
    classified_pending: set[Path] = set()
    for chunk in chunks:
        artifact, checkpoint = _chunk_paths(staging_root, chunk)
        chunk_pending = tuple(path for path in pending_files if artifact.stem in path.name)
        if chunk_pending:
            classified_pending.update(chunk_pending)
            error = "non-authoritative partial temporary artifact: " + ", ".join(
                str(path) for path in chunk_pending
            )
            statuses.append(ChunkStatusV1(chunk, "INVALID", error))
            invalid.extend(str(path) for path in chunk_pending)
            continue
        if not artifact.exists() and not checkpoint.exists():
            statuses.append(ChunkStatusV1(chunk, "PENDING"))
            continue
        try:
            _verify_checkpoint(chunk, artifact, checkpoint, verified)
        except Exception as error:  # noqa: BLE001 - any checkpoint parse failure is invalid
            statuses.append(ChunkStatusV1(chunk, "INVALID", str(error)))
            invalid.append(chunk.identity)
        else:
            statuses.append(ChunkStatusV1(chunk, "VERIFIED"))
            processed += chunk.minute_count
    manifest = final_root / "manifest.json"
    causal_violations: int | None = None
    if final_root.exists():
        try:
            verify_feature_dataset(final_root, verified, require_full_timeline=True)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            causal_violations = sum(
                int(item["causal_violation_count"]) for item in payload["quality"]
            )
            dataset_status = "COMPLETE" if causal_violations == 0 else "CORRUPT"
        except Exception:  # noqa: BLE001 - any final verification failure is corrupt
            dataset_status = "CORRUPT"
    invalid.extend(str(path) for path in pending_files if path not in classified_pending)
    final_exists = final_root.exists()
    if final_exists and pending_files:
        dataset_status = "CORRUPT"
    elif not final_exists:
        if invalid or not compatible or (not marker_present and processed):
            dataset_status = "CORRUPT"
        elif marker_present or processed:
            dataset_status = "INCOMPLETE_RESUMABLE"
        else:
            dataset_status = "ABSENT"
    return MaterializationStatusV1(
        dataset_status,
        compatible,
        "VALIDATED",
        tuple(statuses),
        processed,
        FEATURE_TIMELINE_MINUTES,
        manifest.exists(),
        _active_run_state(staging_root),
        tuple(invalid),
        causal_violations,
    )


def _rows_for_chunk(
    chunk: TimelineChunkV1,
    materialize: Callable[[datetime], Iterable[CapitalFlowFeatureValueV2]],
) -> Iterator[CapitalFlowFeatureValueV2]:
    for timestamp in iter_minutes(chunk.start, chunk.end_exclusive):
        yield from materialize(timestamp)


def _chunk_paths(staging: Path, chunk: TimelineChunkV1) -> tuple[Path, Path]:
    stem = f"{chunk.start:%Y%m%dT%H%M}-{chunk.end_exclusive:%Y%m%dT%H%M}"
    return staging / "chunks" / f"{stem}.ndjson", staging / "chunks" / f"{stem}.json"


def _checkpoint_payload(
    chunk: TimelineChunkV1,
    artifact: Path,
    authorities: tuple[SourceAuthorityBindingV1, ...],
) -> dict[str, object]:
    evidence = verify_feature_artifact(artifact, authorities)
    if (
        evidence.first_timestamp != chunk.start
        or evidence.last_timestamp != chunk.end_exclusive - timedelta(minutes=1)
        or evidence.minute_count != chunk.minute_count
        or evidence.row_count != chunk.minute_count * len(FEATURE_ORDER)
    ):
        raise ValueError("chunk timeline contract mismatch")
    return {
        "classification": "NON_AUTHORITATIVE_CHECKPOINT",
        "dataset_contract": dataset_contract_payload(authorities),
        "chunk_identity": chunk.identity,
        "start": chunk.start.isoformat(),
        "end_exclusive": chunk.end_exclusive.isoformat(),
        "row_count": evidence.row_count,
        "sha256": evidence.digest.hexdigest(),  # type: ignore[attr-defined]
        "quality": evidence.quality.payload(),
    }


def _verify_checkpoint(
    chunk: TimelineChunkV1,
    artifact: Path,
    checkpoint: Path,
    authorities: tuple[SourceAuthorityBindingV1, ...],
) -> None:
    if artifact.exists() != checkpoint.exists():
        raise ValueError("partial chunk checkpoint")
    expected = json.loads(_bytes(_checkpoint_payload(chunk, artifact, authorities)))
    if json.loads(checkpoint.read_text(encoding="utf-8")) != expected:
        raise ValueError("corrupt chunk checkpoint")


def _write_checkpoint(
    chunk: TimelineChunkV1,
    staging: Path,
    authorities: tuple[SourceAuthorityBindingV1, ...],
    materialize: Callable[[datetime], Iterable[CapitalFlowFeatureValueV2]],
) -> tuple[Path, Path]:
    artifact, checkpoint = _chunk_paths(staging, chunk)
    artifact.parent.mkdir(parents=True, exist_ok=True)
    if artifact.exists() or checkpoint.exists():
        _verify_checkpoint(chunk, artifact, checkpoint, authorities)
        return artifact, checkpoint
    descriptor, name = tempfile.mkstemp(
        prefix=f".{artifact.stem}.", suffix=".pending", dir=artifact.parent
    )
    pending = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            write_feature_rows(_rows_for_chunk(chunk, materialize), handle, authorities)
            handle.flush()
            os.fsync(handle.fileno())
        payload = _checkpoint_payload(chunk, pending, authorities)
        os.replace(pending, artifact)
        _canonical_json(checkpoint, payload)
        _verify_checkpoint(chunk, artifact, checkpoint, authorities)
        return artifact, checkpoint
    except BaseException:
        pending.unlink(missing_ok=True)
        if artifact.exists() and not checkpoint.exists():
            artifact.unlink(missing_ok=True)
        raise


def _finalize(
    chunks: tuple[TimelineChunkV1, ...],
    staging: Path,
    final_root: Path,
    authorities: tuple[SourceAuthorityBindingV1, ...],
    *,
    require_full_timeline: bool,
    phase_hook: Callable[[str], None],
) -> PublishedFeatureDatasetV1:
    if final_root.exists():
        raise FileExistsError("feature dataset already exists")
    final_root.mkdir(parents=True)
    artifact = final_root / "features.ndjson"
    manifest_path = final_root / "manifest.json"
    descriptor, name = tempfile.mkstemp(prefix=".features.", suffix=".pending", dir=final_root)
    pending = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as target:
            for chunk in chunks:
                source, checkpoint = _chunk_paths(staging, chunk)
                _verify_checkpoint(chunk, source, checkpoint, authorities)
                with source.open("rb") as handle:
                    shutil.copyfileobj(handle, target, length=1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
        evidence = verify_feature_artifact(pending, authorities)
        if require_full_timeline and (
            evidence.minute_count != FEATURE_TIMELINE_MINUTES
            or evidence.first_timestamp != FEATURE_TIMELINE_START
            or evidence.last_timestamp != FEATURE_TIMELINE_END_EXCLUSIVE - timedelta(minutes=1)
        ):
            raise ValueError("full feature timeline contract mismatch")
        phase_hook("final_artifact_verified")
        os.replace(pending, artifact)
        phase_hook("final_artifact_published")
        _canonical_json(manifest_path, _manifest(authorities, evidence))
        result = verify_feature_dataset(
            final_root, authorities, require_full_timeline=require_full_timeline
        )
        phase_hook("manifest_published")
        return result
    except BaseException:
        pending.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)
        artifact.unlink(missing_ok=True)
        try:
            final_root.rmdir()
        except OSError:
            pass
        raise


def run_streaming_materialization(
    *,
    run_id: str,
    staging_root: Path = FREE_V2_STAGING_ROOT,
    final_root: Path = FREE_V2_PUBLICATION_ROOT,
    start: datetime = FEATURE_TIMELINE_START,
    end_exclusive: datetime = FEATURE_TIMELINE_END_EXCLUSIVE,
    authorities: tuple[SourceAuthorityBindingV1, ...] | None = None,
    materialize: Callable[[datetime], Iterable[CapitalFlowFeatureValueV2]] | None = None,
    chunk_materializer_factory: Callable[
        [TimelineChunkV1, tuple[SourceAuthorityBindingV1, ...]],
        Callable[[datetime], Iterable[CapitalFlowFeatureValueV2]],
    ]
    | None = None,
    phase_hook: Callable[[str], None] | None = None,
) -> PublishedFeatureDatasetV1:
    """Execute or resume one deterministic run; production defaults verify sources once."""
    if not run_id:
        raise ValueError("explicit execution run_id required")
    production = start == FEATURE_TIMELINE_START and end_exclusive == FEATURE_TIMELINE_END_EXCLUSIVE
    if production and (
        authorities is not None or materialize is not None or chunk_materializer_factory is not None
    ):
        raise ValueError("production source and materializer overrides are forbidden")
    if final_root.exists():
        raise FileExistsError("feature dataset already exists")
    verified = authorities or verify_real_source_authorities()
    if materialize is not None and chunk_materializer_factory is not None:
        raise ValueError("choose one materialization provider")

    def real_factory(
        chunk: TimelineChunkV1, bindings: tuple[SourceAuthorityBindingV1, ...]
    ) -> Callable[[datetime], Iterable[CapitalFlowFeatureValueV2]]:
        inputs = load_real_materializer_chunk_inputs(chunk.start, chunk.end_exclusive, bindings)
        return lambda timestamp: materialize_timestamp(inputs, timestamp)

    factory = chunk_materializer_factory or (
        (lambda _chunk, _bindings: materialize) if materialize is not None else real_factory
    )
    contract = _run_contract(run_id, start, end_exclusive, verified)
    notify = phase_hook or (lambda _: None)
    with _exclusive_run_lock(staging_root):
        pending_files = _pending_files(staging_root)
        if pending_files:
            raise ValueError(
                "non-authoritative partial temporary artifacts require manual inspection; "
                "after confirming no active run, remove only these .pending files, then resume: "
                + ", ".join(str(path) for path in pending_files)
            )
        marker = staging_root / "run.json"
        if marker.exists():
            normalized_contract = json.loads(_bytes(contract))
            if json.loads(marker.read_text(encoding="utf-8")) != normalized_contract:
                raise RuntimeError("execution-count guard rejected a different run")
        else:
            _canonical_json(marker, contract)
        chunks = calendar_month_chunks(start, end_exclusive)
        for chunk in chunks:
            artifact, checkpoint = _chunk_paths(staging_root, chunk)
            if artifact.exists() or checkpoint.exists():
                _verify_checkpoint(chunk, artifact, checkpoint, verified)
                notify(f"chunk_reused:{chunk.identity}")
            else:
                chunk_materialize = factory(chunk, verified)
                _write_checkpoint(chunk, staging_root, verified, chunk_materialize)
                notify(f"chunk_completed:{chunk.identity}")
        return _finalize(
            chunks,
            staging_root,
            final_root,
            verified,
            require_full_timeline=production,
            phase_hook=notify,
        )
