"""Fail-closed binding of the published V2 Validation feature and target authorities."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from .short_horizon_direction_v2_contract import (
    EXPERIMENT_IDENTITY,
    VALIDATION_END_EXCLUSIVE,
    VALIDATION_START,
)
from .short_horizon_direction_v2_materializer import (
    verify_validation_feature_store,
)
from .short_horizon_direction_v2_validation_targets import (
    VALIDATION_HORIZON,
    verify_validation_targets,
)

VALIDATION_BOUNDARY_IDENTITY = "CHAPPIE_SHORT_HORIZON_DIRECTION_V2_VALIDATION_BOUNDARY/1.0.0"
VALIDATION_BOUNDARY_ROOT = Path("E:/projects/chappie/data/research/derived/market_brain/short_horizon_direction_v2/validation_boundary")


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class ValidationBoundaryV2:
    feature_manifest: dict[str, object]
    target_manifest: dict[str, object]

    def payload(self) -> dict[str, object]:
        feature, target = self.feature_manifest, self.target_manifest
        if feature.get("source_authority_identity") != target.get("source_authority_identity") or feature.get(
            "source_authority_digest"
        ) != target.get("source_authority_digest"):
            raise ValueError("Validation authorities have incompatible source bindings")
        expected_terminal = int(VALIDATION_HORIZON / timedelta(minutes=1)) - 1
        if target.get("terminal_rows_excluded") != expected_terminal:
            raise ValueError("Validation terminal exclusion contract mismatch")
        if feature.get("row_count") != int((VALIDATION_END_EXCLUSIVE - VALIDATION_START) / timedelta(minutes=1)):
            raise ValueError("Validation feature row-count contract mismatch")
        if target.get("row_count") != int(feature["row_count"]) - expected_terminal:
            raise ValueError("Validation scored intersection row-count mismatch")
        if target.get("first_timestamp") != feature.get("first_timestamp") or target.get("last_timestamp") != "2026-05-31T23:00:00Z":
            raise ValueError("Validation feature/target timestamp alignment mismatch")
        return {
            "identity": VALIDATION_BOUNDARY_IDENTITY,
            "phase": "VALIDATION",
            "experiment_identity": EXPERIMENT_IDENTITY,
            "start": VALIDATION_START.isoformat(),
            "end_exclusive": VALIDATION_END_EXCLUSIVE.isoformat(),
            "horizon": "1h",
            "feature": {
                "identity": feature["identity"], "manifest_semantic_digest": feature["manifest_semantic_digest"],
                "schema_identity": feature["feature_schema_identity"], "semantic_digest": feature["feature_semantic_digest"],
                "row_count": feature["row_count"], "first_timestamp": feature["first_timestamp"], "last_timestamp": feature["last_timestamp"],
            },
            "target": {
                "identity": target["identity"], "manifest_semantic_digest": target["manifest_semantic_digest"],
                "semantic_identity": target["target_semantic_identity"], "semantic_digest": target["target_semantic_digest"],
                "row_count": target["row_count"], "first_timestamp": target["first_timestamp"], "last_timestamp": target["last_timestamp"],
                "terminal_rows_excluded": target["terminal_rows_excluded"],
            },
            "source_authority_identity": feature["source_authority_identity"],
            "source_authority_digest": feature["source_authority_digest"],
            "scored_intersection": {"row_count": target["row_count"], "first_timestamp": target["first_timestamp"], "last_timestamp": target["last_timestamp"]},
        }

    def manifest(self) -> dict[str, object]:
        payload = self.payload(); payload["boundary_semantic_digest"] = _digest(payload); return payload


def verify_validation_boundary(root: Path = VALIDATION_BOUNDARY_ROOT) -> dict[str, object]:
    manifest = json.loads((Path(root) / "manifest.json").read_text(encoding="utf-8"))
    claimed = manifest.pop("boundary_semantic_digest", None)
    if claimed != _digest(manifest): raise ValueError("Validation boundary digest mismatch")
    feature, target = verify_validation_feature_store(), verify_validation_targets()
    expected = ValidationBoundaryV2(feature, target).payload()
    if manifest != expected: raise ValueError("Validation boundary authority binding mismatch")
    return {**manifest, "boundary_semantic_digest": claimed}


def materialize_validation_boundary(root: Path = VALIDATION_BOUNDARY_ROOT) -> dict[str, object]:
    root = Path(root); path = root / "manifest.json"
    if path.exists(): return {"reused": True, **verify_validation_boundary(root)}
    if root.exists(): raise FileExistsError("Validation boundary root exists without canonical manifest")
    manifest = ValidationBoundaryV2(verify_validation_feature_store(), verify_validation_targets()).manifest()
    root.mkdir(parents=True); pending = root / "manifest.json.pending"; pending.write_bytes(_canonical(manifest)); os.replace(pending, path)
    return {"reused": False, **verify_validation_boundary(root)}
