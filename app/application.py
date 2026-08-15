import logging
from dataclasses import dataclass
from decimal import Decimal

import httpx

from app.agent.trader import TraderAgent
from app.coordination import TradingDecisionCoordinator, UUIDCoordinationIdFactory
from app.core.config import Settings
from app.exchange_runtime.composition import (
    ProductionTestnetComponents,
    build_production_testnet_components,
)
from app.execution import (
    PaperExecutionConfig,
    PaperExecutionProvider,
    UUIDExecutionIdFactory,
    UUIDFillIdFactory,
)
from app.infrastructure.binance import BinanceSpotTestnetAdapter
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
from app.paper.exceptions import PaperTradingConfigurationError
from app.persistence.canonical import canonical_sha256
from app.persistence.keys import (
    EmaStrategyFingerprintInput,
    InstrumentRulesFingerprintInput,
    PaperExecutionFingerprintInput,
    PlannerFingerprintInput,
    RiskPolicyFingerprintInput,
    SnapshotPolicyFingerprintInput,
    fingerprint_ema_strategy,
    fingerprint_instrument_rules,
    fingerprint_paper_execution,
    fingerprint_planner,
    fingerprint_risk_policy,
    fingerprint_snapshot_policy,
)
from app.persistence.models import (
    InitialPortfolioRecord,
    PersistenceStatus,
    RecoveryResult,
    RecoveryStatus,
    SQLitePersistenceConfig,
)
from app.persistence.ports import TradingPersistence
from app.persistence.runtime import (
    DurablePaperCycleStore,
    DurablePaperRuntimeConfig,
    snapshot_to_state,
    state_to_snapshot,
)
from app.persistence.sqlite import SQLiteTradingPersistence
from app.portfolio import InstrumentRules, Portfolio, PortfolioConfig
from app.risk import PortfolioRiskPolicy, RiskManager, UUIDRiskDecisionIdFactory
from app.server_status import ServerProcessState, status_for_application
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
    persistence_enabled: bool = False
    persistence_backend: str | None = None
    persistence_schema_version: int | None = None
    recovery_status: str | None = None
    persistence_last_journal_seq: int = 0
    persistence_checkpoint_version: int | None = None
    testnet_runtime_availability: str | None = None
    testnet_readiness: str | None = None
    testnet_readiness_reasons: tuple[str, ...] = ()
    testnet_submission_capability_present: bool = False
    testnet_reconciliation_status: str | None = None
    testnet_settlement_status: str | None = None
    testnet_non_terminal_order_count: int = 0
    testnet_safe_stop_reason: str | None = None


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
    persistence: TradingPersistence | None = None
    recovery_result: RecoveryResult | None = None
    testnet: ProductionTestnetComponents | None = None
    _provider_closed: bool = False
    _last_persistence_status: PersistenceStatus | None = None

    @property
    def startup_safe_stop_reason(self) -> str | None:
        """Return the sanitized startup blocker for active Testnet only."""
        if self.testnet is None or self.testnet.submission_ready:
            return None
        reasons = ",".join(reason.value for reason in self.testnet.readiness.reasons)
        return f"TESTNET_STARTUP_BLOCKED:{reasons or 'UNKNOWN'}"

    def request_shutdown(self) -> None:
        """Disable decision delivery before asking the polling engine to stop."""
        if self.coordinator is not None:
            self.coordinator.stop()
        self.agent.stop()

    def close(self) -> None:
        """Idempotently release resources owned by the composition root."""

        if self._provider_closed:
            return
        self._provider_closed = True
        try:
            if self.coordinator is not None:
                self.coordinator.stop()
        finally:
            try:
                if self.testnet is not None:
                    self.testnet.close()
                if self.persistence is not None:
                    if self.paper_portfolio is not None:
                        status = self.persistence.status
                        if status.last_journal_seq > 0:
                            try:
                                self.persistence.create_checkpoint(
                                    snapshot_to_state(self.paper_portfolio.snapshot),
                                    status.last_journal_seq,
                                )
                            except Exception:
                                logging.getLogger("Persistence").exception(
                                    "Final Portfolio checkpoint failed"
                                )
                    self._last_persistence_status = self.persistence.status
                    self.persistence.close()
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
        persistence_status = (
            self._last_persistence_status
            if self._last_persistence_status is not None
            else self.persistence.status
            if self.persistence is not None
            else None
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
            persistence_enabled=self.persistence is not None,
            persistence_backend=(self.settings.persistence_backend if persistence_status else None),
            persistence_schema_version=(
                persistence_status.schema_version if persistence_status else None
            ),
            recovery_status=(
                self.recovery_result.status.value if self.recovery_result is not None else None
            ),
            persistence_last_journal_seq=(
                persistence_status.last_journal_seq if persistence_status else 0
            ),
            persistence_checkpoint_version=(
                persistence_status.latest_checkpoint_version if persistence_status else None
            ),
            testnet_runtime_availability=(
                self.testnet.runtime_availability if self.testnet is not None else None
            ),
            testnet_readiness=(
                self.testnet.readiness.state.value if self.testnet is not None else None
            ),
            testnet_readiness_reasons=(
                tuple(reason.value for reason in self.testnet.readiness.reasons)
                if self.testnet is not None
                else ()
            ),
            testnet_submission_capability_present=(
                self.testnet.readiness.capability is not None if self.testnet is not None else False
            ),
            testnet_reconciliation_status=(
                self.testnet.reconciliation_status if self.testnet is not None else None
            ),
            testnet_settlement_status=(
                self.testnet.settlement_status.value if self.testnet is not None else None
            ),
            testnet_non_terminal_order_count=(
                self.testnet.non_terminal_order_count if self.testnet is not None else 0
            ),
            testnet_safe_stop_reason=(
                coordination.testnet_safe_stop_reason if coordination is not None else None
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
    persistence: TradingPersistence | None = None,
    exchange_persistence=None,
    testnet_http_client: httpx.Client | None = None,
    testnet_adapter: BinanceSpotTestnetAdapter | None = None,
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
    persistence_adapter = None
    recovery_result = None
    testnet_components = None
    if (
        settings.shadow_decisions_enabled
        or settings.paper_trading_enabled
        or settings.trading_mode == "testnet"
    ):
        strategy_config = EmaCrossoverConfig(settings.ema_fast_window, settings.ema_slow_window)
        strategy = EmaCrossoverStrategy(
            strategy_config,
            UUIDSignalIdFactory(),
        )
        planner_config = LongOnlyIntentPlannerConfig(settings.long_target_exposure)
        planner = LongOnlyIntentPlanner(
            planner_config,
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
            portfolio_config = PortfolioConfig(
                portfolio_id=settings.paper_portfolio_id,
                stream_key=stream_key,
                base_asset="BTC",
                quote_asset="USDT",
                initial_base_balance=Decimal(0),
                initial_quote_balance=settings.paper_initial_quote_balance,
                instrument_rules=rules,
                applied_fill_id_capacity=settings.portfolio_fill_dedup_capacity,
            )
            initial_portfolio = Portfolio(portfolio_config)
            instrument_fingerprint = fingerprint_instrument_rules(
                InstrumentRulesFingerprintInput(
                    symbol=stream_key.symbol,
                    interval=stream_key.interval,
                    base_asset=rules.base_asset,
                    quote_asset=rules.quote_asset,
                    price_tick=rules.price_tick,
                    quantity_step=rules.quantity_step,
                    min_quantity=rules.min_quantity,
                    min_notional=rules.min_notional,
                    base_asset_precision=rules.base_asset_precision,
                    quote_asset_precision=rules.quote_asset_precision,
                    rules_version=rules.rules_version,
                )
            )
            portfolio_fingerprint = canonical_sha256(
                {
                    "portfolio_id": portfolio_config.portfolio_id,
                    "symbol": stream_key.symbol,
                    "interval": stream_key.interval,
                    "base_asset": portfolio_config.base_asset,
                    "quote_asset": portfolio_config.quote_asset,
                    "initial_equity": portfolio_config.initial_quote_balance,
                    "instrument_rules_fingerprint": instrument_fingerprint,
                },
                algorithm_version="paper-portfolio-config/v1",
            )
            try:
                persistence_adapter = persistence or SQLiteTradingPersistence(
                    SQLitePersistenceConfig(
                        settings.sqlite_path,
                        busy_timeout_ms=settings.sqlite_busy_timeout_ms,
                        checkpoint_interval_transitions=(
                            settings.portfolio_checkpoint_interval_transitions
                        ),
                        exclusive_writer=True,
                    )
                )
            except Exception:
                market_provider.close()
                raise
            if persistence_adapter.load_initial(settings.paper_portfolio_id) is None:
                persistence_adapter.initialize_portfolio(
                    InitialPortfolioRecord(
                        snapshot_to_state(initial_portfolio.snapshot),
                        portfolio_fingerprint,
                        production_clock.now_ms(),
                    )
                )
            recovery_result = persistence_adapter.recover(
                settings.paper_portfolio_id,
                expected_stream_key=stream_key,
                expected_config_fingerprint=portfolio_fingerprint,
            )
            if (
                recovery_result.status is RecoveryStatus.RECOVERY_REQUIRED
                or recovery_result.portfolio_state is None
            ):
                persistence_adapter.close()
                market_provider.close()
                raise PaperTradingConfigurationError(
                    "paper persistence recovery failed: " + "; ".join(recovery_result.diagnostics)
                )
            if (
                recovery_result.status is RecoveryStatus.RECOVERED
                and persistence_adapter.load_current(settings.paper_portfolio_id) is None
            ):
                persistence_adapter.rebuild_portfolio_current(
                    recovery_result.portfolio_state,
                    recovery_result.last_journal_seq,
                )
            persistence_status = persistence_adapter.status
            logging.getLogger("Persistence").info(
                "Persistence startup backend=sqlite path=%s schema=%s recovery=%s "
                "portfolio_id=%s version=%s equity=%s journal_seq=%s checkpoint=%s replayed=%s",
                persistence_status.database_path,
                persistence_status.schema_version,
                recovery_result.status.value,
                settings.paper_portfolio_id,
                recovery_result.portfolio_state.version,
                recovery_result.portfolio_state.current_equity,
                recovery_result.last_journal_seq,
                recovery_result.checkpoint_version,
                recovery_result.replayed_transitions,
            )
            paper_portfolio = Portfolio.restore(
                portfolio_config, state_to_snapshot(recovery_result.portfolio_state)
            )
            risk_policy = PortfolioRiskPolicy(
                max_order_notional=settings.risk_max_order_notional,
                max_position_notional=settings.risk_max_position_notional,
                max_target_exposure=settings.long_target_exposure,
                max_fee_rate=settings.risk_max_fee_rate,
                max_slippage_bps=settings.risk_max_slippage_bps,
            )
            risk_manager = RiskManager(
                risk_policy,
                rules,
                UUIDRiskDecisionIdFactory(),
            )
            execution_config = PaperExecutionConfig(
                fee_rate=settings.paper_fee_rate,
                latency_ms=settings.paper_execution_latency_ms,
            )
            execution = PaperExecutionProvider(
                execution_config,
                rules,
                UUIDExecutionIdFactory(),
                UUIDFillIdFactory(),
            )
            durable_store = DurablePaperCycleStore(
                persistence_adapter,
                DurablePaperRuntimeConfig(
                    exchange=settings.exchange_name,
                    strategy_config_fingerprint=fingerprint_ema_strategy(
                        EmaStrategyFingerprintInput(
                            strategy_config.fast_window,
                            strategy_config.slow_window,
                            strategy_config.strategy_name,
                            strategy_config.strategy_version,
                        )
                    ),
                    planner_name=planner_config.planner_name,
                    planner_version=planner_config.planner_version,
                    planner_config_fingerprint=fingerprint_planner(
                        PlannerFingerprintInput(
                            planner_config.long_target_exposure,
                            planner_config.planner_name,
                            planner_config.planner_version,
                        )
                    ),
                    snapshot_policy_fingerprint=fingerprint_snapshot_policy(
                        SnapshotPolicyFingerprintInput(
                            settings.snapshot_stale_after_ms, "market-snapshot/1"
                        )
                    ),
                    risk_policy_fingerprint=fingerprint_risk_policy(
                        RiskPolicyFingerprintInput(
                            risk_policy.max_order_notional,
                            risk_policy.max_position_notional,
                            risk_policy.max_target_exposure,
                            risk_policy.min_cash_reserve,
                            risk_policy.max_fee_rate,
                            risk_policy.max_slippage_bps,
                            risk_policy.reject_stale_market,
                            risk_policy.policy_name,
                            risk_policy.policy_version,
                        )
                    ),
                    instrument_rules_fingerprint=instrument_fingerprint,
                    execution_config_fingerprint=fingerprint_paper_execution(
                        PaperExecutionFingerprintInput(
                            execution_config.fee_rate,
                            execution_config.latency_ms,
                            execution_config.provider_name,
                            execution_config.provider_version,
                        )
                    ),
                    checkpoint_interval_transitions=(
                        settings.portfolio_checkpoint_interval_transitions
                    ),
                ),
            )
            paper_engine = PaperTradingEngine(
                PaperTradingEngineConfig(dedup_capacity=settings.paper_dedup_capacity),
                paper_portfolio,
                risk_manager,
                execution,
                UUIDPaperTradingCycleIdFactory(),
                LoggingPaperTradingObserver(),
                production_clock.monotonic,
                durable_store,
            )
        if settings.trading_mode == "testnet":
            try:
                testnet_components = build_production_testnet_components(
                    settings,
                    now_ms=production_clock.now_ms,
                    persistence=exchange_persistence,
                    http_client=testnet_http_client,
                    adapter=testnet_adapter,
                )
            except BaseException:
                if persistence_adapter is not None:
                    persistence_adapter.close()
                market_provider.close()
                raise
        testnet_consumer = testnet_components.consumer if testnet_components is not None else None
        if shadow_consumer is not None or paper_engine is not None or testnet_consumer is not None:
            try:
                coordinator = TradingDecisionCoordinator(
                    market_cache,
                    MarketSnapshotFactory(settings.snapshot_stale_after_ms),
                    pipeline,
                    UUIDSnapshotIdFactory(),
                    UUIDCoordinationIdFactory(),
                    shadow_consumer=shadow_consumer,
                    paper_engine=paper_engine,
                    testnet_consumer=testnet_consumer,
                    history_limit=settings.decision_coordinator_history_limit,
                    dedup_capacity=settings.decision_coordinator_dedup_capacity,
                    monotonic_clock=production_clock.monotonic,
                )
                coordinator.start(dispatcher)
            except BaseException:
                if coordinator is not None:
                    coordinator.stop()
                if testnet_components is not None:
                    testnet_components.close()
                if persistence_adapter is not None:
                    persistence_adapter.close()
                market_provider.close()
                raise
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
        persistence=persistence_adapter,
        recovery_result=recovery_result,
        testnet=testnet_components,
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
    logger.info(
        "Persistence enabled=%s backend=%s schema=%s recovery=%s portfolio_version=%s "
        "journal_seq=%s checkpoint_version=%s paper_health=%s",
        report.persistence_enabled,
        report.persistence_backend,
        report.persistence_schema_version,
        report.recovery_status,
        report.paper_portfolio_version,
        report.persistence_last_journal_seq,
        report.persistence_checkpoint_version,
        report.paper_health,
    )
    logger.info(
        "Testnet runtime availability=%s readiness=%s reasons=%s "
        "submission_capability_present=%s reconciliation=%s settlement=%s non_terminal=%s "
        "safe_stop_reason=%s",
        report.testnet_runtime_availability,
        report.testnet_readiness,
        report.testnet_readiness_reasons,
        report.testnet_submission_capability_present,
        report.testnet_reconciliation_status,
        report.testnet_settlement_status,
        report.testnet_non_terminal_order_count,
        report.testnet_safe_stop_reason,
    )


def run_application(
    application: ChappieTraderApplication,
    logger: logging.Logger,
    *,
    status_publisher=None,
) -> int:
    """Run, report and clean up the application, returning a process exit code."""

    exit_code = 0
    interrupted = False
    startup_safe_stop = application.startup_safe_stop_reason
    try:
        if startup_safe_stop is not None:
            exit_code = 1
            logger.error("Testnet startup SAFE_STOP reason=%s", startup_safe_stop)
            if status_publisher is not None:
                status_publisher.publish(
                    status_for_application(
                        application,
                        ServerProcessState.SAFE_STOP,
                        safe_stop_reason=startup_safe_stop,
                    )
                )
        else:
            if status_publisher is not None:
                status_publisher.publish(
                    status_for_application(application, ServerProcessState.READY)
                )
                application.engine.set_cycle_observer(
                    lambda: status_publisher.publish(
                        status_for_application(application, ServerProcessState.READY)
                    )
                )
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
        application.request_shutdown()
        try:
            application.close()
        except Exception:
            exit_code = 1
            logger.exception("Application resource cleanup failed")
        log_stage_one_status(application.status_report(), logger)
        if status_publisher is not None:
            try:
                status_publisher.publish(
                    status_for_application(application, ServerProcessState.STOPPED)
                )
            except Exception:
                exit_code = 1
                logger.exception("Server status shutdown publication failed")

    if interrupted and exit_code == 0:
        logger.info("Application stopped cleanly by user")
    return exit_code
