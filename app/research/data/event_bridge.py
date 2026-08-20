"""Compact causal behavioral events with cached-outcome matched controls.

This module reuses the active behavioral conventions: event-start anchors,
adjacent five-minute collapse, SHA-256 IDs, and per-horizon cooldowns.  It is
research-only and never imports execution or strategy components.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from math import sqrt
from pathlib import Path

from app.research.data.outcomes import HORIZONS
from app.research.data.store import ResearchStore

BRIDGE_SCHEMA = "chappie-compact-event-bridge/v3"
EVENT_TYPES = ("CROWDED_STRESS", "CROWDED_EXPANSION", "OI_ACTIVE_VOLATILITY_CLUSTER")
FIVE_MINUTES_MS = 300_000


@dataclass(frozen=True, slots=True)
class CompactEvent:
    event_id: str
    event_type: str
    asset: str
    start_ms: int
    end_ms: int
    segment_id: int
    raw_rows: int
    snapshot: dict[str, object]


@dataclass(frozen=True, slots=True)
class CompactMatchTrace:
    event_id: str
    asset: str
    horizon_minutes: int
    event_timestamp: int
    event_volatility: float
    eligible_pool_count: int
    ordered_timestamps: tuple[int, ...]
    bisect_center: int
    slice_start: int
    slice_end: int
    local_timestamps: tuple[int, ...]
    exclusions: tuple[tuple[int, str], ...]
    rankings: tuple[tuple[int, tuple[float, float, float, int]], ...]
    selected_control_timestamp: int | None
    unmatched_reason: str | None


def trace_compact_match(
    *,
    event_id: str,
    asset: str,
    horizon_minutes: int,
    event_timestamp: int,
    event_snapshot: dict[str, object],
    pool: list[tuple[int, dict[str, object]]],
    used: set[int] = frozenset(),
    event_times: set[int] = frozenset(),
    available: set[int] = frozenset(),
) -> CompactMatchTrace:
    """Bounded read-only trace of the canonical v3 local-window selector."""
    timestamps = [timestamp for timestamp, _ in pool]
    if len(timestamps) != len(set(timestamps)):
        raise ValueError("control pool contains duplicate timestamps")
    ordered = sorted(pool, key=lambda item: (float(item[1]["volatility_bps"]), item[0]))
    values = [float(item[1]["volatility_bps"]) for item in ordered]
    center = bisect_left(values, float(event_snapshot["volatility_bps"]))
    begin, end = max(0, center - 24), center + 24
    local = ordered[begin:end]
    exclusions: list[tuple[int, str]] = []
    candidates: list[tuple[int, dict[str, object]]] = []
    for timestamp, snapshot in local:
        reason = (
            "USED"
            if timestamp in used
            else "EVENT_OVERLAP"
            if timestamp in event_times
            else "OUTCOME_UNAVAILABLE"
            if timestamp not in available
            else "ELIGIBLE"
        )
        exclusions.append((timestamp, reason))
        if reason == "ELIGIBLE":
            candidates.append((timestamp, snapshot))
    rankings = tuple(
        (
            timestamp,
            (
                abs(float(snapshot["volatility_bps"]) - float(event_snapshot["volatility_bps"])),
                abs(
                    float(snapshot["recent_return_bps"])
                    - float(event_snapshot["recent_return_bps"])
                ),
                abs(
                    float(snapshot["relative_volume"] or 0)
                    - float(event_snapshot["relative_volume"] or 0)
                ),
                timestamp,
            ),
        )
        for timestamp, snapshot in candidates
    )
    selected = min(rankings, key=lambda item: item[1])[0] if rankings else None
    return CompactMatchTrace(
        event_id,
        asset,
        horizon_minutes,
        event_timestamp,
        float(event_snapshot["volatility_bps"]),
        len(ordered),
        tuple(item[0] for item in ordered),
        center,
        begin,
        end,
        tuple(item[0] for item in local),
        tuple(exclusions),
        rankings,
        selected,
        None if selected is not None else "NO_CAUSAL_CONTROL",
    )


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _event_id(event_type: str, asset: str, start_ms: int, end_ms: int) -> str:
    return _digest([event_type, asset, start_ms, end_ms])


def _condition(event_type: str, row: sqlite3.Row) -> bool:
    price_oi = row["price_oi_state"]
    funding = row["funding"]
    positioning = row["positioning_state"]
    if event_type == "CROWDED_STRESS":
        return (
            price_oi == "PRICE_DOWN_OI_UP"
            and funding is not None
            and funding < 0
            and positioning == "EXTREME_LONG"
        )
    if event_type == "CROWDED_EXPANSION":
        return (
            price_oi == "PRICE_UP_OI_UP"
            and funding is not None
            and funding > 0
            and positioning == "EXTREME_LONG"
        )
    if event_type == "OI_ACTIVE_VOLATILITY_CLUSTER":
        return price_oi not in {None, "FLAT_OR_MIXED"}
    raise ValueError("unsupported compact behavioral event type")


def _snapshot(
    row: sqlite3.Row, recent_return_bps: float, volatility_bps: float
) -> dict[str, object]:
    timestamp = int(row["timestamp_ms"])
    return {
        "year": datetime.fromtimestamp(timestamp / 1000, UTC).year,
        "hour_utc": datetime.fromtimestamp(timestamp / 1000, UTC).hour,
        "trend": "UP"
        if recent_return_bps >= 20
        else "DOWN"
        if recent_return_bps <= -20
        else "FLAT",
        "recent_return_bps": recent_return_bps,
        "volatility_bps": volatility_bps,
        "relative_volume": row["relative_volume"],
        "price_oi_state": row["price_oi_state"],
        "funding": row["funding"],
        "positioning_state": row["positioning_state"],
        "basis_state": row["premium_state"],
        "basis_dynamic_state": row["premium_dynamic_state"],
        "open_interest": row["open_interest"],
    }


def _causal_snapshots(rows: list[sqlite3.Row]) -> dict[int, dict[str, object]]:
    """Behavioral parity: twelve consecutive completed 5m return observations."""
    timestamps = [int(row["timestamp_ms"]) for row in rows]
    if timestamps != sorted(timestamps) or len(timestamps) != len(set(timestamps)):
        raise ValueError("compact event input timestamps must be strictly ordered and unique")
    result: dict[int, dict[str, object]] = {}
    returns: list[float] = []
    prior: sqlite3.Row | None = None
    for row in rows:
        contiguous = (
            prior is not None
            and int(row["timestamp_ms"]) - int(prior["timestamp_ms"]) == FIVE_MINUTES_MS
            and row["aligned_segment_id"] == prior["aligned_segment_id"]
        )
        if not contiguous:
            returns = []
        if contiguous and prior["close"] and row["close"]:
            returns.append((float(row["close"]) / float(prior["close"]) - 1) * 10_000)
        if len(returns) >= 12:
            values = returns[-12:]
            result[int(row["timestamp_ms"])] = _snapshot(
                row, sum(values), sqrt(sum(item * item for item in values) / 12)
            )
        prior = row
    return result


def _matching_signature(snapshot: dict[str, object]) -> str:
    return json.dumps(
        {
            key: snapshot[key]
            for key in (
                "year",
                "hour_utc",
                "trend",
            )
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def extract_events(
    *,
    store_path: Path,
    asset: str,
    event_type: str,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> tuple[CompactEvent, ...]:
    """Collapse adjacent causal condition rows; event anchor is always start_ms."""
    if event_type not in EVENT_TYPES:
        raise ValueError("unsupported compact behavioral event type")
    store = ResearchStore(store_path)
    store.initialize()
    sql = "SELECT * FROM intelligence_observations_v1 WHERE asset=?"
    params: list[object] = [asset]
    if start_ms is not None:
        sql += " AND timestamp_ms>=?"
        params.append(start_ms)
    if end_ms is not None:
        sql += " AND timestamp_ms<?"
        params.append(end_ms)
    sql += " ORDER BY timestamp_ms"
    source = list(store.connection.execute(sql, params))
    snapshots = _causal_snapshots(source)
    result: list[CompactEvent] = []
    open_start: sqlite3.Row | None = None
    prior: sqlite3.Row | None = None
    try:
        for row in source:
            adjacent = (
                prior is not None
                and int(row["timestamp_ms"]) - int(prior["timestamp_ms"]) == FIVE_MINUTES_MS
                and row["aligned_segment_id"] == prior["aligned_segment_id"]
            )
            active = int(row["timestamp_ms"]) in snapshots and _condition(event_type, row)
            if open_start is not None and (not active or not adjacent):
                assert prior is not None
                result.append(
                    CompactEvent(
                        _event_id(
                            event_type,
                            asset,
                            int(open_start["timestamp_ms"]),
                            int(prior["timestamp_ms"]),
                        ),
                        event_type,
                        asset,
                        int(open_start["timestamp_ms"]),
                        int(prior["timestamp_ms"]),
                        int(open_start["aligned_segment_id"]),
                        (int(prior["timestamp_ms"]) - int(open_start["timestamp_ms"]))
                        // FIVE_MINUTES_MS
                        + 1,
                        snapshots[int(open_start["timestamp_ms"])],
                    )
                )
                open_start = None
            if active and open_start is None:
                open_start = row
            prior = row
        if open_start is not None and prior is not None:
            result.append(
                CompactEvent(
                    _event_id(
                        event_type,
                        asset,
                        int(open_start["timestamp_ms"]),
                        int(prior["timestamp_ms"]),
                    ),
                    event_type,
                    asset,
                    int(open_start["timestamp_ms"]),
                    int(prior["timestamp_ms"]),
                    int(open_start["aligned_segment_id"]),
                    (int(prior["timestamp_ms"]) - int(open_start["timestamp_ms"]))
                    // FIVE_MINUTES_MS
                    + 1,
                    snapshots[int(open_start["timestamp_ms"])],
                )
            )
        return tuple(result)
    finally:
        store.close()


def _verified(connection: sqlite3.Connection, table: str, asset: str) -> sqlite3.Row:
    manifest = connection.execute(f"SELECT * FROM {table} WHERE asset=?", (asset,)).fetchone()
    if manifest is None or manifest["state"] != "VERIFIED" or not manifest["content_digest"]:
        raise ValueError(f"{asset} {table} is not VERIFIED with a digest")
    return manifest


def build_event_match_cache(
    *, store_path: Path, asset: str, event_type: str
) -> dict[str, int | str]:
    """Persist deterministic per-horizon matches, reusing cached forward outcomes."""
    store = ResearchStore(store_path)
    store.initialize()
    c = store.connection
    try:
        aligned = _verified(c, "aligned_manifest_v1", asset)
        outcomes = _verified(c, "outcome_manifest_v1", asset)
        config = _digest(
            {
                "schema": BRIDGE_SCHEMA,
                "event_type": event_type,
                "horizons": HORIZONS,
                "anchor": "event-start",
                "controls": "same year/hour/trend; stable timestamp tie-break",
            }
        )
        prior = c.execute(
            "SELECT * FROM event_match_manifest_v1 WHERE asset=? AND event_type=?",
            (asset, event_type),
        ).fetchone()
        if (
            prior
            and prior["state"] == "VERIFIED"
            and prior["aligned_content_digest"] == aligned["content_digest"]
            and prior["outcome_content_digest"] == outcomes["content_digest"]
            and prior["config_digest"] == config
        ):
            return {
                "state": "REUSED",
                "events": int(prior["event_count"]),
                "matches": int(prior["match_count"]),
            }
        events = extract_events(store_path=store_path, asset=asset, event_type=event_type)
        c.execute("BEGIN IMMEDIATE")
        old_ids = [
            row[0]
            for row in c.execute(
                "SELECT event_id FROM intelligence_events_v1 WHERE asset=? AND event_type=?",
                (asset, event_type),
            )
        ]
        if old_ids:
            c.executemany(
                "DELETE FROM event_matches_v1 WHERE event_id=?", [(item,) for item in old_ids]
            )
        c.execute(
            "DELETE FROM intelligence_events_v1 WHERE asset=? AND event_type=?", (asset, event_type)
        )
        c.executemany(
            "INSERT INTO intelligence_events_v1 VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (
                    e.event_id,
                    e.event_type,
                    e.asset,
                    e.start_ms,
                    e.end_ms,
                    e.start_ms,
                    e.segment_id,
                    e.raw_rows,
                    json.dumps(e.snapshot, sort_keys=True, separators=(",", ":")),
                )
                for e in events
            ],
        )
        # The reusable causal index is built from compact present-row fields
        # only.  It deliberately excludes outcomes and all future observations.
        event_times = {
            timestamp
            for event in events
            for timestamp in range(event.start_ms, event.end_ms + FIVE_MINUTES_MS, FIVE_MINUTES_MS)
        }
        controls_by_signature: dict[str, list[tuple[int, dict[str, object]]]] = defaultdict(list)
        candidate_rows = list(
            c.execute(
                "SELECT * FROM intelligence_observations_v1 WHERE asset=? ORDER BY timestamp_ms",
                (asset,),
            )
        )
        for timestamp, snapshot in _causal_snapshots(candidate_rows).items():
            controls_by_signature[_matching_signature(snapshot)].append((timestamp, snapshot))
        for pool in controls_by_signature.values():
            pool.sort(key=lambda item: (float(item[1]["volatility_bps"]), item[0]))
        matches = 0
        material: list[tuple[object, ...]] = []
        for minutes, bars in HORIZONS.items():
            cooldown = bars * FIVE_MINUTES_MS
            accepted: list[CompactEvent] = []
            for event in events:
                if not accepted or event.start_ms >= accepted[-1].start_ms + cooldown:
                    accepted.append(event)
            used: set[int] = set()
            available = {
                int(row[0])
                for row in c.execute(
                    "SELECT timestamp_ms FROM intelligence_outcomes_v1 WHERE asset=? AND horizon_minutes=? AND outcome_available=1",
                    (asset, minutes),
                )
            }
            for event in accepted:
                outcome_available = event.start_ms in available
                signature = _matching_signature(event.snapshot)
                control = None
                if outcome_available:
                    control = trace_compact_match(
                        event_id=event.event_id,
                        asset=asset,
                        horizon_minutes=minutes,
                        event_timestamp=event.start_ms,
                        event_snapshot=event.snapshot,
                        pool=controls_by_signature[signature],
                        used=used,
                        event_times=event_times,
                        available=available,
                    ).selected_control_timestamp
                    if control is not None:
                        used.add(control)
                        matches += 1
                material.append(
                    (
                        event.event_id,
                        asset,
                        minutes,
                        event.start_ms,
                        control,
                        signature,
                        "MATCHED"
                        if control is not None
                        else "OUTCOME_UNAVAILABLE"
                        if not outcome_available
                        else "NO_CAUSAL_CONTROL",
                    )
                )
        c.executemany("INSERT INTO event_matches_v1 VALUES (?,?,?,?,?,?,?)", material)
        digest = _digest(
            [
                tuple(row)
                for row in c.execute(
                    "SELECT * FROM event_matches_v1 WHERE asset=? ORDER BY event_id,horizon_minutes",
                    (asset,),
                )
            ]
        )
        c.execute(
            "INSERT OR REPLACE INTO event_match_manifest_v1 VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                asset,
                event_type,
                "VERIFIED",
                BRIDGE_SCHEMA,
                aligned["content_digest"],
                outcomes["content_digest"],
                config,
                len(events),
                matches,
                digest,
                "verified",
                datetime.now(UTC).isoformat(timespec="seconds"),
            ),
        )
        c.commit()
        return {"state": "VERIFIED", "events": len(events), "matches": matches}
    except Exception:
        c.rollback()
        raise
    finally:
        store.close()


def iter_event_matches(
    *, store_path: Path, asset: str, horizon_minutes: int, batch_size: int = 5000
) -> Iterator[tuple[sqlite3.Row, ...]]:
    store = ResearchStore(store_path)
    store.initialize()
    cursor = store.connection.execute(
        "SELECT * FROM event_matches_v1 WHERE asset=? AND horizon_minutes=? ORDER BY event_anchor_timestamp_ms,event_id",
        (asset, horizon_minutes),
    )
    try:
        while batch := tuple(cursor.fetchmany(batch_size)):
            yield batch
    finally:
        store.close()
