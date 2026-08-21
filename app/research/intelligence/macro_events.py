"""Official macro-event ingestion for the research-only News/Event sidecar.

The module accepts only preserved primary-source representations.  Acquisition
is deliberately separate from normalization: callers cache official HTML first,
then pass its bytes and canonical URL here.  No trading/runtime code imports it.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from zoneinfo import ZoneInfo

from app.research.intelligence.news_events import (
    DEFAULT_NEWS_EVENT_STORE_PATH,
    EventFamily,
    EventInterpretationV1,
    FirstSeenBasis,
    MarketKnownBasis,
    NewsEventStore,
    NewsEventV1,
    RDEBridgeRecordV1,
    SourceQuality,
    SourceReferenceV1,
    TimestampPrecision,
    classify_source_reference,
    strict_after_five_minute_anchor,
)
from app.research.intelligence.universe import UNIVERSE_NAME, UNIVERSE_VERSION, universe_digest
from app.research_methodology import METHODOLOGY_NAME, METHODOLOGY_VERSION, methodology_digest

REGISTRY_NAME = "news_event_official_macro_sources"
REGISTRY_VERSION = "1.0.0"
PARSER_VERSION = "official-macro-html/1.0.0"
MACRO_STORE_SCHEMA = "chappie-news-event-macro/v1"
_EASTERN = ZoneInfo("America/New_York")
_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")
_DATE = re.compile(r"([A-Z][a-z]+\s+\d{1,2},\s+\d{4})")
_TIME = re.compile(
    r"(?:for\s+release(?:\s+at)?|release\s+time\s*:)\s*(\d{1,2}:\d{2})\s*(a\.m\.|p\.m\.|AM|PM)?\s*(?:\(?([A-Z]{2,4}|ET)\)?)?",
    re.IGNORECASE,
)


class OfficialMacroSource(StrEnum):
    FED_FOMC = "FED_FOMC"
    BLS_CPI = "BLS_CPI"
    BLS_EMPLOYMENT = "BLS_EMPLOYMENT"


@dataclass(frozen=True, slots=True)
class OfficialSourceRegistryEntry:
    source_id: OfficialMacroSource
    publisher: str
    source_quality: SourceQuality
    canonical_base_reference: str
    source_family: str
    parser_version: str
    timestamp_policy: str
    retention_policy: str


def official_macro_source_registry() -> tuple[OfficialSourceRegistryEntry, ...]:
    return (
        OfficialSourceRegistryEntry(
            OfficialMacroSource.FED_FOMC,
            "Federal Reserve Board",
            SourceQuality.PRIMARY_OFFICIAL,
            "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
            "FOMC_STATEMENT",
            PARSER_VERSION,
            "explicit release line only; otherwise date-only",
            "URL/title/minimal excerpt/digest",
        ),
        OfficialSourceRegistryEntry(
            OfficialMacroSource.BLS_CPI,
            "U.S. Bureau of Labor Statistics",
            SourceQuality.PRIMARY_OFFICIAL,
            "https://www.bls.gov/news.release/cpi.htm",
            "BLS_CPI_RELEASE",
            PARSER_VERSION,
            "explicit release line only; otherwise date-only",
            "URL/title/minimal excerpt/digest",
        ),
        OfficialSourceRegistryEntry(
            OfficialMacroSource.BLS_EMPLOYMENT,
            "U.S. Bureau of Labor Statistics",
            SourceQuality.PRIMARY_OFFICIAL,
            "https://www.bls.gov/news.release/empsit.htm",
            "BLS_EMPLOYMENT_SITUATION",
            PARSER_VERSION,
            "explicit release line only; otherwise date-only",
            "URL/title/minimal excerpt/digest",
        ),
    )


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def official_macro_registry_digest() -> str:
    return hashlib.sha256(
        _canonical([asdict(item) for item in official_macro_source_registry()]).encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class ParsedOfficialMacroRecord:
    source_id: OfficialMacroSource
    release_id: str
    canonical_reference: str
    title: str
    excerpt: str
    published_at: datetime | None
    timestamp_precision: TimestampPrecision
    source_timestamp: datetime | None
    updated_at: datetime | None
    raw_content_sha256: str

    @property
    def intraday_eligible(self) -> bool:
        return self.published_at is not None and self.timestamp_precision in {
            TimestampPrecision.SECOND,
            TimestampPrecision.MILLISECOND,
        }


def _plain_text(raw: bytes) -> str:
    return _SPACE.sub(" ", _TAG.sub(" ", raw.decode("utf-8", errors="replace"))).strip()


def _date_and_time(text: str) -> tuple[datetime | None, TimestampPrecision]:
    date = _DATE.search(text)
    time = _TIME.search(text)
    if not date or not time:
        return None, TimestampPrecision.DATE
    try:
        clock = time.group(1)
        meridiem = (time.group(2) or "").replace(".", "").upper()
        if not meridiem:
            return None, TimestampPrecision.DATE
        local = datetime.strptime(  # noqa: DTZ007 -- explicit Eastern zone follows
            f"{date.group(1)} {clock} {meridiem}", "%B %d, %Y %I:%M %p"
        )
        return local.replace(tzinfo=_EASTERN).astimezone(UTC), TimestampPrecision.SECOND
    except ValueError:
        return None, TimestampPrecision.DATE


def _title(text: str, fallback: str) -> str:
    match = re.search(r"<title[^>]*>\s*(.*?)\s*</title>", text, re.IGNORECASE | re.DOTALL)
    return _SPACE.sub(" ", _TAG.sub(" ", match.group(1))).strip() if match else fallback


def parse_official_macro_html(
    *, source_id: OfficialMacroSource, canonical_reference: str, raw: bytes
) -> ParsedOfficialMacroRecord:
    """Normalize one official representation; date-only records fail closed."""
    html = raw.decode("utf-8", errors="replace")
    text = _plain_text(raw)
    published, precision = _date_and_time(text)
    title = _title(html, source_id.value.replace("_", " "))
    date = _DATE.search(text)
    release_id = hashlib.sha256(
        _canonical(
            {
                "source": source_id.value,
                "url": canonical_reference,
                "date": date.group(1) if date else None,
                "title": title,
            }
        ).encode()
    ).hexdigest()
    return ParsedOfficialMacroRecord(
        source_id,
        release_id,
        canonical_reference,
        title,
        text[:600],
        published,
        precision,
        published,
        None,
        hashlib.sha256(raw).hexdigest(),
    )


def _event_subtype(source_id: OfficialMacroSource) -> str:
    return {
        OfficialMacroSource.FED_FOMC: "FOMC_DECISION",
        OfficialMacroSource.BLS_CPI: "CPI_RELEASE",
        OfficialMacroSource.BLS_EMPLOYMENT: "EMPLOYMENT_RELEASE",
    }[source_id]


def normalized_event(
    record: ParsedOfficialMacroRecord, *, retrieved_at: datetime
) -> tuple[SourceReferenceV1, NewsEventV1]:
    entry = next(
        item for item in official_macro_source_registry() if item.source_id == record.source_id
    )
    metadata = (
        ("parser_version", entry.parser_version),
        ("release_number", record.release_id),
        ("source_id", record.source_id.value),
    )
    reference = SourceReferenceV1(
        source_record_id=record.release_id,
        source_name=entry.publisher,
        source_quality=entry.source_quality,
        canonical_reference=record.canonical_reference,
        published_at=record.published_at,
        updated_at=record.updated_at,
        source_timestamp=record.source_timestamp,
        timestamp_precision=record.timestamp_precision,
        source_timezone="America/New_York" if record.intraday_eligible else None,
        title=record.title,
        excerpt=record.excerpt,
        raw_content_sha256=record.raw_content_sha256,
        retrieved_at=retrieved_at,
        retrieval_metadata=metadata,
    )
    known = record.published_at if record.intraday_eligible else None
    interpretation = EventInterpretationV1(
        event_type=EventFamily.MACRO,
        event_subtype=_event_subtype(record.source_id),
        entities=(),
        affected_assets=(),
        affected_groups=("ALL_CRYPTO", "MACRO_GLOBAL"),
        severity=None,
        novelty=None,
        language="en",
        confidence=1.0,
        dedup_group="",
        market_known_at=known,
        market_known_basis=MarketKnownBasis.SOURCE_PUBLISHED_PRECISE
        if known
        else MarketKnownBasis.UNAVAILABLE_DATE_ONLY,
        classifier_version=PARSER_VERSION,
    )
    event = NewsEventV1(
        (reference,),
        known,
        FirstSeenBasis.SOURCE_DECLARED if known else FirstSeenBasis.HISTORICAL_BACKFILL_UNKNOWN,
        interpretation,
    )
    return reference, event


def _snapshot_at(store_path: Path, anchor: int) -> dict[str, object] | None:
    """Read only rows exactly at an anchor; no future observations enter the snapshot."""
    connection = sqlite3.connect(store_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = list(
            connection.execute(
                "SELECT * FROM intelligence_observations_v1 WHERE timestamp_ms=? ORDER BY asset",
                (anchor,),
            )
        )
        if not rows:
            return None
        fields = (
            "close",
            "relative_volume",
            "taker_buy_ratio",
            "signed_imbalance",
            "open_interest",
            "funding",
            "global_positioning",
            "top_positioning",
            "price_oi_state",
            "volatility_bucket",
            "trend",
        )
        assets = {
            row["asset"]: {field: row[field] for field in fields if field in row} for row in rows
        }
        return {
            "anchor_timestamp_ms": anchor,
            "assets": assets,
            "universe": {
                "name": UNIVERSE_NAME,
                "version": UNIVERSE_VERSION,
                "digest": universe_digest(),
            },
        }
    finally:
        connection.close()


def _outcomes_at(store_path: Path, anchor: int) -> tuple[dict[str, object], str] | None:
    connection = sqlite3.connect(store_path)
    connection.row_factory = sqlite3.Row
    try:
        manifest = connection.execute(
            "SELECT content_digest FROM outcome_manifest_v1 WHERE asset='BTCUSDT' AND state='VERIFIED'"
        ).fetchone()
        if manifest is None:
            return None
        rows = list(
            connection.execute(
                "SELECT horizon_minutes,forward_return_bps,max_favorable_excursion_bps,max_adverse_excursion_bps,outcome_available FROM intelligence_outcomes_v1 WHERE asset='BTCUSDT' AND timestamp_ms=? ORDER BY horizon_minutes",
                (anchor,),
            )
        )
        return (
            {str(row["horizon_minutes"]): dict(row) for row in rows},
            str(manifest["content_digest"]),
        )
    finally:
        connection.close()


@dataclass(frozen=True, slots=True)
class HistoricalEventAIContextV1:
    event_id: str
    event_facts: dict[str, object]
    market_snapshot: dict[str, object]
    information_cutoff: int
    context_digest: str


def historical_event_ai_context(
    event: NewsEventV1, snapshot: dict[str, object], anchor: int
) -> HistoricalEventAIContextV1:
    facts = {
        "event_id": event.event_id,
        "taxonomy_version": event.taxonomy_version,
        "source_reference_ids": [item.source_reference_id for item in event.source_references],
        "event_type": event.interpretation.event_type.value,
        "event_subtype": event.interpretation.event_subtype,
        "market_known_at_ms": int(event.interpretation.market_known_at.timestamp() * 1000)
        if event.interpretation.market_known_at
        else None,
    }
    body = {"event_facts": facts, "market_snapshot": snapshot, "information_cutoff": anchor}
    return HistoricalEventAIContextV1(
        event.event_id,
        facts,
        snapshot,
        anchor,
        hashlib.sha256(_canonical(body).encode()).hexdigest(),
    )


@dataclass(frozen=True, slots=True)
class IngestionResult:
    candidates: int
    accepted: int
    intraday_eligible: int
    date_only: int
    duplicates: int
    parse_failures: int
    bridges: int


def ingest_official_macro_records(
    *,
    records: tuple[ParsedOfficialMacroRecord, ...],
    sidecar_path: Path = DEFAULT_NEWS_EVENT_STORE_PATH,
    research_store_path: Path | None = None,
    retrieved_at: datetime,
) -> IngestionResult:
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    store = NewsEventStore(sidecar_path)
    store.initialize()
    store.connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS historical_event_ai_contexts_v1 (
            event_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL, payload_digest TEXT NOT NULL
        ) WITHOUT ROWID;
        CREATE TRIGGER IF NOT EXISTS historical_event_ai_contexts_v1_no_update
          BEFORE UPDATE ON historical_event_ai_contexts_v1 BEGIN SELECT RAISE(ABORT, 'append-only'); END;
        CREATE TRIGGER IF NOT EXISTS historical_event_ai_contexts_v1_no_delete
          BEFORE DELETE ON historical_event_ai_contexts_v1 BEGIN SELECT RAISE(ABORT, 'append-only'); END;
        """
    )
    # Manifest is append-only contract metadata; stable reruns reuse the values.
    store.connection.execute(
        "INSERT OR IGNORE INTO news_store_manifest VALUES (?,?)",
        (
            "official_macro_registry",
            f"{REGISTRY_NAME}/{REGISTRY_VERSION}:{official_macro_registry_digest()}",
        ),
    )
    store.connection.execute(
        "INSERT OR IGNORE INTO news_store_manifest VALUES (?,?)",
        ("macro_schema", MACRO_STORE_SCHEMA),
    )
    store.connection.commit()
    accepted = eligible = date_only = duplicates = bridges = failures = 0
    try:
        for record in records:
            try:
                reference, event = normalized_event(record, retrieved_at=retrieved_at)
                existing = store.connection.execute(
                    "SELECT 1 FROM news_events_v1 WHERE event_id=?", (event.event_id,)
                ).fetchone()
                store.add_source_reference(reference)
                store.add_event(event, classify_source_reference(reference))
                if existing:
                    duplicates += 1
                    continue
                accepted += 1
                if not record.intraday_eligible:
                    date_only += 1
                    continue
                eligible += 1
                if research_store_path is None:
                    continue
                anchor = strict_after_five_minute_anchor(record.published_at)
                assert anchor is not None
                snapshot = _snapshot_at(research_store_path, anchor)
                outcome = _outcomes_at(research_store_path, anchor)
                if snapshot is None or outcome is None:
                    continue
                _outcomes, outcome_identity = outcome
                state_digest = hashlib.sha256(_canonical(snapshot).encode()).hexdigest()
                bridge = RDEBridgeRecordV1(
                    event.event_id,
                    record.published_at,
                    anchor,
                    anchor - int(record.published_at.timestamp() * 1000),
                    state_digest,
                    outcome_identity,
                    METHODOLOGY_NAME,
                    METHODOLOGY_VERSION,
                    methodology_digest(),
                )
                store.add_bridge_record(bridge)
                context = historical_event_ai_context(event, snapshot, anchor)
                encoded = _canonical(asdict(context))
                store.connection.execute(
                    "INSERT OR IGNORE INTO historical_event_ai_contexts_v1 VALUES (?,?,?)",
                    (event.event_id, encoded, hashlib.sha256(encoded.encode()).hexdigest()),
                )
                store.connection.commit()
                bridges += 1
            except (ValueError, sqlite3.Error):
                failures += 1
        return IngestionResult(
            len(records), accepted, eligible, date_only, duplicates, failures, bridges
        )
    finally:
        store.close()
