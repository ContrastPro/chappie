from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.research_alignment import build_aligned_intelligence, iter_intelligence_batches
from app.research_data import ResearchStore


def _seed(path: Path, *, flow_state: str = "VERIFIED", basis_state: str = "VERIFIED") -> None:
    store = ResearchStore(path)
    store.initialize()
    c = store.connection
    for dataset, state in (("flow", flow_state), ("basis", basis_state)):
        c.execute(
            """INSERT INTO conversion_manifest(dataset,state,source_path,source_sha256,content_digest)
                     VALUES(?,?,?,?,?)""",
            (dataset, state, dataset, "s" * 64, "c" * 64),
        )
    c.executemany(
        "INSERT INTO flow_observations_v1 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                "flow",
                "BTCUSDT",
                0,
                0,
                100,
                None,
                None,
                None,
                0.5,
                0.1,
                None,
                2,
                10,
                20,
                1,
                2,
                3,
                0.6,
                0.7,
                0.8,
                0.001,
                "PRICE_UP_OI_UP",
                None,
                None,
            ),
            (
                "flow",
                "BTCUSDT",
                300000,
                0,
                101,
                None,
                None,
                None,
                0.5,
                0.1,
                None,
                2,
                11,
                21,
                1,
                2,
                3,
                0.6,
                0.7,
                0.8,
                0.001,
                "PRICE_UP_OI_UP",
                None,
                None,
            ),
            (
                "flow",
                "BTCUSDT",
                600000,
                1,
                102,
                None,
                None,
                None,
                0.5,
                0.1,
                None,
                2,
                12,
                22,
                1,
                2,
                3,
                0.6,
                0.7,
                0.8,
                0.001,
                "PRICE_UP_OI_UP",
                None,
                None,
            ),
        ],
    )
    c.executemany(
        "INSERT INTO basis_observations_v1 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("basis", "BTCUSDT", 0, 0, 100, 99, 101, None, None, None, None, None, "POSITIVE"),
            ("basis", "BTCUSDT", 600000, 1, 102, 101, 99, -2, None, None, None, None, "NEGATIVE"),
            ("basis", "BTCUSDT", 900000, 1, 103, 102, 98, -1, None, None, None, None, "NEGATIVE"),
        ],
    )
    c.commit()
    store.close()


def test_exact_flow_led_alignment_segments_cache_and_batch_reads(tmp_path: Path) -> None:
    path = tmp_path / "r.sqlite"
    _seed(path)
    result = build_aligned_intelligence(
        store_path=path, asset="BTCUSDT", flow_dataset="flow", basis_dataset="basis"
    )
    assert (
        result.state,
        result.rows,
        result.flow_basis_rows,
        result.flow_only_rows,
        result.aligned_segments,
    ) == ("VERIFIED", 3, 2, 1, 3)
    assert (
        build_aligned_intelligence(
            store_path=path, asset="BTCUSDT", flow_dataset="flow", basis_dataset="basis"
        ).state
        == "REUSED"
    )
    rows = [
        row
        for batch in iter_intelligence_batches(
            store_path=path,
            asset="BTCUSDT",
            columns=("timestamp_ms", "basis_available", "premium_bps"),
            batch_size=2,
        )
        for row in batch
    ]
    assert [(row[0], row[1], row[2]) for row in rows] == [
        (0, 1, 101.0),
        (300000, 0, None),
        (600000, 1, 99.0),
    ]
    assert list(
        iter_intelligence_batches(
            store_path=path, asset="BTCUSDT", start_ms=1, end_ms=600000, columns=("timestamp_ms",)
        )
    )
    connection = sqlite3.connect(path)
    digest = connection.execute(
        "SELECT content_digest FROM aligned_manifest_v1 WHERE asset='BTCUSDT'"
    ).fetchone()[0]
    # The basis-only 900000 row cannot create a synthetic intelligence row.
    assert connection.execute(
        "SELECT count(*) FROM intelligence_observations_v1 WHERE timestamp_ms=900000"
    ).fetchone() == (0,)
    connection.execute("DELETE FROM aligned_manifest_v1 WHERE asset='BTCUSDT'")
    connection.commit()
    connection.close()
    rebuilt = build_aligned_intelligence(
        store_path=path, asset="BTCUSDT", flow_dataset="flow", basis_dataset="basis"
    )
    assert rebuilt.state == "VERIFIED"
    connection = sqlite3.connect(path)
    assert connection.execute(
        "SELECT content_digest FROM aligned_manifest_v1 WHERE asset='BTCUSDT'"
    ).fetchone() == (digest,)
    connection.close()


def test_alignment_refuses_unverified_inputs_and_digest_dependency_rebuild(tmp_path: Path) -> None:
    path = tmp_path / "r.sqlite"
    _seed(path, basis_state="IN_PROGRESS")
    with pytest.raises(ValueError, match="VERIFIED"):
        build_aligned_intelligence(
            store_path=path, asset="BTCUSDT", flow_dataset="flow", basis_dataset="basis"
        )
    c = sqlite3.connect(path)
    c.execute("UPDATE conversion_manifest SET state='VERIFIED' WHERE dataset='basis'")
    c.commit()
    c.close()
    assert (
        build_aligned_intelligence(
            store_path=path, asset="BTCUSDT", flow_dataset="flow", basis_dataset="basis"
        ).state
        == "VERIFIED"
    )
    c = sqlite3.connect(path)
    c.execute("UPDATE conversion_manifest SET content_digest='d' WHERE dataset='basis'")
    c.commit()
    c.close()
    assert (
        build_aligned_intelligence(
            store_path=path, asset="BTCUSDT", flow_dataset="flow", basis_dataset="basis"
        ).state
        == "VERIFIED"
    )


def test_alignment_rejects_missing_verification_digest(tmp_path: Path) -> None:
    path = tmp_path / "r.sqlite"
    _seed(path)
    connection = sqlite3.connect(path)
    connection.execute("UPDATE conversion_manifest SET source_sha256='' WHERE dataset='flow'")
    connection.commit()
    connection.close()
    with pytest.raises(ValueError, match="incomplete"):
        build_aligned_intelligence(
            store_path=path, asset="BTCUSDT", flow_dataset="flow", basis_dataset="basis"
        )
