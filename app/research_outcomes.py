"""Segment-safe forward outcome cache for compact research intelligence data.

The cache is deliberately generic: it stores spot-close outcomes once for each
aligned anchor/horizon and does not know about events, matching, or hypotheses.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import deque
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.research_data import ALIGNMENT_SCHEMA_VERSION, ResearchStore

OUTCOME_SCHEMA_VERSION = "chappie-intelligence-outcomes/v1"
HORIZONS = {60: 12, 180: 36, 360: 72, 720: 144, 1440: 288}
THRESHOLDS = (60, 100, 150)
FIVE_MINUTES_MS = 300_000
_OUTCOME_COLUMNS = (
    "asset",
    "timestamp_ms",
    "aligned_segment_id",
    "horizon_minutes",
    "forward_return_bps",
    "max_favorable_excursion_bps",
    "max_adverse_excursion_bps",
    "hit_plus_60_bps",
    "hit_plus_100_bps",
    "hit_plus_150_bps",
    "hit_minus_60_bps",
    "hit_minus_100_bps",
    "hit_minus_150_bps",
    "outcome_available",
    "unavailable_reason",
)


@dataclass(frozen=True, slots=True)
class OutcomeResult:
    asset: str
    state: str
    rows: int = 0
    availability: dict[int, tuple[int, int]] | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _config_digest() -> str:
    payload = {
        "schema": OUTCOME_SCHEMA_VERSION,
        "aligned_schema": ALIGNMENT_SCHEMA_VERSION,
        "horizons": HORIZONS,
        "thresholds_bps": THRESHOLDS,
        "price": "aligned-flow-close",
        "window": "future-exclusive-anchor/inclusive-exact-endpoint/same-aligned-segment",
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _digest_rows(cursor: sqlite3.Cursor) -> str:
    digest = hashlib.sha256()
    for row in cursor:
        digest.update(
            json.dumps(tuple(row), separators=(",", ":"), ensure_ascii=True).encode() + b"\n"
        )
    return digest.hexdigest()


def _verified_alignment(connection: sqlite3.Connection, asset: str) -> sqlite3.Row:
    manifest = connection.execute(
        "SELECT * FROM aligned_manifest_v1 WHERE asset=?", (asset,)
    ).fetchone()
    if manifest is None or manifest["state"] != "VERIFIED":
        raise ValueError(f"{asset} is not a VERIFIED aligned dataset")
    if (
        not manifest["content_digest"]
        or not manifest["flow_content_digest"]
        or not manifest["basis_content_digest"]
    ):
        raise ValueError(f"{asset} aligned manifest has incomplete digests")
    current = _digest_rows(
        connection.execute(
            "SELECT * FROM intelligence_observations_v1 WHERE asset=? ORDER BY asset,timestamp_ms",
            (asset,),
        )
    )
    if current != manifest["content_digest"]:
        raise ValueError(f"{asset} aligned content digest mismatch")
    return manifest


def _outcome_row(
    *,
    asset: str,
    timestamp: int,
    segment: int,
    horizon: int,
    close: float | None,
    endpoint_close: float | None,
    maximum: float | None,
    minimum: float | None,
    reason: str | None,
) -> tuple[object, ...]:
    if (
        reason is not None
        or close is None
        or close <= 0
        or endpoint_close is None
        or endpoint_close <= 0
    ):
        return (
            asset,
            timestamp,
            segment,
            horizon,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            0,
            reason or "INVALID_PRICE",
        )
    forward = (endpoint_close / close - 1) * 10_000
    mfe = (maximum / close - 1) * 10_000 if maximum is not None else None
    mae = (minimum / close - 1) * 10_000 if minimum is not None else None
    assert mfe is not None and mae is not None
    flags = tuple(int(mfe >= threshold) for threshold in THRESHOLDS) + tuple(
        int(mae <= -threshold) for threshold in THRESHOLDS
    )
    return (asset, timestamp, segment, horizon, forward, mfe, mae, *flags, 1, None)


def _segment_rows(
    connection: sqlite3.Connection, asset: str
) -> Iterator[tuple[int, list[sqlite3.Row]]]:
    segments = connection.execute(
        "SELECT DISTINCT aligned_segment_id FROM intelligence_observations_v1 WHERE asset=? ORDER BY aligned_segment_id",
        (asset,),
    )
    for item in segments:
        segment = int(item[0])
        rows = list(
            connection.execute(
                "SELECT timestamp_ms,close FROM intelligence_observations_v1 WHERE asset=? AND aligned_segment_id=? ORDER BY timestamp_ms",
                (asset, segment),
            )
        )
        yield segment, rows


def build_outcome_cache(*, store_path: Path, asset: str) -> OutcomeResult:
    """Build/reuse all fixed-horizon outcomes for one VERIFIED aligned asset."""
    store = ResearchStore(store_path)
    store.initialize()
    connection = store.connection
    try:
        aligned = _verified_alignment(connection, asset)
        config = _config_digest()
        prior = connection.execute(
            "SELECT * FROM outcome_manifest_v1 WHERE asset=?", (asset,)
        ).fetchone()
        if (
            prior
            and prior["state"] == "VERIFIED"
            and prior["aligned_content_digest"] == aligned["content_digest"]
            and prior["outcome_config_digest"] == config
        ):
            availability = {
                int(key): tuple(value)
                for key, value in json.loads(prior["availability_json"]).items()
            }
            return OutcomeResult(asset, "REUSED", int(prior["row_count"]), availability)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM intelligence_outcomes_v1 WHERE asset=?", (asset,))
        connection.execute(
            """INSERT OR REPLACE INTO outcome_manifest_v1(
                asset,state,outcome_schema,aligned_content_digest,outcome_config_digest,horizons_json,thresholds_json,started_at,detail
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                asset,
                "IN_PROGRESS",
                OUTCOME_SCHEMA_VERSION,
                aligned["content_digest"],
                config,
                json.dumps(sorted(HORIZONS)),
                json.dumps(THRESHOLDS),
                _now(),
                "building segment-safe outcomes",
            ),
        )
        batch: list[tuple[object, ...]] = []
        availability = {minutes: [0, 0] for minutes in HORIZONS}
        anchors = 0
        first: int | None = None
        last: int | None = None
        for segment, rows in _segment_rows(connection, asset):
            if not rows:
                continue
            timestamps = [int(row["timestamp_ms"]) for row in rows]
            closes = [float(row["close"]) if row["close"] is not None else None for row in rows]
            anchors += len(rows)
            first = timestamps[0] if first is None else first
            last = timestamps[-1]
            for minutes, bars in HORIZONS.items():
                # A monotonic deque supplies the exact future extrema window
                # [anchor + 1, anchor + bars] in O(segment length).
                maxq: deque[int] = deque()
                minq: deque[int] = deque()
                initial_end = min(bars, len(rows) - 1)
                for index in range(1, initial_end + 1):
                    value = closes[index]
                    if value is None:
                        continue
                    while maxq and closes[maxq[-1]] is not None and closes[maxq[-1]] <= value:
                        maxq.pop()
                    while minq and closes[minq[-1]] is not None and closes[minq[-1]] >= value:
                        minq.pop()
                    maxq.append(index)
                    minq.append(index)
                for index in range(len(rows)):
                    endpoint = index + bars
                    exact = (
                        endpoint < len(rows)
                        and timestamps[endpoint] - timestamps[index] == bars * FIVE_MINUTES_MS
                    )
                    if exact:
                        row = _outcome_row(
                            asset=asset,
                            timestamp=timestamps[index],
                            segment=segment,
                            horizon=minutes,
                            close=closes[index],
                            endpoint_close=closes[endpoint],
                            maximum=closes[maxq[0]] if maxq else None,
                            minimum=closes[minq[0]] if minq else None,
                            reason=None,
                        )
                    else:
                        reason = "SEGMENT_END" if endpoint >= len(rows) else "MISSING_ENDPOINT"
                        row = _outcome_row(
                            asset=asset,
                            timestamp=timestamps[index],
                            segment=segment,
                            horizon=minutes,
                            close=closes[index],
                            endpoint_close=None,
                            maximum=None,
                            minimum=None,
                            reason=reason,
                        )
                    batch.append(row)
                    # Public accounting is (available, unavailable).
                    availability[minutes][1 - int(row[13])] += 1
                    if len(batch) >= 5_000:
                        connection.executemany(
                            f"INSERT INTO intelligence_outcomes_v1 VALUES ({','.join('?' for _ in _OUTCOME_COLUMNS)})",
                            batch,
                        )
                        batch.clear()
                    # Advance the next exact forward window.  Deques are kept
                    # in chronological index order, with extrema at the front.
                    leaving = index + 1
                    while maxq and maxq[0] <= leaving:
                        maxq.popleft()
                    while minq and minq[0] <= leaving:
                        minq.popleft()
                    entering = endpoint + 1
                    if entering < len(rows):
                        value = closes[entering]
                        if value is not None:
                            while (
                                maxq and closes[maxq[-1]] is not None and closes[maxq[-1]] <= value
                            ):
                                maxq.pop()
                            while (
                                minq and closes[minq[-1]] is not None and closes[minq[-1]] >= value
                            ):
                                minq.pop()
                            maxq.append(entering)
                            minq.append(entering)
        if not anchors:
            raise ValueError(f"{asset} aligned dataset has no anchors")
        if batch:
            connection.executemany(
                f"INSERT INTO intelligence_outcomes_v1 VALUES ({','.join('?' for _ in _OUTCOME_COLUMNS)})",
                batch,
            )
        row_count = anchors * len(HORIZONS)
        digest = _digest_rows(
            connection.execute(
                "SELECT * FROM intelligence_outcomes_v1 WHERE asset=? ORDER BY asset,timestamp_ms,horizon_minutes",
                (asset,),
            )
        )
        encoded_availability = json.dumps(
            {str(key): value for key, value in availability.items()},
            sort_keys=True,
            separators=(",", ":"),
        )
        connection.execute(
            """UPDATE outcome_manifest_v1 SET state='VERIFIED',row_count=?,availability_json=?,first_timestamp_ms=?,last_timestamp_ms=?,content_digest=?,detail=?,verified_at=? WHERE asset=?""",
            (row_count, encoded_availability, first, last, digest, "verified", _now(), asset),
        )
        connection.commit()
        return OutcomeResult(
            asset, "VERIFIED", row_count, {key: tuple(value) for key, value in availability.items()}
        )
    except Exception:
        connection.rollback()
        raise
    finally:
        store.close()


def iter_outcome_batches(
    *,
    store_path: Path,
    asset: str,
    horizon_minutes: int | None = None,
    columns: Sequence[str] = ("*",),
    start_ms: int | None = None,
    end_ms: int | None = None,
    batch_size: int = 5_000,
) -> Iterator[tuple[sqlite3.Row, ...]]:
    """Read ordered compact outcome rows without touching legacy source payloads."""
    selected = tuple(columns)
    if (
        batch_size <= 0
        or not selected
        or (selected != ("*",) and not set(selected) <= set(_OUTCOME_COLUMNS))
    ):
        raise ValueError("invalid outcome batch request")
    store = ResearchStore(store_path)
    store.initialize()
    query = f"SELECT {', '.join(selected)} FROM intelligence_outcomes_v1 WHERE asset=?"
    params: list[object] = [asset]
    if horizon_minutes is not None:
        if horizon_minutes not in HORIZONS:
            store.close()
            raise ValueError("unsupported outcome horizon")
        query += " AND horizon_minutes=?"
        params.append(horizon_minutes)
    if start_ms is not None:
        query += " AND timestamp_ms>=?"
        params.append(start_ms)
    if end_ms is not None:
        query += " AND timestamp_ms<?"
        params.append(end_ms)
    query += " ORDER BY asset,timestamp_ms,horizon_minutes"
    cursor = store.connection.execute(query, params)
    try:
        while batch := tuple(cursor.fetchmany(batch_size)):
            yield batch
    finally:
        store.close()
