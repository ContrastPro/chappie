"""Streaming transport and capability checks for DefiLlama stablecoin histories."""

from __future__ import annotations

import codecs
import json
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

import httpx

from .defillama_stablecoin_authority import (
    DEFILLAMA_FLAT_ENDPOINT,
    DEFILLAMA_STABLECOIN_ASSETS,
    DefiLlamaFilterEvidenceV1,
    DefiLlamaStablecoinBoundedResultV1,
    filter_evidence_from_prefix,
    parse_bounded_defillama_stablecoin_json,
)

DEFILLAMA_STABLECOIN_HTTP_TIMEOUT_SECONDS = 10.0
DEFILLAMA_STABLECOIN_HTTP_MAX_ATTEMPTS = 3
DEFILLAMA_STABLECOIN_HTTP_RETRY_DELAYS_SECONDS = (0.25, 0.5)
DEFILLAMA_STABLECOIN_HTTP_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
DEFILLAMA_STABLECOIN_REQUEST_HEADERS = {
    "Accept": "application/json",
    "Accept-Encoding": "identity",
}


class _Response(Protocol):
    status_code: int
    headers: object

    def iter_bytes(self) -> Iterator[bytes]: ...


class _Context(Protocol):
    def __enter__(self) -> _Response: ...

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> object: ...


class DefiLlamaHttpClient(Protocol):
    def stream(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str],
        headers: dict[str, str],
        timeout: float,
    ) -> _Context: ...


@dataclass(frozen=True, slots=True)
class DefiLlamaTransportError(RuntimeError):
    status_code: int | None
    attempts: int
    reason: str

    def __str__(self) -> str:
        return f"DefiLlama transport failed status={self.status_code} attempts={self.attempts} reason={self.reason}"


def _asset(asset_id: int) -> tuple[int, str]:
    for item in DEFILLAMA_STABLECOIN_ASSETS:
        if item[0] == asset_id:
            return item
    raise ValueError("unexpected DefiLlama stablecoin asset")


@contextmanager
def open_defillama_stablecoin_stream(
    asset_id: int,
    *,
    client: DefiLlamaHttpClient | None = None,
    timeout: float = DEFILLAMA_STABLECOIN_HTTP_TIMEOUT_SECONDS,
    max_attempts: int = DEFILLAMA_STABLECOIN_HTTP_MAX_ATTEMPTS,
    retry_delays: Sequence[float] = DEFILLAMA_STABLECOIN_HTTP_RETRY_DELAYS_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[Iterable[bytes]]:
    """Open one exact filtered stream without reading its body."""
    _asset(asset_id)
    if timeout <= 0 or max_attempts < 1 or len(retry_delays) < max_attempts - 1:
        raise ValueError("invalid DefiLlama transport policy")
    owned = httpx.Client() if client is None else None
    active = owned or client
    context: _Context | None = None
    response: _Response | None = None
    try:
        for attempt in range(1, max_attempts + 1):
            try:
                context = active.stream(
                    "GET",
                    DEFILLAMA_FLAT_ENDPOINT,
                    params={"stablecoin": str(asset_id)},
                    headers=DEFILLAMA_STABLECOIN_REQUEST_HEADERS,
                    timeout=timeout,
                )
                response = context.__enter__()
            except (httpx.TimeoutException, httpx.NetworkError) as error:
                context = None
                if attempt == max_attempts:
                    raise DefiLlamaTransportError(None, attempt, "transient") from error
                sleep(retry_delays[attempt - 1])
                continue
            content_type = getattr(response.headers, "get", lambda _: None)("content-type")
            if (
                response.status_code == 200
                and isinstance(content_type, str)
                and "application/json" in content_type.lower()
            ):
                break
            status = response.status_code
            context.__exit__(None, None, None)
            context = response = None
            if (
                status not in DEFILLAMA_STABLECOIN_HTTP_RETRYABLE_STATUSES
                or attempt == max_attempts
            ):
                reason = "content_type" if status == 200 else "http"
                raise DefiLlamaTransportError(status, attempt, reason)
            sleep(retry_delays[attempt - 1])
        if response is None:
            raise AssertionError("DefiLlama response was not established")
        yield response.iter_bytes()
    finally:
        if context is not None:
            context.__exit__(None, None, None)
        if owned is not None:
            owned.close()


def _prefix_records(chunks: Iterable[bytes], count: int = 2) -> tuple[dict[str, object], ...]:
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    json_decoder = json.JSONDecoder(parse_float=Decimal, parse_constant=Decimal)
    buffer = ""
    position = 0
    started = False
    records: list[dict[str, object]] = []
    for chunk in chunks:
        buffer += decoder.decode(chunk)
        while True:
            while position < len(buffer) and buffer[position].isspace():
                position += 1
            if not started:
                if position >= len(buffer):
                    break
                if buffer[position] != "[":
                    raise ValueError("unexpected DefiLlama JSON top-level type")
                position += 1
                started = True
            while position < len(buffer) and buffer[position] in " \t\r\n,":
                position += 1
            try:
                value, end = json_decoder.raw_decode(buffer, position)
            except json.JSONDecodeError:
                break
            if not isinstance(value, dict):
                raise TypeError("unexpected DefiLlama prefix record schema")
            records.append(value)
            if len(records) == count:
                return tuple(records)
            position = end
        if position:
            buffer, position = buffer[position:], 0
    raise ValueError("insufficient DefiLlama prefix records")


def _validate_prefix_schema(records: tuple[dict[str, object], ...]) -> None:
    for record in records:
        if set(record) != {"date", "totalCirculating", "totalCirculatingUSD"}:
            raise ValueError("DEFILLAMA_STABLECOIN_FILTER_SEMANTICS_CHANGED")
        if not isinstance(record["totalCirculating"], dict) or not isinstance(
            record["totalCirculatingUSD"], dict
        ):
            raise TypeError("DEFILLAMA_STABLECOIN_FILTER_SEMANTICS_CHANGED")
        if (
            "peggedUSD" not in record["totalCirculating"]
            or "peggedUSD" not in record["totalCirculatingUSD"]
        ):
            raise ValueError("DEFILLAMA_STABLECOIN_FILTER_SEMANTICS_CHANGED")


def verify_defillama_stablecoin_filters(
    *, client: DefiLlamaHttpClient, prefix_records: int = 2
) -> tuple[DefiLlamaFilterEvidenceV1, ...]:
    """Verify all three observed filters from small, pre-blind response prefixes."""
    evidence: list[DefiLlamaFilterEvidenceV1] = []
    fingerprints: set[str] = set()
    for asset_id, symbol in DEFILLAMA_STABLECOIN_ASSETS:
        with open_defillama_stablecoin_stream(asset_id, client=client) as chunks:
            records = _prefix_records(chunks, prefix_records)
        _validate_prefix_schema(records)
        item = filter_evidence_from_prefix(asset_id, symbol, records, fingerprints)
        fingerprints.add(item.response_fingerprint)
        evidence.append(item)
    return tuple(evidence)


def fetch_and_parse_bounded_defillama_stablecoin(
    asset_id: int, *, client: DefiLlamaHttpClient, filter_evidence: DefiLlamaFilterEvidenceV1
) -> DefiLlamaStablecoinBoundedResultV1:
    _, symbol = _asset(asset_id)
    with open_defillama_stablecoin_stream(asset_id, client=client) as chunks:
        return parse_bounded_defillama_stablecoin_json(
            chunks, asset_id=asset_id, symbol=symbol, filter_evidence=filter_evidence
        )


def fetch_and_parse_all_defillama_stablecoins(
    *, client: DefiLlamaHttpClient
) -> tuple[DefiLlamaStablecoinBoundedResultV1, ...]:
    evidence = verify_defillama_stablecoin_filters(client=client)
    return tuple(
        fetch_and_parse_bounded_defillama_stablecoin(
            item.asset_id, client=client, filter_evidence=item
        )
        for item in evidence
    )
