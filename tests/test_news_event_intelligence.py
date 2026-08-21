import sqlite3
from datetime import UTC, datetime

import pytest

from app.research.intelligence.news_events import (
    ASSET_GROUPS,
    AIEnrichmentV1,
    EventFamily,
    EventInterpretationV1,
    FirstSeenBasis,
    HypothesisValidationStatus,
    MarketHypothesisV1,
    MarketKnownBasis,
    NewsEventStore,
    NewsEventV1,
    RDEBridgeRecordV1,
    SourceQuality,
    SourceReferenceV1,
    TimestampPrecision,
    classify_source_reference,
    dedup_group_id,
    fallback_dedup_candidate,
    market_known_at_for,
    strict_after_five_minute_anchor,
    taxonomy_digest,
    taxonomy_payload,
)


def _time(hour=12, minute=1, second=0, millisecond=0):
    return datetime(2024, 1, 10, hour, minute, second, millisecond * 1000, tzinfo=UTC)


def _reference(
    *,
    title="FOMC decision",
    record="release-1",
    precision=TimestampPrecision.SECOND,
    published=None,
    updated=None,
    metadata=(),
):
    return SourceReferenceV1(
        record,
        "Federal Reserve",
        SourceQuality.PRIMARY_OFFICIAL,
        f"https://example.test/{record}",
        published or _time(),
        updated,
        published or _time(),
        precision,
        "UTC" if precision not in {TimestampPrecision.DATE, TimestampPrecision.UNKNOWN} else None,
        title,
        "Official public release",
        "a" * 64,
        _time(hour=13),
        metadata,
    )


def _event(reference=None, *, entities=("FED",), known=None):
    reference = reference or _reference()
    known = _time() if known is None else known
    return NewsEventV1(
        (reference,),
        _time(),
        FirstSeenBasis.SOURCE_DECLARED,
        EventInterpretationV1(
            EventFamily.MACRO,
            "FOMC_DECISION",
            entities,
            (),
            ("ALL_CRYPTO", "MACRO_GLOBAL"),
            None,
            None,
            "en",
            0.9,
            "pending",
            known,
            MarketKnownBasis.SOURCE_PUBLISHED_PRECISE,
            "rules/1.0.0",
        ),
    )


def test_taxonomy_digest_is_deterministic_descriptive_and_versioned():
    assert taxonomy_payload()["name"] == "news_event_taxonomy"
    assert taxonomy_digest() == taxonomy_digest()
    assert "BULLISH" not in str(taxonomy_payload())
    changed = taxonomy_payload()
    changed["version"] = "2.0.0"
    assert taxonomy_digest(changed) != taxonomy_digest()


def test_source_reference_identity_uses_facts_not_retrieval_metadata():
    first = _reference(metadata=(("release_number", "2024-1"),))
    second = _reference(metadata=(("release_number", "2024-1"),))
    assert first.source_reference_id == second.source_reference_id
    assert first.source_quality == SourceQuality.PRIMARY_OFFICIAL


def test_source_quality_is_provenance_only_not_market_direction_or_impact():
    official = _reference(record="shared-release")
    secondary = SourceReferenceV1(
        official.source_record_id,
        official.source_name,
        SourceQuality.SECONDARY_AGGREGATOR,
        official.canonical_reference,
        official.published_at,
        official.updated_at,
        official.source_timestamp,
        official.timestamp_precision,
        official.source_timezone,
        official.title,
        official.excerpt,
        official.raw_content_sha256,
        official.retrieved_at,
    )
    assert official.source_reference_id == secondary.source_reference_id
    assert not {name for name in SourceQuality if name.value in {"BULLISH", "BEARISH"}}


def test_event_identity_is_immune_to_ai_interpretation_and_factual_parts_are_frozen():
    event = _event()
    enrichment = AIEnrichmentV1(
        event.event_id,
        "example",
        "model",
        "1",
        "prompt/1",
        _time(hour=14),
        "summary",
        "conflicting opinion",
        (),
        ("ambiguous",),
        (),
        0.1,
        "b" * 64,
        "c" * 64,
    )
    assert event.event_id != enrichment.enrichment_id
    with pytest.raises(AttributeError):
        event.source_references = ()
    assert event.interpretation.market_known_at == _time()


def test_time_contract_rejects_date_only_intraday_anchor_and_never_uses_updated_at():
    date_only = _reference(precision=TimestampPrecision.DATE, updated=_time(hour=18))
    known, basis = market_known_at_for(date_only)
    assert known is None and basis == MarketKnownBasis.UNAVAILABLE_DATE_ONLY
    assert strict_after_five_minute_anchor(known) is None
    precise = _reference(updated=_time(hour=18))
    known, basis = market_known_at_for(precise)
    assert known == _time() and basis == MarketKnownBasis.SOURCE_PUBLISHED_PRECISE


def test_strict_after_five_minute_anchor_has_exact_boundaries():
    assert strict_after_five_minute_anchor(_time(12, 1)) == int(_time(12, 5).timestamp() * 1000) - 1
    assert (
        strict_after_five_minute_anchor(_time(12, 4, 59, 999))
        == int(_time(12, 10).timestamp() * 1000) - 1
    )


def test_asset_group_mapping_is_multi_asset_and_not_forced_to_btc():
    result = classify_source_reference(
        _reference(title="Ethereum network outage", record="eth-outage")
    )
    assert result.affected_assets == ("ETH",)
    assert "BTC" not in result.affected_assets
    assert set(ASSET_GROUPS) >= {"ALL_CRYPTO", "MACRO_GLOBAL"}


def test_dedup_prefers_authoritative_identity_and_ambiguity_stays_separate():
    first = _event(_reference(record="one", metadata=(("release_number", "34-123"),)))
    second = _event(_reference(record="two", metadata=(("release_number", "34-123"),)))
    assert dedup_group_id(first) == dedup_group_id(second)
    uncertain = _event(
        _reference(record="three", title="different federal reserve publication"),
        entities=("OTHER",),
    )
    assert not fallback_dedup_candidate(first, uncertain)
    assert dedup_group_id(first) != dedup_group_id(uncertain)


@pytest.mark.parametrize(
    ("title", "subtype"),
    [
        ("FOMC statement", "FOMC_DECISION"),
        ("Consumer Price Index CPI release", "CPI_RELEASE"),
        ("Employment Situation nonfarm payroll", "EMPLOYMENT_RELEASE"),
        ("ETF approval", "ETF_APPROVAL"),
        ("ETF rejection", "ETF_REJECTION"),
        ("ETF delay", "ETF_DELAY"),
        ("Exchange withdrawal suspend", "WITHDRAWAL_SUSPENSION"),
        ("Exchange outage", "EXCHANGE_OUTAGE"),
        ("Exchange hack", "EXCHANGE_HACK"),
        ("Listing announcement", "LISTING"),
        ("Delisting announcement", "DELISTING"),
        ("Ethereum network outage", "NETWORK_OUTAGE"),
    ],
)
def test_classifier_rules_are_deterministic(title, subtype):
    assert classify_source_reference(_reference(title=title, record=title)).event_subtype == subtype


def test_classifier_fails_closed_to_unclassified():
    result = classify_source_reference(_reference(title="General publication", record="unknown"))
    assert result.status == "UNCLASSIFIED" and result.event_type is None


def test_hypothesis_cannot_claim_supported_without_deterministic_validator():
    kwargs = {
        "created_at": _time(hour=14),
        "information_cutoff": _time(),
        "source_event_ids": ("a",),
        "market_state_digest": "a" * 64,
        "claim": "test",
        "target_assets": ("ETH",),
        "target_horizon": "1h",
        "scenario": "base",
        "confidence": 0.2,
        "model_identity": "optional-ai",
        "input_digest": "b" * 64,
    }
    with pytest.raises(ValueError):
        MarketHypothesisV1(**kwargs, validation_status=HypothesisValidationStatus.SUPPORTED)
    with pytest.raises(ValueError):
        MarketHypothesisV1(
            **kwargs,
            validation_status=HypothesisValidationStatus.SUPPORTED,
            validator_identity="pretend-ai-validator",
        )
    assert MarketHypothesisV1(**kwargs).validation_status == HypothesisValidationStatus.UNVALIDATED


def test_sidecar_store_is_append_only_and_separate_from_runtime_database(tmp_path):
    path = tmp_path / "news_events_v1.sqlite3"
    store = NewsEventStore(path)
    store.initialize()
    reference, event = _reference(), _event()
    store.add_source_reference(reference)
    store.add_event(event, classify_source_reference(reference))
    enrichment = AIEnrichmentV1(
        event.event_id,
        "offline-test",
        "none",
        "1",
        "prompt/1",
        _time(hour=14),
        "candidate summary",
        "conflicting interpretation",
        (),
        ("ambiguous",),
        (),
        0.2,
        "c" * 64,
        "d" * 64,
    )
    store.add_enrichment(enrichment)
    bridge = RDEBridgeRecordV1(
        event.event_id,
        _time(),
        strict_after_five_minute_anchor(_time()),
        239_999,
        "a" * 64,
        "outcomes/v1",
        "compact_matched_event_research",
        "1.0.0",
        "b" * 64,
    )
    store.add_bridge_record(bridge)
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute("UPDATE source_references_v1 SET payload_json='bad'")
    assert path.name == "news_events_v1.sqlite3"
    table_names = {
        row[0]
        for row in store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "source_references_v1",
        "news_events_v1",
        "dedup_groups_v1",
        "classifier_evidence_v1",
        "ai_enrichments_v1",
        "rde_bridge_records_v1",
    } <= table_names
    assert "orders" not in " ".join(table_names)
    assert (
        store.connection.execute("SELECT event_id FROM news_events_v1").fetchone()[0]
        == event.event_id
    )
    store.close()
