import socket
import sqlite3
import time
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.exchange_execution import (
    DurableExchangeOrderLifecycle,
    ExchangeCommand,
    ExchangeOrderSide,
    ExchangeOrderStatus,
    ExchangeReconciler,
    ExchangeTrade,
    ExchangeTradeIdentity,
    ExecutionEnvironment,
    ExecutionSession,
    ExecutionSessionState,
    KillSwitchState,
    PendingExposure,
)
from app.exchange_execution.exceptions import ExchangeSubmissionSafetyError
from app.exchange_runtime import (
    ExchangeSettlementStatus,
    SQLiteExchangeSettlement,
    evaluate_testnet_readiness,
)
from app.exchange_runtime import (
    TestnetPreSubmitGate as PreSubmitGate,
)
from app.exchange_runtime import (
    TestnetReadinessReason as ReadinessReason,
)
from app.exchange_runtime import (
    TestnetReadinessState as ReadinessState,
)
from app.exchange_runtime import (
    TestnetRuntimeStartup as RuntimeStartup,
)
from app.exchange_runtime.application_service import (
    TestnetExecutionApplicationService as ExecutionApplicationService,
)
from app.exchange_runtime.consumer import (
    TestnetDecisionOutcome as ConsumerOutcome,
)
from app.exchange_runtime.consumer import (
    TestnetExecutionConsumer as ExecutionConsumer,
)
from app.exchange_runtime.exceptions import ExchangeSettlementError
from app.exchange_runtime.identity import SQLiteOrderIdentityResolver
from app.exchange_runtime.models import ExchangeSettlementResult
from app.exchange_runtime.models import TestnetExecutionReadiness as ExecutionReadiness
from app.exchange_runtime.submission import PreSubmitBlockReason, PreSubmitGateResult
from app.market.models import CandleSnapshot, MarketStreamKey
from app.persistence import (
    ExchangeExecutionRoot,
    ExchangeReconciliationStatus,
    PersistentExchangeReconciliationRun,
    SQLiteExchangePersistence,
    SQLitePersistenceConfig,
)
from app.persistence.exceptions import PersistenceConflictError
from app.persistence.exchange_models import LocalExchangeRecoveryStatus
from app.portfolio import InstrumentRules, Portfolio, PortfolioConfig
from app.risk import PortfolioRiskPolicy, RiskManager
from app.risk.exchange import (
    ExchangeReconciliationStatus as RiskReconciliationStatus,
)
from app.risk.exchange import (
    ExchangeRiskAssessment,
    ExchangeRiskContext,
    ExchangeRiskOutcome,
    ExchangeRiskPolicy,
    ExchangeRiskReason,
    ReconciledAccountState,
)
from app.trading.intent import IntentPlanOutcome, IntentPlanResult
from app.trading.models import (
    MarketSnapshot,
    OrderIntent,
    OrderIntentAction,
    RiskDecisionOutcome,
    Signal,
    SignalDirection,
)
from app.trading.pipeline import DecisionPipelineResult, PipelineOutcome

D = Decimal
KEY = MarketStreamKey("BTCUSDT", "1m")
ROOT = "root-m48"
SESSION = "session-m48"
COMMAND = "a" * 64
CLIENT = "ctt1t-abcdefghijklmnopqrstuvwxyz"


@pytest.fixture(autouse=True)
def forbid_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """M4.8 safety proofs must remain hermetic even when production wiring is exercised."""

    def blocked(*_args, **_kwargs):
        raise AssertionError("M4.8 safety test attempted external network or DNS access")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)


class FixedRiskIds:
    def new_risk_decision_id(self) -> str:
        return "risk-fixed"


def execution_decision(*, stale: bool = False, exposure: str = "0.5"):
    candle = CandleSnapshot(40, 99, D(100), D(100), D(100), D(100), D(1))
    snapshot = MarketSnapshot(
        "snapshot-execution",
        KEY,
        100,
        (candle,),
        candle,
        None,
        "event-execution",
        "cycle-execution",
        "market-snapshot/1",
        0,
        stale,
    )
    signal = Signal(
        "signal-execution",
        snapshot.snapshot_id,
        KEY,
        100,
        100,
        SignalDirection.BUY,
        "bullish_crossover",
        "test",
        "ema",
        "1",
        {},
        snapshot.correlation_cycle_id,
    )
    intent = OrderIntent(
        "intent-execution",
        signal.signal_id,
        KEY,
        100,
        OrderIntentAction.ENTER_LONG,
        D(exposure),
        "test",
        "ema",
        "1",
        snapshot.correlation_cycle_id,
    )
    plan = IntentPlanResult(
        IntentPlanOutcome.CREATED,
        signal.signal_id,
        intent,
        "created",
        "planner",
        "1",
        snapshot.correlation_cycle_id,
    )
    return (
        DecisionPipelineResult(
            "pipeline-execution",
            snapshot.snapshot_id,
            KEY,
            100,
            "ema",
            "1",
            signal,
            plan,
            intent,
            PipelineOutcome.INTENT_CREATED,
            "created",
            snapshot.correlation_cycle_id,
            ("snapshot_validated", "strategy_evaluated", "intent_planned", "intent_created"),
        ),
        snapshot,
    )


def non_executable_decision(outcome: PipelineOutcome):
    decision, snapshot = execution_decision()
    direction = SignalDirection.HOLD if outcome is PipelineOutcome.HOLD else SignalDirection.BUY
    signal = replace(
        decision.signal,
        direction=direction,
        reason_code="no_signal" if direction is SignalDirection.HOLD else "bullish_crossover",
    )
    plan = IntentPlanResult(
        IntentPlanOutcome.NO_INTENT_FOR_HOLD
        if outcome is PipelineOutcome.HOLD
        else IntentPlanOutcome.DECLINED,
        signal.signal_id,
        None,
        "no_intent",
        "planner",
        "1",
        snapshot.correlation_cycle_id,
    )
    return (
        replace(
            decision,
            signal=signal,
            intent_plan=plan,
            order_intent=None,
            outcome=outcome,
            completed_stages=("snapshot_validated", "strategy_evaluated", "intent_planned"),
        ),
        snapshot,
    )


def portfolio() -> Portfolio:
    rules = InstrumentRules(
        KEY, "BTC", "USDT", D("0.01"), D("0.00001"), D("0.00001"), D(5), 8, 2, "v1"
    )
    return Portfolio(PortfolioConfig("testnet-btcusdt", KEY, "BTC", "USDT", D(0), D(20000), rules))


def store(path: Path, *, failure_injector=None) -> SQLiteExchangePersistence:
    value = SQLiteExchangePersistence(
        SQLitePersistenceConfig(path, expected_schema_version=6),
        failure_injector=failure_injector,
    )
    value.initialize_execution(
        ExchangeExecutionRoot(ROOT, "account-m48", KEY, "9" * 64, 10),
        ExecutionSession(SESSION, "account-m48", 10, ExecutionSessionState.ACTIVE),
        audit_identifier="m48-test",
    )
    value.record_reconciliation_run(
        PersistentExchangeReconciliationRun(
            "baseline-safe",
            ROOT,
            SESSION,
            "account-m48",
            0,
            1,
            ExchangeReconciliationStatus.SAFE,
            0,
            0,
            0,
            0,
            2,
            (),
            10,
            11,
            0,
            11,
            True,
            True,
            "BTCUSDT",
        )
    )
    return value


def persist_quote_fee_trade(
    value: SQLiteExchangePersistence, fee_asset: str = "USDT", commission: str = "1.00"
) -> None:
    command = ExchangeCommand(
        COMMAND,
        "b" * 64,
        "c" * 64,
        KEY,
        ExchangeOrderSide.BUY,
        D("0.1"),
        D("100000"),
        D("0.5"),
        D("10000"),
        "d" * 64,
        "e" * 64,
        "f" * 64,
        100,
    )
    value.prepare_order(
        execution_root_id=ROOT,
        session_id=SESSION,
        command=command,
        client_order_id=CLIENT,
        occurred_at_ms=100,
    )
    value.begin_submission(COMMAND, occurred_at_ms=110)
    value.record_status(
        COMMAND,
        status=ExchangeOrderStatus.FILLED,
        occurred_at_ms=130,
        reason_code="fake_exchange",
        exchange_order_id="900719925474099312345",
        acknowledged_at_ms=120,
        cumulative_executed_base_quantity=D("0.1"),
        cumulative_quote_quantity=D("10000"),
        exchange_timestamp_ms=130,
    )
    value.persist_trades(
        COMMAND,
        (
            ExchangeTrade(
                ExchangeTradeIdentity(
                    "binance-spot",
                    ExecutionEnvironment.TESTNET,
                    "account-m48",
                    "BTCUSDT",
                    "900719925474099312345",
                    "9223372036854775808123",
                ),
                CLIENT,
                KEY,
                ExchangeOrderSide.BUY,
                D("0.1"),
                D("100000"),
                D("10000"),
                D(commission),
                fee_asset,
                130,
            ),
        ),
    )


def multipart_buy_setup(value: SQLiteExchangePersistence) -> tuple[ExchangeTrade, ...]:
    command = ExchangeCommand(
        COMMAND,
        "b" * 64,
        "c" * 64,
        KEY,
        ExchangeOrderSide.BUY,
        D("1"),
        D("100"),
        D("0.5"),
        D("100"),
        "d" * 64,
        "e" * 64,
        "f" * 64,
        100,
    )
    value.prepare_order(
        execution_root_id=ROOT,
        session_id=SESSION,
        command=command,
        client_order_id=CLIENT,
        occurred_at_ms=100,
    )
    value.begin_submission(COMMAND, occurred_at_ms=110)
    value.record_status(
        COMMAND,
        status=ExchangeOrderStatus.FILLED,
        occurred_at_ms=140,
        reason_code="fake_exchange",
        exchange_order_id="multipart-order",
        acknowledged_at_ms=120,
        cumulative_executed_base_quantity=D("1"),
        cumulative_quote_quantity=D("100"),
        exchange_timestamp_ms=140,
    )
    return tuple(
        ExchangeTrade(
            ExchangeTradeIdentity(
                "binance-spot",
                ExecutionEnvironment.TESTNET,
                "account-m48",
                "BTCUSDT",
                "multipart-order",
                trade_id,
            ),
            CLIENT,
            KEY,
            ExchangeOrderSide.BUY,
            quantity,
            D("100"),
            quote,
            fee,
            "USDT",
            timestamp,
        )
        for trade_id, quantity, quote, fee, timestamp in (
            ("10", D("0.25"), D("25"), D("0.02"), 1000),
            ("20", D("0.35"), D("35"), D("0.03"), 2000),
            ("30", D("0.40"), D("40"), D("0.04"), 3000),
        )
    )


def multipart_sell_setup(value: SQLiteExchangePersistence) -> tuple[ExchangeTrade, ...]:
    value.persist_trades(COMMAND, multipart_buy_setup(value))
    sell_key = "7" * 64
    sell_client = "ctt1t-zyxwvutsrqponmlkjihgfedcba"
    sell = ExchangeCommand(
        sell_key,
        "6" * 64,
        "5" * 64,
        KEY,
        ExchangeOrderSide.SELL,
        D("1"),
        D("110"),
        D(0),
        D("110"),
        "4" * 64,
        "e" * 64,
        "f" * 64,
        200,
    )
    value.prepare_order(
        execution_root_id=ROOT,
        session_id=SESSION,
        command=sell,
        client_order_id=sell_client,
        occurred_at_ms=200,
    )
    value.begin_submission(sell_key, occurred_at_ms=210)
    value.record_status(
        sell_key,
        status=ExchangeOrderStatus.FILLED,
        occurred_at_ms=240,
        reason_code="fake_exchange",
        exchange_order_id="multipart-sell-order",
        acknowledged_at_ms=220,
        cumulative_executed_base_quantity=D("1"),
        cumulative_quote_quantity=D("121.5"),
        exchange_timestamp_ms=240,
    )
    return tuple(
        ExchangeTrade(
            ExchangeTradeIdentity(
                "binance-spot",
                ExecutionEnvironment.TESTNET,
                "account-m48",
                "BTCUSDT",
                "multipart-sell-order",
                trade_id,
            ),
            sell_client,
            KEY,
            ExchangeOrderSide.SELL,
            quantity,
            price,
            quote,
            fee,
            "USDT",
            timestamp,
        )
        for trade_id, quantity, price, quote, fee, timestamp in (
            ("sell-10", D("0.25"), D("110"), D("27.5"), D("0.02"), 4000),
            ("sell-20", D("0.35"), D("120"), D("42"), D("0.03"), 5000),
            ("sell-30", D("0.40"), D("130"), D("52"), D("0.04"), 6000),
        )
    )


def settlement(value: SQLiteExchangePersistence, managed: Portfolio) -> SQLiteExchangeSettlement:
    return SQLiteExchangeSettlement(
        value,
        managed,
        execution_root_id=ROOT,
        session_id=SESSION,
        config_fingerprint="8" * 64,
        timestamp_provider=lambda: 200,
    )


class CountingGateway:
    def __init__(self) -> None:
        self.calls = 0

    def submit_market_order(self, request):
        self.calls += 1
        from app.exchange_execution import SubmissionObservation, TransportOutcomeKind

        return SubmissionObservation(
            TransportOutcomeKind.DEFINITELY_NOT_TRANSMITTED, request.client_order_id
        )


def risk_inputs(value: SQLiteExchangePersistence, generation: int):
    session = value.load_session(SESSION)
    context = ExchangeRiskContext(
        ReconciledAccountState(
            "account-m48",
            KEY,
            "BTC",
            "USDT",
            D(0),
            D(0),
            D(20000),
            D(0),
            RiskReconciliationStatus.SAFE,
            generation,
        ),
        PendingExposure(),
        session,
        None,
        0,
        0,
        value.read_control_state(ROOT).kill_switch.generation,
    )
    assessment = ExchangeRiskAssessment(
        ExchangeRiskOutcome.APPROVED,
        RiskDecisionOutcome.APPROVED,
        "risk-m48",
        SESSION,
        (),
        None,
    )
    return assessment, context


def test_quote_fee_settlement_is_atomic_exact_once_and_restores(tmp_path: Path) -> None:
    path = tmp_path / "m48.sqlite3"
    value = store(path)
    persist_quote_fee_trade(value)
    managed = portfolio()
    service = settlement(value, managed)
    service.initialize()
    result = service.settle_pending()
    assert result.status is ExchangeSettlementStatus.SYNCHRONIZED
    assert result.settled_count == 1
    assert managed.snapshot.base_balance == D("0.1")
    assert managed.snapshot.quote_balance == D("9999.00")
    assert service.settle_pending().settled_count == 0
    expected = managed.snapshot
    value.close()

    reopened = store(path)
    recovered = portfolio()
    settlement(reopened, recovered).initialize()
    assert recovered.snapshot == expected
    assert reopened.load_session(SESSION).cumulative_executed_turnover == D("10000")
    reopened.close()


def test_three_part_buy_restart_matches_uninterrupted_exact_accounting(tmp_path: Path) -> None:
    uninterrupted = store(tmp_path / "multipart-uninterrupted.sqlite3")
    all_trades = multipart_buy_setup(uninterrupted)
    uninterrupted.persist_trades(COMMAND, all_trades)
    uninterrupted_portfolio = portfolio()
    uninterrupted_service = settlement(uninterrupted, uninterrupted_portfolio)
    uninterrupted_service.initialize()
    assert uninterrupted_service.settle_pending().settled_count == 3
    expected = uninterrupted_portfolio.snapshot
    expected_turnover = uninterrupted.load_session(SESSION).cumulative_executed_turnover
    uninterrupted.close()

    path = tmp_path / "multipart-restarts.sqlite3"
    current = store(path)
    trades = multipart_buy_setup(current)
    current.close()
    for index, trade in enumerate(trades, start=1):
        current = store(path)
        current.persist_trades(COMMAND, (trade,))
        recovered = portfolio()
        service = settlement(current, recovered)
        service.initialize()
        assert service.settle_pending().settled_count == 1
        assert recovered.snapshot.version == index
        current.close()
    final = store(path)
    restored = portfolio()
    final_service = settlement(final, restored)
    final_service.initialize()
    assert restored.snapshot == expected
    assert final.load_session(SESSION).cumulative_executed_turnover == expected_turnover == D("100")
    assert (
        final._connection.execute(
            "SELECT count(*) FROM exchange_trade_settlements WHERE status='SETTLED'"
        ).fetchone()[0]
        == 3
    )
    final.persist_trades(COMMAND, all_trades)
    assert final_service.settle_pending().settled_count == 0
    final.close()


def test_same_timestamp_shuffled_input_has_deterministic_restart_order(tmp_path: Path) -> None:
    def run(path: Path, permutation: tuple[int, ...]):
        value = store(path)
        trades = tuple(replace(trade, traded_at_ms=1000) for trade in multipart_buy_setup(value))
        value.persist_trades(COMMAND, tuple(trades[index] for index in permutation))
        value.close()
        for _ in trades:
            current = store(path)
            managed = portfolio()

            def stop_after_commit(boundary: str) -> None:
                if boundary == "settlement_after_commit":
                    raise RuntimeError("restart after committed settlement")

            service = SQLiteExchangeSettlement(
                current,
                managed,
                execution_root_id=ROOT,
                session_id=SESSION,
                config_fingerprint="8" * 64,
                timestamp_provider=lambda: 200,
                failure_injector=stop_after_commit,
            )
            service.initialize()
            with pytest.raises(RuntimeError, match="restart after committed"):
                service.settle_pending()
            current.close()
        final = store(path)
        restored = portfolio()
        service = settlement(final, restored)
        service.initialize()
        assert service.settle_pending().settled_count == 0
        sequence = tuple(
            row[0]
            for row in final._connection.execute(
                "SELECT exchange_trade_id FROM exchange_trade_settlements ORDER BY settlement_seq"
            ).fetchall()
        )
        result = (
            sequence,
            restored.snapshot,
            final.load_session(SESSION).cumulative_executed_turnover,
        )
        final.close()
        return result

    forward = run(tmp_path / "same-time-forward.sqlite3", (0, 1, 2))
    shuffled = run(tmp_path / "same-time-shuffled.sqlite3", (2, 0, 1))
    reversed_order = run(tmp_path / "same-time-reversed.sqlite3", (2, 1, 0))
    assert forward == shuffled == reversed_order
    assert forward[0] == ("10", "20", "30")


def test_three_part_sell_restart_matches_uninterrupted_exact_accounting(tmp_path: Path) -> None:
    uninterrupted = store(tmp_path / "sell-uninterrupted.sqlite3")
    all_trades = multipart_sell_setup(uninterrupted)
    uninterrupted_portfolio = portfolio()
    uninterrupted_service = settlement(uninterrupted, uninterrupted_portfolio)
    uninterrupted_service.initialize()
    assert uninterrupted_service.settle_pending().settled_count == 3
    uninterrupted.persist_trades("7" * 64, all_trades)
    assert uninterrupted_service.settle_pending().settled_count == 3
    expected = uninterrupted_portfolio.snapshot
    expected_turnover = uninterrupted.load_session(SESSION).cumulative_executed_turnover
    assert expected.base_balance == D(0)
    assert expected.position is None
    uninterrupted.close()

    path = tmp_path / "sell-restarts.sqlite3"
    current = store(path)
    trades = multipart_sell_setup(current)
    managed = portfolio()
    service = settlement(current, managed)
    service.initialize()
    assert service.settle_pending().settled_count == 3
    current.close()
    for index, trade in enumerate(trades, start=4):
        current = store(path)
        current.persist_trades("7" * 64, (trade,))
        recovered = portfolio()
        service = settlement(current, recovered)
        service.initialize()
        assert service.settle_pending().settled_count == 1
        assert recovered.snapshot.version == index
        current.close()
    final = store(path)
    restored = portfolio()
    final_service = settlement(final, restored)
    final_service.initialize()
    assert restored.snapshot == expected
    assert restored.snapshot.base_balance == D(0)
    assert restored.snapshot.position is None
    assert final.load_session(SESSION).cumulative_executed_turnover == expected_turnover
    assert (
        final._connection.execute(
            "SELECT count(*) FROM exchange_trade_settlements WHERE status='SETTLED'"
        ).fetchone()[0]
        == 6
    )
    final.persist_trades("7" * 64, all_trades)
    assert final_service.settle_pending().settled_count == 0
    final.close()


def test_unsupported_fee_blocks_without_mutating_portfolio(tmp_path: Path) -> None:
    value = store(tmp_path / "fee.sqlite3")
    persist_quote_fee_trade(value, "BNB")
    managed = portfolio()
    service = settlement(value, managed)
    service.initialize()
    result = service.settle_pending()
    assert result.status is ExchangeSettlementStatus.BLOCKED_UNSUPPORTED_FEE
    assert result.blocked_count == 1
    assert managed.snapshot.version == 0
    value.close()


@pytest.mark.parametrize("terminal", [ExchangeOrderStatus.CANCELED, ExchangeOrderStatus.EXPIRED])
def test_terminal_after_partial_fill_settles_only_authoritative_trades(
    tmp_path: Path, terminal: ExchangeOrderStatus
) -> None:
    path = tmp_path / f"partial-{terminal.value}.sqlite3"
    value = store(path)
    command = ExchangeCommand(
        COMMAND,
        "b" * 64,
        "c" * 64,
        KEY,
        ExchangeOrderSide.BUY,
        D("0.1"),
        D("100000"),
        D("0.5"),
        D("10000"),
        "d" * 64,
        "e" * 64,
        "f" * 64,
        100,
    )
    value.prepare_order(
        execution_root_id=ROOT,
        session_id=SESSION,
        command=command,
        client_order_id=CLIENT,
        occurred_at_ms=100,
    )
    value.begin_submission(COMMAND, occurred_at_ms=110)
    value.record_status(
        COMMAND,
        status=ExchangeOrderStatus.PARTIALLY_FILLED,
        occurred_at_ms=120,
        reason_code="partial",
        exchange_order_id="partial-order",
        acknowledged_at_ms=115,
        cumulative_executed_base_quantity=D("0.04"),
        cumulative_quote_quantity=D("4000"),
        exchange_timestamp_ms=120,
    )
    trade = ExchangeTrade(
        ExchangeTradeIdentity(
            "binance-spot",
            ExecutionEnvironment.TESTNET,
            "account-m48",
            "BTCUSDT",
            "partial-order",
            "partial-trade",
        ),
        CLIENT,
        KEY,
        ExchangeOrderSide.BUY,
        D("0.04"),
        D("100000"),
        D("4000"),
        D("0.40"),
        "USDT",
        120,
    )
    value.persist_trades(COMMAND, (trade,))
    value.record_status(
        COMMAND,
        status=terminal,
        occurred_at_ms=130,
        reason_code="terminal_after_partial",
        cumulative_executed_base_quantity=D("0.04"),
        cumulative_quote_quantity=D("4000"),
        exchange_timestamp_ms=130,
    )
    managed = portfolio()
    service = settlement(value, managed)
    service.initialize()
    assert service.settle_pending().settled_count == 1
    assert managed.snapshot.base_balance == D("0.04")
    assert managed.snapshot.quote_balance == D("15999.60")
    assert value.load_order(COMMAND).order.status is terminal
    expected = managed.snapshot
    value.close()

    reopened = store(path)
    recovered = portfolio()
    fresh = settlement(reopened, recovered)
    fresh.initialize()
    assert recovered.snapshot == expected
    reopened.persist_trades(COMMAND, (trade,))
    assert fresh.settle_pending().settled_count == 0
    assert recovered.snapshot == expected
    assert reopened.load_order(COMMAND).order.status is terminal
    assert (
        reopened._connection.execute(
            "SELECT count(*) FROM exchange_trade_settlements WHERE status='SETTLED'"
        ).fetchone()[0]
        == 1
    )
    reopened.close()


@pytest.mark.parametrize("unsupported_asset", ["BNB", "BTC"])
def test_supported_then_unsupported_fee_restart_blocks_without_conversion(
    tmp_path: Path, unsupported_asset: str
) -> None:
    path = tmp_path / f"mixed-fee-{unsupported_asset}.sqlite3"
    value = store(path)
    trades = multipart_buy_setup(value)
    first = trades[0]
    unsupported = replace(trades[1], commission_asset=unsupported_asset)
    value.persist_trades(COMMAND, (first,))
    managed = portfolio()
    service = settlement(value, managed)
    service.initialize()
    assert service.settle_pending().settled_count == 1
    legitimate = managed.snapshot
    value.close()

    reopened = store(path)
    recovered = portfolio()
    fresh = settlement(reopened, recovered)
    fresh.initialize()
    assert recovered.snapshot == legitimate
    reopened.persist_trades(COMMAND, (unsupported,))
    result = fresh.settle_pending()
    assert result.status is ExchangeSettlementStatus.BLOCKED_UNSUPPORTED_FEE
    assert result.blocked_count == 1
    assert recovered.snapshot == legitimate
    readiness = evaluate_testnet_readiness(
        enabled=True,
        bootstrap=reopened.load_bootstrap(ROOT),
        recovery=reopened.recover_local(),
        reconciliation_status=ExchangeReconciliationStatus.SAFE,
        reconciliation_generation=1,
        settlement_status=result.status,
        non_terminal_orders=0,
    )
    assert readiness.state is ReadinessState.BLOCKED
    assert readiness.capability is None
    reopened.close()

    final = store(path)
    restored = portfolio()
    final_service = settlement(final, restored)
    final_service.initialize()
    assert restored.snapshot == legitimate
    assert final_service.settle_pending().blocked_count == 0
    assert (
        final._connection.execute(
            "SELECT count(*) FROM exchange_trade_settlements WHERE status='SETTLED'"
        ).fetchone()[0]
        == 1
    )
    assert (
        final._connection.execute(
            "SELECT count(*) FROM exchange_trade_settlements WHERE status='BLOCKED_UNSUPPORTED_FEE'"
        ).fetchone()[0]
        == 1
    )
    final.close()


def test_settlement_requires_durable_safe_history_generation(tmp_path: Path) -> None:
    value = store(tmp_path / "history-barrier.sqlite3")
    persist_quote_fee_trade(value)
    managed = portfolio()
    service = settlement(value, managed)
    service.initialize()
    current = value.read_reconciliation_state(ROOT)
    value.record_reconciliation_run(
        PersistentExchangeReconciliationRun(
            "incomplete-history",
            ROOT,
            SESSION,
            "account-m48",
            current.generation,
            current.generation,
            ExchangeReconciliationStatus.REQUIRED,
            1,
            0,
            0,
            0,
            2,
            ("TRADE_HISTORY_INCOMPLETE",),
            30,
            31,
        )
    )
    result = service.settle_pending()
    assert result.status is ExchangeSettlementStatus.PENDING
    assert managed.snapshot.version == 0
    assert (
        value._connection.execute("SELECT count(*) FROM exchange_trade_settlements").fetchone()[0]
        == 0
    )
    value.close()


def test_late_older_trade_waits_for_complete_history_then_settles_chronologically(
    tmp_path: Path,
) -> None:
    path = tmp_path / "late-older.sqlite3"
    value = store(path)
    trades = multipart_buy_setup(value)
    older, later = trades[:2]
    state = value.read_reconciliation_state(ROOT)
    value.record_reconciliation_run(
        PersistentExchangeReconciliationRun(
            "incomplete-lower-history",
            ROOT,
            SESSION,
            "account-m48",
            state.generation,
            state.generation,
            ExchangeReconciliationStatus.REQUIRED,
            1,
            0,
            0,
            0,
            1,
            ("TRADE_HISTORY_INCOMPLETE",),
            30,
            31,
            1500,
            2500,
            False,
            True,
            "BTCUSDT",
        )
    )
    value.persist_trades(COMMAND, (later,))
    managed = portfolio()
    service = settlement(value, managed)
    service.initialize()
    pending = service.settle_pending()
    assert pending.status is ExchangeSettlementStatus.PENDING
    assert managed.snapshot.version == 0
    assert value._connection.execute("SELECT count(*) FROM exchange_trades").fetchone()[0] == 1

    state = value.read_reconciliation_state(ROOT)
    value.record_reconciliation_run(
        PersistentExchangeReconciliationRun(
            "complete-lower-history",
            ROOT,
            SESSION,
            "account-m48",
            state.generation,
            state.generation + 1,
            ExchangeReconciliationStatus.SAFE,
            1,
            0,
            1,
            0,
            2,
            (),
            40,
            41,
            0,
            41,
            True,
            True,
            "BTCUSDT",
        )
    )
    value.persist_trades(COMMAND, (older,))
    assert service.settle_pending().settled_count == 2
    sequence = tuple(
        row[0]
        for row in value._connection.execute(
            "SELECT exchange_trade_id FROM exchange_trade_settlements ORDER BY settlement_seq"
        ).fetchall()
    )
    assert sequence == ("10", "20")
    delayed = managed.snapshot
    delayed_turnover = value.load_session(SESSION).cumulative_executed_turnover
    value.close()

    direct = store(tmp_path / "late-older-direct.sqlite3")
    direct_trades = multipart_buy_setup(direct)
    direct.persist_trades(COMMAND, direct_trades[:2])
    direct_portfolio = portfolio()
    direct_service = settlement(direct, direct_portfolio)
    direct_service.initialize()
    assert direct_service.settle_pending().settled_count == 2
    assert direct_portfolio.snapshot == delayed
    assert direct.load_session(SESSION).cumulative_executed_turnover == delayed_turnover
    direct.close()


def test_quote_fee_buy_then_sell_manual_accounting_is_exact(tmp_path: Path) -> None:
    value = store(tmp_path / "buy-sell.sqlite3")
    persist_quote_fee_trade(value)
    managed = portfolio()
    service = settlement(value, managed)
    service.initialize()
    service.settle_pending()
    sell_key = "7" * 64
    sell_client = "ctt1t-zyxwvutsrqponmlkjihgfedcba"
    sell = ExchangeCommand(
        sell_key,
        "6" * 64,
        "5" * 64,
        KEY,
        ExchangeOrderSide.SELL,
        D("0.1"),
        D("110000"),
        D(0),
        D("11000"),
        "4" * 64,
        "e" * 64,
        "f" * 64,
        200,
    )
    value.prepare_order(
        execution_root_id=ROOT,
        session_id=SESSION,
        command=sell,
        client_order_id=sell_client,
        occurred_at_ms=200,
    )
    value.begin_submission(sell_key, occurred_at_ms=210)
    value.record_status(
        sell_key,
        status=ExchangeOrderStatus.FILLED,
        occurred_at_ms=230,
        reason_code="fake_exchange",
        exchange_order_id="900719925474099399999",
        acknowledged_at_ms=220,
        cumulative_executed_base_quantity=D("0.1"),
        cumulative_quote_quantity=D("11000"),
        exchange_timestamp_ms=230,
    )
    value.persist_trades(
        sell_key,
        (
            ExchangeTrade(
                ExchangeTradeIdentity(
                    "binance-spot",
                    ExecutionEnvironment.TESTNET,
                    "account-m48",
                    "BTCUSDT",
                    "900719925474099399999",
                    "9223372036854775808999",
                ),
                sell_client,
                KEY,
                ExchangeOrderSide.SELL,
                D("0.1"),
                D("110000"),
                D("11000"),
                D("1.00"),
                "USDT",
                230,
            ),
        ),
    )
    result = service.settle_pending()
    snapshot = managed.snapshot
    assert result.settled_count == 1
    assert snapshot.base_balance == D(0)
    assert snapshot.quote_balance == D("20998.00")
    assert snapshot.position is None
    assert snapshot.realised_pnl == D("998.00")
    assert snapshot.total_fees == D("2.00")
    assert snapshot.version == 2
    assert value.load_session(SESSION).cumulative_executed_turnover == D("21000")
    value.close()


def test_readiness_is_fail_closed_and_capability_binds_lineage(tmp_path: Path) -> None:
    value = store(tmp_path / "ready.sqlite3")
    value.set_kill_switch(
        ROOT,
        state=KillSwitchState.CLEARED,
        expected_generation=0,
        reason_code="operator_testnet_enable",
        audit_identifier="m48-ready",
        occurred_at_ms=30,
    )
    bootstrap = value.load_bootstrap(ROOT)
    recovery = value.recover_local()
    readiness = evaluate_testnet_readiness(
        enabled=True,
        bootstrap=bootstrap,
        recovery=recovery,
        reconciliation_status=ExchangeReconciliationStatus.SAFE,
        reconciliation_generation=1,
        settlement_status=ExchangeSettlementStatus.SYNCHRONIZED,
        non_terminal_orders=0,
    )
    assert readiness.state is ReadinessState.READY
    assert readiness.capability is not None
    assert readiness.capability.account_fingerprint == "account-m48"
    assert readiness.capability.session_id == SESSION
    blocked = evaluate_testnet_readiness(
        enabled=True,
        bootstrap=bootstrap,
        recovery=recovery,
        reconciliation_status=ExchangeReconciliationStatus.REQUIRED,
        reconciliation_generation=0,
        settlement_status=ExchangeSettlementStatus.PENDING,
        non_terminal_orders=1,
    )
    assert blocked.state is ReadinessState.BLOCKED
    assert blocked.capability is None
    assert ReadinessReason.SETTLEMENT_PENDING in blocked.reasons
    value.close()


def test_live_mode_is_structurally_rejected() -> None:
    from app.core.config import Settings

    with pytest.raises(ValueError, match="LIVE"):
        Settings(trading_mode="live")
    with pytest.raises(ValueError, match="testnet_execution_enabled"):
        Settings(trading_mode="testnet")
    configured = Settings(
        trading_mode="testnet",
        testnet_execution_enabled=True,
        persistence_enabled=True,
        binance_testnet_api_key="test-key",
        binance_testnet_secret_key="test-secret",
        binance_testnet_account_fingerprint="account-m48",
    )
    assert "test-key" not in repr(configured)
    assert "test-secret" not in repr(configured)
    with pytest.raises(ValueError, match="credentials"):
        Settings(
            trading_mode="testnet",
            testnet_execution_enabled=True,
            persistence_enabled=True,
        )
    with pytest.raises(ValueError, match="cannot be enabled together"):
        Settings(
            trading_mode="testnet",
            testnet_execution_enabled=True,
            persistence_enabled=True,
            paper_trading_enabled=True,
            binance_testnet_api_key="key",
            binance_testnet_secret_key="secret",
            binance_testnet_account_fingerprint="account-m48",
        )
    with pytest.raises(ValueError, match="persistence_enabled"):
        Settings(
            trading_mode="testnet",
            testnet_execution_enabled=True,
            binance_testnet_api_key="key",
            binance_testnet_secret_key="secret",
            binance_testnet_account_fingerprint="account-m48",
        )
    with pytest.raises(ValueError, match="requires testnet mode"):
        Settings(testnet_execution_enabled=True)


def test_runtime_startup_runs_ordered_gates_without_creating_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = store(tmp_path / "startup.sqlite3")
    managed = portfolio()
    settle = settlement(value, managed)
    reconciler = object.__new__(ExchangeReconciler)
    calls: list[str] = []
    original_recover = value.recover_local
    original_initialize = settle.initialize
    original_settle = settle.settle_pending

    def recover():
        calls.append("recover")
        return original_recover()

    def reconcile(self, root_id: str):
        calls.append(f"reconcile:{root_id}")
        return SimpleNamespace(outcome=ExchangeReconciliationStatus.SAFE, generation_after=1)

    def initialize():
        calls.append("initialize")
        return original_initialize()

    def settle_pending():
        calls.append("settle")
        return original_settle()

    monkeypatch.setattr(value, "recover_local", recover)
    monkeypatch.setattr(ExchangeReconciler, "reconcile", reconcile)
    monkeypatch.setattr(settle, "initialize", initialize)
    monkeypatch.setattr(settle, "settle_pending", settle_pending)
    result = RuntimeStartup(value, reconciler, settle, execution_root_id=ROOT).start(enabled=True)
    assert calls == ["recover", f"reconcile:{ROOT}", "initialize", "settle"]
    assert result.state is ReadinessState.BLOCKED
    assert result.reasons == (ReadinessReason.KILL_SWITCH_ACTIVE,)
    value.close()


def test_readiness_disabled_and_all_fail_closed_reasons(tmp_path: Path) -> None:
    value = store(tmp_path / "disabled.sqlite3")
    bootstrap = value.load_bootstrap(ROOT)
    recovery = value.recover_local()
    object.__setattr__(recovery, "status", LocalExchangeRecoveryStatus.RECOVERY_REQUIRED)
    object.__setattr__(bootstrap.session, "state", ExecutionSessionState.CLOSED)
    result = evaluate_testnet_readiness(
        enabled=False,
        bootstrap=bootstrap,
        recovery=recovery,
        reconciliation_status=ExchangeReconciliationStatus.CONFLICT,
        reconciliation_generation=0,
        settlement_status=ExchangeSettlementStatus.BLOCKED_UNSUPPORTED_FEE,
        non_terminal_orders=1,
    )
    assert result.state is ReadinessState.DISABLED
    assert result.capability is None
    assert set(result.reasons) == set(ReadinessReason) - {ReadinessReason.SETTLEMENT_PENDING}
    value.close()


def test_ready_result_rejects_stale_or_cross_lineage_capability(tmp_path: Path) -> None:
    value = store(tmp_path / "ready-invariants.sqlite3")
    value.set_kill_switch(
        ROOT,
        state=KillSwitchState.CLEARED,
        expected_generation=0,
        reason_code="operator_testnet_enable",
        audit_identifier="m48-ready",
        occurred_at_ms=30,
    )
    ready = evaluate_testnet_readiness(
        enabled=True,
        bootstrap=value.load_bootstrap(ROOT),
        recovery=value.recover_local(),
        reconciliation_status=ExchangeReconciliationStatus.SAFE,
        reconciliation_generation=1,
        settlement_status=ExchangeSettlementStatus.SYNCHRONIZED,
        non_terminal_orders=0,
    )
    assert ready.capability is not None
    for changes in (
        {"session_id": "different-session"},
        {"reconciliation_generation": 2},
        {"control_generation": 2},
    ):
        with pytest.raises(ValueError, match="readiness lineage"):
            replace(ready, **changes)
    value.close()


def test_settlement_rolls_back_before_commit_and_replays_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "crash.sqlite3"
    value = store(path)
    persist_quote_fee_trade(value)
    managed = portfolio()

    def fail(boundary: str) -> None:
        if boundary == "settlement_before_commit":
            raise RuntimeError("simulated crash")

    crashing = SQLiteExchangeSettlement(
        value,
        managed,
        execution_root_id=ROOT,
        session_id=SESSION,
        config_fingerprint="8" * 64,
        timestamp_provider=lambda: 200,
        failure_injector=fail,
    )
    crashing.initialize()
    with pytest.raises(RuntimeError, match="simulated crash"):
        crashing.settle_pending()
    assert managed.snapshot.version == 0
    assert (
        value._connection.execute("SELECT count(*) FROM exchange_trade_settlements").fetchone()[0]
        == 0
    )
    value.close()

    reopened = store(path)
    recovered = portfolio()
    service = settlement(reopened, recovered)
    service.initialize()
    assert service.settle_pending().settled_count == 1
    assert recovered.snapshot.version == 1
    reopened.close()


@pytest.mark.parametrize("fee_asset", ["BTC", "BNB", "USDT"])
def test_exact_zero_commission_is_asset_neutral_and_exactly_once(
    tmp_path: Path, fee_asset: str
) -> None:
    path = tmp_path / f"zero-{fee_asset}.sqlite3"
    value = store(path)
    persist_quote_fee_trade(value, fee_asset, "-0" if fee_asset == "BTC" else "0.00000000")
    managed = portfolio()
    service = settlement(value, managed)
    service.initialize()
    first = service.settle_pending()
    assert first.status is ExchangeSettlementStatus.SYNCHRONIZED
    assert first.resolved_count == (0 if fee_asset == "USDT" else 1)
    assert first.settled_count == (1 if fee_asset == "USDT" else 0)
    assert managed.snapshot.base_balance == D("0.1")
    assert managed.snapshot.quote_balance == D("10000")
    assert managed.snapshot.total_fees == 0
    assert managed.snapshot.turnover == D("10000")
    assert managed.snapshot.version == 1
    assert tuple(
        value._connection.execute(
            "SELECT commission,commission_asset FROM exchange_trades"
        ).fetchone()
    ) == ("0", fee_asset)
    blocked = value._connection.execute(
        "SELECT count(*) FROM exchange_trade_settlements WHERE status='BLOCKED_UNSUPPORTED_FEE'"
    ).fetchone()[0]
    resolutions = value._connection.execute(
        "SELECT count(*) FROM exchange_trade_settlement_resolutions"
    ).fetchone()[0]
    assert (blocked, resolutions) == ((0, 0) if fee_asset == "USDT" else (1, 1))
    second = service.settle_pending()
    assert second.status is ExchangeSettlementStatus.SYNCHRONIZED
    assert second.resolved_count == 0
    assert managed.snapshot.version == 1
    value.close()

    reopened = store(path)
    recovered = portfolio()
    restarted = settlement(reopened, recovered)
    restarted.initialize()
    assert restarted.settle_pending().status is ExchangeSettlementStatus.SYNCHRONIZED
    assert recovered.snapshot.version == 1
    assert reopened.load_session(SESSION).cumulative_executed_turnover == D("10000")
    reopened.close()


@pytest.mark.parametrize("fee_asset", ["BTC", "BNB", "OTHER"])
def test_positive_unsupported_commission_remains_unresolved(tmp_path: Path, fee_asset: str) -> None:
    value = store(tmp_path / f"positive-{fee_asset}.sqlite3")
    persist_quote_fee_trade(value, fee_asset, "0.00000001")
    managed = portfolio()
    service = settlement(value, managed)
    service.initialize()
    result = service.settle_pending()
    assert result.status is ExchangeSettlementStatus.BLOCKED_UNSUPPORTED_FEE
    assert result.resolved_count == 0
    assert managed.snapshot.version == 0
    assert (
        value._connection.execute(
            "SELECT count(*) FROM exchange_trade_settlement_resolutions"
        ).fetchone()[0]
        == 0
    )
    value.close()


@pytest.mark.parametrize(
    "boundary",
    [
        "resolution_before_record",
        "resolution_after_record",
        "resolution_after_current",
        "resolution_after_turnover",
        "resolution_before_commit",
    ],
)
@pytest.mark.parametrize("crash", [RuntimeError, KeyboardInterrupt, SystemExit, GeneratorExit])
def test_zero_resolution_precommit_baseexceptions_roll_back(
    tmp_path: Path, boundary: str, crash: type[BaseException]
) -> None:
    path = tmp_path / f"resolution-{boundary}-{crash.__name__}.sqlite3"
    value = store(path)
    persist_quote_fee_trade(value, "BTC", "0")
    managed = portfolio()

    def fail(point: str) -> None:
        if point == boundary:
            raise crash()

    service = SQLiteExchangeSettlement(
        value,
        managed,
        execution_root_id=ROOT,
        session_id=SESSION,
        config_fingerprint="8" * 64,
        timestamp_provider=lambda: 200,
        failure_injector=fail,
    )
    service.initialize()
    with pytest.raises(crash):
        service.settle_pending()
    assert managed.snapshot.version == 0
    assert (
        value._connection.execute(
            "SELECT count(*) FROM exchange_trade_settlement_resolutions"
        ).fetchone()[0]
        == 0
    )
    assert value.load_session(SESSION).cumulative_executed_turnover == 0
    value.close()

    reopened = store(path)
    recovered = portfolio()
    restarted = settlement(reopened, recovered)
    restarted.initialize()
    assert restarted.settle_pending().status is ExchangeSettlementStatus.SYNCHRONIZED
    assert recovered.snapshot.version == 1
    reopened.close()


def test_zero_resolution_lost_commit_ack_recovers_once(tmp_path: Path) -> None:
    path = tmp_path / "resolution-lost-ack.sqlite3"
    value = store(path)
    persist_quote_fee_trade(value, "BTC", "0")
    managed = portfolio()

    def lose_ack(point: str) -> None:
        if point == "resolution_after_commit":
            raise RuntimeError("lost resolution commit acknowledgement")

    service = SQLiteExchangeSettlement(
        value,
        managed,
        execution_root_id=ROOT,
        session_id=SESSION,
        config_fingerprint="8" * 64,
        timestamp_provider=lambda: 200,
        failure_injector=lose_ack,
    )
    service.initialize()
    with pytest.raises(RuntimeError, match="lost resolution"):
        service.settle_pending()
    value.close()
    for _ in range(3):
        reopened = store(path)
        recovered = portfolio()
        restarted = settlement(reopened, recovered)
        restarted.initialize()
        result = restarted.settle_pending()
        assert result.status is ExchangeSettlementStatus.SYNCHRONIZED
        assert result.resolved_count == 0
        assert recovered.snapshot.version == 1
        assert (
            reopened._connection.execute(
                "SELECT count(*) FROM exchange_trade_settlement_resolutions"
            ).fetchone()[0]
            == 1
        )
        reopened.close()


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt, SystemExit, GeneratorExit])
def test_zero_resolution_committed_memory_failure_degrades_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: type[BaseException]
) -> None:
    path = tmp_path / f"resolution-memory-{failure.__name__}.sqlite3"
    value = store(path)
    persist_quote_fee_trade(value, "BTC", "0")
    managed = portfolio()
    service = settlement(value, managed)
    service.initialize()

    def fail_memory(_transition) -> None:
        raise failure("resolution memory synchronization failed")

    monkeypatch.setattr(managed, "apply_committed_transition", fail_memory)
    with pytest.raises(failure, match="memory synchronization"):
        service.settle_pending()
    with pytest.raises(ExchangeSettlementError, match="degraded"):
        service.restore_memory()
    value.close()

    reopened = store(path)
    recovered = portfolio()
    restarted = settlement(reopened, recovered)
    restarted.initialize()
    assert restarted.settle_pending().status is ExchangeSettlementStatus.SYNCHRONIZED
    assert recovered.snapshot.version == 1
    reopened.close()


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("record_digest", "0" * 64, "authority mismatch"),
        ("after_state_digest", "0" * 64, "state mismatch"),
        ("fill_id", "0" * 64, "authority mismatch"),
    ],
)
def test_zero_resolution_corruption_fails_closed(
    tmp_path: Path, column: str, value: str, message: str
) -> None:
    path = tmp_path / f"resolution-corrupt-{column}.sqlite3"
    current = store(path)
    persist_quote_fee_trade(current, "BTC", "0")
    service = settlement(current, portfolio())
    service.initialize()
    assert service.settle_pending().status is ExchangeSettlementStatus.SYNCHRONIZED
    current._connection.execute("DROP TRIGGER exchange_trade_settlement_resolutions_no_update")
    current._connection.execute(
        f"UPDATE exchange_trade_settlement_resolutions SET {column}=?", (value,)
    )
    current._connection.commit()
    current.close()

    reopened = store(path)
    broken = settlement(reopened, portfolio())
    with pytest.raises(ExchangeSettlementError, match=message):
        broken.initialize()
    reopened.close()


def test_zero_resolution_and_original_block_are_append_only(tmp_path: Path) -> None:
    value = store(tmp_path / "resolution-append-only.sqlite3")
    persist_quote_fee_trade(value, "BTC", "0")
    service = settlement(value, portfolio())
    service.initialize()
    service.settle_pending()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        value._connection.execute("DELETE FROM exchange_trade_settlement_resolutions")
    value._connection.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        value._connection.execute(
            "UPDATE exchange_trade_settlements SET status='SETTLED' "
            "WHERE status='BLOCKED_UNSUPPORTED_FEE'"
        )
    value._connection.rollback()
    assert (
        value._connection.execute(
            "SELECT count(*) FROM exchange_trade_settlements WHERE status='BLOCKED_UNSUPPORTED_FEE'"
        ).fetchone()[0]
        == 1
    )
    value.close()


def test_zero_resolution_revalidates_coverage_inside_transaction(tmp_path: Path) -> None:
    value = store(tmp_path / "resolution-coverage-race.sqlite3")
    persist_quote_fee_trade(value, "BTC", "0")
    service = settlement(value, portfolio())
    service.initialize()
    original = service._coverage_is_complete
    calls = 0

    def changes_after_initial_check(reconciliation) -> bool:
        nonlocal calls
        calls += 1
        return original(reconciliation) if calls == 1 else False

    service._coverage_is_complete = changes_after_initial_check
    with pytest.raises(ExchangeSettlementError, match="authority changed"):
        service.settle_pending()
    assert (
        value._connection.execute(
            "SELECT count(*) FROM exchange_trade_settlement_resolutions"
        ).fetchone()[0]
        == 0
    )
    value.close()


def test_zero_resolution_portfolio_and_session_compare_and_swap_fail_closed(
    tmp_path: Path,
) -> None:
    for target in ("portfolio", "session"):
        value = store(tmp_path / f"resolution-cas-{target}.sqlite3")
        persist_quote_fee_trade(value, "BTC", "0")
        service = settlement(value, portfolio())
        service.initialize()
        if target == "portfolio":

            def conflict(point: str, current=value) -> None:
                if point == "resolution_before_current":
                    current._connection.execute(
                        "UPDATE exchange_portfolio_current SET state_digest=?", ("0" * 64,)
                    )

            service._failure = conflict
            message = "Portfolio version conflict"
        else:
            service._session_generation = lambda _session: -1
            message = "session generation conflict"
        with pytest.raises(ExchangeSettlementError, match=message):
            service.settle_pending()
        assert (
            value._connection.execute(
                "SELECT count(*) FROM exchange_trade_settlement_resolutions"
            ).fetchone()[0]
            == 0
        )
        value.close()


def test_zero_resolution_duplicate_commit_result_and_legacy_helpers(tmp_path: Path) -> None:
    value = store(tmp_path / "resolution-diagnostic-branches.sqlite3")
    persist_quote_fee_trade(value, "BTC", "0")
    service = settlement(value, portfolio())
    service.initialize()
    service._commit_zero_resolution = lambda *args: False
    result = service.settle_pending()
    assert result.status is ExchangeSettlementStatus.BLOCKED_UNSUPPORTED_FEE
    object.__setattr__(value._trading.config, "expected_schema_version", 5)
    assert service._unresolved_blocked_count() == 1
    assert service._resolve_zero_commission_blocks(value.read_reconciliation_state(ROOT)) == 0
    value.close()


@pytest.mark.parametrize(
    "boundary",
    [
        "settlement_after_record",
        "settlement_after_current",
        "settlement_after_turnover",
        "settlement_before_commit",
    ],
)
@pytest.mark.parametrize("crash", [RuntimeError, KeyboardInterrupt, SystemExit, GeneratorExit])
def test_every_settlement_precommit_write_rolls_back_for_baseexceptions(
    tmp_path: Path, boundary: str, crash: type[BaseException]
) -> None:
    path = tmp_path / f"rollback-{boundary}-{crash.__name__}.sqlite3"
    value = store(path)
    persist_quote_fee_trade(value)
    managed = portfolio()

    def fail(point: str) -> None:
        if point == boundary:
            raise crash()

    service = SQLiteExchangeSettlement(
        value,
        managed,
        execution_root_id=ROOT,
        session_id=SESSION,
        config_fingerprint="8" * 64,
        timestamp_provider=lambda: 200,
        failure_injector=fail,
    )
    service.initialize()
    with pytest.raises(crash):
        service.settle_pending()
    assert managed.snapshot.version == 0
    assert (
        value._connection.execute("SELECT count(*) FROM exchange_trade_settlements").fetchone()[0]
        == 0
    )
    assert value.load_session(SESSION).cumulative_executed_turnover == D(0)
    current = value._connection.execute(
        "SELECT portfolio_version,last_settlement_seq FROM exchange_portfolio_current"
    ).fetchone()
    assert tuple(current) == (0, None)
    value.close()


def test_settlement_postcommit_lost_ack_is_recovered_exactly_once_across_restarts(
    tmp_path: Path,
) -> None:
    path = tmp_path / "postcommit.sqlite3"
    value = store(path)
    persist_quote_fee_trade(value)
    managed = portfolio()

    def lose_ack(point: str) -> None:
        if point == "settlement_after_commit":
            raise RuntimeError("lost commit acknowledgement")

    service = SQLiteExchangeSettlement(
        value,
        managed,
        execution_root_id=ROOT,
        session_id=SESSION,
        config_fingerprint="8" * 64,
        timestamp_provider=lambda: 200,
        failure_injector=lose_ack,
    )
    service.initialize()
    with pytest.raises(RuntimeError, match="lost commit"):
        service.settle_pending()
    assert managed.snapshot.version == 0
    value.close()
    expected = None
    for _ in range(5):
        reopened = store(path)
        recovered = portfolio()
        next_service = settlement(reopened, recovered)
        next_service.initialize()
        assert next_service.settle_pending().settled_count == 0
        expected = expected or recovered.snapshot
        assert recovered.snapshot == expected
        assert (
            reopened._connection.execute(
                "SELECT count(*) FROM exchange_trade_settlements WHERE status='SETTLED'"
            ).fetchone()[0]
            == 1
        )
        assert reopened.load_session(SESSION).cumulative_executed_turnover == D("10000")
        reopened.close()


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt, SystemExit, GeneratorExit])
def test_committed_memory_sync_failure_degrades_old_runtime_and_recovers_fresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: type[BaseException],
) -> None:
    path = tmp_path / f"memory-sync-{failure.__name__}.sqlite3"
    value = store(path)
    persist_quote_fee_trade(value)
    managed = portfolio()
    service = settlement(value, managed)
    service.initialize()

    def fail_memory_sync(_transition) -> None:
        raise failure("injected memory synchronization failure")

    monkeypatch.setattr(managed, "apply_committed_transition", fail_memory_sync)
    with pytest.raises(failure, match="memory synchronization"):
        service.settle_pending()
    assert managed.snapshot.version == 0
    assert (
        value._connection.execute(
            "SELECT count(*) FROM exchange_trade_settlements WHERE status='SETTLED'"
        ).fetchone()[0]
        == 1
    )
    assert value.load_session(SESSION).cumulative_executed_turnover == D("10000")
    with pytest.raises(ExchangeSettlementError, match="degraded"):
        service.validate_authority()
    with pytest.raises(ExchangeSettlementError, match="degraded"):
        service.settle_pending()
    value.close()

    reopened = store(path)
    recovered = portfolio()
    fresh = settlement(reopened, recovered)
    fresh.initialize()
    assert recovered.snapshot.version == 1
    assert fresh.settle_pending().settled_count == 0
    assert reopened.load_session(SESSION).cumulative_executed_turnover == D("10000")
    assert (
        reopened._connection.execute(
            "SELECT count(*) FROM exchange_trade_settlements WHERE status='SETTLED'"
        ).fetchone()[0]
        == 1
    )
    reopened.close()


@pytest.mark.parametrize(
    ("table", "column", "value"),
    [
        ("exchange_trade_settlements", "settlement_id", "f" * 64),
        ("exchange_trade_settlements", "exchange_trade_id", "wrong-trade"),
        ("exchange_trade_settlements", "durable_command_key", "f" * 64),
        ("exchange_trade_settlements", "session_id", "wrong-session"),
        ("exchange_trade_settlements", "reconciliation_id", "wrong-run"),
        ("exchange_trade_settlements", "reconciliation_generation", 99),
        ("exchange_trade_settlements", "portfolio_version_before", 7),
        ("exchange_trade_settlements", "portfolio_version_after", 8),
        ("exchange_trade_settlements", "after_state_payload", "{}"),
        ("exchange_trade_settlements", "record_payload", "{}"),
        ("exchange_trade_settlements", "status", "BLOCKED_UNSUPPORTED_FEE"),
        ("exchange_trades", "exchange_order_id", "wrong-order"),
        ("exchange_reconciliation_coverage", "coverage_start_ms", 11),
        ("exchange_reconciliation_coverage", "coverage_end_ms", 10),
        ("exchange_reconciliation_coverage", "lower_bound_complete", 0),
        ("exchange_reconciliation_coverage", "upper_bound_complete", 0),
        ("exchange_reconciliation_coverage", "algorithm_version", "wrong/v1"),
        ("exchange_reconciliation_coverage", "evidence_digest", "0" * 64),
        ("exchange_portfolio_current", "state_payload", "{}"),
        ("exchange_portfolio_current", "state_digest", "0" * 64),
    ],
)
def test_settlement_recovery_corruption_matrix(
    tmp_path: Path, table: str, column: str, value
) -> None:
    database = store(tmp_path / f"corruption-{table}-{column}.sqlite3")
    persist_quote_fee_trade(database)
    service = settlement(database, portfolio())
    service.initialize()
    service.settle_pending()
    if table == "exchange_trade_settlements":
        database._connection.execute("DROP TRIGGER exchange_trade_settlements_no_update")
    elif table == "exchange_reconciliation_coverage":
        database._connection.execute("DROP TRIGGER exchange_reconciliation_coverage_no_update")
    try:
        database._connection.execute(f"UPDATE {table} SET {column}=?", (value,))
        database._connection.commit()
    except sqlite3.IntegrityError:
        database._connection.rollback()
        unchanged = database._connection.execute(f"SELECT {column} FROM {table}").fetchone()[0]
        assert unchanged != value
        service.validate_authority()
        database.close()
        return
    with pytest.raises(ExchangeSettlementError):
        service.validate_authority()
    database.close()


@pytest.mark.parametrize("column", ["record_digest", "after_state_digest"])
def test_settlement_recovery_rejects_corrupt_authority(tmp_path: Path, column: str) -> None:
    value = store(tmp_path / f"corrupt-{column}.sqlite3")
    persist_quote_fee_trade(value)
    managed = portfolio()
    service = settlement(value, managed)
    service.initialize()
    service.settle_pending()
    value._connection.execute("DROP TRIGGER exchange_trade_settlements_no_update")
    value._connection.execute(f"UPDATE exchange_trade_settlements SET {column}=?", ("0" * 64,))
    value._connection.commit()
    with pytest.raises(ExchangeSettlementError):
        service.validate_authority()
    value.close()


def test_settlement_recovery_rejects_turnover_split_brain(tmp_path: Path) -> None:
    value = store(tmp_path / "turnover-corrupt.sqlite3")
    persist_quote_fee_trade(value)
    managed = portfolio()
    service = settlement(value, managed)
    service.initialize()
    service.settle_pending()
    value._connection.execute(
        "UPDATE exchange_sessions SET executed_turnover='1' WHERE session_id=?", (SESSION,)
    )
    value._connection.commit()
    with pytest.raises(ExchangeSettlementError, match="turnover"):
        service.validate_authority()
    value.close()


def test_authoritative_gate_is_only_gateway_path_and_revalidates_durable_state(
    tmp_path: Path,
) -> None:
    value = store(tmp_path / "gate.sqlite3")
    value.set_kill_switch(
        ROOT,
        state=KillSwitchState.CLEARED,
        expected_generation=0,
        reason_code="operator",
        audit_identifier="gate-test",
        occurred_at_ms=30,
    )
    managed = portfolio()
    settlement(value, managed).initialize()
    command = ExchangeCommand(
        COMMAND,
        "b" * 64,
        "c" * 64,
        KEY,
        ExchangeOrderSide.BUY,
        D("0.1"),
        D("100000"),
        D("0.5"),
        D("10000"),
        "d" * 64,
        "e" * 64,
        "f" * 64,
        100,
    )
    value.prepare_order(
        execution_root_id=ROOT,
        session_id=SESSION,
        command=command,
        client_order_id=CLIENT,
        occurred_at_ms=100,
    )
    gateway = CountingGateway()
    lifecycle = DurableExchangeOrderLifecycle(value, gateway, timestamp_provider=lambda: 110)
    gate = PreSubmitGate(value, lifecycle, execution_root_id=ROOT, max_authorization_age_ms=1000)
    assessment, context = risk_inputs(value, 1)
    capability = evaluate_testnet_readiness(
        enabled=True,
        bootstrap=value.load_bootstrap(ROOT),
        recovery=value.recover_local(),
        reconciliation_status=ExchangeReconciliationStatus.SAFE,
        reconciliation_generation=1,
        settlement_status=ExchangeSettlementStatus.SYNCHRONIZED,
        non_terminal_orders=0,
    ).capability
    with pytest.raises(ExchangeSubmissionSafetyError):
        lifecycle._submit_authorized(None)  # type: ignore[arg-type]
    result, order = gate.authorize_and_submit(
        COMMAND,
        assessment=assessment,
        context=context,
        capability=capability,
        expected_rules_fingerprint="e" * 64,
        now_ms=110,
    )
    assert result.authorized and order is not None
    assert gateway.calls == 1
    assert value.load_session(SESSION).submission_count == 1
    value.close()


def test_final_gate_blocks_stale_control_and_reconciliation_capabilities(tmp_path: Path) -> None:
    value = store(tmp_path / "races.sqlite3")
    value.set_kill_switch(
        ROOT,
        state=KillSwitchState.CLEARED,
        expected_generation=0,
        reason_code="operator",
        audit_identifier="gate-test",
        occurred_at_ms=30,
    )
    settlement(value, portfolio()).initialize()
    command = ExchangeCommand(
        COMMAND,
        "b" * 64,
        "c" * 64,
        KEY,
        ExchangeOrderSide.BUY,
        D("0.1"),
        D("100000"),
        D("0.5"),
        D("10000"),
        "d" * 64,
        "e" * 64,
        "f" * 64,
        100,
    )
    value.prepare_order(
        execution_root_id=ROOT,
        session_id=SESSION,
        command=command,
        client_order_id=CLIENT,
        occurred_at_ms=100,
    )
    gateway = CountingGateway()
    lifecycle = DurableExchangeOrderLifecycle(value, gateway, timestamp_provider=lambda: 110)
    gate = PreSubmitGate(value, lifecycle, execution_root_id=ROOT, max_authorization_age_ms=1000)
    assessment, context = risk_inputs(value, 1)
    old_capability = evaluate_testnet_readiness(
        enabled=True,
        bootstrap=value.load_bootstrap(ROOT),
        recovery=value.recover_local(),
        reconciliation_status=ExchangeReconciliationStatus.SAFE,
        reconciliation_generation=1,
        settlement_status=ExchangeSettlementStatus.SYNCHRONIZED,
        non_terminal_orders=0,
    ).capability
    value.set_kill_switch(
        ROOT,
        state=KillSwitchState.ACTIVE,
        expected_generation=1,
        reason_code="race",
        audit_identifier="race",
        occurred_at_ms=105,
    )
    blocked, order = gate.authorize_and_submit(
        COMMAND,
        assessment=assessment,
        context=context,
        capability=old_capability,
        expected_rules_fingerprint="e" * 64,
        now_ms=110,
    )
    assert not blocked.authorized and order is None
    assert gateway.calls == 0
    assert value.load_order(COMMAND).order.status is ExchangeOrderStatus.PREPARED
    assert value.load_session(SESSION).submission_count == 0
    value.close()


def gate_baseline(path: Path, *, maximum_age: int = 1000):
    value = store(path)
    value.set_kill_switch(
        ROOT,
        state=KillSwitchState.CLEARED,
        expected_generation=0,
        reason_code="operator",
        audit_identifier="matrix",
        occurred_at_ms=30,
    )
    managed = portfolio()
    settle = settlement(value, managed)
    settle.initialize()
    command = ExchangeCommand(
        COMMAND,
        "b" * 64,
        "c" * 64,
        KEY,
        ExchangeOrderSide.BUY,
        D("0.1"),
        D("100000"),
        D("0.5"),
        D("10000"),
        "d" * 64,
        "e" * 64,
        "f" * 64,
        100,
    )
    value.prepare_order(
        execution_root_id=ROOT,
        session_id=SESSION,
        command=command,
        client_order_id=CLIENT,
        occurred_at_ms=100,
    )
    gateway = CountingGateway()
    lifecycle = DurableExchangeOrderLifecycle(value, gateway, timestamp_provider=lambda: 110)
    gate = PreSubmitGate(
        value,
        lifecycle,
        execution_root_id=ROOT,
        max_authorization_age_ms=maximum_age,
        settlement=settle,
    )
    assessment, context = risk_inputs(value, 1)
    capability = evaluate_testnet_readiness(
        enabled=True,
        bootstrap=value.load_bootstrap(ROOT),
        recovery=value.recover_local(),
        reconciliation_status=ExchangeReconciliationStatus.SAFE,
        reconciliation_generation=1,
        settlement_status=ExchangeSettlementStatus.SYNCHRONIZED,
        non_terminal_orders=0,
    ).capability
    assert capability is not None
    return value, settle, gate, lifecycle, gateway, assessment, context, capability


def assert_gate_has_no_effect(value, gateway, before) -> None:
    durable = value.load_order(COMMAND)
    session = value.load_session(SESSION)
    attempts = value._connection.execute(
        "SELECT count(*) FROM exchange_submission_attempts WHERE durable_command_key=?",
        (COMMAND,),
    ).fetchone()[0]
    assert (
        durable.version,
        session.submission_count,
        session.cumulative_submitted_notional,
        attempts,
    ) == before
    assert gateway.calls == 0


@pytest.mark.parametrize(
    ("field", "wrong"),
    [
        ("account_fingerprint", "wrong-account"),
        ("session_id", "wrong-session"),
        ("reconciliation_generation", 2),
        ("control_generation", 2),
        ("environment", ExecutionEnvironment.TESTNET),
    ],
)
def test_final_gate_capability_misuse_matrix(tmp_path: Path, field: str, wrong) -> None:
    value, _, gate, _, gateway, assessment, context, capability = gate_baseline(
        tmp_path / f"capability-{field}.sqlite3"
    )
    if field == "environment":
        from enum import StrEnum

        class FakeLive(StrEnum):
            LIVE = "live"

        wrong = FakeLive.LIVE
    object.__setattr__(capability, field, wrong)
    before_session = value.load_session(SESSION)
    before = (1, before_session.submission_count, before_session.cumulative_submitted_notional, 0)
    result = gate.authorize(
        COMMAND,
        assessment=assessment,
        context=context,
        capability=capability,
        expected_rules_fingerprint="e" * 64,
        now_ms=110,
    )
    assert result.reasons == (PreSubmitBlockReason.CAPABILITY_INVALID,)
    assert_gate_has_no_effect(value, gateway, before)
    value.close()


@pytest.mark.parametrize("now_ms,authorized", [(1100, True), (1101, False)])
def test_final_gate_freshness_exact_boundary(tmp_path: Path, now_ms: int, authorized: bool) -> None:
    value, _, gate, _, gateway, assessment, context, capability = gate_baseline(
        tmp_path / f"fresh-{now_ms}.sqlite3"
    )
    result = gate.authorize(
        COMMAND,
        assessment=assessment,
        context=context,
        capability=capability,
        expected_rules_fingerprint="e" * 64,
        now_ms=now_ms,
    )
    assert result.authorized is authorized
    if not authorized:
        assert result.reasons == (PreSubmitBlockReason.AUTHORIZATION_STALE,)
    assert gateway.calls == 0
    value.close()


def test_final_gate_independent_recovery_and_risk_blockers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value, settle, gate, _, gateway, assessment, context, capability = gate_baseline(
        tmp_path / "recovery-risk.sqlite3"
    )
    unsafe = replace(value.recover_local(), status=LocalExchangeRecoveryStatus.RECOVERY_REQUIRED)
    monkeypatch.setattr(value, "recover_local", lambda: unsafe)
    monkeypatch.setattr(
        settle,
        "validate_authority",
        lambda: (_ for _ in ()).throw(ExchangeSettlementError("corrupt")),
    )
    rejected = replace(
        assessment,
        outcome=ExchangeRiskOutcome.BLOCKED,
        source_risk_outcome=RiskDecisionOutcome.REJECTED,
        reasons=(ExchangeRiskReason.RISK_NOT_APPROVED,),
    )
    before_session = value.load_session(SESSION)
    before = (1, before_session.submission_count, before_session.cumulative_submitted_notional, 0)
    result = gate.authorize(
        COMMAND,
        assessment=rejected,
        context=context,
        capability=capability,
        expected_rules_fingerprint="0" * 64,
        now_ms=99,
    )
    assert set(result.reasons) == {
        PreSubmitBlockReason.AUTHORIZATION_STALE,
        PreSubmitBlockReason.CAPABILITY_INVALID,
        PreSubmitBlockReason.LOCAL_RECOVERY_UNSAFE,
        PreSubmitBlockReason.PORTFOLIO_RECOVERY_UNSAFE,
        PreSubmitBlockReason.RISK_BLOCKED,
        PreSubmitBlockReason.RULES_MISMATCH,
    }
    assert tuple(sorted(result.reasons, key=lambda reason: reason.value)) == result.reasons
    assert_gate_has_no_effect(value, gateway, before)
    value.close()


def test_final_gate_blocks_reconciliation_and_settlement_toctou(tmp_path: Path) -> None:
    value, _, gate, _, gateway, assessment, context, capability = gate_baseline(
        tmp_path / "reconcile-settle-race.sqlite3"
    )
    persist_quote_fee_trade_key = "1" * 64
    other = ExchangeCommand(
        persist_quote_fee_trade_key,
        "2" * 64,
        "3" * 64,
        KEY,
        ExchangeOrderSide.BUY,
        D("0.01"),
        D("100000"),
        D("0.1"),
        D("1000"),
        "4" * 64,
        "e" * 64,
        "f" * 64,
        100,
    )
    value.prepare_order(
        execution_root_id=ROOT,
        session_id=SESSION,
        command=other,
        client_order_id="ctt1t-aaaaaaaaaaaaaaaaaaaaaaaaaa",
        occurred_at_ms=100,
    )
    before_session = value.load_session(SESSION)
    before = (1, before_session.submission_count, before_session.cumulative_submitted_notional, 0)
    result = gate.authorize(
        COMMAND,
        assessment=assessment,
        context=context,
        capability=capability,
        expected_rules_fingerprint="e" * 64,
        now_ms=110,
    )
    assert PreSubmitBlockReason.PENDING_ORDER_LIMIT in result.reasons
    assert PreSubmitBlockReason.RISK_CONTEXT_STALE in result.reasons
    assert_gate_has_no_effect(value, gateway, before)
    value.close()


def test_final_gate_reconciliation_status_matrix(tmp_path: Path) -> None:
    for outcome in (
        ExchangeReconciliationStatus.REQUIRED,
        ExchangeReconciliationStatus.CONFLICT,
        ExchangeReconciliationStatus.FAILED,
    ):
        value, _, gate, _, gateway, assessment, context, capability = gate_baseline(
            tmp_path / f"reconciliation-{outcome.value}.sqlite3"
        )
        value.record_reconciliation_run(
            PersistentExchangeReconciliationRun(
                f"unsafe-{outcome.value.lower()}",
                ROOT,
                SESSION,
                "account-m48",
                1,
                1,
                outcome,
                0,
                0,
                0,
                0,
                0,
                ("TEST_UNSAFE",),
                120,
                121,
            )
        )
        result = gate.authorize(
            COMMAND,
            assessment=assessment,
            context=context,
            capability=capability,
            expected_rules_fingerprint="e" * 64,
            now_ms=110,
        )
        assert PreSubmitBlockReason.RECONCILIATION_UNSAFE in result.reasons
        assert gateway.calls == 0
        value.close()


def test_pre_submit_authorization_is_uncopyable_unserializable_and_single_use(
    tmp_path: Path,
) -> None:
    import copy
    import pickle

    value, _, gate, lifecycle, gateway, assessment, context, capability = gate_baseline(
        tmp_path / "authorization.sqlite3"
    )
    result = gate.authorize(
        COMMAND,
        assessment=assessment,
        context=context,
        capability=capability,
        expected_rules_fingerprint="e" * 64,
        now_ms=110,
    )
    token = result.authorization
    assert token is not None
    for operation in (copy.copy, copy.deepcopy, pickle.dumps):
        with pytest.raises(TypeError):
            operation(token)
    lifecycle._submit_authorized(token)
    assert gateway.calls == 1
    with pytest.raises(ExchangeSubmissionSafetyError):
        lifecycle._submit_authorized(token)
    assert gateway.calls == 1
    value.close()


@pytest.mark.parametrize(
    "outcome",
    [
        ExchangeOrderStatus.ACKNOWLEDGED,
        ExchangeOrderStatus.REJECTED,
        ExchangeOrderStatus.RECONCILIATION_REQUIRED,
        ExchangeOrderStatus.PARTIALLY_FILLED,
        ExchangeOrderStatus.FILLED,
        ExchangeOrderStatus.CANCELED,
        ExchangeOrderStatus.EXPIRED,
    ],
)
def test_consumed_authorization_reuse_is_rejected_after_every_order_outcome(
    tmp_path: Path, outcome: ExchangeOrderStatus
) -> None:
    from app.exchange_execution import (
        SubmissionObservation,
        TransportFailureCategory,
        TransportOutcomeKind,
    )

    class OutcomeGateway:
        def __init__(self) -> None:
            self.calls = 0

        def submit_market_order(self, request):
            self.calls += 1
            if outcome is ExchangeOrderStatus.REJECTED:
                return SubmissionObservation(
                    TransportOutcomeKind.DETERMINISTIC_REJECTION,
                    request.client_order_id,
                )
            if outcome is ExchangeOrderStatus.RECONCILIATION_REQUIRED:
                return SubmissionObservation(
                    TransportOutcomeKind.MAY_HAVE_BEEN_TRANSMITTED,
                    request.client_order_id,
                    failure_category=TransportFailureCategory.TIMEOUT,
                )
            return SubmissionObservation(
                TransportOutcomeKind.RESPONSE_RECEIVED,
                request.client_order_id,
                exchange_order_id="reuse-order",
                exchange_status=ExchangeOrderStatus.ACKNOWLEDGED,
                transact_time_ms=111,
            )

    value, _, gate, _, _, assessment, context, capability = gate_baseline(
        tmp_path / f"authorization-reuse-{outcome.value}.sqlite3"
    )
    gateway = OutcomeGateway()
    lifecycle = DurableExchangeOrderLifecycle(value, gateway, timestamp_provider=lambda: 110)
    gate = PreSubmitGate(
        value,
        lifecycle,
        execution_root_id=ROOT,
        max_authorization_age_ms=1000,
    )
    result = gate.authorize(
        COMMAND,
        assessment=assessment,
        context=context,
        capability=capability,
        expected_rules_fingerprint="e" * 64,
        now_ms=110,
    )
    assert result.authorization is not None
    token = result.authorization
    lifecycle._submit_authorized(token)
    if outcome in (
        ExchangeOrderStatus.PARTIALLY_FILLED,
        ExchangeOrderStatus.FILLED,
        ExchangeOrderStatus.CANCELED,
        ExchangeOrderStatus.EXPIRED,
    ):
        quantities = outcome in (
            ExchangeOrderStatus.PARTIALLY_FILLED,
            ExchangeOrderStatus.FILLED,
        )
        executed = D("0.1") if outcome is ExchangeOrderStatus.FILLED else D("0.05")
        quote = D("10000") if outcome is ExchangeOrderStatus.FILLED else D("5000")
        value.record_status(
            COMMAND,
            status=outcome,
            occurred_at_ms=120,
            reason_code="authoritative_outcome",
            cumulative_executed_base_quantity=executed if quantities else None,
            cumulative_quote_quantity=quote if quantities else None,
            exchange_timestamp_ms=120,
        )
    before = value.load_order(COMMAND)
    session = value.load_session(SESSION)
    attempts = value._connection.execute(
        "SELECT count(*) FROM exchange_submission_attempts WHERE durable_command_key=?",
        (COMMAND,),
    ).fetchone()[0]
    events = value._connection.execute(
        "SELECT count(*) FROM exchange_order_events WHERE durable_command_key=?", (COMMAND,)
    ).fetchone()[0]
    with pytest.raises(ExchangeSubmissionSafetyError):
        lifecycle._submit_authorized(token)
    after = value.load_order(COMMAND)
    assert gateway.calls == 1
    assert after == before
    assert value.load_session(SESSION) == session
    assert (
        value._connection.execute(
            "SELECT count(*) FROM exchange_submission_attempts WHERE durable_command_key=?",
            (COMMAND,),
        ).fetchone()[0]
        == attempts
    )
    assert (
        value._connection.execute(
            "SELECT count(*) FROM exchange_order_events WHERE durable_command_key=?", (COMMAND,)
        ).fetchone()[0]
        == events
    )
    value.close()


@pytest.mark.parametrize("crash", [KeyboardInterrupt, SystemExit, GeneratorExit])
@pytest.mark.parametrize(
    "boundary",
    [
        "attempt_after_record",
        "attempt_after_order",
        "attempt_after_counters",
        "attempt_before_commit",
    ],
)
def test_submission_precommit_baseexceptions_roll_back_all_durable_effects(
    tmp_path: Path, boundary: str, crash: type[BaseException]
) -> None:
    path = tmp_path / f"submission-{boundary}-{crash.__name__}.sqlite3"
    initial, *_ = gate_baseline(path)
    initial.close()

    def fail(point: str) -> None:
        if point == boundary:
            raise crash("injected submission crash")

    crashing = store(path, failure_injector=fail)
    with pytest.raises(crash, match="submission crash"):
        crashing.begin_submission(COMMAND, occurred_at_ms=110)
    assert crashing.load_order(COMMAND).order.status is ExchangeOrderStatus.PREPARED
    assert crashing.load_order(COMMAND).version == 1
    assert crashing.load_session(SESSION).submission_count == 0
    assert crashing.load_session(SESSION).cumulative_submitted_notional == D(0)
    assert (
        crashing._connection.execute(
            "SELECT count(*) FROM exchange_submission_attempts WHERE durable_command_key=?",
            (COMMAND,),
        ).fetchone()[0]
        == 0
    )
    crashing.close()


@pytest.mark.parametrize("crash", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_submission_after_commit_baseexception_preserves_submitting_for_reconciliation(
    tmp_path: Path, crash: type[BaseException]
) -> None:
    path = tmp_path / f"submission-after-commit-{crash.__name__}.sqlite3"
    initial, *_ = gate_baseline(path)
    initial.close()

    def fail(point: str) -> None:
        if point == "attempt_after_commit":
            raise crash("injected post-commit crash")

    crashing = store(path, failure_injector=fail)
    with pytest.raises(crash, match="post-commit crash"):
        crashing.begin_submission(COMMAND, occurred_at_ms=110)
    assert crashing.load_order(COMMAND).order.status is ExchangeOrderStatus.SUBMITTING
    assert crashing.load_session(SESSION).submission_count == 1
    assert (
        crashing._connection.execute(
            "SELECT count(*) FROM exchange_submission_attempts WHERE durable_command_key=?",
            (COMMAND,),
        ).fetchone()[0]
        == 1
    )
    crashing.close()

    fresh = store(path)
    assert fresh.load_order(COMMAND).order.status is ExchangeOrderStatus.SUBMITTING
    recovery = fresh.recover_local()
    assert recovery.status is LocalExchangeRecoveryStatus.CLEAN
    assert tuple(item.order.status for item in recovery.reconciliation_required) == (
        ExchangeOrderStatus.SUBMITTING,
    )
    with pytest.raises(PersistenceConflictError, match="only PREPARED"):
        fresh.begin_submission(COMMAND, occurred_at_ms=120)
    assert fresh.load_session(SESSION).submission_count == 1
    fresh.close()


def application_service_fixture(path: Path, *, financial_policy=None):
    value = store(path)
    value.set_kill_switch(
        ROOT,
        state=KillSwitchState.CLEARED,
        expected_generation=0,
        reason_code="operator",
        audit_identifier="service",
        occurred_at_ms=30,
    )
    managed = portfolio()
    settle = settlement(value, managed)
    settle.initialize()
    gateway = CountingGateway()
    lifecycle = DurableExchangeOrderLifecycle(value, gateway, timestamp_provider=lambda: 110)
    gate = PreSubmitGate(
        value,
        lifecycle,
        execution_root_id=ROOT,
        max_authorization_age_ms=1000,
        settlement=settle,
    )
    capability = evaluate_testnet_readiness(
        enabled=True,
        bootstrap=value.load_bootstrap(ROOT),
        recovery=value.recover_local(),
        reconciliation_status=ExchangeReconciliationStatus.SAFE,
        reconciliation_generation=1,
        settlement_status=ExchangeSettlementStatus.SYNCHRONIZED,
        non_terminal_orders=0,
    ).capability
    assert capability is not None
    manager = RiskManager(
        financial_policy
        or PortfolioRiskPolicy(max_order_notional=D("20000"), max_position_notional=D("20000")),
        managed._config.instrument_rules,
        FixedRiskIds(),  # type: ignore[arg-type]
    )
    service = ExecutionApplicationService(
        value,
        managed,
        manager,
        ExchangeRiskPolicy(D("20000"), D("20000"), D("50000"), D("50000"), 10, 5, D("0.5")),
        lifecycle,
        gate,
        execution_root_id=ROOT,
        capability=capability,
        exchange_name="binance",
        strategy_config_fingerprint="1" * 64,
        planner_name="planner",
        planner_version="1",
        planner_config_fingerprint="2" * 64,
        snapshot_policy_fingerprint="3" * 64,
        risk_policy_fingerprint="4" * 64,
        instrument_rules_fingerprint="e" * 64,
        execution_config_fingerprint="f" * 64,
    )
    return value, service, gateway


def test_authoritative_application_service_executes_only_once_for_stable_economics(
    tmp_path: Path,
) -> None:
    value, service, gateway = application_service_fixture(tmp_path / "service.sqlite3")
    decision, snapshot = execution_decision()
    first = service.execute(decision, snapshot)
    assert first.durable_command_key is not None
    assert first.gate is not None and first.gate.authorized
    assert gateway.calls == 1
    durable = value.load_order(first.durable_command_key)
    original = (durable.order.client_order_id, durable.command_payload)
    second = service.execute(replace(decision, pipeline_run_id="runtime-two"), snapshot)
    assert second.durable_command_key == first.durable_command_key
    assert second.gate is None
    assert gateway.calls == 1
    repeated = value.load_order(first.durable_command_key)
    assert (repeated.order.client_order_id, repeated.command_payload) == original
    value.close()


def test_authoritative_application_service_risk_rejection_has_no_exchange_side_effect(
    tmp_path: Path,
) -> None:
    value, service, gateway = application_service_fixture(tmp_path / "service-reject.sqlite3")
    decision, snapshot = execution_decision(stale=True)
    result = service.execute(decision, snapshot)
    assert result.risk_outcome is RiskDecisionOutcome.REJECTED
    assert result.durable_command_key is None
    assert value.list_orders() == ()
    assert gateway.calls == 0
    assert value.load_session(SESSION).submission_count == 0
    value.close()


def test_authoritative_application_service_modified_command_uses_approved_economics(
    tmp_path: Path,
) -> None:
    value, service, gateway = application_service_fixture(
        tmp_path / "service-modified.sqlite3",
        financial_policy=PortfolioRiskPolicy(
            max_order_notional=D("1000"),
            max_position_notional=D("1000"),
            max_target_exposure=D("0.1"),
        ),
    )
    decision, snapshot = execution_decision(exposure="0.5")
    result = service.execute(decision, snapshot)
    assert result.risk_outcome is RiskDecisionOutcome.MODIFIED
    durable = value.load_order(result.durable_command_key)
    payload = __import__("json").loads(durable.command_payload)
    assert payload["approved_target_exposure"] == {"$decimal": "0.05"}
    assert payload["base_quantity"] == {"$decimal": "10"}
    assert payload["approved_notional"] == {"$decimal": "1000"}
    assert durable.order.original_base_quantity == D("10")
    assert gateway.calls == 1
    value.close()


def test_authoritative_application_service_exchange_risk_block_has_no_order(
    tmp_path: Path,
) -> None:
    value, service, gateway = application_service_fixture(
        tmp_path / "service-exchange-block.sqlite3"
    )
    value._connection.execute(
        "UPDATE exchange_sessions SET submitted_notional='50000',generation=generation+1 "
        "WHERE session_id=?",
        (SESSION,),
    )
    value._connection.commit()
    decision, snapshot = execution_decision()
    result = service.execute(decision, snapshot)
    assert result.assessment is not None
    assert result.assessment.outcome is ExchangeRiskOutcome.BLOCKED
    assert result.durable_command_key is None
    assert value.list_orders() == ()
    assert gateway.calls == 0
    value.close()


def test_authoritative_duplicate_with_conflicting_durable_identity_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value, service, gateway = application_service_fixture(
        tmp_path / "service-existing-conflict.sqlite3"
    )
    decision, snapshot = execution_decision()
    first = service.execute(decision, snapshot)
    durable = value.load_order(first.durable_command_key)
    conflicting = replace(
        durable,
        order=replace(durable.order, client_order_id="ctt1t-bbbbbbbbbbbbbbbbbbbbbbbbbb"),
    )
    monkeypatch.setattr(value, "list_orders", lambda: (conflicting,))
    with pytest.raises(ValueError, match="durable command identity conflicts"):
        service.execute(replace(decision, pipeline_run_id="duplicate-conflict"), snapshot)
    assert gateway.calls == 1
    value.close()


def test_corrected_historical_input_conflicts_on_durable_decision_slot(tmp_path: Path) -> None:
    value, service, gateway = application_service_fixture(tmp_path / "corrected.sqlite3")
    decision, snapshot = execution_decision()
    service.execute(decision, snapshot)
    corrected_candle = replace(snapshot.closed_candles[0], volume=D("2"))
    corrected = replace(
        snapshot,
        snapshot_id="snapshot-corrected",
        closed_candles=(corrected_candle,),
        latest_closed_candle=corrected_candle,
    )
    corrected_signal = replace(
        decision.signal, snapshot_id=corrected.snapshot_id, signal_id="signal-corrected"
    )
    corrected_intent = replace(
        decision.order_intent,
        source_signal_id=corrected_signal.signal_id,
        intent_id="intent-corrected",
    )
    corrected_plan = replace(
        decision.intent_plan,
        signal_id=corrected_signal.signal_id,
        intent=corrected_intent,
    )
    corrected_decision = replace(
        decision,
        pipeline_run_id="pipeline-corrected",
        snapshot_id=corrected.snapshot_id,
        signal=corrected_signal,
        intent_plan=corrected_plan,
        order_intent=corrected_intent,
    )
    with pytest.raises(PersistenceConflictError):
        service.execute(corrected_decision, corrected)
    assert len(value.list_orders()) == 1
    assert gateway.calls == 1
    value.close()


@pytest.mark.parametrize(
    ("pipeline_outcome", "consumer_outcome"),
    [
        (PipelineOutcome.HOLD, ConsumerOutcome.HOLD),
        (PipelineOutcome.PLANNER_DECLINED, ConsumerOutcome.DECLINED),
    ],
)
def test_testnet_consumer_non_executable_matrix_has_no_callback(
    pipeline_outcome: PipelineOutcome, consumer_outcome: ConsumerOutcome
) -> None:
    calls = []
    consumer = ExecutionConsumer(lambda *values: calls.append(values), dedup_capacity=1)
    decision, snapshot = non_executable_decision(pipeline_outcome)
    result = consumer.consume(decision, snapshot)
    assert result.outcome is consumer_outcome
    assert calls == []
    duplicate = consumer.consume(decision, snapshot)
    assert duplicate.outcome is ConsumerOutcome.DUPLICATE
    assert consumer.status.duplicates == 1


def test_testnet_consumer_execution_failure_identity_and_eviction_matrix() -> None:
    with pytest.raises(TypeError):
        ExecutionConsumer(None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ExecutionConsumer(lambda *_: None, dedup_capacity=0)
    decision, snapshot = execution_decision()
    consumer = ExecutionConsumer(
        lambda *_: (_ for _ in ()).throw(RuntimeError("fake execution failure")),
        dedup_capacity=1,
    )
    with pytest.raises(ValueError, match="identity mismatch"):
        consumer.consume(decision, replace(snapshot, snapshot_id="different"))
    with pytest.raises(RuntimeError, match="fake execution"):
        consumer.consume(decision, snapshot)
    assert consumer.status.failures == 1
    successful = ExecutionConsumer(lambda *_: None, dedup_capacity=1)
    assert successful.consume(decision, snapshot).outcome is ConsumerOutcome.EXECUTED
    second = replace(decision, pipeline_run_id="pipeline-second")
    successful.consume(second, snapshot)
    assert successful.consume(decision, snapshot).outcome is ConsumerOutcome.EXECUTED


def test_sqlite_order_identity_resolver_uses_only_durable_identity(tmp_path: Path) -> None:
    value, *_ = gate_baseline(tmp_path / "identity.sqlite3")
    resolver = SQLiteOrderIdentityResolver(value)
    by_client = resolver.by_client_order_id(CLIENT)
    assert by_client is not None
    assert by_client.durable_command_key == COMMAND
    assert by_client.original_base_quantity == D("0.1")
    assert resolver.by_client_order_id("ctt1t-zzzzzzzzzzzzzzzzzzzzzzzzzz") is None
    assert resolver.by_exchange_order_id("missing") is None
    value._connection.execute(
        "UPDATE exchange_orders SET exchange_order_id=? WHERE durable_command_key=?",
        ("exchange-identity", COMMAND),
    )
    value._connection.commit()
    by_exchange = resolver.by_exchange_order_id("exchange-identity")
    assert by_exchange == by_client
    value.close()


def test_safety_service_constructor_and_malformed_input_guards(tmp_path: Path) -> None:
    value = store(tmp_path / "guards.sqlite3")
    managed = portfolio()
    gateway = CountingGateway()
    lifecycle = DurableExchangeOrderLifecycle(value, gateway, timestamp_provider=lambda: 1)
    with pytest.raises(TypeError, match="persistence"):
        PreSubmitGate(object(), lifecycle, execution_root_id=ROOT, max_authorization_age_ms=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="lifecycle"):
        PreSubmitGate(value, object(), execution_root_id=ROOT, max_authorization_age_ms=1)  # type: ignore[arg-type]
    for invalid in (True, 0, "1"):
        with pytest.raises(TypeError, match="positive"):
            PreSubmitGate(
                value,
                lifecycle,
                execution_root_id=ROOT,
                max_authorization_age_ms=invalid,  # type: ignore[arg-type]
            )
    with pytest.raises(ExchangeSettlementError, match="schema version 4"):
        older = SQLiteExchangePersistence(
            SQLitePersistenceConfig(":memory:", expected_schema_version=3)
        )
        try:
            SQLiteExchangeSettlement(
                older,
                managed,
                execution_root_id=ROOT,
                session_id=SESSION,
                config_fingerprint="8" * 64,
                timestamp_provider=lambda: 1,
            )
        finally:
            older.close()
    value.close()


def test_authoritative_service_rejects_identity_and_non_executable_inputs(tmp_path: Path) -> None:
    value, service, gateway = application_service_fixture(tmp_path / "service-guards.sqlite3")
    decision, snapshot = execution_decision()
    with pytest.raises(ValueError, match="identity mismatch"):
        service.execute(decision, replace(snapshot, snapshot_id="wrong"))
    hold, hold_snapshot = non_executable_decision(PipelineOutcome.HOLD)
    with pytest.raises(ValueError, match="executable intent"):
        service.execute(hold, hold_snapshot)
    assert gateway.calls == 0
    value.close()


def test_runtime_result_models_fail_closed_validation_matrix() -> None:
    with pytest.raises(TypeError, match="state"):
        ExecutionReadiness("READY", (), 0, 0, None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="sorted"):
        ExecutionReadiness(
            ReadinessState.BLOCKED,
            (ReadinessReason.SETTLEMENT_PENDING, ReadinessReason.KILL_SWITCH_ACTIVE),
            0,
            0,
            None,
        )
    with pytest.raises(TypeError, match="reason"):
        ExecutionReadiness(ReadinessState.BLOCKED, ("bad",), 0, 0, None)  # type: ignore[arg-type]
    for generation in (True, -1, "1"):
        with pytest.raises(ValueError, match="generation"):
            ExecutionReadiness(
                ReadinessState.BLOCKED,
                (),
                generation,
                0,
                None,  # type: ignore[arg-type]
            )
    with pytest.raises(ValueError, match="TESTNET"):
        ExecutionReadiness(
            ReadinessState.BLOCKED,
            (),
            0,
            0,
            None,
            environment="live",  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="READY"):
        ExecutionReadiness(ReadinessState.READY, (), 0, 0, SESSION)
    with pytest.raises(ValueError, match="only READY"):
        ExecutionReadiness(
            ReadinessState.BLOCKED,
            (),
            0,
            0,
            SESSION,
            capability=object(),  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError, match="settlement status"):
        ExchangeSettlementResult("bad", 0, 0, 0)  # type: ignore[arg-type]
    for field in ("settled_count", "blocked_count", "portfolio_version"):
        values = {"settled_count": 0, "blocked_count": 0, "portfolio_version": 0}
        values[field] = -1
        with pytest.raises(ValueError, match=field):
            ExchangeSettlementResult(ExchangeSettlementStatus.SYNCHRONIZED, **values)
    with pytest.raises(ValueError, match="sorted"):
        PreSubmitGateResult(
            None,
            (PreSubmitBlockReason.RULES_MISMATCH, PreSubmitBlockReason.AUTHORIZATION_STALE),
        )
    with pytest.raises(ValueError, match="mutually exclusive"):
        PreSubmitGateResult(None, ())


def test_settlement_constructor_and_missing_authority_guards(tmp_path: Path) -> None:
    value = store(tmp_path / "settlement-guards.sqlite3")
    managed = portfolio()
    cases = (
        ({"persistence": object()}, TypeError),
        ({"portfolio": object()}, TypeError),
        ({"timestamp_provider": 1}, TypeError),
        ({"session_id": "wrong-session"}, ExchangeSettlementError),
    )
    defaults = {
        "persistence": value,
        "portfolio": managed,
        "execution_root_id": ROOT,
        "session_id": SESSION,
        "config_fingerprint": "8" * 64,
        "timestamp_provider": lambda: 1,
    }
    for changes, error in cases:
        with pytest.raises(error):
            SQLiteExchangeSettlement(**{**defaults, **changes})  # type: ignore[arg-type]
    service = SQLiteExchangeSettlement(**defaults)
    with pytest.raises(ExchangeSettlementError, match="root is missing"):
        service.validate_authority()
    service.initialize()
    value._connection.execute("DELETE FROM exchange_portfolio_current")
    value._connection.commit()
    with pytest.raises(ExchangeSettlementError, match="current Portfolio"):
        service.restore_memory()
    value.close()


def test_settlement_initialization_conflict_and_timestamp_boundaries(tmp_path: Path) -> None:
    value = store(tmp_path / "settlement-init-boundaries.sqlite3")
    managed = portfolio()
    service = settlement(value, managed)
    service.initialize()
    conflicting = SQLiteExchangeSettlement(
        value,
        managed,
        execution_root_id=ROOT,
        session_id=SESSION,
        config_fingerprint="7" * 64,
        timestamp_provider=lambda: 0,
    )
    with pytest.raises(ExchangeSettlementError, match="root conflicts"):
        conflicting.initialize()
    for invalid in (-1, True, 1.5):
        invalid_service = SQLiteExchangeSettlement(
            value,
            managed,
            execution_root_id=ROOT,
            session_id=SESSION,
            config_fingerprint="8" * 64,
            timestamp_provider=lambda invalid=invalid: invalid,
        )
        value._connection.execute("DELETE FROM exchange_portfolio_current")
        value._connection.execute("DELETE FROM exchange_portfolio_roots")
        value._connection.commit()
        with pytest.raises(ExchangeSettlementError, match="timestamp"):
            invalid_service.initialize()
    large = 2**63 - 1
    valid = SQLiteExchangeSettlement(
        value,
        managed,
        execution_root_id=ROOT,
        session_id=SESSION,
        config_fingerprint="8" * 64,
        timestamp_provider=lambda: large,
    )
    valid.initialize()
    assert (
        value._connection.execute("SELECT created_at_ms FROM exchange_portfolio_roots").fetchone()[
            0
        ]
        == large
    )
    value.close()


def test_settlement_missing_coverage_and_session_conflict_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = store(tmp_path / "settlement-conflicts.sqlite3")
    persist_quote_fee_trade(value)
    managed = portfolio()
    service = settlement(value, managed)
    service.initialize()
    value._connection.execute("DROP TRIGGER exchange_reconciliation_coverage_no_delete")
    value._connection.execute("DELETE FROM exchange_reconciliation_coverage")
    value._connection.commit()
    result = service.settle_pending()
    assert result.status is ExchangeSettlementStatus.PENDING
    assert managed.snapshot.version == 0

    state = value.read_reconciliation_state(ROOT)
    value.record_reconciliation_run(
        PersistentExchangeReconciliationRun(
            "restored-coverage",
            ROOT,
            SESSION,
            "account-m48",
            state.generation,
            state.generation + 1,
            ExchangeReconciliationStatus.SAFE,
            0,
            0,
            0,
            0,
            1,
            (),
            40,
            41,
            0,
            41,
            True,
            True,
            "BTCUSDT",
        )
    )
    original_generation = service._session_generation
    monkeypatch.setattr(service, "_session_generation", lambda _session: 999)
    with pytest.raises(ExchangeSettlementError, match="session generation conflict"):
        service.settle_pending()
    assert managed.snapshot.version == 0
    assert value.load_session(SESSION).cumulative_executed_turnover == D(0)
    assert (
        value._connection.execute("SELECT count(*) FROM exchange_trade_settlements").fetchone()[0]
        == 0
    )
    monkeypatch.setattr(service, "_session_generation", original_generation)
    assert service.settle_pending().settled_count == 1
    value.close()


@pytest.mark.parametrize(
    ("table", "assignment", "message"),
    [
        ("exchange_portfolio_roots", "state_digest='" + "0" * 64 + "'", "root authority"),
        (
            "exchange_portfolio_current",
            "portfolio_version=99,last_settlement_seq=99",
            "current Portfolio authority",
        ),
    ],
)
def test_settlement_root_and_current_authority_conflicts_fail_closed(
    tmp_path: Path, table: str, assignment: str, message: str
) -> None:
    value = store(tmp_path / f"authority-{table}.sqlite3")
    service = settlement(value, portfolio())
    service.initialize()
    value._connection.execute(f"UPDATE {table} SET {assignment}")
    value._connection.commit()
    with pytest.raises(ExchangeSettlementError, match=message):
        service.validate_authority() if table.endswith("roots") else service.restore_memory()
    value.close()


def test_duplicate_blocked_settlement_insert_is_idempotent(tmp_path: Path) -> None:
    value = store(tmp_path / "duplicate-settlement.sqlite3")
    persist_quote_fee_trade(value, "BNB")
    managed = portfolio()
    service = settlement(value, managed)
    service.initialize()
    reconciliation = value.read_reconciliation_state(ROOT)
    row = value._connection.execute(
        "SELECT t.*,o.session_id,o.stable_decision_key FROM exchange_trades t "
        "JOIN exchange_orders o ON o.durable_command_key=t.durable_command_key"
    ).fetchone()
    settlement_id = sha256(b"duplicate-settlement-id").hexdigest()
    service._record_blocked(row, settlement_id, reconciliation)
    service._record_blocked(row, settlement_id, reconciliation)
    assert (
        value._connection.execute(
            "SELECT count(*) FROM exchange_trade_settlements WHERE settlement_id=?",
            (settlement_id,),
        ).fetchone()[0]
        == 1
    )
    assert managed.snapshot.version == 0
    assert value.load_session(SESSION).cumulative_executed_turnover == D(0)
    value.close()


def test_portfolio_optimistic_current_conflict_rolls_back_settlement(tmp_path: Path) -> None:
    value = store(tmp_path / "portfolio-optimistic-conflict.sqlite3")
    persist_quote_fee_trade(value)
    managed = portfolio()

    def advance_current(boundary: str) -> None:
        if boundary == "settlement_after_record":
            value._connection.execute(
                "UPDATE exchange_portfolio_current SET portfolio_version=1,last_settlement_seq=99"
            )

    service = SQLiteExchangeSettlement(
        value,
        managed,
        execution_root_id=ROOT,
        session_id=SESSION,
        config_fingerprint="8" * 64,
        timestamp_provider=lambda: 200,
        failure_injector=advance_current,
    )
    service.initialize()
    with pytest.raises(ExchangeSettlementError, match="Portfolio version conflict"):
        service.settle_pending()
    assert managed.snapshot.version == 0
    assert (
        value._connection.execute("SELECT count(*) FROM exchange_trade_settlements").fetchone()[0]
        == 0
    )
    assert (
        value._connection.execute(
            "SELECT portfolio_version FROM exchange_portfolio_current"
        ).fetchone()[0]
        == 0
    )
    assert value.load_session(SESSION).cumulative_executed_turnover == D(0)
    value.close()


def test_missing_portfolio_current_never_falls_back_to_initial_state(tmp_path: Path) -> None:
    value = store(tmp_path / "missing-current-authority.sqlite3")
    persist_quote_fee_trade(value)
    managed = portfolio()
    service = settlement(value, managed)
    service.initialize()
    assert service.settle_pending().settled_count == 1
    value._connection.execute("DELETE FROM exchange_portfolio_current")
    value._connection.commit()
    fresh_memory = portfolio()
    recovery = settlement(value, fresh_memory)
    with pytest.raises(ExchangeSettlementError, match="current Portfolio authority"):
        recovery.validate_authority()
    with pytest.raises(ExchangeSettlementError, match="current Portfolio authority"):
        recovery.initialize()
    assert fresh_memory.snapshot.version == 0
    value.close()


def test_final_gate_candidate_state_and_unsettled_trade_blockers(tmp_path: Path) -> None:
    value, _, gate, _, gateway, assessment, context, capability = gate_baseline(
        tmp_path / "candidate-state.sqlite3"
    )
    value.begin_submission(COMMAND, occurred_at_ms=110)
    result = gate.authorize(
        COMMAND,
        assessment=assessment,
        context=context,
        capability=capability,
        expected_rules_fingerprint="e" * 64,
        now_ms=110,
    )
    assert PreSubmitBlockReason.ORDER_NOT_PREPARED in result.reasons
    assert PreSubmitBlockReason.IN_FLIGHT_LIMIT in result.reasons
    assert gateway.calls == 0
    value.close()


@pytest.mark.parametrize(
    "mutation",
    [
        "reconciliation_generation",
        "control_generation",
        "kill_switch_active",
        "competing_submitting",
        "competing_acknowledged",
        "settlement_pending",
        "settlement_blocked",
        "submission_count",
        "submitted_notional",
        "executed_turnover",
        "failure_count",
        "rules_fingerprint",
        "session_closed",
    ],
)
def test_final_gate_complete_durable_toctou_matrix(tmp_path: Path, mutation: str) -> None:
    value, settle, gate, _, gateway, assessment, context, capability = gate_baseline(
        tmp_path / f"toctou-{mutation}.sqlite3"
    )

    def prepare_competing(*, acknowledged: bool, fee_asset: str | None = None) -> str:
        key = "7" * 64
        command = ExchangeCommand(
            key,
            "6" * 64,
            "5" * 64,
            KEY,
            ExchangeOrderSide.BUY,
            D("0.01"),
            D("100000"),
            D("0.1"),
            D("1000"),
            "4" * 64,
            "e" * 64,
            "f" * 64,
            100,
        )
        client = "ctt1t-aaaaaaaaaaaaaaaaaaaaaaaaaa"
        value.prepare_order(
            execution_root_id=ROOT,
            session_id=SESSION,
            command=command,
            client_order_id=client,
            occurred_at_ms=101,
        )
        value.begin_submission(key, occurred_at_ms=102)
        if acknowledged or fee_asset is not None:
            status = (
                ExchangeOrderStatus.ACKNOWLEDGED
                if fee_asset is None
                else ExchangeOrderStatus.FILLED
            )
            value.record_status(
                key,
                status=status,
                occurred_at_ms=103,
                reason_code="toctou",
                exchange_order_id="toctou-order",
                acknowledged_at_ms=103,
                cumulative_executed_base_quantity=D("0.01") if fee_asset else None,
                cumulative_quote_quantity=D("1000") if fee_asset else None,
                exchange_timestamp_ms=103,
            )
        if fee_asset is not None:
            value.persist_trades(
                key,
                (
                    ExchangeTrade(
                        ExchangeTradeIdentity(
                            "binance-spot",
                            ExecutionEnvironment.TESTNET,
                            "account-m48",
                            "BTCUSDT",
                            "toctou-order",
                            "toctou-trade",
                        ),
                        client,
                        KEY,
                        ExchangeOrderSide.BUY,
                        D("0.01"),
                        D("100000"),
                        D("1000"),
                        D("0.01"),
                        fee_asset,
                        103,
                    ),
                ),
            )
        return key

    expected_rules = "e" * 64
    if mutation == "reconciliation_generation":
        value.record_reconciliation_run(
            PersistentExchangeReconciliationRun(
                "toctou-generation",
                ROOT,
                SESSION,
                "account-m48",
                1,
                2,
                ExchangeReconciliationStatus.SAFE,
                0,
                0,
                0,
                0,
                0,
                (),
                120,
                121,
                0,
                121,
                True,
                True,
                "BTCUSDT",
            )
        )
    elif mutation == "control_generation":
        value.set_kill_switch(
            ROOT,
            state=KillSwitchState.CLEARED,
            expected_generation=1,
            reason_code="generation_changed",
            audit_identifier="toctou",
            occurred_at_ms=105,
        )
    elif mutation == "kill_switch_active":
        value.set_kill_switch(
            ROOT,
            state=KillSwitchState.ACTIVE,
            expected_generation=1,
            reason_code="activated",
            audit_identifier="toctou",
            occurred_at_ms=105,
        )
    elif mutation == "competing_submitting":
        prepare_competing(acknowledged=False)
    elif mutation == "competing_acknowledged":
        prepare_competing(acknowledged=True)
    elif mutation in ("settlement_pending", "settlement_blocked"):
        prepare_competing(
            acknowledged=True,
            fee_asset="USDT" if mutation == "settlement_pending" else "BNB",
        )
        if mutation == "settlement_blocked":
            assert settle.settle_pending().blocked_count == 1
    elif mutation in (
        "submission_count",
        "submitted_notional",
        "executed_turnover",
        "failure_count",
    ):
        assignments = {
            "submission_count": "submission_count=10",
            "submitted_notional": "submitted_notional='50000'",
            "executed_turnover": "executed_turnover='50000'",
            "failure_count": "execution_failure_count=5",
        }
        value._connection.execute(
            f"UPDATE exchange_sessions SET {assignments[mutation]},generation=generation+1 "
            "WHERE session_id=?",
            (SESSION,),
        )
        value._connection.commit()
    elif mutation == "rules_fingerprint":
        expected_rules = "0" * 64
    else:
        value._connection.execute(
            "UPDATE exchange_sessions SET state='CLOSED',generation=generation+1 "
            "WHERE session_id=?",
            (SESSION,),
        )
        value._connection.commit()

    before = value.load_order(COMMAND)
    before_session = value.load_session(SESSION)
    attempts = value._connection.execute(
        "SELECT count(*) FROM exchange_submission_attempts WHERE durable_command_key=?",
        (COMMAND,),
    ).fetchone()[0]
    result = gate.authorize(
        COMMAND,
        assessment=assessment,
        context=context,
        capability=capability,
        expected_rules_fingerprint=expected_rules,
        now_ms=110,
    )
    assert not result.authorized
    after = value.load_order(COMMAND)
    after_session = value.load_session(SESSION)
    assert gateway.calls == 0
    assert after.order.status is ExchangeOrderStatus.PREPARED
    assert after.version == before.version
    assert (
        value._connection.execute(
            "SELECT count(*) FROM exchange_submission_attempts WHERE durable_command_key=?",
            (COMMAND,),
        ).fetchone()[0]
        == attempts
    )
    assert after_session.submission_count == before_session.submission_count
    assert (
        after_session.cumulative_submitted_notional == before_session.cumulative_submitted_notional
    )
    value.close()


def test_file_backed_exchange_recovery_scale_sanity(tmp_path: Path) -> None:
    path = tmp_path / "exchange-scale.sqlite3"
    started = time.perf_counter()
    value = store(path)
    schema_open_seconds = time.perf_counter() - started
    trades_per_order = 5
    order_count = 100
    for order_index in range(order_count):
        command_key = sha256(f"scale-command-{order_index}".encode()).hexdigest()
        stable_key = sha256(f"scale-stable-{order_index}".encode()).hexdigest()
        slot_key = sha256(f"scale-slot-{order_index}".encode()).hexdigest()
        client_id = f"ctt1t-{order_index:026d}"
        command = ExchangeCommand(
            command_key,
            stable_key,
            slot_key,
            KEY,
            ExchangeOrderSide.BUY,
            D("0.25"),
            D("100"),
            D("0.5"),
            D("25"),
            "d" * 64,
            "e" * 64,
            "f" * 64,
            100 + order_index,
        )
        value.prepare_order(
            execution_root_id=ROOT,
            session_id=SESSION,
            command=command,
            client_order_id=client_id,
            occurred_at_ms=100 + order_index,
        )
        value.begin_submission(command_key, occurred_at_ms=1000 + order_index)
        exchange_order_id = f"scale-order-{order_index}"
        value.record_status(
            command_key,
            status=ExchangeOrderStatus.FILLED,
            occurred_at_ms=2000 + order_index,
            reason_code="scale_fixture",
            exchange_order_id=exchange_order_id,
            acknowledged_at_ms=1500 + order_index,
            cumulative_executed_base_quantity=D("0.25"),
            cumulative_quote_quantity=D("25"),
            exchange_timestamp_ms=2000 + order_index,
        )
        value.persist_trades(
            command_key,
            tuple(
                ExchangeTrade(
                    ExchangeTradeIdentity(
                        "binance-spot",
                        ExecutionEnvironment.TESTNET,
                        "account-m48",
                        "BTCUSDT",
                        exchange_order_id,
                        f"scale-trade-{order_index:03d}-{trade_index}",
                    ),
                    client_id,
                    KEY,
                    ExchangeOrderSide.BUY,
                    D("0.05"),
                    D("100"),
                    D("5"),
                    D("0.01"),
                    "USDT",
                    3000 + order_index * trades_per_order + trade_index,
                )
                for trade_index in range(trades_per_order)
            ),
        )
    populate_seconds = time.perf_counter() - started - schema_open_seconds
    recovery_started = time.perf_counter()
    recovery = value.recover_local()
    local_recovery_seconds = time.perf_counter() - recovery_started
    assert recovery.status is LocalExchangeRecoveryStatus.CLEAN
    managed = portfolio()
    service = settlement(value, managed)
    settlement_started = time.perf_counter()
    service.initialize()
    result = service.settle_pending()
    settlement_seconds = time.perf_counter() - settlement_started
    assert result.settled_count == order_count * trades_per_order
    assert managed.snapshot.version == order_count * trades_per_order
    assert value.load_session(SESSION).cumulative_executed_turnover == D("2500")
    value.close()

    reopen_started = time.perf_counter()
    reopened = store(path)
    restored = portfolio()
    fresh = settlement(reopened, restored)
    fresh.initialize()
    reopen_seconds = time.perf_counter() - reopen_started
    assert restored.snapshot == managed.snapshot
    assert fresh.settle_pending().settled_count == 0
    assert (
        reopened._connection.execute("SELECT count(*) FROM exchange_orders").fetchone()[0]
        == order_count
    )
    assert (
        reopened._connection.execute("SELECT count(*) FROM exchange_trades").fetchone()[0]
        == order_count * trades_per_order
    )
    assert (
        max(
            schema_open_seconds,
            populate_seconds,
            local_recovery_seconds,
            settlement_seconds,
            reopen_seconds,
        )
        < 10
    )
    print(
        "scale timings seconds:",
        {
            "schema_open": round(schema_open_seconds, 6),
            "populate_100_orders_500_trades": round(populate_seconds, 6),
            "local_recovery": round(local_recovery_seconds, 6),
            "settle_500": round(settlement_seconds, 6),
            "reopen_restore": round(reopen_seconds, 6),
        },
    )
    reopened.close()
