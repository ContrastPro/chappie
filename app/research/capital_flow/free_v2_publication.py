"""Bounded-memory, create-only publication for Capital Flow Free V2 features."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import BinaryIO

from .free_v2_formulas import (
    FEATURE_TIMELINE_END_EXCLUSIVE,
    FEATURE_TIMELINE_MINUTES,
    FEATURE_TIMELINE_START,
    FEATURE_UNAVAILABLE_STATES,
)
from .free_v2_materializer import (
    _META,
    AVAILABLE,
    CAPITAL_FLOW_FREE_V2_FEATURE_DATASET_IDENTITY,
    FEATURE_ORDER,
    CapitalFlowFeatureValueV2,
    SourceAuthorityBindingV1,
    dataset_contract_payload,
)

FREE_V2_PUBLICATION_ROOT = Path("data/research/derived/capital_flow/free_v2")


def _bytes(value: object, *, pretty: bool = False) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
        )
        + "\n"
    ).encode()


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _record(item: CapitalFlowFeatureValueV2) -> dict[str, object]:
    return {**item.semantic_payload(), "family": item.family, "feature_digest": item.feature_digest}


def _row(
    item: dict[str, object], authorities: tuple[SourceAuthorityBindingV1, ...]
) -> CapitalFlowFeatureValueV2:
    expected_sources = [[value.identity, value.semantic_digest] for value in authorities]
    if item.get("source_authorities") != expected_sources:
        raise ValueError("row authority binding mismatch")
    feature_name = str(item["feature_name"])
    if feature_name not in _META:
        raise ValueError("unknown feature name")
    family, formula, unit = _META[feature_name]
    if (item.get("family"), item.get("formula_identity"), item.get("unit")) != (
        family,
        formula,
        unit,
    ):
        raise ValueError("frozen feature metadata mismatch")
    values = {
        "feature_name": feature_name,
        "family": item["family"],
        "timestamp_utc": datetime.fromisoformat(str(item["timestamp_utc"])),
        "value": None if item["value"] is None else Decimal(str(item["value"])),
        "state": item["state"],
        "unit": item["unit"],
        "formula_identity": item["formula_identity"],
        "source_authority_bindings": authorities,
        "input_causal_identities": tuple(item["input_causal_identities"]),
        "window_identity": item["window_identity"],
        "window_start_utc": None
        if item["window_start_utc"] is None
        else datetime.fromisoformat(str(item["window_start_utc"])),
        "window_end_utc": datetime.fromisoformat(str(item["window_end_utc"])),
        "feature_digest": item["feature_digest"],
    }
    return CapitalFlowFeatureValueV2(**values)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class PublishedFeatureDatasetV1:
    root: Path
    row_count: int
    artifact_digest: str
    dataset_semantic_digest: str
    manifest_semantic_digest: str


@dataclass(slots=True)
class StreamingQualityV1:
    """Incremental quality evidence with memory independent of timeline length."""

    counts: dict[str, dict[str, int]] = field(
        default_factory=lambda: {
            name: {state: 0 for state in (AVAILABLE, *FEATURE_UNAVAILABLE_STATES)}
            for name in FEATURE_ORDER
        }
    )
    first_available: dict[str, str | None] = field(
        default_factory=lambda: dict.fromkeys(FEATURE_ORDER)
    )
    last_available: dict[str, str | None] = field(
        default_factory=lambda: dict.fromkeys(FEATURE_ORDER)
    )
    non_finite: dict[str, int] = field(default_factory=lambda: dict.fromkeys(FEATURE_ORDER, 0))
    causal_violations: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(FEATURE_ORDER, 0)
    )

    def add(self, row: CapitalFlowFeatureValueV2) -> None:
        self.counts[row.feature_name][row.state] += 1
        if row.state == AVAILABLE:
            stamp = row.timestamp_utc.isoformat()
            self.first_available[row.feature_name] = self.first_available[row.feature_name] or stamp
            self.last_available[row.feature_name] = stamp
            if row.value is None or not row.value.is_finite():
                self.non_finite[row.feature_name] += 1
        if row.window_end_utc > row.timestamp_utc or (
            row.window_start_utc is not None and row.window_start_utc > row.timestamp_utc
        ):
            self.causal_violations[row.feature_name] += 1

    def payload(self) -> list[dict[str, object]]:
        result = []
        for name in FEATURE_ORDER:
            counts = self.counts[name]
            total = sum(counts.values())
            result.append(
                {
                    "feature_name": name,
                    "timeline_row_count": total,
                    "available_count": counts[AVAILABLE],
                    "unavailable_count": total - counts[AVAILABLE],
                    "first_available_timestamp": self.first_available[name],
                    "last_available_timestamp": self.last_available[name],
                    **{
                        state.lower() + "_count": counts[state]
                        for state in FEATURE_UNAVAILABLE_STATES
                    },
                    "non_finite_count": self.non_finite[name],
                    "causal_violation_count": self.causal_violations[name],
                }
            )
        return result


@dataclass(slots=True)
class StreamEvidenceV1:
    digest: object = field(default_factory=hashlib.sha256)
    quality: StreamingQualityV1 = field(default_factory=StreamingQualityV1)
    row_count: int = 0
    minute_count: int = 0
    first_timestamp: datetime | None = None
    last_timestamp: datetime | None = None


def write_feature_rows(
    rows: Iterable[CapitalFlowFeatureValueV2],
    handle: BinaryIO,
    authorities: tuple[SourceAuthorityBindingV1, ...],
) -> StreamEvidenceV1:
    """Serialize and validate rows incrementally without retaining prior rows."""
    evidence = StreamEvidenceV1()
    expected_index = 0
    current_timestamp: datetime | None = None
    for row in rows:
        if row.source_authority_bindings != authorities:
            raise ValueError("authority binding mismatch")
        if row.feature_name != FEATURE_ORDER[expected_index]:
            raise ValueError("feature rows must contain complete ordered 15-feature timestamps")
        if expected_index == 0:
            if current_timestamp is not None and row.timestamp_utc != current_timestamp + timedelta(
                minutes=1
            ):
                raise ValueError("feature timestamp continuity invalid")
            current_timestamp = row.timestamp_utc
            evidence.first_timestamp = evidence.first_timestamp or current_timestamp
            evidence.last_timestamp = current_timestamp
            evidence.minute_count += 1
        elif row.timestamp_utc != current_timestamp:
            raise ValueError("partial or mixed feature timestamp")
        payload = _bytes(_record(row))
        handle.write(payload)
        evidence.digest.update(payload)  # type: ignore[attr-defined]
        evidence.quality.add(row)
        evidence.row_count += 1
        expected_index = (expected_index + 1) % len(FEATURE_ORDER)
    if not evidence.row_count or expected_index:
        raise ValueError("feature rows must contain complete ordered 15-feature timestamps")
    return evidence


def iter_feature_rows(
    artifact: Path, authorities: tuple[SourceAuthorityBindingV1, ...]
) -> Iterator[CapitalFlowFeatureValueV2]:
    with artifact.open("r", encoding="utf-8") as handle:
        for line in handle:
            yield _row(json.loads(line), authorities)


def _manifest(
    authorities: tuple[SourceAuthorityBindingV1, ...], evidence: StreamEvidenceV1
) -> dict[str, object]:
    semantic = {
        **dataset_contract_payload(authorities),
        "artifact": {
            "file": "features.ndjson",
            "sha256": evidence.digest.hexdigest(),  # type: ignore[attr-defined]
            "row_count": evidence.row_count,
        },
        "quality": evidence.quality.payload(),
    }
    dataset_digest = _sha(_bytes(semantic))
    manifest = {**semantic, "dataset_semantic_digest": dataset_digest}
    manifest["manifest_semantic_digest"] = _sha(_bytes(manifest))
    return manifest


def verify_feature_artifact(
    artifact: Path, authorities: tuple[SourceAuthorityBindingV1, ...]
) -> StreamEvidenceV1:
    digest = hashlib.sha256()
    quality = StreamingQualityV1()
    row_count = minute_count = expected_index = 0
    first: datetime | None = None
    previous: datetime | None = None
    with artifact.open("rb") as handle:
        for raw in handle:
            digest.update(raw)
            row = _row(json.loads(raw), authorities)
            if row.feature_name != FEATURE_ORDER[expected_index]:
                raise ValueError("feature order mismatch")
            if expected_index == 0:
                if previous is not None and row.timestamp_utc != previous + timedelta(minutes=1):
                    raise ValueError("feature timestamp continuity invalid")
                previous = row.timestamp_utc
                first = first or previous
                minute_count += 1
            elif row.timestamp_utc != previous:
                raise ValueError("partial or mixed feature timestamp")
            quality.add(row)
            row_count += 1
            expected_index = (expected_index + 1) % len(FEATURE_ORDER)
    if not row_count or expected_index:
        raise ValueError("partial feature timestamp")
    return StreamEvidenceV1(digest, quality, row_count, minute_count, first, previous)


def publish_feature_dataset(
    rows: Iterable[CapitalFlowFeatureValueV2],
    authorities: tuple[SourceAuthorityBindingV1, ...],
    root: Path = FREE_V2_PUBLICATION_ROOT,
    *,
    phase_hook: Callable[[str], None] | None = None,
) -> PublishedFeatureDatasetV1:
    """Publish a deterministic artifact using bounded memory and manifest-last semantics."""
    if root.exists():
        raise FileExistsError("feature dataset already exists")
    root.mkdir(parents=True)
    artifact = root / "features.ndjson"
    manifest_path = root / "manifest.json"
    notify = phase_hook or (lambda _: None)
    pending: list[Path] = []
    published: list[Path] = []
    try:
        descriptor, name = tempfile.mkstemp(prefix=".artifact.", suffix=".pending", dir=root)
        artifact_pending = Path(name)
        pending.append(artifact_pending)
        with os.fdopen(descriptor, "wb") as handle:
            evidence = write_feature_rows(rows, handle, authorities)
            handle.flush()
            os.fsync(handle.fileno())
        verified = verify_feature_artifact(artifact_pending, authorities)
        if verified.digest.hexdigest() != evidence.digest.hexdigest():  # type: ignore[attr-defined]
            raise ValueError("artifact reload digest mismatch")
        notify("artifacts_verified")
        os.replace(artifact_pending, artifact)
        published.append(artifact)
        notify("artifacts_published")
        manifest = _manifest(authorities, evidence)
        descriptor, name = tempfile.mkstemp(prefix=".manifest.", suffix=".pending", dir=root)
        manifest_pending = Path(name)
        pending.append(manifest_pending)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_bytes(manifest, pretty=True))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(manifest_pending, manifest_path)
        published.append(manifest_path)
        result = verify_feature_dataset(root, authorities)
        notify("manifest_published")
        return result
    except BaseException:
        for path in pending:
            path.unlink(missing_ok=True)
        for path in published:
            path.unlink(missing_ok=True)
        try:
            root.rmdir()
        except OSError:
            pass
        raise


def verify_feature_dataset(
    root: Path,
    authorities: tuple[SourceAuthorityBindingV1, ...],
    *,
    require_full_timeline: bool = False,
) -> PublishedFeatureDatasetV1:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    manifest_digest = manifest.pop("manifest_semantic_digest", None)
    if manifest_digest != _sha(_bytes(manifest)):
        raise ValueError("manifest semantic digest mismatch")
    if manifest.get("dataset_identity") != CAPITAL_FLOW_FREE_V2_FEATURE_DATASET_IDENTITY:
        raise ValueError("dataset identity mismatch")
    if manifest.get("feature_order") != list(FEATURE_ORDER):
        raise ValueError("feature order mismatch")
    if manifest.get("source_authorities") != [
        list(x) for x in [(a.identity, a.semantic_digest) for a in authorities]
    ]:
        raise ValueError("source authority mismatch")
    contract = dataset_contract_payload(authorities)
    if any(manifest.get(key) != json.loads(json.dumps(value)) for key, value in contract.items()):
        raise ValueError("dataset contract mismatch")
    info = manifest["artifact"]
    artifact = root / "features.ndjson"
    if _sha_file(artifact) != info["sha256"]:
        raise ValueError("artifact digest mismatch")
    evidence = verify_feature_artifact(artifact, authorities)
    if evidence.digest.hexdigest() != info["sha256"]:  # type: ignore[attr-defined]
        raise ValueError("artifact digest mismatch")
    if evidence.row_count != info["row_count"]:
        raise ValueError("artifact row count mismatch")
    if evidence.quality.payload() != manifest["quality"]:
        raise ValueError("quality evidence mismatch")
    if require_full_timeline and (
        evidence.minute_count != FEATURE_TIMELINE_MINUTES
        or evidence.row_count != FEATURE_TIMELINE_MINUTES * len(FEATURE_ORDER)
        or evidence.first_timestamp != FEATURE_TIMELINE_START
        or evidence.last_timestamp != FEATURE_TIMELINE_END_EXCLUSIVE - timedelta(minutes=1)
    ):
        raise ValueError("full feature timeline contract mismatch")
    dataset_digest = manifest.pop("dataset_semantic_digest")
    if dataset_digest != _sha(_bytes(manifest)):
        raise ValueError("dataset semantic digest mismatch")
    return PublishedFeatureDatasetV1(
        root, evidence.row_count, info["sha256"], dataset_digest, manifest_digest
    )
