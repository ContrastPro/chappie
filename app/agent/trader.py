import logging

from app.core.config import Settings


class TraderAgent:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
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

        if self._settings.trading_mode == "live":
            self._logger.warning(
                "Live mode is configured, but real trading is not implemented"
            )

        self._is_running = True
        self._logger.info("TraderAgent started successfully")

    def stop(self) -> None:
        if not self._is_running:
            self._logger.warning("TraderAgent is already stopped")
            return

        self._logger.info("Stopping TraderAgent")
        self._is_running = False
        self._logger.info("TraderAgent stopped successfully")