from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.research.data.event_bridge import (
    build_event_match_cache,
    extract_events,
    iter_event_matches,
)
from app.research.data.outcomes import build_outcome_cache
from tests.test_research_outcomes import _seed


def _prepare(path: Path) -> None:
    _seed(path, [100] * 40)
    build_outcome_cache(store_path=path, asset="BTCUSDT")
    from app.research.data.store import ResearchStore

    store = ResearchStore(path)
    store.initialize()
    c = store.connection
    c.execute(
        "UPDATE intelligence_observations_v1 SET price_oi_state='PRICE_DOWN_OI_UP',funding=-0.01,positioning_state='EXTREME_LONG' WHERE timestamp_ms IN (3600000,3900000)"
    )
    # Update the aligned digest only for this deterministic fixture.
    digest = hashlib.sha256()
    for row in c.execute("SELECT * FROM intelligence_observations_v1 ORDER BY asset,timestamp_ms"):
        digest.update(json.dumps(tuple(row), separators=(",", ":")).encode() + b"\n")
    c.execute(
        "UPDATE aligned_manifest_v1 SET content_digest=? WHERE asset='BTCUSDT'",
        (digest.hexdigest(),),
    )
    c.commit()
    store.close()
    build_outcome_cache(store_path=path, asset="BTCUSDT")


def test_compact_events_collapse_anchor_and_cached_outcomes(tmp_path: Path) -> None:
    path = tmp_path / "r.sqlite"
    _prepare(path)
    events = extract_events(store_path=path, asset="BTCUSDT", event_type="CROWDED_STRESS")
    assert (
        len(events) == 1
        and events[0].start_ms == 3600000
        and events[0].end_ms == 3900000
        and events[0].raw_rows == 2
    )
    result = build_event_match_cache(store_path=path, asset="BTCUSDT", event_type="CROWDED_STRESS")
    assert result["state"] == "VERIFIED"
    assert (
        build_event_match_cache(store_path=path, asset="BTCUSDT", event_type="CROWDED_STRESS")[
            "state"
        ]
        == "REUSED"
    )
    assert list(iter_event_matches(store_path=path, asset="BTCUSDT", horizon_minutes=60))
