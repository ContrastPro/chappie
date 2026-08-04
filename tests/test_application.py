import logging
from collections.abc import Callable
from decimal import Decimal

from app.application import build_application, run_application
from app.core.config import Settings
from app.market.exceptions import MarketProviderHTTPError
from app.market.models import Candle, ServerTime, TickerPrice
from app.market.polling import PollingLifecycleState

OPEN_TIME_MS = 1_700_000_000_000


def make_candle() -> Candle:
    return Candle(
        open_time_ms=OPEN_TIME_MS,
        close_time_ms=OPEN_TIME_MS + 59_999,
        open=Decimal(100),
        high=Decimal(110),
        low=Decimal(90),
        close=Decimal(105),
        volume=Decimal(10),
    )


class ApplicationProvider:
    def __init__(self, response: list[Candle] | BaseException) -> None:
        self.response = response
        self.calls = 0
        self.close_calls = 0

    def get_server_time(self) -> ServerTime:
        return ServerTime(timestamp_ms=OPEN_TIME_MS + 120_000)

    def get_ticker_price(self, symbol: str) -> TickerPrice:
        return TickerPrice(symbol=symbol, price=Decimal(100))

    def get_candles(self, symbol: str, interval: str, limit: int) -> list[Candle]:
        del symbol, interval, limit
        self.calls += 1
        if isinstance(self.response, BaseException):
            raise self.response
        return [candle.model_copy(deep=True) for candle in self.response]

    def close(self) -> None:
        self.close_calls += 1


class FixedClock:
    def now_ms(self) -> int:
        return OPEN_TIME_MS + 120_000

    def monotonic(self) -> float:
        return 0.0


class NoWaitSleeper:
    def __init__(self) -> None:
        self.waits: list[float] = []

    def wait(
        self,
        seconds: float,
        stop_requested: Callable[[], bool],
    ) -> bool:
        self.waits.append(seconds)
        return not stop_requested()


def make_settings(**overrides: object) -> Settings:
    values = {
        "smoke_run_cycles": 1,
        "polling_bootstrap_limit": 10,
        "market_cache_capacity": 10,
        **overrides,
    }
    return Settings(_env_file=None, **values)


def test_composition_runs_finite_smoke_and_closes_provider_once() -> None:
    provider = ApplicationProvider([make_candle()])
    application = build_application(
        make_settings(),
        provider=provider,
        clock=FixedClock(),
        sleeper=NoWaitSleeper(),
    )

    exit_code = run_application(application, logging.getLogger("test"))
    report = application.status_report()
    application.close()

    assert exit_code == 0
    assert provider.calls == 1
    assert provider.close_calls == 1
    assert report.lifecycle_state is PollingLifecycleState.STOPPED
    assert report.completed_cycles == report.successful_cycles == 1
    assert report.failed_cycles == 0
    assert report.cached_candle_count == 1
    assert report.latest_candle is not None
    assert report.latest_closed_candle is not None
    assert report.published_event_count == 1
    assert report.delivery_failure_count == 0
    assert report.last_exchange_time_ms == OPEN_TIME_MS + 120_000
    assert report.last_clock_skew_ms == 0


def test_fatal_provider_error_returns_nonzero_and_closes_provider() -> None:
    provider = ApplicationProvider(MarketProviderHTTPError(400))
    application = build_application(
        make_settings(),
        provider=provider,
        clock=FixedClock(),
        sleeper=NoWaitSleeper(),
    )

    exit_code = run_application(application, logging.getLogger("test"))

    assert exit_code == 1
    assert application.engine.status.lifecycle_state is PollingLifecycleState.FAILED
    assert provider.close_calls == 1


def test_keyboard_interrupt_is_graceful_and_closes_provider_once() -> None:
    provider = ApplicationProvider(KeyboardInterrupt())
    application = build_application(
        make_settings(smoke_run_cycles=None),
        provider=provider,
        clock=FixedClock(),
        sleeper=NoWaitSleeper(),
    )

    exit_code = run_application(application, logging.getLogger("test"))

    assert exit_code == 0
    assert application.engine.status.lifecycle_state is PollingLifecycleState.STOPPED
    assert provider.close_calls == 1
    application.close()
    assert provider.close_calls == 1
