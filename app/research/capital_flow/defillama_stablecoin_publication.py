"""Deterministic create-only publication for parsed DefiLlama stablecoin series."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from .defillama_stablecoin_authority import (
    DEFILLAMA_CROSS_CHAIN_SEMANTICS,
    DEFILLAMA_FILTER_BEHAVIOR_IDENTITY,
    DEFILLAMA_FILTER_CONFIRMED,
    DEFILLAMA_FILTER_DOCUMENTATION_STATUS,
    DEFILLAMA_FLAT_ENDPOINT,
    DEFILLAMA_PROVIDER_IDENTITY,
    DEFILLAMA_STABLECOIN_ASSETS,
    DEFILLAMA_STABLECOIN_AUTHORITY_IDENTITY,
    DEFILLAMA_STABLECOIN_BLIND_START,
    DEFILLAMA_STABLECOIN_END,
    DEFILLAMA_STABLECOIN_STALENESS,
    DEFILLAMA_STABLECOIN_START,
    DefiLlamaStablecoinBoundedResultV1,
    DefiLlamaStablecoinRowV1,
)

DEFILLAMA_PUBLICATION_ROOT = Path("data/research/source/capital_flow/defillama_stablecoin_free_v1")
DEFILLAMA_USABLE_AT_POLICY = "REFERENCE_UTC_DAY_PLUS_FIXED_24_HOURS"
DEFILLAMA_REVISION_MODEL = "CAPTURED_PROVIDER_SNAPSHOT"
DEFILLAMA_TIMESTAMP_SEMANTICS = "UTC_EPOCH_SECONDS_DAILY_REFERENCE"
DEFILLAMA_MANIFEST_NAME = "manifest.json"


@dataclass(frozen=True, slots=True)
class DefiLlamaPublishedAuthorityV1:
    root: Path
    bounded_digests: tuple[str, ...]
    canonical_digests: tuple[str, ...]
    semantic_authority_digest: str
    manifest_semantic_digest: str


def usable_at_utc(reference_timestamp_utc: datetime) -> datetime:
    if reference_timestamp_utc.tzinfo is None:
        raise ValueError("DefiLlama reference timestamp must be UTC-aware")
    return reference_timestamp_utc.astimezone(UTC) + timedelta(days=1)


def _json_bytes(value: object, *, pretty: bool = False) -> bytes:
    if pretty:
        return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("DefiLlama timestamp must be UTC-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _quality(result: DefiLlamaStablecoinBoundedResultV1) -> dict[str, object]:
    item = result.quality
    return {
        "conflicting_duplicate_count": item.conflicting_duplicate_count,
        "cutoff_crossing_timestamp_utc": _utc(item.cutoff_crossing_timestamp_utc)
        if item.cutoff_crossing_timestamp_utc
        else None,
        "first_retained_timestamp_utc": _utc(item.first_retained_timestamp_utc)
        if item.first_retained_timestamp_utc
        else None,
        "identical_duplicate_count": item.identical_duplicate_count,
        "last_retained_timestamp_utc": _utc(item.last_retained_timestamp_utc)
        if item.last_retained_timestamp_utc
        else None,
        "malformed_count": item.malformed_count,
        "missingness_evidence": item.missingness_evidence,
        "objects_seen_after_cutoff": item.objects_seen_after_cutoff,
        "retained_row_count": item.retained_row_count,
        "source_row_count": item.source_row_count,
    }


def _validate_result(result: DefiLlamaStablecoinBoundedResultV1, expected: tuple[int, str]) -> None:
    asset_id, symbol = expected
    quality = result.quality
    if (
        result.authority_identity != DEFILLAMA_STABLECOIN_AUTHORITY_IDENTITY
        or result.provider_identity != DEFILLAMA_PROVIDER_IDENTITY
        or result.filter_behavior_identity != DEFILLAMA_FILTER_BEHAVIOR_IDENTITY
        or result.documentation_status != DEFILLAMA_FILTER_DOCUMENTATION_STATUS
        or result.asset_id != asset_id
        or result.symbol != symbol
        or result.authorized_start != DEFILLAMA_STABLECOIN_START
        or result.authorized_end != DEFILLAMA_STABLECOIN_END
        or result.filter_evidence.verification_state != DEFILLAMA_FILTER_CONFIRMED
        or result.filter_evidence.asset_id != asset_id
        or result.filter_evidence.symbol != symbol
        or result.filter_evidence.endpoint != DEFILLAMA_FLAT_ENDPOINT
        or result.filter_evidence.query_parameter != ("stablecoin", str(asset_id))
        or result.filter_evidence.provider_identity != DEFILLAMA_PROVIDER_IDENTITY
        or result.filter_evidence.documentation_status != DEFILLAMA_FILTER_DOCUMENTATION_STATUS
    ):
        raise ValueError("DefiLlama publication contract invalid")
    if (
        quality.conflicting_duplicate_count
        or quality.malformed_count
        or quality.objects_seen_after_cutoff != 1
        or quality.cutoff_crossing_timestamp_utc is None
        or quality.cutoff_crossing_timestamp_utc.date() <= DEFILLAMA_STABLECOIN_END
        or quality.cutoff_crossing_timestamp_utc.date() >= DEFILLAMA_STABLECOIN_BLIND_START
    ):
        raise ValueError("DefiLlama publication quality gate failed")
    dates = tuple(row.reference_timestamp_utc for row in result.rows)
    for row in result.rows:
        if row.reference_timestamp_utc.tzinfo is None:
            raise ValueError("DefiLlama reference timestamp must be UTC-aware")
        if row.reference_timestamp_utc.astimezone(UTC).time() != datetime.min.time():
            raise ValueError("DefiLlama reference timestamp is not a UTC daily boundary")
        if not row.market_cap_usd.is_finite() or row.market_cap_usd < 0:
            raise ValueError("DefiLlama canonical Decimal invalid")
    if dates != tuple(sorted(dates)) or len(dates) != len(set(dates)):
        raise ValueError("DefiLlama canonical ordering invalid")
    if any(
        item.date() < DEFILLAMA_STABLECOIN_START or item.date() > DEFILLAMA_STABLECOIN_END
        for item in dates
    ):
        raise ValueError("DefiLlama canonical row outside bounds")
    if quality.retained_row_count != len(result.rows):
        raise ValueError("DefiLlama retained count invalid")
    if quality.first_retained_timestamp_utc != (
        dates[0] if dates else None
    ) or quality.last_retained_timestamp_utc != (dates[-1] if dates else None):
        raise ValueError("DefiLlama retained timestamp evidence invalid")


def _source_bytes(result: DefiLlamaStablecoinBoundedResultV1) -> bytes:
    records: list[dict[str, object]] = [
        {
            "kind": "header",
            "provider": "DEFILLAMA",
            "endpoint": DEFILLAMA_FLAT_ENDPOINT,
            "asset_id": result.asset_id,
            "symbol": result.symbol,
            "filter_behavior_identity": DEFILLAMA_FILTER_BEHAVIOR_IDENTITY,
            "documentation_status": DEFILLAMA_FILTER_DOCUMENTATION_STATUS,
            "filter_evidence": {
                "query": list(result.filter_evidence.query_parameter),
                "fingerprint": result.filter_evidence.response_fingerprint,
                "state": result.filter_evidence.verification_state,
            },
            "authorized_start": DEFILLAMA_STABLECOIN_START.isoformat(),
            "authorized_end": DEFILLAMA_STABLECOIN_END.isoformat(),
        }
    ]
    records.extend(
        {
            "kind": "row",
            "reference_timestamp_utc": _utc(row.reference_timestamp_utc),
            "market_cap_usd": str(row.market_cap_usd),
        }
        for row in result.rows
    )
    records.append({"kind": "quality", **_quality(result)})
    return b"".join(_json_bytes(item) for item in records)


def _canonical_bytes(result: DefiLlamaStablecoinBoundedResultV1) -> bytes:
    return b"".join(
        _json_bytes(
            {
                "reference_timestamp_utc": _utc(row.reference_timestamp_utc),
                "market_cap_usd": str(row.market_cap_usd),
                "usable_at_utc": _utc(usable_at_utc(row.reference_timestamp_utc)),
            }
        )
        for row in result.rows
    )


def _temporary(root: Path, prefix: str, payload: bytes) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=prefix, suffix=".pending", dir=root)
    path = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        _cleanup(path)
        raise
    return path


def _cleanup(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _read_rows(path: Path) -> tuple[DefiLlamaStablecoinRowV1, ...]:
    rows: list[DefiLlamaStablecoinRowV1] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        item = json.loads(line)
        timestamp = datetime.fromisoformat(item["reference_timestamp_utc"]).astimezone(UTC)
        value = Decimal(item["market_cap_usd"])
        if not value.is_finite() or value < 0:
            raise ValueError("DefiLlama canonical Decimal invalid")
        if item.get("usable_at_utc") != _utc(usable_at_utc(timestamp)):
            raise ValueError("DefiLlama canonical usable_at invalid")
        rows.append(DefiLlamaStablecoinRowV1(timestamp, value))
    timestamps = tuple(row.reference_timestamp_utc for row in rows)
    if timestamps != tuple(sorted(timestamps)) or len(timestamps) != len(set(timestamps)):
        raise ValueError("DefiLlama canonical reload ordering invalid")
    return tuple(rows)


def _read_source(
    path: Path,
) -> tuple[dict[str, object], tuple[DefiLlamaStablecoinRowV1, ...], dict[str, object]]:
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    if (
        len(records) < 2
        or records[0].get("kind") != "header"
        or records[-1].get("kind") != "quality"
    ):
        raise ValueError("DefiLlama bounded source framing invalid")
    rows: list[DefiLlamaStablecoinRowV1] = []
    for item in records[1:-1]:
        if item.get("kind") != "row":
            continue
        value = Decimal(item["market_cap_usd"])
        if not value.is_finite() or value < 0:
            raise ValueError("DefiLlama bounded source Decimal invalid")
        rows.append(
            DefiLlamaStablecoinRowV1(
                datetime.fromisoformat(item["reference_timestamp_utc"]).astimezone(UTC), value
            )
        )
    rows_tuple = tuple(rows)
    if len(rows_tuple) != len(records) - 2:
        raise ValueError("DefiLlama bounded source row framing invalid")
    return (
        records[0],
        rows_tuple,
        {key: value for key, value in records[-1].items() if key != "kind"},
    )


def _manifest_semantic(
    results: tuple[DefiLlamaStablecoinBoundedResultV1, ...],
    bounded: tuple[str, ...],
    canonical: tuple[str, ...],
) -> dict[str, object]:
    return {
        "authority_identity": DEFILLAMA_STABLECOIN_AUTHORITY_IDENTITY,
        "provider": "DEFILLAMA",
        "assets": [
            {"asset_id": item[0], "symbol": item[1]} for item in DEFILLAMA_STABLECOIN_ASSETS
        ],
        "endpoint": DEFILLAMA_FLAT_ENDPOINT,
        "filter_behavior_identity": DEFILLAMA_FILTER_BEHAVIOR_IDENTITY,
        "documentation_status": DEFILLAMA_FILTER_DOCUMENTATION_STATUS,
        "aggregate_semantics": DEFILLAMA_CROSS_CHAIN_SEMANTICS,
        "authorized_start": DEFILLAMA_STABLECOIN_START.isoformat(),
        "authorized_end": DEFILLAMA_STABLECOIN_END.isoformat(),
        "timestamp_semantics": DEFILLAMA_TIMESTAMP_SEMANTICS,
        "usable_at_policy": DEFILLAMA_USABLE_AT_POLICY,
        "staleness_policy": DEFILLAMA_STABLECOIN_STALENESS,
        "revision_model": DEFILLAMA_REVISION_MODEL,
        "series": [
            {
                "asset_id": item.asset_id,
                "symbol": item.symbol,
                "filter_state": item.filter_evidence.verification_state,
                "filter_query": list(item.filter_evidence.query_parameter),
                "filter_response_fingerprint": item.filter_evidence.response_fingerprint,
                "quality": _quality(item),
                "bounded_source_digest": bounded[index],
                "canonical_digest": canonical[index],
            }
            for index, item in enumerate(results)
        ],
        "blind_exclusion": {
            "blind_start": DEFILLAMA_STABLECOIN_BLIND_START.isoformat(),
            "blind_rows_observed": 0,
        },
    }


def publish_defillama_stablecoin_authority(
    results: Iterable[DefiLlamaStablecoinBoundedResultV1],
    root: Path = DEFILLAMA_PUBLICATION_ROOT,
    *,
    phase_hook: Callable[[str], None] | None = None,
) -> DefiLlamaPublishedAuthorityV1:
    ordered = tuple(results)
    if len(ordered) != 3:
        raise ValueError("DefiLlama publication requires exactly three series")
    for result, expected in zip(ordered, DEFILLAMA_STABLECOIN_ASSETS, strict=True):
        _validate_result(result, expected)
    root.mkdir(parents=True, exist_ok=True)
    finals = tuple(
        root / f"{kind}_{symbol.lower()}.ndjson"
        for kind in ("bounded_source", "canonical")
        for _, symbol in DEFILLAMA_STABLECOIN_ASSETS
    )
    source_paths, canonical_paths = finals[:3], finals[3:]
    manifest_path = root / DEFILLAMA_MANIFEST_NAME
    if any(path.exists() for path in (*finals, manifest_path)):
        raise FileExistsError("DefiLlama authority artifact already exists")
    source = tuple(_source_bytes(item) for item in ordered)
    canonical = tuple(_canonical_bytes(item) for item in ordered)
    source_digests, canonical_digests = (
        tuple(_digest(item) for item in source),
        tuple(_digest(item) for item in canonical),
    )
    semantic = _manifest_semantic(ordered, source_digests, canonical_digests)
    authority_digest = _digest(_json_bytes(semantic))
    manifest: dict[str, object] = {**semantic, "semantic_authority_digest": authority_digest}
    manifest["manifest_semantic_digest"] = _digest(_json_bytes(manifest))
    notify = phase_hook or (lambda _: None)
    temporaries: list[Path] = []
    published: list[Path] = []
    try:
        for index, payload in enumerate(source):
            temp = _temporary(root, ".bounded.", payload)
            temporaries.append(temp)
            if _digest(temp.read_bytes()) != source_digests[index]:
                raise ValueError("bounded source reload digest mismatch")
        notify("bounded_validated")
        for index, payload in enumerate(canonical):
            temp = _temporary(root, ".canonical.", payload)
            temporaries.append(temp)
            if (
                _read_rows(temp) != ordered[index].rows
                or _digest(temp.read_bytes()) != canonical_digests[index]
            ):
                raise ValueError("canonical reload mismatch")
        notify("canonical_validated")
        manifest_temp = _temporary(root, ".manifest.", _json_bytes(manifest, pretty=True))
        temporaries.append(manifest_temp)
        for temp, final in zip(temporaries[:3], source_paths, strict=True):
            os.link(temp, final)
            published.append(final)
        notify("bounded_published")
        for temp, final in zip(temporaries[3:6], canonical_paths, strict=True):
            os.link(temp, final)
            published.append(final)
        notify("canonical_published")
        os.link(manifest_temp, manifest_path)
        published.append(manifest_path)
        notify("manifest_published")
    except BaseException:
        for path in reversed(published):
            _cleanup(path)
        raise
    finally:
        for path in temporaries:
            _cleanup(path)
    return DefiLlamaPublishedAuthorityV1(
        root,
        source_digests,
        canonical_digests,
        authority_digest,
        str(manifest["manifest_semantic_digest"]),
    )


def verify_defillama_stablecoin_authority(root: Path) -> DefiLlamaPublishedAuthorityV1:
    manifest_path = root / DEFILLAMA_MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    digest = manifest.pop("manifest_semantic_digest")
    if digest != _digest(_json_bytes(manifest)):
        raise ValueError("DefiLlama manifest digest mismatch")
    source_digests = tuple(
        _digest((root / f"bounded_source_{symbol.lower()}.ndjson").read_bytes())
        for _, symbol in DEFILLAMA_STABLECOIN_ASSETS
    )
    canonical_digests = tuple(
        _digest((root / f"canonical_{symbol.lower()}.ndjson").read_bytes())
        for _, symbol in DEFILLAMA_STABLECOIN_ASSETS
    )
    if (
        tuple(item["bounded_source_digest"] for item in manifest["series"]) != source_digests
        or tuple(item["canonical_digest"] for item in manifest["series"]) != canonical_digests
    ):
        raise ValueError("DefiLlama artifact digest mismatch")
    if (
        manifest.get("authority_identity") != DEFILLAMA_STABLECOIN_AUTHORITY_IDENTITY
        or manifest.get("provider") != "DEFILLAMA"
        or manifest.get("assets")
        != [
            {"asset_id": asset_id, "symbol": symbol}
            for asset_id, symbol in DEFILLAMA_STABLECOIN_ASSETS
        ]
        or manifest.get("timestamp_semantics") != DEFILLAMA_TIMESTAMP_SEMANTICS
        or manifest.get("usable_at_policy") != DEFILLAMA_USABLE_AT_POLICY
        or manifest.get("revision_model") != DEFILLAMA_REVISION_MODEL
    ):
        raise ValueError("DefiLlama manifest contract invalid")
    for index, (asset_id, symbol) in enumerate(DEFILLAMA_STABLECOIN_ASSETS):
        source_path = root / f"bounded_source_{symbol.lower()}.ndjson"
        canonical_path = root / f"canonical_{symbol.lower()}.ndjson"
        header, source_rows, quality = _read_source(source_path)
        canonical_rows = _read_rows(canonical_path)
        series = manifest["series"][index]
        if (
            header.get("provider") != "DEFILLAMA"
            or header.get("endpoint") != DEFILLAMA_FLAT_ENDPOINT
            or header.get("asset_id") != asset_id
            or header.get("symbol") != symbol
            or header.get("filter_behavior_identity") != DEFILLAMA_FILTER_BEHAVIOR_IDENTITY
            or header.get("documentation_status") != DEFILLAMA_FILTER_DOCUMENTATION_STATUS
            or header.get("authorized_start") != DEFILLAMA_STABLECOIN_START.isoformat()
            or header.get("authorized_end") != DEFILLAMA_STABLECOIN_END.isoformat()
            or header.get("filter_evidence", {}).get("state") != DEFILLAMA_FILTER_CONFIRMED
            or source_rows != canonical_rows
            or quality != series.get("quality")
            or series.get("asset_id") != asset_id
            or series.get("symbol") != symbol
            or series.get("filter_state") != DEFILLAMA_FILTER_CONFIRMED
            or series.get("filter_query") != header["filter_evidence"]["query"]
            or series.get("filter_response_fingerprint") != header["filter_evidence"]["fingerprint"]
        ):
            raise ValueError("DefiLlama published series contract invalid")
        timestamps = tuple(row.reference_timestamp_utc for row in source_rows)
        cutoff = quality.get("cutoff_crossing_timestamp_utc")
        if (
            any(
                timestamp.date() < DEFILLAMA_STABLECOIN_START
                or timestamp.date() > DEFILLAMA_STABLECOIN_END
                for timestamp in timestamps
            )
            or quality.get("retained_row_count") != len(source_rows)
            or quality.get("conflicting_duplicate_count") != 0
            or quality.get("malformed_count") != 0
            or quality.get("objects_seen_after_cutoff") != 1
            or cutoff is None
            or datetime.fromisoformat(cutoff).date() <= DEFILLAMA_STABLECOIN_END
            or datetime.fromisoformat(cutoff).date() >= DEFILLAMA_STABLECOIN_BLIND_START
        ):
            raise ValueError("DefiLlama published series quality invalid")
    semantic = {key: value for key, value in manifest.items() if key != "semantic_authority_digest"}
    authority_digest = _digest(_json_bytes(semantic))
    if authority_digest != manifest["semantic_authority_digest"]:
        raise ValueError("DefiLlama semantic digest mismatch")
    return DefiLlamaPublishedAuthorityV1(
        root, source_digests, canonical_digests, authority_digest, digest
    )
