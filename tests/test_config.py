from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def make_settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_market_data_configuration_defaults() -> None:
    settings = make_settings()

    assert settings.market_cache_capacity == 1000
    assert settings.polling_cadence_seconds == 10.0
    assert settings.polling_bootstrap_limit == 1000
    assert settings.polling_regular_limit == 2
    assert settings.max_consecutive_failures == 3
    assert settings.market_clock_skew_warning_seconds == 5.0
    assert settings.run_log_file == Path("logs/latest_run.log")
    assert settings.smoke_run_cycles is None
    assert settings.trading_mode == "paper"
    assert settings.shadow_decisions_enabled is False
    assert settings.paper_trading_enabled is False
    assert settings.persistence_enabled is False
    assert settings.persistence_backend == "sqlite"
    assert settings.sqlite_path == Path("data/chappie-paper.sqlite3")
    assert settings.sqlite_busy_timeout_ms == 5000
    assert settings.portfolio_checkpoint_interval_transitions == 100
    assert settings.decision_coordinator_history_limit == 1000
    assert settings.decision_coordinator_dedup_capacity == 10000
    assert settings.shadow_history_limit == 1000
    assert settings.shadow_dedup_capacity == 10000
    assert settings.snapshot_stale_after_ms == 120000
    assert settings.ema_fast_window == 12
    assert settings.ema_slow_window == 26
    assert settings.long_target_exposure == Decimal(1)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("market_cache_capacity", 0),
        ("polling_cadence_seconds", 0),
        ("polling_bootstrap_limit", 0),
        ("polling_bootstrap_limit", 1001),
        ("polling_regular_limit", 1),
        ("max_consecutive_failures", 0),
        ("market_clock_skew_warning_seconds", -1),
        ("smoke_run_cycles", 0),
        ("smoke_run_cycles", -1),
    ],
)
def test_invalid_market_data_configuration(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        make_settings(**{field: value})


def test_bootstrap_limit_cannot_exceed_cache_capacity() -> None:
    with pytest.raises(ValidationError, match="must not exceed"):
        make_settings(market_cache_capacity=10, polling_bootstrap_limit=11)


def test_shadow_history_limit_cannot_exceed_cache_even_when_disabled() -> None:
    with pytest.raises(ValidationError, match="shadow_history_limit"):
        make_settings(
            market_cache_capacity=10,
            polling_bootstrap_limit=10,
            shadow_history_limit=11,
        )


def test_live_mode_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make_settings(trading_mode="live")


def test_regular_limit_may_be_greater_than_two() -> None:
    assert make_settings(polling_regular_limit=3).polling_regular_limit == 3


@pytest.mark.parametrize(
    "overrides",
    [
        {"shadow_history_limit": 0},
        {"shadow_dedup_capacity": 0},
        {"snapshot_stale_after_ms": 0},
        {"ema_fast_window": 1},
        {"ema_fast_window": 12, "ema_slow_window": 12},
        {"long_target_exposure": Decimal(0)},
        {"long_target_exposure": Decimal("1.1")},
        {"long_target_exposure": Decimal("NaN")},
        {"long_target_exposure": 0.5},
        {
            "shadow_decisions_enabled": True,
            "market_cache_capacity": 10,
            "polling_bootstrap_limit": 10,
            "shadow_history_limit": 11,
        },
    ],
)
def test_invalid_shadow_configuration(overrides: dict[str, object]) -> None:
    with pytest.raises((ValidationError, TypeError)):
        make_settings(**overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "market_cache_capacity": 10,
            "polling_bootstrap_limit": 10,
            "shadow_history_limit": 10,
            "decision_coordinator_history_limit": 11,
        },
        {"paper_initial_quote_balance": Decimal(0)},
        {"paper_initial_quote_balance": Decimal("NaN")},
        {"instrument_price_tick": Decimal(0)},
        {"instrument_quantity_step": Decimal(0)},
        {"instrument_min_quantity": Decimal(0)},
        {"instrument_min_notional": Decimal(0)},
        {"risk_max_order_notional": Decimal(0)},
        {"risk_max_position_notional": Decimal(0)},
        {"paper_fee_rate": Decimal(-1)},
        {"paper_fee_rate": Decimal(1)},
        {"risk_max_fee_rate": Decimal(-1)},
        {"risk_max_fee_rate": Decimal(1)},
        {"paper_fee_rate": Decimal("0.002"), "risk_max_fee_rate": Decimal("0.001")},
        {"risk_max_order_notional": Decimal(2), "risk_max_position_notional": Decimal(1)},
        {"risk_max_slippage_bps": Decimal(-1)},
        {"paper_fee_rate": 0.1},
        {"paper_trading_enabled": True, "persistence_enabled": False},
        {"sqlite_busy_timeout_ms": 0},
        {"portfolio_checkpoint_interval_transitions": 0},
        {"paper_portfolio_id": " "},
        {"sqlite_path": ""},
    ],
)
def test_invalid_paper_runtime_configuration(overrides: dict[str, object]) -> None:
    with pytest.raises((ValidationError, TypeError)):
        make_settings(**overrides)


def test_shadow_only_does_not_require_persistence() -> None:
    settings = make_settings(shadow_decisions_enabled=True, persistence_enabled=False)
    assert settings.paper_trading_enabled is False
    assert settings.persistence_enabled is False
