import pytest

from app.ai.model_policy import (
    DEVELOPMENT_MODEL_ROLES,
    LOCAL_MODELS,
    DevelopmentModelRoleV1,
    LocalModelIdV1,
    development_model_role_for_model_id,
    local_model_descriptor_for_concrete_model,
    project_local_inventory,
    resolve_development_model_role,
    resolve_local_model,
)


@pytest.mark.parametrize(
    ("model_id", "concrete"),
    (
        (LocalModelIdV1.QWEN3_4B, "qwen3:4b"),
        (LocalModelIdV1.QWEN3_CODER_30B, "qwen3-coder:30b"),
        (LocalModelIdV1.QWEN3_14B, "qwen3:14b"),
    ),
)
def test_allowed_local_models_resolve_to_canonical_concrete_models(model_id, concrete):
    assert resolve_local_model(model_id).concrete_model == concrete


def test_approved_models_have_request_local_execution_budgets():
    assert resolve_local_model(LocalModelIdV1.QWEN3_4B).execution_timeout_seconds == 45.0
    assert resolve_local_model(LocalModelIdV1.QWEN3_CODER_30B).execution_timeout_seconds == 90.0
    assert resolve_local_model(LocalModelIdV1.QWEN3_14B).execution_timeout_seconds == 75.0


def test_development_roles_have_one_explicit_non_fallback_mapping() -> None:
    assert [(item.role, item.model_id) for item in DEVELOPMENT_MODEL_ROLES] == [
        (DevelopmentModelRoleV1.FAST, LocalModelIdV1.QWEN3_4B),
        (DevelopmentModelRoleV1.CODE, LocalModelIdV1.QWEN3_CODER_30B),
        (DevelopmentModelRoleV1.DEEP, LocalModelIdV1.QWEN3_14B),
    ]
    role, model = resolve_development_model_role(DevelopmentModelRoleV1.CODE)
    assert model.concrete_model == "qwen3-coder:30b"
    assert role.profile_identity == "CODE_ADVISORY"
    assert role.capability_identity == "development_model_role/CODE_ADVISORY"


def test_unknown_development_role_is_rejected() -> None:
    with pytest.raises(ValueError, match="not allowed"):
        resolve_development_model_role("UNKNOWN")  # type: ignore[arg-type]


def test_unknown_model_is_rejected():
    with pytest.raises(ValueError, match="not allowed"):
        resolve_local_model("not-a-model")  # type: ignore[arg-type]


def test_unknown_concrete_model_has_no_approved_execution_budget():
    assert local_model_descriptor_for_concrete_model("qwen3:8b") is None


def test_inventory_is_policy_ordered_and_excludes_unrelated_models():
    installed = {"qwen3:4b", "some-random-model:latest"}
    result = project_local_inventory(
        installed_models=installed, ready_models={"qwen3:4b"}, default_model="qwen3:4b"
    )
    assert tuple(item.model_id for item in result) == tuple(item.model_id for item in LOCAL_MODELS)
    assert [(item.installed, item.ready, item.default) for item in result] == [
        (True, True, True),
        (False, False, False),
        (False, False, False),
    ]
    assert installed == {"qwen3:4b", "some-random-model:latest"}


def test_inventory_keeps_default_when_missing_and_never_marks_uninstalled_ready():
    result = project_local_inventory(
        installed_models={"qwen3:14b"},
        ready_models={"qwen3:4b", "qwen3:14b"},
        default_model="qwen3:4b",
    )
    assert [(item.installed, item.ready, item.default) for item in result] == [
        (False, False, True),
        (False, False, False),
        (True, True, False),
    ]


def test_inventory_exposes_stable_role_mapping_and_explicit_availability() -> None:
    result = project_local_inventory(
        installed_models={"qwen3:4b", "qwen3-coder:30b"},
        ready_models={"qwen3:4b"},
        default_model="qwen3:4b",
    )
    assert [(item.role, item.payload()["availability"]) for item in result] == [
        (DevelopmentModelRoleV1.FAST, "AVAILABLE"),
        (DevelopmentModelRoleV1.CODE, "UNAVAILABLE"),
        (DevelopmentModelRoleV1.DEEP, "UNAVAILABLE"),
    ]
    assert development_model_role_for_model_id(LocalModelIdV1.QWEN3_CODER_30B) is not None
