"""Backend-owned allowlist for request-local local-AI model selection."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class LocalModelIdV1(StrEnum):
    QWEN3_4B = "QWEN3_4B"
    QWEN3_CODER_30B = "QWEN3_CODER_30B"
    QWEN3_14B = "QWEN3_14B"


class DevelopmentModelRoleV1(StrEnum):
    """Stable application roles; concrete models remain an implementation detail."""

    FAST = "FAST"
    CODE = "CODE"
    DEEP = "DEEP"


@dataclass(frozen=True, slots=True)
class LocalModelDescriptorV1:
    model_id: LocalModelIdV1
    concrete_model: str
    execution_timeout_seconds: float


@dataclass(frozen=True, slots=True)
class DevelopmentModelRoleDescriptorV1:
    """One explicit, non-fallback mapping from a stable role to an approved model."""

    role: DevelopmentModelRoleV1
    model_id: LocalModelIdV1
    profile_identity: str
    capability_identity: str


@dataclass(frozen=True, slots=True)
class LocalModelInventoryItemV1:
    model_id: LocalModelIdV1
    concrete_model: str
    installed: bool
    ready: bool
    default: bool
    role: DevelopmentModelRoleV1 | None = None
    profile_identity: str | None = None
    capability_identity: str | None = None

    def payload(self) -> dict[str, object]:
        return {
            "model_id": self.model_id.value,
            "concrete_model": self.concrete_model,
            "installed": self.installed,
            "ready": self.ready,
            "default": self.default,
            "role": self.role.value if self.role else None,
            "profile_identity": self.profile_identity,
            "capability_identity": self.capability_identity,
            "availability": "AVAILABLE" if self.ready else "UNAVAILABLE",
        }


LOCAL_MODELS: tuple[LocalModelDescriptorV1, ...] = (
    LocalModelDescriptorV1(LocalModelIdV1.QWEN3_4B, "qwen3:4b", 45.0),
    LocalModelDescriptorV1(LocalModelIdV1.QWEN3_CODER_30B, "qwen3-coder:30b", 90.0),
    LocalModelDescriptorV1(LocalModelIdV1.QWEN3_14B, "qwen3:14b", 75.0),
)


DEVELOPMENT_MODEL_ROLES: tuple[DevelopmentModelRoleDescriptorV1, ...] = (
    DevelopmentModelRoleDescriptorV1(
        DevelopmentModelRoleV1.FAST,
        LocalModelIdV1.QWEN3_4B,
        "FAST",
        "development_model_role/FAST",
    ),
    DevelopmentModelRoleDescriptorV1(
        DevelopmentModelRoleV1.CODE,
        LocalModelIdV1.QWEN3_CODER_30B,
        "CODE_ADVISORY",
        "development_model_role/CODE_ADVISORY",
    ),
    DevelopmentModelRoleDescriptorV1(
        DevelopmentModelRoleV1.DEEP,
        LocalModelIdV1.QWEN3_14B,
        "DEEP",
        "development_model_role/DEEP",
    ),
)


def resolve_local_model(model_id: LocalModelIdV1) -> LocalModelDescriptorV1:
    try:
        return next(item for item in LOCAL_MODELS if item.model_id == model_id)
    except StopIteration as error:
        raise ValueError("model is not allowed") from error


def resolve_development_model_role(
    role: DevelopmentModelRoleV1,
) -> tuple[DevelopmentModelRoleDescriptorV1, LocalModelDescriptorV1]:
    """Resolve one stable role exactly; callers must never substitute another model."""
    try:
        role_descriptor = next(item for item in DEVELOPMENT_MODEL_ROLES if item.role == role)
    except StopIteration as error:
        raise ValueError("development model role is not allowed") from error
    return role_descriptor, resolve_local_model(role_descriptor.model_id)


def development_model_role_for_model_id(
    model_id: LocalModelIdV1,
) -> DevelopmentModelRoleDescriptorV1 | None:
    return next((item for item in DEVELOPMENT_MODEL_ROLES if item.model_id == model_id), None)


def local_model_id_for_concrete_model(model: str) -> LocalModelIdV1 | None:
    """Return the policy ID for a concrete model without reinterpreting legacy config."""
    descriptor = local_model_descriptor_for_concrete_model(model)
    return descriptor.model_id if descriptor else None


def local_model_descriptor_for_concrete_model(model: str) -> LocalModelDescriptorV1 | None:
    """Return the approved descriptor for an exact configured local model, if any."""
    return next((item for item in LOCAL_MODELS if item.concrete_model == model), None)


def project_local_inventory(
    *, installed_models: set[str], ready_models: set[str], default_model: str | None
) -> tuple[LocalModelInventoryItemV1, ...]:
    """Project only policy-approved local models; never inspect remote state."""
    return tuple(
        LocalModelInventoryItemV1(
            item.model_id,
            item.concrete_model,
            item.concrete_model in installed_models,
            item.concrete_model in installed_models and item.concrete_model in ready_models,
            item.concrete_model == default_model,
            role.role if (role := development_model_role_for_model_id(item.model_id)) else None,
            role.profile_identity if role else None,
            role.capability_identity if role else None,
        )
        for item in LOCAL_MODELS
    )
