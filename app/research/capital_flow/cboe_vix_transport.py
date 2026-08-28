"""HTTP streaming transport for the official Cboe VIX daily CSV."""

from __future__ import annotations

import codecs
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Protocol

import httpx

from .cboe_vix_authority import CboeVixBoundedResultV1, parse_bounded_cboe_vix_csv

CBOE_VIX_OFFICIAL_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
CBOE_VIX_HTTP_TIMEOUT_SECONDS = 10.0
CBOE_VIX_HTTP_MAX_ATTEMPTS = 3
CBOE_VIX_HTTP_RETRY_DELAYS_SECONDS = (0.25, 0.5)
CBOE_VIX_HTTP_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


class _StreamingResponse(Protocol):
    status_code: int

    def iter_bytes(self) -> Iterator[bytes]: ...


class _ResponseContext(Protocol):
    def __enter__(self) -> _StreamingResponse: ...

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> object: ...


class CboeVixHttpClient(Protocol):
    def stream(self, method: str, url: str, *, timeout: float) -> _ResponseContext: ...


@dataclass(frozen=True, slots=True)
class CboeVixTransportError(RuntimeError):
    status_code: int | None
    attempts: int
    reason: str

    def __str__(self) -> str:
        status = "none" if self.status_code is None else str(self.status_code)
        return (
            "CBOE VIX transport failed "
            f"endpoint=official status={status} attempts={self.attempts} reason={self.reason}"
        )


def _strict_utf8_lines(response: _StreamingResponse, attempts: int) -> Iterator[str]:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    pending = ""
    try:
        for chunk in response.iter_bytes():
            pending += decoder.decode(chunk)
            while "\n" in pending:
                line, pending = pending.split("\n", 1)
                yield f"{line}\n"
    except httpx.TransportError as error:
        raise CboeVixTransportError(None, attempts, "stream") from error
    pending += decoder.decode(b"", final=True)
    if pending:
        yield pending


def _validate_policy(timeout: float, max_attempts: int, retry_delays: Sequence[float]) -> None:
    if timeout <= 0 or max_attempts < 1 or len(retry_delays) < max_attempts - 1:
        raise ValueError("invalid Cboe VIX transport policy")
    if any(delay < 0 for delay in retry_delays[: max_attempts - 1]):
        raise ValueError("invalid Cboe VIX retry delay")


@contextmanager
def open_official_cboe_vix_csv(
    *,
    client: CboeVixHttpClient | None = None,
    timeout: float = CBOE_VIX_HTTP_TIMEOUT_SECONDS,
    max_attempts: int = CBOE_VIX_HTTP_MAX_ATTEMPTS,
    retry_delays: Sequence[float] = CBOE_VIX_HTTP_RETRY_DELAYS_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[Iterable[str]]:
    """Open the official response as strict UTF-8 lines without buffering its body."""
    _validate_policy(timeout, max_attempts, retry_delays)
    owned_client = httpx.Client() if client is None else None
    active_client = owned_client or client
    response_context: _ResponseContext | None = None
    response: _StreamingResponse | None = None
    try:
        for attempt in range(1, max_attempts + 1):
            try:
                response_context = active_client.stream(
                    "GET", CBOE_VIX_OFFICIAL_URL, timeout=timeout
                )
                response = response_context.__enter__()
            except (httpx.TimeoutException, httpx.NetworkError) as error:
                response_context = None
                if attempt == max_attempts:
                    raise CboeVixTransportError(None, attempt, "transient") from error
                sleep(retry_delays[attempt - 1])
                continue
            if response.status_code == 200:
                break
            status = response.status_code
            response_context.__exit__(None, None, None)
            response_context = None
            response = None
            if status not in CBOE_VIX_HTTP_RETRYABLE_STATUSES or attempt == max_attempts:
                reason = (
                    "http_retry_exhausted" if status in CBOE_VIX_HTTP_RETRYABLE_STATUSES else "http"
                )
                raise CboeVixTransportError(status, attempt, reason)
            sleep(retry_delays[attempt - 1])
        if response is None:
            raise AssertionError("Cboe VIX response was not established")
        yield _strict_utf8_lines(response, attempt)
    finally:
        if response_context is not None:
            response_context.__exit__(None, None, None)
        if owned_client is not None:
            owned_client.close()


def fetch_and_parse_bounded_cboe_vix(
    *,
    client: CboeVixHttpClient | None = None,
    timeout: float = CBOE_VIX_HTTP_TIMEOUT_SECONDS,
    max_attempts: int = CBOE_VIX_HTTP_MAX_ATTEMPTS,
    retry_delays: Sequence[float] = CBOE_VIX_HTTP_RETRY_DELAYS_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> CboeVixBoundedResultV1:
    """Fetch and parse in memory; this does not publish an authority artifact."""
    with open_official_cboe_vix_csv(
        client=client,
        timeout=timeout,
        max_attempts=max_attempts,
        retry_delays=retry_delays,
        sleep=sleep,
    ) as lines:
        return parse_bounded_cboe_vix_csv(lines)
