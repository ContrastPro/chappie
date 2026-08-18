"""Deterministic, flow-led compact research alignment.

This is an offline research cache only.  It joins VERIFIED compact flow and
basis inputs on their exact completed-bar timestamp.  It neither interpolates
missing observations nor computes outcomes, controls, or trading signals.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.research_data import ALIGNMENT_SCHEMA_VERSION, ResearchStore

FIVE_MINUTES_MS = 300_000

_COLUMNS = (
    "asset",
    "timestamp_ms",
    "aligned_segment_id",
    "flow_segment_id",
    "basis_segment_id",
    "flow_available",
    "basis_available",
    "close",
    "relative_volume",
    "taker_buy_ratio",
    "signed_imbalance",
    "open_interest",
    "open_interest_value",
    "oi_change_5m",
    "oi_change_15m",
    "oi_change_1h",
    "price_oi_state",
    "futures_taker_ratio",
    "global_positioning",
    "top_positioning",
    "funding",
    "positioning_state",
    "futures_taker_state",
    "trend",
    "volatility_bucket",
    "mark_price",
    "index_price",
    "premium_bps",
    "premium_change_5m",
    "premium_change_15m",
    "premium_change_1h",
    "premium_state",
    "premium_dynamic_state",
)


@dataclass(frozen=True, slots=True)
class AlignmentResult:
    asset: str
    state: str
    rows: int = 0
    flow_only_rows: int = 0
    flow_basis_rows: int = 0
    aligned_segments: int = 0


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _config_digest() -> str:
    # This is intentionally a versioned description of join/segment semantics.
    payload = {
        "schema": ALIGNMENT_SCHEMA_VERSION,
        "join": "flow-led/exact-timestamp/no-forward-fill",
        "segments": "break-flow-gap-or-segment-or-basis-availability-or-basis-segment",
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _verified_source(
    connection: sqlite3.Connection, dataset: str, asset: str, table: str
) -> sqlite3.Row:
    manifest = connection.execute(
        "SELECT state,source_sha256,content_digest FROM conversion_manifest WHERE dataset=?",
        (dataset,),
    ).fetchone()
    if manifest is None or manifest["state"] != "VERIFIED":
        raise ValueError(f"{dataset} is not a VERIFIED conversion")
    if not manifest["source_sha256"] or not manifest["content_digest"]:
        raise ValueError(f"{dataset} has incomplete verified digests")
    identity = connection.execute(
        f"SELECT count(*), min(asset), max(asset), count(DISTINCT asset) FROM {table} WHERE dataset=?",
        (dataset,),
    ).fetchone()
    if (
        identity is None
        or not identity[0]
        or identity[1] != asset
        or identity[2] != asset
        or identity[3] != 1
    ):
        raise ValueError(f"{dataset} does not contain exactly asset {asset}")
    ordered = connection.execute(
        f"SELECT timestamp_ms FROM {table} WHERE dataset=? ORDER BY timestamp_ms", (dataset,)
    )
    prior: int | None = None
    for row in ordered:
        timestamp = int(row[0])
        if prior is not None and timestamp <= prior:
            raise ValueError(f"{dataset} timestamps are not strictly ordered")
        prior = timestamp
    return manifest


def _row_digest(values: Sequence[object]) -> bytes:
    return json.dumps(values, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"


def build_aligned_intelligence(
    *, store_path: Path, asset: str, flow_dataset: str, basis_dataset: str
) -> AlignmentResult:
    """Build or reuse a compact, exact-timestamp flow/basis alignment for one asset."""
    store = ResearchStore(store_path)
    store.initialize()
    connection = store.connection
    try:
        flow = _verified_source(connection, flow_dataset, asset, "flow_observations_v1")
        basis = _verified_source(connection, basis_dataset, asset, "basis_observations_v1")
        config = _config_digest()
        prior = connection.execute(
            "SELECT * FROM aligned_manifest_v1 WHERE asset=?", (asset,)
        ).fetchone()
        if (
            prior
            and prior["state"] == "VERIFIED"
            and all(
                prior[key] == value
                for key, value in {
                    "flow_dataset": flow_dataset,
                    "basis_dataset": basis_dataset,
                    "flow_content_digest": flow["content_digest"],
                    "basis_content_digest": basis["content_digest"],
                    "flow_source_sha256": flow["source_sha256"],
                    "basis_source_sha256": basis["source_sha256"],
                    "build_config_digest": config,
                }.items()
            )
        ):
            return AlignmentResult(
                asset,
                "REUSED",
                int(prior["row_count"]),
                int(prior["flow_only_rows"]),
                int(prior["flow_basis_rows"]),
                int(prior["aligned_segment_count"]),
            )

        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM intelligence_observations_v1 WHERE asset=?", (asset,))
        connection.execute(
            """INSERT OR REPLACE INTO aligned_manifest_v1(
                asset,state,alignment_schema,flow_dataset,basis_dataset,flow_content_digest,basis_content_digest,
                flow_source_sha256,basis_source_sha256,build_config_digest,started_at,detail
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                asset,
                "IN_PROGRESS",
                ALIGNMENT_SCHEMA_VERSION,
                flow_dataset,
                basis_dataset,
                flow["content_digest"],
                basis["content_digest"],
                flow["source_sha256"],
                basis["source_sha256"],
                config,
                _now(),
                "building exact flow-led alignment",
            ),
        )
        # One ordered SQL cursor streams the primary timeline.  A missing basis row
        # stays NULL; basis-only timestamps are deliberately absent from this result.
        query = """
            SELECT f.asset,f.timestamp_ms,f.segment_id AS flow_segment_id,f.close,f.relative_volume,
              f.taker_buy_ratio,f.signed_imbalance,f.open_interest,f.open_interest_value,f.oi_change_5m,
              f.oi_change_15m,f.oi_change_1h,f.price_oi_state,f.futures_taker_ratio,f.global_positioning,
              f.top_positioning,f.funding,f.positioning_state,f.futures_taker_state,
              b.segment_id AS basis_segment_id,b.mark_close,b.index_close,b.premium_bps,b.premium_change_5m,
              b.premium_change_15m,b.premium_change_1h,b.state AS premium_state
            FROM flow_observations_v1 AS f
            LEFT JOIN basis_observations_v1 AS b
              ON b.dataset=? AND b.asset=f.asset AND b.timestamp_ms=f.timestamp_ms
            WHERE f.dataset=? AND f.asset=?
            ORDER BY f.asset,f.timestamp_ms
        """
        cursor = connection.execute(query, (basis_dataset, flow_dataset, asset))
        digest = hashlib.sha256()
        batch: list[tuple[object, ...]] = []
        count = only = with_basis = segments = 0
        first: int | None = None
        last: int | None = None
        previous_time: int | None = None
        previous_flow_segment: int | None = None
        previous_basis_segment: int | None = None
        previous_available: bool | None = None
        aligned_segment = -1
        for source in cursor:
            timestamp, flow_segment = int(source["timestamp_ms"]), int(source["flow_segment_id"])
            available = source["basis_segment_id"] is not None
            discontinuity = (
                previous_time is None
                or timestamp - previous_time != FIVE_MINUTES_MS
                or flow_segment != previous_flow_segment
                or available != previous_available
                or (
                    available
                    and previous_available
                    and int(source["basis_segment_id"]) != previous_basis_segment
                )
            )
            if discontinuity:
                aligned_segment += 1
                segments += 1
            premium_change = source["premium_change_5m"]
            dynamic = (
                None
                if premium_change is None
                else (
                    "WIDENING"
                    if premium_change > 0
                    else "NARROWING"
                    if premium_change < 0
                    else "FLAT"
                )
            )
            row = (
                source["asset"],
                timestamp,
                aligned_segment,
                flow_segment,
                source["basis_segment_id"],
                1,
                int(available),
                source["close"],
                source["relative_volume"],
                source["taker_buy_ratio"],
                source["signed_imbalance"],
                source["open_interest"],
                source["open_interest_value"],
                source["oi_change_5m"],
                source["oi_change_15m"],
                source["oi_change_1h"],
                source["price_oi_state"],
                source["futures_taker_ratio"],
                source["global_positioning"],
                source["top_positioning"],
                source["funding"],
                source["positioning_state"],
                source["futures_taker_state"],
                None,
                None,
                source["mark_close"],
                source["index_close"],
                source["premium_bps"],
                source["premium_change_5m"],
                source["premium_change_15m"],
                source["premium_change_1h"],
                source["premium_state"],
                dynamic,
            )
            batch.append(row)
            digest.update(_row_digest(row))
            count += 1
            with_basis += int(available)
            only += int(not available)
            first = timestamp if first is None else first
            last = timestamp
            previous_time, previous_flow_segment = timestamp, flow_segment
            previous_available = available
            previous_basis_segment = int(source["basis_segment_id"]) if available else None
            if len(batch) >= 5_000:
                connection.executemany(
                    f"INSERT INTO intelligence_observations_v1 VALUES ({','.join('?' for _ in _COLUMNS)})",
                    batch,
                )
                batch.clear()
        if not count:
            raise ValueError("flow source produced no aligned rows")
        if batch:
            connection.executemany(
                f"INSERT INTO intelligence_observations_v1 VALUES ({','.join('?' for _ in _COLUMNS)})",
                batch,
            )
        connection.execute(
            """UPDATE aligned_manifest_v1 SET state='VERIFIED',row_count=?,flow_only_rows=?,flow_basis_rows=?,
               first_timestamp_ms=?,last_timestamp_ms=?,aligned_segment_count=?,content_digest=?,detail=?,verified_at=?
               WHERE asset=?""",
            (
                count,
                only,
                with_basis,
                first,
                last,
                segments,
                digest.hexdigest(),
                "verified",
                _now(),
                asset,
            ),
        )
        connection.commit()
        return AlignmentResult(asset, "VERIFIED", count, only, with_basis, segments)
    except Exception:
        connection.rollback()
        raise
    finally:
        store.close()


def iter_intelligence_batches(
    *,
    store_path: Path,
    asset: str,
    columns: Sequence[str] = ("*",),
    start_ms: int | None = None,
    end_ms: int | None = None,
    segment_id: int | None = None,
    batch_size: int = 5_000,
) -> Iterator[tuple[sqlite3.Row, ...]]:
    """Yield ordered selected-column compact rows without legacy JSON access."""
    selected = tuple(columns)
    allowed = set(_COLUMNS)
    if batch_size <= 0 or not selected or (selected != ("*",) and not set(selected) <= allowed):
        raise ValueError("invalid intelligence batch request")
    store = ResearchStore(store_path)
    store.initialize()
    query = f"SELECT {', '.join(selected)} FROM intelligence_observations_v1 WHERE asset=?"
    params: list[object] = [asset]
    if start_ms is not None:
        query += " AND timestamp_ms>=?"
        params.append(start_ms)
    if end_ms is not None:
        query += " AND timestamp_ms<?"
        params.append(end_ms)
    if segment_id is not None:
        query += " AND aligned_segment_id=?"
        params.append(segment_id)
    query += " ORDER BY asset,timestamp_ms"
    cursor = store.connection.execute(query, params)
    try:
        while batch := tuple(cursor.fetchmany(batch_size)):
            yield batch
    finally:
        store.close()
