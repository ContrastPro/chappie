from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.research.capital_flow.fred_alfred_v2_acquisition import (
    FredAlfredTransportError,
    build_fred_alfred_v2_request_plan,
    execute_request_part,
)
from app.research.capital_flow.fred_alfred_v2_acquisition.contracts import (
    FRED_OFFICIAL_API_HOST,
    FRED_SERIES_OBSERVATIONS_ENDPOINT,
    FRED_SERIES_VINTAGEDATES_ENDPOINT,
)
from app.research.capital_flow.fred_alfred_v2_acquisition.transport import (
    FRED_HTTP_MAX_ATTEMPTS,
    FRED_HTTP_RETRY_DELAYS_SECONDS,
    FRED_HTTP_TIMEOUT_SECONDS,
)

SECRET = "transport-test-secret"


@pytest.fixture(autouse=True)
def _credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRED_API_KEY", SECRET)


def _part(series: str = "NASDAQCOM", limit: int = 2):
    part = build_fred_alfred_v2_request_plan(series).initial_parts[0]
    parameters = tuple(
        (key, str(limit) if key == "limit" else value) for key, value in part.parameters
    )
    return replace(part, parameters=parameters, pagination=replace(part.pagination, limit=limit))


def _payload(part: Any, records: list[object] | None = None, **updates: object):
    key = "observations" if part.endpoint == FRED_SERIES_OBSERVATIONS_ENDPOINT else "vintage_dates"
    default_records = (
        [{"date": "2024-01-02", "value": "1"}] if key == "observations" else ["2024-01-02"]
    )
    payload = {"count": "1", "offset": "0", "limit": "2", key: default_records}
    payload.update(updates)
    if records is not None:
        payload[key] = records
    return payload


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _execute(payload: object, part=None, **response_kwargs: object):
    active_part = part or _part()
    with _client(lambda _: httpx.Response(200, json=payload, **response_kwargs)) as client:
        return execute_request_part(active_part, client, sleeper=lambda _: None)


def test_missing_key_fails_before_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FRED_API_KEY")
    calls = 0

    def handler(_: httpx.Request):
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    with _client(handler) as client, pytest.raises(RuntimeError, match="FRED_API_KEY_REQUIRED"):
        execute_request_part(_part(), client)
    assert calls == 0


def test_plan_building_does_not_need_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FRED_API_KEY")
    assert build_fred_alfred_v2_request_plan("NASDAQCOM").series_id == "NASDAQCOM"


def test_request_boundary_host_endpoint_key_timeout_and_no_redirect() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request):
        seen.update(url=request.url, timeout=request.extensions["timeout"])
        return httpx.Response(200, json=_payload(_part()))

    with _client(handler) as client:
        page = execute_request_part(_part(), client)
    url = seen["url"]
    assert isinstance(url, httpx.URL)
    assert f"{url.scheme}://{url.host}" == FRED_OFFICIAL_API_HOST
    assert url.path == f"/{FRED_SERIES_OBSERVATIONS_ENDPOINT}"
    assert url.params["api_key"] == SECRET
    assert set(seen["timeout"].values()) == {FRED_HTTP_TIMEOUT_SECONDS}
    assert SECRET not in repr(page)


def test_vintage_dates_endpoint_and_page() -> None:
    part = _part("DTWEXBGS")
    page = _execute(_payload(part), part)
    assert part.endpoint == FRED_SERIES_VINTAGEDATES_ENDPOINT
    assert page.records == ("2024-01-02",)


def test_unexpected_endpoint_fails_closed_before_network() -> None:
    part = _part()
    object.__setattr__(part, "endpoint", "fred/other")
    calls = 0

    def handler(_: httpx.Request):
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    with _client(handler) as client, pytest.raises(FredAlfredTransportError) as caught:
        execute_request_part(part, client)
    assert caught.value.category == "PERMANENT_PROVIDER_FAILURE"
    assert calls == 0


def test_observation_page_metadata_records_identity_and_immutability() -> None:
    part = _part()
    records = [{"date": "2024-01-03", "value": "2"}, {"date": "2024-01-02", "value": "1"}]
    first = _execute(_payload(part, records, count=2, offset=0, limit=2), part)
    second = _execute(_payload(part, records, count="2", offset="0", limit="2"), part)
    assert (first.provider_count, first.provider_offset, first.provider_limit) == (2, 0, 2)
    assert first.returned_record_count == 2
    assert [row["date"] for row in first.records] == ["2024-01-03", "2024-01-02"]
    assert first.identity == second.identity
    with pytest.raises(TypeError):
        first.records[0]["value"] = "changed"
    with pytest.raises(FrozenInstanceError):
        first.provider_count = 3


def test_secret_change_does_not_affect_page_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    part = _part()
    first = _execute(_payload(part), part)
    monkeypatch.setenv("FRED_API_KEY", "different-secret")
    second = _execute(_payload(part), part)
    assert first.identity == second.identity
    assert SECRET not in first.identity + repr(first)


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        ({"count": -1}, "invalid_count"),
        ({"count": "x"}, "invalid_count"),
        ({"offset": -1}, "invalid_offset"),
        ({"offset": 1}, "offset_mismatch"),
        ({"limit": 0}, "limit_mismatch"),
        ({"limit": 3}, "limit_mismatch"),
        ({"count": True}, "invalid_count"),
    ],
)
def test_invalid_pagination_metadata(updates: dict[str, object], reason: str) -> None:
    part = _part()
    with pytest.raises(FredAlfredTransportError) as caught:
        _execute(_payload(part, **updates), part)
    assert (caught.value.category, caught.value.reason) == ("MALFORMED_PROVIDER_RESPONSE", reason)


def test_records_exceed_limit() -> None:
    part = _part(limit=1)
    with pytest.raises(FredAlfredTransportError, match="records_exceed_limit"):
        _execute(_payload(part, [{}, {}], count=2, limit=1), part)


def test_premature_empty_page() -> None:
    part = _part()
    with pytest.raises(FredAlfredTransportError, match="premature_empty"):
        _execute(_payload(part, [], count=1), part)


@pytest.mark.parametrize(
    ("part", "payload", "reason"),
    [
        (_part(), {"count": 1, "offset": 0, "limit": 2}, "invalid_observations"),
        (
            _part(),
            {"count": 1, "offset": 0, "limit": 2, "observations": {}},
            "invalid_observations",
        ),
        (
            _part(),
            {"count": 1, "offset": 0, "limit": 2, "observations": ["x"]},
            "invalid_observation",
        ),
        (_part("DTWEXBGS"), {"count": 1, "offset": 0, "limit": 2}, "invalid_vintage_dates"),
        (
            _part("DTWEXBGS"),
            {"count": 1, "offset": 0, "limit": 2, "vintage_dates": {}},
            "invalid_vintage_dates",
        ),
        (
            _part("DTWEXBGS"),
            {"count": 1, "offset": 0, "limit": 2, "vintage_dates": [1]},
            "invalid_vintage_date",
        ),
    ],
)
def test_invalid_endpoint_envelopes(part: Any, payload: object, reason: str) -> None:
    with pytest.raises(FredAlfredTransportError) as caught:
        _execute(payload, part)
    assert caught.value.reason == reason


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (
            httpx.Response(200, content=b"not-json", headers={"content-type": "application/json"}),
            "json",
        ),
        (
            httpx.Response(200, text="<html>x</html>", headers={"content-type": "text/html"}),
            "content_type",
        ),
        (httpx.Response(200, json=[]), "json_object"),
        (
            httpx.Response(
                200,
                content=b'{"count":NaN,"offset":0,"limit":2,"observations":[]}',
                headers={"content-type": "application/json"},
            ),
            "json",
        ),
    ],
)
def test_malformed_success_response(response: httpx.Response, reason: str) -> None:
    with _client(lambda _: response) as client, pytest.raises(FredAlfredTransportError) as caught:
        execute_request_part(_part(), client)
    assert caught.value.reason == reason


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_retryable_status_then_success(status: int) -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(_: httpx.Request):
        nonlocal calls
        calls += 1
        return httpx.Response(status) if calls == 1 else httpx.Response(200, json=_payload(_part()))

    with _client(handler) as client:
        execute_request_part(_part(), client, sleeper=sleeps.append)
    assert calls == 2
    assert sleeps == [FRED_HTTP_RETRY_DELAYS_SECONDS[0]]


@pytest.mark.parametrize("exception_type", [httpx.ReadTimeout, httpx.ConnectError])
def test_transient_exception_then_success(exception_type: type[httpx.RequestError]) -> None:
    calls = 0

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise exception_type("safe", request=request)
        return httpx.Response(200, json=_payload(_part()))

    with _client(handler) as client:
        execute_request_part(_part(), client, sleeper=lambda _: None)
    assert calls == 2


def test_retry_exhaustion_is_bounded_and_sanitized() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(_: httpx.Request):
        nonlocal calls
        calls += 1
        return httpx.Response(503, text=SECRET)

    with _client(handler) as client, pytest.raises(FredAlfredTransportError) as caught:
        execute_request_part(_part(), client, sleeper=sleeps.append)
    assert calls == FRED_HTTP_MAX_ATTEMPTS
    assert sleeps == list(FRED_HTTP_RETRY_DELAYS_SECONDS)
    assert caught.value.category == "TRANSIENT_PROVIDER_FAILURE"
    assert SECRET not in str(caught.value) + repr(caught.value)


@pytest.mark.parametrize(
    ("status", "category"),
    [
        (400, "PERMANENT_PROVIDER_FAILURE"),
        (401, "AUTHENTICATION_FAILURE"),
        (403, "AUTHORIZATION_FAILURE"),
        (404, "PERMANENT_PROVIDER_FAILURE"),
        (302, "PERMANENT_PROVIDER_FAILURE"),
    ],
)
def test_non_retryable_status_classification(status: int, category: str) -> None:
    calls = 0

    def handler(_: httpx.Request):
        nonlocal calls
        calls += 1
        return httpx.Response(status, headers={"location": "https://unrelated.example/"})

    with _client(handler) as client, pytest.raises(FredAlfredTransportError) as caught:
        execute_request_part(_part(), client)
    assert calls == 1
    assert caught.value.category == category


def test_malformed_page_is_not_retried() -> None:
    calls = 0

    def handler(_: httpx.Request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    with _client(handler) as client, pytest.raises(FredAlfredTransportError):
        execute_request_part(_part(), client)
    assert calls == 1


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"content-type": "application/json; charset=utf-8"},
        {"content-type": "application/problem+json"},
    ],
)
def test_json_compatible_content_types(headers: dict[str, str]) -> None:
    part = _part()
    with _client(lambda _: httpx.Response(200, json=_payload(part), headers=headers)) as client:
        assert execute_request_part(part, client).provider_count == 1


def test_empty_page_at_provider_end_is_valid() -> None:
    part = _part()
    page = _execute(_payload(part, [], count=0), part)
    assert page.returned_record_count == 0


def test_credential_named_provider_record_is_rejected() -> None:
    part = _part()
    with pytest.raises(FredAlfredTransportError, match="unsafe_response"):
        _execute(_payload(part, [{"api_key": SECRET}]), part)


def test_network_retry_exhaustion_is_transient() -> None:
    def handler(request: httpx.Request):
        raise httpx.ConnectError("safe", request=request)

    with _client(handler) as client, pytest.raises(FredAlfredTransportError) as caught:
        execute_request_part(_part(), client, sleeper=lambda _: None)
    assert caught.value.category == "TRANSIENT_PROVIDER_FAILURE"
    assert caught.value.attempts == 3


def test_rate_limit_exhaustion_classification() -> None:
    with (
        _client(lambda _: httpx.Response(429)) as client,
        pytest.raises(FredAlfredTransportError) as caught,
    ):
        execute_request_part(_part(), client, sleeper=lambda _: None)
    assert caught.value.category == "RATE_LIMITED"


def test_transport_source_has_no_persistence_or_research_dependencies() -> None:
    source = __import__(
        "app.research.capital_flow.fred_alfred_v2_acquisition.transport", fromlist=["x"]
    ).__file__
    text = Path(source).read_text(encoding="utf-8")
    assert "open(" not in text
    assert all(
        term not in text for term in ("raw_evidence", "feature", "blind_oos", "research_split")
    )
