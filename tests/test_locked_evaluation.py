"""Acceptance-only validation for the immutable frozen EMA OOS lock."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.persistence.canonical import canonical_sha256
from app.research.strategy.locked_evaluation import IDENTITY, prepare_locked_replay

ROOT = Path(__file__).parents[1]
DATASET = ROOT / "data/research/ema_oos_btcusdt_1m_2025-01-01_2025-03-01.json"
MANIFEST = ROOT / "data/research/ema_oos_btcusdt_1m_2025-01-01_2025-03-01.pre_oos_manifest.json"
MANIFEST_DIGEST = "d19e609b0db1b4e52e55caf46347856ae16c5e718f3428ee38e79c6566793bec"


def _prepare(manifest: Path = MANIFEST, dataset: Path = DATASET):
    return prepare_locked_replay(manifest, dataset, expected_manifest_digest=MANIFEST_DIGEST)


def _alter_manifest(tmp_path: Path, path: str, value: object) -> Path:
    raw = json.loads(MANIFEST.read_text(encoding="utf-8"))
    target = raw
    keys = path.split(".")
    for key in keys[:-1]:
        target = target[key]
    target[keys[-1]] = value
    authority = {key: value for key, value in raw.items() if key != "manifest_digest"}
    raw["manifest_digest"] = canonical_sha256(
        authority, algorithm_version="frozen-strategy-evaluation/v1"
    )
    altered = tmp_path / "altered.json"
    altered.write_text(json.dumps(raw), encoding="utf-8")
    return altered


def test_locked_manifest_prepares_exact_oos_boundary_without_execution() -> None:
    prepared = _prepare()
    repeated = _prepare()
    assert prepared.manifest_digest == MANIFEST_DIGEST
    assert prepared.source_commit == "5a098a14db0aaafd4e410c038ec3c2fb0e1c97a2"
    assert prepared.dataset.metadata.candles_sha256 == (
        "acd39632b51ba6ad41dfd28bbdfe6b3d0821109dc3310ae959ca53bcdf671a3a"
    )
    assert prepared.evaluation_start_index == 67_968
    assert (
        prepared.dataset.candles[prepared.evaluation_start_index].open_time_ms == 1_739_767_680_000
    )
    assert prepared.strategy.name == "ema_crossover"
    assert prepared.config.fee_rate == Decimal("0.001")
    assert prepared.preparation_digest == repeated.preparation_digest
    assert (
        prepared.dataset.candles[prepared.evaluation_start_index - 1].open_time_ms
        < 1_739_767_680_000
    )


@pytest.mark.parametrize(
    ("path", "value"),
    (
        ("identity", "wrong/1.0.0"),
        ("created_before_oos", False),
        ("dataset.candles_sha256", "0" * 64),
        ("dataset.symbol", "ETHUSDT"),
        ("dataset.interval", "5m"),
        ("strategy.name", "other"),
        ("strategy.parameters.fast_window", 13),
        ("strategy.config_fingerprint", "0" * 64),
        ("splits.final_oos.first_open_utc", "2025-02-17T04:49:00Z"),
        ("splits.final_oos.last_close_utc", "2025-02-28T23:58:59.999000Z"),
        ("splits.final_oos.count", 16_991),
        ("evaluation.fee_rate_per_fill", "0.002"),
        ("evaluation.decision_policy.stop", "changed"),
    ),
)
def test_locked_manifest_rejects_any_changed_authority(
    tmp_path: Path, path: str, value: object
) -> None:
    with pytest.raises(ValueError):
        _prepare(_alter_manifest(tmp_path, path, value))


def test_locked_manifest_rejects_missing_exact_oos_boundary(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _prepare(
            _alter_manifest(tmp_path, "splits.final_oos.first_open_utc", "2025-03-01T00:00:00Z")
        )


def test_locked_manifest_rejects_insufficient_warmup(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _prepare(_alter_manifest(tmp_path, "splits.train.count", 1))


def test_manifest_identity_is_versioned() -> None:
    assert IDENTITY == "chappie_frozen_strategy_evaluation/1.0.0"
