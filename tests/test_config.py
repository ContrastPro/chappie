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


def test_live_mode_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make_settings(trading_mode="live")


def test_regular_limit_may_be_greater_than_two() -> None:
    assert make_settings(polling_regular_limit=3).polling_regular_limit == 3
