from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest

from app.research.capital_flow.defillama_stablecoin_transport import (
    DEFILLAMA_FLAT_ENDPOINT,
    DEFILLAMA_STABLECOIN_HTTP_TIMEOUT_SECONDS,
    DefiLlamaTransportError,
    fetch_and_parse_bounded_defillama_stablecoin,
    open_defillama_stablecoin_stream,
    verify_defillama_stablecoin_filters,
)


class Response:
    def __init__(
        self,
        status: int = 200,
        chunks: list[bytes] | None = None,
        content_type: str = "application/json",
    ) -> None:
        self.status_code, self.chunks, self.headers = (
            status,
            chunks or [],
            {"content-type": content_type},
        )
        self.closed = False
        self.read = 0

    def iter_bytes(self) -> Iterator[bytes]:
        for chunk in self.chunks:
            self.read += 1
            yield chunk


class Context:
    def __init__(self, outcome: Response | Exception) -> None:
        self.outcome = outcome

    def __enter__(self) -> Response:
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    def __exit__(self, *_: object) -> None:
        if isinstance(self.outcome, Response):
            self.outcome.closed = True


class Client:
    def __init__(self, outcomes: list[Response | Exception]) -> None:
        self.outcomes, self.calls = iter(outcomes), []

    def stream(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str],
        headers: dict[str, str],
        timeout: float,
    ) -> Context:
        self.calls.append((method, url, params, headers, timeout))
        return Context(next(self.outcomes))


def record(timestamp: str, value: str) -> bytes:
    return (
        '{"date":"'
        + timestamp
        + '","totalCirculating":{"peggedUSD":'
        + value
        + '},"totalCirculatingUSD":{"peggedUSD":'
        + value
        + "}}"
    ).encode()


def prefix(asset: int) -> list[bytes]:
    values = {1: ("1511913600", "110105"), 2: ("1536624000", "2"), 5: ("1574121600", "1835204")}[
        asset
    ]
    return [b"[", record(*values), b",", record(str(int(values[0]) + 86400), values[1]), b",TAIL"]


def test_exact_endpoint_policy_timeout_and_normal_close() -> None:
    response = Response(chunks=[b"[]"])
    client = Client([response])
    with open_defillama_stablecoin_stream(1, client=client) as chunks:
        assert list(chunks) == [b"[]"]
    method, url, params, headers, timeout = client.calls[0]
    assert (method, url, params, timeout) == (
        "GET",
        DEFILLAMA_FLAT_ENDPOINT,
        {"stablecoin": "1"},
        DEFILLAMA_STABLECOIN_HTTP_TIMEOUT_SECONDS,
    )
    assert headers == {"Accept": "application/json", "Accept-Encoding": "identity"}
    assert response.closed


@pytest.mark.parametrize("status", [429, 500, 503])
def test_retryable_status_then_success(status: int) -> None:
    first, second = Response(status), Response(chunks=[b"[]"])
    sleeps: list[float] = []
    client = Client([first, second])
    with open_defillama_stablecoin_stream(2, client=client, sleep=sleeps.append) as chunks:
        list(chunks)
    assert first.closed and sleeps == [0.25] and len(client.calls) == 2


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("timeout"), httpx.ConnectError("temporary")])
def test_transient_then_success(failure: Exception) -> None:
    client = Client([failure, Response(chunks=[b"[]"])])
    sleeps: list[float] = []
    with open_defillama_stablecoin_stream(5, client=client, sleep=sleeps.append) as chunks:
        list(chunks)
    assert len(client.calls) == 2 and sleeps == [0.25]


@pytest.mark.parametrize("status", [400, 403, 404])
def test_nonretryable_status(status: int) -> None:
    client = Client([Response(status)])
    with (
        pytest.raises(DefiLlamaTransportError) as error,
        open_defillama_stablecoin_stream(1, client=client),
    ):
        pass
    assert error.value.status_code == status and len(client.calls) == 1


def test_fourth_asset_and_retry_exhaustion_reject() -> None:
    with (
        pytest.raises(ValueError, match="unexpected"),
        open_defillama_stablecoin_stream(6, client=Client([])),
    ):
        pass
    responses = [Response(429) for _ in range(3)]
    sleeps: list[float] = []
    with (
        pytest.raises(DefiLlamaTransportError) as error,
        open_defillama_stablecoin_stream(1, client=Client(responses), sleep=sleeps.append),
    ):
        pass
    assert error.value.attempts == 3 and sleeps == [0.25, 0.5]


def test_filter_capability_distinguishes_three_assets_without_tail_drain() -> None:
    responses = [Response(chunks=prefix(asset)) for asset in (1, 2, 5)]
    client = Client(responses)
    evidence = verify_defillama_stablecoin_filters(client=client)
    assert [item.asset_id for item in evidence] == [1, 2, 5]
    assert len({item.response_fingerprint for item in evidence}) == 3
    assert all(response.closed and response.read == 4 for response in responses)


def test_ignored_filter_schema_and_utf8_fail_closed() -> None:
    same = [Response(chunks=prefix(1)) for _ in range(3)]
    with pytest.raises(ValueError, match="FILTER_SEMANTICS_CHANGED"):
        verify_defillama_stablecoin_filters(client=Client(same))
    with pytest.raises(UnicodeDecodeError):
        verify_defillama_stablecoin_filters(client=Client([Response(chunks=[b"[\xff"])]))
    with (
        pytest.raises(DefiLlamaTransportError, match="content_type"),
        open_defillama_stablecoin_stream(1, client=Client([Response(content_type="text/html")])),
    ):
        pass


def test_parser_cutoff_closes_response_without_future_tail() -> None:
    response = Response(
        chunks=[
            b"[",
            record("1693526400", "10"),
            b",",
            b'{"date":"1780358400","totalCirculating":{"peggedUSD":99}',
            b",SHOULD_NOT_BE_READ",
        ]
    )
    evidence = verify_defillama_stablecoin_filters(
        client=Client(
            [Response(chunks=prefix(1)), Response(chunks=prefix(2)), Response(chunks=prefix(5))]
        )
    )[0]
    result = fetch_and_parse_bounded_defillama_stablecoin(
        1, client=Client([response]), filter_evidence=evidence
    )
    assert result.quality.objects_seen_after_cutoff == 1 and response.closed and response.read == 4


def test_parser_error_closes_response() -> None:
    evidence = verify_defillama_stablecoin_filters(
        client=Client(
            [Response(chunks=prefix(1)), Response(chunks=prefix(2)), Response(chunks=prefix(5))]
        )
    )[0]
    response = Response(chunks=[b'[{"wrong":1}]'])
    with pytest.raises(ValueError, match="schema"):
        fetch_and_parse_bounded_defillama_stablecoin(
            1, client=Client([response]), filter_evidence=evidence
        )
    assert response.closed
