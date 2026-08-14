from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Chappie Trader"
    app_version: str = "0.1.0"
    environment: str = "development"

    log_level: str = "INFO"
    log_directory: Path = Path("logs")
    run_log_file: Path = Path("logs/latest_run.log")

    trading_mode: Literal["paper", "testnet", "live"] = Field(
        default="paper",
        description="Explicit execution mode; LIVE is structurally unavailable",
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
    paper_trading_enabled: bool = False
    persistence_enabled: bool = False
    persistence_backend: Literal["sqlite"] = "sqlite"
    sqlite_path: Path = Path("data/chappie-paper.sqlite3")
    sqlite_busy_timeout_ms: int = Field(default=5000, gt=0)
    portfolio_checkpoint_interval_transitions: int = Field(default=100, gt=0)
    decision_coordinator_history_limit: int = Field(default=1000, gt=0)
    decision_coordinator_dedup_capacity: int = Field(default=10000, gt=0)
    shadow_history_limit: int = Field(default=1000, gt=0)
    shadow_dedup_capacity: int = Field(default=10000, gt=0)
    snapshot_stale_after_ms: int = Field(default=120000, gt=0)
    ema_fast_window: int = Field(default=12, ge=2)
    ema_slow_window: int = Field(default=26, gt=2)
    long_target_exposure: Decimal = Decimal(1)

    paper_portfolio_id: str = "paper-btcusdt"
    paper_initial_quote_balance: Decimal = Decimal(10_000)
    paper_fee_rate: Decimal = Decimal("0.001")
    paper_execution_latency_ms: int = Field(default=0, ge=0)
    paper_dedup_capacity: int = Field(default=10000, gt=0)
    portfolio_fill_dedup_capacity: int = Field(default=10000, gt=0)
    instrument_price_tick: Decimal = Decimal("0.01")
    instrument_quantity_step: Decimal = Decimal("0.00001")
    instrument_min_quantity: Decimal = Decimal("0.00001")
    instrument_max_quantity: Decimal = Decimal(1000)
    instrument_min_notional: Decimal = Decimal(5)
    instrument_base_precision: int = Field(default=8, ge=0)
    instrument_quote_precision: int = Field(default=2, ge=0)
    risk_max_order_notional: Decimal = Decimal(1_000)
    risk_max_position_notional: Decimal = Decimal(5_000)
    risk_max_fee_rate: Decimal = Decimal("0.001")
    risk_max_slippage_bps: Decimal = Decimal(5)

    binance_testnet_api_key: SecretStr | None = None
    binance_testnet_secret_key: SecretStr | None = None
    binance_testnet_account_fingerprint: str | None = None
    binance_testnet_base_url: str = "https://testnet.binance.vision"
    testnet_execution_enabled: bool = False
    testnet_execution_root_id: str = "testnet-btcusdt"
    testnet_session_id: str = "testnet-btcusdt-session"
    testnet_portfolio_id: str = "testnet-btcusdt-portfolio"
    testnet_initial_quote_balance: Decimal = Decimal(10_000)
    testnet_authorization_max_age_ms: int = Field(default=120_000, gt=0)
    testnet_max_session_submitted_notional: Decimal = Decimal(10_000)
    testnet_max_session_executed_turnover: Decimal = Decimal(20_000)
    testnet_max_session_submission_count: int = Field(default=100, gt=0)
    testnet_max_session_execution_failures: int = Field(default=5, gt=0)
    testnet_max_drawdown: Decimal = Decimal("0.20")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @model_validator(mode="after")
    def validate_market_data_configuration(self) -> "Settings":
        if self.trading_mode == "live":
            raise ValueError("LIVE execution is not implemented")
        if self.trading_mode == "testnet" and not self.testnet_execution_enabled:
            raise ValueError("testnet mode requires testnet_execution_enabled")
        if self.trading_mode == "testnet" and (
            self.binance_testnet_api_key is None
            or not self.binance_testnet_api_key.get_secret_value().strip()
            or self.binance_testnet_secret_key is None
            or not self.binance_testnet_secret_key.get_secret_value().strip()
            or self.binance_testnet_account_fingerprint is None
            or not self.binance_testnet_account_fingerprint.strip()
        ):
            raise ValueError("testnet mode requires dedicated credentials and account fingerprint")
        if self.trading_mode == "testnet" and not self.persistence_enabled:
            raise ValueError("testnet mode requires persistence_enabled")
        if self.trading_mode == "testnet" and self.paper_trading_enabled:
            raise ValueError("paper and testnet execution cannot be enabled together")
        if self.trading_mode != "testnet" and self.testnet_execution_enabled:
            raise ValueError("testnet_execution_enabled requires testnet mode")
        if self.binance_testnet_base_url != "https://testnet.binance.vision":
            raise ValueError("only the Binance Spot Testnet execution URL is allowed")
        if self.paper_trading_enabled and not self.persistence_enabled:
            raise ValueError("paper_trading_enabled requires persistence_enabled")
        if not self.paper_portfolio_id.strip():
            raise ValueError("paper_portfolio_id must be non-empty")
        if any(
            not value.strip()
            for value in (
                self.testnet_execution_root_id,
                self.testnet_session_id,
                self.testnet_portfolio_id,
            )
        ):
            raise ValueError("Testnet durable lineage identifiers must be non-empty")
        if (
            not str(self.sqlite_path).strip()
            or self.sqlite_path.exists()
            and self.sqlite_path.is_dir()
        ):
            raise ValueError("sqlite_path must identify a database file")
        if self.polling_bootstrap_limit > self.market_cache_capacity:
            raise ValueError("polling_bootstrap_limit must not exceed market_cache_capacity")
        if self.shadow_history_limit > self.market_cache_capacity:
            raise ValueError("shadow_history_limit must not exceed market_cache_capacity")
        if self.decision_coordinator_history_limit > self.market_cache_capacity:
            raise ValueError(
                "decision_coordinator_history_limit must not exceed market_cache_capacity"
            )
        if self.ema_slow_window <= self.ema_fast_window:
            raise ValueError("ema_slow_window must be greater than ema_fast_window")
        if not self.long_target_exposure.is_finite() or not (
            Decimal(0) < self.long_target_exposure <= Decimal(1)
        ):
            raise ValueError("long_target_exposure must be finite and in (0, 1]")
        decimal_fields = (
            "paper_initial_quote_balance",
            "paper_fee_rate",
            "instrument_price_tick",
            "instrument_quantity_step",
            "instrument_min_quantity",
            "instrument_max_quantity",
            "instrument_min_notional",
            "risk_max_order_notional",
            "risk_max_position_notional",
            "risk_max_fee_rate",
            "risk_max_slippage_bps",
            "testnet_initial_quote_balance",
            "testnet_max_session_submitted_notional",
            "testnet_max_session_executed_turnover",
            "testnet_max_drawdown",
        )
        if any(not getattr(self, name).is_finite() for name in decimal_fields):
            raise ValueError("paper financial settings must be finite")
        if self.paper_initial_quote_balance <= 0:
            raise ValueError("paper_initial_quote_balance must be positive")
        for name in (
            "instrument_price_tick",
            "instrument_quantity_step",
            "instrument_min_quantity",
            "instrument_max_quantity",
            "instrument_min_notional",
            "risk_max_order_notional",
            "risk_max_position_notional",
            "testnet_initial_quote_balance",
            "testnet_max_session_submitted_notional",
            "testnet_max_session_executed_turnover",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if not (Decimal(0) <= self.paper_fee_rate < Decimal(1)):
            raise ValueError("paper_fee_rate must be in [0, 1)")
        if not (Decimal(0) <= self.risk_max_fee_rate < Decimal(1)):
            raise ValueError("risk_max_fee_rate must be in [0, 1)")
        if self.paper_fee_rate > self.risk_max_fee_rate:
            raise ValueError("paper_fee_rate must not exceed risk_max_fee_rate")
        if self.risk_max_position_notional < self.risk_max_order_notional:
            raise ValueError("risk position cap must not be below order cap")
        if self.risk_max_slippage_bps < 0:
            raise ValueError("risk_max_slippage_bps must not be negative")
        if not (Decimal(0) < self.testnet_max_drawdown < Decimal(1)):
            raise ValueError("testnet_max_drawdown must be in (0, 1)")
        return self

    @field_validator(
        "long_target_exposure",
        "paper_initial_quote_balance",
        "paper_fee_rate",
        "instrument_price_tick",
        "instrument_quantity_step",
        "instrument_min_quantity",
        "instrument_max_quantity",
        "instrument_min_notional",
        "risk_max_order_notional",
        "risk_max_position_notional",
        "risk_max_fee_rate",
        "risk_max_slippage_bps",
        "testnet_initial_quote_balance",
        "testnet_max_session_submitted_notional",
        "testnet_max_session_executed_turnover",
        "testnet_max_drawdown",
        mode="before",
    )
    @classmethod
    def reject_float_exposure(cls, value: object) -> object:
        if isinstance(value, float):
            raise TypeError("financial settings must not use float")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
