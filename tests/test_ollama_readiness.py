import httpx

from app.ollama_readiness import OllamaReadinessProbe, OllamaReadinessStateV1


def test_ready():
    p = OllamaReadinessProbe(
        httpx.Client(
            base_url="http://localhost:11434",
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"models": []})),
        )
    )
    assert p.probe().state == OllamaReadinessStateV1.READY


def test_unavailable():
    def fail(r):
        raise httpx.ConnectError("x", request=r)

    assert (
        OllamaReadinessProbe(
            httpx.Client(base_url="http://localhost:11434", transport=httpx.MockTransport(fail))
        )
        .probe()
        .state
        == OllamaReadinessStateV1.NOT_READY
    )


def test_timeout_and_localhost_policy():
    def fail(r):
        raise httpx.TimeoutException("x", request=r)

    assert (
        OllamaReadinessProbe(
            httpx.Client(base_url="http://localhost:11434", transport=httpx.MockTransport(fail))
        )
        .probe()
        .error_category
        == "TIMEOUT"
    )


def test_malformed_response_is_typed_error():
    probe = OllamaReadinessProbe(
        httpx.Client(
            base_url="http://localhost:11434",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x")),
        )
    )
    assert probe.probe().state == OllamaReadinessStateV1.ERROR
