"""Deterministic 2A-2 integrity and matching-contamination attacks."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.research.data.alignment import build_aligned_intelligence
from app.research_event_bridge import (
    _causal_snapshots,
    build_event_match_cache,
    trace_compact_match,
)
from app.research_methodology import (
    METHODOLOGY_NAME,
    METHODOLOGY_VERSION,
    methodology_digest,
)
from tests.test_research_alignment import _seed as _seed_alignment
from tests.test_research_event_bridge import _prepare
from tests.test_research_outcomes import _seed as _seed_outcomes


def _snapshot(volatility: float, recent: float = 0, volume: float | None = 1) -> dict[str, object]:
    return {
        "volatility_bps": volatility,
        "recent_return_bps": recent,
        "relative_volume": volume,
    }


def _rows(path: Path) -> list[sqlite3.Row]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    result = list(
        connection.execute("SELECT * FROM intelligence_observations_v1 ORDER BY timestamp_ms")
    )
    connection.close()
    return result


def test_duplicate_flow_and_basis_source_timestamps_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "source.sqlite"
    _seed_alignment(path)
    connection = sqlite3.connect(path)
    flow = connection.execute("SELECT * FROM flow_observations_v1 LIMIT 1").fetchone()
    basis = connection.execute("SELECT * FROM basis_observations_v1 LIMIT 1").fetchone()
    assert flow is not None and basis is not None
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            f"INSERT INTO flow_observations_v1 VALUES ({','.join('?' for _ in flow)})", flow
        )
    conflicting = list(flow)
    conflicting[4] = 999_999
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            f"INSERT INTO flow_observations_v1 VALUES ({','.join('?' for _ in flow)})", conflicting
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            f"INSERT INTO basis_observations_v1 VALUES ({','.join('?' for _ in basis)})", basis
        )
    connection.close()


def test_raw_event_inputs_reject_duplicate_and_unsorted_timestamps(tmp_path: Path) -> None:
    path = tmp_path / "event.sqlite"
    _prepare(path)
    rows = _rows(path)
    with pytest.raises(ValueError, match="strictly ordered and unique"):
        _causal_snapshots(rows + [rows[-1]])
    with pytest.raises(ValueError, match="strictly ordered and unique"):
        _causal_snapshots(list(reversed(rows)))


def test_matching_rejects_duplicate_pool_and_canonically_normalizes_pool_order() -> None:
    event = _snapshot(10)
    pool = [(20, _snapshot(20)), (10, _snapshot(10)), (30, _snapshot(30))]
    first = trace_compact_match(
        event_id="event",
        asset="BTCUSDT",
        horizon_minutes=60,
        event_timestamp=999,
        event_snapshot=event,
        pool=pool,
        available={10, 20, 30},
    )
    second = trace_compact_match(
        event_id="event",
        asset="BTCUSDT",
        horizon_minutes=60,
        event_timestamp=999,
        event_snapshot=event,
        pool=list(reversed(pool)),
        available={10, 20, 30},
    )
    assert first == second and first.selected_control_timestamp == 10
    with pytest.raises(ValueError, match="duplicate timestamps"):
        trace_compact_match(
            event_id="event",
            asset="BTCUSDT",
            horizon_minutes=60,
            event_timestamp=999,
            event_snapshot=event,
            pool=pool + [(10, _snapshot(10))],
            available={10, 20, 30},
        )


def test_matching_excludes_event_overlap_unavailable_outcomes_and_used_controls() -> None:
    event = _snapshot(10)
    pool = [(10, _snapshot(10)), (11, _snapshot(11)), (12, _snapshot(12)), (13, _snapshot(13))]
    trace = trace_compact_match(
        event_id="event",
        asset="BTCUSDT",
        horizon_minutes=60,
        event_timestamp=10,
        event_snapshot=event,
        pool=pool,
        # Canonical overlap exclusion is the collapsed target event's exact
        # timestamp interval; it does not infer a second outcome-window rule.
        event_times={10, 11},
        available={10, 11, 13},
        used={13},
    )
    assert dict(trace.exclusions) == {
        10: "EVENT_OVERLAP",
        11: "EVENT_OVERLAP",
        12: "OUTCOME_UNAVAILABLE",
        13: "USED",
    }
    assert trace.selected_control_timestamp is None


def test_matching_is_outcome_blind_and_follows_per_horizon_used_policy() -> None:
    event = _snapshot(10, recent=5, volume=2)
    pool = [(20, _snapshot(10, recent=5, volume=2)), (21, _snapshot(11, recent=5, volume=2))]
    selected = trace_compact_match(
        event_id="A",
        asset="BTCUSDT",
        horizon_minutes=60,
        event_timestamp=1,
        event_snapshot=event,
        pool=pool,
        available={20, 21},
    )
    # Future returns/MFE/MAE are intentionally absent from the selector input.
    # Replacing an external outcome payload cannot alter the causal choice.
    mutated_future_outcomes = {20: (-9_999, 9_999, -9_999), 21: (9_999, -9_999, 9_999)}
    assert mutated_future_outcomes[20] != mutated_future_outcomes[21]
    again = trace_compact_match(
        event_id="A",
        asset="BTCUSDT",
        horizon_minutes=60,
        event_timestamp=1,
        event_snapshot=event,
        pool=pool,
        available={20, 21},
    )
    assert selected.selected_control_timestamp == again.selected_control_timestamp == 20
    next_event = trace_compact_match(
        event_id="B",
        asset="BTCUSDT",
        horizon_minutes=60,
        event_timestamp=2,
        event_snapshot=event,
        pool=pool,
        available={20, 21},
        used={20},
    )
    assert next_event.selected_control_timestamp == 21


def test_invalid_segment_candidate_never_enters_causal_control_pool(tmp_path: Path) -> None:
    path = tmp_path / "segmented.sqlite"
    _seed_outcomes(path, [100 + index for index in range(32)], split_at=15)
    snapshots = _causal_snapshots(_rows(path))
    # The otherwise attractive first timestamp of segment 1 has no inherited
    # return history. It is absent from the causal snapshot/control universe.
    assert 15 * 300_000 not in snapshots
    assert 27 * 300_000 in snapshots


def test_complete_synthetic_experiment_is_reproducible_and_caches_reuse(tmp_path: Path) -> None:
    first, second = tmp_path / "first.sqlite", tmp_path / "second.sqlite"
    _prepare(first)
    _prepare(second)
    first_result = build_event_match_cache(
        store_path=first, asset="BTCUSDT", event_type="CROWDED_STRESS"
    )
    second_result = build_event_match_cache(
        store_path=second, asset="BTCUSDT", event_type="CROWDED_STRESS"
    )
    assert first_result == second_result
    assert (
        build_event_match_cache(store_path=first, asset="BTCUSDT", event_type="CROWDED_STRESS")[
            "state"
        ]
        == "REUSED"
    )
    for table in (
        "aligned_manifest_v1",
        "outcome_manifest_v1",
        "event_match_manifest_v1",
        "intelligence_events_v1",
        "event_matches_v1",
    ):
        one = sqlite3.connect(first).execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
        two = sqlite3.connect(second).execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
        # The only allowed non-semantic difference is build wall-clock metadata.
        if table.endswith("manifest_v1"):
            assert [row[:-1] for row in one] == [row[:-1] for row in two]
        else:
            assert one == two


def test_source_and_test_only_methodology_dependency_changes_invalidate_only_dependents(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "source.sqlite"
    _seed_alignment(source_path)
    connection = sqlite3.connect(source_path)
    flow_rows = connection.execute("SELECT * FROM flow_observations_v1").fetchall()
    basis_rows = connection.execute("SELECT * FROM basis_observations_v1").fetchall()
    connection.execute(
        """INSERT INTO conversion_manifest(dataset,state,source_path,source_sha256,content_digest)
           VALUES('flow-eth','VERIFIED','flow-eth','eth-flow','eth-flow')"""
    )
    connection.execute(
        """INSERT INTO conversion_manifest(dataset,state,source_path,source_sha256,content_digest)
           VALUES('basis-eth','VERIFIED','basis-eth','eth-basis','eth-basis')"""
    )
    connection.executemany(
        "INSERT INTO flow_observations_v1 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [("flow-eth", "ETHUSDT", *row[2:]) for row in flow_rows],
    )
    connection.executemany(
        "INSERT INTO basis_observations_v1 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [("basis-eth", "ETHUSDT", *row[2:]) for row in basis_rows],
    )
    connection.commit()
    connection.close()
    assert (
        build_aligned_intelligence(
            store_path=source_path,
            asset="BTCUSDT",
            flow_dataset="flow",
            basis_dataset="basis",
        ).state
        == "VERIFIED"
    )
    assert (
        build_aligned_intelligence(
            store_path=source_path,
            asset="ETHUSDT",
            flow_dataset="flow-eth",
            basis_dataset="basis-eth",
        ).state
        == "VERIFIED"
    )
    connection = sqlite3.connect(source_path)
    connection.execute(
        "UPDATE conversion_manifest SET content_digest='new-flow' WHERE dataset='flow'"
    )
    connection.commit()
    connection.close()
    assert (
        build_aligned_intelligence(
            store_path=source_path,
            asset="BTCUSDT",
            flow_dataset="flow",
            basis_dataset="basis",
        ).state
        == "VERIFIED"
    )
    assert (
        build_aligned_intelligence(
            store_path=source_path,
            asset="BTCUSDT",
            flow_dataset="flow",
            basis_dataset="basis",
        ).state
        == "REUSED"
    )
    assert (
        build_aligned_intelligence(
            store_path=source_path,
            asset="ETHUSDT",
            flow_dataset="flow-eth",
            basis_dataset="basis-eth",
        ).state
        == "REUSED"
    )

    event_path = tmp_path / "event.sqlite"
    _prepare(event_path)
    assert (
        build_event_match_cache(
            store_path=event_path, asset="BTCUSDT", event_type="CROWDED_STRESS"
        )["state"]
        == "VERIFIED"
    )
    connection = sqlite3.connect(event_path)
    connection.execute("UPDATE event_match_manifest_v1 SET config_digest='test-only-incompatible'")
    connection.commit()
    connection.close()
    # This simulates an incompatible test-only methodology configuration.  It
    # invalidates the event/match cache but preserves reusable source layers.
    assert (
        build_event_match_cache(
            store_path=event_path, asset="BTCUSDT", event_type="CROWDED_STRESS"
        )["state"]
        == "VERIFIED"
    )


def test_frozen_methodology_identity_is_unchanged() -> None:
    assert (
        METHODOLOGY_NAME,
        METHODOLOGY_VERSION,
        methodology_digest(),
    ) == (
        "compact_matched_event_research",
        "1.0.0",
        "b52776bd5045532f83054fd43e6cdd8929d2de95d61301269b8b05751ebff690",
    )
