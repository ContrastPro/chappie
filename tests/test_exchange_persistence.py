import sqlite3
from dataclasses import replace
from decimal import Decimal, localcontext
from hashlib import sha256
from pathlib import Path

import pytest

from app.exchange_execution import (
    DurableExchangeOrderLifecycle,
    ExchangeCommand,
    ExchangeOrder,
    ExchangeOrderEventSource,
    ExchangeOrderSide,
    ExchangeOrderStatus,
    ExchangeTrade,
    ExchangeTradeIdentity,
    ExecutionEnvironment,
    ExecutionSession,
    ExecutionSessionState,
    KillSwitch,
    KillSwitchState,
    SubmissionObservation,
    TransportFailureCategory,
    TransportOutcomeKind,
)
from app.exchange_execution.exceptions import ExchangeOrderStateError
from app.market.models import MarketStreamKey
from app.persistence import (
    ExchangeControlState,
    ExchangeExecutionRoot,
    InitialPortfolioRecord,
    LocalExchangeRecoveryResult,
    LocalExchangeRecoveryStatus,
    PortfolioState,
    RestartDisposition,
    SQLiteExchangePersistence,
    SQLitePersistenceConfig,
    SQLiteTradingPersistence,
    TradeCompletenessStatus,
)
from app.persistence.exceptions import (
    PersistenceConflictError,
    PersistenceIntegrityError,
    PersistenceMigrationError,
    PersistenceWriterConflictError,
)

D = Decimal
KEY = MarketStreamKey("BTCUSDT", "1m")
COMMAND_KEY = "a" * 64
DECISION_KEY = "b" * 64
CLIENT_ID = "ctt1t-abcdefghijklmnopqrstuvwxyz"
ROOT_ID = "testnet-root-1"
SESSION_ID = "testnet-session-1"


def command(**changes: object) -> ExchangeCommand:
    values = {
        "durable_command_key": COMMAND_KEY,
        "stable_decision_key": DECISION_KEY,
        "decision_slot_key": "c" * 64,
        "stream_key": KEY,
        "side": ExchangeOrderSide.BUY,
        "base_quantity": D("0.1"),
        "reference_price": D("100000"),
        "approved_target_exposure": D("0.5"),
        "approved_notional": D("10000"),
        "risk_authorization_fingerprint": "d" * 64,
        "instrument_rules_fingerprint": "e" * 64,
        "execution_configuration_fingerprint": "f" * 64,
        "authorized_at_ms": 100,
    }
    return ExchangeCommand(**{**values, **changes})  # type: ignore[arg-type]


def open_store(path: Path, *, failure_injector=None) -> SQLiteExchangePersistence:
    return SQLiteExchangePersistence(
        SQLitePersistenceConfig(path, expected_schema_version=2),
        failure_injector=failure_injector,
    )


def bootstrap(store: SQLiteExchangePersistence) -> None:
    root = ExchangeExecutionRoot(ROOT_ID, "account-1", KEY, "9" * 64, 10)
    session = ExecutionSession(SESSION_ID, "account-1", 10, ExecutionSessionState.ACTIVE)
    result = store.initialize_execution(root, session, audit_identifier="bootstrap-test")
    assert result.control.kill_switch.state is KillSwitchState.ACTIVE


def prepare(store: SQLiteExchangePersistence):
    return store.prepare_order(
        execution_root_id=ROOT_ID,
        session_id=SESSION_ID,
        command=command(),
        client_order_id=CLIENT_ID,
        occurred_at_ms=100,
    )


class FakeGateway:
    def __init__(self, outcome: TransportOutcomeKind = TransportOutcomeKind.RESPONSE_RECEIVED):
        self.outcome = outcome
        self.calls = 0

    def submit_market_order(self, request):
        self.calls += 1
        if self.outcome is TransportOutcomeKind.RESPONSE_RECEIVED:
            return SubmissionObservation(
                self.outcome,
                request.client_order_id,
                "900719925474099312345",
                ExchangeOrderStatus.ACKNOWLEDGED,
                201,
            )
        if self.outcome is TransportOutcomeKind.MAY_HAVE_BEEN_TRANSMITTED:
            return SubmissionObservation(
                self.outcome,
                request.client_order_id,
                failure_category=TransportFailureCategory.TIMEOUT,
            )
        return SubmissionObservation(self.outcome, request.client_order_id)


def test_migration_v2_is_additive_idempotent_and_stage3_v1_remains_supported(
    tmp_path: Path,
) -> None:
    path = tmp_path / "upgrade.sqlite3"
    stage3 = SQLiteTradingPersistence(SQLitePersistenceConfig(path))
    portfolio = PortfolioState(
        "paper-main",
        KEY,
        "BTC",
        "USDT",
        D("1000"),
        D(0),
        None,
        D(0),
        D(0),
        D(0),
        D("1000"),
        D(0),
        0,
        0,
        D("1000"),
        D("1000"),
        D(0),
        D(0),
        None,
        0,
        0,
    )
    stage3.initialize_portfolio(InitialPortfolioRecord(portfolio, "7" * 64, 0))
    stage3._connection.execute(
        "INSERT INTO paper_decisions VALUES(?,?,?,?,?,?)",
        (DECISION_KEY, "6" * 64, "{}", sha256(b"{}").hexdigest(), 1, "HOLD"),
    )
    before = {
        table: tuple(
            tuple(row) for row in stage3._connection.execute(f"SELECT * FROM {table}").fetchall()
        )
        for table in ("paper_decisions", "portfolio_roots", "portfolio_current")
    }
    stage3.close()
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 1
    finally:
        connection.close()
    store = open_store(path)
    store.close()
    reopened = open_store(path)
    try:
        rows = reopened._connection.execute(
            "SELECT version,name FROM schema_migrations ORDER BY version"
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            (1, "001_initial.sql"),
            (2, "002_exchange_execution.sql"),
        ]
        assert (
            reopened._connection.execute("SELECT count(*) FROM portfolio_roots").fetchone()[0] == 1
        )
        after = {
            table: tuple(
                tuple(row)
                for row in reopened._connection.execute(f"SELECT * FROM {table}").fetchall()
            )
            for table in before
        }
        assert after == before
        assert reopened._trading.load_current("paper-main") == portfolio
        assert (
            reopened._connection.execute("SELECT count(*) FROM exchange_orders").fetchone()[0] == 0
        )
    finally:
        reopened.close()


def test_v2_migration_rollback_and_checksum_are_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "migration-failure.sqlite3"
    SQLiteTradingPersistence(SQLitePersistenceConfig(path)).close()
    original = SQLiteTradingPersistence._migration

    def broken(name: str = "001_initial.sql") -> tuple[str, str]:
        if name == "002_exchange_execution.sql":
            return "CREATE TABLE partial_exchange(id INTEGER); BROKEN SQL;", "0" * 64
        return original(name)

    monkeypatch.setattr(SQLiteTradingPersistence, "_migration", staticmethod(broken))
    with pytest.raises(PersistenceMigrationError):
        open_store(path)
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE name='partial_exchange'"
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()
    monkeypatch.setattr(SQLiteTradingPersistence, "_migration", staticmethod(original))
    valid = open_store(path)
    valid.close()
    connection = sqlite3.connect(path)
    try:
        connection.execute("UPDATE schema_migrations SET checksum=? WHERE version=2", ("0" * 64,))
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(PersistenceMigrationError, match="checksum"):
        open_store(path)


def test_root_session_control_and_prepared_claim_survive_restart(tmp_path: Path) -> None:
    path = tmp_path / "prepared.sqlite3"
    store = open_store(path)
    bootstrap(store)
    first = prepare(store)
    duplicate = store.prepare_order(
        execution_root_id=ROOT_ID,
        session_id=SESSION_ID,
        command=command(),
        client_order_id=CLIENT_ID,
        occurred_at_ms=999,
    )
    assert duplicate == first
    assert (
        store._connection.execute("SELECT count(*) FROM exchange_order_events").fetchone()[0] == 1
    )
    assert (
        store._connection.execute("SELECT count(*) FROM exchange_submission_attempts").fetchone()[0]
        == 0
    )
    assert store.restart_disposition(first) is RestartDisposition.FIRST_SUBMISSION_ELIGIBLE
    assert store.load_session(SESSION_ID).submission_count == 0
    control = store.set_kill_switch(
        ROOT_ID,
        state=KillSwitchState.CLEARED,
        expected_generation=0,
        reason_code="operator_authorized",
        audit_identifier="operator-ticket-1",
        occurred_at_ms=101,
    )
    assert control.kill_switch.generation == 1
    store.close()
    reopened = open_store(path)
    try:
        assert reopened.list_submit_eligible()[0] == first
        assert reopened.read_control_state(ROOT_ID) == control
        with pytest.raises(PersistenceConflictError):
            reopened.prepare_order(
                execution_root_id=ROOT_ID,
                session_id=SESSION_ID,
                command=command(approved_notional=D("9999")),
                client_order_id=CLIENT_ID,
                occurred_at_ms=100,
            )
        with pytest.raises(PersistenceConflictError, match="generation"):
            reopened.set_kill_switch(
                ROOT_ID,
                state=KillSwitchState.ACTIVE,
                expected_generation=0,
                reason_code="stale",
                audit_identifier="operator-ticket-2",
                occurred_at_ms=102,
            )
    finally:
        reopened.close()


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (TransportOutcomeKind.RESPONSE_RECEIVED, ExchangeOrderStatus.ACKNOWLEDGED),
        (TransportOutcomeKind.DETERMINISTIC_REJECTION, ExchangeOrderStatus.REJECTED),
        (
            TransportOutcomeKind.MAY_HAVE_BEEN_TRANSMITTED,
            ExchangeOrderStatus.RECONCILIATION_REQUIRED,
        ),
    ],
)
def test_atomic_attempt_and_outcome_persistence(tmp_path: Path, outcome, expected) -> None:
    path = tmp_path / f"{outcome}.sqlite3"
    store = open_store(path)
    bootstrap(store)
    prepare(store)
    gateway = FakeGateway(outcome)
    lifecycle = DurableExchangeOrderLifecycle(
        store, gateway, timestamp_provider=iter(range(200, 210)).__next__
    )
    result = lifecycle.submit_prepared(COMMAND_KEY)
    assert result.order.status is expected
    assert gateway.calls == 1
    assert lifecycle.submit_prepared(COMMAND_KEY) == result
    assert gateway.calls == 1
    session = store.load_session(SESSION_ID)
    assert session.submission_count == 1
    assert session.cumulative_submitted_notional == D("10000")
    assert session.execution_failure_count == (
        0 if expected is ExchangeOrderStatus.ACKNOWLEDGED else 1
    )
    store.close()
    reopened = open_store(path)
    try:
        assert reopened.load_order(COMMAND_KEY) == result
        assert not reopened.list_submit_eligible()
        if expected is ExchangeOrderStatus.ACKNOWLEDGED:
            assert (
                reopened.load_order(COMMAND_KEY).order.exchange_order_id == "900719925474099312345"
            )
        elif expected is ExchangeOrderStatus.REJECTED:
            assert reopened.recover_local().terminal[0] == result
        else:
            assert reopened.list_orders_requiring_reconciliation()[0] == result
    finally:
        reopened.close()


def test_crash_after_submitting_or_ack_before_persist_never_resubmits(tmp_path: Path) -> None:
    path = tmp_path / "ack-lost.sqlite3"
    store = open_store(path)
    bootstrap(store)
    prepare(store)

    class AcceptedThenCrashed(FakeGateway):
        def submit_market_order(self, request):
            self.calls += 1
            raise RuntimeError("process died after exchange accepted")

    gateway = AcceptedThenCrashed()
    lifecycle = DurableExchangeOrderLifecycle(store, gateway, timestamp_provider=lambda: 200)
    with pytest.raises(RuntimeError, match="process died"):
        lifecycle.submit_prepared(COMMAND_KEY)
    assert gateway.calls == 1
    assert store.load_order(COMMAND_KEY).order.status is ExchangeOrderStatus.SUBMITTING
    store.close()
    for _ in range(5):
        reopened = open_store(path)
        try:
            restarted = DurableExchangeOrderLifecycle(
                reopened, gateway, timestamp_provider=lambda: 300
            )
            assert (
                restarted.submit_prepared(COMMAND_KEY).order.status
                is ExchangeOrderStatus.SUBMITTING
            )
            assert gateway.calls == 1
            assert restarted.inspect_local_recovery().reconciliation_required
        finally:
            reopened.close()


def test_trade_truth_is_durable_with_unsupported_fee_and_no_portfolio_fill(tmp_path: Path) -> None:
    path = tmp_path / "trades.sqlite3"
    store = open_store(path)
    bootstrap(store)
    prepare(store)
    store.begin_submission(COMMAND_KEY, occurred_at_ms=200)
    store.record_status(
        COMMAND_KEY,
        status=ExchangeOrderStatus.PARTIALLY_FILLED,
        occurred_at_ms=201,
        reason_code="authoritative_query",
        exchange_order_id="900719925474099312345",
        acknowledged_at_ms=201,
        cumulative_executed_base_quantity=D("0.04"),
        cumulative_quote_quantity=D("4000"),
    )
    trade = ExchangeTrade(
        ExchangeTradeIdentity(
            "binance-spot",
            ExecutionEnvironment.TESTNET,
            "account-1",
            "BTCUSDT",
            "900719925474099312345",
            "18446744073709551616000",
        ),
        CLIENT_ID,
        KEY,
        ExchangeOrderSide.BUY,
        D("0.04"),
        D("100000"),
        D("4000"),
        D("0.00004"),
        "BNB",
        202,
        False,
    )
    assert store.persist_trades(COMMAND_KEY, (trade,)) == (trade,)
    assert store.persist_trades(COMMAND_KEY, (trade,)) == (trade,)
    assert store.trade_completeness(COMMAND_KEY) is TradeCompletenessStatus.COMPLETE
    with pytest.raises(PersistenceConflictError):
        store.persist_trades(COMMAND_KEY, (replace(trade, commission=D("0.1")),))
    assert store._connection.execute("SELECT count(*) FROM fills").fetchone()[0] == 0
    row = store._connection.execute(
        "SELECT typeof(quantity),typeof(exchange_trade_id),commission_asset,settlement_status "
        "FROM exchange_trades"
    ).fetchone()
    assert tuple(row) == ("text", "text", "BNB", "PENDING")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._connection.execute("UPDATE exchange_trades SET commission='1'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._connection.execute("DELETE FROM exchange_trades")
    store.record_status(
        COMMAND_KEY,
        status=ExchangeOrderStatus.FILLED,
        occurred_at_ms=203,
        reason_code="authoritative_filled",
        cumulative_executed_base_quantity=D("0.1"),
        cumulative_quote_quantity=D("10000"),
    )
    assert store.trade_completeness(COMMAND_KEY) is TradeCompletenessStatus.INCOMPLETE
    store.close()


def test_projection_corruption_and_append_only_authority_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.sqlite3"
    store = open_store(path)
    bootstrap(store)
    prepare(store)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._connection.execute("DELETE FROM exchange_order_events")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._connection.execute("UPDATE exchange_order_events SET reason_code='changed'")
    store._connection.execute(
        "UPDATE exchange_orders SET status='SUBMITTING' WHERE durable_command_key=?",
        (COMMAND_KEY,),
    )
    with pytest.raises(PersistenceIntegrityError, match="projection/event"):
        store.load_order(COMMAND_KEY)
    store.close()


def test_fail_closed_validation_and_missing_identity_branches(tmp_path: Path) -> None:
    path = tmp_path / "validation.sqlite3"
    with pytest.raises(PersistenceIntegrityError, match="schema version 2"):
        SQLiteExchangePersistence(SQLitePersistenceConfig(path))
    store = open_store(path)
    try:
        with pytest.raises(PersistenceIntegrityError, match="missing"):
            store.load_order(COMMAND_KEY)
        with pytest.raises(PersistenceIntegrityError, match="control"):
            store.read_control_state("missing")
        root = ExchangeExecutionRoot(ROOT_ID, "account-1", KEY, "9" * 64, 10)
        bad_session = ExecutionSession(SESSION_ID, "account-2", 10, ExecutionSessionState.ACTIVE)
        with pytest.raises(PersistenceConflictError, match="account"):
            store.initialize_execution(root, bad_session, audit_identifier="test")
        bootstrap(store)
        with pytest.raises(PersistenceConflictError, match="root conflicts"):
            store.initialize_execution(
                replace(root, configuration_fingerprint="8" * 64),
                ExecutionSession(SESSION_ID, "account-1", 10, ExecutionSessionState.ACTIVE),
                audit_identifier="test",
            )
        with pytest.raises(PersistenceConflictError, match="session"):
            store.prepare_order(
                execution_root_id=ROOT_ID,
                session_id="wrong-session",
                command=command(),
                client_order_id=CLIENT_ID,
                occurred_at_ms=100,
            )
    finally:
        store.close()


def test_transition_identity_attempt_and_trade_consistency_fail_closed(tmp_path: Path) -> None:
    store = open_store(tmp_path / "conflicts.sqlite3")
    bootstrap(store)
    prepare(store)
    store.begin_submission(COMMAND_KEY, occurred_at_ms=200)
    with pytest.raises(PersistenceConflictError, match="PREPARED"):
        store.begin_submission(COMMAND_KEY, occurred_at_ms=201)
    acknowledged = store.record_status(
        COMMAND_KEY,
        status=ExchangeOrderStatus.ACKNOWLEDGED,
        occurred_at_ms=202,
        reason_code="ack",
        exchange_order_id="123",
        acknowledged_at_ms=202,
    )
    assert store.restart_disposition(acknowledged) is RestartDisposition.RECONCILIATION_REQUIRED
    with pytest.raises(Exception, match="identity cannot change"):
        store.record_status(
            COMMAND_KEY,
            status=ExchangeOrderStatus.PARTIALLY_FILLED,
            occurred_at_ms=203,
            reason_code="conflict",
            exchange_order_id="456",
            cumulative_executed_base_quantity=D("0.01"),
            cumulative_quote_quantity=D("1000"),
        )
    cross_order = ExchangeTrade(
        ExchangeTradeIdentity(
            "binance-spot", ExecutionEnvironment.TESTNET, "account-1", "BTCUSDT", "999", "1"
        ),
        CLIENT_ID,
        KEY,
        ExchangeOrderSide.BUY,
        D("0.01"),
        D("100000"),
        D("1000"),
        D("0"),
        "USDT",
        204,
    )
    with pytest.raises(PersistenceConflictError, match="identity"):
        store.persist_trades(COMMAND_KEY, (cross_order,))
    store.close()


def test_client_and_command_collision_defense_in_depth(tmp_path: Path) -> None:
    store = open_store(tmp_path / "identity-collisions.sqlite3")
    bootstrap(store)
    prepare(store)
    with pytest.raises(PersistenceConflictError, match="identity"):
        store.prepare_order(
            execution_root_id=ROOT_ID,
            session_id=SESSION_ID,
            command=command(durable_command_key="1" * 64),
            client_order_id=CLIENT_ID,
            occurred_at_ms=101,
        )
    original = tuple(
        store._connection.execute(
            "SELECT * FROM exchange_orders WHERE durable_command_key=?", (COMMAND_KEY,)
        ).fetchone()
    )
    collision = list(original)
    collision[0] = "2" * 64
    collision[1] = "3" * 64
    with pytest.raises(sqlite3.IntegrityError):
        store._connection.execute(
            "INSERT INTO exchange_orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(collision),
        )
    with pytest.raises(PersistenceConflictError, match="identity"):
        store.prepare_order(
            execution_root_id=ROOT_ID,
            session_id=SESSION_ID,
            command=command(),
            client_order_id="ctt1t-bbbbbbbbbbbbbbbbbbbbbbbbbb",
            occurred_at_ms=101,
        )
    store.begin_submission(COMMAND_KEY, occurred_at_ms=200)
    with pytest.raises(sqlite3.IntegrityError):
        store._connection.execute(
            "INSERT INTO exchange_submission_attempts VALUES(?,?,?,?,?,?,?)",
            ("second", COMMAND_KEY, 2, CLIENT_ID, 201, "10000", "0" * 64),
        )
    assert (
        store._connection.execute("SELECT count(*) FROM exchange_submission_attempts").fetchone()[0]
        == 1
    )
    store.close()


@pytest.mark.parametrize(
    "boundary",
    [
        "attempt_after_record",
        "attempt_after_order",
        "attempt_after_counters",
        "attempt_before_commit",
        "attempt_after_commit",
    ],
)
def test_attempt_transaction_failpoints_are_all_or_nothing(tmp_path: Path, boundary: str) -> None:
    path = tmp_path / f"{boundary}.sqlite3"
    initial = open_store(path)
    bootstrap(initial)
    prepare(initial)
    initial.close()

    def failpoint(value: str) -> None:
        if value == boundary:
            raise RuntimeError("injected crash")

    crashing = open_store(path, failure_injector=failpoint)
    with pytest.raises(RuntimeError, match="injected crash"):
        crashing.begin_submission(COMMAND_KEY, occurred_at_ms=200)
    crashing.close()
    reopened = open_store(path)
    try:
        order = reopened.load_order(COMMAND_KEY)
        committed = boundary == "attempt_after_commit"
        assert order.order.status is (
            ExchangeOrderStatus.SUBMITTING if committed else ExchangeOrderStatus.PREPARED
        )
        session = reopened.load_session(SESSION_ID)
        assert session.submission_count == int(committed)
        assert session.cumulative_submitted_notional == (D("10000") if committed else D(0))
        assert reopened._connection.execute(
            "SELECT count(*) FROM exchange_submission_attempts"
        ).fetchone()[0] == int(committed)
    finally:
        reopened.close()


@pytest.mark.parametrize(
    "boundary",
    ["status_after_order", "status_after_counters", "status_before_commit", "status_after_commit"],
)
def test_ack_and_ambiguity_transactions_are_atomic(tmp_path: Path, boundary: str) -> None:
    for target in (ExchangeOrderStatus.ACKNOWLEDGED, ExchangeOrderStatus.RECONCILIATION_REQUIRED):
        path = tmp_path / f"{target}-{boundary}.sqlite3"
        initial = open_store(path)
        bootstrap(initial)
        prepare(initial)
        initial.begin_submission(COMMAND_KEY, occurred_at_ms=200)
        initial.close()

        def failpoint(value: str) -> None:
            if value == boundary:
                raise RuntimeError("injected crash")

        crashing = open_store(path, failure_injector=failpoint)
        kwargs = (
            {"exchange_order_id": "123", "acknowledged_at_ms": 201}
            if target is ExchangeOrderStatus.ACKNOWLEDGED
            else {"count_failure": True}
        )
        with pytest.raises(RuntimeError, match="injected crash"):
            crashing.record_status(
                COMMAND_KEY,
                status=target,
                occurred_at_ms=201,
                reason_code="result",
                **kwargs,
            )
        crashing.close()
        reopened = open_store(path)
        try:
            committed = boundary == "status_after_commit"
            order = reopened.load_order(COMMAND_KEY).order
            assert order.status is (target if committed else ExchangeOrderStatus.SUBMITTING)
            assert order.exchange_order_id == (
                "123" if committed and target is ExchangeOrderStatus.ACKNOWLEDGED else None
            )
            failures = reopened.load_session(SESSION_ID).execution_failure_count
            assert failures == int(
                committed and target is ExchangeOrderStatus.RECONCILIATION_REQUIRED
            )
        finally:
            reopened.close()


def test_ack_replay_is_idempotent_and_conflicting_ack_fails(tmp_path: Path) -> None:
    store = open_store(tmp_path / "ack-replay.sqlite3")
    bootstrap(store)
    prepare(store)
    store.begin_submission(COMMAND_KEY, occurred_at_ms=200)
    acked = store.record_status(
        COMMAND_KEY,
        status=ExchangeOrderStatus.ACKNOWLEDGED,
        occurred_at_ms=201,
        reason_code="ack",
        exchange_order_id="123",
        acknowledged_at_ms=201,
    )
    event_count = store._connection.execute(
        "SELECT count(*) FROM exchange_order_events"
    ).fetchone()[0]
    replay = store.record_status(
        COMMAND_KEY,
        status=ExchangeOrderStatus.ACKNOWLEDGED,
        occurred_at_ms=999,
        reason_code="ack_replay",
        exchange_order_id="123",
        acknowledged_at_ms=201,
    )
    assert replay == acked
    assert (
        store._connection.execute("SELECT count(*) FROM exchange_order_events").fetchone()[0]
        == event_count
    )
    with pytest.raises(PersistenceConflictError, match="conflicting facts"):
        store.record_status(
            COMMAND_KEY,
            status=ExchangeOrderStatus.ACKNOWLEDGED,
            occurred_at_ms=1000,
            reason_code="conflicting_ack",
            exchange_order_id="456",
        )
    store.close()


@pytest.mark.parametrize(
    "outcome",
    [
        TransportOutcomeKind.MAY_HAVE_BEEN_TRANSMITTED,
        TransportOutcomeKind.DETERMINISTIC_REJECTION,
    ],
)
def test_ambiguous_and_rejection_replays_never_repeat_attempt_or_counter(
    tmp_path: Path, outcome: TransportOutcomeKind
) -> None:
    path = tmp_path / f"storm-{outcome}.sqlite3"
    store = open_store(path)
    bootstrap(store)
    prepare(store)
    gateway = FakeGateway(outcome)
    lifecycle = DurableExchangeOrderLifecycle(store, gateway, timestamp_provider=lambda: 200)
    result = lifecycle.submit_prepared(COMMAND_KEY)
    expected_failures = store.load_session(SESSION_ID).execution_failure_count
    expected_events = store._connection.execute(
        "SELECT count(*) FROM exchange_order_events"
    ).fetchone()[0]
    store.close()
    for _ in range(5):
        reopened = open_store(path)
        try:
            restarted = DurableExchangeOrderLifecycle(
                reopened, gateway, timestamp_provider=lambda: 300
            )
            assert restarted.submit_prepared(COMMAND_KEY) == result
            assert reopened.load_session(SESSION_ID).execution_failure_count == expected_failures
            assert (
                reopened._connection.execute(
                    "SELECT count(*) FROM exchange_submission_attempts"
                ).fetchone()[0]
                == 1
            )
            assert (
                reopened._connection.execute(
                    "SELECT count(*) FROM exchange_order_events"
                ).fetchone()[0]
                == expected_events
            )
        finally:
            reopened.close()
    assert gateway.calls == 1


def test_ack_returned_then_local_commit_crash_has_one_submission_across_restarts(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ack-returned-local-crash.sqlite3"

    def failpoint(boundary: str) -> None:
        if boundary == "status_before_commit":
            raise RuntimeError("crash before ACK commit")

    store = open_store(path, failure_injector=failpoint)
    bootstrap(store)
    prepare(store)
    gateway = FakeGateway()
    lifecycle = DurableExchangeOrderLifecycle(store, gateway, timestamp_provider=lambda: 200)
    with pytest.raises(RuntimeError, match="ACK commit"):
        lifecycle.submit_prepared(COMMAND_KEY)
    store.close()
    assert gateway.calls == 1
    for _ in range(5):
        reopened = open_store(path)
        try:
            restarted = DurableExchangeOrderLifecycle(
                reopened, gateway, timestamp_provider=lambda: 300
            )
            order = restarted.submit_prepared(COMMAND_KEY)
            assert order.order.status is ExchangeOrderStatus.SUBMITTING
            assert order.order.client_order_id == CLIENT_ID
            assert reopened.list_orders_requiring_reconciliation() == (order,)
        finally:
            reopened.close()
    assert gateway.calls == 1


def test_prepared_restart_allows_exactly_one_first_submission(tmp_path: Path) -> None:
    path = tmp_path / "prepared-resume.sqlite3"
    store = open_store(path)
    bootstrap(store)
    prepared = prepare(store)
    store.close()
    gateway = FakeGateway()
    reopened = open_store(path)
    try:
        assert reopened.list_submit_eligible() == (prepared,)
        lifecycle = DurableExchangeOrderLifecycle(reopened, gateway, timestamp_provider=lambda: 200)
        assert (
            lifecycle.submit_prepared(COMMAND_KEY).order.status is ExchangeOrderStatus.ACKNOWLEDGED
        )
        assert (
            lifecycle.submit_prepared(COMMAND_KEY).order.status is ExchangeOrderStatus.ACKNOWLEDGED
        )
    finally:
        reopened.close()
    assert gateway.calls == 1


def test_reconciliation_resolution_is_authoritative_and_terminal_states_are_immutable(
    tmp_path: Path,
) -> None:
    store = open_store(tmp_path / "resolution.sqlite3")
    bootstrap(store)
    prepare(store)
    store.begin_submission(COMMAND_KEY, occurred_at_ms=200)
    store.record_status(
        COMMAND_KEY,
        status=ExchangeOrderStatus.RECONCILIATION_REQUIRED,
        occurred_at_ms=201,
        reason_code="ambiguous",
        count_failure=True,
    )
    with pytest.raises(ExchangeOrderStateError, match="forbidden"):
        store.record_status(
            COMMAND_KEY,
            status=ExchangeOrderStatus.SUBMITTING,
            occurred_at_ms=202,
            reason_code="unsafe_reset",
        )
    resolved = store.record_status(
        COMMAND_KEY,
        status=ExchangeOrderStatus.REJECTED,
        occurred_at_ms=203,
        reason_code="authoritative_reconciliation",
        source=ExchangeOrderEventSource.RECONCILIATION,
    )
    assert resolved.order.status is ExchangeOrderStatus.REJECTED
    for forbidden in (
        ExchangeOrderStatus.PREPARED,
        ExchangeOrderStatus.SUBMITTING,
        ExchangeOrderStatus.ACKNOWLEDGED,
    ):
        with pytest.raises(ExchangeOrderStateError, match="forbidden"):
            store.record_status(
                COMMAND_KEY,
                status=forbidden,
                occurred_at_ms=204,
                reason_code="terminal_reset",
            )
    store.close()


def test_durable_financial_arithmetic_ignores_global_decimal_precision(tmp_path: Path) -> None:
    path = tmp_path / "decimal-context.sqlite3"
    store = open_store(path)
    bootstrap(store)
    exact = D("123456789.123456789123456789")
    store.prepare_order(
        execution_root_id=ROOT_ID,
        session_id=SESSION_ID,
        command=command(approved_notional=exact),
        client_order_id=CLIENT_ID,
        occurred_at_ms=100,
    )
    with localcontext() as context:
        context.prec = 2
        store.begin_submission(COMMAND_KEY, occurred_at_ms=200)
    store.close()
    reopened = open_store(path)
    try:
        assert reopened.load_session(SESSION_ID).cumulative_submitted_notional == exact
    finally:
        reopened.close()


def test_exchange_writer_exclusion_and_unresolved_root_guard(tmp_path: Path) -> None:
    path = tmp_path / "writer.sqlite3"
    config = SQLitePersistenceConfig(
        path, busy_timeout_ms=1, expected_schema_version=2, exclusive_writer=True
    )
    first = SQLiteExchangePersistence(config)
    bootstrap(first)
    prepare(first)
    with pytest.raises(PersistenceWriterConflictError):
        SQLiteExchangePersistence(config)
    with pytest.raises(PersistenceConflictError, match="unresolved"):
        first.initialize_execution(
            ExchangeExecutionRoot(
                "testnet-root-2",
                "account-2",
                MarketStreamKey("BTCUSDT", "5m"),
                "8" * 64,
                20,
            ),
            ExecutionSession("testnet-session-2", "account-2", 20, ExecutionSessionState.ACTIVE),
            audit_identifier="new-root",
        )
    first.close()


@pytest.mark.parametrize(
    "observation",
    [
        SubmissionObservation(
            TransportOutcomeKind.RESPONSE_RECEIVED,
            "ctt1t-bbbbbbbbbbbbbbbbbbbbbbbbbb",
            "123",
            ExchangeOrderStatus.ACKNOWLEDGED,
            201,
        ),
        SubmissionObservation(
            TransportOutcomeKind.RESPONSE_RECEIVED,
            CLIENT_ID,
            None,
            ExchangeOrderStatus.ACKNOWLEDGED,
            201,
        ),
        SubmissionObservation(
            TransportOutcomeKind.DETERMINISTIC_REJECTION,
            CLIENT_ID,
            "123",
            ExchangeOrderStatus.REJECTED,
        ),
    ],
)
def test_malformed_gateway_outcomes_require_reconciliation(
    tmp_path: Path, observation: SubmissionObservation
) -> None:
    class Gateway:
        def submit_market_order(self, request):
            return observation

    store = open_store(tmp_path / f"malformed-{hash(observation)}.sqlite3")
    bootstrap(store)
    prepare(store)
    result = DurableExchangeOrderLifecycle(
        store, Gateway(), timestamp_provider=lambda: 200
    ).submit_prepared(COMMAND_KEY)
    assert result.order.status is ExchangeOrderStatus.RECONCILIATION_REQUIRED
    assert store.load_session(SESSION_ID).execution_failure_count == 1
    store.close()


def test_model_and_lifecycle_dependency_validation(tmp_path: Path) -> None:
    control = ExchangeControlState(
        ROOT_ID,
        KillSwitch(KillSwitchState.ACTIVE, 0, "safe_default", 10),
        0,
        "audit",
    )
    result = LocalExchangeRecoveryResult(LocalExchangeRecoveryStatus.CLEAN, (), (), ())
    assert result.status is LocalExchangeRecoveryStatus.CLEAN
    for constructor in (
        lambda: ExchangeExecutionRoot(
            ROOT_ID, "account-1", MarketStreamKey("ETHUSDT", "1m"), "9" * 64, 10
        ),
        lambda: ExchangeControlState(ROOT_ID, control.kill_switch, -1, "audit"),
        lambda: LocalExchangeRecoveryResult("bad", (), (), ()),
        lambda: ExchangeOrder(
            COMMAND_KEY,
            CLIENT_ID,
            "account-1",
            KEY,
            ExchangeOrderSide.BUY,
            D("0.1"),
            ExchangeOrderStatus.PREPARED,
            10,
            exchange_order_id="123",
        ),
    ):
        with pytest.raises((PersistenceIntegrityError, ValueError)):
            constructor()
    store = open_store(tmp_path / "lifecycle-validation.sqlite3")
    bootstrap(store)
    prepare(store)
    gateway = FakeGateway()
    with pytest.raises(TypeError):
        DurableExchangeOrderLifecycle(object(), gateway, timestamp_provider=lambda: 1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="gateway"):
        DurableExchangeOrderLifecycle(store, object(), timestamp_provider=lambda: 1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="timestamp_provider"):
        DurableExchangeOrderLifecycle(store, gateway, timestamp_provider=1)  # type: ignore[arg-type]
    lifecycle = DurableExchangeOrderLifecycle(store, gateway, timestamp_provider=lambda: -1)
    with pytest.raises(TypeError, match="timestamp"):
        lifecycle.prepare(
            execution_root_id=ROOT_ID,
            session_id=SESSION_ID,
            command=command(durable_command_key="1" * 64),
            client_order_id="ctt1t-bbbbbbbbbbbbbbbbbbbbbbbbbb",
        )
    store.close()


def test_context_ownership_control_authorization_and_terminal_trade_guard(tmp_path: Path) -> None:
    path = tmp_path / "context.sqlite3"
    with open_store(path) as store:
        bootstrap(store)
        prepare(store)
        store.begin_submission(COMMAND_KEY, occurred_at_ms=200)
        store.record_status(
            COMMAND_KEY,
            status=ExchangeOrderStatus.REJECTED,
            occurred_at_ms=201,
            reason_code="deterministic_rejection",
        )
        with pytest.raises(PersistenceConflictError, match="REJECTED"):
            store.persist_trades(COMMAND_KEY, (object(),))  # type: ignore[arg-type]
        with pytest.raises(PersistenceIntegrityError, match="reason"):
            store.set_kill_switch(
                ROOT_ID,
                state=KillSwitchState.CLEARED,
                expected_generation=0,
                reason_code="",
                audit_identifier="",
                occurred_at_ms=202,
            )
        with pytest.raises(PersistenceIntegrityError, match="target state"):
            store.set_kill_switch(
                ROOT_ID,
                state=False,  # type: ignore[arg-type]
                expected_generation=0,
                reason_code="operator",
                audit_identifier="ticket",
                occurred_at_ms=202,
            )
        with pytest.raises(PersistenceIntegrityError, match="session"):
            store.load_session("missing")


@pytest.mark.parametrize(
    "corruption",
    [
        "root_digest",
        "root_environment",
        "event_digest",
        "event_payload",
        "state_digest",
        "projection_client",
        "projection_exchange_id",
        "projection_cumulative",
        "projection_version",
        "missing_projection",
        "session_lineage",
    ],
)
def test_local_recovery_detects_authority_corruption(tmp_path: Path, corruption: str) -> None:
    store = open_store(tmp_path / f"{corruption}.sqlite3")
    bootstrap(store)
    prepare(store)
    if corruption == "root_digest":
        store._connection.execute("UPDATE exchange_execution_roots SET state_digest=?", ("0" * 64,))
        with pytest.raises(PersistenceIntegrityError, match="root digest"):
            store.load_bootstrap(ROOT_ID)
    elif corruption == "root_environment":
        store._connection.execute("PRAGMA ignore_check_constraints=ON")
        store._connection.execute("UPDATE exchange_execution_roots SET environment='live'")
        with pytest.raises(PersistenceIntegrityError, match="root is invalid"):
            store.recover_local()
    elif corruption in {"event_digest", "event_payload"}:
        store._connection.execute("DROP TRIGGER exchange_order_events_no_update")
        column, value = (
            ("payload_digest", "0" * 64)
            if corruption == "event_digest"
            else ("payload", "not-json")
        )
        store._connection.execute(f"UPDATE exchange_order_events SET {column}=?", (value,))
        with pytest.raises(PersistenceIntegrityError, match="event"):
            store.load_order(COMMAND_KEY)
    elif corruption == "state_digest":
        store._connection.execute("UPDATE exchange_orders SET state_digest=?", ("0" * 64,))
        with pytest.raises(PersistenceIntegrityError, match="state digest"):
            store.load_order(COMMAND_KEY)
    elif corruption == "projection_client":
        store._connection.execute(
            "UPDATE exchange_orders SET client_order_id=?",
            ("ctt1t-bbbbbbbbbbbbbbbbbbbbbbbbbb",),
        )
        with pytest.raises(PersistenceIntegrityError, match="state digest"):
            store.load_order(COMMAND_KEY)
    elif corruption == "projection_exchange_id":
        store._connection.execute("UPDATE exchange_orders SET exchange_order_id='123'")
        with pytest.raises(ExchangeOrderStateError, match="PREPARED"):
            store.load_order(COMMAND_KEY)
    elif corruption == "projection_cumulative":
        store._connection.execute("UPDATE exchange_orders SET cumulative_executed_base='0.01'")
        with pytest.raises(ExchangeOrderStateError, match="PREPARED"):
            store.load_order(COMMAND_KEY)
    elif corruption == "projection_version":
        store._connection.execute("UPDATE exchange_orders SET version=2")
        with pytest.raises(PersistenceIntegrityError, match="projection/event"):
            store.load_order(COMMAND_KEY)
    elif corruption == "missing_projection":
        store._connection.execute("PRAGMA foreign_keys=OFF")
        store._connection.execute("DELETE FROM exchange_orders")
        with pytest.raises(PersistenceIntegrityError, match="order is missing"):
            store.load_order(COMMAND_KEY)
    else:
        store._connection.execute("PRAGMA foreign_keys=OFF")
        store._connection.execute(
            "UPDATE exchange_sessions SET account_fingerprint='other' WHERE session_id=?",
            (SESSION_ID,),
        )
        with pytest.raises(PersistenceIntegrityError, match="session lineage"):
            store.load_order(COMMAND_KEY)
    store.close()
