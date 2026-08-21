"""Research CLI validates typed artifacts without a storage adapter."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from app.ai.research.capabilities import ExistingEventResearchQueryV1
from app.tools.rde_query_execute import main
from tests.rde_executor_fixture_support import btc_fixture


def _canonical_database(path: Path) -> None:
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
        INSERT INTO aligned_manifest_v1 VALUES ('BTCUSDT','VERIFIED','alignment/v1','aligned');
        INSERT INTO outcome_manifest_v1 VALUES
            ('BTCUSDT','VERIFIED','outcome/v1','aligned','config','outcome');
        INSERT INTO event_match_manifest_v1 VALUES
            ('BTCUSDT','CROWDED_EXPANSION','VERIFIED','bridge/v1','aligned','outcome','config','match');
        """
    )
    connection.commit()
    connection.close()


def _write_query(path: Path) -> None:
    fixture = btc_fixture()
    query = ExistingEventResearchQueryV1(
        "btc-fixture-query",
        fixture.asset,
        fixture.family,
        "BASE",
        fixture.metric,
        "6h",
        "GREATER_THAN_MATCHED_CONTROL",
        "MATCHED_CONTROL",
        fixture.identity.research_cache_identity_digest,
        fixture.event_match_digest,
        fixture.outcome_digest,
    )
    path.write_text(json.dumps(asdict(query)), encoding="utf-8")


def test_dry_run_with_unverified_artifact_fails_machine_readably(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    database = tmp_path / "research.sqlite3"
    _canonical_database(database)
    monkeypatch.setattr("app.tools.rde_query_execute.compact_db_path", lambda: database)
    query = tmp_path / "query.json"
    _write_query(query)
    assert main(["--query", str(query), "--dry-run", "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "VERIFIED_CACHE_IDENTITY_INVALID"
    assert payload["capability"] == "EXECUTABLE_EXISTING_CACHE"
    assert payload["executable"] is True


def test_invalid_artifacts_fail_closed(tmp_path: Path, capsys) -> None:
    invalid = tmp_path / "invalid.json"
    invalid.write_text("[]", encoding="utf-8")
    assert main(["--query", str(invalid), "--dry-run", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "QUERY_ARTIFACT_INVALID"
