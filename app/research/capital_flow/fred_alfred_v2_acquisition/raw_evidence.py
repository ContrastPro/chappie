"""Deterministic raw-page evidence and create-only cache for FRED/ALFRED V2."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from ..fred_alfred_v2 import (
    FRED_ALFRED_GLOBAL_RISK_AUTHORITY_V2_IDENTITY,
    FRED_SERIES_V2,
    SOURCE_RANGE_V2,
)
from .contracts import (
    FRED_ALLOWED_ENDPOINTS,
    FULL_VINTAGE_SERIES_V2,
    RELEASE_VINTAGE_SERIES_V2,
    RequestPartV2,
    RequestPlanV2,
)
from .request_plan import build_full_vintage_observation_parts
from .transport import ProviderPageV2

FRED_ALFRED_V2_RAW_EVIDENCE_IDENTITY = "chappie_fred_alfred_v2_raw_evidence/1.0.0"
FRED_ALFRED_V2_RAW_EVIDENCE_ROOT = Path("data/research/source/capital_flow/fred_alfred_v2/raw")

CACHE_CREATED = "CACHE_CREATED"
CACHE_REUSED = "CACHE_REUSED"
RAW_EVIDENCE_CACHE_CORRUPT = "RAW_EVIDENCE_CACHE_CORRUPT"
RAW_EVIDENCE_CACHE_CONFLICT = "RAW_EVIDENCE_CACHE_CONFLICT"

EVIDENCE_COMPLETE = "COMPLETE"
EVIDENCE_INCOMPLETE = "INCOMPLETE"
EVIDENCE_CORRUPT = "CORRUPT"
VINTAGE_ENUMERATION_COMPLETE = "VINTAGE_ENUMERATION_COMPLETE"
OBSERVATION_PARTS_COMPLETE = "OBSERVATION_PARTS_COMPLETE"
FULL_RAW_EVIDENCE_COMPLETE = "FULL_RAW_EVIDENCE_COMPLETE"
RELEASE_RAW_EVIDENCE_COMPLETE = "RELEASE_RAW_EVIDENCE_COMPLETE"
FULL_VINTAGE_DYNAMIC_REQUEST_DERIVATION = "RELEASED_REQUEST_PLAN_INTERFACE"

type JsonScalar = str | int | float | bool | None
type FrozenJson = JsonScalar | tuple["FrozenJson", ...] | Mapping[str, "FrozenJson"]


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode()


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-standard JSON constant")


def _freeze(value: object) -> FrozenJson:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, Mapping) and all(isinstance(key, str) for key in value):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    raise ValueError("raw evidence contains an invalid JSON structural value")


def _thaw(value: FrozenJson) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class RawEvidenceCacheError(RuntimeError):
    category: str
    series_id: str
    request_part_identity: str

    def __str__(self) -> str:
        return (
            f"FRED/ALFRED raw evidence failure category={self.category} "
            f"series={self.series_id} request_part={self.request_part_identity}"
        )


def _semantic_payload(evidence: RawEvidencePartV2) -> dict[str, object]:
    return {
        "schema_identity": evidence.schema_identity,
        "authority_identity": evidence.authority_identity,
        "series_id": evidence.series_id,
        "request_part_identity": evidence.request_part_identity,
        "provider_page_identity": evidence.provider_page_identity,
        "endpoint": evidence.endpoint,
        "request_kind": evidence.request_kind,
        "request_index": evidence.request_index,
        "pit_strategy": evidence.pit_strategy,
        "authorized_observation_bounds": list(evidence.authorized_observation_bounds),
        "provider_count": evidence.provider_count,
        "provider_offset": evidence.provider_offset,
        "provider_limit": evidence.provider_limit,
        "returned_record_count": evidence.returned_record_count,
        "provider_records": _thaw(evidence.provider_records),
        "provider_response_metadata": [list(item) for item in evidence.provider_response_metadata],
    }


@dataclass(frozen=True, slots=True)
class RawEvidencePartV2:
    schema_identity: str
    authority_identity: str
    series_id: str
    request_part_identity: str
    provider_page_identity: str
    endpoint: str
    request_kind: str
    request_index: int
    pit_strategy: str
    authorized_observation_bounds: tuple[str, str]
    provider_count: int
    provider_offset: int
    provider_limit: int
    returned_record_count: int
    provider_records: tuple[FrozenJson, ...]
    provider_response_metadata: tuple[tuple[str, str], ...]
    raw_semantic_digest: str

    def __post_init__(self) -> None:
        if (
            self.schema_identity != FRED_ALFRED_V2_RAW_EVIDENCE_IDENTITY
            or self.authority_identity != FRED_ALFRED_GLOBAL_RISK_AUTHORITY_V2_IDENTITY
            or self.series_id not in FRED_SERIES_V2
            or self.endpoint not in FRED_ALLOWED_ENDPOINTS
            or self.authorized_observation_bounds != SOURCE_RANGE_V2
            or not self.request_kind
            or self.request_index < 0
        ):
            raise ValueError("raw evidence identity contract invalid")
        if (
            any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in (
                    self.provider_count,
                    self.provider_offset,
                    self.provider_limit,
                    self.returned_record_count,
                )
            )
            or self.provider_limit <= 0
        ):
            raise ValueError("raw evidence pagination metadata invalid")
        if (
            self.returned_record_count != len(self.provider_records)
            or self.returned_record_count > self.provider_limit
        ):
            raise ValueError("raw evidence record count invalid")
        if any(
            len(item) != 2 or not all(isinstance(value, str) for value in item)
            for item in self.provider_response_metadata
        ):
            raise ValueError("raw evidence response metadata invalid")
        reconstructed_page = ProviderPageV2(
            self.request_part_identity,
            self.series_id,
            self.endpoint,
            self.provider_count,
            self.provider_offset,
            self.provider_limit,
            self.returned_record_count,
            self.provider_records,
            self.provider_response_metadata,
        )
        if reconstructed_page.identity != self.provider_page_identity:
            raise ValueError("raw evidence provider page identity invalid")
        if self.raw_semantic_digest != _digest(_semantic_payload(self)):
            raise ValueError("raw evidence semantic digest invalid")

    @property
    def identity(self) -> str:
        return self.raw_semantic_digest


@dataclass(frozen=True, slots=True)
class RawEvidenceCacheResultV2:
    status: str
    relative_path: Path
    evidence: RawEvidencePartV2

    def __post_init__(self) -> None:
        if self.status not in (CACHE_CREATED, CACHE_REUSED) or self.relative_path.is_absolute():
            raise ValueError("raw evidence cache result invalid")


@dataclass(frozen=True, slots=True)
class RawEvidenceSetV2:
    series_id: str
    status: str
    achieved_stages: tuple[str, ...]
    required_request_part_identities: tuple[str, ...]
    validated_request_part_identities: tuple[str, ...]
    missing_request_part_identities: tuple[str, ...]
    corrupt_request_part_identities: tuple[str, ...]
    dynamic_request_derivation: str | None
    semantic_digest: str

    def __post_init__(self) -> None:
        if self.series_id not in FRED_SERIES_V2 or self.status not in (
            EVIDENCE_COMPLETE,
            EVIDENCE_INCOMPLETE,
            EVIDENCE_CORRUPT,
        ):
            raise ValueError("raw evidence set contract invalid")
        payload = {
            "series_id": self.series_id,
            "status": self.status,
            "achieved_stages": self.achieved_stages,
            "required_request_part_identities": self.required_request_part_identities,
            "validated_request_part_identities": self.validated_request_part_identities,
            "missing_request_part_identities": self.missing_request_part_identities,
            "corrupt_request_part_identities": self.corrupt_request_part_identities,
            "dynamic_request_derivation": self.dynamic_request_derivation,
        }
        if self.semantic_digest != _digest(payload):
            raise ValueError("raw evidence set digest invalid")


def build_raw_evidence(
    request_part: RequestPartV2, provider_page: ProviderPageV2
) -> RawEvidencePartV2:
    """Bind one sanitized provider page to its exact released request part."""
    if (
        provider_page.request_part_identity != request_part.identity
        or provider_page.series_id != request_part.series_id
        or provider_page.endpoint != request_part.endpoint
        or provider_page.provider_offset != int(dict(request_part.parameters)["offset"])
        or provider_page.provider_limit != request_part.pagination.limit
    ):
        raise ValueError("request part and provider page binding mismatch")
    values = {
        "schema_identity": FRED_ALFRED_V2_RAW_EVIDENCE_IDENTITY,
        "authority_identity": FRED_ALFRED_GLOBAL_RISK_AUTHORITY_V2_IDENTITY,
        "series_id": request_part.series_id,
        "request_part_identity": request_part.identity,
        "provider_page_identity": provider_page.identity,
        "endpoint": request_part.endpoint,
        "request_kind": request_part.part_kind,
        "request_index": request_part.part_index,
        "pit_strategy": request_part.pit_strategy,
        "authorized_observation_bounds": request_part.authorized_reference_bounds,
        "provider_count": provider_page.provider_count,
        "provider_offset": provider_page.provider_offset,
        "provider_limit": provider_page.provider_limit,
        "returned_record_count": provider_page.returned_record_count,
        "provider_records": tuple(_freeze(item) for item in provider_page.records),
        "provider_response_metadata": provider_page.response_metadata,
    }
    provisional = object.__new__(RawEvidencePartV2)
    for key, value in values.items():
        object.__setattr__(provisional, key, value)
    digest = _digest(_semantic_payload(provisional))
    return RawEvidencePartV2(**values, raw_semantic_digest=digest)


def serialize_raw_evidence(evidence: RawEvidencePartV2) -> bytes:
    """Return stable UTF-8 canonical JSON with the self-verifying digest."""
    return _canonical_bytes(
        {**_semantic_payload(evidence), "raw_semantic_digest": evidence.raw_semantic_digest}
    )


def raw_evidence_relative_path(series_id: str, request_part_identity: str) -> Path:
    if (
        series_id not in FRED_SERIES_V2
        or len(request_part_identity) != 64
        or any(character not in "0123456789abcdef" for character in request_part_identity)
    ):
        raise ValueError("unsafe raw evidence cache identity")
    return Path(series_id) / f"{request_part_identity}.json"


def _cache_error(category: str, request_part: RequestPartV2) -> RawEvidenceCacheError:
    return RawEvidenceCacheError(category, request_part.series_id, request_part.identity)


def _parse_raw_evidence(payload: bytes, request_part: RequestPartV2) -> RawEvidencePartV2:
    try:
        item = json.loads(payload, parse_constant=_reject_json_constant)
        if not isinstance(item, dict):
            raise TypeError
        expected_keys = {
            "schema_identity",
            "authority_identity",
            "series_id",
            "request_part_identity",
            "provider_page_identity",
            "endpoint",
            "request_kind",
            "request_index",
            "pit_strategy",
            "authorized_observation_bounds",
            "provider_count",
            "provider_offset",
            "provider_limit",
            "returned_record_count",
            "provider_records",
            "provider_response_metadata",
            "raw_semantic_digest",
        }
        if set(item) != expected_keys:
            raise ValueError
        bounds = item["authorized_observation_bounds"]
        records = item["provider_records"]
        metadata = item["provider_response_metadata"]
        if (
            not isinstance(bounds, list)
            or len(bounds) != 2
            or not all(isinstance(value, str) for value in bounds)
            or not isinstance(records, list)
            or not isinstance(metadata, list)
            or any(
                not isinstance(entry, list)
                or len(entry) != 2
                or not all(isinstance(value, str) for value in entry)
                for entry in metadata
            )
        ):
            raise ValueError
        evidence = RawEvidencePartV2(
            schema_identity=item["schema_identity"],
            authority_identity=item["authority_identity"],
            series_id=item["series_id"],
            request_part_identity=item["request_part_identity"],
            provider_page_identity=item["provider_page_identity"],
            endpoint=item["endpoint"],
            request_kind=item["request_kind"],
            request_index=item["request_index"],
            pit_strategy=item["pit_strategy"],
            authorized_observation_bounds=tuple(bounds),
            provider_count=item["provider_count"],
            provider_offset=item["provider_offset"],
            provider_limit=item["provider_limit"],
            returned_record_count=item["returned_record_count"],
            provider_records=tuple(_freeze(value) for value in records),
            provider_response_metadata=tuple(tuple(value) for value in metadata),
            raw_semantic_digest=item["raw_semantic_digest"],
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise _cache_error(RAW_EVIDENCE_CACHE_CORRUPT, request_part) from None
    if (
        evidence.series_id != request_part.series_id
        or evidence.request_part_identity != request_part.identity
        or evidence.endpoint != request_part.endpoint
        or evidence.request_kind != request_part.part_kind
        or evidence.request_index != request_part.part_index
        or evidence.pit_strategy != request_part.pit_strategy
        or evidence.authorized_observation_bounds != request_part.authorized_reference_bounds
        or evidence.provider_offset != int(dict(request_part.parameters)["offset"])
        or evidence.provider_limit != request_part.pagination.limit
    ):
        raise _cache_error(RAW_EVIDENCE_CACHE_CORRUPT, request_part)
    return evidence


def load_raw_evidence(path: Path, request_part: RequestPartV2) -> RawEvidencePartV2:
    """Reload and fully verify an authoritative final cache file."""
    if path.suffix != ".json" or path.name.endswith((".partial", ".tmp")):
        raise _cache_error(RAW_EVIDENCE_CACHE_CORRUPT, request_part)
    try:
        payload = path.read_bytes()
    except OSError:
        raise _cache_error(RAW_EVIDENCE_CACHE_CORRUPT, request_part) from None
    return _parse_raw_evidence(payload, request_part)


def verify_raw_evidence(
    evidence: RawEvidencePartV2,
    request_part: RequestPartV2,
    provider_page_identity: str | None = None,
) -> RawEvidencePartV2:
    """Validate all resume bindings without trusting filename existence."""
    reloaded = _parse_raw_evidence(serialize_raw_evidence(evidence), request_part)
    if (
        provider_page_identity is not None
        and reloaded.provider_page_identity != provider_page_identity
    ):
        raise _cache_error(RAW_EVIDENCE_CACHE_CONFLICT, request_part)
    return reloaded


def _cleanup(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def write_raw_evidence(
    request_part: RequestPartV2,
    provider_page: ProviderPageV2,
    root: Path = FRED_ALFRED_V2_RAW_EVIDENCE_ROOT,
    *,
    phase_hook: Callable[[str, Path], None] | None = None,
) -> RawEvidenceCacheResultV2:
    """Create or reuse one fully validated cache entry without overwriting evidence."""
    evidence = build_raw_evidence(request_part, provider_page)
    relative = raw_evidence_relative_path(request_part.series_id, request_part.identity)
    final = root / relative
    if final.exists():
        existing = load_raw_evidence(final, request_part)
        if (
            existing.provider_page_identity != evidence.provider_page_identity
            or existing.raw_semantic_digest != evidence.raw_semantic_digest
        ):
            raise _cache_error(RAW_EVIDENCE_CACHE_CONFLICT, request_part)
        return RawEvidenceCacheResultV2(CACHE_REUSED, relative, existing)

    final.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{request_part.identity}.", suffix=".tmp", dir=final.parent
    )
    temporary = Path(temporary_name)
    notify = phase_hook or (lambda _phase, _path: None)
    created = False
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(serialize_raw_evidence(evidence))
            handle.flush()
            os.fsync(handle.fileno())
        notify("temporary_closed", temporary)
        reloaded = _parse_raw_evidence(temporary.read_bytes(), request_part)
        verify_raw_evidence(reloaded, request_part, provider_page.identity)
        notify("temporary_validated", temporary)
        try:
            os.link(temporary, final)
        except FileExistsError:
            existing = load_raw_evidence(final, request_part)
            if (
                existing.provider_page_identity != evidence.provider_page_identity
                or existing.raw_semantic_digest != evidence.raw_semantic_digest
            ):
                raise _cache_error(RAW_EVIDENCE_CACHE_CONFLICT, request_part) from None
            return RawEvidenceCacheResultV2(CACHE_REUSED, relative, existing)
        created = True
        notify("cache_created", final)
        published = load_raw_evidence(final, request_part)
        verify_raw_evidence(published, request_part, provider_page.identity)
        return RawEvidenceCacheResultV2(CACHE_CREATED, relative, published)
    except BaseException:
        if created:
            _cleanup(final)
        raise
    finally:
        _cleanup(temporary)


def _evidence_set(
    series_id: str,
    status: str,
    stages: tuple[str, ...],
    required: tuple[str, ...],
    validated: tuple[str, ...],
    missing: tuple[str, ...],
    corrupt: tuple[str, ...],
    derivation: str | None,
) -> RawEvidenceSetV2:
    payload = {
        "series_id": series_id,
        "status": status,
        "achieved_stages": stages,
        "required_request_part_identities": required,
        "validated_request_part_identities": validated,
        "missing_request_part_identities": missing,
        "corrupt_request_part_identities": corrupt,
        "dynamic_request_derivation": derivation,
    }
    return RawEvidenceSetV2(
        series_id,
        status,
        stages,
        required,
        validated,
        missing,
        corrupt,
        derivation,
        _digest(payload),
    )


def inspect_raw_evidence_set(
    request_plan: RequestPlanV2,
    evidence_parts: Iterable[RawEvidencePartV2],
    *,
    corrupt_request_part_identities: Iterable[str] = (),
) -> RawEvidenceSetV2:
    """Inspect structural completeness without acquiring or materializing authority data."""
    parts = tuple(evidence_parts)
    corrupt = tuple(sorted(set(corrupt_request_part_identities)))
    identities = tuple(sorted(part.request_part_identity for part in parts))
    if (
        corrupt
        or len(identities) != len(set(identities))
        or any(part.series_id != request_plan.series_id for part in parts)
    ):
        return _evidence_set(
            request_plan.series_id,
            EVIDENCE_CORRUPT,
            (),
            tuple(part.identity for part in request_plan.initial_parts),
            tuple(sorted(set(identities))),
            (),
            corrupt or tuple(sorted(set(identities))),
            None,
        )

    initial_required = tuple(part.identity for part in request_plan.initial_parts)
    initial_by_identity = {part.identity: part for part in request_plan.initial_parts}
    validated_set = set(identities)
    initial_missing = tuple(value for value in initial_required if value not in validated_set)
    if request_plan.series_id in RELEASE_VINTAGE_SERIES_V2:
        invalid = tuple(identity for identity in identities if identity not in initial_by_identity)
        try:
            for part in parts:
                if part.request_part_identity in initial_by_identity:
                    verify_raw_evidence(part, initial_by_identity[part.request_part_identity])
        except RawEvidenceCacheError:
            invalid += (part.request_part_identity,)
        if invalid:
            return _evidence_set(
                request_plan.series_id,
                EVIDENCE_CORRUPT,
                (),
                initial_required,
                identities,
                initial_missing,
                tuple(sorted(set(invalid))),
                None,
            )
        stages = () if initial_missing else (RELEASE_RAW_EVIDENCE_COMPLETE,)
        status = EVIDENCE_INCOMPLETE if initial_missing else EVIDENCE_COMPLETE
        return _evidence_set(
            request_plan.series_id,
            status,
            stages,
            initial_required,
            identities,
            initial_missing,
            (),
            None,
        )

    if request_plan.series_id not in FULL_VINTAGE_SERIES_V2 or initial_missing:
        return _evidence_set(
            request_plan.series_id,
            EVIDENCE_INCOMPLETE,
            (),
            initial_required,
            identities,
            initial_missing,
            (),
            FULL_VINTAGE_DYNAMIC_REQUEST_DERIVATION,
        )
    enumeration = next(part for part in parts if part.request_part_identity == initial_required[0])
    try:
        verify_raw_evidence(enumeration, request_plan.initial_parts[0])
    except RawEvidenceCacheError:
        return _evidence_set(
            request_plan.series_id,
            EVIDENCE_CORRUPT,
            (),
            initial_required,
            identities,
            (),
            (enumeration.request_part_identity,),
            FULL_VINTAGE_DYNAMIC_REQUEST_DERIVATION,
        )
    if not all(isinstance(value, str) for value in enumeration.provider_records):
        return _evidence_set(
            request_plan.series_id,
            EVIDENCE_CORRUPT,
            (),
            initial_required,
            identities,
            (),
            (enumeration.request_part_identity,),
            FULL_VINTAGE_DYNAMIC_REQUEST_DERIVATION,
        )
    try:
        observation_parts = build_full_vintage_observation_parts(
            request_plan.series_id, tuple(enumeration.provider_records)
        )
    except ValueError:
        return _evidence_set(
            request_plan.series_id,
            EVIDENCE_CORRUPT,
            (VINTAGE_ENUMERATION_COMPLETE,),
            initial_required,
            identities,
            (),
            (enumeration.request_part_identity,),
            FULL_VINTAGE_DYNAMIC_REQUEST_DERIVATION,
        )
    required = initial_required + tuple(part.identity for part in observation_parts)
    expected_by_identity = {
        part.identity: part for part in (*request_plan.initial_parts, *observation_parts)
    }
    invalid: list[str] = []
    for evidence in parts:
        expected = expected_by_identity.get(evidence.request_part_identity)
        if expected is None:
            invalid.append(evidence.request_part_identity)
            continue
        try:
            verify_raw_evidence(evidence, expected)
        except RawEvidenceCacheError:
            invalid.append(evidence.request_part_identity)
    if invalid:
        return _evidence_set(
            request_plan.series_id,
            EVIDENCE_CORRUPT,
            (VINTAGE_ENUMERATION_COMPLETE,),
            required,
            identities,
            (),
            tuple(sorted(set(invalid))),
            FULL_VINTAGE_DYNAMIC_REQUEST_DERIVATION,
        )
    missing = tuple(value for value in required if value not in validated_set)
    stages = (VINTAGE_ENUMERATION_COMPLETE,)
    if not missing:
        stages += (OBSERVATION_PARTS_COMPLETE, FULL_RAW_EVIDENCE_COMPLETE)
    return _evidence_set(
        request_plan.series_id,
        EVIDENCE_INCOMPLETE if missing else EVIDENCE_COMPLETE,
        stages,
        required,
        identities,
        missing,
        (),
        FULL_VINTAGE_DYNAMIC_REQUEST_DERIVATION,
    )
