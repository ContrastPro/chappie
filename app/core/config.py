from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Chappie Trader"
    app_version: str = "0.1.0"
    environment: str = "development"

    log_level: str = "INFO"
    log_directory: Path = Path("logs")
    run_log_file: Path = Path("logs/latest_run.log")

    trading_mode: Literal["paper"] = Field(
        default="paper",
        description="Stage 1 supports paper mode only",
    )

    exchange_name: str = "binance"
    binance_market_base_url: str = "https://data-api.binance.vision"
    default_symbol: str = "BTCUSDT"
    default_interval: str = "1m"

    market_cache_capacity: int = Field(default=1000, gt=0)
    polling_cadence_seconds: float = Field(default=10.0, gt=0)
    polling_bootstrap_limit: int = Field(default=1000, gt=0, le=1000)
    polling_regular_limit: int = Field(default=2, ge=2)
    max_consecutive_failures: int = Field(default=3, gt=0)
    market_clock_skew_warning_seconds: float = Field(default=5.0, ge=0)
    smoke_run_cycles: int | None = Field(default=None, gt=0)

    shadow_decisions_enabled: bool = False
    shadow_history_limit: int = Field(default=1000, gt=0)
    shadow_dedup_capacity: int = Field(default=10000, gt=0)
    snapshot_stale_after_ms: int = Field(default=120000, gt=0)
    ema_fast_window: int = Field(default=12, ge=2)
    ema_slow_window: int = Field(default=26, gt=2)
    long_target_exposure: Decimal = Decimal(1)

    binance_api_key: str | None = None
    binance_secret_key: str | None = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @model_validator(mode="after")
    def validate_market_data_configuration(self) -> "Settings":
        if self.polling_bootstrap_limit > self.market_cache_capacity:
            raise ValueError("polling_bootstrap_limit must not exceed market_cache_capacity")
        if self.shadow_history_limit > self.market_cache_capacity:
            raise ValueError("shadow_history_limit must not exceed market_cache_capacity")
        if self.ema_slow_window <= self.ema_fast_window:
            raise ValueError("ema_slow_window must be greater than ema_fast_window")
        if not self.long_target_exposure.is_finite() or not (
            Decimal(0) < self.long_target_exposure <= Decimal(1)
        ):
            raise ValueError("long_target_exposure must be finite and in (0, 1]")
        return self

    @field_validator("long_target_exposure", mode="before")
    @classmethod
    def reject_float_exposure(cls, value: object) -> object:
        if isinstance(value, float):
            raise TypeError("long_target_exposure must not be a float")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
