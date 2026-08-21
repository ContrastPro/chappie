from datetime import UTC, datetime

import pytest

from app.control_plane.workspace_context import (
    WorkspaceContextV1,
    WorkspaceRangeV1,
    parse_workspace_context,
)


def _context() -> WorkspaceContextV1:
    return WorkspaceContextV1(
        "Chappie",
        "workspace-safe-id",
        "app/ai/runtime.py",
        "python",
        "selected code",
        WorkspaceRangeV1(10, 12),
        None,
        None,
        "main",
        "a" * 40,
        True,
    )


def test_workspace_context_is_bounded_relative_and_deterministic() -> None:
    value = _context()
    assert value.context_digest == _context().context_digest
    assert "/Users/" not in str(value.payload())
    assert {fact.source_type for fact in value.facts(datetime.now(tz=UTC))} == {
        "USER_SUPPLIED_WORKSPACE_CONTEXT"
    }
    assert parse_workspace_context(value.payload()) == value


@pytest.mark.parametrize("path", ("../secret.py", "/tmp/secret.py", "C:\\secret.py"))
def test_workspace_context_rejects_unsafe_paths(path: str) -> None:
    with pytest.raises(ValueError, match="relative"):
        WorkspaceContextV1("Chappie", "safe", path)


def test_workspace_context_rejects_oversized_and_tampered_payload() -> None:
    with pytest.raises(ValueError, match="exceeds"):
        WorkspaceContextV1(
            "Chappie", "safe", selected_text="x" * 12001, selected_text_range=WorkspaceRangeV1(0, 0)
        )
    payload = _context().payload()
    payload["context_digest"] = "wrong"
    with pytest.raises(ValueError, match="digest"):
        parse_workspace_context(payload)
    with pytest.raises(ValueError, match="hash"):
        WorkspaceContextV1("Chappie", "safe", git_commit="not-a-hash", git_dirty=False)
