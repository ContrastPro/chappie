"""Offline contracts for the loopback-only Control Plane transport."""

from __future__ import annotations

import http.client
import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.ai.model_policy import DevelopmentModelRoleV1, LocalModelIdV1, LocalModelInventoryItemV1
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
    PROJECT_QA_MAX_REQUEST_BODY_BYTES,
    ChappieControlPlaneServerV1,
    ControlPlaneTransportErrorV1,
    _development_diagnostics,
    _log_development_lifecycle,
)
from app.development_analysis.models import DevelopmentAnalysisStateV1, DevelopmentAnalysisV1
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

    def list_models(self) -> tuple[LocalModelInventoryItemV1, ...]:
        return (
            LocalModelInventoryItemV1(
                LocalModelIdV1.QWEN3_4B,
                "qwen3:4b",
                True,
                True,
                True,
                DevelopmentModelRoleV1.FAST,
                "FAST",
                "development_model_role/FAST",
            ),
            LocalModelInventoryItemV1(
                LocalModelIdV1.QWEN3_CODER_30B,
                "qwen3-coder:30b",
                False,
                False,
                False,
                DevelopmentModelRoleV1.CODE,
                "CODE_ADVISORY",
                "development_model_role/CODE_ADVISORY",
            ),
            LocalModelInventoryItemV1(
                LocalModelIdV1.QWEN3_14B,
                "qwen3:14b",
                False,
                False,
                False,
                DevelopmentModelRoleV1.DEEP,
                "DEEP",
                "development_model_role/DEEP",
            ),
        )

    def ask_project(
        self, question: str, _workspace_context: object | None = None, **_selection: object
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

    def analyze_development(
        self, request: str, *, requested_role: str, workspace_context: object | None = None
    ) -> DevelopmentAnalysisV1:
        _ = requested_role, workspace_context
        return DevelopmentAnalysisV1(
            DevelopmentAnalysisStateV1.OK,
            request,
            facts=("bounded fact",),
            inferences=("bounded inference",),
            required_tests=("test",),
            non_goals=("No mutation.",),
            acceptance_criteria=("criterion",),
            validation_status="VALID",
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


def test_model_inventory_route_projects_service_values_without_recomputation(server) -> None:
    status, _headers, body = _request(server, "GET", "/v1/ai/models")

    assert status == 200
    assert body["data"] == {
        "models": [
            {
                "model_id": "QWEN3_4B",
                "concrete_model": "qwen3:4b",
                "installed": True,
                "ready": True,
                "default": True,
                "role": "FAST",
                "profile_identity": "FAST",
                "capability_identity": "development_model_role/FAST",
                "availability": "AVAILABLE",
            },
            {
                "model_id": "QWEN3_CODER_30B",
                "concrete_model": "qwen3-coder:30b",
                "installed": False,
                "ready": False,
                "default": False,
                "role": "CODE",
                "profile_identity": "CODE_ADVISORY",
                "capability_identity": "development_model_role/CODE_ADVISORY",
                "availability": "UNAVAILABLE",
            },
            {
                "model_id": "QWEN3_14B",
                "concrete_model": "qwen3:14b",
                "installed": False,
                "ready": False,
                "default": False,
                "role": "DEEP",
                "profile_identity": "DEEP",
                "capability_identity": "development_model_role/DEEP",
                "availability": "UNAVAILABLE",
            },
        ]
    }


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


def test_project_qa_transport_accepts_one_stable_model_role(server) -> None:
    status, _headers, body = _request(
        server,
        "POST",
        "/v1/project/qa",
        json.dumps({"question": "Who owns evidence?", "requested_model_role": "CODE"}),
    )
    assert status == 200
    assert body["data"]["answer"] == "RDE owns evidence authority."

    status, _headers, body = _request(
        server,
        "POST",
        "/v1/project/qa",
        json.dumps(
            {
                "question": "Who owns evidence?",
                "requested_model_id": "QWEN3_4B",
                "requested_model_role": "FAST",
            }
        ),
    )
    assert status == 400
    assert body["error"]["code"] == "INVALID_REQUEST"


def test_development_analysis_transport_round_trip_is_read_only(server) -> None:
    status, _headers, body = _request(
        server,
        "POST",
        "/v1/development/analyze",
        json.dumps({"request": "Analyze architecture", "requested_role": "FAST"}),
    )
    assert status == 200
    assert body["data"]["state"] == "OK"
    assert body["data"]["inferences"] == ["bounded inference"]
    assert body["data"]["outcome"] == {
        "identity": "chappie_development_outcome/1.0.0",
        "status": "COMPLETED",
        "blocker_code": None,
        "explanation": "Development analysis completed with sufficient bounded evidence.",
        "required_context": None,
        "suggested_next_action": "Review the advisory analysis and obtain human approval before implementation.",
        "safe_to_retry": False,
        "request_id": None,
        "failed_stage": None,
        "reason_code": None,
        "no_changes_made": True,
        "why_it_blocks": "",
    }
    diagnostic = server.get_development_diagnostic(body["request_id"])
    assert diagnostic is not None
    assert diagnostic["status"] == "COMPLETED"
    assert diagnostic["reason_code"] is None
    assert diagnostic["failed_stage"] is None


def test_invalid_development_request_is_retained_as_a_typed_transport_failure(server) -> None:
    status, _headers, body = _request(server, "POST", "/v1/development/analyze", "{")

    assert status == 400
    assert body["error"]["code"] == "INVALID_REQUEST"
    assert body["diagnostics"] == {
        "operation": "DEVELOPMENT_ANALYSIS",
        "request_id": body["request_id"],
        "analysis_request_id": None,
        "status": "FAILED",
        "reason_code": "INVALID_REQUEST",
        "failed_stage": "TRANSPORT",
        "completed_stages": [],
        "lifecycle": [],
        "evidence": None,
        "readiness": None,
        "handoff_status": "NOT_REACHED",
    }
    assert server.get_development_diagnostic(body["request_id"]) == body["diagnostics"]


def test_development_transport_propagates_explicit_task_intent(server) -> None:
    observed = []

    class IntentPlane(_FakeControlPlane):
        def analyze_development(
            self,
            request: str,
            *,
            requested_role: str,
            requested_intent: str | None = None,
            workspace_context: object | None = None,
        ) -> DevelopmentAnalysisV1:
            observed.append((requested_role, requested_intent, workspace_context))
            return DevelopmentAnalysisV1(
                DevelopmentAnalysisStateV1.OK, request, validation_status="VALID"
            )

    server._control_plane = IntentPlane()
    status, _headers, _body = _request(
        server,
        "POST",
        "/v1/development/analyze",
        json.dumps(
            {
                "request": "Implement the approved bounded change.",
                "requested_role": "CODE",
                "requested_intent": "IMPLEMENTATION",
            }
        ),
    )
    assert status == 200
    assert observed == [("CODE", "IMPLEMENTATION", None)]


@pytest.mark.parametrize(
    ("state", "reason", "expected_status"),
    (
        (DevelopmentAnalysisStateV1.TIMEOUT, "AI_PROVIDER_TIMEOUT", 503),
        (DevelopmentAnalysisStateV1.ANALYSIS_INVALID, "DEVELOPMENT_SCHEMA_INVALID", 503),
        (DevelopmentAnalysisStateV1.MODEL_UNAVAILABLE, "AI_UNAVAILABLE", 503),
        (DevelopmentAnalysisStateV1.VALIDATION_ERROR, "INVALID_REQUEST", 400),
    ),
)
def test_development_analysis_typed_failure_is_not_an_opaque_500(
    server, state, reason, expected_status
) -> None:
    class TypedFailurePlane(_FakeControlPlane):
        def analyze_development(
            self, request: str, *, requested_role: str, workspace_context: object | None = None
        ) -> DevelopmentAnalysisV1:
            _ = requested_role, workspace_context
            return DevelopmentAnalysisV1(state, request, reason_code=reason)

    server._control_plane = TypedFailurePlane()
    status, _headers, body = _request(
        server,
        "POST",
        "/v1/development/analyze",
        json.dumps({"request": "Analyze architecture", "requested_role": "CODE"}),
    )

    assert status == expected_status
    assert body["error"] == {"code": reason, "message": "Development analysis is unavailable"}
    assert body["diagnostics"]["request_id"] == body["request_id"]
    assert body["diagnostics"]["reason_code"] == reason
    assert server.get_development_diagnostic(body["request_id"]) == body["diagnostics"]


def test_unexpected_development_exception_remains_fail_closed_500(server, caplog) -> None:
    class BrokenPlane(_FakeControlPlane):
        def analyze_development(
            self, _request: str, *, requested_role: str, workspace_context: object | None = None
        ) -> DevelopmentAnalysisV1:
            _ = requested_role, workspace_context
            raise RuntimeError("secret internal detail")

    caplog.set_level(logging.INFO, logger="app.control_plane.transport")
    server._control_plane = BrokenPlane()
    status, _headers, body = _request(
        server,
        "POST",
        "/v1/development/analyze",
        json.dumps({"request": "Analyze architecture", "requested_role": "CODE"}),
    )

    assert status == 500
    assert body["error"] == {
        "code": "INTERNAL_ERROR",
        "message": "Development Analysis request could not be completed",
    }
    assert "secret" not in json.dumps(body)
    assert '"reason_code": "INTERNAL_ERROR"' in caplog.text
    assert "Analyze architecture" not in caplog.text
    assert "secret internal detail" not in caplog.text
    assert body["diagnostics"] == server.get_development_diagnostic(body["request_id"])
    assert body["diagnostics"]["failed_stage"] == "TRANSPORT"


def test_development_failure_telemetry_survives_completion_without_sensitive_content(
    caplog,
) -> None:
    caplog.set_level(logging.WARNING, logger="app.control_plane.transport")
    _log_development_lifecycle(
        {
            "request": "sensitive prompt must never reach diagnostics",
            "lifecycle": {
                "request_id": "a" * 32,
                "operation": "DEVELOPMENT_ANALYSIS",
                "requested_role": "CODE",
                "effective_model": "qwen3-coder:30b",
                "events": [
                    {
                        "stage": "SYNTHESIS",
                        "status": "FAILED",
                        "elapsed_ms": 90_000,
                        "reason_code": "UNSUPPORTED_KNOWLEDGE_CITATION",
                    }
                ],
            },
            "evidence": {
                "allowed_fact_ids": ["knowledge.record.statement"],
                "record_ids": ["record"],
                "evidence_digest": "b" * 64,
                "knowledge_context_digest": "c" * 64,
                "requested_role": "CODE",
                "effective_model": "qwen3-coder:30b",
                "answer": "sensitive model answer",
            },
            "citation_failure": {
                "citation_kind": "FACT_ID",
                "rejected_identifier": "knowledge.unsupplied.statement",
                "rejection": "NOT_IN_ALLOWLIST",
            },
        }
    )
    assert '"request_id": "' + "a" * 32 in caplog.text
    assert "UNSUPPORTED_KNOWLEDGE_CITATION" in caplog.text
    assert "knowledge.unsupplied.statement" in caplog.text
    assert "knowledge.record.statement" in caplog.text
    assert "sensitive prompt" not in caplog.text
    assert "sensitive model answer" not in caplog.text


_STAGED_FAILURES = (
    ("ROLE_RESOLUTION", "MODEL_ROLE_UNAVAILABLE"),
    ("GROUNDING_SYNTHESIS", "AI_PROVIDER_TIMEOUT"),
    ("GROUNDING_PARSE", "GROUNDING_SCHEMA_INVALID"),
    ("PROPOSAL_SYNTHESIS", "AI_PROVIDER_TIMEOUT"),
    ("PROPOSAL_PARSE", "PROPOSAL_SCHEMA_INVALID"),
    ("REVIEW_SYNTHESIS", "AI_PROVIDER_TIMEOUT"),
    ("REVIEW_PARSE", "REVIEW_SCHEMA_INVALID"),
    ("STRUCTURAL_VALIDATION", "DEVELOPMENT_ANALYSIS_INVALID"),
    ("READINESS_EVALUATION", "SOURCE_ANALYSIS_NOT_READY"),
)
_LIFECYCLE_STAGES = (
    "REQUEST_ACCEPTED",
    "ROLE_RESOLUTION",
    "EVIDENCE_BUILD",
    "GROUNDING_SYNTHESIS",
    "GROUNDING_PARSE",
    "PROPOSAL_SYNTHESIS",
    "PROPOSAL_PARSE",
    "REVIEW_SYNTHESIS",
    "REVIEW_PARSE",
    "STRUCTURAL_VALIDATION",
    "READINESS_EVALUATION",
)


def _staged_failure_payload(failing_stage: str, reason_code: str) -> dict[str, object]:
    index = _LIFECYCLE_STAGES.index(failing_stage)
    events = [
        {"stage": stage, "status": "COMPLETED", "elapsed_ms": 1, "reason_code": None}
        for stage in _LIFECYCLE_STAGES[:index]
    ]
    events.extend(
        [
            {"stage": failing_stage, "status": "STARTED", "elapsed_ms": 0, "reason_code": None},
            {
                "stage": failing_stage,
                "status": "FAILED",
                "elapsed_ms": 1,
                "reason_code": reason_code,
            },
        ]
    )
    return {
        "state": "ANALYSIS_INVALID",
        "reason_code": reason_code,
        "request": "sensitive prompt",
        "lifecycle": {"request_id": "b" * 32, "events": events},
        "evidence": {
            "source_commit": "c" * 40,
            "knowledge_identity": "chappie_knowledge/1.0.0",
            "knowledge_manifest_digest": "d" * 64,
            "knowledge_context_digest": "e" * 64,
            "evidence_digest": "f" * 64,
            "requested_role": "CODE",
            "effective_model": "qwen3-coder:30b",
            "record_ids": ["record.one"],
            "raw_knowledge": "must not leak",
        },
        "answer": "must not leak",
        "provider_payload": "must not leak",
    }


@pytest.mark.parametrize(("failing_stage", "reason_code"), _STAGED_FAILURES)
def test_recent_development_projection_preserves_first_failed_stage_and_prior_stages(
    failing_stage: str, reason_code: str
) -> None:
    diagnostic = _development_diagnostics(
        _staged_failure_payload(failing_stage, reason_code), request_id="a" * 32
    )

    assert diagnostic["request_id"] == "a" * 32
    assert diagnostic["analysis_request_id"] == "b" * 32
    assert diagnostic["status"] == "FAILED"
    assert diagnostic["reason_code"] == reason_code
    assert diagnostic["failed_stage"] == failing_stage
    assert (
        diagnostic["completed_stages"]
        == _LIFECYCLE_STAGES[: _LIFECYCLE_STAGES.index(failing_stage)]
    )
    assert diagnostic["lifecycle"][-1] == {
        "stage": failing_stage,
        "status": "FAILED",
        "elapsed_ms": 1,
        "reason_code": reason_code,
    }
    assert diagnostic["handoff_status"] == "NOT_REACHED"
    rendered = json.dumps(diagnostic, sort_keys=True)
    assert "sensitive prompt" not in rendered
    assert "must not leak" not in rendered
    assert "provider_payload" not in rendered


def test_recent_development_projection_retention_lookup_and_deterministic_eviction() -> None:
    value = ChappieControlPlaneServerV1(
        _FakeControlPlane(), port=0, development_diagnostics_capacity=2
    )
    request_ids = ("a" * 32, "b" * 32, "c" * 32)
    for request_id in request_ids:
        value._record_development_diagnostic(
            request_id,
            _development_diagnostics(
                _staged_failure_payload("ROLE_RESOLUTION", "MODEL_ROLE_UNAVAILABLE"),
                request_id=request_id,
            ),
        )

    assert value.get_development_diagnostic(request_ids[0]) is None
    assert [item["request_id"] for item in value.recent_development_diagnostics()] == list(
        request_ids[1:]
    )
    retained = value.get_development_diagnostic(request_ids[1])
    assert retained is not None
    retained["reason_code"] = "MUTATED_BY_CALLER"
    assert (
        value.get_development_diagnostic(request_ids[1])["reason_code"] == "MODEL_ROLE_UNAVAILABLE"
    )
    assert value.get_development_diagnostic("invalid") is None


def test_project_qa_transport_accepts_the_canonical_deep_question_boundary(server) -> None:
    body = json.dumps({"question": "architecture " * 1_000})
    assert len(body.encode("utf-8")) > 2_048
    status, _headers, response = _request(server, "POST", "/v1/project/qa", body)
    assert status == 200
    assert response["data"]["answer"] == "RDE owns evidence authority."


def test_project_qa_transport_rejects_only_a_request_above_its_explicit_body_budget(server) -> None:
    body = "x" * (PROJECT_QA_MAX_REQUEST_BODY_BYTES + 1)
    status, _headers, response = _request(server, "POST", "/v1/project/qa", body)
    assert status == 400
    assert response["error"] == {
        "code": "REQUEST_BODY_TOO_LARGE",
        "message": "Project QA request exceeds the Control Plane transport budget",
    }
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
