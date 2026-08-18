"""Stream legacy V3B/V4C rows into compact research-only SQLite tables."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from app.research_data import ResearchStore
from app.research_json import iter_legacy_rows
from app.research_paths import compact_db_path

BATCH_SIZE = 1_000
CONVERSION_SCHEMA = "research-conversion/v2d"


def _number(value: object) -> float | None:
    return None if value is None else float(str(value))


def _source_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _source_identity(path: Path) -> str:
    stat = path.stat()
    return f"{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _flow(row: dict[str, object], dataset: str, segment: int) -> tuple[object, ...]:
    spot = row.get("spot")
    metrics = row.get("metrics")
    if not isinstance(spot, dict):
        raise TypeError("legacy flow row has no spot object")
    metric = metrics if isinstance(metrics, dict) else {}
    return (
        dataset,
        str(row["symbol"]),
        int(row["timestamp_ms"]),
        segment,
        _number(row["close"]),
        _number(spot.get("quote_volume")),
        _number(spot.get("taker_buy_quote_volume")),
        _number(spot.get("taker_sell_quote_volume")),
        _number(spot.get("taker_buy_quote_ratio")),
        _number(spot.get("signed_taker_quote_imbalance")),
        spot.get("trade_count"),
        _number(spot.get("relative_quote_volume")),
        _number(metric.get("open_interest")),
        _number(metric.get("open_interest_value")),
        _number(row.get("oi_change_5m")),
        _number(row.get("oi_change_15m")),
        _number(row.get("oi_change_1h")),
        _number(metric.get("taker_long_short_volume_ratio")),
        _number(metric.get("global_long_short_ratio")),
        _number(metric.get("top_trader_long_short_ratio")),
        _number(row.get("funding")),
        row.get("price_oi_state"),
        row.get("positioning_state"),
        row.get("futures_taker_state"),
    )


def _basis(row: dict[str, object], dataset: str, segment: int) -> tuple[object, ...]:
    return (
        dataset,
        str(row["symbol"]),
        int(row["timestamp_ms"]),
        segment,
        _number(row["mark_close"]),
        _number(row["index_close"]),
        _number(row["premium_bps"]),
        _number(row.get("premium_change_5m")),
        _number(row.get("premium_change_15m")),
        _number(row.get("premium_change_1h")),
        _number(row.get("rolling_mean_bps")),
        _number(row.get("rolling_percentile")),
        row.get("state"),
    )


def _row_digest(row: tuple[object, ...]) -> bytes:
    """Return the canonical, schema-order representation of one stored row."""

    return json.dumps(row, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"


def convert(
    *,
    store_path: Path,
    source: Path,
    dataset: str,
    kind: str,
    max_chunks: int | None = None,
) -> dict[str, object]:
    started = time.monotonic()
    formats = {"flow": "chappie-flow-positioning/v1", "basis": "chappie-leverage-stress-basis/v1"}
    table = f"{kind}_observations_v1"
    expected = formats[kind]
    print(f"Opening {dataset} ({source.stat().st_size:,} bytes)...", flush=True)
    identity = _source_identity(source)
    store = ResearchStore(store_path)
    store.initialize()
    connection = store.connection
    prior = connection.execute(
        "SELECT state, source_identity, source_sha256, inserted_rows, chunk_count "
        "FROM conversion_manifest WHERE dataset=?",
        (dataset,),
    ).fetchone()
    if (
        prior
        and prior["state"] == "VERIFIED"
        and prior["source_identity"] == identity
        and prior["source_sha256"] == _source_digest(source)
    ):
        # Reuse is permitted only after the final source digest reconfirms the
        # exact bytes that produced the VERIFIED rows.
        store.close()
        return {"dataset": dataset, "state": "REUSED"}
    resume = (
        int(prior["inserted_rows"])
        if prior and prior["state"] == "IN_PROGRESS" and prior["source_identity"] == identity
        else 0
    )
    chunks = int(prior["chunk_count"]) if resume else 0
    if not resume:
        connection.execute(f"DELETE FROM {table} WHERE dataset=?", (dataset,))
        connection.execute("DELETE FROM conversion_chunks WHERE dataset=?", (dataset,))
        chunks = 0
    connection.execute(
        """INSERT OR REPLACE INTO conversion_manifest(
               dataset,state,source_path,source_sha256,detail,source_identity,
               conversion_schema,parsed_rows,inserted_rows,chunk_count,started_at,updated_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            dataset,
            "IN_PROGRESS",
            str(source),
            "",
            "",
            identity,
            CONVERSION_SCHEMA,
            resume,
            resume,
            chunks,
            _now(),
            _now(),
        ),
    )
    parsed = segment = 0
    inserted = resume
    first = last = None
    prior_time = None
    batch: list[tuple[object, ...]] = []
    mapper = _flow if kind == "flow" else _basis
    chunk_content = hashlib.sha256()

    def commit_chunk() -> None:
        nonlocal inserted, chunks
        if not batch:
            return
        first_row = inserted + 1
        connection.executemany(
            f"INSERT INTO {table} VALUES ({','.join('?' for _ in batch[0])})", batch
        )
        inserted += len(batch)
        chunk_index = chunks + 1
        connection.execute(
            "INSERT INTO conversion_chunks VALUES(?,?,?,?,?,?,?)",
            (
                dataset,
                chunk_index,
                first_row,
                inserted,
                last,
                len(batch),
                chunk_content.hexdigest(),
            ),
        )
        connection.execute(
            """UPDATE conversion_manifest
               SET parsed_rows=?, inserted_rows=?, last_timestamp_ms=?, chunk_count=?, updated_at=?
               WHERE dataset=?""",
            (parsed, inserted, last, chunk_index, _now(), dataset),
        )
        connection.commit()
        chunks = chunk_index
        batch.clear()
        print(f"Committed {dataset}: {inserted:,} rows (chunk {chunks:,})", flush=True)

    try:
        for raw in iter_legacy_rows(source, expected_format=expected):
            timestamp = int(raw["timestamp_ms"])
            if prior_time is not None and timestamp <= prior_time:
                raise ValueError("legacy rows are not strictly ordered")
            if prior_time is not None and timestamp - prior_time != 300_000:
                segment += 1
            prior_time = timestamp
            parsed += 1
            first = timestamp if first is None else first
            last = timestamp
            row = mapper(raw, dataset, segment)
            if parsed <= resume:
                chunk_content.update(_row_digest(row))
                if parsed % BATCH_SIZE == 0:
                    expected_chunk = connection.execute(
                        "SELECT chunk_digest FROM conversion_chunks WHERE dataset=? AND chunk_index=?",
                        (dataset, parsed // BATCH_SIZE),
                    ).fetchone()
                    if (
                        expected_chunk is None
                        or expected_chunk["chunk_digest"] != chunk_content.hexdigest()
                    ):
                        raise ValueError("checkpoint chunk digest does not match source")
                    chunk_content = hashlib.sha256()
                continue
            batch.append(row)
            chunk_content.update(_row_digest(row))
            if len(batch) >= BATCH_SIZE:
                commit_chunk()
                chunk_content = hashlib.sha256()
                if max_chunks is not None and chunks >= max_chunks:
                    return {
                        "dataset": dataset,
                        "state": "IN_PROGRESS",
                        "rows": inserted,
                        "seconds": time.monotonic() - started,
                    }
        if batch:
            commit_chunk()
        if parsed != inserted or not parsed:
            raise ValueError("parsed/inserted row count mismatch")
        if _source_identity(source) != identity:
            raise ValueError("source changed during conversion")
        source_sha256 = _source_digest(source)
        final_content = hashlib.sha256()
        for chunk in connection.execute(
            "SELECT chunk_digest FROM conversion_chunks WHERE dataset=? ORDER BY chunk_index",
            (dataset,),
        ):
            final_content.update(str(chunk["chunk_digest"]).encode() + b"\n")
        connection.execute(
            """UPDATE conversion_manifest
               SET state='VERIFIED', source_sha256=?, parsed_rows=?, inserted_rows=?,
                   first_timestamp_ms=?, last_timestamp_ms=?, content_digest=?, chunk_count=?,
                   updated_at=?, verified_at=? WHERE dataset=?""",
            (
                source_sha256,
                parsed,
                inserted,
                first,
                last,
                final_content.hexdigest(),
                chunks,
                _now(),
                _now(),
                dataset,
            ),
        )
        connection.commit()
        return {
            "dataset": dataset,
            "state": "VERIFIED",
            "rows": inserted,
            "segments": segment + 1,
            "seconds": time.monotonic() - started,
        }
    except (KeyboardInterrupt, SystemExit):
        # A controlled interruption preserves the last committed chunk and its
        # IN_PROGRESS manifest.  A later invocation verifies the same source
        # digest and resumes from that durable boundary.
        connection.commit()
        raise
    except Exception as error:
        connection.rollback()
        connection.execute(
            "UPDATE conversion_manifest SET state='FAILED',detail=?,updated_at=? WHERE dataset=?",
            (str(error), _now(), dataset),
        )
        connection.commit()
        raise
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=("flow", "basis"))
    parser.add_argument("source", type=Path)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--store", type=Path, default=compact_db_path())
    parser.add_argument(
        "--max-chunks",
        type=int,
        help="stop after this many committed chunks; useful to verify resume behavior",
    )
    args = parser.parse_args(argv)
    print(
        convert(
            store_path=args.store,
            source=args.source,
            dataset=args.dataset,
            kind=args.kind,
            max_chunks=args.max_chunks,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
