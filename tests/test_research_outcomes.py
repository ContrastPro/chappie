from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from app.research_data import ResearchStore
from app.research_outcomes import build_outcome_cache, iter_outcome_batches


def _seed(
    path: Path, closes: list[float], *, split_at: int | None = None, state: str = "VERIFIED"
) -> None:
    store = ResearchStore(path)
    store.initialize()
    c = store.connection
    rows = []
    for index, close in enumerate(closes):
        segment = 0 if split_at is None or index < split_at else 1
        rows.append(
            (
                "BTCUSDT",
                index * 300000,
                segment,
                0,
                None,
                1,
                0,
                close,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
            )
        )
    c.executemany(
        f"INSERT INTO intelligence_observations_v1 VALUES ({','.join('?' for _ in rows[0])})", rows
    )
    digest = hashlib.sha256()
    for row in c.execute("SELECT * FROM intelligence_observations_v1 ORDER BY asset,timestamp_ms"):
        digest.update(
            json.dumps(tuple(row), separators=(",", ":"), ensure_ascii=True).encode() + b"\n"
        )
    c.execute(
        """INSERT INTO aligned_manifest_v1(asset,state,alignment_schema,flow_dataset,basis_dataset,flow_content_digest,basis_content_digest,flow_source_sha256,basis_source_sha256,build_config_digest,content_digest)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        ("BTCUSDT", state, "v", "f", "b", "a", "b", "c", "d", "e", digest.hexdigest()),
    )
    c.commit()
    store.close()


def _outcome(path: Path, timestamp: int, horizon: int = 60):
    c = sqlite3.connect(path)
    row = c.execute(
        "SELECT * FROM intelligence_outcomes_v1 WHERE asset='BTCUSDT' AND timestamp_ms=? AND horizon_minutes=?",
        (timestamp, horizon),
    ).fetchone()
    c.close()
    return row


def test_rise_then_reversal_extrema_thresholds_and_exact_endpoint(tmp_path: Path) -> None:
    path = tmp_path / "r.sqlite"
    _seed(path, [100 + index for index in range(12)] + [99] * 16)
    result = build_outcome_cache(store_path=path, asset="BTCUSDT")
    assert result.availability and result.availability[60] == (16, 12)
    row = _outcome(path, 0)
    assert row[4] == pytest.approx(-100)
    assert row[5] == pytest.approx(1100)
    assert row[6] == pytest.approx(-100)
    assert row[7:13] == (1, 1, 1, 1, 1, 0)
    assert _outcome(path, 20 * 300000)[13:15] == (0, "SEGMENT_END")


def test_segment_end_no_crossing_cache_reuse_and_invalidation(tmp_path: Path) -> None:
    path = tmp_path / "r.sqlite"
    _seed(path, [100] * 30, split_at=15)
    first = build_outcome_cache(store_path=path, asset="BTCUSDT")
    assert first.availability and first.availability[60] == (6, 24)
    assert _outcome(path, 0)[13:15] == (1, None)
    assert _outcome(path, 4 * 300000)[13:15] == (0, "SEGMENT_END")
    assert build_outcome_cache(store_path=path, asset="BTCUSDT").state == "REUSED"
    c = sqlite3.connect(path)
    c.execute("UPDATE aligned_manifest_v1 SET content_digest='changed' WHERE asset='BTCUSDT'")
    c.commit()
    c.close()
    with pytest.raises(ValueError, match="digest mismatch"):
        build_outcome_cache(store_path=path, asset="BTCUSDT")


def test_falling_then_rebound_preserves_negative_mae_and_positive_touch(tmp_path: Path) -> None:
    path = tmp_path / "r.sqlite"
    _seed(path, [100] + list(range(99, 88, -1)) + [102] + [102] * 12)
    build_outcome_cache(store_path=path, asset="BTCUSDT")
    row = _outcome(path, 0)
    assert row[4] == pytest.approx(200)
    assert row[5] == pytest.approx(200)
    assert row[6] == pytest.approx(-1100)
    assert row[7:13] == (1, 1, 1, 1, 1, 1)


def test_unverified_input_and_selected_ordered_batches(tmp_path: Path) -> None:
    path = tmp_path / "r.sqlite"
    _seed(path, [100] * 20, state="IN_PROGRESS")
    with pytest.raises(ValueError, match="VERIFIED"):
        build_outcome_cache(store_path=path, asset="BTCUSDT")
    c = sqlite3.connect(path)
    c.execute("UPDATE aligned_manifest_v1 SET state='VERIFIED' WHERE asset='BTCUSDT'")
    c.commit()
    c.close()
    build_outcome_cache(store_path=path, asset="BTCUSDT")
    rows = [
        row
        for batch in iter_outcome_batches(
            store_path=path,
            asset="BTCUSDT",
            horizon_minutes=60,
            columns=("timestamp_ms", "outcome_available"),
            batch_size=2,
        )
        for row in batch
    ]
    assert [tuple(row) for row in rows] == sorted(tuple(row) for row in rows)
