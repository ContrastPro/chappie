from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from uuid import uuid4

import httpx

from app.core.config import Settings
from app.exchange_execution import (
    DurableExchangeOrderLifecycle,
    ExchangeInstrumentRules,
    ExchangeReconciler,
    ExecutionSession,
    ExecutionSessionState,
)
from app.exchange_runtime.application_service import TestnetExecutionApplicationService
from app.exchange_runtime.consumer import TestnetExecutionConsumer
from app.exchange_runtime.identity import SQLiteOrderIdentityResolver
from app.exchange_runtime.models import (
    ExchangeSettlementStatus,
    TestnetExecutionReadiness,
    TestnetReadinessReason,
    TestnetReadinessState,
)
from app.exchange_runtime.runtime import TestnetRuntimeStartup
from app.exchange_runtime.settlement import SQLiteExchangeSettlement
from app.exchange_runtime.submission import TestnetPreSubmitGate
from app.infrastructure.binance import (
    BinanceSpotTestnetAdapter,
    BinanceSpotTestnetConfig,
    encode_parameters,
)
from app.market.models import MarketStreamKey
from app.persistence import (
    ExchangeExecutionRoot,
    SQLiteExchangePersistence,
    SQLitePersistenceConfig,
)
from app.persistence.canonical import canonical_sha256
from app.persistence.keys import (
    EmaStrategyFingerprintInput,
    InstrumentRulesFingerprintInput,
    PlannerFingerprintInput,
    RiskPolicyFingerprintInput,
    SnapshotPolicyFingerprintInput,
    fingerprint_ema_strategy,
    fingerprint_instrument_rules,
    fingerprint_planner,
    fingerprint_risk_policy,
    fingerprint_snapshot_policy,
)
from app.portfolio import InstrumentRules, Portfolio, PortfolioConfig
from app.risk import PortfolioRiskPolicy, RiskManager, UUIDRiskDecisionIdFactory
from app.risk.exchange import ExchangeRiskPolicy
from app.trading import LongOnlyIntentPlannerConfig


@dataclass(slots=True)
class ProductionTestnetComponents:
    persistence: SQLiteExchangePersistence
    portfolio: Portfolio
    adapter: BinanceSpotTestnetAdapter
    reconciler: ExchangeReconciler
    settlement: SQLiteExchangeSettlement
    readiness: TestnetExecutionReadiness
    reconciliation_status: str
    settlement_status: ExchangeSettlementStatus
    non_terminal_order_count: int
    application_service: TestnetExecutionApplicationService | None
    consumer: TestnetExecutionConsumer | None
    http_client: httpx.Client
    owns_persistence: bool
    owns_adapter: bool
    owns_http_client: bool
    _closed: bool = False

    def __post_init__(self) -> None:
        ready = self.readiness.state is TestnetReadinessState.READY
        if ready != (self.application_service is not None and self.consumer is not None):
            raise ValueError("submit-capable Testnet components must match READY state")

    @property
    def runtime_availability(self) -> str:
        return "READY" if self.readiness.state is TestnetReadinessState.READY else "RECOVERY_ONLY"

    @property
    def submission_ready(self) -> bool:
        return self.readiness.state is TestnetReadinessState.READY

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self.owns_adapter:
                self.adapter.close()
        finally:
            try:
                if self.owns_http_client:
                    self.http_client.close()
            finally:
                if self.owns_persistence:
                    self.persistence.close()


def build_production_testnet_components(
    settings: Settings,
    *,
    now_ms,
    persistence: SQLiteExchangePersistence | None = None,
    http_client: httpx.Client | None = None,
    adapter: BinanceSpotTestnetAdapter | None = None,
) -> ProductionTestnetComponents:
    """Build the private Testnet graph; all exchange I/O occurs during explicit startup."""
    if settings.trading_mode != "testnet" or not settings.testnet_execution_enabled:
        raise ValueError("production Testnet composition requires explicit Testnet mode")
    stream = MarketStreamKey(settings.default_symbol, settings.default_interval)
    rules = InstrumentRules(
        stream,
        "BTC",
        "USDT",
        settings.instrument_price_tick,
        settings.instrument_quantity_step,
        settings.instrument_min_quantity,
        settings.instrument_min_notional,
        settings.instrument_base_precision,
        settings.instrument_quote_precision,
        "binance-spot-btcusdt/1",
    )
    expected_rules = ExchangeInstrumentRules(
        stream,
        rules.quantity_step,
        rules.min_quantity,
        rules.min_notional,
        sha256(
            encode_parameters(
                [
                    ("symbol", stream.symbol),
                    ("contractVersion", "binance-market-rules/v2"),
                    ("quantityFilter", "MARKET_LOT_SIZE"),
                    ("stepSize", rules.quantity_step),
                    ("minQty", rules.min_quantity),
                    ("maxQty", settings.instrument_max_quantity),
                    ("notionalFilter", "MIN_NOTIONAL"),
                    ("minNotional", rules.min_notional),
                    ("minNotionalAppliesToMarket", "true"),
                ]
            ).encode()
        ).hexdigest(),
        settings.instrument_max_quantity,
    )
    rules_fingerprint = fingerprint_instrument_rules(
        InstrumentRulesFingerprintInput(
            stream.symbol,
            stream.interval,
            rules.base_asset,
            rules.quote_asset,
            rules.price_tick,
            rules.quantity_step,
            rules.min_quantity,
            rules.min_notional,
            rules.base_asset_precision,
            rules.quote_asset_precision,
            rules.rules_version,
        )
    )
    execution_fingerprint = canonical_sha256(
        {
            "venue": "binance-spot",
            "environment": "testnet",
            "base_url": settings.binance_testnet_base_url,
            "rules_fingerprint": rules_fingerprint,
        },
        algorithm_version="testnet-execution-config/v1",
    )
    owned_persistence = persistence is None
    store = persistence or SQLiteExchangePersistence(
        SQLitePersistenceConfig(
            settings.sqlite_path,
            busy_timeout_ms=settings.sqlite_busy_timeout_ms,
            checkpoint_interval_transitions=settings.portfolio_checkpoint_interval_transitions,
            exclusive_writer=True,
            expected_schema_version=4,
        )
    )
    owned_client = http_client is None
    client = http_client
    owned_adapter = adapter is None
    private_adapter = adapter
    try:
        if client is None:
            client = httpx.Client(
                base_url=settings.binance_testnet_base_url,
                timeout=float(Decimal(10)),
            )
        root = ExchangeExecutionRoot(
            settings.testnet_execution_root_id,
            settings.binance_testnet_account_fingerprint or "invalid",
            stream,
            execution_fingerprint,
            0,
        )
        session = ExecutionSession(
            settings.testnet_session_id,
            root.account_fingerprint,
            0,
            ExecutionSessionState.ACTIVE,
        )
        bootstrap = store.initialize_execution(root, session, audit_identifier="production-startup")
        managed = Portfolio(
            PortfolioConfig(
                settings.testnet_portfolio_id,
                stream,
                "BTC",
                "USDT",
                Decimal(0),
                settings.testnet_initial_quote_balance,
                rules,
                settings.portfolio_fill_dedup_capacity,
            )
        )
        private_adapter = adapter or BinanceSpotTestnetAdapter(
            BinanceSpotTestnetConfig(
                settings.binance_testnet_api_key.get_secret_value(),  # type: ignore[union-attr]
                settings.binance_testnet_secret_key.get_secret_value(),  # type: ignore[union-attr]
                root.account_fingerprint,
                settings.binance_testnet_base_url,
            ),
            client=client,
            timestamp_provider=now_ms,
            identity_resolver=SQLiteOrderIdentityResolver(store),
        )
        reconciler = ExchangeReconciler(
            store,
            private_adapter,
            expected_rules=expected_rules,
            timestamp_provider=now_ms,
            reconciliation_id_factory=lambda: str(uuid4()),
        )
        settlement = SQLiteExchangeSettlement(
            store,
            managed,
            execution_root_id=root.execution_root_id,
            session_id=bootstrap.session.session_id,
            config_fingerprint=execution_fingerprint,
            timestamp_provider=now_ms,
        )
        readiness = TestnetRuntimeStartup(
            store, reconciler, settlement, execution_root_id=root.execution_root_id
        ).start(enabled=True)
        reconciliation_status = store.read_reconciliation_state(root.execution_root_id).status.value
        settlement_status = (
            ExchangeSettlementStatus.BLOCKED_UNSUPPORTED_FEE
            if TestnetReadinessReason.SETTLEMENT_BLOCKED in readiness.reasons
            else ExchangeSettlementStatus.PENDING
            if TestnetReadinessReason.SETTLEMENT_PENDING in readiness.reasons
            else ExchangeSettlementStatus.SYNCHRONIZED
        )
        non_terminal_order_count = sum(
            not value.order.status.is_exchange_terminal
            for value in store.list_orders()
            if value.execution_root_id == root.execution_root_id
        )
        if readiness.state is not TestnetReadinessState.READY:
            return ProductionTestnetComponents(
                store,
                managed,
                private_adapter,
                reconciler,
                settlement,
                readiness,
                reconciliation_status,
                settlement_status,
                non_terminal_order_count,
                None,
                None,
                client,
                owned_persistence,
                owned_adapter,
                owned_client,
            )
        if readiness.capability is None:  # defensive: enforced by TestnetExecutionReadiness
            raise RuntimeError("READY Testnet startup did not mint a submission capability")
        financial_policy = PortfolioRiskPolicy(
            settings.risk_max_order_notional,
            settings.risk_max_position_notional,
            max_target_exposure=settings.long_target_exposure,
            max_fee_rate=settings.risk_max_fee_rate,
            max_slippage_bps=settings.risk_max_slippage_bps,
        )
        financial_risk = RiskManager(financial_policy, rules, UUIDRiskDecisionIdFactory())
        exchange_policy = ExchangeRiskPolicy(
            settings.risk_max_order_notional,
            settings.risk_max_position_notional,
            settings.testnet_max_session_submitted_notional,
            settings.testnet_max_session_executed_turnover,
            settings.testnet_max_session_submission_count,
            settings.testnet_max_session_execution_failures,
            settings.testnet_max_drawdown,
        )
        lifecycle = DurableExchangeOrderLifecycle(store, private_adapter, timestamp_provider=now_ms)
        gate = TestnetPreSubmitGate(
            store,
            lifecycle,
            execution_root_id=root.execution_root_id,
            max_authorization_age_ms=settings.testnet_authorization_max_age_ms,
            settlement=settlement,
        )
        strategy_fingerprint = fingerprint_ema_strategy(
            EmaStrategyFingerprintInput(
                settings.ema_fast_window, settings.ema_slow_window, "ema_crossover", "1.0.0"
            )
        )
        planner_config = LongOnlyIntentPlannerConfig(settings.long_target_exposure)
        planner_fingerprint = fingerprint_planner(
            PlannerFingerprintInput(
                planner_config.long_target_exposure,
                planner_config.planner_name,
                planner_config.planner_version,
            )
        )
        snapshot_fingerprint = fingerprint_snapshot_policy(
            SnapshotPolicyFingerprintInput(settings.snapshot_stale_after_ms, "market-snapshot/1")
        )
        risk_fingerprint = fingerprint_risk_policy(
            RiskPolicyFingerprintInput(
                financial_policy.max_order_notional,
                financial_policy.max_position_notional,
                financial_policy.max_target_exposure,
                financial_policy.min_cash_reserve,
                financial_policy.max_fee_rate,
                financial_policy.max_slippage_bps,
                financial_policy.reject_stale_market,
                financial_policy.policy_name,
                financial_policy.policy_version,
            )
        )
        service = TestnetExecutionApplicationService(
            store,
            managed,
            financial_risk,
            exchange_policy,
            lifecycle,
            gate,
            execution_root_id=root.execution_root_id,
            capability=readiness.capability,
            exchange_name=settings.exchange_name,
            strategy_config_fingerprint=strategy_fingerprint,
            planner_name=planner_config.planner_name,
            planner_version=planner_config.planner_version,
            planner_config_fingerprint=planner_fingerprint,
            snapshot_policy_fingerprint=snapshot_fingerprint,
            risk_policy_fingerprint=risk_fingerprint,
            instrument_rules_fingerprint=rules_fingerprint,
            execution_config_fingerprint=execution_fingerprint,
        )
        consumer = TestnetExecutionConsumer(service.execute)
        return ProductionTestnetComponents(
            store,
            managed,
            private_adapter,
            reconciler,
            settlement,
            readiness,
            reconciliation_status,
            settlement_status,
            non_terminal_order_count,
            service,
            consumer,
            client,
            owned_persistence,
            owned_adapter,
            owned_client,
        )
    except BaseException:
        if owned_adapter and private_adapter is not None:
            private_adapter.close()
        if owned_client and client is not None:
            client.close()
        if owned_persistence:
            store.close()
        raise
