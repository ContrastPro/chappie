from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from app.research.capital_flow import short_horizon_direction_v2_materializer as materializer
from app.research.capital_flow.short_horizon_direction_v2_materializer import (
    CAPITAL_FLOW_FEATURE_ORDER,
    FINAL_FEATURE_ORDER,
    IDENTITY,
    NEW_FEATURES,
    STATE_AVAILABLE,
    STATE_CODES,
    VALIDATION_FEATURE_STORE_IDENTITY,
    ValidationFeatureStoreV2,
    verify_validation_feature_manifest,
)


def _manifest() -> dict[str, object]:
    return ValidationFeatureStoreV2(
        row_count=1,
        first_timestamp="2025-06-01T00:00:00+00:00",
        last_timestamp="2025-06-01T00:00:00+00:00",
    ).manifest()


def test_validation_feature_manifest_round_trip_is_frozen() -> None:
    manifest = _manifest()
    assert verify_validation_feature_manifest(manifest) == manifest
    assert manifest["identity"] == VALIDATION_FEATURE_STORE_IDENTITY
    assert manifest["identity"] != IDENTITY


@pytest.mark.parametrize("field,value", [("identity", IDENTITY), ("phase", "RESEARCH"), ("start", "2025-05-31T00:00:00+00:00")])
def test_validation_feature_manifest_rejects_contract_drift(field: str, value: object) -> None:
    manifest = deepcopy(_manifest())
    manifest[field] = value
    with pytest.raises(ValueError):
        verify_validation_feature_manifest(manifest)


def test_validation_feature_manifest_rejects_digest_and_boundary_tampering() -> None:
    manifest = deepcopy(_manifest())
    manifest["last_timestamp"] = "2026-06-01T00:00:00+00:00"
    with pytest.raises(ValueError):
        verify_validation_feature_manifest(manifest)
    manifest = deepcopy(_manifest())
    manifest["manifest_semantic_digest"] = "0" * 64
    with pytest.raises(ValueError, match="digest"):
        verify_validation_feature_manifest(manifest)


def test_validation_matrix_is_bounded_and_uses_frozen_column_order(monkeypatch: pytest.MonkeyPatch) -> None:
    start = datetime(2025, 6, 1, tzinfo=UTC)
    end = start + timedelta(minutes=3)
    timestamps = pd.date_range(start, periods=5, freq="min", tz="UTC")
    minute = pd.DataFrame({"close": 1.0}, index=timestamps)
    minute.index.name = "timestamp_utc"
    new = pd.DataFrame(
        {feature.name: np.arange(len(timestamps), dtype=float) for feature in NEW_FEATURES},
        index=timestamps,
    )

    def capital_flow(index: pd.DatetimeIndex, _root: object) -> pd.DataFrame:
        values: dict[str, object] = {}
        for feature in CAPITAL_FLOW_FEATURE_ORDER:
            values[feature] = 1.0
            values[f"{feature}__state"] = STATE_CODES[STATE_AVAILABLE]
        return pd.DataFrame(values, index=index)

    monkeypatch.setattr(materializer, "VALIDATION_START", start)
    monkeypatch.setattr(materializer, "VALIDATION_END_EXCLUSIVE", end)
    monkeypatch.setattr(materializer, "_load_validation_btc_minute_source", lambda _root: minute)
    monkeypatch.setattr(materializer, "build_new_features", lambda _minute: new)
    monkeypatch.setattr(materializer, "_load_validation_capital_flow_features", capital_flow)

    matrix = materializer.build_validation_feature_matrix()

    assert tuple(matrix.columns) == tuple(materializer._projection_columns(FINAL_FEATURE_ORDER))
    assert matrix["timestamp_utc"].tolist() == list(timestamps[:3])
    assert (matrix["timestamp_utc"] >= pd.Timestamp(start)).all()
    assert (matrix["timestamp_utc"] < pd.Timestamp(end)).all()


def _small_validation_matrix(start: datetime) -> pd.DataFrame:
    timestamps = pd.date_range(start, periods=3, freq="min", tz="UTC")
    values: dict[str, object] = {"timestamp_utc": timestamps}
    for feature in FINAL_FEATURE_ORDER:
        values[feature] = 1.0
        values[f"{feature}__state"] = STATE_CODES[STATE_AVAILABLE]
    return pd.DataFrame(values, columns=materializer._projection_columns(FINAL_FEATURE_ORDER))


def test_validation_publication_reloads_exact_matrix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    start = datetime(2025, 6, 1, tzinfo=UTC)
    end = start + timedelta(minutes=3)
    matrix = _small_validation_matrix(start)
    monkeypatch.setattr(materializer, "VALIDATION_START", start)
    monkeypatch.setattr(materializer, "VALIDATION_END_EXCLUSIVE", end)
    monkeypatch.setattr(materializer, "build_validation_feature_matrix", lambda *_args: matrix)

    root = tmp_path / "validation"
    published = materializer.materialize_validation_only(root)
    verified = materializer.verify_validation_feature_store(root)

    assert published["reused"] is False
    assert verified["row_count"] == 3
    assert verified["first_timestamp"] == "2025-06-01T00:00:00Z"
    assert verified["last_timestamp"] == "2025-06-01T00:02:00Z"
    assert len(verified["partitions"]) == 1
    assert verify_validation_feature_manifest(verified) == verified
    assert published["dataset_semantic_digest"] == verified["dataset_semantic_digest"]
    assert materializer.materialize_validation_only(root)["reused"] is True


def test_validation_publication_rejects_pending_artifact(tmp_path: object) -> None:
    root = tmp_path / "validation"
    root.mkdir()
    with pytest.raises(FileExistsError, match="without a final canonical manifest"):
        materializer.materialize_validation_only(root)


def test_validation_publication_rejects_incompatible_existing_manifest(tmp_path: object) -> None:
    root = tmp_path / "validation"
    root.mkdir()
    (root / "manifest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="semantic digest"):
        materializer.materialize_validation_only(root)
