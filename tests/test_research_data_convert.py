from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.tools.research_data_convert import BATCH_SIZE, _basis, _flow, convert


def _basis_row(timestamp_ms: int) -> dict[str, object]:
    return {
        "symbol": "BTCUSDT",
        "timestamp_ms": timestamp_ms,
        "mark_close": "100.0",
        "index_close": "99.9",
        "premium_bps": "10.0",
        "premium_change_5m": "0",
        "premium_change_15m": "0",
        "premium_change_1h": "0",
        "rolling_mean_bps": "10.0",
        "rolling_percentile": "0.5",
        "state": "NEUTRAL",
    }


def _write_basis(path: Path, rows: int) -> None:
    path.write_text(
        json.dumps(
            {
                "format": "chappie-leverage-stress-basis/v1",
                "rows": [
                    _basis_row(1_700_000_000_000 + (index * 300_000)) for index in range(rows)
                ],
            }
        ),
        encoding="utf-8",
    )


def test_conversion_is_verified_and_reused(tmp_path: Path) -> None:
    source = tmp_path / "basis.json"
    store = tmp_path / "research.sqlite3"
    _write_basis(source, 2)

    result = convert(store_path=store, source=source, dataset="basis_fixture", kind="basis")
    assert result["state"] == "VERIFIED"
    assert result["rows"] == 2
    assert convert(store_path=store, source=source, dataset="basis_fixture", kind="basis") == {
        "dataset": "basis_fixture",
        "state": "REUSED",
    }

    connection = sqlite3.connect(store)
    manifest = connection.execute(
        "SELECT state, inserted_rows, content_digest FROM conversion_manifest WHERE dataset=?",
        ("basis_fixture",),
    ).fetchone()
    assert manifest == ("VERIFIED", 2, manifest[2])
    assert manifest[2] and len(manifest[2]) == 64
    assert connection.execute("SELECT count(*) FROM basis_observations_v1").fetchone() == (2,)
    connection.close()


def test_committed_chunk_resumes_without_duplicate_rows(tmp_path: Path) -> None:
    source = tmp_path / "basis.json"
    store = tmp_path / "research.sqlite3"
    _write_basis(source, BATCH_SIZE + 2)

    partial = convert(
        store_path=store,
        source=source,
        dataset="basis_resume",
        kind="basis",
        max_chunks=1,
    )
    assert partial["state"] == "IN_PROGRESS"
    assert partial["rows"] == BATCH_SIZE

    completed = convert(store_path=store, source=source, dataset="basis_resume", kind="basis")
    assert completed["state"] == "VERIFIED"
    assert completed["rows"] == BATCH_SIZE + 2
    connection = sqlite3.connect(store)
    assert connection.execute("SELECT count(*) FROM basis_observations_v1").fetchone() == (
        BATCH_SIZE + 2,
    )
    assert connection.execute(
        "SELECT count(*) FROM conversion_manifest WHERE dataset=? AND state='VERIFIED'",
        ("basis_resume",),
    ).fetchone() == (1,)
    connection.close()


def test_changed_source_invalidates_checkpoint(tmp_path: Path) -> None:
    source = tmp_path / "basis.json"
    store = tmp_path / "research.sqlite3"
    _write_basis(source, BATCH_SIZE + 1)
    convert(
        store_path=store,
        source=source,
        dataset="basis_changed",
        kind="basis",
        max_chunks=1,
    )
    _write_basis(source, 2)

    result = convert(store_path=store, source=source, dataset="basis_changed", kind="basis")
    assert result["state"] == "VERIFIED"
    assert result["rows"] == 2
    connection = sqlite3.connect(store)
    assert connection.execute("SELECT count(*) FROM basis_observations_v1").fetchone() == (2,)
    connection.close()


def test_malformed_later_row_is_failed_not_verified(tmp_path: Path) -> None:
    source = tmp_path / "basis.json"
    store = tmp_path / "research.sqlite3"
    rows = [_basis_row(1_700_000_000_000 + (index * 300_000)) for index in range(BATCH_SIZE)]
    rows.append({"symbol": "BTCUSDT", "timestamp_ms": 1_700_300_000_000})
    source.write_text(
        json.dumps({"format": "chappie-leverage-stress-basis/v1", "rows": rows}), encoding="utf-8"
    )

    with pytest.raises(KeyError):
        convert(store_path=store, source=source, dataset="basis_bad", kind="basis")
    connection = sqlite3.connect(store)
    assert connection.execute(
        "SELECT state FROM conversion_manifest WHERE dataset=?", ("basis_bad",)
    ).fetchone() == ("FAILED",)
    assert connection.execute("SELECT count(*) FROM basis_observations_v1").fetchone() == (
        BATCH_SIZE,
    )
    connection.close()


def test_mappers_preserve_nulls_and_numeric_values() -> None:
    flow = _flow(
        {
            "symbol": "BTCUSDT",
            "timestamp_ms": 1,
            "close": "100.25",
            "spot": {"quote_volume": None, "trade_count": 2},
            "metrics": {"open_interest": "12.5"},
        },
        "flow_fixture",
        0,
    )
    basis = _basis(
        {
            "symbol": "BTCUSDT",
            "timestamp_ms": 1,
            "mark_close": "100.0",
            "index_close": "99.5",
            "premium_bps": "50.251",
        },
        "basis_fixture",
        0,
    )
    assert flow[4] == pytest.approx(100.25)
    assert flow[5] is None
    assert flow[10] == 2
    assert flow[12] == pytest.approx(12.5)
    assert basis[4:7] == pytest.approx((100.0, 99.5, 50.251))
