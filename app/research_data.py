"""Compact, typed SQLite store for offline Market Intelligence research.

SQLite is deliberately used here because it is a dependency-free, indexed,
typed local analytical cache.  It is not part of the application persistence
surface and is never opened by the trading runtime.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

SCHEMA_VERSION = "chappie-research-data/v2"
HORIZON_BARS = {"1h": 12, "3h": 36, "6h": 72, "12h": 144, "24h": 288}


@dataclass(frozen=True, slots=True)
class IntelligenceRow:
    timestamp_ms: int
    segment_id: int
    asset: str
    close: float
    mark_close: float | None
    index_close: float | None
    premium_bps: float | None
    funding: float | None
    oi_change_5m: float | None
    price_oi_state: str | None
    trend: str | None
    volatility_bucket: str | None
    relative_volume: float | None


def _digest(rows: Iterable[IntelligenceRow]) -> str:
    return hashlib.sha256(
        "\n".join(
            json.dumps(asdict(row), sort_keys=True, separators=(",", ":")) for row in rows
        ).encode()
    ).hexdigest()


class ResearchStore:
    """A compact cache with deterministic batches and segment-safe outcomes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row

    def close(self) -> None:
        self.connection.close()

    def initialize(self) -> None:
        self.connection.executescript(
            """
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            CREATE TABLE IF NOT EXISTS intelligence_rows (
                asset TEXT NOT NULL, timestamp_ms INTEGER NOT NULL, segment_id INTEGER NOT NULL,
                close REAL NOT NULL, mark_close REAL, index_close REAL, premium_bps REAL,
                funding REAL, oi_change_5m REAL, price_oi_state TEXT, trend TEXT,
                volatility_bucket TEXT, relative_volume REAL,
                PRIMARY KEY (asset, timestamp_ms)
            ) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS intelligence_segment_range
              ON intelligence_rows(asset, segment_id, timestamp_ms);
            CREATE TABLE IF NOT EXISTS research_manifest (
                name TEXT PRIMARY KEY, value TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS conversion_manifest (
                dataset TEXT PRIMARY KEY, state TEXT NOT NULL, source_path TEXT NOT NULL,
                source_sha256 TEXT NOT NULL, parsed_rows INTEGER NOT NULL DEFAULT 0,
                inserted_rows INTEGER NOT NULL DEFAULT 0, first_timestamp_ms INTEGER,
                last_timestamp_ms INTEGER, content_digest TEXT, detail TEXT,
                source_identity TEXT NOT NULL DEFAULT '', conversion_schema TEXT NOT NULL DEFAULT '',
                chunk_count INTEGER NOT NULL DEFAULT 0, started_at TEXT, updated_at TEXT,
                verified_at TEXT
            ) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS conversion_chunks (
                dataset TEXT NOT NULL, chunk_index INTEGER NOT NULL,
                first_row_index INTEGER NOT NULL, last_row_index INTEGER NOT NULL,
                last_timestamp_ms INTEGER NOT NULL, row_count INTEGER NOT NULL,
                chunk_digest TEXT NOT NULL,
                PRIMARY KEY(dataset, chunk_index)
            ) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS flow_observations_v1 (
                dataset TEXT NOT NULL, asset TEXT NOT NULL, timestamp_ms INTEGER NOT NULL,
                segment_id INTEGER NOT NULL, close REAL NOT NULL, quote_volume REAL,
                taker_buy_quote_volume REAL, taker_sell_quote_volume REAL, taker_buy_ratio REAL,
                signed_imbalance REAL, trade_count INTEGER, relative_volume REAL, open_interest REAL,
                open_interest_value REAL, oi_change_5m REAL, oi_change_15m REAL, oi_change_1h REAL,
                futures_taker_ratio REAL, global_positioning REAL, top_positioning REAL, funding REAL,
                price_oi_state TEXT, positioning_state TEXT, futures_taker_state TEXT,
                PRIMARY KEY(dataset, timestamp_ms)
            ) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS flow_asset_segment_time ON flow_observations_v1(asset, segment_id, timestamp_ms);
            CREATE TABLE IF NOT EXISTS basis_observations_v1 (
                dataset TEXT NOT NULL, asset TEXT NOT NULL, timestamp_ms INTEGER NOT NULL,
                segment_id INTEGER NOT NULL, mark_close REAL NOT NULL, index_close REAL NOT NULL,
                premium_bps REAL NOT NULL, premium_change_5m REAL, premium_change_15m REAL,
                premium_change_1h REAL, rolling_mean_bps REAL, rolling_percentile REAL, state TEXT,
                PRIMARY KEY(dataset, timestamp_ms)
            ) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS basis_asset_segment_time ON basis_observations_v1(asset, segment_id, timestamp_ms);
            """
        )
        # Existing local research caches predate the expanded v2D manifest.
        # These additive fields keep those disposable caches readable while a
        # new conversion publishes its own v2D checkpoint data.
        known = {
            row[1] for row in self.connection.execute("PRAGMA table_info(conversion_manifest)")
        }
        for name, declaration in (
            ("source_identity", "TEXT NOT NULL DEFAULT ''"),
            ("conversion_schema", "TEXT NOT NULL DEFAULT ''"),
            ("chunk_count", "INTEGER NOT NULL DEFAULT 0"),
            ("started_at", "TEXT"),
            ("updated_at", "TEXT"),
            ("verified_at", "TEXT"),
        ):
            if name not in known:
                self.connection.execute(
                    f"ALTER TABLE conversion_manifest ADD COLUMN {name} {declaration}"
                )
        self.connection.commit()

    def replace_rows(self, rows: Sequence[IntelligenceRow], *, source_digest: str) -> None:
        if tuple(sorted((row.asset, row.timestamp_ms) for row in rows)) != tuple(
            (row.asset, row.timestamp_ms) for row in rows
        ):
            raise ValueError("rows must be canonical asset/timestamp order")
        self.connection.execute("DELETE FROM intelligence_rows")
        self.connection.executemany(
            """INSERT INTO intelligence_rows VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    r.asset,
                    r.timestamp_ms,
                    r.segment_id,
                    r.close,
                    r.mark_close,
                    r.index_close,
                    r.premium_bps,
                    r.funding,
                    r.oi_change_5m,
                    r.price_oi_state,
                    r.trend,
                    r.volatility_bucket,
                    r.relative_volume,
                )
                for r in rows
            ],
        )
        payload = {
            "schema": SCHEMA_VERSION,
            "source_digest": source_digest,
            "content_digest": _digest(rows),
            "row_count": len(rows),
        }
        self.connection.execute("DELETE FROM research_manifest")
        self.connection.executemany(
            "INSERT INTO research_manifest VALUES (?, ?)", [(k, str(v)) for k, v in payload.items()]
        )
        self.connection.commit()

    def manifest(self) -> dict[str, str]:
        return {
            row["name"]: row["value"]
            for row in self.connection.execute("SELECT * FROM research_manifest")
        }

    def iter_batches(
        self,
        *,
        asset: str,
        start_ms: int | None = None,
        end_ms: int | None = None,
        columns: Sequence[str] = ("*",),
        batch_size: int = 10_000,
    ) -> Iterator[tuple[sqlite3.Row, ...]]:
        allowed = {
            "asset",
            "timestamp_ms",
            "segment_id",
            "close",
            "mark_close",
            "index_close",
            "premium_bps",
            "funding",
            "oi_change_5m",
            "price_oi_state",
            "trend",
            "volatility_bucket",
            "relative_volume",
        }
        selected = tuple(columns)
        if not selected or (selected != ("*",) and not set(selected) <= allowed):
            raise ValueError("invalid selected columns")
        sql = f"SELECT {', '.join(selected)} FROM intelligence_rows WHERE asset = ?"
        params: list[object] = [asset]
        if start_ms is not None:
            sql += " AND timestamp_ms >= ?"
            params.append(start_ms)
        if end_ms is not None:
            sql += " AND timestamp_ms < ?"
            params.append(end_ms)
        sql += " ORDER BY timestamp_ms"
        cursor = self.connection.execute(sql, params)
        while batch := tuple(cursor.fetchmany(batch_size)):
            yield batch

    def build_outcomes(self) -> None:
        self.connection.execute("DROP TABLE IF EXISTS forward_outcomes")
        self.connection.execute(
            "CREATE TABLE forward_outcomes (asset TEXT, timestamp_ms INTEGER, horizon TEXT, return_bps REAL, mfe_bps REAL, mae_bps REAL, PRIMARY KEY(asset,timestamp_ms,horizon)) WITHOUT ROWID"
        )
        for asset_row in self.connection.execute(
            "SELECT DISTINCT asset FROM intelligence_rows ORDER BY asset"
        ):
            asset = str(asset_row[0])
            rows = list(
                self.connection.execute(
                    "SELECT timestamp_ms, segment_id, close FROM intelligence_rows WHERE asset=? ORDER BY timestamp_ms",
                    (asset,),
                )
            )
            for index, row in enumerate(rows):
                for horizon, bars in HORIZON_BARS.items():
                    if (
                        index + bars >= len(rows)
                        or rows[index + bars]["segment_id"] != row["segment_id"]
                    ):
                        continue
                    path = rows[index + 1 : index + bars + 1]
                    if any(
                        later["timestamp_ms"] - prior["timestamp_ms"] != 300_000
                        for prior, later in zip([row, *path], path)
                    ):
                        continue
                    values = [(item["close"] / row["close"] - 1) * 10_000 for item in path]
                    self.connection.execute(
                        "INSERT INTO forward_outcomes VALUES (?, ?, ?, ?, ?, ?)",
                        (asset, row["timestamp_ms"], horizon, values[-1], max(values), min(values)),
                    )
        self.connection.commit()
