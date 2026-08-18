"""Research-only News/Event Intelligence v1 contracts.

This module deliberately contains no network client, source scraper, AI client,
trading dependency, or runtime-persistence integration.  It defines immutable
provenance and optional interpretation records for a future offline research
pipeline.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final

from app.market_intelligence_universe import (
    ASSETS,
    UNIVERSE_NAME,
    UNIVERSE_VERSION,
    universe_digest,
)

TAXONOMY_NAME: Final = "news_event_taxonomy"
TAXONOMY_VERSION: Final = "1.0.0"
SIDECAR_SCHEMA_VERSION: Final = "chappie-news-events/v1"
DEFAULT_NEWS_EVENT_STORE_PATH: Final = Path("data/research/compact/news_events_v1.sqlite3")
FIVE_MINUTES_MS: Final = 300_000
ASSET_GROUPS: Final = ("ALL_CRYPTO", "HIGH_BETA", "EXCHANGE_SPECIFIC", "MACRO_GLOBAL")


class EventFamily(StrEnum):
    MACRO = "MACRO"
    REGULATORY = "REGULATORY"
    EXCHANGE = "EXCHANGE"
    CRYPTO_PROTOCOL = "CRYPTO_PROTOCOL"
    ETF_INSTITUTIONAL = "ETF_INSTITUTIONAL"
    MARKET_STRUCTURE = "MARKET_STRUCTURE"
    SOCIAL_NARRATIVE = "SOCIAL_NARRATIVE"


class SourceQuality(StrEnum):
    PRIMARY_OFFICIAL = "PRIMARY_OFFICIAL"
    HIGH_QUALITY_NEWS = "HIGH_QUALITY_NEWS"
    SECONDARY_AGGREGATOR = "SECONDARY_AGGREGATOR"
    SOCIAL_VERIFIED = "SOCIAL_VERIFIED"
    SOCIAL_UNVERIFIED = "SOCIAL_UNVERIFIED"


class TimestampPrecision(StrEnum):
    MILLISECOND = "MILLISECOND"
    SECOND = "SECOND"
    DATE = "DATE"
    UNKNOWN = "UNKNOWN"


class FirstSeenBasis(StrEnum):
    SOURCE_DECLARED = "SOURCE_DECLARED"
    PRESERVED_ARCHIVE = "PRESERVED_ARCHIVE"
    HISTORICAL_BACKFILL_UNKNOWN = "HISTORICAL_BACKFILL_UNKNOWN"


class MarketKnownBasis(StrEnum):
    SOURCE_PUBLISHED_PRECISE = "SOURCE_PUBLISHED_PRECISE"
    PRIMARY_PUBLICATION_PRECISE = "PRIMARY_PUBLICATION_PRECISE"
    UNAVAILABLE_DATE_ONLY = "UNAVAILABLE_DATE_ONLY"
    UNAVAILABLE_UNEVIDENCED = "UNAVAILABLE_UNEVIDENCED"


class HypothesisValidationStatus(StrEnum):
    UNVALIDATED = "UNVALIDATED"
    SUPPORTED = "SUPPORTED"
    WEAK = "WEAK"
    NOT_SUPPORTED = "NOT_SUPPORTED"
    CONTRADICTED = "CONTRADICTED"


TAXONOMY: Final = {
    EventFamily.MACRO: (
        "FOMC_DECISION",
        "CPI_RELEASE",
        "EMPLOYMENT_RELEASE",
        "LIQUIDITY_CONDITIONS",
    ),
    EventFamily.REGULATORY: (
        "REGULATORY_AGENCY_ACTION",
        "COURT_DECISION",
        "LEGISLATION",
        "ETF_APPROVAL",
        "ETF_REJECTION",
        "ETF_DELAY",
    ),
    EventFamily.EXCHANGE: (
        "EXCHANGE_OUTAGE",
        "WITHDRAWAL_SUSPENSION",
        "DEPOSIT_SUSPENSION",
        "EXCHANGE_HACK",
        "LISTING",
        "DELISTING",
    ),
    EventFamily.CRYPTO_PROTOCOL: (
        "PROTOCOL_EXPLOIT",
        "NETWORK_OUTAGE",
        "PROTOCOL_UPGRADE",
        "GOVERNANCE_INCIDENT",
    ),
    EventFamily.ETF_INSTITUTIONAL: (
        "ETF_FLOW",
        "INSTITUTIONAL_PURCHASE",
        "INSTITUTIONAL_SALE",
        "TREASURY_ADOPTION",
    ),
    EventFamily.MARKET_STRUCTURE: (
        "LIQUIDATION_SHOCK",
        "STABLECOIN_DEPEG",
        "LIQUIDITY_INCIDENT",
    ),
    EventFamily.SOCIAL_NARRATIVE: ("PUBLIC_STATEMENT", "VIRAL_NARRATIVE", "SENTIMENT_SHOCK"),
}


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _timestamp_ms(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return int(value.astimezone(UTC).timestamp() * 1000)


def taxonomy_payload() -> dict[str, object]:
    return {
        "name": TAXONOMY_NAME,
        "version": TAXONOMY_VERSION,
        "asset_universe": {
            "name": UNIVERSE_NAME,
            "version": UNIVERSE_VERSION,
            "digest": universe_digest(),
            "assets": list(ASSETS),
        },
        "families": {family.value: list(subtypes) for family, subtypes in TAXONOMY.items()},
        "descriptive_only": True,
    }


def taxonomy_digest(payload: dict[str, object] | None = None) -> str:
    return _digest(payload or taxonomy_payload())


def _metadata_pairs(
    metadata: dict[str, str] | tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    values = tuple(sorted(metadata.items())) if isinstance(metadata, dict) else tuple(metadata)
    if values != tuple(sorted(values)) or len({key for key, _ in values}) != len(values):
        raise ValueError("retrieval metadata must be sorted, unique key/value pairs")
    return values


@dataclass(frozen=True, slots=True)
class SourceReferenceV1:
    source_record_id: str
    source_name: str
    source_quality: SourceQuality
    canonical_reference: str
    published_at: datetime | None
    updated_at: datetime | None
    source_timestamp: datetime | None
    timestamp_precision: TimestampPrecision
    source_timezone: str | None
    title: str
    excerpt: str | None
    raw_content_sha256: str
    retrieved_at: datetime
    retrieval_metadata: tuple[tuple[str, str], ...] = ()
    source_reference_id: str = ""

    def __post_init__(self) -> None:
        for field in ("source_record_id", "source_name", "canonical_reference", "title"):
            if not getattr(self, field):
                raise ValueError(f"{field} is required")
        if len(self.raw_content_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in self.raw_content_sha256
        ):
            raise ValueError("raw_content_sha256 must be lowercase SHA-256")
        for value in (self.published_at, self.updated_at, self.source_timestamp, self.retrieved_at):
            _timestamp_ms(value)
        if (
            self.timestamp_precision in {TimestampPrecision.DATE, TimestampPrecision.UNKNOWN}
            and self.source_timestamp is not None
            and self.source_timezone is not None
        ):
            raise ValueError("date-only/unknown source timestamps cannot claim a precise timezone")
        object.__setattr__(self, "retrieval_metadata", _metadata_pairs(self.retrieval_metadata))
        expected = _digest(
            {
                "source_name": self.source_name,
                "source_record_id": self.source_record_id,
                "canonical_reference": self.canonical_reference,
                "published_at_ms": _timestamp_ms(self.published_at),
                "raw_content_sha256": self.raw_content_sha256,
            }
        )
        if self.source_reference_id and self.source_reference_id != expected:
            raise ValueError("source_reference_id does not match factual identity")
        object.__setattr__(self, "source_reference_id", expected)


@dataclass(frozen=True, slots=True)
class EventInterpretationV1:
    event_type: EventFamily
    event_subtype: str
    entities: tuple[str, ...]
    affected_assets: tuple[str, ...]
    affected_groups: tuple[str, ...]
    severity: str | None
    novelty: str | None
    language: str | None
    confidence: float
    dedup_group: str
    market_known_at: datetime | None
    market_known_basis: MarketKnownBasis
    classifier_version: str

    def __post_init__(self) -> None:
        if self.event_subtype not in TAXONOMY[self.event_type]:
            raise ValueError("event subtype is not in the declared family")
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between zero and one")
        if tuple(sorted(set(self.entities))) != self.entities:
            raise ValueError("entities must be sorted and unique")
        if tuple(sorted(set(self.affected_assets))) != self.affected_assets or set(
            self.affected_assets
        ) - set(ASSETS):
            raise ValueError("affected_assets must be sorted, unique Universe v1 assets")
        if tuple(sorted(set(self.affected_groups))) != self.affected_groups or set(
            self.affected_groups
        ) - set(ASSET_GROUPS):
            raise ValueError("affected_groups must be sorted, unique known groups")
        if self.market_known_at is None and self.market_known_basis not in {
            MarketKnownBasis.UNAVAILABLE_DATE_ONLY,
            MarketKnownBasis.UNAVAILABLE_UNEVIDENCED,
        }:
            raise ValueError("unavailable market time requires an unavailable basis")
        if self.market_known_at is not None and self.market_known_basis in {
            MarketKnownBasis.UNAVAILABLE_DATE_ONLY,
            MarketKnownBasis.UNAVAILABLE_UNEVIDENCED,
        }:
            raise ValueError("available market time requires evidenced basis")
        _timestamp_ms(self.market_known_at)


def factual_event_id(
    *,
    taxonomy_version: str,
    source_references: tuple[SourceReferenceV1, ...],
    first_seen_timestamp: datetime | None,
    first_seen_basis: FirstSeenBasis,
) -> str:
    return _digest(
        {
            "taxonomy_version": taxonomy_version,
            "source_reference_ids": sorted(item.source_reference_id for item in source_references),
            "first_seen_timestamp_ms": _timestamp_ms(first_seen_timestamp),
            "first_seen_basis": first_seen_basis.value,
        }
    )


@dataclass(frozen=True, slots=True)
class NewsEventV1:
    source_references: tuple[SourceReferenceV1, ...]
    first_seen_timestamp: datetime | None
    first_seen_basis: FirstSeenBasis
    interpretation: EventInterpretationV1
    taxonomy_version: str = TAXONOMY_VERSION
    event_id: str = ""

    def __post_init__(self) -> None:
        if self.taxonomy_version != TAXONOMY_VERSION:
            raise ValueError("unsupported taxonomy version")
        if not self.source_references:
            raise ValueError("a news event requires at least one source reference")
        identifiers = tuple(item.source_reference_id for item in self.source_references)
        if identifiers != tuple(sorted(identifiers)) or len(set(identifiers)) != len(identifiers):
            raise ValueError("source references must be canonical sorted unique")
        _timestamp_ms(self.first_seen_timestamp)
        if (
            self.first_seen_basis == FirstSeenBasis.HISTORICAL_BACKFILL_UNKNOWN
            and self.first_seen_timestamp is not None
        ):
            raise ValueError("historical backfill cannot invent first-seen timestamp")
        expected = factual_event_id(
            taxonomy_version=self.taxonomy_version,
            source_references=self.source_references,
            first_seen_timestamp=self.first_seen_timestamp,
            first_seen_basis=self.first_seen_basis,
        )
        if self.event_id and self.event_id != expected:
            raise ValueError("event_id does not match immutable factual identity")
        object.__setattr__(self, "event_id", expected)


def market_known_at_for(reference: SourceReferenceV1) -> tuple[datetime | None, MarketKnownBasis]:
    """Return only a source-evidenced precise publication time.

    A schedule, retrieval timestamp, update timestamp, and date-only publication
    are intentionally insufficient for intraday research.
    """
    if reference.timestamp_precision in {TimestampPrecision.DATE, TimestampPrecision.UNKNOWN}:
        return None, MarketKnownBasis.UNAVAILABLE_DATE_ONLY
    if reference.published_at is None:
        return None, MarketKnownBasis.UNAVAILABLE_UNEVIDENCED
    return reference.published_at.astimezone(UTC), MarketKnownBasis.SOURCE_PUBLISHED_PRECISE


def strict_after_five_minute_anchor(market_known_at: datetime | None) -> int | None:
    """Return the first fully closed canonical interval strictly after knowledge time."""
    known_ms = _timestamp_ms(market_known_at)
    return (
        (((known_ms + 1) // FIVE_MINUTES_MS) + 1) * FIVE_MINUTES_MS - 1
        if known_ms is not None
        else None
    )


def authoritative_dedup_key(reference: SourceReferenceV1) -> str | None:
    metadata = dict(reference.retrieval_metadata)
    for key in (
        "release_number",
        "docket_number",
        "incident_id",
        "announcement_id",
        "governance_id",
    ):
        if value := metadata.get(key):
            return f"{key}:{value}"
    return None


def dedup_group_id(event: NewsEventV1) -> str:
    """Use authoritative IDs only; uncertainty intentionally remains separate."""
    keys = sorted(key for item in event.source_references if (key := authoritative_dedup_key(item)))
    identity = keys[0] if keys else f"event:{event.event_id}"
    return _digest({"family": event.interpretation.event_type.value, "identity": identity})


def fallback_dedup_candidate(
    left: NewsEventV1, right: NewsEventV1, *, window_ms: int = 7_200_000
) -> bool:
    """Conservative fallback: identical normalized title tokens, family, entity, and window."""
    if left.interpretation.event_type != right.interpretation.event_type:
        return False
    if not set(left.interpretation.entities) & set(right.interpretation.entities):
        return False
    left_time, right_time = (
        left.interpretation.market_known_at,
        right.interpretation.market_known_at,
    )
    if (
        left_time is None
        or right_time is None
        or abs(_timestamp_ms(left_time) - _timestamp_ms(right_time)) > window_ms
    ):
        return False

    def tokens(event: NewsEventV1) -> set[str]:
        return {
            token
            for item in event.source_references
            for token in item.title.lower().replace("-", " ").split()
            if len(token) > 2
        }

    first, second = tokens(left), tokens(right)
    return bool(first) and first == second


@dataclass(frozen=True, slots=True)
class AIEnrichmentV1:
    event_id: str
    model_provider: str
    model_name: str
    model_version: str
    prompt_contract_version: str
    created_at: datetime
    summary: str | None
    candidate_interpretation: str | None
    secondary_relationships: tuple[str, ...]
    ambiguity_notes: tuple[str, ...]
    candidate_scenarios: tuple[str, ...]
    confidence: float
    input_digest: str
    output_digest: str
    enrichment_id: str = ""

    def __post_init__(self) -> None:
        _timestamp_ms(self.created_at)
        if not self.event_id or not 0 <= self.confidence <= 1:
            raise ValueError("AI enrichment requires event_id and bounded confidence")
        expected = _digest(
            {
                "event_id": self.event_id,
                "provider": self.model_provider,
                "model": self.model_name,
                "version": self.model_version,
                "prompt": self.prompt_contract_version,
                "input": self.input_digest,
                "output": self.output_digest,
            }
        )
        if self.enrichment_id and self.enrichment_id != expected:
            raise ValueError("enrichment_id does not match immutable enrichment identity")
        object.__setattr__(self, "enrichment_id", expected)


@dataclass(frozen=True, slots=True)
class MarketHypothesisV1:
    created_at: datetime
    information_cutoff: datetime
    source_event_ids: tuple[str, ...]
    market_state_digest: str
    claim: str
    target_assets: tuple[str, ...]
    target_horizon: str
    scenario: str
    confidence: float
    model_identity: str
    input_digest: str
    validation_status: HypothesisValidationStatus = HypothesisValidationStatus.UNVALIDATED
    validator_identity: str | None = None
    hypothesis_id: str = ""

    def __post_init__(self) -> None:
        _timestamp_ms(self.created_at)
        _timestamp_ms(self.information_cutoff)
        if self.information_cutoff > self.created_at or not self.claim:
            raise ValueError("hypothesis must have a causal cutoff and claim")
        if tuple(sorted(set(self.source_event_ids))) != self.source_event_ids:
            raise ValueError("source_event_ids must be canonical sorted unique")
        if set(self.target_assets) - set(ASSETS) or not 0 <= self.confidence <= 1:
            raise ValueError("invalid target assets or confidence")
        if (
            self.validation_status != HypothesisValidationStatus.UNVALIDATED
            or self.validator_identity is not None
        ):
            raise ValueError(
                "hypotheses are created unvalidated; deterministic validation evidence is separate"
            )
        expected = _digest(
            {
                "created_at": _timestamp_ms(self.created_at),
                "cutoff": _timestamp_ms(self.information_cutoff),
                "source_event_ids": self.source_event_ids,
                "market_state_digest": self.market_state_digest,
                "claim": self.claim,
                "target_assets": self.target_assets,
                "target_horizon": self.target_horizon,
                "scenario": self.scenario,
                "model_identity": self.model_identity,
                "input_digest": self.input_digest,
            }
        )
        if self.hypothesis_id and self.hypothesis_id != expected:
            raise ValueError("hypothesis_id does not match immutable input identity")
        object.__setattr__(self, "hypothesis_id", expected)


@dataclass(frozen=True, slots=True)
class HypothesisValidationEvidenceV1:
    """Future deterministic validation record; never an AI-enrichment output."""

    hypothesis_id: str
    validation_status: HypothesisValidationStatus
    validator_identity: str
    evidence_digest: str
    validated_at: datetime

    def __post_init__(self) -> None:
        _timestamp_ms(self.validated_at)
        if (
            not self.hypothesis_id
            or not self.validator_identity
            or self.validation_status == HypothesisValidationStatus.UNVALIDATED
        ):
            raise ValueError("validation evidence requires a final deterministic status")


@dataclass(frozen=True, slots=True)
class RDEBridgeRecordV1:
    event_id: str
    market_known_at: datetime
    rde_anchor_timestamp: int
    alignment_lag_ms: int
    market_state_digest: str
    outcome_cache_identity: str
    base_methodology_name: str
    base_methodology_version: str
    base_methodology_digest: str
    bridge_schema: str = "news-event-rde-bridge/v1"

    def __post_init__(self) -> None:
        known = _timestamp_ms(self.market_known_at)
        if self.rde_anchor_timestamp != strict_after_five_minute_anchor(self.market_known_at):
            raise ValueError("RDE anchor must be strictly after market-known timestamp")
        if self.alignment_lag_ms != self.rde_anchor_timestamp - known:
            raise ValueError("alignment lag must be exact")


@dataclass(frozen=True, slots=True)
class ClassifierResultV1:
    event_type: EventFamily | None
    event_subtype: str | None
    matched_rule_ids: tuple[str, ...]
    entities: tuple[str, ...]
    affected_assets: tuple[str, ...]
    affected_groups: tuple[str, ...]
    evidence_completeness: str
    confidence: float
    status: str


_ALIASES: Final = {
    "bitcoin": "BTC",
    "btc": "BTC",
    "ethereum": "ETH",
    "eth": "ETH",
    "solana": "SOL",
    "sol": "SOL",
    "binance coin": "BNB",
    "bnb": "BNB",
    "avalanche": "AVAX",
    "avax": "AVAX",
    "ripple": "XRP",
    "xrp": "XRP",
    "chainlink": "LINK",
    "link": "LINK",
    "dogecoin": "DOGE",
    "doge": "DOGE",
}


def classify_source_reference(reference: SourceReferenceV1) -> ClassifierResultV1:
    """Small, deterministic, non-directional rule layer; unknown is safe."""
    text = f"{reference.title} {reference.excerpt or ''}".lower()
    assets = tuple(sorted({asset for alias, asset in _ALIASES.items() if alias in text}))
    groups: tuple[str, ...] = ()
    family: EventFamily | None = None
    subtype: str | None = None
    rule: str | None = None
    rules = (
        ("FOMC_DECISION", EventFamily.MACRO, "FOMC_DECISION", ("fomc",)),
        ("CPI_RELEASE", EventFamily.MACRO, "CPI_RELEASE", ("consumer price index", " cpi ")),
        (
            "EMPLOYMENT_RELEASE",
            EventFamily.MACRO,
            "EMPLOYMENT_RELEASE",
            ("employment situation", "nonfarm payroll"),
        ),
        ("ETF_APPROVAL", EventFamily.REGULATORY, "ETF_APPROVAL", ("etf", "approv")),
        ("ETF_REJECTION", EventFamily.REGULATORY, "ETF_REJECTION", ("etf", "reject")),
        ("ETF_DELAY", EventFamily.REGULATORY, "ETF_DELAY", ("etf", "delay")),
        (
            "WITHDRAWAL_SUSPENSION",
            EventFamily.EXCHANGE,
            "WITHDRAWAL_SUSPENSION",
            ("withdrawal", "suspend"),
        ),
        ("EXCHANGE_OUTAGE", EventFamily.EXCHANGE, "EXCHANGE_OUTAGE", ("exchange", "outage")),
        ("HACK_OR_EXPLOIT", EventFamily.EXCHANGE, "EXCHANGE_HACK", ("exploit",)),
        ("HACK_OR_EXPLOIT", EventFamily.EXCHANGE, "EXCHANGE_HACK", ("hack",)),
        ("DELISTING", EventFamily.EXCHANGE, "DELISTING", ("delist",)),
        ("LISTING", EventFamily.EXCHANGE, "LISTING", ("listing",)),
        ("NETWORK_OUTAGE", EventFamily.CRYPTO_PROTOCOL, "NETWORK_OUTAGE", ("network", "outage")),
    )
    for candidate_rule, candidate_family, candidate_subtype, words in rules:
        if all(word in text for word in words):
            rule, family, subtype = candidate_rule, candidate_family, candidate_subtype
            break
    if family == EventFamily.MACRO:
        groups = ("ALL_CRYPTO", "MACRO_GLOBAL")
    elif family == EventFamily.EXCHANGE:
        groups = ("EXCHANGE_SPECIFIC",)
    elif family is not None and not assets:
        groups = ("ALL_CRYPTO",)
    return ClassifierResultV1(
        family,
        subtype,
        (rule,) if rule else (),
        assets,
        assets,
        groups,
        "TITLE_AND_EXCERPT" if reference.excerpt else "TITLE_ONLY",
        0.9 if rule and reference.excerpt else 0.7 if rule else 0.0,
        "CLASSIFIED" if rule else "UNCLASSIFIED",
    )


class NewsEventStore:
    """Append-only research-only sidecar; never open this from the trading runtime."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection = sqlite3.connect(path)

    def close(self) -> None:
        self.connection.close()

    def initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS news_store_manifest (name TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS source_references_v1 (source_reference_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL, payload_digest TEXT NOT NULL) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS news_events_v1 (event_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL, payload_digest TEXT NOT NULL) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS dedup_groups_v1 (dedup_group TEXT NOT NULL, event_id TEXT NOT NULL, payload_digest TEXT NOT NULL, PRIMARY KEY(dedup_group, event_id)) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS classifier_evidence_v1 (event_id TEXT NOT NULL, classifier_version TEXT NOT NULL, payload_json TEXT NOT NULL, payload_digest TEXT NOT NULL, PRIMARY KEY(event_id, classifier_version)) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS ai_enrichments_v1 (enrichment_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, payload_json TEXT NOT NULL, payload_digest TEXT NOT NULL) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS rde_bridge_records_v1 (event_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL, payload_digest TEXT NOT NULL) WITHOUT ROWID;
            CREATE TRIGGER IF NOT EXISTS source_references_v1_no_update BEFORE UPDATE ON source_references_v1 BEGIN SELECT RAISE(ABORT, 'append-only'); END;
            CREATE TRIGGER IF NOT EXISTS source_references_v1_no_delete BEFORE DELETE ON source_references_v1 BEGIN SELECT RAISE(ABORT, 'append-only'); END;
            CREATE TRIGGER IF NOT EXISTS news_events_v1_no_update BEFORE UPDATE ON news_events_v1 BEGIN SELECT RAISE(ABORT, 'append-only'); END;
            CREATE TRIGGER IF NOT EXISTS news_events_v1_no_delete BEFORE DELETE ON news_events_v1 BEGIN SELECT RAISE(ABORT, 'append-only'); END;
            CREATE TRIGGER IF NOT EXISTS dedup_groups_v1_no_update BEFORE UPDATE ON dedup_groups_v1 BEGIN SELECT RAISE(ABORT, 'append-only'); END;
            CREATE TRIGGER IF NOT EXISTS dedup_groups_v1_no_delete BEFORE DELETE ON dedup_groups_v1 BEGIN SELECT RAISE(ABORT, 'append-only'); END;
            CREATE TRIGGER IF NOT EXISTS classifier_evidence_v1_no_update BEFORE UPDATE ON classifier_evidence_v1 BEGIN SELECT RAISE(ABORT, 'append-only'); END;
            CREATE TRIGGER IF NOT EXISTS classifier_evidence_v1_no_delete BEFORE DELETE ON classifier_evidence_v1 BEGIN SELECT RAISE(ABORT, 'append-only'); END;
            CREATE TRIGGER IF NOT EXISTS ai_enrichments_v1_no_update BEFORE UPDATE ON ai_enrichments_v1 BEGIN SELECT RAISE(ABORT, 'append-only'); END;
            CREATE TRIGGER IF NOT EXISTS ai_enrichments_v1_no_delete BEFORE DELETE ON ai_enrichments_v1 BEGIN SELECT RAISE(ABORT, 'append-only'); END;
            CREATE TRIGGER IF NOT EXISTS rde_bridge_records_v1_no_update BEFORE UPDATE ON rde_bridge_records_v1 BEGIN SELECT RAISE(ABORT, 'append-only'); END;
            CREATE TRIGGER IF NOT EXISTS rde_bridge_records_v1_no_delete BEFORE DELETE ON rde_bridge_records_v1 BEGIN SELECT RAISE(ABORT, 'append-only'); END;
            """
        )
        self.connection.execute(
            "INSERT OR IGNORE INTO news_store_manifest VALUES (?, ?)",
            ("schema", SIDECAR_SCHEMA_VERSION),
        )
        self.connection.commit()

    def _append(
        self, table: str, identifier: str, payload: object, extra: tuple[object, ...] = ()
    ) -> None:
        encoded = _canonical(
            asdict(payload) if hasattr(payload, "__dataclass_fields__") else payload
        )
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        values = (
            (identifier, encoded, digest) if not extra else (identifier, *extra, encoded, digest)
        )
        existing = self.connection.execute(
            f"SELECT payload_digest FROM {table} WHERE {('source_reference_id' if table == 'source_references_v1' else 'event_id' if table in {'news_events_v1', 'rde_bridge_records_v1'} else 'enrichment_id' if table == 'ai_enrichments_v1' else 'event_id')}=?",
            (identifier,),
        ).fetchone()
        if existing:
            if existing[0] != digest:
                raise ValueError("append-only identity conflict")
            return
        placeholders = ",".join("?" for _ in values)
        self.connection.execute(f"INSERT INTO {table} VALUES ({placeholders})", values)
        self.connection.commit()

    def add_source_reference(self, reference: SourceReferenceV1) -> None:
        self._append("source_references_v1", reference.source_reference_id, reference)

    def add_event(self, event: NewsEventV1, classifier: ClassifierResultV1) -> None:
        self._append("news_events_v1", event.event_id, event)
        group = dedup_group_id(event)
        self.connection.execute(
            "INSERT OR IGNORE INTO dedup_groups_v1 VALUES (?,?,?)",
            (group, event.event_id, _digest({"event_id": event.event_id, "group": group})),
        )
        self.connection.execute(
            "INSERT OR IGNORE INTO classifier_evidence_v1 VALUES (?,?,?,?)",
            (
                event.event_id,
                event.interpretation.classifier_version,
                _canonical(asdict(classifier)),
                _digest(asdict(classifier)),
            ),
        )
        self.connection.commit()

    def add_enrichment(self, enrichment: AIEnrichmentV1) -> None:
        self._append(
            "ai_enrichments_v1", enrichment.enrichment_id, enrichment, (enrichment.event_id,)
        )

    def add_bridge_record(self, record: RDEBridgeRecordV1) -> None:
        self._append("rde_bridge_records_v1", record.event_id, record)
