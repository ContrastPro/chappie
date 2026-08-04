import logging
from dataclasses import dataclass

from app.agent.trader import TraderAgent
from app.core.config import Settings
from app.market.binance_provider import BinanceMarketProvider
from app.market.cache import InMemoryMarketCache, MarketStreamKey
from app.market.event_logging import register_market_event_logging
from app.market.events import (
    CorrelationIdFactory,
    EventIdFactory,
    MarketEventDispatcher,
    MarketEventProjector,
    SubscriptionToken,
    UUIDCorrelationIdFactory,
    UUIDEventIdFactory,
)
from app.market.models import CandleSnapshot
from app.market.polling import (
    Clock,
    PollingLifecycleState,
    PollingPolicy,
    Sleeper,
    StopAwareSleeper,
    SynchronousRestPollingEngine,
    SystemClock,
)
from app.market.provider import MarketProvider


@dataclass(frozen=True, slots=True)
class StageOneStatusReport:
    lifecycle_state: PollingLifecycleState
    completed_cycles: int
    successful_cycles: int
    failed_cycles: int
    consecutive_failures: int
    last_success_time_ms: int | None
    last_error_category: str | None
    cached_candle_count: int
    latest_candle: CandleSnapshot | None
    latest_closed_candle: CandleSnapshot | None
    published_event_count: int
    delivery_failure_count: int
    last_exchange_time_ms: int | None
    last_clock_skew_ms: int | None


@dataclass(slots=True)
class ChappieTraderApplication:
    settings: Settings
    provider: MarketProvider
    cache: InMemoryMarketCache
    dispatcher: MarketEventDispatcher
    projector: MarketEventProjector
    engine: SynchronousRestPollingEngine
    agent: TraderAgent
    clock: Clock
    logging_subscriptions: tuple[SubscriptionToken, ...]
    _provider_closed: bool = False

    def close(self) -> None:
        """Idempotently release resources owned by the composition root."""

        if self._provider_closed:
            return
        self._provider_closed = True
        self.provider.close()

    def status_report(self) -> StageOneStatusReport:
        status = self.engine.status
        stream_key = status.stream_key
        snapshot = self.cache.get_snapshot(stream_key)
        latest = self.cache.get_latest_candle(stream_key)
        market_as_of_ms = (
            status.last_exchange_time_ms if status.last_exchange_time_ms is not None else 0
        )
        latest_closed = self.cache.get_latest_closed_candle(
            stream_key,
            market_as_of_ms,
        )
        return StageOneStatusReport(
            lifecycle_state=status.lifecycle_state,
            completed_cycles=status.completed_cycles,
            successful_cycles=status.successful_cycles,
            failed_cycles=status.failed_cycles,
            consecutive_failures=status.consecutive_failures,
            last_success_time_ms=status.last_success_time_ms,
            last_error_category=(
                status.last_error_category.value if status.last_error_category is not None else None
            ),
            cached_candle_count=len(snapshot),
            latest_candle=(CandleSnapshot.from_candle(latest) if latest is not None else None),
            latest_closed_candle=(
                CandleSnapshot.from_candle(latest_closed) if latest_closed is not None else None
            ),
            published_event_count=status.published_event_count,
            delivery_failure_count=status.delivery_failure_count,
            last_exchange_time_ms=status.last_exchange_time_ms,
            last_clock_skew_ms=status.last_clock_skew_ms,
        )


def build_application(
    settings: Settings,
    *,
    provider: MarketProvider | None = None,
    clock: Clock | None = None,
    sleeper: Sleeper | None = None,
    projector_event_id_factory: EventIdFactory | None = None,
    diagnostic_event_id_factory: EventIdFactory | None = None,
    correlation_id_factory: CorrelationIdFactory | None = None,
    event_logger: logging.Logger | None = None,
) -> ChappieTraderApplication:
    """Create and wire the complete Stage 1 market-data application."""

    market_provider = provider or BinanceMarketProvider(
        base_url=settings.binance_market_base_url,
    )
    market_cache = InMemoryMarketCache(settings.market_cache_capacity)
    dispatcher = MarketEventDispatcher()
    projector = MarketEventProjector(projector_event_id_factory or UUIDEventIdFactory())
    production_clock = clock or SystemClock()
    production_sleeper = sleeper or StopAwareSleeper()
    policy = PollingPolicy(
        cadence_seconds=settings.polling_cadence_seconds,
        max_bootstrap_limit=settings.polling_bootstrap_limit,
        regular_limit=settings.polling_regular_limit,
        max_failed_cycles=settings.max_consecutive_failures,
        clock_skew_warning_seconds=(settings.market_clock_skew_warning_seconds),
    )
    stream_key = MarketStreamKey(
        settings.default_symbol,
        settings.default_interval,
    )
    engine = SynchronousRestPollingEngine(
        market_provider,
        market_cache,
        stream_key,
        policy=policy,
        clock=production_clock,
        sleeper=production_sleeper,
        dispatcher=dispatcher,
        projector=projector,
        event_id_factory=diagnostic_event_id_factory or UUIDEventIdFactory(),
        correlation_id_factory=(correlation_id_factory or UUIDCorrelationIdFactory()),
    )
    agent = TraderAgent(settings=settings, polling_engine=engine)
    subscriptions = register_market_event_logging(
        dispatcher,
        event_logger or logging.getLogger("MarketEvents"),
    )
    return ChappieTraderApplication(
        settings=settings,
        provider=market_provider,
        cache=market_cache,
        dispatcher=dispatcher,
        projector=projector,
        engine=engine,
        agent=agent,
        clock=production_clock,
        logging_subscriptions=subscriptions,
    )


def log_stage_one_status(
    report: StageOneStatusReport,
    logger: logging.Logger,
) -> None:
    logger.info(
        "Stage 1 status lifecycle=%s completed=%s successful=%s failed=%s "
        "consecutive_failures=%s last_success_time_ms=%s last_error=%s "
        "cached_candles=%s latest_candle=%s latest_closed_candle=%s "
        "published_events=%s delivery_failures=%s last_exchange_time_ms=%s "
        "last_clock_skew_ms=%s",
        report.lifecycle_state.value,
        report.completed_cycles,
        report.successful_cycles,
        report.failed_cycles,
        report.consecutive_failures,
        report.last_success_time_ms,
        report.last_error_category,
        report.cached_candle_count,
        report.latest_candle,
        report.latest_closed_candle,
        report.published_event_count,
        report.delivery_failure_count,
        report.last_exchange_time_ms,
        report.last_clock_skew_ms,
    )


def run_application(
    application: ChappieTraderApplication,
    logger: logging.Logger,
) -> int:
    """Run, report and clean up the application, returning a process exit code."""

    exit_code = 0
    interrupted = False
    try:
        application.agent.start()
        status = application.engine.status
        if status.lifecycle_state is PollingLifecycleState.FAILED or status.failed_cycles > 0:
            exit_code = 1
    except KeyboardInterrupt:
        interrupted = True
        logger.info("Manual stop requested")
        application.agent.stop()
    except Exception:
        exit_code = 1
        logger.exception("Fatal application error")
    finally:
        application.agent.stop()
        try:
            application.close()
        except Exception:
            exit_code = 1
            logger.exception("Application resource cleanup failed")
        log_stage_one_status(application.status_report(), logger)

    if interrupted and exit_code == 0:
        logger.info("Application stopped cleanly by user")
    return exit_code
