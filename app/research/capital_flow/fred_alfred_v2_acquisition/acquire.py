"""Bounded acquisition and cache-resume orchestration for FRED/ALFRED V2."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..fred_alfred_v2 import FRED_SERIES_V2
from .raw_evidence import (
    EVIDENCE_COMPLETE,
    RawEvidenceCacheError,
    RawEvidencePartV2,
    RawEvidenceSetV2,
    inspect_raw_evidence_set,
    load_raw_evidence,
    raw_evidence_relative_path,
    verify_raw_evidence,
    write_raw_evidence,
)
from .request_plan import (
    build_fred_alfred_v2_request_plan,
    build_full_vintage_observation_parts,
)
from .transport import FredAlfredTransportError, execute_request_part

ACQUISITION_COMPLETE = "COMPLETE"
ACQUISITION_INCOMPLETE = "INCOMPLETE"
ACQUISITION_FAILED = "FAILED"
FRED_ALFRED_V2_RAW_EVIDENCE_COMPLETE = "FRED_ALFRED_V2_RAW_EVIDENCE_COMPLETE"
REQUEST_PLAN_VALIDATION_FAILURE = "REQUEST_PLAN_VALIDATION_FAILURE"


def _digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class AcquisitionResultV2:
    series_id: str
    status: str
    required_part_count: int
    cache_reused_count: int
    downloaded_count: int
    raw_evidence_set_status: str
    raw_evidence_set_digest: str
    raw_evidence_part_digests: tuple[str, ...]
    semantic_digest: str

    def __post_init__(self) -> None:
        if (
            self.series_id not in FRED_SERIES_V2
            or self.status not in (FRED_ALFRED_V2_RAW_EVIDENCE_COMPLETE, ACQUISITION_INCOMPLETE)
            or min(self.required_part_count, self.cache_reused_count, self.downloaded_count) < 0
            or self.cache_reused_count + self.downloaded_count
            != len(self.raw_evidence_part_digests)
        ):
            raise ValueError("FRED acquisition result contract invalid")
        payload = {
            "series_id": self.series_id,
            "status": self.status,
            "required_part_count": self.required_part_count,
            "cache_reused_count": self.cache_reused_count,
            "downloaded_count": self.downloaded_count,
            "raw_evidence_set_status": self.raw_evidence_set_status,
            "raw_evidence_set_digest": self.raw_evidence_set_digest,
            "raw_evidence_part_digests": self.raw_evidence_part_digests,
        }
        if self.semantic_digest != _digest(payload):
            raise ValueError("FRED acquisition result digest invalid")


@dataclass(frozen=True, slots=True)
class AllAcquisitionResultV2:
    status: str
    series_results: tuple[AcquisitionResultV2, ...]
    failed_series_id: str | None
    failure_category: str | None
    semantic_digest: str

    def __post_init__(self) -> None:
        if self.status not in (
            ACQUISITION_COMPLETE,
            ACQUISITION_INCOMPLETE,
            ACQUISITION_FAILED,
        ):
            raise ValueError("FRED all-series acquisition status invalid")
        observed = tuple(result.series_id for result in self.series_results)
        if observed != FRED_SERIES_V2[: len(observed)]:
            raise ValueError("FRED all-series acquisition order invalid")
        if self.status == ACQUISITION_FAILED:
            if self.failed_series_id not in FRED_SERIES_V2 or not self.failure_category:
                raise ValueError("FRED failed acquisition result invalid")
        elif self.failed_series_id is not None or self.failure_category is not None:
            raise ValueError("FRED successful acquisition contains failure metadata")
        payload = {
            "status": self.status,
            "series_result_digests": tuple(
                result.semantic_digest for result in self.series_results
            ),
            "failed_series_id": self.failed_series_id,
            "failure_category": self.failure_category,
        }
        if self.semantic_digest != _digest(payload):
            raise ValueError("FRED all-series acquisition digest invalid")


@dataclass(frozen=True, slots=True)
class AllAcquisitionFailedV2(RuntimeError):
    result: AllAcquisitionResultV2

    def __str__(self) -> str:
        return (
            "FRED/ALFRED all-series acquisition failed "
            f"series={self.result.failed_series_id} category={self.result.failure_category}"
        )


def _all_result(
    status: str,
    series_results: tuple[AcquisitionResultV2, ...],
    failed_series_id: str | None = None,
    failure_category: str | None = None,
) -> AllAcquisitionResultV2:
    payload = {
        "status": status,
        "series_result_digests": tuple(result.semantic_digest for result in series_results),
        "failed_series_id": failed_series_id,
        "failure_category": failure_category,
    }
    return AllAcquisitionResultV2(
        status,
        series_results,
        failed_series_id,
        failure_category,
        _digest(payload),
    )


def _series_result(
    evidence_set: RawEvidenceSetV2,
    evidence_parts: tuple[RawEvidencePartV2, ...],
    reused: int,
    downloaded: int,
) -> AcquisitionResultV2:
    status = (
        FRED_ALFRED_V2_RAW_EVIDENCE_COMPLETE
        if evidence_set.status == EVIDENCE_COMPLETE
        else ACQUISITION_INCOMPLETE
    )
    payload = {
        "series_id": evidence_set.series_id,
        "status": status,
        "required_part_count": len(evidence_set.required_request_part_identities),
        "cache_reused_count": reused,
        "downloaded_count": downloaded,
        "raw_evidence_set_status": evidence_set.status,
        "raw_evidence_set_digest": evidence_set.semantic_digest,
        "raw_evidence_part_digests": tuple(part.raw_semantic_digest for part in evidence_parts),
    }
    return AcquisitionResultV2(**payload, semantic_digest=_digest(payload))


def _safe_cache_path(root: Path, series_id: str, request_identity: str) -> Path:
    root_resolved = root.resolve()
    candidate = (root / raw_evidence_relative_path(series_id, request_identity)).resolve()
    if not candidate.is_relative_to(root_resolved):
        raise ValueError("raw evidence cache path escapes configured root")
    return candidate


def _acquire_part(
    root: Path,
    request_part,
    client: httpx.Client,
    sleeper: Callable[[float], None],
) -> tuple[RawEvidencePartV2, bool]:
    cache_path = _safe_cache_path(root, request_part.series_id, request_part.identity)
    if cache_path.exists():
        evidence = load_raw_evidence(cache_path, request_part)
        return verify_raw_evidence(evidence, request_part), True
    provider_page = execute_request_part(request_part, client, sleeper)
    write_result = write_raw_evidence(request_part, provider_page, root)
    evidence = load_raw_evidence(root / write_result.relative_path, request_part)
    return verify_raw_evidence(evidence, request_part, provider_page.identity), False


def acquire_series_raw_evidence(
    root: Path,
    series_id: str,
    client: httpx.Client,
    sleeper: Callable[[float], None] = time.sleep,
) -> AcquisitionResultV2:
    """Acquire only missing raw pages for one exact released series plan."""
    plan = build_fred_alfred_v2_request_plan(series_id)
    acquired: list[RawEvidencePartV2] = []
    reused = 0
    downloaded = 0
    for request_part in plan.initial_parts:
        evidence, was_reused = _acquire_part(root, request_part, client, sleeper)
        acquired.append(evidence)
        reused += int(was_reused)
        downloaded += int(not was_reused)

    if plan.full_vintage_template is not None:
        enumeration = acquired[0]
        vintage_dates = tuple(enumeration.provider_records)
        if not all(isinstance(value, str) for value in vintage_dates):
            raise ValueError("validated vintage enumeration records are not strings")
        observation_parts = build_full_vintage_observation_parts(series_id, vintage_dates)
        for request_part in observation_parts:
            evidence, was_reused = _acquire_part(root, request_part, client, sleeper)
            acquired.append(evidence)
            reused += int(was_reused)
            downloaded += int(not was_reused)

    evidence_parts = tuple(acquired)
    evidence_set = inspect_raw_evidence_set(plan, evidence_parts)
    return _series_result(evidence_set, evidence_parts, reused, downloaded)


def _failure_category(error: BaseException) -> str:
    if isinstance(error, FredAlfredTransportError | RawEvidenceCacheError):
        return error.category
    if isinstance(error, ValueError):
        return REQUEST_PLAN_VALIDATION_FAILURE
    return type(error).__name__


def acquire_all_fred_alfred_v2_raw_evidence(
    root: Path,
    client: httpx.Client,
    sleeper: Callable[[float], None] = time.sleep,
) -> AllAcquisitionResultV2:
    """Acquire all exact V2 series in frozen order or fail with partial structural status."""
    results: list[AcquisitionResultV2] = []
    for series_id in FRED_SERIES_V2:
        try:
            results.append(acquire_series_raw_evidence(root, series_id, client, sleeper))
        except (FredAlfredTransportError, RawEvidenceCacheError, ValueError) as error:
            failed = _all_result(
                ACQUISITION_FAILED,
                tuple(results),
                series_id,
                _failure_category(error),
            )
            raise AllAcquisitionFailedV2(failed) from error
    ordered = tuple(results)
    status = (
        ACQUISITION_COMPLETE
        if len(ordered) == len(FRED_SERIES_V2)
        and all(result.status == FRED_ALFRED_V2_RAW_EVIDENCE_COMPLETE for result in ordered)
        else ACQUISITION_INCOMPLETE
    )
    return _all_result(status, ordered)
