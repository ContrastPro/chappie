"""Backend-owned allowlist for request-local local-AI model selection."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class LocalModelIdV1(StrEnum):
    QWEN3_4B = "QWEN3_4B"
    QWEN3_14B = "QWEN3_14B"


@dataclass(frozen=True, slots=True)
class LocalModelDescriptorV1:
    model_id: LocalModelIdV1
    concrete_model: str
    execution_timeout_seconds: float


@dataclass(frozen=True, slots=True)
class LocalModelInventoryItemV1:
    model_id: LocalModelIdV1
    concrete_model: str
    installed: bool
    ready: bool
    default: bool

    def payload(self) -> dict[str, object]:
        return {
            "model_id": self.model_id.value,
            "concrete_model": self.concrete_model,
            "installed": self.installed,
            "ready": self.ready,
            "default": self.default,
        }


LOCAL_MODELS: tuple[LocalModelDescriptorV1, ...] = (
    LocalModelDescriptorV1(LocalModelIdV1.QWEN3_4B, "qwen3:4b", 45.0),
    LocalModelDescriptorV1(LocalModelIdV1.QWEN3_14B, "qwen3:14b", 75.0),
)


def resolve_local_model(model_id: LocalModelIdV1) -> LocalModelDescriptorV1:
    try:
        return next(item for item in LOCAL_MODELS if item.model_id == model_id)
    except StopIteration as error:
        raise ValueError("model is not allowed") from error


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
        )
        for item in LOCAL_MODELS
    )
