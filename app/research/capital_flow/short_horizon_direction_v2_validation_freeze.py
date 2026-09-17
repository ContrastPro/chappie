"""Immutable pre-Validation candidate freeze for the two released V2 1h candidates."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .short_horizon_direction_v2_fit import (
    FROZEN_FAMILY_COMBINATION_PROJECTIONS,
    FULL_V2_FEATURE_ORDER,
    LOGISTIC_BASELINE_CONFIGURATION,
    V2_SCIENTIFIC_FIT_IDENTITY,
)
from .short_horizon_direction_v2_validation_boundary import (
    VALIDATION_BOUNDARY_IDENTITY,
    VALIDATION_BOUNDARY_ROOT,
    verify_validation_boundary,
)

VALIDATION_CANDIDATE_FREEZE_IDENTITY = "CHAPPIE_SHORT_HORIZON_DIRECTION_V2_VALIDATION_CANDIDATE_FREEZE/1.0.0"
VALIDATION_CANDIDATE_FREEZE_ROOT = Path("E:/projects/chappie/data/research/derived/market_brain/short_horizon_direction_v2/validation_candidate_freeze")


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def frozen_validation_candidates() -> list[dict[str, object]]:
    return [
        {"identity": "FULL_V2/1h", "projection": list(FULL_V2_FEATURE_ORDER), "fit_balanced_accuracy": 0.354886, "fit_delta_vs_baseline": 0.021553},
        {"identity": "TECHNICAL+VOLUME/1h", "projection": list(FROZEN_FAMILY_COMBINATION_PROJECTIONS["TECHNICAL+VOLUME"]), "fit_balanced_accuracy": 0.351038, "fit_delta_vs_baseline": 0.017704},
    ]


def candidate_freeze_payload(boundary: dict[str, object]) -> dict[str, object]:
    if boundary.get("identity") != VALIDATION_BOUNDARY_IDENTITY:
        raise ValueError("Validation candidate freeze requires exact boundary identity")
    return {
        "identity": VALIDATION_CANDIDATE_FREEZE_IDENTITY,
        "selection_completed_before_validation": True,
        "candidate_mutation_prohibited": True,
        "horizon": "1h",
        "fit_identity": V2_SCIENTIFIC_FIT_IDENTITY,
        "model": "logistic_baseline",
        "model_configuration": list(LOGISTIC_BASELINE_CONFIGURATION),
        "candidates": frozen_validation_candidates(),
        "validation_boundary_identity": boundary["identity"],
        "validation_boundary_semantic_digest": boundary["boundary_semantic_digest"],
        "feature_schema_identity": boundary["feature"]["schema_identity"],
        "feature_semantic_digest": boundary["feature"]["semantic_digest"],
        "target_semantic_identity": boundary["target"]["semantic_identity"],
        "target_semantic_digest": boundary["target"]["semantic_digest"],
        "validation_metrics": None,
        "blind_oos_accessed": False,
    }


def materialize_validation_candidate_freeze(root: Path = VALIDATION_CANDIDATE_FREEZE_ROOT) -> dict[str, object]:
    root, path = Path(root), Path(root) / "manifest.json"
    boundary = verify_validation_boundary(VALIDATION_BOUNDARY_ROOT)
    payload = candidate_freeze_payload(boundary)
    payload["semantic_digest"] = hashlib.sha256(_canonical(payload)).hexdigest()
    if path.exists():
        current = json.loads(path.read_text(encoding="utf-8"))
        if current != payload: raise ValueError("incompatible Validation candidate freeze already exists")
        return {"reused": True, **current}
    if root.exists(): raise FileExistsError("candidate freeze root exists without canonical manifest")
    root.mkdir(parents=True); pending = root / "manifest.json.pending"; pending.write_bytes(_canonical(payload)); os.replace(pending, path)
    return {"reused": False, **payload}
