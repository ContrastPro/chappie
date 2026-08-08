import logging
from dataclasses import dataclass
from decimal import Decimal

from app.agent.trader import TraderAgent
from app.coordination import TradingDecisionCoordinator, UUIDCoordinationIdFactory
from app.core.config import Settings
from app.execution import (
    PaperExecutionConfig,
    PaperExecutionProvider,
    UUIDExecutionIdFactory,
    UUIDFillIdFactory,
)
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
from app.paper import (
    LoggingPaperTradingObserver,
    PaperTradingEngine,
    PaperTradingEngineConfig,
    UUIDPaperTradingCycleIdFactory,
)
from app.portfolio import InstrumentRules, Portfolio, PortfolioConfig
from app.risk import PortfolioRiskPolicy, RiskManager, UUIDRiskDecisionIdFactory
from app.shadow import (
    LoggingShadowDecisionObserver,
    ShadowDecisionConsumer,
    UUIDShadowRunIdFactory,
    UUIDSnapshotIdFactory,
)
from app.strategies.ema_crossover import EmaCrossoverConfig, EmaCrossoverStrategy
from app.trading import (
    LongOnlyIntentPlanner,
    LongOnlyIntentPlannerConfig,
    MarketSnapshotFactory,
    TradingDecisionPipeline,
    UUIDIntentIdFactory,
    UUIDPipelineRunIdFactory,
    UUIDSignalIdFactory,
)


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
    shadow_enabled: bool = False
    shadow_lifecycle: str | None = None
    shadow_handled_events: int = 0
    shadow_successful_decisions: int = 0
    shadow_hold_decisions: int = 0
    shadow_intent_decisions: int = 0
    shadow_declined_decisions: int = 0
    shadow_failed_decisions: int = 0
    shadow_observation_failures: int = 0
    shadow_ignored_duplicates: int = 0
    shadow_last_event_id: str | None = None
    shadow_last_signal_direction: str | None = None
    shadow_last_error_category: str | None = None
    coordinator_enabled: bool = False
    coordinator_lifecycle: str | None = None
    coordinator_handled_events: int = 0
    coordinator_successful_decisions: int = 0
    coordinator_failed_decisions: int = 0
    coordinator_ignored_duplicates: int = 0
    coordinator_shadow_deliveries: int = 0
    coordinator_shadow_failures: int = 0
    coordinator_paper_deliveries: int = 0
    coordinator_paper_failures: int = 0
    coordinator_paper_unavailable: int = 0
    paper_enabled: bool = False
    paper_health: str | None = None
    paper_quote_balance: Decimal | None = None
    paper_base_balance: Decimal | None = None
    paper_equity: Decimal | None = None
    paper_realised_pnl: Decimal | None = None
    paper_unrealised_pnl: Decimal | None = None
    paper_drawdown: Decimal | None = None
    paper_trade_count: int = 0
    paper_portfolio_version: int | None = None
    paper_last_outcome: str | None = None
    paper_last_fill_id: str | None = None


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
    coordinator: TradingDecisionCoordinator | None = None
    shadow_consumer: ShadowDecisionConsumer | None = None
    paper_engine: PaperTradingEngine | None = None
    paper_portfolio: Portfolio | None = None
    _provider_closed: bool = False

    def close(self) -> None:
        """Idempotently release resources owned by the composition root."""

        if self._provider_closed:
            return
        self._provider_closed = True
        try:
            if self.coordinator is not None:
                self.coordinator.stop()
        finally:
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
        coordination = self.coordinator.status if self.coordinator is not None else None
        shadow = self.shadow_consumer.status if self.shadow_consumer is not None else None
        paper = self.paper_engine.status if self.paper_engine is not None else None
        portfolio = self.paper_portfolio.snapshot if self.paper_portfolio is not None else None
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
            shadow_enabled=self.shadow_consumer is not None,
            shadow_lifecycle=coordination.lifecycle.value if shadow else None,
            shadow_handled_events=shadow.handled_decisions if shadow else 0,
            shadow_successful_decisions=shadow.successful_records if shadow else 0,
            shadow_hold_decisions=coordination.hold_decisions if shadow and coordination else 0,
            shadow_intent_decisions=(
                coordination.intent_decisions if shadow and coordination else 0
            ),
            shadow_declined_decisions=(
                coordination.planner_declined_decisions if shadow and coordination else 0
            ),
            shadow_failed_decisions=coordination.failed_decisions if coordination else 0,
            shadow_observation_failures=shadow.observation_failures if shadow else 0,
            shadow_ignored_duplicates=coordination.ignored_duplicates if coordination else 0,
            shadow_last_event_id=coordination.last_event_id if coordination else None,
            shadow_last_signal_direction=(
                coordination.last_signal_direction.value
                if coordination and coordination.last_signal_direction is not None
                else None
            ),
            shadow_last_error_category=coordination.last_error_category if coordination else None,
            coordinator_enabled=coordination is not None,
            coordinator_lifecycle=coordination.lifecycle.value if coordination else None,
            coordinator_handled_events=coordination.handled_events if coordination else 0,
            coordinator_successful_decisions=(
                coordination.successful_decisions if coordination else 0
            ),
            coordinator_failed_decisions=coordination.failed_decisions if coordination else 0,
            coordinator_ignored_duplicates=coordination.ignored_duplicates if coordination else 0,
            coordinator_shadow_deliveries=(coordination.shadow_deliveries if coordination else 0),
            coordinator_shadow_failures=coordination.shadow_failures if coordination else 0,
            coordinator_paper_deliveries=coordination.paper_deliveries if coordination else 0,
            coordinator_paper_failures=coordination.paper_failures if coordination else 0,
            coordinator_paper_unavailable=coordination.paper_unavailable if coordination else 0,
            paper_enabled=paper is not None,
            paper_health=paper.health.value if paper else None,
            paper_quote_balance=portfolio.quote_balance if portfolio else None,
            paper_base_balance=portfolio.base_balance if portfolio else None,
            paper_equity=portfolio.current_equity if portfolio else None,
            paper_realised_pnl=portfolio.realised_pnl if portfolio else None,
            paper_unrealised_pnl=portfolio.unrealised_pnl if portfolio else None,
            paper_drawdown=portfolio.drawdown if portfolio else None,
            paper_trade_count=portfolio.trade_count if portfolio else 0,
            paper_portfolio_version=portfolio.version if portfolio else None,
            paper_last_outcome=(
                self.paper_engine.last_result.outcome.value
                if self.paper_engine is not None and self.paper_engine.last_result is not None
                else None
            ),
            paper_last_fill_id=(
                self.paper_engine.last_result.fill_id
                if self.paper_engine is not None and self.paper_engine.last_result is not None
                else None
            ),
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
    shadow_logger: logging.Logger | None = None,
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
    coordinator = None
    shadow_consumer = None
    paper_engine = None
    paper_portfolio = None
    if settings.shadow_decisions_enabled or settings.paper_trading_enabled:
        strategy = EmaCrossoverStrategy(
            EmaCrossoverConfig(settings.ema_fast_window, settings.ema_slow_window),
            UUIDSignalIdFactory(),
        )
        planner = LongOnlyIntentPlanner(
            LongOnlyIntentPlannerConfig(settings.long_target_exposure),
            UUIDIntentIdFactory(),
        )
        pipeline = TradingDecisionPipeline(strategy, planner, UUIDPipelineRunIdFactory())
        if settings.shadow_decisions_enabled:
            shadow_consumer = ShadowDecisionConsumer(
                UUIDShadowRunIdFactory(),
                LoggingShadowDecisionObserver(shadow_logger),
                production_clock.monotonic,
            )
        if settings.paper_trading_enabled:
            rules = InstrumentRules(
                stream_key=stream_key,
                base_asset="BTC",
                quote_asset="USDT",
                price_tick=settings.instrument_price_tick,
                quantity_step=settings.instrument_quantity_step,
                min_quantity=settings.instrument_min_quantity,
                min_notional=settings.instrument_min_notional,
                base_asset_precision=settings.instrument_base_precision,
                quote_asset_precision=settings.instrument_quote_precision,
                rules_version="binance-spot-btcusdt/1",
            )
            paper_portfolio = Portfolio(
                PortfolioConfig(
                    portfolio_id=settings.paper_portfolio_id,
                    stream_key=stream_key,
                    base_asset="BTC",
                    quote_asset="USDT",
                    initial_base_balance=Decimal(0),
                    initial_quote_balance=settings.paper_initial_quote_balance,
                    instrument_rules=rules,
                    applied_fill_id_capacity=settings.portfolio_fill_dedup_capacity,
                )
            )
            risk_manager = RiskManager(
                PortfolioRiskPolicy(
                    max_order_notional=settings.risk_max_order_notional,
                    max_position_notional=settings.risk_max_position_notional,
                    max_target_exposure=settings.long_target_exposure,
                    max_fee_rate=settings.risk_max_fee_rate,
                    max_slippage_bps=settings.risk_max_slippage_bps,
                ),
                rules,
                UUIDRiskDecisionIdFactory(),
            )
            execution = PaperExecutionProvider(
                PaperExecutionConfig(
                    fee_rate=settings.paper_fee_rate,
                    latency_ms=settings.paper_execution_latency_ms,
                ),
                rules,
                UUIDExecutionIdFactory(),
                UUIDFillIdFactory(),
            )
            paper_engine = PaperTradingEngine(
                PaperTradingEngineConfig(dedup_capacity=settings.paper_dedup_capacity),
                paper_portfolio,
                risk_manager,
                execution,
                UUIDPaperTradingCycleIdFactory(),
                LoggingPaperTradingObserver(),
                production_clock.monotonic,
            )
        coordinator = TradingDecisionCoordinator(
            market_cache,
            MarketSnapshotFactory(settings.snapshot_stale_after_ms),
            pipeline,
            UUIDSnapshotIdFactory(),
            UUIDCoordinationIdFactory(),
            shadow_consumer=shadow_consumer,
            paper_engine=paper_engine,
            history_limit=settings.decision_coordinator_history_limit,
            dedup_capacity=settings.decision_coordinator_dedup_capacity,
            monotonic_clock=production_clock.monotonic,
        )
        coordinator.start(dispatcher)
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
        coordinator=coordinator,
        shadow_consumer=shadow_consumer,
        paper_engine=paper_engine,
        paper_portfolio=paper_portfolio,
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
    logger.info(
        "Shadow status enabled=%s lifecycle=%s handled=%s successful=%s hold=%s "
        "intent=%s declined=%s failed=%s observation_failures=%s duplicates=%s "
        "last_event_id=%s last_direction=%s last_error=%s",
        report.shadow_enabled,
        report.shadow_lifecycle,
        report.shadow_handled_events,
        report.shadow_successful_decisions,
        report.shadow_hold_decisions,
        report.shadow_intent_decisions,
        report.shadow_declined_decisions,
        report.shadow_failed_decisions,
        report.shadow_observation_failures,
        report.shadow_ignored_duplicates,
        report.shadow_last_event_id,
        report.shadow_last_signal_direction,
        report.shadow_last_error_category,
    )
    logger.info(
        "Decision coordinator enabled=%s lifecycle=%s handled=%s successful=%s "
        "failed=%s duplicates=%s shadow_delivered=%s shadow_failed=%s "
        "paper_delivered=%s paper_failed=%s paper_unavailable=%s paper_enabled=%s "
        "paper_health=%s portfolio_quote=%s "
        "portfolio_base=%s equity=%s realised_pnl=%s unrealised_pnl=%s drawdown=%s "
        "trades=%s portfolio_version=%s last_paper_outcome=%s last_fill_id=%s",
        report.coordinator_enabled,
        report.coordinator_lifecycle,
        report.coordinator_handled_events,
        report.coordinator_successful_decisions,
        report.coordinator_failed_decisions,
        report.coordinator_ignored_duplicates,
        report.coordinator_shadow_deliveries,
        report.coordinator_shadow_failures,
        report.coordinator_paper_deliveries,
        report.coordinator_paper_failures,
        report.coordinator_paper_unavailable,
        report.paper_enabled,
        report.paper_health,
        report.paper_quote_balance,
        report.paper_base_balance,
        report.paper_equity,
        report.paper_realised_pnl,
        report.paper_unrealised_pnl,
        report.paper_drawdown,
        report.paper_trade_count,
        report.paper_portfolio_version,
        report.paper_last_outcome,
        report.paper_last_fill_id,
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
