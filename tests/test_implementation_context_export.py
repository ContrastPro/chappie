from __future__ import annotations

from tools.acceptance.export_implementation_context import (
    EXPORT_IDENTITY,
    export_real_diff_review_context,
)


def test_real_context_export_is_deterministic_and_neutral() -> None:
    first = export_real_diff_review_context()
    second = export_real_diff_review_context()
    assert first == second
    assert first["identity"] == EXPORT_IDENTITY
    assert first["sufficiency"] == "IMPLEMENTATION_CONTEXT_SUFFICIENT"
    assert first["context"]["unknown_items"] == []
    assert "task_id" not in str(first)
    assert "# DevelopmentDiffReviewV1" not in str(first)
