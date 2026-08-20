"""Read-only adapter from canonical compact SQLite rows to the E1 executor surface."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from app.ai.rde_query_executor import HORIZON_MINUTES, METRIC_COLUMNS
from app.research.data.cache_identity import (
    VerifiedResearchCacheIdentityV1,
    build_verified_research_cache_identity,
)


@dataclass(frozen=True, slots=True)
class CanonicalEventRowV1:
    event_id: str
    asset: str
    event_ms: int
    raw_row_count: int


@dataclass(frozen=True, slots=True)
class CanonicalPairRowV1:
    event_id: str
    match_id: str
    asset: str
    event_ms: int
    control_asset: str
    control_ms: int
    event_value: float | None = None
    control_value: float | None = None


class CanonicalExistingCacheStorageV1:
    """Never writes; all SQL executes on a SQLite read-only URI connection."""

    def __init__(self, database_path: Path, *, asset: str, family: str, horizon: str) -> None:
        if horizon not in HORIZON_MINUTES:
            raise ValueError("CANONICAL_HORIZON_INVALID")
        self.database_path = database_path
        self.asset = asset
        self.family = family
        self._horizon = horizon
        self.connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
        self.connection.row_factory = sqlite3.Row
        self.event_reads = 0
        self.match_reads = 0
        self.outcome_reads = 0
        self._load_verified_identity()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def reads(self) -> int:
        return self.event_reads + self.match_reads + self.outcome_reads

    def _manifest(self, table: str, where: str, parameters: tuple[str, ...]) -> dict[str, object]:
        row = self.connection.execute(f"SELECT * FROM {table} WHERE {where}", parameters).fetchone()
        if row is None:
            raise ValueError("CANONICAL_MANIFEST_MISSING")
        return dict(row)

    def _load_verified_identity(self) -> None:
        aligned = self._manifest("aligned_manifest_v1", "asset=?", (self.asset,))
        outcome = self._manifest("outcome_manifest_v1", "asset=?", (self.asset,))
        event_match = self._manifest(
            "event_match_manifest_v1", "asset=? AND event_type=?", (self.asset, self.family)
        )
        identity = build_verified_research_cache_identity(aligned, outcome, event_match)
        if identity is None:
            raise ValueError("CANONICAL_VERIFIED_IDENTITY_INVALID")
        self.identity: VerifiedResearchCacheIdentityV1 = identity
        self.aligned_digest = identity.aligned_content_digest
        self.outcome_digest = identity.outcome_content_digest
        self.event_match_digest = identity.event_match_content_digest

    def read_events(self, asset: str, family: str, variant: str) -> tuple[CanonicalEventRowV1, ...]:
        if (asset, family, variant) != (self.asset, self.family, "BASE"):
            return ()
        self.event_reads += 1
        rows = self.connection.execute(
            """SELECT event_id, asset, anchor_timestamp_ms, raw_row_count
               FROM intelligence_events_v1
               WHERE asset=? AND event_type=?
               ORDER BY anchor_timestamp_ms, event_id""",
            (asset, family),
        ).fetchall()
        return tuple(
            CanonicalEventRowV1(
                row["event_id"], row["asset"], row["anchor_timestamp_ms"], row["raw_row_count"]
            )
            for row in rows
        )

    def read_matches(self, event_ids: tuple[str, ...]) -> tuple[CanonicalPairRowV1, ...]:
        if not event_ids:
            return ()
        self.match_reads += 1
        placeholders = ",".join("?" for _ in event_ids)
        rows = self.connection.execute(
            f"""SELECT event_id, asset, horizon_minutes, event_anchor_timestamp_ms,
                       control_anchor_timestamp_ms
                FROM event_matches_v1
                WHERE asset=? AND horizon_minutes=? AND matching_status='MATCHED'
                  AND control_anchor_timestamp_ms IS NOT NULL AND event_id IN ({placeholders})
                ORDER BY event_anchor_timestamp_ms, event_id""",
            (self.asset, HORIZON_MINUTES[self._horizon], *event_ids),
        ).fetchall()
        return tuple(
            CanonicalPairRowV1(
                row["event_id"],
                f"{row['event_id']}:{row['horizon_minutes']}",
                row["asset"],
                row["event_anchor_timestamp_ms"],
                row["asset"],
                row["control_anchor_timestamp_ms"],
            )
            for row in rows
        )

    def read_outcomes(
        self, pairs: tuple[CanonicalPairRowV1, ...], metric: str, horizon: str
    ) -> tuple[CanonicalPairRowV1, ...]:
        if metric not in METRIC_COLUMNS or horizon not in HORIZON_MINUTES:
            return ()
        if not pairs:
            return ()
        self.outcome_reads += 1
        event_ids = tuple(pair.event_id for pair in pairs)
        placeholders = ",".join("?" for _ in event_ids)
        column = METRIC_COLUMNS[metric]
        rows = self.connection.execute(
            f"""SELECT m.event_id,
                       CASE WHEN event_outcome.outcome_available=1 THEN event_outcome.{column} END AS event_value,
                       CASE WHEN control_outcome.outcome_available=1 THEN control_outcome.{column} END AS control_value
                FROM event_matches_v1 m
                LEFT JOIN intelligence_outcomes_v1 event_outcome
                  ON event_outcome.asset=m.asset
                 AND event_outcome.timestamp_ms=m.event_anchor_timestamp_ms
                 AND event_outcome.horizon_minutes=m.horizon_minutes
                LEFT JOIN intelligence_outcomes_v1 control_outcome
                  ON control_outcome.asset=m.asset
                 AND control_outcome.timestamp_ms=m.control_anchor_timestamp_ms
                 AND control_outcome.horizon_minutes=m.horizon_minutes
                WHERE m.asset=? AND m.horizon_minutes=? AND m.event_id IN ({placeholders})""",
            (self.asset, HORIZON_MINUTES[horizon], *event_ids),
        ).fetchall()
        values = {row["event_id"]: (row["event_value"], row["control_value"]) for row in rows}
        return tuple(
            CanonicalPairRowV1(
                pair.event_id,
                pair.match_id,
                pair.asset,
                pair.event_ms,
                pair.control_asset,
                pair.control_ms,
                *values.get(pair.event_id, (None, None)),
            )
            for pair in pairs
        )
