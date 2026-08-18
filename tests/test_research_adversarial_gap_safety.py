"""Deterministic gap and segment attacks for the frozen compact methodology."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from app.research_event_bridge import _causal_snapshots, extract_events
from app.research_methodology import (
    METHODOLOGY_NAME,
    METHODOLOGY_VERSION,
    methodology_digest,
)
from app.research_outcomes import build_outcome_cache
from tests.test_research_outcomes import _outcome, _seed

FIVE_MINUTES_MS = 300_000


def _refresh_alignment_digest(path: Path) -> None:
    connection = sqlite3.connect(path)
    digest = hashlib.sha256()
    for row in connection.execute(
        "SELECT * FROM intelligence_observations_v1 ORDER BY asset,timestamp_ms"
    ):
        digest.update(
            json.dumps(tuple(row), separators=(",", ":"), ensure_ascii=True).encode() + b"\n"
        )
    connection.execute(
        "UPDATE aligned_manifest_v1 SET content_digest=? WHERE asset='BTCUSDT'",
        (digest.hexdigest(),),
    )
    connection.commit()
    connection.close()


def _snapshots(path: Path) -> dict[int, dict[str, object]]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    rows = list(
        connection.execute("SELECT * FROM intelligence_observations_v1 ORDER BY timestamp_ms")
    )
    connection.close()
    return _causal_snapshots(rows)


def _mark_crowded_stress(path: Path, indexes: range) -> None:
    connection = sqlite3.connect(path)
    connection.executemany(
        """UPDATE intelligence_observations_v1
           SET price_oi_state='PRICE_DOWN_OI_UP', funding=-0.01,
               positioning_state='EXTREME_LONG'
           WHERE timestamp_ms=?""",
        [(index * FIVE_MINUTES_MS,) for index in indexes],
    )
    connection.commit()
    connection.close()
    _refresh_alignment_digest(path)


def _methodology_identity() -> tuple[str, str, str]:
    return METHODOLOGY_NAME, METHODOLOGY_VERSION, methodology_digest()


def test_timestamp_gap_resets_causal_warmup_and_event_eligibility(tmp_path: Path) -> None:
    continuous = tmp_path / "continuous.sqlite"
    gapped = tmp_path / "gapped.sqlite"
    closes = [100 + index for index in range(32)]
    _seed(continuous, closes)
    _seed(gapped, closes)
    # Remove index 6.  The next snapshot may only occur after twelve new
    # consecutive returns, at index 19; it cannot borrow the first segment.
    connection = sqlite3.connect(gapped)
    connection.execute(
        "DELETE FROM intelligence_observations_v1 WHERE timestamp_ms=?", (6 * FIVE_MINUTES_MS,)
    )
    connection.commit()
    connection.close()
    _refresh_alignment_digest(gapped)
    _mark_crowded_stress(gapped, range(7, 20))

    assert 15 * FIVE_MINUTES_MS in _snapshots(continuous)
    gapped_snapshots = _snapshots(gapped)
    assert 18 * FIVE_MINUTES_MS not in gapped_snapshots
    assert 19 * FIVE_MINUTES_MS in gapped_snapshots
    events = extract_events(store_path=gapped, asset="BTCUSDT", event_type="CROWDED_STRESS")
    assert [(event.start_ms, event.end_ms) for event in events] == [
        (19 * FIVE_MINUTES_MS, 19 * FIVE_MINUTES_MS)
    ]


def test_explicit_segment_boundary_resets_features_and_splits_events(tmp_path: Path) -> None:
    continuous = tmp_path / "continuous.sqlite"
    segmented = tmp_path / "segmented.sqlite"
    closes = [100 + index for index in range(34)]
    _seed(continuous, closes)
    _seed(segmented, closes, split_at=15)
    _mark_crowded_stress(segmented, range(12, 15))
    _mark_crowded_stress(segmented, range(27, 30))

    assert 16 * FIVE_MINUTES_MS in _snapshots(continuous)
    snapshots = _snapshots(segmented)
    assert 26 * FIVE_MINUTES_MS not in snapshots
    assert 27 * FIVE_MINUTES_MS in snapshots
    events = extract_events(store_path=segmented, asset="BTCUSDT", event_type="CROWDED_STRESS")
    assert [(event.start_ms, event.end_ms, event.segment_id) for event in events] == [
        (12 * FIVE_MINUTES_MS, 14 * FIVE_MINUTES_MS, 0),
        (27 * FIVE_MINUTES_MS, 29 * FIVE_MINUTES_MS, 1),
    ]


def test_outcome_refuses_horizon_that_crosses_segment_and_continuous_control_succeeds(
    tmp_path: Path,
) -> None:
    continuous = tmp_path / "continuous.sqlite"
    segmented = tmp_path / "segmented.sqlite"
    closes = [100 + index for index in range(30)]
    _seed(continuous, closes)
    _seed(segmented, closes, split_at=10)

    build_outcome_cache(store_path=continuous, asset="BTCUSDT")
    build_outcome_cache(store_path=segmented, asset="BTCUSDT")

    # Index 0 has an exact one-hour endpoint at index 12 only in the
    # continuous control.  In the segmented case the horizon reaches segment 1.
    assert _outcome(continuous, 0)[13:15] == (1, None)
    assert _outcome(segmented, 0)[13:15] == (0, "SEGMENT_END")
    assert _outcome(segmented, 0)[4:13] == (None,) * 9


def test_gap_and_segment_attacks_leave_frozen_methodology_identity_unchanged() -> None:
    before = _methodology_identity()
    after = _methodology_identity()
    assert (
        before
        == after
        == (
            "compact_matched_event_research",
            "1.0.0",
            methodology_digest(),
        )
    )
