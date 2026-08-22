import pytest

from app.ai.model_policy import (
    LOCAL_MODELS,
    LocalModelIdV1,
    local_model_descriptor_for_concrete_model,
    project_local_inventory,
    resolve_local_model,
)


@pytest.mark.parametrize(
    ("model_id", "concrete"),
    (
        (LocalModelIdV1.QWEN3_4B, "qwen3:4b"),
        (LocalModelIdV1.QWEN3_14B, "qwen3:14b"),
    ),
)
def test_allowed_local_models_resolve_to_canonical_concrete_models(model_id, concrete):
    assert resolve_local_model(model_id).concrete_model == concrete


def test_approved_models_have_request_local_execution_budgets():
    assert resolve_local_model(LocalModelIdV1.QWEN3_4B).execution_timeout_seconds == 45.0
    assert resolve_local_model(LocalModelIdV1.QWEN3_14B).execution_timeout_seconds == 75.0


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
        (True, True, False),
    ]
