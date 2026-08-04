import logging
from typing import Protocol

from app.core.config import Settings


class PollingEngine(Protocol):
    def run_forever(self) -> None:
        """Run until stopped or failed."""

    def run_cycles(self, count: int) -> None:
        """Run a finite diagnostic session."""

    def request_stop(self) -> None:
        """Request graceful termination."""


class TraderAgent:
    def __init__(
        self,
        settings: Settings,
        polling_engine: PollingEngine,
    ) -> None:
        self._settings = settings
        self._polling_engine = polling_engine
        self._logger = logging.getLogger(self.__class__.__name__)
        self._is_running = False

    @property
    def is_running(self) -> bool:
        return self._is_running

    def start(self) -> None:
        if self._is_running:
            self._logger.warning("TraderAgent is already running")
            return

        self._logger.info("Starting %s", self._settings.app_name)
        self._logger.info("Version: %s", self._settings.app_version)
        self._logger.info("Environment: %s", self._settings.environment)
        self._logger.info("Trading mode: %s", self._settings.trading_mode)
        self._logger.info("Exchange: %s", self._settings.exchange_name)
        self._logger.info("Default symbol: %s", self._settings.default_symbol)
        self._logger.info("Default interval: %s", self._settings.default_interval)

        self._is_running = True
        smoke_run_cycles = self._settings.smoke_run_cycles
        if smoke_run_cycles is None:
            self._logger.info("TraderAgent started in continuous polling mode")
            self._polling_engine.run_forever()
        else:
            self._logger.info(
                "TraderAgent started in smoke-run mode: cycles=%s",
                smoke_run_cycles,
            )
            self._polling_engine.run_cycles(smoke_run_cycles)
        self._is_running = False
        self._logger.info("TraderAgent polling finished")

    def stop(self) -> None:
        if self._is_running:
            self._logger.info("Stopping TraderAgent")
        self._polling_engine.request_stop()
        self._is_running = False
        self._logger.info("TraderAgent stop requested")
