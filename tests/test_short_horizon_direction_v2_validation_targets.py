from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.research.capital_flow.short_horizon_direction_v2_contract import (
    VALIDATION_END_EXCLUSIVE,
)
from app.research.capital_flow.short_horizon_direction_v2_validation_targets import (
    TERMINAL_ROWS_EXCLUDED,
    VALIDATION_HORIZON,
    VALIDATION_TARGET_IDENTITY,
    materialize_validation_targets,
    validation_target_contract,
    validation_target_semantic_identity,
    verify_validation_targets,
)
from app.research.capital_flow.tsunami_forecast_contract import (
    Direction,
    classify_standardized_return,
)
from app.research.capital_flow.tsunami_research_targets import (
    RESEARCH_TARGET_IDENTITY,
    ResearchTargetRowV1,
    TargetState,
)


def _row(timestamp: datetime, score: str) -> ResearchTargetRowV1:
    z_score = Decimal(score)
    label = classify_standardized_return(z_score)
    return ResearchTargetRowV1(timestamp, VALIDATION_HORIZON, timestamp + timedelta(minutes=1), timestamp + VALIDATION_HORIZON, Decimal(100), Decimal(101), Decimal("0.01"), Decimal("0.002"), z_score, label.direction, label.severity, False, False, False, False, False, False, TargetState.AVAILABLE)


def test_validation_target_contract_is_sealed_and_distinct() -> None:
    contract = validation_target_contract()
    assert VALIDATION_TARGET_IDENTITY != RESEARCH_TARGET_IDENTITY
    assert contract["start"] == "2025-06-01T00:00:00+00:00"
    assert contract["end_exclusive"] == "2026-06-01T00:00:00+00:00"
    assert TERMINAL_ROWS_EXCLUDED == 59
    assert VALIDATION_END_EXCLUSIVE - VALIDATION_HORIZON == datetime(2026, 5, 31, 23, tzinfo=UTC)
    assert validation_target_semantic_identity() == validation_target_semantic_identity()


def test_frozen_class_semantics_remain_unchanged() -> None:
    assert classify_standardized_return(Decimal("0.49")).direction is Direction.NO_MOVE
    assert classify_standardized_return(Decimal("0.5")).direction is Direction.UP
    assert classify_standardized_return(Decimal("-0.5")).direction is Direction.DOWN


def test_validation_publication_round_trip_and_rejections(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    start = datetime(2025, 6, 1, tzinfo=UTC)
    rows = (_row(start, "0.5"), _row(start + timedelta(minutes=1), "-0.5"), _row(start + timedelta(minutes=2), "0"))
    monkeypatch.setattr("app.research.capital_flow.short_horizon_direction_v2_validation_targets.build_validation_targets", lambda _root: rows)
    root = tmp_path / "targets"
    published = materialize_validation_targets(root)
    verified = verify_validation_targets(root)
    assert published["reused"] is False
    assert verified["row_count"] == 3
    assert verified["class_support"] == {"UP": 1, "DOWN": 1, "NO_MOVE": 1}
    assert verified["first_timestamp"] == "2025-06-01T00:00:00Z"
    assert materialize_validation_targets(root)["reused"] is True
    manifest = root / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        verify_validation_targets(root)
    pending = tmp_path / "pending"; pending.mkdir()
    with pytest.raises(FileExistsError, match="without canonical manifest"):
        materialize_validation_targets(pending)
