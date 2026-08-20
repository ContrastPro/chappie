"""Frontend-neutral, read-only Chappie Local Control Plane v1.

This module deliberately contains no transport, setup, provider, Knowledge rebuild,
AI generation, RDE execution, or trading authority.  It projects existing bounded
application diagnostics into stable JSON-safe contracts for the CLI and future local
frontends.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from app.ai.schemas import canonical_json, digest
from app.bootstrap import (
    ChappieDoctorReportV1,
    ChappieLocalConfigV1,
    ChappieStatusV1,
    ComponentStateV1,
    doctor,
    load_local_config,
)

CONTROL_PLANE_IDENTITY = "chappie_control_plane/1.0.0"


class ControlPlaneStateV1(StrEnum):
    OK = "OK"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class CapabilityStatusV1(StrEnum):
    AVAILABLE = "AVAILABLE"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"
    PLANNED = "PLANNED"


CONTROL_PLANE_CONTRACT_DIGEST = digest(
    {
        "identity": CONTROL_PLANE_IDENTITY,
        "states": [item.value for item in ControlPlaneStateV1],
        "capability_states": [item.value for item in CapabilityStatusV1],
        "methods": ["get_status", "get_diagnostics", "get_ai_status", "get_capabilities"],
        "authority": "read_only_application_projection",
    }
)


def _json_safe(value: object) -> object:
    """Normalize nested legacy payload tuples without exposing Python objects."""
    return json.loads(canonical_json(value))


@dataclass(frozen=True, slots=True)
class ControlPlaneErrorV1:
    state: ControlPlaneStateV1
    reason_code: str

    def payload(self) -> dict[str, str]:
        return {"state": self.state.value, "reason_code": self.reason_code}


@dataclass(frozen=True, slots=True)
class ChappieCapabilityV1:
    capability_id: str
    category: str
    status: CapabilityStatusV1
    authority: str
    read_only: bool
    requires_ai: bool
    requires_knowledge: bool
    description: str

    def payload(self) -> dict[str, object]:
        return {
            "capability_id": self.capability_id,
            "category": self.category,
            "status": self.status.value,
            "authority": self.authority,
            "read_only": self.read_only,
            "requires_ai": self.requires_ai,
            "requires_knowledge": self.requires_knowledge,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class ControlPlaneStatusV1:
    state: ControlPlaneStateV1
    status: ChappieStatusV1 | None
    error: ControlPlaneErrorV1 | None = None

    def payload(self) -> dict[str, object]:
        return {
            "identity": CONTROL_PLANE_IDENTITY,
            "contract_digest": CONTROL_PLANE_CONTRACT_DIGEST,
            "state": self.state.value,
            "status": _json_safe(self.status.payload()) if self.status else None,
            "error": self.error.payload() if self.error else None,
        }


@dataclass(frozen=True, slots=True)
class ControlPlaneDiagnosticsV1:
    state: ControlPlaneStateV1
    report: ChappieDoctorReportV1 | None
    error: ControlPlaneErrorV1 | None = None

    def payload(self) -> dict[str, object]:
        return {
            "identity": CONTROL_PLANE_IDENTITY,
            "contract_digest": CONTROL_PLANE_CONTRACT_DIGEST,
            "state": self.state.value,
            "diagnostics": _json_safe(self.report.payload()) if self.report else None,
            "error": self.error.payload() if self.error else None,
        }


@dataclass(frozen=True, slots=True)
class ControlPlaneAIStatusV1:
    state: ControlPlaneStateV1
    provider: str | None
    profile: str | None
    model: str | None
    provider_state: str | None
    model_state: str | None
    error: ControlPlaneErrorV1 | None = None

    def payload(self) -> dict[str, object]:
        return {
            "identity": CONTROL_PLANE_IDENTITY,
            "contract_digest": CONTROL_PLANE_CONTRACT_DIGEST,
            "state": self.state.value,
            "provider": self.provider,
            "profile": self.profile,
            "model": self.model,
            "provider_state": self.provider_state,
            "model_state": self.model_state,
            "error": self.error.payload() if self.error else None,
        }


@dataclass(frozen=True, slots=True)
class ControlPlaneCapabilitiesV1:
    state: ControlPlaneStateV1
    capabilities: tuple[ChappieCapabilityV1, ...]
    error: ControlPlaneErrorV1 | None = None

    def payload(self) -> dict[str, object]:
        return {
            "identity": CONTROL_PLANE_IDENTITY,
            "contract_digest": CONTROL_PLANE_CONTRACT_DIGEST,
            "state": self.state.value,
            "capabilities": [item.payload() for item in self.capabilities],
            "error": self.error.payload() if self.error else None,
        }


def _state(status: ChappieStatusV1) -> ControlPlaneStateV1:
    if status.overall_state.value == "READY":
        return ControlPlaneStateV1.OK
    return ControlPlaneStateV1.DEGRADED


def _component_state(status: ChappieStatusV1, name: str) -> ComponentStateV1 | None:
    return next((item.state for item in status.components if item.component == name), None)


def _availability(component: ComponentStateV1 | None) -> CapabilityStatusV1:
    if component == ComponentStateV1.READY:
        return CapabilityStatusV1.AVAILABLE
    if component in {ComponentStateV1.NOT_CONFIGURED, ComponentStateV1.NOT_INSTALLED}:
        return CapabilityStatusV1.UNAVAILABLE
    return CapabilityStatusV1.DEGRADED


class ChappieControlPlaneV1:
    """Read-only facade over existing product status and diagnostics services."""

    def __init__(
        self,
        paths,
        *,
        status_collector: Callable[[object], ChappieStatusV1],
        diagnostics_collector: Callable[[object], ChappieDoctorReportV1] = doctor,
        config_loader: Callable[[object], ChappieLocalConfigV1 | None] = load_local_config,
    ) -> None:
        self._paths = paths
        self._status_collector = status_collector
        self._diagnostics_collector = diagnostics_collector
        self._config_loader = config_loader

    def get_status(self) -> ControlPlaneStatusV1:
        try:
            status = self._status_collector(self._paths)
        except (OSError, RuntimeError, TypeError, ValueError):
            return ControlPlaneStatusV1(
                ControlPlaneStateV1.INTERNAL_ERROR,
                None,
                ControlPlaneErrorV1(ControlPlaneStateV1.INTERNAL_ERROR, "STATUS_UNAVAILABLE"),
            )
        return ControlPlaneStatusV1(_state(status), status)

    def get_diagnostics(self) -> ControlPlaneDiagnosticsV1:
        try:
            report = self._diagnostics_collector(self._paths)
        except (OSError, RuntimeError, TypeError, ValueError):
            return ControlPlaneDiagnosticsV1(
                ControlPlaneStateV1.INTERNAL_ERROR,
                None,
                ControlPlaneErrorV1(ControlPlaneStateV1.INTERNAL_ERROR, "DIAGNOSTICS_UNAVAILABLE"),
            )
        return ControlPlaneDiagnosticsV1(_state(report.status), report)

    def get_ai_status(self) -> ControlPlaneAIStatusV1:
        status_response = self.get_status()
        if status_response.status is None:
            return ControlPlaneAIStatusV1(
                ControlPlaneStateV1.INTERNAL_ERROR,
                None,
                None,
                None,
                None,
                None,
                status_response.error,
            )
        try:
            config = self._config_loader(self._paths)
        except (OSError, RuntimeError, TypeError, ValueError):
            config = None
        provider_state = _component_state(status_response.status, "AI_PROVIDER")
        model_state = _component_state(status_response.status, "AI_MODEL")
        state = (
            ControlPlaneStateV1.OK
            if provider_state == model_state == ComponentStateV1.READY
            else ControlPlaneStateV1.DEGRADED
        )
        return ControlPlaneAIStatusV1(
            state,
            config.provider if config else None,
            config.enabled_capabilities[0].upper()
            if config and config.enabled_capabilities
            else None,
            config.model if config else None,
            provider_state.value if provider_state else None,
            model_state.value if model_state else None,
        )

    def get_capabilities(self) -> ControlPlaneCapabilitiesV1:
        status_response = self.get_status()
        if status_response.status is None:
            return ControlPlaneCapabilitiesV1(
                ControlPlaneStateV1.INTERNAL_ERROR, (), status_response.error
            )
        status = status_response.status
        knowledge = _availability(_component_state(status, "KNOWLEDGE"))
        ai = _availability(_component_state(status, "AI_MODEL"))
        research = _availability(_component_state(status, "RESEARCH_STORAGE"))
        capabilities = (
            ChappieCapabilityV1(
                "system.status",
                "SYSTEM",
                CapabilityStatusV1.AVAILABLE,
                "CONTROL_PLANE",
                True,
                False,
                False,
                "Read current product status.",
            ),
            ChappieCapabilityV1(
                "system.doctor",
                "SYSTEM",
                CapabilityStatusV1.AVAILABLE,
                "CONTROL_PLANE",
                True,
                False,
                False,
                "Read safe diagnostics and remediation codes.",
            ),
            ChappieCapabilityV1(
                "ai.status",
                "AI",
                CapabilityStatusV1.AVAILABLE,
                "CONTROL_PLANE",
                True,
                False,
                False,
                "Read provider and configured-model status.",
            ),
            ChappieCapabilityV1(
                "knowledge.query",
                "KNOWLEDGE",
                knowledge,
                "KNOWLEDGE",
                True,
                False,
                True,
                "Retrieve bounded canonical Knowledge.",
            ),
            ChappieCapabilityV1(
                "project.qa",
                "AI",
                CapabilityStatusV1.AVAILABLE
                if ai == knowledge == CapabilityStatusV1.AVAILABLE
                else CapabilityStatusV1.DEGRADED,
                "AI_WITH_KNOWLEDGE",
                True,
                True,
                True,
                "Grounded Project QA; AI has no Knowledge write authority.",
            ),
            ChappieCapabilityV1(
                "research.existing_cache",
                "RESEARCH",
                research,
                "RDE",
                True,
                False,
                False,
                "Read verified existing-cache research evidence; execution authority remains RDE.",
            ),
        )
        return ControlPlaneCapabilitiesV1(_state(status), capabilities)
