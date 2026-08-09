import logging
from collections.abc import Callable
from decimal import Decimal

import pytest

from app.application import build_application, run_application
from app.coordination import TradingCoordinationStateError
from app.core.config import Settings
from app.market.cache import MarketStreamKey
from app.market.events import (
    CandleClosed,
    CandleClosedReason,
    MarketEventSource,
)
from app.market.exceptions import MarketProviderHTTPError
from app.market.models import Candle, CandleSnapshot, ServerTime, TickerPrice
from app.market.polling import PollingLifecycleState
from app.paper import PaperTradingCycleOutcome
from app.paper.exceptions import PaperTradingConfigurationError
from app.persistence.exceptions import PersistenceWriterConflictError

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


class ClosingCandleProvider(ApplicationProvider):
    def __init__(self, response: list[Candle]) -> None:
        super().__init__(response)
        self.server_times = iter((OPEN_TIME_MS + 30_000, OPEN_TIME_MS + 60_000))

    def get_server_time(self) -> ServerTime:
        return ServerTime(timestamp_ms=next(self.server_times))


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
        "shadow_history_limit": 10,
        "decision_coordinator_history_limit": 10,
        **overrides,
    }
    if values.get("paper_trading_enabled") is True:
        values.setdefault("persistence_enabled", True)
        values.setdefault("sqlite_path", ":memory:")
    return Settings(_env_file=None, **values)


def test_composition_runs_finite_smoke_and_closes_provider_once() -> None:
    provider = ApplicationProvider([make_candle()])
    application = build_application(
        make_settings(),
        provider=provider,
        clock=FixedClock(),
        sleeper=NoWaitSleeper(),
    )
    assert application.coordinator is None

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


def test_shadow_composition_is_feature_gated_and_stops_before_provider_close() -> None:
    provider = ApplicationProvider([make_candle()])
    settings = make_settings(
        shadow_decisions_enabled=True,
        shadow_history_limit=10,
    )
    application = build_application(
        settings,
        provider=provider,
        clock=FixedClock(),
        sleeper=NoWaitSleeper(),
    )
    assert application.coordinator is not None
    assert application.shadow_consumer is not None
    assert application.coordinator.status.lifecycle.value == "RUNNING"
    assert application.status_report().shadow_enabled
    application.close()
    assert application.coordinator.status.lifecycle.value == "STOPPED"
    assert provider.close_calls == 1


@pytest.mark.parametrize(
    ("shadow_enabled", "paper_enabled"),
    ((False, False), (True, False), (False, True), (True, True)),
)
def test_shared_decision_feature_matrix(
    shadow_enabled: bool,
    paper_enabled: bool,
) -> None:
    provider = ApplicationProvider([make_candle()])
    application = build_application(
        make_settings(
            shadow_decisions_enabled=shadow_enabled,
            paper_trading_enabled=paper_enabled,
        ),
        provider=provider,
        clock=FixedClock(),
        sleeper=NoWaitSleeper(),
    )

    assert (application.coordinator is not None) is (shadow_enabled or paper_enabled)
    assert (application.shadow_consumer is not None) is shadow_enabled
    assert (application.paper_engine is not None) is paper_enabled
    assert (application.paper_portfolio is not None) is paper_enabled
    decision_subscriptions = [
        subscription
        for subscription in application.dispatcher._subscriptions
        if subscription.event_type is CandleClosed
        and application.coordinator is not None
        and getattr(subscription.handler, "__self__", None) is application.coordinator
    ]
    assert len(decision_subscriptions) == int(shadow_enabled or paper_enabled)
    if application.coordinator is not None:
        assert application.coordinator.status.lifecycle.value == "RUNNING"
    report = application.status_report()
    assert report.coordinator_enabled is (shadow_enabled or paper_enabled)
    assert report.shadow_enabled is shadow_enabled
    assert report.paper_enabled is paper_enabled
    assert (report.paper_health is not None) is paper_enabled
    assert (report.paper_quote_balance is not None) is paper_enabled
    application.close()


def test_combined_runtime_uses_real_pipeline_shadow_and_paper_once() -> None:
    provider = ApplicationProvider([make_candle()])
    application = build_application(
        make_settings(shadow_decisions_enabled=True, paper_trading_enabled=True),
        provider=provider,
        clock=FixedClock(),
        sleeper=NoWaitSleeper(),
    )
    key = MarketStreamKey("BTCUSDT", "1m")
    source = make_candle()
    occurred_at_ms = source.close_time_ms + 1
    application.cache.upsert_candle(key, source, occurred_at_ms)
    delivery = application.dispatcher.publish(
        CandleClosed(
            event_id="runtime-close-1",
            occurred_at_ms=occurred_at_ms,
            stream_key=key,
            correlation_cycle_id="runtime-cycle-1",
            source=MarketEventSource.REPLAY,
            candle=CandleSnapshot.from_candle(source),
            reason=CandleClosedReason.TIME_CONFIRMED,
        )
    )

    assert delivery.failure_count == 0
    assert application.coordinator is not None
    assert application.coordinator.status.successful_decisions == 1
    assert application.shadow_consumer is not None
    assert application.shadow_consumer.status.successful_records == 1
    assert application.paper_engine is not None
    assert application.paper_engine.last_result is not None
    assert application.paper_engine.last_result.outcome is PaperTradingCycleOutcome.NO_INTENT
    assert application.paper_portfolio is not None
    assert application.paper_portfolio.snapshot.version == 1
    report = application.status_report()
    assert report.coordinator_shadow_deliveries == 1
    assert report.coordinator_paper_deliveries == 1
    assert report.coordinator_paper_unavailable == 0
    assert report.paper_last_outcome == PaperTradingCycleOutcome.NO_INTENT.value
    assert report.paper_last_fill_id is None
    application.close()


@pytest.mark.parametrize(
    ("shadow_enabled", "paper_enabled"),
    ((True, False), (False, True), (True, True)),
)
def test_offline_polling_closure_reaches_only_enabled_shared_consumers(
    shadow_enabled: bool,
    paper_enabled: bool,
) -> None:
    provider = ClosingCandleProvider([make_candle()])
    application = build_application(
        make_settings(
            shadow_decisions_enabled=shadow_enabled,
            paper_trading_enabled=paper_enabled,
        ),
        provider=provider,
        clock=FixedClock(),
        sleeper=NoWaitSleeper(),
    )

    bootstrap = application.engine.run_once()
    regular = application.engine.run_once()

    assert bootstrap.bootstrap
    assert regular.bootstrap is False
    assert application.coordinator is not None
    assert application.coordinator.status.successful_decisions == 1
    assert application.coordinator.status.hold_decisions == 1
    assert application.coordinator.status.shadow_deliveries == int(shadow_enabled)
    assert application.coordinator.status.paper_deliveries == int(paper_enabled)
    if shadow_enabled:
        assert application.shadow_consumer is not None
        assert application.shadow_consumer.status.successful_records == 1
    if paper_enabled:
        assert application.paper_engine is not None
        assert application.paper_engine.last_result is not None
        assert application.paper_engine.last_result.outcome is PaperTradingCycleOutcome.NO_INTENT
        assert application.paper_portfolio is not None
        assert application.paper_portfolio.snapshot.version == 1
    application.close()


def test_application_restores_paper_portfolio_before_coordinator_start(tmp_path) -> None:
    database = tmp_path / "runtime.sqlite3"
    settings = make_settings(
        paper_trading_enabled=True,
        persistence_enabled=True,
        sqlite_path=database,
    )
    first = build_application(
        settings,
        provider=ApplicationProvider([make_candle()]),
        clock=FixedClock(),
        sleeper=NoWaitSleeper(),
    )
    first_snapshot = first.paper_portfolio.snapshot  # type: ignore[union-attr]
    first.close()

    second = build_application(
        settings,
        provider=ApplicationProvider([make_candle()]),
        clock=FixedClock(),
        sleeper=NoWaitSleeper(),
    )
    assert second.paper_portfolio.snapshot == first_snapshot  # type: ignore[union-attr]
    assert second.recovery_result is not None
    assert second.coordinator is not None
    assert second.coordinator.status.lifecycle.value == "RUNNING"
    second.close()


def test_application_config_mismatch_fails_before_runtime_subscription(tmp_path) -> None:
    database = tmp_path / "mismatch.sqlite3"
    first = build_application(
        make_settings(
            paper_trading_enabled=True,
            persistence_enabled=True,
            sqlite_path=database,
        ),
        provider=ApplicationProvider([make_candle()]),
        clock=FixedClock(),
        sleeper=NoWaitSleeper(),
    )
    first.close()
    provider = ApplicationProvider([make_candle()])
    with pytest.raises(PaperTradingConfigurationError, match="recovery"):
        build_application(
            make_settings(
                paper_trading_enabled=True,
                persistence_enabled=True,
                sqlite_path=database,
                paper_initial_quote_balance=Decimal(9999),
            ),
            provider=provider,
            clock=FixedClock(),
            sleeper=NoWaitSleeper(),
        )
    assert provider.close_calls == 1


def test_second_application_writer_is_rejected_before_polling(tmp_path) -> None:
    database = tmp_path / "writer.sqlite3"
    settings = make_settings(
        paper_trading_enabled=True,
        persistence_enabled=True,
        sqlite_path=database,
        sqlite_busy_timeout_ms=1,
    )
    first = build_application(
        settings,
        provider=ApplicationProvider([make_candle()]),
        clock=FixedClock(),
        sleeper=NoWaitSleeper(),
    )
    second_provider = ApplicationProvider([make_candle()])
    with pytest.raises(PersistenceWriterConflictError):
        build_application(
            settings,
            provider=second_provider,
            clock=FixedClock(),
            sleeper=NoWaitSleeper(),
        )
    assert second_provider.close_calls == 1
    first.close()


def test_shadow_unsubscribe_failure_keeps_state_explicit_and_still_closes_provider(
    monkeypatch,
) -> None:
    provider = ApplicationProvider([make_candle()])
    application = build_application(
        make_settings(shadow_decisions_enabled=True),
        provider=provider,
        clock=FixedClock(),
        sleeper=NoWaitSleeper(),
    )

    def fail_unsubscribe(token):
        del token
        raise RuntimeError("dispatcher unavailable")

    monkeypatch.setattr(application.dispatcher, "unsubscribe", fail_unsubscribe)
    with pytest.raises(TradingCoordinationStateError, match="unsubscribe"):
        application.close()
    assert provider.close_calls == 1
    assert application.coordinator is not None
    assert application.coordinator.status.lifecycle.value == "RUNNING"
    application.close()
    assert provider.close_calls == 1


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
