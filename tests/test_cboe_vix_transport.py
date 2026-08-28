from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import httpx
import pytest

from app.research.capital_flow.cboe_vix_transport import (
    CBOE_VIX_HTTP_MAX_ATTEMPTS,
    CBOE_VIX_HTTP_TIMEOUT_SECONDS,
    CBOE_VIX_OFFICIAL_URL,
    CboeVixTransportError,
    fetch_and_parse_bounded_cboe_vix,
    open_official_cboe_vix_csv,
)


class FakeResponse:
    def __init__(self, status: int, chunks: list[bytes] | None = None) -> None:
        self.status_code = status
        self.chunks = chunks or []
        self.closed = False
        self.chunks_read = 0

    def iter_bytes(self) -> Iterator[bytes]:
        for chunk in self.chunks:
            self.chunks_read += 1
            yield chunk


class FakeContext:
    def __init__(self, outcome: FakeResponse | Exception) -> None:
        self.outcome = outcome

    def __enter__(self) -> FakeResponse:
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    def __exit__(self, *_: object) -> None:
        if isinstance(self.outcome, FakeResponse):
            self.outcome.closed = True


class FakeClient:
    def __init__(self, outcomes: list[FakeResponse | Exception]) -> None:
        self.outcomes = iter(outcomes)
        self.calls: list[tuple[str, str, float]] = []

    def stream(self, method: str, url: str, *, timeout: float) -> FakeContext:
        self.calls.append((method, url, timeout))
        return FakeContext(next(self.outcomes))


HEADER = b"DATE,OPEN,HIGH,LOW,CLOSE\n"
IN_RANGE = b"05/29/2026,1,1,1,18.25\n"
CUTOFF = b"06/01/2026,SECRET,SECRET,SECRET,NOT_A_NUMBER\n"


def test_exact_endpoint_timeout_stream_order_and_normal_close() -> None:
    response = FakeResponse(200, [b"DATE,OPEN,HIGH,LOW,CLOSE\r", b"\n", IN_RANGE])
    client = FakeClient([response])
    with open_official_cboe_vix_csv(client=client) as lines:
        assert list(lines) == ["DATE,OPEN,HIGH,LOW,CLOSE\r\n", IN_RANGE.decode()]
        assert not response.closed
    assert client.calls == [("GET", CBOE_VIX_OFFICIAL_URL, CBOE_VIX_HTTP_TIMEOUT_SECONDS)]
    assert response.closed
    assert not hasattr(response, "read")


def test_parser_normal_completion_closes_response() -> None:
    response = FakeResponse(200, [HEADER, IN_RANGE])
    result = fetch_and_parse_bounded_cboe_vix(client=FakeClient([response]))
    assert result.rows[0].close.as_tuple().exponent == -2
    assert response.closed


def test_parser_cutoff_closes_without_draining_future_tail() -> None:
    response = FakeResponse(200, [HEADER, IN_RANGE, CUTOFF, b"09/01/2026,1,1,1,99\n"])
    result = fetch_and_parse_bounded_cboe_vix(client=FakeClient([response]))
    assert result.rows[0].reference_date == date(2026, 5, 29)
    assert result.quality.cutoff_crossing_date == date(2026, 6, 1)
    assert response.chunks_read == 3
    assert response.closed


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_retryable_http_status_then_success(status: int) -> None:
    failed = FakeResponse(status, [b"untrusted body"])
    success = FakeResponse(200, [HEADER, IN_RANGE])
    sleeps: list[float] = []
    with open_official_cboe_vix_csv(
        client=FakeClient([failed, success]), sleep=sleeps.append
    ) as lines:
        assert len(list(lines)) == 2
    assert failed.closed
    assert failed.chunks_read == 0
    assert sleeps == [0.25]


def test_repeated_429_exhausts_at_bounded_attempt_count() -> None:
    responses = [FakeResponse(429) for _ in range(CBOE_VIX_HTTP_MAX_ATTEMPTS)]
    client = FakeClient(responses)
    sleeps: list[float] = []
    with (
        pytest.raises(CboeVixTransportError) as captured,
        open_official_cboe_vix_csv(client=client, sleep=sleeps.append),
    ):
        pass
    assert captured.value.status_code == 429
    assert captured.value.attempts == 3
    assert len(client.calls) == 3
    assert sleeps == [0.25, 0.5]
    assert all(response.closed for response in responses)


@pytest.mark.parametrize(
    "failure",
    [httpx.ReadTimeout("timeout"), httpx.ConnectError("temporary connection")],
)
def test_transient_transport_failure_then_success(failure: Exception) -> None:
    client = FakeClient([failure, FakeResponse(200, [HEADER])])
    sleeps: list[float] = []
    with open_official_cboe_vix_csv(client=client, sleep=sleeps.append) as lines:
        assert list(lines) == [HEADER.decode()]
    assert len(client.calls) == 2
    assert sleeps == [0.25]


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_non_retryable_http_status_is_sanitized(status: int) -> None:
    client = FakeClient([FakeResponse(status, [b"SENSITIVE GIANT BODY"])])
    with (
        pytest.raises(CboeVixTransportError) as captured,
        open_official_cboe_vix_csv(client=client),
    ):
        pass
    assert len(client.calls) == 1
    assert "SENSITIVE" not in str(captured.value)
    assert f"status={status}" in str(captured.value)


def test_decoding_error_is_not_retried_and_response_closes() -> None:
    response = FakeResponse(200, [HEADER, b"\xff"])
    client = FakeClient([response, FakeResponse(200, [HEADER])])
    with (
        pytest.raises(UnicodeDecodeError),
        open_official_cboe_vix_csv(client=client) as lines,
    ):
        list(lines)
    assert len(client.calls) == 1
    assert response.closed


def test_parser_validation_error_propagates_and_closes_without_retry() -> None:
    response = FakeResponse(200, [b"WRONG,HEADER\n", b"ignored\n"])
    client = FakeClient([response])
    with pytest.raises(ValueError, match="schema"):
        fetch_and_parse_bounded_cboe_vix(client=client)
    assert len(client.calls) == 1
    assert response.chunks_read == 1
    assert response.closed
