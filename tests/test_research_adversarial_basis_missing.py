"""Exact-join and missing-basis attacks for frozen compact research data."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.research.data.alignment import build_aligned_intelligence, iter_intelligence_batches
from app.research.data.event_bridge import _causal_snapshots, extract_events
from app.research.data.outcomes import build_outcome_cache
from app.research.data.store import ResearchStore
from app.research_methodology import (
    METHODOLOGY_NAME,
    METHODOLOGY_VERSION,
    methodology_digest,
)
from tests.test_research_outcomes import _outcome

FIVE_MINUTES_MS = 300_000


def _seed_basis_availability(path: Path) -> None:
    """Continuous flow; basis is present at 0..13 and 29..39 only."""
    store = ResearchStore(path)
    store.initialize()
    connection = store.connection
    for dataset in ("flow", "basis"):
        connection.execute(
            """INSERT INTO conversion_manifest(dataset,state,source_path,source_sha256,content_digest)
               VALUES(?,?,?,?,?)""",
            (dataset, "VERIFIED", dataset, dataset * 16, dataset * 16),
        )
    connection.executemany(
        "INSERT INTO flow_observations_v1 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                "flow",
                "BTCUSDT",
                index * FIVE_MINUTES_MS,
                0,
                100 + index,
                None,
                None,
                None,
                1.0,
                0.5,
                None,
                1,
                10,
                20,
                1,
                2,
                3,
                0.6,
                0.7,
                0.8,
                -0.001 if 26 <= index < 29 else None,
                "PRICE_DOWN_OI_UP" if 26 <= index < 29 else None,
                "EXTREME_LONG" if 26 <= index < 29 else None,
                None,
            )
            for index in range(40)
        ],
    )
    basis_indexes = tuple(range(14)) + tuple(range(29, 40))
    connection.executemany(
        "INSERT INTO basis_observations_v1 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                "basis",
                "BTCUSDT",
                index * FIVE_MINUTES_MS,
                0,
                1_000 + index,
                900 + index,
                100 + index,
                100 + index,
                1,
                2,
                3,
                None,
                "POSITIVE" if index < 14 else "NEGATIVE",
            )
            for index in basis_indexes
        ],
    )
    connection.commit()
    store.close()


def _aligned_rows(path: Path) -> list[sqlite3.Row]:
    return [
        row
        for batch in iter_intelligence_batches(store_path=path, asset="BTCUSDT")
        for row in batch
    ]


def _rebuild(path: Path) -> None:
    build_aligned_intelligence(
        store_path=path,
        asset="BTCUSDT",
        flow_dataset="flow",
        basis_dataset="basis",
    )


def test_missing_basis_is_never_filled_and_availability_transitions_are_segments(
    tmp_path: Path,
) -> None:
    path = tmp_path / "basis.sqlite"
    _seed_basis_availability(path)
    result = build_aligned_intelligence(
        store_path=path,
        asset="BTCUSDT",
        flow_dataset="flow",
        basis_dataset="basis",
    )
    assert (
        result.rows,
        result.flow_basis_rows,
        result.flow_only_rows,
        result.aligned_segments,
    ) == (
        40,
        25,
        15,
        3,
    )
    rows = _aligned_rows(path)
    assert [int(row["timestamp_ms"]) for row in rows] == [
        index * FIVE_MINUTES_MS for index in range(40)
    ]
    assert [(row["aligned_segment_id"], row["basis_available"]) for row in rows[13:30]] == [
        (0, 1),
        *[(1, 0)] * 15,
        (2, 1),
    ]
    for row in rows[14:29]:
        assert row["basis_segment_id"] is None
        assert row["mark_price"] is None
        assert row["index_price"] is None
        assert row["premium_bps"] is None
        assert row["premium_state"] is None
        assert row["premium_dynamic_state"] is None
    # The exact join leaves T=14 unavailable even though basis exists at T-5m
    # and T+15*5m; no as-of, nearest, forward, or backward fill is permitted.
    assert rows[14]["basis_available"] == 0
    assert rows[13]["mark_price"] == 1_013
    assert rows[29]["mark_price"] == 1_029

    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE basis_observations_v1 SET mark_close=999999, index_close=-999999 WHERE timestamp_ms IN (?, ?)",
        (13 * FIVE_MINUTES_MS, 29 * FIVE_MINUTES_MS),
    )
    connection.execute(
        "UPDATE conversion_manifest SET content_digest='changed' WHERE dataset='basis'"
    )
    connection.commit()
    connection.close()
    _rebuild(path)
    mutated_missing = _aligned_rows(path)[14]
    assert mutated_missing["basis_available"] == 0
    assert all(
        mutated_missing[column] is None
        for column in (
            "mark_price",
            "index_price",
            "premium_bps",
            "premium_state",
            "premium_dynamic_state",
        )
    )


def test_missing_basis_keeps_basis_fields_unavailable_but_flow_only_event_and_outcome_work(
    tmp_path: Path,
) -> None:
    path = tmp_path / "basis.sqlite"
    _seed_basis_availability(path)
    _rebuild(path)
    snapshots = _causal_snapshots(_aligned_rows(path))
    assert snapshots[26 * FIVE_MINUTES_MS]["basis_state"] is None
    assert snapshots[26 * FIVE_MINUTES_MS]["basis_dynamic_state"] is None
    events = extract_events(store_path=path, asset="BTCUSDT", event_type="CROWDED_STRESS")
    assert [(event.start_ms, event.end_ms, event.segment_id) for event in events] == [
        (26 * FIVE_MINUTES_MS, 28 * FIVE_MINUTES_MS, 1)
    ]

    # Basis availability transitions define segments, but a full one-hour
    # horizon wholly inside the missing-basis segment remains a valid spot outcome.
    build_outcome_cache(store_path=path, asset="BTCUSDT")
    assert _outcome(path, 14 * FIVE_MINUTES_MS)[13:15] == (1, None)
    assert _outcome(path, 13 * FIVE_MINUTES_MS)[13:15] == (0, "SEGMENT_END")


def test_missing_basis_attack_keeps_frozen_methodology_identity() -> None:
    assert (
        METHODOLOGY_NAME,
        METHODOLOGY_VERSION,
        methodology_digest(),
    ) == (
        "compact_matched_event_research",
        "1.0.0",
        "b52776bd5045532f83054fd43e6cdd8929d2de95d61301269b8b05751ebff690",
    )
