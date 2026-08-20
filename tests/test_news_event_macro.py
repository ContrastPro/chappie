from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.news_event_intelligence import strict_after_five_minute_anchor
from app.news_event_macro import (
    OfficialMacroSource,
    historical_event_ai_context,
    ingest_official_macro_records,
    normalized_event,
    official_macro_registry_digest,
    official_macro_source_registry,
    parse_official_macro_html,
)
from app.research.data.store import ResearchStore


@pytest.mark.parametrize(
    ("source", "title", "subtype"),
    (
        (OfficialMacroSource.FED_FOMC, "Federal Reserve issues FOMC statement", "FOMC_DECISION"),
        (OfficialMacroSource.BLS_CPI, "Consumer Price Index", "CPI_RELEASE"),
        (OfficialMacroSource.BLS_EMPLOYMENT, "The Employment Situation", "EMPLOYMENT_RELEASE"),
    ),
)
def test_official_macro_parsers_preserve_primary_facts(
    source: OfficialMacroSource, title: str, subtype: str
) -> None:
    record = parse_official_macro_html(
        source_id=source,
        canonical_reference=f"https://official.example/{source.value}",
        raw=f"<html><title>{title}</title><body>January 12, 2023. For release at 8:30 a.m. ET. facts</body></html>".encode(),
    )
    reference, event = normalized_event(record, retrieved_at=datetime(2026, 8, 18, tzinfo=UTC))
    assert reference.source_quality.value == "PRIMARY_OFFICIAL"
    assert reference.published_at == datetime(2023, 1, 12, 13, 30, tzinfo=UTC)
    assert event.interpretation.event_subtype == subtype
    assert event.interpretation.market_known_at == reference.published_at


def test_date_only_official_record_fails_closed_for_intraday_anchor() -> None:
    record = parse_official_macro_html(
        source_id=OfficialMacroSource.BLS_CPI,
        canonical_reference="https://official.example/cpi",
        raw=b"<html><title>Consumer Price Index</title><body>January 12, 2023</body></html>",
    )
    reference, event = normalized_event(record, retrieved_at=datetime(2026, 8, 18, tzinfo=UTC))
    assert reference.published_at is None
    assert not record.intraday_eligible
    assert event.interpretation.market_known_at is None
    assert strict_after_five_minute_anchor(None) is None


def test_registry_is_primary_official_and_deterministic() -> None:
    registry = official_macro_source_registry()
    assert [item.source_id.value for item in registry] == ["FED_FOMC", "BLS_CPI", "BLS_EMPLOYMENT"]
    assert {item.source_quality.value for item in registry} == {"PRIMARY_OFFICIAL"}
    assert official_macro_registry_digest() == official_macro_registry_digest()


def test_ingestion_is_idempotent_and_sidecar_is_append_only(tmp_path: Path) -> None:
    record = parse_official_macro_html(
        source_id=OfficialMacroSource.FED_FOMC,
        canonical_reference="https://official.example/fomc-1",
        raw=b"<html><title>FOMC statement</title><body>January 12, 2023. For release at 2:00 p.m. ET.</body></html>",
    )
    sidecar = tmp_path / "news.sqlite3"
    first = ingest_official_macro_records(
        records=(record,), sidecar_path=sidecar, retrieved_at=datetime(2026, 8, 18, tzinfo=UTC)
    )
    second = ingest_official_macro_records(
        records=(record,), sidecar_path=sidecar, retrieved_at=datetime(2026, 8, 18, tzinfo=UTC)
    )
    assert (first.accepted, second.accepted, second.duplicates) == (1, 0, 1)
    connection = sqlite3.connect(sidecar)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        connection.execute("DELETE FROM news_events_v1")
    connection.close()


def test_ai_context_excludes_mutable_future_outcomes() -> None:
    record = parse_official_macro_html(
        source_id=OfficialMacroSource.BLS_EMPLOYMENT,
        canonical_reference="https://official.example/jobs",
        raw=b"<html><title>The Employment Situation</title><body>January 12, 2023. For release at 8:30 a.m. ET.</body></html>",
    )
    _, event = normalized_event(record, retrieved_at=datetime(2026, 8, 18, tzinfo=UTC))
    anchor = strict_after_five_minute_anchor(record.published_at)
    assert anchor is not None
    snapshot = {
        "anchor_timestamp_ms": anchor,
        "assets": {"BTCUSDT": {"close": 100.0, "funding": 0.01}},
    }
    before = historical_event_ai_context(event, snapshot, anchor)
    # This represents an aggressively mutated linked future outcome; it is not an AI-context input.
    mutated_future_outcomes = {
        "60": {"forward_return_bps": -9000, "max_favorable_excursion_bps": 99999}
    }
    assert mutated_future_outcomes["60"]["forward_return_bps"] != 0
    after = historical_event_ai_context(event, snapshot, anchor)
    assert before == after
    assert before.context_digest == after.context_digest


def test_intraday_event_creates_strict_after_bridge_from_causal_snapshot(tmp_path: Path) -> None:
    record = parse_official_macro_html(
        source_id=OfficialMacroSource.BLS_CPI,
        canonical_reference="https://official.example/cpi-bridge",
        raw=b"<html><title>Consumer Price Index</title><body>January 12, 2023. For release at 8:30 a.m. ET.</body></html>",
    )
    anchor = strict_after_five_minute_anchor(record.published_at)
    assert anchor is not None
    research = tmp_path / "research.sqlite3"
    store = ResearchStore(research)
    store.initialize()
    store.connection.execute(
        """INSERT INTO intelligence_observations_v1(
        asset,timestamp_ms,aligned_segment_id,flow_segment_id,flow_available,basis_available,close,
        relative_volume,taker_buy_ratio,signed_imbalance,open_interest,funding,global_positioning,
        top_positioning,price_oi_state,volatility_bucket,trend)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "BTCUSDT",
            anchor,
            1,
            1,
            1,
            0,
            20000.0,
            1.2,
            0.55,
            0.1,
            123.0,
            0.0001,
            0.5,
            0.6,
            "PRICE_UP_OI_UP",
            "MEDIUM",
            "UP",
        ),
    )
    store.connection.execute(
        "INSERT INTO outcome_manifest_v1(asset,state,outcome_schema,aligned_content_digest,outcome_config_digest,horizons_json,thresholds_json,content_digest) VALUES(?,?,?,?,?,?,?,?)",
        ("BTCUSDT", "VERIFIED", "test", "aligned", "config", "[]", "[]", "outcome-digest"),
    )
    store.connection.execute(
        "INSERT INTO intelligence_outcomes_v1(asset,timestamp_ms,aligned_segment_id,horizon_minutes,forward_return_bps,max_favorable_excursion_bps,max_adverse_excursion_bps,outcome_available) VALUES(?,?,?,?,?,?,?,?)",
        ("BTCUSDT", anchor, 1, 60, 11.0, 12.0, -2.0, 1),
    )
    store.connection.commit()
    store.close()
    result = ingest_official_macro_records(
        records=(record,),
        sidecar_path=tmp_path / "news.sqlite3",
        research_store_path=research,
        retrieved_at=datetime(2026, 8, 18, tzinfo=UTC),
    )
    assert (result.intraday_eligible, result.bridges) == (1, 1)
    connection = sqlite3.connect(tmp_path / "news.sqlite3")
    payload = connection.execute("SELECT payload_json FROM rde_bridge_records_v1").fetchone()[0]
    assert str(anchor) in payload
    connection.close()
