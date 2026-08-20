from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from app.research.data.event_bridge import _causal_snapshots, extract_events
from app.research.data.outcomes import build_outcome_cache
from app.research_methodology import METHODOLOGY_NAME, METHODOLOGY_VERSION, methodology_digest
from tests.test_research_outcomes import _outcome, _seed


def _state(
    path: Path, anchor: int
) -> tuple[dict[str, object], tuple[object, ...], tuple[str, ...]]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    rows = list(
        connection.execute("SELECT * FROM intelligence_observations_v1 ORDER BY timestamp_ms")
    )
    snapshot = _causal_snapshots(rows)[anchor]
    connection.close()
    events = extract_events(store_path=path, asset="BTCUSDT", event_type="CROWDED_STRESS")
    return snapshot, _outcome(path, anchor), tuple(event.event_id for event in events)


def _refresh_digest(path: Path) -> None:
    c = sqlite3.connect(path)
    digest = hashlib.sha256()
    for row in c.execute("SELECT * FROM intelligence_observations_v1 ORDER BY asset,timestamp_ms"):
        digest.update(json.dumps(tuple(row), separators=(",", ":")).encode() + b"\n")
    c.execute(
        "UPDATE aligned_manifest_v1 SET content_digest=? WHERE asset='BTCUSDT'",
        (digest.hexdigest(),),
    )
    c.commit()
    c.close()


def test_post_anchor_mutation_cannot_change_causal_state(tmp_path: Path) -> None:
    anchor = 12 * 300_000
    baseline, mutated = tmp_path / "baseline.sqlite", tmp_path / "mutated.sqlite"
    closes = [100 + index for index in range(40)]
    _seed(baseline, closes)
    _seed(mutated, closes)
    for path in (baseline, mutated):
        c = sqlite3.connect(path)
        c.execute(
            "UPDATE intelligence_observations_v1 SET price_oi_state='PRICE_DOWN_OI_UP',funding=-0.01,positioning_state='EXTREME_LONG' WHERE timestamp_ms=?",
            (anchor,),
        )
        c.commit()
        c.close()
    c = sqlite3.connect(mutated)
    c.execute(
        "UPDATE intelligence_observations_v1 SET close=1000000,relative_volume=999,funding=999,positioning_state='EXTREME_LONG' WHERE timestamp_ms>?",
        (anchor,),
    )
    c.commit()
    c.close()
    _refresh_digest(baseline)
    _refresh_digest(mutated)
    # Recompute only outcomes after mutation; causal snapshot/event formation reads no future state.
    for path in (baseline, mutated):
        build_outcome_cache(store_path=path, asset="BTCUSDT")
    before, before_outcome, before_events = _state(baseline, anchor)
    after, after_outcome, after_events = _state(mutated, anchor)
    assert before == after
    assert before_events == after_events
    assert before_outcome[4] != after_outcome[4]
    assert (METHODOLOGY_NAME, METHODOLOGY_VERSION, methodology_digest()) == (
        "compact_matched_event_research",
        "1.0.0",
        methodology_digest(),
    )
