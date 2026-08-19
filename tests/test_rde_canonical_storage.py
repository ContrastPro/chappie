"""The canonical adapter exposes only persisted event/match/outcome facts read-only."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.ai.rde_canonical_storage import CanonicalExistingCacheStorageV1


def _database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE aligned_manifest_v1 (asset TEXT, state TEXT, alignment_schema TEXT, content_digest TEXT);
        CREATE TABLE outcome_manifest_v1 (asset TEXT, state TEXT, outcome_schema TEXT,
            aligned_content_digest TEXT, outcome_config_digest TEXT, content_digest TEXT);
        CREATE TABLE event_match_manifest_v1 (asset TEXT, event_type TEXT, state TEXT,
            bridge_schema TEXT, aligned_content_digest TEXT, outcome_content_digest TEXT,
            config_digest TEXT, content_digest TEXT);
        CREATE TABLE intelligence_events_v1 (event_id TEXT, event_type TEXT, asset TEXT,
            anchor_timestamp_ms INTEGER, raw_row_count INTEGER);
        CREATE TABLE event_matches_v1 (event_id TEXT, asset TEXT, horizon_minutes INTEGER,
            event_anchor_timestamp_ms INTEGER, control_anchor_timestamp_ms INTEGER,
            matching_status TEXT);
        CREATE TABLE intelligence_outcomes_v1 (asset TEXT, timestamp_ms INTEGER,
            horizon_minutes INTEGER, forward_return_bps REAL, outcome_available INTEGER);
        """
    )
    connection.execute(
        "INSERT INTO aligned_manifest_v1 VALUES ('BTCUSDT','VERIFIED','alignment/v1','aligned')"
    )
    connection.execute(
        "INSERT INTO outcome_manifest_v1 VALUES ('BTCUSDT','VERIFIED','outcome/v1','aligned','config','outcome')"
    )
    connection.execute(
        "INSERT INTO event_match_manifest_v1 VALUES ('BTCUSDT','CROWDED_EXPANSION','VERIFIED','bridge/v1','aligned','outcome','config','match')"
    )
    connection.execute(
        "INSERT INTO intelligence_events_v1 VALUES ('event','CROWDED_EXPANSION','BTCUSDT',1000,2)"
    )
    connection.execute(
        "INSERT INTO event_matches_v1 VALUES ('event','BTCUSDT',360,1000,2000,'MATCHED')"
    )
    connection.executemany(
        "INSERT INTO intelligence_outcomes_v1 VALUES (?,?,?,?,?)",
        (("BTCUSDT", 1000, 360, 10.0, 1), ("BTCUSDT", 2000, 360, -5.0, 1)),
    )
    connection.commit()
    connection.close()


def test_adapter_reads_persisted_rows_and_rejects_writes(tmp_path: Path) -> None:
    database = tmp_path / "research.sqlite3"
    _database(database)
    with CanonicalExistingCacheStorageV1(
        database, asset="BTCUSDT", family="CROWDED_EXPANSION", horizon="6h"
    ) as storage:
        events = storage.read_events("BTCUSDT", "CROWDED_EXPANSION", "BASE")
        matches = storage.read_matches(tuple(item.event_id for item in events))
        outcomes = storage.read_outcomes(matches, "FORWARD_RETURN_BPS", "6h")
        assert events[0].raw_row_count == 2
        assert matches[0].match_id == "event:360"
        assert (outcomes[0].event_value, outcomes[0].control_value) == (10.0, -5.0)
        with pytest.raises(sqlite3.OperationalError):
            storage.connection.execute(
                "INSERT INTO intelligence_events_v1 VALUES ('x','x','x',0,1)"
            )
