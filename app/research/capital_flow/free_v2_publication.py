"""Create-only, manifest-last publication for Capital Flow Free V2 feature rows."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from .free_v2_formulas import FEATURE_UNAVAILABLE_STATES
from .free_v2_materializer import (
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


def _record(item: CapitalFlowFeatureValueV2) -> dict[str, object]:
    return {**item.semantic_payload(), "family": item.family, "feature_digest": item.feature_digest}


def _row(
    item: dict[str, object], authorities: tuple[SourceAuthorityBindingV1, ...]
) -> CapitalFlowFeatureValueV2:
    from datetime import datetime
    from decimal import Decimal

    values = {
        "feature_name": item["feature_name"],
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


def _quality(rows: tuple[CapitalFlowFeatureValueV2, ...]) -> list[dict[str, object]]:
    result = []
    for name in FEATURE_ORDER:
        selected = tuple(x for x in rows if x.feature_name == name)
        counts = Counter(x.state for x in selected)
        available = tuple(x for x in selected if x.state == AVAILABLE)
        result.append(
            {
                "feature_name": name,
                "timeline_row_count": len(selected),
                "available_count": len(available),
                "unavailable_count": len(selected) - len(available),
                "first_available_timestamp": None
                if not available
                else available[0].timestamp_utc.isoformat(),
                "last_available_timestamp": None
                if not available
                else available[-1].timestamp_utc.isoformat(),
                **{state.lower() + "_count": counts[state] for state in FEATURE_UNAVAILABLE_STATES},
                "non_finite_count": 0,
                "causal_violation_count": 0,
            }
        )
    return result


def publish_feature_dataset(
    rows: Iterable[CapitalFlowFeatureValueV2],
    authorities: tuple[SourceAuthorityBindingV1, ...],
    root: Path = FREE_V2_PUBLICATION_ROOT,
    *,
    phase_hook: Callable[[str], None] | None = None,
) -> PublishedFeatureDatasetV1:
    ordered = tuple(rows)
    if (
        not ordered
        or len(ordered) % 15
        or any(
            tuple(x.feature_name for x in ordered[i : i + 15]) != FEATURE_ORDER
            for i in range(0, len(ordered), 15)
        )
    ):
        raise ValueError("feature rows must contain complete ordered 15-feature timestamps")
    timestamps = tuple(ordered[i].timestamp_utc for i in range(0, len(ordered), 15))
    if timestamps != tuple(sorted(timestamps)) or len(set(timestamps)) != len(timestamps):
        raise ValueError("feature timestamp order invalid")
    if any(x.source_authority_bindings != authorities for x in ordered):
        raise ValueError("authority binding mismatch")
    root.mkdir(parents=True, exist_ok=True)
    artifact = root / "features.ndjson"
    manifest_path = root / "manifest.json"
    if artifact.exists() or manifest_path.exists():
        raise FileExistsError("feature dataset already exists")
    payload = b"".join(_bytes(_record(x)) for x in ordered)
    artifact_digest = _sha(payload)
    quality = _quality(ordered)
    semantic = {
        **dataset_contract_payload(authorities),
        "artifact": {
            "file": "features.ndjson",
            "sha256": artifact_digest,
            "row_count": len(ordered),
        },
        "quality": quality,
    }
    dataset_digest = _sha(_bytes(semantic))
    manifest = {**semantic, "dataset_semantic_digest": dataset_digest}
    manifest["manifest_semantic_digest"] = _sha(_bytes(manifest))
    notify = phase_hook or (lambda _: None)
    pending = []
    published = []
    try:
        for final, data, label in (
            (artifact, payload, "artifact"),
            (manifest_path, _bytes(manifest, pretty=True), "manifest"),
        ):
            descriptor, name = tempfile.mkstemp(prefix=f".{label}.", suffix=".pending", dir=root)
            path = Path(name)
            pending.append(path)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if final == artifact:
                if _sha(path.read_bytes()) != artifact_digest:
                    raise ValueError("artifact reload digest mismatch")
                reloaded = tuple(
                    _row(json.loads(line), authorities) for line in path.read_text().splitlines()
                )
                if reloaded != ordered:
                    raise ValueError("artifact reload mismatch")
                notify("artifacts_verified")
            os.replace(path, final)
            published.append(final)
            if final == artifact:
                notify("artifacts_published")
        verified = verify_feature_dataset(root, authorities)
        notify("manifest_published")
        return verified
    except BaseException:
        for path in pending:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        for path in published:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def verify_feature_dataset(
    root: Path, authorities: tuple[SourceAuthorityBindingV1, ...]
) -> PublishedFeatureDatasetV1:
    manifest_path = root / "manifest.json"
    artifact = root / "features.ndjson"
    manifest = json.loads(manifest_path.read_text())
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
    info = manifest["artifact"]
    if _sha(artifact.read_bytes()) != info["sha256"]:
        raise ValueError("artifact digest mismatch")
    rows = tuple(_row(json.loads(line), authorities) for line in artifact.read_text().splitlines())
    if len(rows) != info["row_count"]:
        raise ValueError("artifact row count mismatch")
    dataset_digest = manifest.pop("dataset_semantic_digest")
    if dataset_digest != _sha(_bytes(manifest)):
        raise ValueError("dataset semantic digest mismatch")
    return PublishedFeatureDatasetV1(
        root, len(rows), info["sha256"], dataset_digest, manifest_digest
    )
