"""Pure bounded streaming contracts for DefiLlama flat stablecoin histories."""

from __future__ import annotations

import codecs
import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

DEFILLAMA_STABLECOIN_AUTHORITY_IDENTITY = "chappie_defillama_stablecoin_historical_authority/1.0.0"
DEFILLAMA_PROVIDER_IDENTITY = "DEFILLAMA"
DEFILLAMA_FLAT_ENDPOINT = "https://stablecoins.llama.fi/stablecoincharts/all"
DEFILLAMA_FILTER_BEHAVIOR_IDENTITY = "DEFILLAMA_STABLECOIN_FLAT_FILTER_OBSERVED/1.0.0"
DEFILLAMA_FILTER_DOCUMENTATION_STATUS = "OBSERVED_PROVIDER_FILTER_BEHAVIOR"
DEFILLAMA_FILTER_CONFIRMED = "FILTER_CONFIRMED"
DEFILLAMA_CROSS_CHAIN_SEMANTICS = "PROVIDER_SUPPLIED_CROSS_CHAIN_AGGREGATE"
DEFILLAMA_FLAT_SCHEMA_IDENTITY = (
    "DATE_TOTAL_CIRCULATING_PEGGED_USD_TOTAL_CIRCULATING_USD_PEGGED_USD"
)
DEFILLAMA_STABLECOIN_ASSETS = ((1, "USDT"), (2, "USDC"), (5, "DAI"))
DEFILLAMA_STABLECOIN_START = date(2023, 9, 1)
DEFILLAMA_STABLECOIN_END = date(2026, 5, 31)
DEFILLAMA_STABLECOIN_BLIND_START = date(2026, 9, 1)
DEFILLAMA_STABLECOIN_STALENESS = "MAX_48_HOURS"


@dataclass(frozen=True, slots=True)
class DefiLlamaFilterEvidenceV1:
    asset_id: int
    symbol: str
    endpoint: str
    query_parameter: tuple[str, str]
    response_fingerprint: str
    verification_state: str
    provider_identity: str = DEFILLAMA_PROVIDER_IDENTITY
    schema_identity: str = DEFILLAMA_FLAT_SCHEMA_IDENTITY
    documentation_status: str = DEFILLAMA_FILTER_DOCUMENTATION_STATUS


@dataclass(frozen=True, slots=True)
class DefiLlamaStablecoinRowV1:
    reference_timestamp_utc: datetime
    market_cap_usd: Decimal


@dataclass(frozen=True, slots=True)
class DefiLlamaStablecoinQualityV1:
    source_row_count: int
    retained_row_count: int
    identical_duplicate_count: int
    conflicting_duplicate_count: int
    malformed_count: int
    missingness_evidence: str
    first_retained_timestamp_utc: datetime | None
    last_retained_timestamp_utc: datetime | None
    cutoff_crossing_timestamp_utc: datetime | None
    objects_seen_after_cutoff: int


@dataclass(frozen=True, slots=True)
class DefiLlamaStablecoinBoundedResultV1:
    authority_identity: str
    provider_identity: str
    filter_behavior_identity: str
    documentation_status: str
    asset_id: int
    symbol: str
    authorized_start: date
    authorized_end: date
    rows: tuple[DefiLlamaStablecoinRowV1, ...]
    quality: DefiLlamaStablecoinQualityV1
    filter_evidence: DefiLlamaFilterEvidenceV1


def _asset(asset_id: int, symbol: str) -> None:
    if (asset_id, symbol) not in DEFILLAMA_STABLECOIN_ASSETS:
        raise ValueError("unexpected DefiLlama stablecoin asset binding")


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.isdigit():
        raise ValueError("malformed DefiLlama timestamp")
    result = datetime.fromtimestamp(int(value), UTC)
    if result.time() != datetime.min.time():
        raise ValueError("DefiLlama timestamp is not a UTC daily boundary")
    return result


def _decimal(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise TypeError("malformed DefiLlama USD value")
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError("malformed DefiLlama USD value") from error
    if not result.is_finite() or result < 0:
        raise ValueError("malformed DefiLlama USD value")
    return result


class _ArrayReader:
    """Incremental UTF-8 top-level JSON array reader; never materializes the body."""

    def __init__(self, chunks: Iterable[bytes | str]) -> None:
        self._chunks = iter(chunks)
        self._decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self._json = json.JSONDecoder(parse_float=Decimal, parse_constant=Decimal)
        self._buffer = ""
        self._position = 0
        self._finished = False

    def _pull(self) -> bool:
        if self._finished:
            return False
        try:
            chunk = next(self._chunks)
        except StopIteration:
            self._buffer += self._decoder.decode(b"", final=True)
            self._finished = True
            return False
        self._buffer += chunk if isinstance(chunk, str) else self._decoder.decode(chunk)
        return True

    def _whitespace(self) -> None:
        while True:
            while self._position < len(self._buffer) and self._buffer[self._position].isspace():
                self._position += 1
            if self._position < len(self._buffer) or not self._pull():
                return

    def _character(self) -> str:
        self._whitespace()
        if self._position >= len(self._buffer):
            raise ValueError("truncated DefiLlama JSON stream")
        value = self._buffer[self._position]
        self._position += 1
        return value

    def value(self) -> object:
        while True:
            self._whitespace()
            try:
                result, end = self._json.raw_decode(self._buffer, self._position)
            except json.JSONDecodeError as error:
                if self._pull():
                    continue
                raise ValueError("malformed DefiLlama JSON stream") from error
            self._position = end
            return result

    def discard(self) -> None:
        self._buffer = self._buffer[self._position :]
        self._position = 0


def _record(reader: _ArrayReader) -> Mapping[str, object]:
    if reader._character() != "{":
        raise ValueError("unexpected DefiLlama record schema")
    key = reader.value()
    if key != "date" or reader._character() != ":":
        raise ValueError("unexpected DefiLlama record schema")
    return {"date": reader.value()}


def _complete_record(reader: _ArrayReader, fields: dict[str, object]) -> Mapping[str, object]:
    while True:
        delimiter = reader._character()
        if delimiter == "}":
            return fields
        if delimiter != ",":
            raise ValueError("unexpected DefiLlama record schema")
        key = reader.value()
        if not isinstance(key, str) or reader._character() != ":":
            raise ValueError("unexpected DefiLlama record schema")
        fields[key] = reader.value()


def _market_cap(fields: Mapping[str, object]) -> Decimal:
    if set(fields) != {"date", "totalCirculating", "totalCirculatingUSD"}:
        raise ValueError("unexpected DefiLlama record schema")
    circulating = fields["totalCirculating"]
    usd = fields["totalCirculatingUSD"]
    if not isinstance(circulating, Mapping) or not isinstance(usd, Mapping):
        raise TypeError("unexpected DefiLlama record schema")
    if "peggedUSD" not in circulating or "peggedUSD" not in usd:
        raise ValueError("unexpected DefiLlama USD schema")
    return _decimal(usd["peggedUSD"])


def _validate_evidence(evidence: DefiLlamaFilterEvidenceV1, asset_id: int, symbol: str) -> None:
    if (
        evidence.asset_id != asset_id
        or evidence.symbol != symbol
        or evidence.endpoint != DEFILLAMA_FLAT_ENDPOINT
        or evidence.query_parameter != ("stablecoin", str(asset_id))
        or evidence.verification_state != DEFILLAMA_FILTER_CONFIRMED
        or evidence.provider_identity != DEFILLAMA_PROVIDER_IDENTITY
        or evidence.schema_identity != DEFILLAMA_FLAT_SCHEMA_IDENTITY
        or evidence.documentation_status != DEFILLAMA_FILTER_DOCUMENTATION_STATUS
        or len(evidence.response_fingerprint) != 64
    ):
        raise ValueError("DEFILLAMA_STABLECOIN_FILTER_SEMANTICS_CHANGED")


def filter_evidence_from_prefix(
    asset_id: int,
    symbol: str,
    prefix_records: tuple[Mapping[str, object], ...],
    peer_fingerprints: set[str],
) -> DefiLlamaFilterEvidenceV1:
    """Confirm a requested filter produced a distinct bounded response fingerprint."""
    _asset(asset_id, symbol)
    if not prefix_records:
        raise ValueError("DEFILLAMA_STABLECOIN_FILTER_SEMANTICS_CHANGED")
    payload = json.dumps(prefix_records, sort_keys=True, separators=(",", ":"), default=str)
    fingerprint = hashlib.sha256(payload.encode()).hexdigest()
    if fingerprint in peer_fingerprints:
        raise ValueError("DEFILLAMA_STABLECOIN_FILTER_SEMANTICS_CHANGED")
    return DefiLlamaFilterEvidenceV1(
        asset_id,
        symbol,
        DEFILLAMA_FLAT_ENDPOINT,
        ("stablecoin", str(asset_id)),
        fingerprint,
        DEFILLAMA_FILTER_CONFIRMED,
    )


def parse_bounded_defillama_stablecoin_json(
    chunks: Iterable[bytes | str],
    *,
    asset_id: int,
    symbol: str,
    filter_evidence: DefiLlamaFilterEvidenceV1,
) -> DefiLlamaStablecoinBoundedResultV1:
    _asset(asset_id, symbol)
    _validate_evidence(filter_evidence, asset_id, symbol)
    reader = _ArrayReader(chunks)
    if reader._character() != "[":
        raise ValueError("unexpected DefiLlama JSON top-level type")
    rows: dict[datetime, DefiLlamaStablecoinRowV1] = {}
    previous: datetime | None = None
    source_count = identical = conflicts = malformed = 0
    first_object = True
    cutoff: datetime | None = None
    while True:
        delimiter = reader._character()
        if delimiter == "]":
            break
        if first_object:
            if delimiter != "{":
                raise ValueError("malformed DefiLlama JSON array")
            reader._position -= 1
            first_object = False
        elif delimiter != ",":
            raise ValueError("malformed DefiLlama JSON array")
        try:
            partial = dict(_record(reader))
            timestamp = _timestamp(partial.get("date"))
        except (TypeError, ValueError):
            malformed += 1
            raise
        if previous is not None and timestamp < previous:
            raise ValueError("DEFILLAMA_STABLECOIN_SOURCE_ORDER_INVALID")
        previous = timestamp
        if timestamp.date() > DEFILLAMA_STABLECOIN_END:
            cutoff = timestamp
            break
        source_count += 1
        try:
            fields = _complete_record(reader, partial)
            market_cap = _market_cap(fields)
        except (TypeError, ValueError):
            malformed += 1
            raise
        if timestamp.date() < DEFILLAMA_STABLECOIN_START:
            reader.discard()
            continue
        row = DefiLlamaStablecoinRowV1(timestamp, market_cap)
        existing = rows.get(timestamp)
        if existing is not None:
            if existing == row:
                identical += 1
                reader.discard()
                continue
            conflicts += 1
            raise ValueError("conflicting DefiLlama stablecoin duplicate")
        rows[timestamp] = row
        reader.discard()
    ordered = tuple(rows[item] for item in sorted(rows))
    quality = DefiLlamaStablecoinQualityV1(
        source_count,
        len(ordered),
        identical,
        conflicts,
        malformed,
        "VALID_SOURCE_ABSENCE_ONLY_NO_INTERPOLATION_OR_FORWARD_FILL",
        ordered[0].reference_timestamp_utc if ordered else None,
        ordered[-1].reference_timestamp_utc if ordered else None,
        cutoff,
        1 if cutoff else 0,
    )
    return DefiLlamaStablecoinBoundedResultV1(
        DEFILLAMA_STABLECOIN_AUTHORITY_IDENTITY,
        DEFILLAMA_PROVIDER_IDENTITY,
        DEFILLAMA_FILTER_BEHAVIOR_IDENTITY,
        DEFILLAMA_FILTER_DOCUMENTATION_STATUS,
        asset_id,
        symbol,
        DEFILLAMA_STABLECOIN_START,
        DEFILLAMA_STABLECOIN_END,
        ordered,
        quality,
        filter_evidence,
    )
