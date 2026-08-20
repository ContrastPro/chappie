"""Loopback-only HTTP adapter for the read-only Chappie Control Plane."""

from __future__ import annotations

import errno
import json
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from app.ai.schemas import canonical_json, digest
from app.control_plane import CONTROL_PLANE_CONTRACT_DIGEST, CONTROL_PLANE_IDENTITY

CONTROL_PLANE_TRANSPORT_IDENTITY = "chappie_control_plane_transport/1.2.0"
LOOPBACK_HOST = "127.0.0.1"
DEFAULT_CONTROL_PLANE_PORT = 8765
CONTROL_PLANE_TRANSPORT_DIGEST = digest(
    {
        "identity": CONTROL_PLANE_TRANSPORT_IDENTITY,
        "host_policy": LOOPBACK_HOST,
        "default_port": DEFAULT_CONTROL_PLANE_PORT,
        "methods": {
            "GET": [
                "/v1/health",
                "/v1/status",
                "/v1/diagnostics",
                "/v1/ai/status",
                "/v1/capabilities",
            ],
            "POST": ["/v1/project/qa"],
        },
        "cors": "omitted",
        "authority": "read_only_transport_adapter",
    }
)


class ControlPlaneTransportErrorV1(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _envelope(
    *,
    request_id: str,
    data: object | None = None,
    error: ControlPlaneTransportErrorV1 | None = None,
) -> dict[str, object]:
    return {
        "transport_contract": CONTROL_PLANE_TRANSPORT_IDENTITY,
        "transport_contract_digest": CONTROL_PLANE_TRANSPORT_DIGEST,
        "control_plane_contract": CONTROL_PLANE_IDENTITY,
        "control_plane_contract_digest": CONTROL_PLANE_CONTRACT_DIGEST,
        "request_id": request_id,
        "status": "ERROR" if error else "OK",
        "data": data if error is None else None,
        "error": {"code": error.code, "message": error.message} if error else None,
    }


class ChappieControlPlaneServerV1:
    """Owns a loopback server and delegates all application work to its service."""

    def __init__(
        self,
        control_plane: Any,
        *,
        host: str = LOOPBACK_HOST,
        port: int = DEFAULT_CONTROL_PLANE_PORT,
    ) -> None:
        if host != LOOPBACK_HOST:
            raise ValueError("Control Plane transport must bind only to 127.0.0.1")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("Control Plane port must be between 0 and 65535")
        self._control_plane = control_plane
        self.host = host
        self.port = port
        self._httpd: ThreadingHTTPServer | None = None

    @property
    def endpoint(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        if self._httpd is not None:
            return
        transport = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                transport._handle_get(self)

            def do_POST(self) -> None:
                transport._handle_post(self)

            def do_PUT(self) -> None:
                transport._method_not_allowed(self)

            def do_PATCH(self) -> None:
                transport._method_not_allowed(self)

            def do_DELETE(self) -> None:
                transport._method_not_allowed(self)

            def log_message(self, _format: str, *_args: object) -> None:
                """Transport logs no request details by default."""

        try:
            self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
            self.port = int(self._httpd.server_address[1])
        except OSError as error:
            code = "PORT_IN_USE" if error.errno == errno.EADDRINUSE else "BIND_FAILED"
            raise ControlPlaneTransportErrorV1(
                code, "Control Plane transport could not bind"
            ) from None

    def serve_forever(self) -> None:
        if self._httpd is None:
            raise ControlPlaneTransportErrorV1(
                "INTERNAL_ERROR", "Control Plane server is not started"
            )
        self._httpd.serve_forever()

    def shutdown(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def startup_payload(self) -> dict[str, object]:
        return {
            "transport_contract": CONTROL_PLANE_TRANSPORT_IDENTITY,
            "transport_contract_digest": CONTROL_PLANE_TRANSPORT_DIGEST,
            "control_plane_contract": CONTROL_PLANE_IDENTITY,
            "control_plane_contract_digest": CONTROL_PLANE_CONTRACT_DIGEST,
            "endpoint": self.endpoint,
        }

    def _handle_get(self, handler: BaseHTTPRequestHandler) -> None:
        request_id = uuid.uuid4().hex
        routes = {
            "/v1/status": "get_status",
            "/v1/diagnostics": "get_diagnostics",
            "/v1/ai/status": "get_ai_status",
            "/v1/capabilities": "get_capabilities",
        }
        if handler.path == "/v1/health":
            self._write(handler, 200, _envelope(request_id=request_id, data={"status": "OK"}))
            return
        method = routes.get(handler.path)
        if method is None:
            self._write(
                handler,
                404,
                _envelope(
                    request_id=request_id,
                    error=ControlPlaneTransportErrorV1(
                        "NOT_FOUND", "Control Plane route not found"
                    ),
                ),
            )
            return
        try:
            response = getattr(self._control_plane, method)()
            error = getattr(response, "error", None)
            if error is not None:
                self._write(
                    handler,
                    503,
                    _envelope(
                        request_id=request_id,
                        error=ControlPlaneTransportErrorV1(
                            "CONTROL_PLANE_UNAVAILABLE", "Control Plane dependency is unavailable"
                        ),
                    ),
                )
                return
            self._write(handler, 200, _envelope(request_id=request_id, data=response.payload()))
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            self._write(
                handler,
                500,
                _envelope(
                    request_id=request_id,
                    error=ControlPlaneTransportErrorV1(
                        "INTERNAL_ERROR", "Control Plane request could not be completed"
                    ),
                ),
            )

    def _method_not_allowed(self, handler: BaseHTTPRequestHandler) -> None:
        self._write(
            handler,
            405,
            _envelope(
                request_id=uuid.uuid4().hex,
                error=ControlPlaneTransportErrorV1(
                    "METHOD_NOT_ALLOWED", "Control Plane method is not supported"
                ),
            ),
        )

    def _handle_post(self, handler: BaseHTTPRequestHandler) -> None:
        request_id = uuid.uuid4().hex
        if handler.path != "/v1/project/qa":
            self._method_not_allowed(handler)
            return
        try:
            content_length = int(handler.headers.get("Content-Length", ""))
            if content_length < 1 or content_length > 2048:
                raise ValueError
            value = json.loads(handler.rfile.read(content_length))
            if (
                not isinstance(value, dict)
                or set(value).difference({"question", "workspace_context"})
                or "question" not in value
            ):
                raise ValueError
            question = value["question"]
            if not isinstance(question, str):
                raise TypeError
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            self._write(
                handler,
                400,
                _envelope(
                    request_id=request_id,
                    error=ControlPlaneTransportErrorV1(
                        "INVALID_REQUEST", "Project QA request must contain one bounded question"
                    ),
                ),
            )
            return
        try:
            response = self._control_plane.ask_project(question, value.get("workspace_context"))
            payload = response.payload()
            if response.error is not None:
                status = 400 if response.state.value == "VALIDATION_ERROR" else 503
                self._write(
                    handler,
                    status,
                    _envelope(
                        request_id=request_id,
                        error=ControlPlaneTransportErrorV1(
                            response.error.reason_code, "Project QA is unavailable"
                        ),
                    ),
                )
                return
            self._write(handler, 200, _envelope(request_id=request_id, data=payload))
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            self._write(
                handler,
                500,
                _envelope(
                    request_id=request_id,
                    error=ControlPlaneTransportErrorV1(
                        "INTERNAL_ERROR", "Project QA request could not be completed"
                    ),
                ),
            )

    @staticmethod
    def _write(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, object]) -> None:
        body = canonical_json(payload).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
