"""Create-only canonical publication for the validated FRED/ALFRED V2 authority."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .fred_alfred_v2 import (
    FRED_ALFRED_GLOBAL_RISK_AUTHORITY_V2_IDENTITY,
    FRED_SERIES_SEMANTICS_V2,
    FRED_SERIES_V2,
    PROSPECTIVE_BLIND_OOS_RANGE_V2,
    RESEARCH_RANGE_V2,
    SOURCE_RANGE_V2,
    VALIDATION_RANGE_V2,
    VIXCLS_USAGE_RESTRICTIONS_V2,
)
from .fred_alfred_v2_acquisition.contracts import (
    FULL_VINTAGE_SERIES_V2,
    RELEASE_VINTAGE_SERIES_V2,
)
from .fred_alfred_v2_availability import (
    CBOE_VIX_DAILY_CLOSE_SESSION_CALENDAR_IDENTITY,
    CBOE_VIX_MARKET_SOURCE_IDENTITY,
    FRED_CAUSAL_AVAILABILITY_BINDINGS_V2,
    US_FEDERAL_BUSINESS_DAY_CALENDAR_IDENTITY,
)
from .fred_alfred_v2_pit import (
    FRED_ALFRED_V2_PIT_MATERIALIZATION_IDENTITY,
    FRESH,
    KNOWN,
    STALE_SOURCE_RECORD,
    _result,
    _staleness,
    _usable_at,
)
from .fred_alfred_v2_semantic import (
    FRED_ALFRED_V2_SEMANTIC_EVIDENCE_IDENTITY,
    VALID_SOURCE_ABSENCE,
    FredSemanticEvidenceSetV2,
    FredVintageObservationV2,
)
from .nasdaq_calendar import NASDAQ_CALENDAR_AUTHORITY_IDENTITY

FRED_ALFRED_V2_PUBLICATION_ROOT = Path("data/research/source/capital_flow/fred_alfred_v2")
FRED_ALFRED_V2_CANONICAL_DIRECTORY = "canonical"
FRED_ALFRED_V2_MANIFEST_NAME = "manifest.json"
FRED_ALFRED_V2_AUTHORITY_VALIDATED = "FRED_ALFRED_V2_AUTHORITY_VALIDATED"
FRED_ALFRED_V2_NON_AUTHORITATIVE = "NON_AUTHORITATIVE"
FRED_ALFRED_PROVIDER = "FRED_ALFRED"
FRED_ALFRED_BLIND_EXCLUSION = "BLIND_OOS_MARKET_OBSERVATIONS_EXCLUDED"
NASDAQ_SESSION_SEMANTIC = "NASDAQCOM_PREVIOUS_COMPLETED_NASDAQ_REGULAR_SESSION_ONLY"

_BINDINGS = {item.series_id: item for item in FRED_CAUSAL_AVAILABILITY_BINDINGS_V2}
_SEMANTICS = dict(FRED_SERIES_SEMANTICS_V2)


@dataclass(frozen=True, slots=True)
class FredAlfredV2PublishedAuthority:
    root: Path
    canonical_digests: tuple[str, ...]
    semantic_authority_digest: str
    manifest_semantic_digest: str
    status: str = FRED_ALFRED_V2_AUTHORITY_VALIDATED


def _json_bytes(value: object, *, pretty: bool = False) -> bytes:
    if pretty:
        return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("canonical timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _canonical_path(root: Path, series_id: str) -> Path:
    return root / FRED_ALFRED_V2_CANONICAL_DIRECTORY / f"canonical_{series_id.lower()}.ndjson"


def _vintage_identity(observation: FredVintageObservationV2) -> str:
    return _digest(
        json.dumps(
            {
                "reference_date": observation.reference_date.isoformat(),
                "realtime_start": observation.realtime_start.isoformat(),
                "semantic_digest": observation.semantic_digest,
                "series_id": observation.series_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )


def _effective_intervals(
    observations: tuple[FredVintageObservationV2, ...],
) -> dict[str, datetime | None]:
    grouped: defaultdict[date, list[FredVintageObservationV2]] = defaultdict(list)
    for observation in observations:
        grouped[observation.reference_date].append(observation)
    intervals: dict[str, datetime | None] = {}
    for vintages in grouped.values():
        ordered = sorted(vintages, key=lambda item: (item.realtime_start, item.semantic_digest))
        for index, observation in enumerate(ordered):
            intervals[observation.semantic_digest] = (
                _usable_at(ordered[index + 1]) if index + 1 < len(ordered) else None
            )
    return intervals


def _publishable_observations(
    evidence: FredSemanticEvidenceSetV2,
) -> tuple[FredVintageObservationV2, ...]:
    result: list[FredVintageObservationV2] = []
    for observation in evidence.observations:
        try:
            _usable_at(observation)
        except ValueError as error:
            if (
                evidence.series_id in {"NASDAQCOM", "VIXCLS"}
                and str(error) == "INVALID_SOURCE_SESSION"
            ):
                continue
            raise
        result.append(observation)
    return tuple(result)


def _canonical_record(
    evidence: FredSemanticEvidenceSetV2,
    observation: FredVintageObservationV2,
    effective_until: datetime | None,
) -> dict[str, object]:
    usable_at = _usable_at(observation)
    if observation.value_state == VALID_SOURCE_ABSENCE:
        status = VALID_SOURCE_ABSENCE
        staleness_state = None
        staleness_age = None
    else:
        staleness_state, staleness_age = _staleness(observation, usable_at)
        status = KNOWN if staleness_state == FRESH else STALE_SOURCE_RECORD
    pit = _result(
        evidence,
        observation.reference_date,
        status,
        observation,
        usable_at=usable_at,
        effective_until=effective_until,
        staleness_state=staleness_state,
        staleness_age=staleness_age,
    )
    if pit.selected_vintage_identity != _vintage_identity(observation):
        raise ValueError("canonical PIT vintage invariant violated")
    binding = _BINDINGS[observation.series_id]
    return {
        "authority_identity": FRED_ALFRED_GLOBAL_RISK_AUTHORITY_V2_IDENTITY,
        "availability_policy_identity": binding.availability_policy_identity,
        "effective_until_utc": None if effective_until is None else _utc(effective_until),
        "pit_digest": pit.materialization_digest,
        "pit_identity": FRED_ALFRED_V2_PIT_MATERIALIZATION_IDENTITY,
        "realtime_end": (
            None if observation.realtime_end is None else observation.realtime_end.isoformat()
        ),
        "realtime_start": observation.realtime_start.isoformat(),
        "reference_date": observation.reference_date.isoformat(),
        "selected_vintage_identity": pit.selected_vintage_identity,
        "series_id": observation.series_id,
        "source_semantic_digest": observation.semantic_digest,
        "staleness_calendar_identity": binding.staleness_calendar_identity,
        "staleness_policy": binding.staleness_policy,
        "unit": observation.unit,
        "usable_at_utc": _utc(usable_at),
        "value": None if observation.value is None else str(observation.value),
        "value_state": observation.value_state,
    }


def _canonical_bytes(evidence: FredSemanticEvidenceSetV2) -> bytes:
    observations = _publishable_observations(evidence)
    intervals = _effective_intervals(observations)
    return b"".join(
        _json_bytes(_canonical_record(evidence, item, intervals[item.semantic_digest]))
        for item in observations
    )


def _quality(evidence: FredSemanticEvidenceSetV2) -> dict[str, object]:
    observations = _publishable_observations(evidence)
    reference_dates = tuple(item.reference_date for item in observations)
    return {
        "canonical_row_count": len(observations),
        "first_reference_date": (None if not reference_dates else min(reference_dates).isoformat()),
        "last_reference_date": (None if not reference_dates else max(reference_dates).isoformat()),
        "invalid_source_session_excluded_count": evidence.record_count - len(observations),
        "malformed_count": 0,
        "maximum_vintages_per_reference_date": evidence.maximum_vintages_per_reference_date,
        "missing_count": sum(item.value_state == VALID_SOURCE_ABSENCE for item in observations),
        "pit_usable_at_invariant_violations": 0,
        "revision_count": evidence.revision_count,
        "reference_dates_with_revisions": evidence.reference_dates_with_revisions,
        "semantic_record_count": evidence.record_count,
        "vintage_count": evidence.vintage_count,
    }


def _series_manifest(
    evidence: FredSemanticEvidenceSetV2, canonical_digest: str
) -> dict[str, object]:
    binding = _BINDINGS[evidence.series_id]
    unit = evidence.observations[0].unit if evidence.observations else None
    item: dict[str, object] = {
        "availability_calendar_identity": binding.availability_calendar_identity,
        "availability_policy": binding.availability_policy,
        "availability_policy_identity": binding.availability_policy_identity,
        "canonical_digest": canonical_digest,
        "canonical_file": f"canonical_{evidence.series_id.lower()}.ndjson",
        "pit_classification": binding.pit_classification,
        "quality": _quality(evidence),
        "semantic": _SEMANTICS[evidence.series_id],
        "semantic_evidence_digest": evidence.semantic_digest,
        "series_id": evidence.series_id,
        "staleness_calendar_identity": binding.staleness_calendar_identity,
        "staleness_policy": binding.staleness_policy,
        "unit": unit,
    }
    if evidence.series_id == "NASDAQCOM":
        item["source_session_semantic"] = NASDAQ_SESSION_SEMANTIC
    if evidence.series_id == "VIXCLS":
        item.update(
            {
                "source_session_identity": CBOE_VIX_MARKET_SOURCE_IDENTITY,
                "usage_restrictions": list(VIXCLS_USAGE_RESTRICTIONS_V2),
            }
        )
    return item


def _manifest_semantic(
    evidence_sets: tuple[FredSemanticEvidenceSetV2, ...], digests: tuple[str, ...]
) -> dict[str, object]:
    return {
        "authority_identity": FRED_ALFRED_GLOBAL_RISK_AUTHORITY_V2_IDENTITY,
        "blind_exclusion": {
            "declaration": FRED_ALFRED_BLIND_EXCLUSION,
            "range": list(PROSPECTIVE_BLIND_OOS_RANGE_V2),
            "rows_published": 0,
        },
        "calendar_authorities": [
            NASDAQ_CALENDAR_AUTHORITY_IDENTITY,
            CBOE_VIX_DAILY_CLOSE_SESSION_CALENDAR_IDENTITY,
            US_FEDERAL_BUSINESS_DAY_CALENDAR_IDENTITY,
        ],
        "canonical_directory": FRED_ALFRED_V2_CANONICAL_DIRECTORY,
        "pit_full_vintage_series": list(FULL_VINTAGE_SERIES_V2),
        "pit_identity": FRED_ALFRED_V2_PIT_MATERIALIZATION_IDENTITY,
        "pit_release_vintage_series": list(RELEASE_VINTAGE_SERIES_V2),
        "provider": FRED_ALFRED_PROVIDER,
        "ranges": {
            "source": list(SOURCE_RANGE_V2),
            "research": list(RESEARCH_RANGE_V2),
            "validation": list(VALIDATION_RANGE_V2),
            "prospective_blind_oos": list(PROSPECTIVE_BLIND_OOS_RANGE_V2),
        },
        "semantic_evidence_identity": FRED_ALFRED_V2_SEMANTIC_EVIDENCE_IDENTITY,
        "series": [
            _series_manifest(evidence, digests[index])
            for index, evidence in enumerate(evidence_sets)
        ],
        "series_order": list(FRED_SERIES_V2),
    }


def _temporary(directory: Path, prefix: str, payload: bytes) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=prefix, suffix=".pending", dir=directory)
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


def _read_canonical(path: Path, series_id: str) -> tuple[dict[str, object], ...]:
    records: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        item = json.loads(line)
        if not isinstance(item, dict):
            raise TypeError("canonical record must be an object")
        expected_keys = {
            "authority_identity",
            "availability_policy_identity",
            "effective_until_utc",
            "pit_digest",
            "pit_identity",
            "realtime_end",
            "realtime_start",
            "reference_date",
            "selected_vintage_identity",
            "series_id",
            "source_semantic_digest",
            "staleness_calendar_identity",
            "staleness_policy",
            "unit",
            "usable_at_utc",
            "value",
            "value_state",
        }
        if set(item) != expected_keys or item["series_id"] != series_id:
            raise ValueError("canonical record shape invalid")
        try:
            date.fromisoformat(str(item["reference_date"]))
            date.fromisoformat(str(item["realtime_start"]))
            if item["realtime_end"] is not None:
                date.fromisoformat(str(item["realtime_end"]))
            usable = datetime.fromisoformat(str(item["usable_at_utc"]))
            if usable.tzinfo is None or not str(item["usable_at_utc"]).endswith("Z"):
                raise ValueError
            if item["effective_until_utc"] is not None:
                effective = datetime.fromisoformat(str(item["effective_until_utc"]))
                if effective.tzinfo is None or not str(item["effective_until_utc"]).endswith("Z"):
                    raise ValueError
            if item["value"] is not None:
                value = Decimal(str(item["value"]))
                if not value.is_finite():
                    raise ValueError
            elif item["value_state"] != VALID_SOURCE_ABSENCE:
                raise ValueError
        except (InvalidOperation, TypeError, ValueError) as error:
            raise ValueError("canonical temporal/Decimal value invalid") from error
        records.append(item)
    keys = tuple(
        (item["reference_date"], item["realtime_start"], item["source_semantic_digest"])
        for item in records
    )
    if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
        raise ValueError("canonical ordering or uniqueness invalid")
    return tuple(records)


def _validate_evidence_sets(
    evidence_sets: tuple[FredSemanticEvidenceSetV2, ...],
) -> None:
    if tuple(item.series_id for item in evidence_sets) != FRED_SERIES_V2:
        raise ValueError("publication requires exact frozen seven-series order")
    source_start, source_end = (date.fromisoformat(item) for item in SOURCE_RANGE_V2)
    for evidence in evidence_sets:
        if (
            evidence.schema_identity != FRED_ALFRED_V2_SEMANTIC_EVIDENCE_IDENTITY
            or any(
                item.authority_identity != FRED_ALFRED_GLOBAL_RISK_AUTHORITY_V2_IDENTITY
                for item in evidence.observations
            )
            or any(
                not source_start <= item.reference_date <= source_end
                for item in evidence.observations
            )
        ):
            raise ValueError("semantic evidence outside final authority contract")


def publish_fred_alfred_v2_authority(
    evidence_sets: Iterable[FredSemanticEvidenceSetV2],
    root: Path = FRED_ALFRED_V2_PUBLICATION_ROOT,
    *,
    phase_hook: Callable[[str], None] | None = None,
) -> FredAlfredV2PublishedAuthority:
    """Publish a complete authority create-only; the manifest is linked last."""
    ordered = tuple(evidence_sets)
    _validate_evidence_sets(ordered)
    canonical_dir = root / FRED_ALFRED_V2_CANONICAL_DIRECTORY
    manifest_path = root / FRED_ALFRED_V2_MANIFEST_NAME
    if canonical_dir.exists() or manifest_path.exists():
        raise FileExistsError("FRED/ALFRED V2 final authority already exists")
    payloads = tuple(_canonical_bytes(item) for item in ordered)
    digests = tuple(_digest(item) for item in payloads)
    semantic = _manifest_semantic(ordered, digests)
    authority_digest = _digest(_json_bytes(semantic))
    manifest: dict[str, object] = {
        **semantic,
        "semantic_authority_digest": authority_digest,
        "status": FRED_ALFRED_V2_AUTHORITY_VALIDATED,
    }
    manifest["manifest_semantic_digest"] = _digest(_json_bytes(manifest))
    root.mkdir(parents=True, exist_ok=True)
    canonical_dir.mkdir()
    notify = phase_hook or (lambda _: None)
    temporaries: list[Path] = []
    published: list[Path] = []
    manifest_temp: Path | None = None
    try:
        for series_id, payload, digest in zip(FRED_SERIES_V2, payloads, digests, strict=True):
            temporary = _temporary(canonical_dir, f".{series_id.lower()}.", payload)
            temporaries.append(temporary)
            if _digest(temporary.read_bytes()) != digest:
                raise ValueError("canonical temporary digest mismatch")
            _read_canonical(temporary, series_id)
        notify("canonical_validated")
        for series_id, temporary in zip(FRED_SERIES_V2, temporaries, strict=True):
            final = _canonical_path(root, series_id)
            os.link(temporary, final)
            published.append(final)
        notify("canonical_published")
        for index, series_id in enumerate(FRED_SERIES_V2):
            path = _canonical_path(root, series_id)
            records = _read_canonical(path, series_id)
            quality = semantic["series"][index]["quality"]  # type: ignore[index]
            if (
                len(records) != quality["canonical_row_count"]
                or _digest(path.read_bytes()) != digests[index]
            ):  # type: ignore[index]
                raise ValueError("canonical cross-series validation failed")
        notify("cross_series_validated")
        manifest_temp = _temporary(root, ".manifest.", _json_bytes(manifest, pretty=True))
        loaded = json.loads(manifest_temp.read_text(encoding="utf-8"))
        if loaded != manifest:
            raise ValueError("manifest temporary reload mismatch")
        os.link(manifest_temp, manifest_path)
        published.append(manifest_path)
        notify("manifest_published")
        verified = verify_fred_alfred_v2_authority(root)
        notify("manifest_verified")
        return verified
    except BaseException:
        for path in reversed(published):
            _cleanup(path)
        raise
    finally:
        for path in temporaries:
            _cleanup(path)
        if manifest_temp is not None:
            _cleanup(manifest_temp)
        try:
            canonical_dir.rmdir()
        except OSError:
            pass


def _require_mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"manifest {field} invalid")
    return value


def verify_fred_alfred_v2_authority(root: Path) -> FredAlfredV2PublishedAuthority:
    """Reload and fully verify a published canonical FRED/ALFRED V2 authority."""
    manifest_path = root / FRED_ALFRED_V2_MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise TypeError("manifest must be an object")
    manifest_digest = manifest.pop("manifest_semantic_digest", None)
    if manifest_digest != _digest(_json_bytes(manifest)):
        raise ValueError("manifest semantic digest mismatch")
    series = manifest.get("series")
    if not isinstance(series, list) or len(series) != len(FRED_SERIES_V2):
        raise ValueError("manifest series invalid")
    expected_binding_ids = tuple(
        item.availability_policy_identity for item in FRED_CAUSAL_AVAILABILITY_BINDINGS_V2
    )
    expected_staleness = tuple(
        item.staleness_policy for item in FRED_CAUSAL_AVAILABILITY_BINDINGS_V2
    )
    loaded_digests: list[str] = []
    for index, series_id in enumerate(FRED_SERIES_V2):
        item = _require_mapping(series[index], "series item")
        path = _canonical_path(root, series_id)
        records = _read_canonical(path, series_id)
        digest = _digest(path.read_bytes())
        loaded_digests.append(digest)
        quality = _require_mapping(item.get("quality"), "quality")
        if (
            item.get("series_id") != series_id
            or item.get("canonical_file") != path.name
            or item.get("canonical_digest") != digest
            or item.get("availability_policy_identity") != expected_binding_ids[index]
            or item.get("staleness_policy") != expected_staleness[index]
            or item.get("staleness_calendar_identity")
            != FRED_CAUSAL_AVAILABILITY_BINDINGS_V2[index].staleness_calendar_identity
            or quality.get("canonical_row_count") != len(records)
            or quality.get("pit_usable_at_invariant_violations") != 0
            or quality.get("malformed_count") != 0
            or sum(record["value_state"] == VALID_SOURCE_ABSENCE for record in records)
            != quality.get("missing_count")
            or any(
                record["availability_policy_identity"] != expected_binding_ids[index]
                for record in records
            )
            or any(record["staleness_policy"] != expected_staleness[index] for record in records)
        ):
            raise ValueError("published series contract invalid")
    if (
        manifest.get("authority_identity") != FRED_ALFRED_GLOBAL_RISK_AUTHORITY_V2_IDENTITY
        or manifest.get("provider") != FRED_ALFRED_PROVIDER
        or manifest.get("status") != FRED_ALFRED_V2_AUTHORITY_VALIDATED
        or manifest.get("series_order") != list(FRED_SERIES_V2)
        or manifest.get("pit_identity") != FRED_ALFRED_V2_PIT_MATERIALIZATION_IDENTITY
        or manifest.get("pit_release_vintage_series") != list(RELEASE_VINTAGE_SERIES_V2)
        or manifest.get("pit_full_vintage_series") != list(FULL_VINTAGE_SERIES_V2)
        or manifest.get("calendar_authorities")
        != [
            NASDAQ_CALENDAR_AUTHORITY_IDENTITY,
            CBOE_VIX_DAILY_CLOSE_SESSION_CALENDAR_IDENTITY,
            US_FEDERAL_BUSINESS_DAY_CALENDAR_IDENTITY,
        ]
        or manifest.get("ranges", {}).get("source") != list(SOURCE_RANGE_V2)  # type: ignore[union-attr]
        or manifest.get("blind_exclusion", {}).get("rows_published") != 0  # type: ignore[union-attr]
    ):
        raise ValueError("manifest authority contract invalid")
    semantic = {
        key: value
        for key, value in manifest.items()
        if key not in {"semantic_authority_digest", "status"}
    }
    authority_digest = _digest(_json_bytes(semantic))
    if authority_digest != manifest.get("semantic_authority_digest"):
        raise ValueError("semantic authority digest mismatch")
    return FredAlfredV2PublishedAuthority(
        root,
        tuple(loaded_digests),
        authority_digest,
        str(manifest_digest),
    )
