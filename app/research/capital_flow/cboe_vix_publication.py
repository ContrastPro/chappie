"""Deterministic create-only publication for parsed Cboe VIX authority data."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .calendar import XNYS_CALENDAR_ID
from .cboe_vix_authority import (
    CBOE_PROVIDER_IDENTITY,
    CBOE_VIX_AUTHORITY_IDENTITY,
    CBOE_VIX_BLIND_START,
    CBOE_VIX_END,
    CBOE_VIX_SOURCE_IDENTITY,
    CBOE_VIX_START,
    CBOE_VIX_USABLE_POLICY,
    CboeVixBoundedResultV1,
    CboeVixSourceRowV1,
)

CBOE_VIX_PUBLICATION_ROOT = Path("data/research/source/capital_flow/cboe_vix_free_v1")
CBOE_VIX_BOUNDED_SOURCE_NAME = "bounded_source.ndjson"
CBOE_VIX_CANONICAL_NAME = "canonical.ndjson"
CBOE_VIX_MANIFEST_NAME = "manifest.json"
CBOE_VIX_BOUNDED_EXTRACTION_IDENTITY = "BOUNDED_OFFICIAL_SOURCE_EXTRACTION/1.0.0"
CBOE_VIX_FREQUENCY = "daily"
CBOE_VIX_UNITS = "index_points"
CBOE_VIX_STALENESS_POLICY = "LATEST_COMPLETED_XNYS_SESSION"


@dataclass(frozen=True, slots=True)
class CboeVixPublishedAuthorityV1:
    root: Path
    bounded_source_path: Path
    canonical_path: Path
    manifest_path: Path
    bounded_source_digest: str
    canonical_digest: str
    semantic_authority_digest: str
    manifest_semantic_digest: str


def _json_bytes(value: object, *, pretty: bool = False) -> bytes:
    if pretty:
        return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != datetime.now(UTC).utcoffset():
        raise ValueError("Cboe VIX datetime must be UTC")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _row_payload(row: CboeVixSourceRowV1) -> dict[str, str]:
    return {
        "close": str(row.close),
        "reference_date": row.reference_date.isoformat(),
        "session_close_utc": _utc_text(row.session_close_utc),
        "usable_at_utc": _utc_text(row.usable_at_utc),
    }


def _quality_payload(result: CboeVixBoundedResultV1) -> dict[str, object]:
    quality = result.quality
    return {
        "conflicting_duplicate_count": quality.conflicting_duplicate_count,
        "cutoff_crossing_date": (
            quality.cutoff_crossing_date.isoformat() if quality.cutoff_crossing_date else None
        ),
        "expected_session_count": quality.expected_session_count,
        "first_session": quality.first_session.isoformat() if quality.first_session else None,
        "identical_duplicate_count": quality.identical_duplicate_count,
        "last_session": quality.last_session.isoformat() if quality.last_session else None,
        "malformed_count": quality.malformed_count,
        "missing_expected_sessions": [
            item.isoformat() for item in quality.missing_expected_sessions
        ],
        "row_count": quality.row_count,
        "rows_seen_after_cutoff": quality.rows_seen_after_cutoff,
        "unexpected_sessions": [item.isoformat() for item in quality.unexpected_sessions],
    }


def _validate_result(result: CboeVixBoundedResultV1) -> None:
    quality = result.quality
    if (
        result.authority_identity != CBOE_VIX_AUTHORITY_IDENTITY
        or result.provider_identity != CBOE_PROVIDER_IDENTITY
        or result.authorized_start != CBOE_VIX_START
        or result.authorized_end != CBOE_VIX_END
    ):
        raise ValueError("Cboe VIX publication identity or bounds invalid")
    if (
        quality.missing_expected_sessions
        or quality.unexpected_sessions
        or quality.conflicting_duplicate_count
        or quality.malformed_count
        or quality.rows_seen_after_cutoff > 1
    ):
        raise ValueError("Cboe VIX publication quality gate failed")
    if quality.row_count != len(result.rows) or quality.expected_session_count != len(result.rows):
        raise ValueError("Cboe VIX publication coverage evidence inconsistent")
    dates = tuple(row.reference_date for row in result.rows)
    if dates != tuple(sorted(dates)) or len(dates) != len(set(dates)):
        raise ValueError("Cboe VIX canonical row ordering invalid")
    if any(item < CBOE_VIX_START or item > CBOE_VIX_END for item in dates):
        raise ValueError("Cboe VIX canonical row outside authorized range")
    if quality.first_session != (dates[0] if dates else None) or quality.last_session != (
        dates[-1] if dates else None
    ):
        raise ValueError("Cboe VIX publication endpoints inconsistent")
    if quality.cutoff_crossing_date is not None and not (
        CBOE_VIX_END < quality.cutoff_crossing_date < CBOE_VIX_BLIND_START
    ):
        raise ValueError("Cboe VIX blind exclusion gate failed")
    if quality.rows_seen_after_cutoff != (1 if quality.cutoff_crossing_date else 0):
        raise ValueError("Cboe VIX cutoff evidence inconsistent")
    for row in result.rows:
        if not row.close.is_finite() or row.close <= 0:
            raise ValueError("Cboe VIX canonical Decimal invalid")
        _utc_text(row.session_close_utc)
        _utc_text(row.usable_at_utc)


def serialize_bounded_cboe_vix_source(result: CboeVixBoundedResultV1) -> bytes:
    _validate_result(result)
    header = {
        "authorized_end": result.authorized_end.isoformat(),
        "authorized_start": result.authorized_start.isoformat(),
        "authority_identity": result.authority_identity,
        "extraction_identity": CBOE_VIX_BOUNDED_EXTRACTION_IDENTITY,
        "provider": result.provider_identity,
        "source_identity": CBOE_VIX_SOURCE_IDENTITY,
        "usable_at_policy": CBOE_VIX_USABLE_POLICY,
    }
    records = [{"kind": "header", **header}]
    records.extend({"kind": "row", **_row_payload(row)} for row in result.rows)
    records.append({"kind": "quality", **_quality_payload(result)})
    return b"".join(_json_bytes(record) for record in records)


def serialize_canonical_cboe_vix(result: CboeVixBoundedResultV1) -> bytes:
    _validate_result(result)
    return b"".join(_json_bytes(_row_payload(row)) for row in result.rows)


def read_bounded_cboe_vix_source(path: Path) -> tuple[dict[str, Any], ...]:
    records = tuple(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
    if (
        len(records) < 2
        or records[0].get("kind") != "header"
        or records[-1].get("kind") != "quality"
    ):
        raise ValueError("invalid bounded Cboe VIX source evidence")
    header = records[0]
    expected = {
        "authority_identity": CBOE_VIX_AUTHORITY_IDENTITY,
        "authorized_start": CBOE_VIX_START.isoformat(),
        "authorized_end": CBOE_VIX_END.isoformat(),
        "extraction_identity": CBOE_VIX_BOUNDED_EXTRACTION_IDENTITY,
        "provider": CBOE_PROVIDER_IDENTITY,
        "source_identity": CBOE_VIX_SOURCE_IDENTITY,
        "usable_at_policy": CBOE_VIX_USABLE_POLICY,
    }
    if any(header.get(key) != value for key, value in expected.items()):
        raise ValueError("bounded Cboe VIX source identity mismatch")
    row_records = records[1:-1]
    if any(record.get("kind") != "row" for record in row_records):
        raise ValueError("invalid bounded Cboe VIX row record")
    rows = tuple(_row_from_payload(record) for record in row_records)
    dates = tuple(row.reference_date for row in rows)
    if dates != tuple(sorted(dates)) or len(dates) != len(set(dates)):
        raise ValueError("bounded Cboe VIX source ordering invalid")
    quality = records[-1]
    if quality.get("row_count") != len(rows):
        raise ValueError("bounded Cboe VIX source row count mismatch")
    if quality.get("first_session") != (dates[0].isoformat() if dates else None):
        raise ValueError("bounded Cboe VIX source first session mismatch")
    if quality.get("last_session") != (dates[-1].isoformat() if dates else None):
        raise ValueError("bounded Cboe VIX source last session mismatch")
    return records


def _parse_utc(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() != datetime.now(UTC).utcoffset():
        raise ValueError("canonical Cboe VIX datetime is not UTC")
    return result.astimezone(UTC)


def _row_from_payload(payload: dict[str, Any]) -> CboeVixSourceRowV1:
    return CboeVixSourceRowV1(
        date.fromisoformat(payload["reference_date"]),
        Decimal(payload["close"]),
        _parse_utc(payload["session_close_utc"]),
        _parse_utc(payload["usable_at_utc"]),
    )


def read_canonical_cboe_vix(path: Path) -> tuple[CboeVixSourceRowV1, ...]:
    rows: list[CboeVixSourceRowV1] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        rows.append(_row_from_payload(json.loads(line)))
    dates = tuple(row.reference_date for row in rows)
    if dates != tuple(sorted(dates)) or len(dates) != len(set(dates)):
        raise ValueError("canonical Cboe VIX ordering invalid")
    if any(item < CBOE_VIX_START or item > CBOE_VIX_END for item in dates):
        raise ValueError("canonical Cboe VIX row outside authorized range")
    if any(not row.close.is_finite() or row.close <= 0 for row in rows):
        raise ValueError("canonical Cboe VIX Decimal invalid")
    return tuple(rows)


def read_cboe_vix_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("authority_identity") != CBOE_VIX_AUTHORITY_IDENTITY:
        raise ValueError("Cboe VIX manifest authority identity mismatch")
    semantic_digest = payload.pop("manifest_semantic_digest", None)
    if semantic_digest != _digest(_json_bytes(payload)):
        raise ValueError("Cboe VIX manifest semantic digest mismatch")
    payload["manifest_semantic_digest"] = semantic_digest
    expected = {
        "authority_identity": CBOE_VIX_AUTHORITY_IDENTITY,
        "authorized_start": CBOE_VIX_START.isoformat(),
        "authorized_end": CBOE_VIX_END.isoformat(),
        "bounded_extraction_identity": CBOE_VIX_BOUNDED_EXTRACTION_IDENTITY,
        "bounded_source_path": CBOE_VIX_BOUNDED_SOURCE_NAME,
        "calendar_authority": XNYS_CALENDAR_ID,
        "canonical_path": CBOE_VIX_CANONICAL_NAME,
        "frequency": CBOE_VIX_FREQUENCY,
        "provider": CBOE_PROVIDER_IDENTITY,
        "source_identity": CBOE_VIX_SOURCE_IDENTITY,
        "staleness_policy": CBOE_VIX_STALENESS_POLICY,
        "units": CBOE_VIX_UNITS,
        "usable_at_policy": CBOE_VIX_USABLE_POLICY,
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise ValueError("Cboe VIX manifest binding mismatch")
    if payload.get("blind_exclusion") != {
        "blind_start": CBOE_VIX_BLIND_START.isoformat(),
        "blind_rows_observed": 0,
    }:
        raise ValueError("Cboe VIX manifest blind exclusion mismatch")
    return payload


def _write_temporary(root: Path, prefix: str, payload: bytes) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=prefix, suffix=".pending", dir=root)
    path = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        _cleanup_owned(path)
        raise
    return path


def _cleanup_owned(path: Path) -> None:
    """Best-effort cleanup that never masks the primary publication failure."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _publish_create_only(temporary: Path, final: Path) -> None:
    os.link(temporary, final)


def _semantic_payload(
    result: CboeVixBoundedResultV1, source_digest: str, canonical_digest: str
) -> dict[str, object]:
    return {
        "authority_identity": result.authority_identity,
        "authorized_end": result.authorized_end.isoformat(),
        "authorized_start": result.authorized_start.isoformat(),
        "bounded_source_digest": source_digest,
        "calendar_authority": XNYS_CALENDAR_ID,
        "canonical_digest": canonical_digest,
        "frequency": CBOE_VIX_FREQUENCY,
        "provider": result.provider_identity,
        "quality": _quality_payload(result),
        "source_identity": CBOE_VIX_SOURCE_IDENTITY,
        "staleness_policy": CBOE_VIX_STALENESS_POLICY,
        "units": CBOE_VIX_UNITS,
        "usable_at_policy": CBOE_VIX_USABLE_POLICY,
    }


def publish_canonical_cboe_vix(
    result: CboeVixBoundedResultV1,
    root: Path = CBOE_VIX_PUBLICATION_ROOT,
    *,
    phase_hook: Callable[[str], None] | None = None,
) -> CboeVixPublishedAuthorityV1:
    """Publish bounded source, canonical rows, then manifest using create-only links."""
    _validate_result(result)
    root.mkdir(parents=True, exist_ok=True)
    source_path = root / CBOE_VIX_BOUNDED_SOURCE_NAME
    canonical_path = root / CBOE_VIX_CANONICAL_NAME
    manifest_path = root / CBOE_VIX_MANIFEST_NAME
    if any(path.exists() for path in (source_path, canonical_path, manifest_path)):
        raise FileExistsError("Cboe VIX authority artifact already exists")
    notify = phase_hook or (lambda _phase: None)
    source_bytes = serialize_bounded_cboe_vix_source(result)
    canonical_bytes = serialize_canonical_cboe_vix(result)
    source_digest = _digest(source_bytes)
    canonical_digest = _digest(canonical_bytes)
    semantic = _semantic_payload(result, source_digest, canonical_digest)
    authority_digest = _digest(_json_bytes(semantic))
    manifest: dict[str, object] = {
        **semantic,
        "blind_exclusion": {
            "blind_start": CBOE_VIX_BLIND_START.isoformat(),
            "blind_rows_observed": 0,
        },
        "bounded_extraction_identity": CBOE_VIX_BOUNDED_EXTRACTION_IDENTITY,
        "bounded_source_path": CBOE_VIX_BOUNDED_SOURCE_NAME,
        "canonical_path": CBOE_VIX_CANONICAL_NAME,
        "semantic_authority_digest": authority_digest,
    }
    manifest["manifest_semantic_digest"] = _digest(_json_bytes(manifest))
    manifest_bytes = _json_bytes(manifest, pretty=True)
    temporaries: list[Path] = []
    published: list[Path] = []
    try:
        source_temp = _write_temporary(root, ".bounded-source.", source_bytes)
        temporaries.append(source_temp)
        notify("bounded_source_temporary")
        read_bounded_cboe_vix_source(source_temp)
        if _digest(source_temp.read_bytes()) != source_digest:
            raise ValueError("bounded Cboe VIX source digest mismatch")
        notify("bounded_source_validated")

        canonical_temp = _write_temporary(root, ".canonical.", canonical_bytes)
        temporaries.append(canonical_temp)
        notify("canonical_temporary")
        if read_canonical_cboe_vix(canonical_temp) != result.rows:
            raise ValueError("canonical Cboe VIX reload mismatch")
        if _digest(canonical_temp.read_bytes()) != canonical_digest:
            raise ValueError("canonical Cboe VIX digest mismatch")
        notify("canonical_validated")

        manifest_temp = _write_temporary(root, ".manifest.", manifest_bytes)
        temporaries.append(manifest_temp)
        read_cboe_vix_manifest(manifest_temp)
        notify("manifest_validated")

        _publish_create_only(source_temp, source_path)
        published.append(source_path)
        notify("bounded_source_published")
        _publish_create_only(canonical_temp, canonical_path)
        published.append(canonical_path)
        notify("canonical_published")
        _publish_create_only(manifest_temp, manifest_path)
        published.append(manifest_path)
        notify("manifest_published")
    except BaseException:
        for path in reversed(published):
            _cleanup_owned(path)
        raise
    finally:
        for path in temporaries:
            _cleanup_owned(path)
    return CboeVixPublishedAuthorityV1(
        root,
        source_path,
        canonical_path,
        manifest_path,
        source_digest,
        canonical_digest,
        authority_digest,
        str(manifest["manifest_semantic_digest"]),
    )


def verify_published_cboe_vix_authority(root: Path) -> CboeVixPublishedAuthorityV1:
    source_path = root / CBOE_VIX_BOUNDED_SOURCE_NAME
    canonical_path = root / CBOE_VIX_CANONICAL_NAME
    manifest_path = root / CBOE_VIX_MANIFEST_NAME
    manifest = read_cboe_vix_manifest(manifest_path)
    source_records = read_bounded_cboe_vix_source(source_path)
    canonical_rows = read_canonical_cboe_vix(canonical_path)
    source_digest = _digest(source_path.read_bytes())
    canonical_digest = _digest(canonical_path.read_bytes())
    if source_digest != manifest["bounded_source_digest"]:
        raise ValueError("published bounded Cboe VIX source digest mismatch")
    if canonical_digest != manifest["canonical_digest"]:
        raise ValueError("published canonical Cboe VIX digest mismatch")
    source_rows = tuple(_row_from_payload(record) for record in source_records[1:-1])
    if source_rows != canonical_rows:
        raise ValueError("bounded and canonical Cboe VIX rows mismatch")
    source_quality = dict(source_records[-1])
    source_quality.pop("kind", None)
    if source_quality != manifest["quality"]:
        raise ValueError("bounded and manifest Cboe VIX quality mismatch")
    semantic = {
        key: manifest[key]
        for key in (
            "authority_identity",
            "authorized_end",
            "authorized_start",
            "bounded_source_digest",
            "calendar_authority",
            "canonical_digest",
            "frequency",
            "provider",
            "quality",
            "source_identity",
            "staleness_policy",
            "units",
            "usable_at_policy",
        )
    }
    authority_digest = _digest(_json_bytes(semantic))
    if authority_digest != manifest["semantic_authority_digest"]:
        raise ValueError("published Cboe VIX semantic authority digest mismatch")
    return CboeVixPublishedAuthorityV1(
        root,
        source_path,
        canonical_path,
        manifest_path,
        source_digest,
        canonical_digest,
        authority_digest,
        str(manifest["manifest_semantic_digest"]),
    )
