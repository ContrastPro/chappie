"""Offline contracts for the loopback-only Control Plane transport."""

from __future__ import annotations

import http.client
import json
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.control_plane import (
    CONTROL_PLANE_IDENTITY,
    ChappieControlPlaneV1,
    ProjectQAControlPlaneResponseV1,
    ProjectQASourceV1,
    ProjectQAStateV1,
)
from app.control_plane.transport import (
    CONTROL_PLANE_TRANSPORT_DIGEST,
    CONTROL_PLANE_TRANSPORT_IDENTITY,
    LOOPBACK_HOST,
    ChappieControlPlaneServerV1,
    ControlPlaneTransportErrorV1,
)
from app.platform.bootstrap import (
    ChappieLocalConfigV1,
    ChappiePaths,
    ChappieStatusV1,
    ComponentStateV1,
    ComponentStatusV1,
    HardwareProfileV1,
    InstallationStateV1,
)


@dataclass(frozen=True)
class _Response:
    name: str
    error: object | None = None

    def payload(self) -> dict[str, str]:
        return {"response": self.name}


class _FakeControlPlane:
    def get_status(self) -> _Response:
        return _Response("status")

    def get_diagnostics(self) -> _Response:
        return _Response("diagnostics")

    def get_ai_status(self) -> _Response:
        return _Response("ai-status")

    def get_capabilities(self) -> _Response:
        return _Response("capabilities")

    def ask_project(
        self, question: str, _workspace_context: object | None = None
    ) -> ProjectQAControlPlaneResponseV1:
        return ProjectQAControlPlaneResponseV1(
            ProjectQAStateV1.OK,
            question,
            "RDE owns evidence authority.",
            ("knowledge.record.statement",),
            (ProjectQASourceV1("record", "Evidence authority", "VERSIONED_CONTRACT", "FROZEN"),),
            ("No execution authority is granted.",),
            "a" * 64,
            "mock",
            "mock-model",
            "VALID",
        )


def _request(server: ChappieControlPlaneServerV1, method: str, path: str, body: str | None = None):
    connection = http.client.HTTPConnection(LOOPBACK_HOST, server.port, timeout=2)
    connection.request(method, path, body=body, headers={"Content-Type": "application/json"})
    response = connection.getresponse()
    body = json.loads(response.read())
    headers = dict(response.getheaders())
    connection.close()
    return response.status, headers, body


@pytest.fixture
def server():
    value = ChappieControlPlaneServerV1(_FakeControlPlane(), port=0)
    value.start()
    thread = threading.Thread(target=value.serve_forever, daemon=True)
    thread.start()
    yield value
    value.shutdown()
    thread.join(timeout=2)
    assert not thread.is_alive()


@pytest.mark.parametrize(
    ("path", "expected"),
    (
        ("/v1/status", "status"),
        ("/v1/diagnostics", "diagnostics"),
        ("/v1/ai/status", "ai-status"),
        ("/v1/capabilities", "capabilities"),
    ),
)
def test_routes_delegate_only_to_control_plane(server, path: str, expected: str) -> None:
    status, headers, body = _request(server, "GET", path)
    assert status == 200
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert "Access-Control-Allow-Origin" not in headers
    assert body["status"] == "OK"
    assert len(body["request_id"]) == 32
    assert body["data"] == {"response": expected}
    assert body["transport_contract"] == CONTROL_PLANE_TRANSPORT_IDENTITY
    assert body["transport_contract_digest"] == CONTROL_PLANE_TRANSPORT_DIGEST


def test_health_is_minimal_version_handshake_without_control_plane_call(server) -> None:
    status, _headers, body = _request(server, "GET", "/v1/health")
    assert status == 200
    assert body["data"] == {"status": "OK"}
    assert body["control_plane_contract"] == CONTROL_PLANE_IDENTITY
    assert len(body["control_plane_contract_digest"]) == 64


def test_unknown_and_mutating_routes_return_safe_json_errors(server) -> None:
    status, headers, body = _request(server, "GET", "/v1/unknown")
    assert status == 404
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert body["error"] == {"code": "NOT_FOUND", "message": "Control Plane route not found"}
    status, _headers, body = _request(server, "POST", "/v1/status")
    assert status == 405
    assert body["error"] == {
        "code": "METHOD_NOT_ALLOWED",
        "message": "Control Plane method is not supported",
    }


def test_project_qa_post_is_strict_and_serializes_grounded_response(server) -> None:
    status, _headers, body = _request(
        server, "POST", "/v1/project/qa", json.dumps({"question": "Who owns evidence?"})
    )
    assert status == 200
    assert body["data"]["answer"] == "RDE owns evidence authority."
    assert body["data"]["supporting_records"] == [
        {
            "record_id": "record",
            "title": "Evidence authority",
            "authority": "VERSIONED_CONTRACT",
            "status": "FROZEN",
        }
    ]
    status, _headers, body = _request(server, "POST", "/v1/project/qa", "{")
    assert status == 400
    assert body["error"]["code"] == "INVALID_REQUEST"
    status, _headers, body = _request(
        server,
        "POST",
        "/v1/project/qa",
        json.dumps({"question": "valid", "unexpected": True}),
    )
    assert status == 400
    assert body["error"]["code"] == "INVALID_REQUEST"


def test_safe_control_plane_failure_never_leaks_exception(server) -> None:
    class BrokenPlane(_FakeControlPlane):
        def get_status(self) -> _Response:
            raise RuntimeError("secret internal detail")

    server._control_plane = BrokenPlane()
    status, _headers, body = _request(server, "GET", "/v1/status")
    assert status == 500
    assert body["error"] == {
        "code": "INTERNAL_ERROR",
        "message": "Control Plane request could not be completed",
    }
    assert "secret" not in json.dumps(body)


def test_loopback_policy_rejects_external_interfaces() -> None:
    with pytest.raises(ValueError, match="127.0.0.1"):
        ChappieControlPlaneServerV1(_FakeControlPlane(), host="0.0.0.0")
    with pytest.raises(ValueError, match="port"):
        ChappieControlPlaneServerV1(_FakeControlPlane(), port=-1)


def test_occupied_port_is_a_typed_startup_failure(server) -> None:
    contender = ChappieControlPlaneServerV1(_FakeControlPlane(), port=server.port)
    with pytest.raises(ControlPlaneTransportErrorV1) as error:
        contender.start()
    assert error.value.code == "PORT_IN_USE"
    assert error.value.message == "Control Plane transport could not bind"


def _paths(tmp_path: Path) -> ChappiePaths:
    root = tmp_path / "project"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname = 'fixture'\n", encoding="utf-8")
    return ChappiePaths.resolve(root)


def _ready_status() -> ChappieStatusV1:
    components = tuple(
        ComponentStatusV1(name, ComponentStateV1.READY, True, None, None, "fixture")
        for name in ("KNOWLEDGE", "AI_PROVIDER", "AI_MODEL", "RESEARCH_STORAGE")
    )
    return ChappieStatusV1(
        "chappie_installation_state/1.0.0",
        "a" * 64,
        InstallationStateV1.READY,
        "Windows",
        "AMD64",
        HardwareProfileV1("Windows", "fixture", "AMD64", None, 16, 32 * 1024**3),
        components,
        ("BALANCED",),
        (),
        (),
    )


def test_real_control_plane_serializes_fake_injected_dependencies(tmp_path: Path) -> None:
    plane = ChappieControlPlaneV1(
        _paths(tmp_path),
        status_collector=lambda _paths: _ready_status(),
        config_loader=lambda _paths: ChappieLocalConfigV1(
            provider="ollama", model="qwen3:8b", enabled_capabilities=("BALANCED",)
        ),
    )
    server = ChappieControlPlaneServerV1(plane, port=0)
    server.start()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _headers, body = _request(server, "GET", "/v1/ai/status")
    finally:
        server.shutdown()
        thread.join(timeout=2)
    assert status == 200
    assert body["data"]["provider"] == "ollama"
    assert body["data"]["model"] == "qwen3:8b"
    assert body["data"]["state"] == "OK"


def test_cli_serve_uses_loopback_server_factory_and_keeps_json_machine_only(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from app import cli

    calls: list[object] = []

    class FakeServer:
        def start(self) -> None:
            calls.append("start")

        def startup_payload(self) -> dict[str, str]:
            return {
                "endpoint": "http://127.0.0.1:8765",
                "transport_contract": CONTROL_PLANE_TRANSPORT_IDENTITY,
            }

        def serve_forever(self) -> None:
            calls.append("serve")

        def shutdown(self) -> None:
            calls.append("shutdown")

    def factory(_paths: object, *, port: int) -> FakeServer:
        calls.append(port)
        return FakeServer()

    monkeypatch.setattr(cli, "_control_plane_server", factory)
    assert cli.main(["serve", "--json"], paths=_paths(tmp_path)) == 0
    assert json.loads(capsys.readouterr().out) == {
        "endpoint": "http://127.0.0.1:8765",
        "transport_contract": CONTROL_PLANE_TRANSPORT_IDENTITY,
    }
    assert calls == [8765, "start", "serve", "shutdown"]
