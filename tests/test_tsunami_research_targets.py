from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.research.capital_flow.binance_breadth_authority_v2 import (
    BINANCE_BREADTH_AUTHORITY_V2_IDENTITY,
)
from app.research.capital_flow.free_v2_analytical_view import ANALYTICAL_VIEW_IDENTITY
from app.research.capital_flow.free_v2_materializer import BINANCE_V2_AUTHORITY_DIGEST
from app.research.capital_flow.tsunami_forecast_contract import (
    EXPERIMENT_IDENTITY,
    PRIMARY_HORIZONS,
    RESEARCH_END_EXCLUSIVE,
    Direction,
    Severity,
    classify_standardized_return,
)
from app.research.capital_flow.tsunami_research_targets import (
    ANALYTICAL_SEMANTIC_DIGEST,
    ANALYTICAL_VIEW_STATUS,
    EPISODE_CONTRACT_IDENTITY,
    PAPER_TRADING_STATUS,
    REAL_RESEARCH_TARGETS_STATUS,
    RESEARCH_MODELS_STATUS,
    RESEARCH_TARGET_IDENTITY,
    RESEARCH_TARGET_MATERIALIZER_STATUS,
    ROLLING_DIRECT_ABSOLUTE_TOLERANCE,
    TARGET_PARTITIONING,
    TSUNAMI_CONTRACT_STATUS,
    VALIDATION_STATUS,
    BtcMinuteCandleV1,
    ResearchTargetRowV1,
    RollingCausalVolatilityV1,
    TargetState,
    calculate_research_target,
    direct_sigma,
    feature_target_join_sql,
    horizon_name,
    publish_research_targets,
    realized_tsunami_episodes,
    research_target_selection_sql,
    target_contract_payload,
    target_semantic_digest,
    validate_analytical_source,
    validate_binance_v2_binding,
    verify_research_targets,
)
from app.tools.tsunami_research_targets import build_parser

T = datetime(2024, 3, 1, tzinfo=UTC)


def _ready_rolling(
    timestamp: datetime = T,
) -> tuple[RollingCausalVolatilityV1, tuple[Decimal, ...]]:
    rolling = RollingCausalVolatilityV1()
    closes = tuple(Decimal(100) + Decimal(index % 19) / 100 for index in range(43_201))
    start = timestamp - timedelta(minutes=43_200)
    for index, close in enumerate(closes):
        rolling.add_completed_close(start + timedelta(minutes=index), close)
    return rolling, closes


def _available_row(timestamp: datetime, horizon: timedelta, z: str = "4.5") -> ResearchTargetRowV1:
    score = Decimal(z)
    label = classify_standardized_return(score)
    return ResearchTargetRowV1(
        timestamp,
        horizon,
        timestamp + timedelta(minutes=1),
        timestamp + horizon,
        Decimal(100),
        Decimal(101),
        Decimal("0.01"),
        Decimal("0.002"),
        score,
        label.direction,
        label.severity,
        abs(score) >= 1,
        abs(score) >= 2,
        abs(score) >= 4,
        score >= 4,
        score <= -4,
        abs(score) >= 4,
        TargetState.AVAILABLE,
    )


def test_identity_bindings_and_architecture_states_are_exact() -> None:
    contract = target_contract_payload()
    assert RESEARCH_TARGET_IDENTITY == "CHAPPIE_CAPITAL_FLOW_TSUNAMI_RESEARCH_TARGETS/1.0.0"
    assert contract["experiment_identity"] == EXPERIMENT_IDENTITY
    assert contract["analytical_view_identity"] == ANALYTICAL_VIEW_IDENTITY
    assert contract["analytical_semantic_digest"] == ANALYTICAL_SEMANTIC_DIGEST
    assert contract["binance_authority_identity"] == BINANCE_BREADTH_AUTHORITY_V2_IDENTITY
    assert contract["binance_authority_semantic_digest"] == BINANCE_V2_AUTHORITY_DIGEST
    assert contract["episode_contract_identity"] == EPISODE_CONTRACT_IDENTITY
    assert contract["partitioning"] == TARGET_PARTITIONING
    assert (
        ANALYTICAL_VIEW_STATUS,
        TSUNAMI_CONTRACT_STATUS,
        RESEARCH_TARGET_MATERIALIZER_STATUS,
        REAL_RESEARCH_TARGETS_STATUS,
        RESEARCH_MODELS_STATUS,
        VALIDATION_STATUS,
        PAPER_TRADING_STATUS,
    ) == (
        "VALIDATED",
        "RELEASED",
        "IMPLEMENTED",
        "NOT_MATERIALIZED",
        "NOT_RUN",
        "NOT_ACCESSED",
        "NOT_RUN",
    )


def test_only_exact_binance_v2_binding_is_accepted() -> None:
    validate_binance_v2_binding(BINANCE_BREADTH_AUTHORITY_V2_IDENTITY, BINANCE_V2_AUTHORITY_DIGEST)
    with pytest.raises(ValueError, match="exact validated Binance V2"):
        validate_binance_v2_binding(
            "chappie_binance_breadth_historical_authority/1.0.0", BINANCE_V2_AUTHORITY_DIGEST
        )
    with pytest.raises(ValueError, match="exact validated Binance V2"):
        validate_binance_v2_binding(BINANCE_BREADTH_AUTHORITY_V2_IDENTITY, "0" * 64)


def test_analytical_source_requires_exact_released_digests(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "app.research.capital_flow.tsunami_research_targets.verify_analytical_view",
        lambda _root: {
            "analytical_view_identity": ANALYTICAL_VIEW_IDENTITY,
            "analytical_semantic_digest": ANALYTICAL_SEMANTIC_DIGEST,
            "manifest_semantic_digest": "0" * 64,
        },
    )
    with pytest.raises(ValueError, match="exact validated analytical view"):
        validate_analytical_source(tmp_path)


def test_rolling_sigma_matches_direct_n_minus_one_and_sqrt_h_scaling() -> None:
    rolling, closes = _ready_rolling()
    sigma_1h = rolling.sigma(timedelta(hours=1))
    direct_1h = direct_sigma(closes, timedelta(hours=1))
    assert abs(sigma_1h - direct_1h) <= ROLLING_DIRECT_ABSOLUTE_TOLERANCE
    sigma_4h = rolling.sigma(timedelta(hours=4))
    assert abs(sigma_4h / sigma_1h - Decimal(2)) <= ROLLING_DIRECT_ABSOLUTE_TOLERANCE


def test_rolling_window_is_exactly_causal_and_resets_on_gap_or_invalid_close() -> None:
    rolling, _ = _ready_rolling()
    assert rolling.ready
    rolling.add_completed_close(T + timedelta(minutes=2), Decimal(101))
    assert not rolling.ready
    with pytest.raises(ValueError, match="positive and finite"):
        rolling.add_completed_close(T + timedelta(minutes=3), Decimal(0))
    assert not rolling.ready


def test_entry_exit_future_return_and_all_threshold_labels() -> None:
    rolling, _ = _ready_rolling()
    horizon = timedelta(hours=1)
    candles = {
        T + timedelta(minutes=1): BtcMinuteCandleV1(
            T + timedelta(minutes=1), Decimal(100), Decimal(100)
        ),
        T + horizon - timedelta(minutes=1): BtcMinuteCandleV1(
            T + horizon - timedelta(minutes=1), Decimal(100), Decimal(101)
        ),
    }
    row = calculate_research_target(T, horizon, candles, rolling)
    assert row.target_state is TargetState.AVAILABLE
    assert row.entry_timestamp == T + timedelta(minutes=1)
    assert row.exit_timestamp == T + horizon
    assert row.future_log_return is not None and row.future_log_return > 0
    assert row.z_score is not None
    assert row.direction_class in tuple(Direction)
    assert row.severity_class in tuple(Severity)
    assert row.any_tsunami == row.tsunami_flag


@pytest.mark.parametrize(
    ("hours", "last"),
    (
        (1, datetime(2025, 5, 31, 23, tzinfo=UTC)),
        (4, datetime(2025, 5, 31, 20, tzinfo=UTC)),
        (12, datetime(2025, 5, 31, 12, tzinfo=UTC)),
        (24, datetime(2025, 5, 31, tzinfo=UTC)),
        (48, datetime(2025, 5, 30, tzinfo=UTC)),
        (72, datetime(2025, 5, 29, tzinfo=UTC)),
    ),
)
def test_research_boundary_and_final_timestamps_never_read_validation(
    hours: int, last: datetime
) -> None:
    horizon = timedelta(hours=hours)
    rolling, _ = _ready_rolling(last)
    candles = {
        last + timedelta(minutes=1): BtcMinuteCandleV1(
            last + timedelta(minutes=1), Decimal(100), Decimal(100)
        ),
        last + horizon - timedelta(minutes=1): BtcMinuteCandleV1(
            last + horizon - timedelta(minutes=1), Decimal(100), Decimal(101)
        ),
    }
    row = calculate_research_target(last, horizon, candles, rolling)
    assert row.exit_timestamp == RESEARCH_END_EXCLUSIVE
    outside = calculate_research_target(last + timedelta(minutes=1), horizon, {}, rolling)
    assert outside.target_state is TargetState.TARGET_OUTSIDE_RESEARCH_BOUNDARY
    assert outside.entry_timestamp is None and outside.exit_timestamp is None


def test_warmup_missing_invalid_and_zero_volatility_states_fail_closed() -> None:
    horizon = timedelta(hours=1)
    cold = RollingCausalVolatilityV1()
    assert (
        calculate_research_target(T, horizon, {}, cold).target_state
        is TargetState.INSUFFICIENT_VOLATILITY_HISTORY
    )
    rolling, _ = _ready_rolling()
    assert (
        calculate_research_target(T, horizon, {}, rolling).target_state
        is TargetState.MISSING_BTC_PRICE
    )
    candles = {
        T + timedelta(minutes=1): BtcMinuteCandleV1(
            T + timedelta(minutes=1), Decimal(0), Decimal(1)
        ),
        T + timedelta(minutes=59): BtcMinuteCandleV1(
            T + timedelta(minutes=59), Decimal(1), Decimal(1)
        ),
    }
    assert (
        calculate_research_target(T, horizon, candles, rolling).target_state
        is TargetState.INVALID_BTC_PRICE
    )
    flat = RollingCausalVolatilityV1()
    for index in range(43_201):
        flat.add_completed_close(T - timedelta(minutes=43_200 - index), Decimal(100))
    assert (
        calculate_research_target(
            T,
            horizon,
            {
                **candles,
                T + timedelta(minutes=1): BtcMinuteCandleV1(
                    T + timedelta(minutes=1), Decimal(1), Decimal(1)
                ),
            },
            flat,
        ).target_state
        is TargetState.ZERO_VOLATILITY
    )


def test_thresholds_and_signed_tsunami_are_exact() -> None:
    values = (
        ("0.49", Direction.NO_MOVE, Severity.NONE, False, False),
        ("0.5", Direction.UP, Severity.NONE, False, False),
        ("-0.5", Direction.DOWN, Severity.NONE, False, False),
        ("1", Direction.UP, Severity.MOVE, False, False),
        ("-2", Direction.DOWN, Severity.STRESS, False, False),
        ("4", Direction.UP, Severity.TSUNAMI, True, False),
        ("-4", Direction.DOWN, Severity.TSUNAMI, False, True),
    )
    for value, direction, severity, up, down in values:
        row = _available_row(T, timedelta(hours=24), value)
        assert (row.direction_class, row.severity_class) == (direction, severity)
        assert (row.up_tsunami_flag, row.down_tsunami_flag) == (up, down)


def test_realized_episode_api_uses_ground_truth_labels_only() -> None:
    horizon = timedelta(hours=1)
    rows = (
        _available_row(T, horizon, "4.1"),
        _available_row(T + timedelta(minutes=30), horizon, "-5"),
        _available_row(T + timedelta(hours=2), horizon, "4.2"),
    )
    episodes = realized_tsunami_episodes(rows, horizon)
    assert len(episodes) == 2
    assert episodes[0].candidate_count == 2


def test_target_digest_is_deterministic_and_changes_with_semantics() -> None:
    rows = (_available_row(T, timedelta(hours=24)),)
    assert target_semantic_digest(rows) == target_semantic_digest(rows)
    assert target_semantic_digest(rows) != target_semantic_digest(
        (_available_row(T, timedelta(hours=24), "5"),)
    )


def test_manifest_last_create_only_and_tamper_detection(tmp_path: Path) -> None:
    root = tmp_path / "targets"
    phases: list[str] = []
    rows = (
        _available_row(datetime(2024, 1, 1, tzinfo=UTC), timedelta(hours=1)),
        _available_row(datetime(2024, 2, 1, tzinfo=UTC), timedelta(hours=1)),
        _available_row(datetime(2024, 1, 1, tzinfo=UTC), timedelta(hours=4)),
    )
    manifest = publish_research_targets(rows, root, phase_hook=phases.append)
    assert phases == ["partitions_verified", "manifest_published"]
    assert manifest["total_rows"] == 3
    assert manifest["partition_count"] == 3
    with pytest.raises(FileExistsError, match="create-only"):
        publish_research_targets(rows, root)
    artifact = root / manifest["partitions"][0]["relative_path"]
    with artifact.open("ab") as handle:
        handle.write(b"tamper")
    with pytest.raises(ValueError, match="partition digest"):
        verify_research_targets(root)


def test_research_selector_and_feature_join_cannot_enter_validation(tmp_path: Path) -> None:
    sql = research_target_selection_sql(
        tmp_path,
        timedelta(hours=24),
        datetime(2025, 5, 1, tzinfo=UTC),
        RESEARCH_END_EXCLUSIVE,
        direction=Direction.UP,
        severity=Severity.TSUNAMI,
    )
    assert "horizon=24h" in sql and "direction_class = 'UP'" in sql
    join = feature_target_join_sql(
        tmp_path / "features",
        tmp_path / "targets",
        timedelta(hours=24),
        datetime(2025, 5, 1, tzinfo=UTC),
        RESEARCH_END_EXCLUSIVE,
    )
    assert "USING (timestamp_utc)" in join
    with pytest.raises(ValueError, match="wholly inside Research"):
        research_target_selection_sql(
            tmp_path,
            timedelta(hours=24),
            datetime(2025, 5, 1, tzinfo=UTC),
            RESEARCH_END_EXCLUSIVE + timedelta(minutes=1),
        )
    with pytest.raises(ValueError, match="wholly inside Research"):
        research_target_selection_sql(
            tmp_path,
            timedelta(hours=24),
            datetime(2026, 9, 1, tzinfo=UTC),
            datetime(2026, 9, 2, tzinfo=UTC),
        )


def test_horizon_order_preserves_24h_primary_metadata() -> None:
    assert PRIMARY_HORIZONS == tuple(timedelta(hours=value) for value in (1, 4, 12, 24, 48, 72))
    assert [horizon_name(item) for item in PRIMARY_HORIZONS] == [
        "1h",
        "4h",
        "12h",
        "24h",
        "48h",
        "72h",
    ]
    assert target_contract_payload()["primary_horizon"] == "24h"


def test_operator_defaults_to_e_target_root_and_parsing_does_not_execute() -> None:
    args = build_parser().parse_args([])
    assert args.output_root.drive == "E:"
    assert args.analytical_root.drive == "E:"
    assert args.binance_v2_root.name == "binance_breadth_free_v2"
