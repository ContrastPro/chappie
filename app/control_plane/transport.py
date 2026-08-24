"""Loopback-only HTTP adapter for the read-only Chappie Control Plane."""

from __future__ import annotations

import errno
import json
import logging
import socket
import uuid
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from typing import Any

from app.ai.schemas import canonical_json, digest
from app.control_plane.service import CONTROL_PLANE_CONTRACT_DIGEST, CONTROL_PLANE_IDENTITY

LOGGER = logging.getLogger(__name__)

CONTROL_PLANE_TRANSPORT_IDENTITY = "chappie_control_plane_transport/1.2.0"
LOOPBACK_HOST = "127.0.0.1"
DEFAULT_CONTROL_PLANE_PORT = 8765
# This bounds one complete JSON request, not the Project QA question itself.  It
# covers the 12k-character question and the independently bounded optional
# workspace context without making either value silently shorter at transport.
PROJECT_QA_MAX_REQUEST_BODY_BYTES = 128 * 1024
MAX_RECENT_DEVELOPMENT_DIAGNOSTICS = 32
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


class _RecentDevelopmentDiagnosticsV1:
    """Bounded, in-memory operator projection for completed Development requests.

    This deliberately retains structural lifecycle metadata only.  It is not an
    evidence store and never retains a request, model output, source content, or
    provider payload.
    """

    def __init__(self, capacity: int = MAX_RECENT_DEVELOPMENT_DIAGNOSTICS) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError("development diagnostics capacity must be positive")
        self._capacity = capacity
        self._items: OrderedDict[str, dict[str, object]] = OrderedDict()
        self._lock = Lock()

    def record(self, request_id: str, diagnostic: dict[str, object]) -> dict[str, object]:
        if not _is_request_id(request_id):
            raise ValueError("development diagnostic request id is invalid")
        item = _copy_safe_diagnostic({**diagnostic, "request_id": request_id})
        with self._lock:
            self._items[request_id] = item
            self._items.move_to_end(request_id)
            while len(self._items) > self._capacity:
                self._items.popitem(last=False)
        return item

    def get(self, request_id: str) -> dict[str, object] | None:
        if not _is_request_id(request_id):
            return None
        with self._lock:
            item = self._items.get(request_id)
        return _copy_safe_diagnostic(item) if item is not None else None

    def recent(self) -> tuple[dict[str, object], ...]:
        with self._lock:
            items = tuple(self._items.values())
        return tuple(_copy_safe_diagnostic(item) for item in items)


class _ExclusiveLoopbackHTTPServer(ThreadingHTTPServer):
    """Prevent a second listener from sharing a Control Plane address on Windows."""

    allow_reuse_address = False

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def _envelope(
    *,
    request_id: str,
    data: object | None = None,
    error: ControlPlaneTransportErrorV1 | None = None,
    diagnostics: object | None = None,
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
        "diagnostics": diagnostics if error else None,
    }


class ChappieControlPlaneServerV1:
    """Owns a loopback server and delegates all application work to its service."""

    def __init__(
        self,
        control_plane: Any,
        *,
        host: str = LOOPBACK_HOST,
        port: int = DEFAULT_CONTROL_PLANE_PORT,
        development_diagnostics_capacity: int = MAX_RECENT_DEVELOPMENT_DIAGNOSTICS,
    ) -> None:
        if host != LOOPBACK_HOST:
            raise ValueError("Control Plane transport must bind only to 127.0.0.1")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("Control Plane port must be between 0 and 65535")
        self._control_plane = control_plane
        self.host = host
        self.port = port
        self._httpd: ThreadingHTTPServer | None = None
        self._recent_development_diagnostics = _RecentDevelopmentDiagnosticsV1(
            development_diagnostics_capacity
        )

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
            self._httpd = _ExclusiveLoopbackHTTPServer((self.host, self.port), Handler)
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

    def get_development_diagnostic(self, request_id: str) -> dict[str, object] | None:
        """Return one bounded operator diagnostic without exposing request content."""
        return self._recent_development_diagnostics.get(request_id)

    def recent_development_diagnostics(self) -> tuple[dict[str, object], ...]:
        """Return retained diagnostics in deterministic oldest-to-newest completion order."""
        return self._recent_development_diagnostics.recent()

    def _record_development_diagnostic(
        self, request_id: str, diagnostic: dict[str, object]
    ) -> dict[str, object]:
        item = self._recent_development_diagnostics.record(request_id, diagnostic)
        _log_development_diagnostic(item)
        return item

    def _handle_get(self, handler: BaseHTTPRequestHandler) -> None:
        request_id = uuid.uuid4().hex
        routes = {
            "/v1/status": "get_status",
            "/v1/diagnostics": "get_diagnostics",
            "/v1/ai/status": "get_ai_status",
            "/v1/ai/models": "list_models",
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
            if method == "list_models":
                self._write(
                    handler,
                    200,
                    _envelope(
                        request_id=request_id,
                        data={"models": [item.payload() for item in response]},
                    ),
                )
                return
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
        development = handler.path == "/v1/development/analyze"
        task = handler.path == "/v1/development/task"
        if handler.path not in {
            "/v1/project/qa",
            "/v1/development/analyze",
            "/v1/development/task",
        }:
            self._method_not_allowed(handler)
            return
        try:
            content_length = int(handler.headers.get("Content-Length", ""))
        except ValueError:
            diagnostics = (
                self._record_development_diagnostic(
                    request_id, _development_transport_diagnostics(request_id, "INVALID_REQUEST")
                )
                if development
                else None
            )
            self._write(
                handler,
                400,
                _envelope(
                    request_id=request_id,
                    error=ControlPlaneTransportErrorV1(
                        "INVALID_REQUEST", "Project QA request must contain one bounded question"
                    ),
                    diagnostics=diagnostics,
                ),
            )
            return
        if content_length < 1 or content_length > PROJECT_QA_MAX_REQUEST_BODY_BYTES:
            # Drain a bounded near-limit body before replying so Windows clients can
            # receive the typed envelope instead of a connection reset.  Never
            # buffer an arbitrarily claimed body length.
            if content_length <= PROJECT_QA_MAX_REQUEST_BODY_BYTES * 2:
                handler.rfile.read(content_length)
            diagnostics = (
                self._record_development_diagnostic(
                    request_id,
                    _development_transport_diagnostics(request_id, "REQUEST_BODY_TOO_LARGE"),
                )
                if development
                else None
            )
            self._write(
                handler,
                400,
                _envelope(
                    request_id=request_id,
                    error=ControlPlaneTransportErrorV1(
                        "REQUEST_BODY_TOO_LARGE",
                        "Project QA request exceeds the Control Plane transport budget",
                    ),
                    diagnostics=diagnostics,
                ),
            )
            return
        try:
            value = json.loads(handler.rfile.read(content_length))
            allowed = (
                {"request", "requested_role", "requested_intent", "workspace_context"}
                if development
                else {"analysis"}
                if task
                else {"question", "workspace_context", "requested_model_id", "requested_model_role"}
            )
            required = "request" if development else "analysis" if task else "question"
            if (
                not isinstance(value, dict)
                or set(value).difference(allowed)
                or required not in value
            ):
                raise ValueError
            question = value[required]
            if not task and not isinstance(question, str):
                raise TypeError
            if development:
                from app.ai.model_policy import DevelopmentModelRoleV1

                requested_role = value.get("requested_role")
                if not isinstance(requested_role, str):
                    raise TypeError
                requested_role = DevelopmentModelRoleV1(requested_role).value
                requested_model_id = requested_model_role = None
            else:
                requested_role = None
                requested_model_id = value.get("requested_model_id")
                requested_model_role = value.get("requested_model_role")
                if requested_model_id is not None and requested_model_role is not None:
                    raise ValueError
                if requested_model_id is not None:
                    from app.ai.model_policy import LocalModelIdV1

                    if not isinstance(requested_model_id, str):
                        raise TypeError
                    requested_model_id = LocalModelIdV1(requested_model_id).value
                if requested_model_role is not None:
                    from app.ai.model_policy import DevelopmentModelRoleV1

                    if not isinstance(requested_model_role, str):
                        raise TypeError
                    requested_model_role = DevelopmentModelRoleV1(requested_model_role).value
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            diagnostics = (
                self._record_development_diagnostic(
                    request_id, _development_transport_diagnostics(request_id, "INVALID_REQUEST")
                )
                if development
                else None
            )
            self._write(
                handler,
                400,
                _envelope(
                    request_id=request_id,
                    error=ControlPlaneTransportErrorV1(
                        "INVALID_REQUEST", "Project QA request must contain one bounded question"
                    ),
                    diagnostics=diagnostics,
                ),
            )
            return
        try:
            if task:
                response = self._control_plane.prepare_development_task(question)
                if response.state.value != "ADVISORY":
                    self._write(
                        handler,
                        400,
                        _envelope(
                            request_id=request_id,
                            error=ControlPlaneTransportErrorV1(
                                response.reason_code or "TASK_VALIDATION_FAILURE",
                                "Codex task is unavailable",
                            ),
                        ),
                    )
                    return
                self._write(handler, 200, _envelope(request_id=request_id, data=response.payload()))
                return
            if development:
                development_kwargs = {
                    "requested_role": requested_role,
                    "workspace_context": value.get("workspace_context"),
                }
                if value.get("requested_intent") is not None:
                    development_kwargs["requested_intent"] = value["requested_intent"]
                response = self._control_plane.analyze_development(question, **development_kwargs)
                payload = response.payload()
                from app.development_analysis.outcome import project_development_outcome

                outcome = project_development_outcome(response)
                payload["outcome"] = outcome.payload()
                payload["unknowns"] = list(outcome.unknowns)
                diagnostics = self._record_development_diagnostic(
                    request_id, _development_diagnostics(payload, request_id=request_id)
                )
                if response.state.value == "VALIDATION_ERROR":
                    self._write(
                        handler,
                        400,
                        _envelope(
                            request_id=request_id,
                            error=ControlPlaneTransportErrorV1(
                                response.reason_code or "DEVELOPMENT_ANALYSIS_UNAVAILABLE",
                                "Development analysis is unavailable",
                            ),
                            diagnostics=diagnostics,
                        ),
                    )
                    return
                self._write(handler, 200, _envelope(request_id=request_id, data=payload))
                return
            if requested_model_id is None and requested_model_role is None:
                response = self._control_plane.ask_project(question, value.get("workspace_context"))
            elif requested_model_id is not None:
                response = self._control_plane.ask_project(
                    question, value.get("workspace_context"), requested_model_id=requested_model_id
                )
            else:
                response = self._control_plane.ask_project(
                    question,
                    value.get("workspace_context"),
                    requested_model_role=requested_model_role,
                )
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
            if development:
                diagnostics = self._record_development_diagnostic(
                    request_id, _development_transport_diagnostics(request_id, "INTERNAL_ERROR")
                )
            else:
                diagnostics = None
            self._write(
                handler,
                500,
                _envelope(
                    request_id=request_id,
                    error=ControlPlaneTransportErrorV1(
                        "INTERNAL_ERROR",
                        (
                            "Development Analysis request could not be completed"
                            if development
                            else "Project QA request could not be completed"
                        ),
                    ),
                    diagnostics=diagnostics,
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


def _log_development_lifecycle(payload: object) -> None:
    """Retain safe completed/failure lifecycle metadata in the existing operator log."""
    if not isinstance(payload, dict) or not isinstance(payload.get("lifecycle"), dict):
        return
    evidence = payload.get("evidence")
    safe_evidence = None
    if isinstance(evidence, dict):
        safe_evidence = {
            key: evidence.get(key)
            for key in (
                "allowed_fact_ids",
                "record_ids",
                "evidence_digest",
                "knowledge_context_digest",
                "requested_role",
                "effective_model",
            )
        }
    safe = {
        "lifecycle": payload["lifecycle"],
        "evidence": safe_evidence,
        "citation_failure": payload.get("citation_failure"),
    }
    # Warning is deliberately used: the standard local service configuration emits it to
    # the Chappie Output channel, unlike INFO, while the payload contains no request or model text.
    LOGGER.warning("Development Analysis lifecycle=%s", json.dumps(safe, sort_keys=True))


def _development_transport_diagnostics(request_id: str, reason_code: str) -> dict[str, object]:
    return {
        "operation": "DEVELOPMENT_ANALYSIS",
        "request_id": request_id,
        "analysis_request_id": None,
        "status": "FAILED",
        "reason_code": reason_code,
        "failed_stage": "TRANSPORT",
        "completed_stages": (),
        "lifecycle": (),
        "evidence": None,
        "readiness": None,
        "handoff_status": "NOT_REACHED",
    }


def _development_diagnostics(payload: object, *, request_id: str) -> dict[str, object]:
    if not isinstance(payload, dict):
        return _development_transport_diagnostics(request_id, "DEVELOPMENT_ANALYSIS_UNAVAILABLE")
    lifecycle = payload.get("lifecycle")
    events = lifecycle.get("events") if isinstance(lifecycle, dict) else ()
    safe_events = [
        {key: item.get(key) for key in ("stage", "status", "elapsed_ms", "reason_code")}
        for item in events
        if isinstance(item, dict)
    ]
    failed = next(
        (item.get("stage") for item in safe_events if item.get("status") == "FAILED"),
        None,
    )
    completed_stages = tuple(
        item["stage"] for item in safe_events if item.get("status") == "COMPLETED"
    )
    state = payload.get("state")
    completed = state == "OK"
    evidence = payload.get("evidence")
    safe_evidence = (
        {
            key: evidence.get(key)
            for key in (
                "source_commit",
                "knowledge_identity",
                "knowledge_manifest_digest",
                "knowledge_context_digest",
                "evidence_digest",
                "requested_role",
                "effective_model",
                "record_ids",
            )
        }
        if isinstance(evidence, dict)
        else None
    )
    return {
        "operation": "DEVELOPMENT_ANALYSIS",
        "request_id": request_id,
        "analysis_request_id": lifecycle.get("request_id") if isinstance(lifecycle, dict) else None,
        "status": "COMPLETED" if completed else "FAILED",
        "reason_code": None if completed else payload.get("reason_code"),
        "failed_stage": None if completed else failed,
        "completed_stages": completed_stages,
        "lifecycle": safe_events,
        "evidence": safe_evidence,
        "readiness": payload.get("readiness"),
        "handoff_status": "NOT_REACHED",
    }


def _is_request_id(value: object) -> bool:
    return isinstance(value, str) and len(value) == 32 and value.isascii() and value.isalnum()


def _copy_safe_diagnostic(value: dict[str, object]) -> dict[str, object]:
    """Make diagnostics immutable to callers through a deterministic JSON projection."""
    return json.loads(canonical_json(value))


def _log_development_diagnostic(diagnostic: dict[str, object]) -> None:
    """Emit only the retained bounded operator projection."""
    LOGGER.warning("Development Analysis diagnostic=%s", json.dumps(diagnostic, sort_keys=True))


def _log_development_internal_failure(request_id: str) -> None:
    LOGGER.warning(
        "Development Analysis lifecycle=%s",
        json.dumps(
            {
                "request_id": request_id,
                "operation": "DEVELOPMENT_ANALYSIS",
                "stage": "SERIALIZATION",
                "status": "FAILED",
                "reason_code": "INTERNAL_ERROR",
            },
            sort_keys=True,
        ),
    )
