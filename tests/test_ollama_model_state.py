import httpx

from app.ai.model_policy import project_local_inventory
from app.platform.ollama.model_state import LocalOllamaModelStateAdapterV1


class _Response:
    def __init__(self, status: int, payload: object) -> None:
        self.status_code = status
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Client:
    def __init__(self, response: _Response | Exception) -> None:
        self.response = response
        self.paths: list[str] = []

    def get(self, path: str):
        self.paths.append(path)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_model_state_is_sorted_local_tags_and_composes_with_policy():
    client = _Client(_Response(200, {"models": [{"name": "qwen3:14b"}, {"name": "qwen3:4b"}]}))
    state = LocalOllamaModelStateAdapterV1(client).read()
    assert state.runtime_ready is True
    assert state.installed_models == ("qwen3:14b", "qwen3:4b")
    inventory = project_local_inventory(
        installed_models=set(state.installed_models),
        ready_models=set(state.installed_models),
        default_model="qwen3:4b",
    )
    assert [(x.installed, x.ready, x.default) for x in inventory] == [
        (True, True, True),
        (True, True, False),
    ]
    assert client.paths == ["/api/tags"]


def test_model_state_preserves_unrelated_models_and_empty_inventory():
    state = LocalOllamaModelStateAdapterV1(
        _Client(_Response(200, {"models": [{"name": "llama3.2:latest"}, {"name": "qwen3:4b"}]}))
    ).read()
    assert state.installed_models == ("llama3.2:latest", "qwen3:4b")
    assert (
        LocalOllamaModelStateAdapterV1(_Client(_Response(200, {"models": []}))).read().runtime_ready
        is True
    )


def test_model_state_fails_closed_for_unavailable_and_malformed_responses():
    assert (
        LocalOllamaModelStateAdapterV1(_Client(httpx.ConnectError("down"))).read().runtime_ready
        is False
    )
    assert LocalOllamaModelStateAdapterV1(_Client(_Response(500, {}))).read().installed_models == ()
    assert (
        LocalOllamaModelStateAdapterV1(_Client(_Response(200, {"models": "bad"})))
        .read()
        .runtime_ready
        is False
    )
