"""Frozen candidate contract for compact matched-event research."""

from __future__ import annotations

import hashlib
import json

from app.research.data.event_bridge import BRIDGE_SCHEMA
from app.research.data.outcomes import HORIZONS, OUTCOME_SCHEMA_VERSION, THRESHOLDS
from app.research.data.store import ALIGNMENT_SCHEMA_VERSION

METHODOLOGY_NAME = "compact_matched_event_research"
METHODOLOGY_VERSION = "1.0.0"
LOCAL_WINDOW_RADIUS = 24


def config_payload() -> dict[str, object]:
    return {
        "name": METHODOLOGY_NAME,
        "version": METHODOLOGY_VERSION,
        "alignment_schema": ALIGNMENT_SCHEMA_VERSION,
        "outcome_schema": OUTCOME_SCHEMA_VERSION,
        "event_match_schema": BRIDGE_SCHEMA,
        "horizons_minutes": sorted(HORIZONS),
        "thresholds_bps": THRESHOLDS,
        "local_window_radius": LOCAL_WINDOW_RADIUS,
        "anchor": "event-start",
        "match_order": "volatility,return,relative_volume,timestamp",
        "missing_relative_volume": "zero",
    }


def methodology_digest(payload: dict[str, object] | None = None) -> str:
    return hashlib.sha256(
        json.dumps(payload or config_payload(), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
