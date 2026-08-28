"""Bounded HTTP transport for one sanitized FRED/ALFRED V2 request part."""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

import httpx

from ..fred_alfred_v2 import FRED_CREDENTIAL_ENVIRONMENT_VARIABLE_V2
from .contracts import (
    FRED_ALLOWED_ENDPOINTS,
    FRED_OFFICIAL_API_HOST,
    FRED_SERIES_OBSERVATIONS_ENDPOINT,
    PROVIDER_ERROR_CATEGORIES_V2,
    RequestPartV2,
)

FRED_HTTP_TIMEOUT_SECONDS = 10.0
FRED_HTTP_MAX_ATTEMPTS = 3
FRED_HTTP_RETRY_DELAYS_SECONDS = (0.25, 0.5)
FRED_HTTP_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

type JsonScalar = str | int | float | bool | None
type FrozenJson = JsonScalar | tuple["FrozenJson", ...] | Mapping[str, "FrozenJson"]


@dataclass(frozen=True, slots=True)
class FredAlfredTransportError(RuntimeError):
    """A categorized failure with deliberately credential-free diagnostics."""

    category: str
    attempts: int
    status_code: int | None = None
    reason: str = "transport"

    def __post_init__(self) -> None:
        if self.category not in PROVIDER_ERROR_CATEGORIES_V2:
            raise ValueError("unknown FRED provider error category")

    def __str__(self) -> str:
        status = "none" if self.status_code is None else str(self.status_code)
        return (
            "FRED/ALFRED transport failed "
            f"category={self.category} status={status} attempts={self.attempts} "
            f"reason={self.reason}"
        )


def _thaw(value: FrozenJson) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


@dataclass(frozen=True, slots=True)
class ProviderPageV2:
    """Immutable, sanitized structural evidence for exactly one provider page."""

    request_part_identity: str
    series_id: str
    endpoint: str
    provider_count: int
    provider_offset: int
    provider_limit: int
    returned_record_count: int
    records: tuple[FrozenJson, ...]
    response_metadata: tuple[tuple[str, str], ...]

    @property
    def identity(self) -> str:
        content = {
            "request_part_identity": self.request_part_identity,
            "series_id": self.series_id,
            "endpoint": self.endpoint,
            "provider_count": self.provider_count,
            "provider_offset": self.provider_offset,
            "provider_limit": self.provider_limit,
            "returned_record_count": self.returned_record_count,
            "records": _thaw(self.records),
            "response_metadata": self.response_metadata,
        }
        return hashlib.sha256(_canonical_json(content).encode()).hexdigest()


def _failure(
    category: str, attempts: int, *, status: int | None = None, reason: str
) -> FredAlfredTransportError:
    return FredAlfredTransportError(category, attempts, status, reason)


def _status_failure(status: int, attempts: int) -> FredAlfredTransportError:
    if status == 401:
        category = "AUTHENTICATION_FAILURE"
    elif status == 403:
        category = "AUTHORIZATION_FAILURE"
    elif status == 429:
        category = "RATE_LIMITED"
    elif status in FRED_HTTP_RETRYABLE_STATUSES:
        category = "TRANSIENT_PROVIDER_FAILURE"
    else:
        category = "PERMANENT_PROVIDER_FAILURE"
    return _failure(category, attempts, status=status, reason="http")


def _integer_like(value: object, name: str, attempts: int) -> int:
    if isinstance(value, bool):
        raise _failure("MALFORMED_PROVIDER_RESPONSE", attempts, reason=f"invalid_{name}")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.isascii() and value.isdigit():
        parsed = int(value)
    else:
        raise _failure("MALFORMED_PROVIDER_RESPONSE", attempts, reason=f"invalid_{name}")
    if parsed < 0:
        raise _failure("MALFORMED_PROVIDER_RESPONSE", attempts, reason=f"invalid_{name}")
    return parsed


def _freeze_json(value: object, attempts: int) -> FrozenJson:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list):
        return tuple(_freeze_json(item, attempts) for item in value)
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        forbidden = {"api_key", "apikey", "credential", "secret"}
        if forbidden & {key.lower() for key in value}:
            raise _failure("MALFORMED_PROVIDER_RESPONSE", attempts, reason="unsafe_record")
        return MappingProxyType({key: _freeze_json(item, attempts) for key, item in value.items()})
    raise _failure("MALFORMED_PROVIDER_RESPONSE", attempts, reason="invalid_json_value")


def _json_content_type(content_type: str | None) -> bool:
    if content_type is None:
        return True
    media_type = content_type.partition(";")[0].strip().lower()
    return media_type == "application/json" or media_type.endswith("+json")


def _reject_nonstandard_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _contains_secret(value: object, credential: str) -> bool:
    if isinstance(value, str):
        return credential in value
    if isinstance(value, list):
        return any(_contains_secret(item, credential) for item in value)
    if isinstance(value, dict):
        return any(
            credential in str(key) or _contains_secret(item, credential)
            for key, item in value.items()
        )
    return False


def _provider_page(
    request_part: RequestPartV2,
    response: httpx.Response,
    attempts: int,
    credential: str,
) -> ProviderPageV2:
    content_type = response.headers.get("content-type")
    if not _json_content_type(content_type):
        raise _failure("MALFORMED_PROVIDER_RESPONSE", attempts, reason="content_type")
    try:
        payload = json.loads(
            response.content,
            parse_constant=_reject_nonstandard_json_constant,
        )
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        raise _failure("MALFORMED_PROVIDER_RESPONSE", attempts, reason="json") from None
    if not isinstance(payload, dict):
        raise _failure("MALFORMED_PROVIDER_RESPONSE", attempts, reason="json_object")
    if _contains_secret(payload, credential):
        raise _failure("MALFORMED_PROVIDER_RESPONSE", attempts, reason="unsafe_response")
    count = _integer_like(payload.get("count"), "count", attempts)
    offset = _integer_like(payload.get("offset"), "offset", attempts)
    limit = _integer_like(payload.get("limit"), "limit", attempts)
    requested = dict(request_part.parameters)
    if offset != int(requested["offset"]):
        raise _failure("MALFORMED_PROVIDER_RESPONSE", attempts, reason="offset_mismatch")
    if limit <= 0 or limit != int(requested["limit"]) or limit != request_part.pagination.limit:
        raise _failure("MALFORMED_PROVIDER_RESPONSE", attempts, reason="limit_mismatch")
    record_key = (
        "observations"
        if request_part.endpoint == FRED_SERIES_OBSERVATIONS_ENDPOINT
        else "vintage_dates"
    )
    raw_records = payload.get(record_key)
    if not isinstance(raw_records, list):
        raise _failure("MALFORMED_PROVIDER_RESPONSE", attempts, reason=f"invalid_{record_key}")
    if len(raw_records) > limit:
        raise _failure("MALFORMED_PROVIDER_RESPONSE", attempts, reason="records_exceed_limit")
    if count > offset and not raw_records:
        raise _failure("MALFORMED_PROVIDER_RESPONSE", attempts, reason="premature_empty")
    if record_key == "observations" and any(not isinstance(row, dict) for row in raw_records):
        raise _failure("MALFORMED_PROVIDER_RESPONSE", attempts, reason="invalid_observation")
    if record_key == "vintage_dates" and any(not isinstance(row, str) for row in raw_records):
        raise _failure("MALFORMED_PROVIDER_RESPONSE", attempts, reason="invalid_vintage_date")
    metadata = (
        ()
        if content_type is None
        else (("content_type", content_type.partition(";")[0].strip().lower()),)
    )
    records = tuple(_freeze_json(row, attempts) for row in raw_records)
    return ProviderPageV2(
        request_part.identity,
        request_part.series_id,
        request_part.endpoint,
        count,
        offset,
        limit,
        len(records),
        records,
        metadata,
    )


def execute_request_part(
    request_part: RequestPartV2, client: httpx.Client, sleeper: Callable[[float], None] = time.sleep
) -> ProviderPageV2:
    """Execute one released request part without persisting or interpreting records."""
    if request_part.endpoint not in FRED_ALLOWED_ENDPOINTS:
        raise _failure("PERMANENT_PROVIDER_FAILURE", 0, reason="endpoint")
    credential = os.environ.get(FRED_CREDENTIAL_ENVIRONMENT_VARIABLE_V2)
    if not credential:
        raise RuntimeError("FRED_API_KEY_REQUIRED")
    parameters = dict(request_part.parameters)
    parameters["api_key"] = credential
    url = f"{FRED_OFFICIAL_API_HOST}/{request_part.endpoint}"
    for attempt in range(1, FRED_HTTP_MAX_ATTEMPTS + 1):
        try:
            response = client.get(
                url, params=parameters, timeout=FRED_HTTP_TIMEOUT_SECONDS, follow_redirects=False
            )
        except (httpx.TimeoutException, httpx.NetworkError):
            if attempt == FRED_HTTP_MAX_ATTEMPTS:
                raise _failure("TRANSIENT_PROVIDER_FAILURE", attempt, reason="network") from None
            sleeper(FRED_HTTP_RETRY_DELAYS_SECONDS[attempt - 1])
            continue
        if response.status_code == 200:
            return _provider_page(request_part, response, attempt, credential)
        if (
            response.status_code not in FRED_HTTP_RETRYABLE_STATUSES
            or attempt == FRED_HTTP_MAX_ATTEMPTS
        ):
            raise _status_failure(response.status_code, attempt)
        sleeper(FRED_HTTP_RETRY_DELAYS_SECONDS[attempt - 1])
    raise AssertionError("unreachable FRED transport state")
