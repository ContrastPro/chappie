from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Chappie Trader"
    app_version: str = "0.1.0"
    environment: str = "development"

    log_level: str = "INFO"
    log_directory: Path = Path("logs")

    trading_mode: str = Field(
        default="paper",
        description="Trading mode: paper or live",
    )

    exchange_name: str = "binance"
    binance_market_base_url: str = "https://data-api.binance.vision"
    default_symbol: str = "BTCUSDT"
    default_interval: str = "1m"

    binance_api_key: str | None = None
    binance_secret_key: str | None = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()