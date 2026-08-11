import sqlite3
import time
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from app.exchange_execution import (
    AccountBalance,
    DeterministicClientOrderIdFactory,
    ExchangeCommand,
    ExchangeInstrumentRules,
    ExchangeOrder,
    ExchangeOrderSide,
    ExchangeOrderStatus,
    ExchangeReconciler,
    ExchangeReconciliationReason,
    ExchangeReconciliationResult,
    ExchangeTrade,
    ExchangeTradeHistory,
    ExchangeTradeIdentity,
    ExecutionEnvironment,
    ExecutionSession,
    ExecutionSessionState,
    OrderQueryResult,
    QueryResultKind,
)
from app.market.models import MarketStreamKey
from app.persistence import (
    ExchangeExecutionRoot,
    ExchangeReconciliationStatus,
    PersistentExchangeReconciliationRun,
    SQLiteExchangePersistence,
    SQLitePersistenceConfig,
    TradeCompletenessStatus,
)
from app.persistence.canonical import canonical_json, canonical_sha256
from app.persistence.exceptions import (
    PersistenceConflictError,
    PersistenceConnectionError,
    PersistenceIntegrityError,
    PersistenceMigrationError,
    PersistenceTransactionError,
)
from app.persistence.sqlite import SQLiteTradingPersistence

D = Decimal
KEY = MarketStreamKey("BTCUSDT", "1m")
ROOT = "root-m47"
SESSION = "session-m47"
COMMAND = "a" * 64
CLIENT = "ctt1t-abcdefghijklmnopqrstuvwxyz"
RULES = ExchangeInstrumentRules(KEY, D("0.00001"), D("0.00001"), D(5), "rules-v1")


def command(key: str = COMMAND) -> ExchangeCommand:
    return ExchangeCommand(
        key,
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


def store(path: Path, *, failure_injector=None) -> SQLiteExchangePersistence:
    value = SQLiteExchangePersistence(
        SQLitePersistenceConfig(path, expected_schema_version=3),
        failure_injector=failure_injector,
    )
    value.initialize_execution(
        ExchangeExecutionRoot(ROOT, "account-m47", KEY, "9" * 64, 10),
        ExecutionSession(SESSION, "account-m47", 10, ExecutionSessionState.ACTIVE),
        audit_identifier="m47-test",
    )
    return value


def prepare(value: SQLiteExchangePersistence) -> None:
    value.prepare_order(
        execution_root_id=ROOT,
        session_id=SESSION,
        command=command(),
        client_order_id=CLIENT,
        occurred_at_ms=100,
    )


def remote(status: ExchangeOrderStatus, **changes: object) -> ExchangeOrder:
    values: dict[str, object] = {
        "durable_command_key": COMMAND,
        "client_order_id": CLIENT,
        "account_fingerprint": "account-m47",
        "stream_key": KEY,
        "side": ExchangeOrderSide.BUY,
        "original_base_quantity": D("0.1"),
        "status": status,
        "created_at_ms": 100,
        "submitted_at_ms": 110,
        "exchange_order_id": "900719925474099312345",
        "acknowledged_at_ms": 120,
        "latest_exchange_update_ms": 130,
    }
    if status is ExchangeOrderStatus.PARTIALLY_FILLED:
        values.update(
            cumulative_executed_base_quantity=D("0.04"), cumulative_quote_quantity=D("4000")
        )
    if status is ExchangeOrderStatus.FILLED:
        values.update(
            cumulative_executed_base_quantity=D("0.1"), cumulative_quote_quantity=D("10000")
        )
    if status is ExchangeOrderStatus.REJECTED:
        values["exchange_order_id"] = None
        values["acknowledged_at_ms"] = None
    values.update(changes)
    return ExchangeOrder(**values)  # type: ignore[arg-type]


def trade(
    quantity: Decimal | None = None, trade_id: str = "9223372036854775808123"
) -> ExchangeTrade:
    quantity = D("0.1") if quantity is None else quantity
    return ExchangeTrade(
        ExchangeTradeIdentity(
            "binance-spot",
            ExecutionEnvironment.TESTNET,
            "account-m47",
            "BTCUSDT",
            "900719925474099312345",
            trade_id,
        ),
        CLIENT,
        KEY,
        ExchangeOrderSide.BUY,
        quantity,
        D("100000"),
        quantity * D("100000"),
        D("0.0001"),
        "BNB",
        130,
    )


class FakeReader:
    def __init__(self) -> None:
        self.query = OrderQueryResult(QueryResultKind.NOT_FOUND)
        self.trades: tuple[ExchangeTrade, ...] = ()
        self.open_orders: tuple[ExchangeOrder, ...] = ()
        self.recent_trades: tuple[ExchangeTrade, ...] = ()
        self.rules = RULES
        self.fail_query = False
        self.calls: list[str] = []

    def query_order(self, *, stream_key: MarketStreamKey, client_order_id: str) -> OrderQueryResult:
        self.calls.append(f"query:{client_order_id}")
        if self.fail_query:
            raise RuntimeError("normalized fake failure")
        return self.query

    def list_order_trades(
        self, *, stream_key: MarketStreamKey, exchange_order_id: str
    ) -> tuple[ExchangeTrade, ...]:
        self.calls.append(f"trades:{exchange_order_id}")
        return self.trades

    def list_open_orders(self, *, stream_key: MarketStreamKey) -> tuple[ExchangeOrder, ...]:
        self.calls.append("open")
        return self.open_orders

    def read_trade_history(
        self, *, stream_key: MarketStreamKey, start_at_ms: int
    ) -> ExchangeTradeHistory:
        self.calls.append("recent")
        return ExchangeTradeHistory(self.recent_trades, start_at_ms, 10_000, True)

    def read_balances(self, assets: tuple[str, ...]) -> tuple[AccountBalance, ...]:
        self.calls.append("balances")
        return tuple(AccountBalance(asset, D(0), D(0)) for asset in assets)

    def read_instrument_rules(self, stream_key: MarketStreamKey) -> ExchangeInstrumentRules:
        self.calls.append("rules")
        return self.rules


def reconciler(
    value: SQLiteExchangePersistence, reader: FakeReader, ids=None
) -> ExchangeReconciler:
    ticks = iter(range(1000, 1100))
    ids = iter(("r-1", "r-2", "r-3")) if ids is None else ids
    return ExchangeReconciler(
        value,
        reader,
        expected_rules=RULES,
        timestamp_provider=lambda: next(ticks),
        reconciliation_id_factory=lambda: next(ids),
    )


def test_empty_and_prepared_only_roots_are_safe_without_order_query(tmp_path: Path) -> None:
    value = store(tmp_path / "safe.sqlite3")
    reader = FakeReader()
    service = reconciler(value, reader)
    first = service.reconcile(ROOT)
    assert first.outcome is ExchangeReconciliationStatus.SAFE
    assert first.generation_after == 1
    prepare(value)
    second = service.reconcile(ROOT)
    assert second.outcome is ExchangeReconciliationStatus.SAFE
    assert second.generation_after == 2
    assert not any(call.startswith("query:") for call in reader.calls)
    assert value.read_control_state(ROOT).kill_switch.state.value == "ACTIVE"
    value.close()


def test_migration_v3_upgrades_v2_without_editing_history(tmp_path: Path) -> None:
    path = tmp_path / "upgrade.sqlite3"
    legacy = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=2))
    legacy.initialize_execution(
        ExchangeExecutionRoot(ROOT, "account-m47", KEY, "9" * 64, 10),
        ExecutionSession(SESSION, "account-m47", 10, ExecutionSessionState.ACTIVE),
        audit_identifier="m47-test",
    )
    legacy.prepare_order(
        execution_root_id=ROOT,
        session_id=SESSION,
        command=command(),
        client_order_id=CLIENT,
        occurred_at_ms=100,
    )
    legacy.begin_submission(COMMAND, occurred_at_ms=110)
    legacy.record_status(
        COMMAND,
        status=ExchangeOrderStatus.ACKNOWLEDGED,
        occurred_at_ms=120,
        reason_code="upgrade-fixture",
        exchange_order_id="900719925474099312345",
        acknowledged_at_ms=120,
    )
    v2_tables = (
        "exchange_execution_roots",
        "exchange_sessions",
        "exchange_control_state",
        "exchange_orders",
        "exchange_order_events",
        "exchange_submission_attempts",
    )
    before = {
        table: tuple(tuple(row) for row in legacy._connection.execute(f"SELECT * FROM {table}"))
        for table in v2_tables
    }
    legacy.close()
    upgraded = store(path)
    rows = upgraded._connection.execute(
        "SELECT version,name FROM schema_migrations ORDER BY version"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        (1, "001_initial.sql"),
        (2, "002_exchange_execution.sql"),
        (3, "003_exchange_reconciliation.sql"),
    ]
    state = upgraded.read_reconciliation_state(ROOT)
    assert state.status is ExchangeReconciliationStatus.REQUIRED and state.generation == 0
    after = {
        table: tuple(tuple(row) for row in upgraded._connection.execute(f"SELECT * FROM {table}"))
        for table in v2_tables
    }
    assert after == before
    assert upgraded.load_order(COMMAND).order.status is ExchangeOrderStatus.ACKNOWLEDGED
    upgraded.close()


def test_migration_v3_failure_rolls_back_and_reopen_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "migration-failure.sqlite3"
    legacy = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=2))
    legacy.close()
    original = SQLiteTradingPersistence._migration

    def broken(name: str = "001_initial.sql") -> tuple[str, str]:
        if name == "003_exchange_reconciliation.sql":
            return "CREATE TABLE partial_m47(id INTEGER); BROKEN SQL;", "0" * 64
        return original(name)

    monkeypatch.setattr(SQLiteTradingPersistence, "_migration", staticmethod(broken))
    with pytest.raises(PersistenceMigrationError):
        SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=3))
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 2
        assert (
            connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE name='partial_m47'"
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()
    monkeypatch.setattr(SQLiteTradingPersistence, "_migration", staticmethod(original))
    first = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=3))
    first.close()
    second = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=3))
    assert (
        second._connection.execute(
            "SELECT count(*) FROM schema_migrations WHERE version=3"
        ).fetchone()[0]
        == 1
    )
    second.close()


def test_submitting_not_found_stays_required_and_does_not_advance(tmp_path: Path) -> None:
    value = store(tmp_path / "not-found.sqlite3")
    prepare(value)
    value.begin_submission(COMMAND, occurred_at_ms=110)
    result = reconciler(value, FakeReader()).reconcile(ROOT)
    assert result.outcome is ExchangeReconciliationStatus.REQUIRED
    assert result.generation_after == 0
    assert result.reasons == (ExchangeReconciliationReason.ORDER_NOT_FOUND,)
    assert value.load_order(COMMAND).order.status is ExchangeOrderStatus.RECONCILIATION_REQUIRED
    value.close()


def test_submitting_found_acknowledged_assigns_exchange_id_without_trade(
    tmp_path: Path,
) -> None:
    value = store(tmp_path / "submitting-new.sqlite3")
    prepare(value)
    value.begin_submission(COMMAND, occurred_at_ms=110)
    reader = FakeReader()
    acknowledged = remote(ExchangeOrderStatus.ACKNOWLEDGED)
    reader.query = OrderQueryResult(QueryResultKind.FOUND, acknowledged)
    reader.open_orders = (acknowledged,)
    result = reconciler(value, reader).reconcile(ROOT)
    durable = value.load_order(COMMAND)
    assert result.outcome is ExchangeReconciliationStatus.SAFE
    assert durable.order.status is ExchangeOrderStatus.ACKNOWLEDGED
    assert durable.order.exchange_order_id == "900719925474099312345"
    assert value._connection.execute("SELECT count(*) FROM exchange_trades").fetchone()[0] == 0
    assert result.submission_ready is False
    value.close()


@pytest.mark.parametrize(
    "target",
    (
        ExchangeOrderStatus.ACKNOWLEDGED,
        ExchangeOrderStatus.PARTIALLY_FILLED,
        ExchangeOrderStatus.FILLED,
        ExchangeOrderStatus.REJECTED,
        ExchangeOrderStatus.CANCELED,
        ExchangeOrderStatus.EXPIRED,
    ),
)
def test_reconciliation_required_resolution_matrix(tmp_path: Path, target) -> None:
    value = store(tmp_path / f"resolution-{target.value}.sqlite3")
    prepare(value)
    value.begin_submission(COMMAND, occurred_at_ms=110)
    value.record_status(
        COMMAND,
        status=ExchangeOrderStatus.RECONCILIATION_REQUIRED,
        occurred_at_ms=115,
        reason_code="test-ambiguity",
    )
    reader = FakeReader()
    authoritative = remote(target)
    reader.query = OrderQueryResult(QueryResultKind.FOUND, authoritative)
    if target in {ExchangeOrderStatus.ACKNOWLEDGED, ExchangeOrderStatus.PARTIALLY_FILLED}:
        reader.open_orders = (authoritative,)
    if target is ExchangeOrderStatus.PARTIALLY_FILLED:
        reader.trades = (trade(D("0.04")),)
        reader.recent_trades = reader.trades
    elif target is ExchangeOrderStatus.FILLED:
        reader.trades = (trade(),)
        reader.recent_trades = reader.trades
    result = reconciler(value, reader).reconcile(ROOT)
    assert result.outcome is ExchangeReconciliationStatus.SAFE
    assert value.load_order(COMMAND).order.status is target
    value.close()


def test_reconciliation_required_not_found_repeats_without_new_event(tmp_path: Path) -> None:
    value = store(tmp_path / "repeated-not-found.sqlite3")
    prepare(value)
    value.begin_submission(COMMAND, occurred_at_ms=110)
    first = reconciler(value, FakeReader()).reconcile(ROOT)
    version = value.load_order(COMMAND).version
    second = reconciler(value, FakeReader(), iter(("not-found-again",))).reconcile(ROOT)
    assert first.generation_after == second.generation_after == 0
    assert value.load_order(COMMAND).version == version
    assert value.list_submit_eligible() == ()
    value.close()


def test_found_filled_persists_trade_and_advances_once(tmp_path: Path) -> None:
    path = tmp_path / "filled.sqlite3"
    value = store(path)
    prepare(value)
    value.begin_submission(COMMAND, occurred_at_ms=110)
    reader = FakeReader()
    reader.query = OrderQueryResult(QueryResultKind.FOUND, remote(ExchangeOrderStatus.FILLED))
    reader.trades = (trade(),)
    reader.recent_trades = reader.trades
    result = reconciler(value, reader).reconcile(ROOT)
    assert result.outcome is ExchangeReconciliationStatus.SAFE
    assert result.generation_after == 1
    assert value.trade_completeness(COMMAND) is TradeCompletenessStatus.COMPLETE
    assert value.load_session(SESSION).submission_count == 1
    value.close()
    reopened = store(path)
    assert reopened.read_reconciliation_state(ROOT).generation == 1
    assert reopened.trade_completeness(COMMAND) is TradeCompletenessStatus.COMPLETE
    reopened.close()


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (
            lambda reader: setattr(
                reader,
                "open_orders",
                (
                    remote(
                        ExchangeOrderStatus.ACKNOWLEDGED,
                        client_order_id="ctt1t-abcdefghijklmnopqrstuvwxy1",
                        durable_command_key="8" * 64,
                    ),
                ),
            ),
            ExchangeReconciliationReason.UNKNOWN_OPEN_ORDER,
        ),
        (
            lambda reader: setattr(
                reader,
                "recent_trades",
                (
                    replace(
                        trade(), identity=replace(trade().identity, exchange_order_id="unknown")
                    ),
                ),
            ),
            ExchangeReconciliationReason.UNKNOWN_TRADE,
        ),
        (
            lambda reader: setattr(reader, "rules", replace(RULES, min_notional=D(6))),
            ExchangeReconciliationReason.INSTRUMENT_RULES_MISMATCH,
        ),
    ],
)
def test_unmanaged_facts_and_rules_mismatch_conflict(tmp_path: Path, mutate, reason) -> None:
    value = store(tmp_path / f"{reason.value}.sqlite3")
    reader = FakeReader()
    mutate(reader)
    result = reconciler(value, reader).reconcile(ROOT)
    assert result.outcome is ExchangeReconciliationStatus.CONFLICT
    assert reason in result.reasons
    assert result.generation_after == 0
    value.close()


def test_reader_failure_is_failed_and_local_recovery_remains_clean(tmp_path: Path) -> None:
    value = store(tmp_path / "failed.sqlite3")
    prepare(value)
    value.begin_submission(COMMAND, occurred_at_ms=110)
    reader = FakeReader()
    reader.fail_query = True
    result = reconciler(value, reader).reconcile(ROOT)
    assert result.outcome is ExchangeReconciliationStatus.FAILED
    assert result.generation_after == 0
    assert value.recover_local().status.value == "CLEAN"
    assert value.read_reconciliation_state(ROOT).status is ExchangeReconciliationStatus.FAILED
    value.close()


def test_reconciliation_run_replay_and_append_only_guards(tmp_path: Path) -> None:
    value = store(tmp_path / "guards.sqlite3")
    result = reconciler(value, FakeReader()).reconcile(ROOT)
    row = value._connection.execute("SELECT * FROM exchange_reconciliation_runs").fetchone()
    assert row["generation_before"] == 0 and row["generation_after"] == 1
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        value._connection.execute("UPDATE exchange_reconciliation_runs SET outcome='FAILED'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        value._connection.execute("DELETE FROM exchange_reconciliation_runs")
    assert result.generation_after == 1
    value.close()


def test_corrupt_reconciliation_state_and_stale_generation_fail_closed(tmp_path: Path) -> None:
    value = store(tmp_path / "corrupt.sqlite3")
    value._connection.execute(
        "UPDATE exchange_reconciliation_current SET state_digest=?", ("0" * 64,)
    )
    with pytest.raises(PersistenceIntegrityError, match="digest"):
        value.read_reconciliation_state(ROOT)
    with pytest.raises(PersistenceIntegrityError):
        value.recover_local()
    value.close()


@pytest.mark.parametrize("field", ("status", "generation", "latest", "version"))
def test_consistently_rehashed_current_corruption_fails_closed(tmp_path: Path, field: str) -> None:
    value = store(tmp_path / f"current-{field}.sqlite3")
    reconciler(value, FakeReader()).reconcile(ROOT)
    row = value._connection.execute(
        "SELECT * FROM exchange_reconciliation_current WHERE execution_root_id=?", (ROOT,)
    ).fetchone()
    payload = {
        "execution_root_id": ROOT,
        "status": row["status"],
        "generation": row["generation"],
        "latest_reconciliation_id": row["latest_reconciliation_id"],
        "updated_at_ms": row["updated_at_ms"],
        "model_version": row["model_version"],
    }
    if field == "status":
        payload["status"] = "FAILED"
    elif field == "generation":
        payload["generation"] = 2
        value._connection.execute(
            "UPDATE exchange_control_state SET reconciliation_generation=2 WHERE execution_root_id=?",
            (ROOT,),
        )
    elif field == "latest":
        payload["latest_reconciliation_id"] = "missing-run"
    else:
        payload["model_version"] = "exchange-reconciliation-state/v999"
    model_version = str(payload["model_version"])
    value._connection.execute(
        "UPDATE exchange_reconciliation_current SET status=?,generation=?,"
        "latest_reconciliation_id=?,model_version=?,state_payload=?,state_digest=? "
        "WHERE execution_root_id=?",
        (
            payload["status"],
            payload["generation"],
            payload["latest_reconciliation_id"],
            model_version,
            canonical_json(payload),
            canonical_sha256(payload, algorithm_version=model_version),
            ROOT,
        ),
    )
    with pytest.raises(PersistenceIntegrityError):
        value.recover_local()
    value.close()


def test_latest_run_digest_and_session_authority_corruption_fail_closed(tmp_path: Path) -> None:
    for corruption in ("digest", "session"):
        value = store(tmp_path / f"run-{corruption}.sqlite3")
        reconciler(value, FakeReader()).reconcile(ROOT)
        value._connection.commit()
        value._connection.execute("PRAGMA foreign_keys=OFF")
        value._connection.execute("DROP TRIGGER exchange_reconciliation_runs_no_update")
        if corruption == "digest":
            value._connection.execute(
                "UPDATE exchange_reconciliation_runs SET result_digest=?", ("0" * 64,)
            )
        else:
            value._connection.execute(
                "UPDATE exchange_reconciliation_runs SET session_id='foreign-session'"
            )
        with pytest.raises(PersistenceIntegrityError):
            value.recover_local()
        value.close()


def test_crash_before_final_commit_leaves_generation_unchanged(tmp_path: Path) -> None:
    def fail(boundary: str) -> None:
        if boundary == "reconciliation_before_commit":
            raise RuntimeError("crash")

    value = store(tmp_path / "crash.sqlite3", failure_injector=fail)
    with pytest.raises(RuntimeError, match="crash"):
        reconciler(value, FakeReader()).reconcile(ROOT)
    assert value.read_reconciliation_state(ROOT).generation == 0
    value.close()


@pytest.mark.parametrize(
    "boundary",
    (
        "reconciliation_after_run",
        "reconciliation_after_current",
        "reconciliation_after_control",
        "reconciliation_before_commit",
    ),
)
def test_every_final_reconciliation_write_boundary_rolls_back(
    tmp_path: Path, boundary: str
) -> None:
    path = tmp_path / f"{boundary}.sqlite3"

    def fail(value: str) -> None:
        if value == boundary:
            raise RuntimeError("injected crash")

    value = store(path, failure_injector=fail)
    with pytest.raises(RuntimeError, match="injected crash"):
        reconciler(value, FakeReader()).reconcile(ROOT)
    value.close()
    reopened = store(path)
    state = reopened.read_reconciliation_state(ROOT)
    assert state.generation == 0
    assert state.status is ExchangeReconciliationStatus.REQUIRED
    assert (
        reopened._connection.execute(
            "SELECT count(*) FROM exchange_reconciliation_runs"
        ).fetchone()[0]
        == 0
    )
    reopened.close()


def test_safe_commit_ack_loss_is_decided_by_fresh_database_state(tmp_path: Path) -> None:
    path = tmp_path / "commit-ack-loss.sqlite3"

    def fail(boundary: str) -> None:
        if boundary == "reconciliation_after_commit":
            raise RuntimeError("commit acknowledgement lost")

    value = store(path, failure_injector=fail)
    with pytest.raises(RuntimeError, match="acknowledgement"):
        reconciler(value, FakeReader()).reconcile(ROOT)
    value.close()
    reopened = store(path)
    state = reopened.read_reconciliation_state(ROOT)
    assert state.generation == 1 and state.status is ExchangeReconciliationStatus.SAFE
    assert (
        reopened._connection.execute(
            "SELECT count(*) FROM exchange_reconciliation_runs"
        ).fetchone()[0]
        == 1
    )
    next_cycle = reconciler(reopened, FakeReader(), iter(("new-cycle",))).reconcile(ROOT)
    assert next_cycle.generation_before == 1 and next_cycle.generation_after == 2
    reopened.close()


def test_five_failed_cycles_do_not_drift_generation_then_safe_advances_once(
    tmp_path: Path,
) -> None:
    value = store(tmp_path / "failed-cycles.sqlite3")
    for index in range(5):
        reader = FaultReader("recent")
        result = reconciler(value, reader, iter((f"failed-{index}",))).reconcile(ROOT)
        assert result.outcome is ExchangeReconciliationStatus.FAILED
        assert result.generation_before == result.generation_after == 0
    successful = reconciler(value, FakeReader(), iter(("successful",))).reconcile(ROOT)
    assert successful.generation_before == 0 and successful.generation_after == 1
    assert (
        value._connection.execute("SELECT count(*) FROM exchange_reconciliation_runs").fetchone()[0]
        == 6
    )
    value.close()


def test_incomplete_trade_history_horizon_blocks_safe(tmp_path: Path) -> None:
    class IncompleteHistoryReader(FakeReader):
        def read_trade_history(self, *, stream_key, start_at_ms):
            return ExchangeTradeHistory((), start_at_ms + 1, 999, False)

    value = store(tmp_path / "history-gap.sqlite3")
    result = reconciler(value, IncompleteHistoryReader()).reconcile(ROOT)
    assert result.outcome is ExchangeReconciliationStatus.REQUIRED
    assert result.generation_after == 0
    assert result.reasons == (ExchangeReconciliationReason.TRADE_HISTORY_INCOMPLETE,)
    value.close()


def test_truthful_order_a_survives_order_b_conflict_and_restart(tmp_path: Path) -> None:
    path = tmp_path / "partial-truth.sqlite3"
    second_key = "7" * 64
    second_client = "ctt1t-abcdefghijklmnopqrstuvwxy1"
    value = store(path)
    prepare(value)
    value.begin_submission(COMMAND, occurred_at_ms=110)
    value.prepare_order(
        execution_root_id=ROOT,
        session_id=SESSION,
        command=command(second_key),
        client_order_id=second_client,
        occurred_at_ms=101,
    )
    value.begin_submission(second_key, occurred_at_ms=111)
    order_a = remote(ExchangeOrderStatus.ACKNOWLEDGED)
    order_b = replace(
        order_a,
        durable_command_key=second_key,
        client_order_id=second_client,
        exchange_order_id="9002",
    )

    class MappingReader(FakeReader):
        def __init__(self, facts: dict[str, ExchangeOrder]) -> None:
            super().__init__()
            self.facts = facts
            self.open_orders = tuple(facts.values())

        def query_order(self, *, stream_key, client_order_id):
            self.calls.append(f"query:{client_order_id}")
            return OrderQueryResult(QueryResultKind.FOUND, self.facts[client_order_id])

    conflicting_b = replace(order_b, side=ExchangeOrderSide.SELL)
    first = reconciler(
        value,
        MappingReader({CLIENT: order_a, second_client: conflicting_b}),
    ).reconcile(ROOT)
    assert first.outcome is ExchangeReconciliationStatus.CONFLICT
    assert first.generation_after == 0
    assert value.load_order(COMMAND).order.status is ExchangeOrderStatus.ACKNOWLEDGED
    assert value.load_order(second_key).order.status is ExchangeOrderStatus.SUBMITTING
    value.close()

    reopened = store(path)
    second = reconciler(
        reopened,
        MappingReader({CLIENT: order_a, second_client: order_b}),
        iter(("restart-success",)),
    ).reconcile(ROOT)
    assert second.outcome is ExchangeReconciliationStatus.SAFE
    assert second.generation_before == 0 and second.generation_after == 1
    assert reopened.load_order(COMMAND).order.status is ExchangeOrderStatus.ACKNOWLEDGED
    assert reopened.load_order(second_key).order.status is ExchangeOrderStatus.ACKNOWLEDGED
    reopened.close()


def test_partial_fill_requires_open_order_and_ingests_incrementally(tmp_path: Path) -> None:
    value = store(tmp_path / "partial.sqlite3")
    prepare(value)
    value.begin_submission(COMMAND, occurred_at_ms=110)
    reader = FakeReader()
    order = remote(ExchangeOrderStatus.PARTIALLY_FILLED)
    reader.query = OrderQueryResult(QueryResultKind.FOUND, order)
    reader.trades = (trade(D("0.04")),)
    reader.recent_trades = reader.trades
    reader.open_orders = (order,)
    result = reconciler(value, reader).reconcile(ROOT)
    assert result.outcome is ExchangeReconciliationStatus.SAFE
    assert result.orders_advanced == 1 and result.trades_persisted == 1
    assert value.trade_completeness(COMMAND) is TradeCompletenessStatus.COMPLETE
    value.close()


def test_missing_expected_open_order_and_ambiguous_query_require_reconciliation(
    tmp_path: Path,
) -> None:
    value = store(tmp_path / "required.sqlite3")
    prepare(value)
    value.begin_submission(COMMAND, occurred_at_ms=110)
    reader = FakeReader()
    reader.query = OrderQueryResult(QueryResultKind.FOUND, remote(ExchangeOrderStatus.ACKNOWLEDGED))
    result = reconciler(value, reader).reconcile(ROOT)
    assert result.outcome is ExchangeReconciliationStatus.REQUIRED
    assert result.reasons == (ExchangeReconciliationReason.EXPECTED_OPEN_ORDER_MISSING,)
    reader.query = OrderQueryResult(QueryResultKind.AMBIGUOUS)
    again = reconciler(value, reader, iter(("r-ambiguous",))).reconcile(ROOT)
    assert ExchangeReconciliationReason.ORDER_STATUS_AMBIGUOUS in again.reasons
    assert again.generation_after == 0
    value.close()


@pytest.mark.parametrize(
    "changed",
    [
        {"account_fingerprint": "other-account"},
        {"side": ExchangeOrderSide.SELL},
        {"original_base_quantity": D("0.2")},
    ],
)
def test_remote_identity_conflicts_fail_closed(tmp_path: Path, changed: dict[str, object]) -> None:
    value = store(tmp_path / f"identity-{len(changed)}-{next(iter(changed))}.sqlite3")
    prepare(value)
    value.begin_submission(COMMAND, occurred_at_ms=110)
    reader = FakeReader()
    reader.query = OrderQueryResult(
        QueryResultKind.FOUND, remote(ExchangeOrderStatus.ACKNOWLEDGED, **changed)
    )
    result = reconciler(value, reader).reconcile(ROOT)
    assert result.outcome is ExchangeReconciliationStatus.CONFLICT
    assert ExchangeReconciliationReason.ORDER_FACT_CONFLICT in result.reasons
    value.close()


def test_trade_incomplete_and_conflicting_duplicate_fail_closed(tmp_path: Path) -> None:
    value = store(tmp_path / "trade-conflict.sqlite3")
    prepare(value)
    value.begin_submission(COMMAND, occurred_at_ms=110)
    reader = FakeReader()
    filled = remote(ExchangeOrderStatus.FILLED)
    reader.query = OrderQueryResult(QueryResultKind.FOUND, filled)
    reader.trades = (trade(D("0.04")),)
    reader.recent_trades = reader.trades
    incomplete = reconciler(value, reader).reconcile(ROOT)
    assert incomplete.outcome is ExchangeReconciliationStatus.REQUIRED
    assert ExchangeReconciliationReason.TRADE_INCOMPLETE in incomplete.reasons
    reader.trades = (trade(D("0.04")), trade(D("0.07"), "trade-2"))
    reader.recent_trades = reader.trades
    conflict = reconciler(value, reader, iter(("r-conflict",))).reconcile(ROOT)
    assert conflict.outcome is ExchangeReconciliationStatus.CONFLICT
    assert ExchangeReconciliationReason.TRADE_FACT_CONFLICT in conflict.reasons
    value.close()


def test_incremental_trade_completion_persists_only_missing_identity(tmp_path: Path) -> None:
    value = store(tmp_path / "incremental-trades.sqlite3")
    prepare(value)
    value.begin_submission(COMMAND, occurred_at_ms=110)
    partial = remote(
        ExchangeOrderStatus.PARTIALLY_FILLED,
        cumulative_executed_base_quantity=D("0.06"),
        cumulative_quote_quantity=D("6000"),
    )
    value.record_status(
        COMMAND,
        status=partial.status,
        occurred_at_ms=120,
        reason_code="partial-fixture",
        exchange_order_id=partial.exchange_order_id,
        acknowledged_at_ms=partial.acknowledged_at_ms,
        cumulative_executed_base_quantity=partial.cumulative_executed_base_quantity,
        cumulative_quote_quantity=partial.cumulative_quote_quantity,
    )
    first = trade(D("0.025"), "trade-1")
    second = trade(D("0.035"), "trade-2")
    missing = trade(D("0.04"), "trade-3")
    value.persist_trades(COMMAND, (first, second))
    reader = FakeReader()
    reader.query = OrderQueryResult(QueryResultKind.FOUND, remote(ExchangeOrderStatus.FILLED))
    reader.trades = (first, second, missing)
    reader.recent_trades = reader.trades
    result = reconciler(value, reader).reconcile(ROOT)
    assert result.outcome is ExchangeReconciliationStatus.SAFE
    assert result.trades_persisted == 1
    assert value.trade_completeness(COMMAND) is TradeCompletenessStatus.COMPLETE
    assert value._connection.execute("SELECT count(*) FROM exchange_trades").fetchone()[0] == 3
    replay = reconciler(value, reader, iter(("trade-replay",))).reconcile(ROOT)
    assert replay.outcome is ExchangeReconciliationStatus.SAFE
    assert replay.trades_persisted == 0
    assert value._connection.execute("SELECT count(*) FROM exchange_trades").fetchone()[0] == 3
    value.close()


def test_partial_cumulative_base_and_quote_regressions_conflict_exactly(tmp_path: Path) -> None:
    for field, lower in (
        ("cumulative_executed_base_quantity", D("0.03999")),
        ("cumulative_quote_quantity", D("3999.99999")),
    ):
        value = store(tmp_path / f"regression-{field}.sqlite3")
        prepare(value)
        value.begin_submission(COMMAND, occurred_at_ms=110)
        partial = remote(ExchangeOrderStatus.PARTIALLY_FILLED)
        value.record_status(
            COMMAND,
            status=partial.status,
            occurred_at_ms=120,
            reason_code="partial-fixture",
            exchange_order_id=partial.exchange_order_id,
            acknowledged_at_ms=partial.acknowledged_at_ms,
            cumulative_executed_base_quantity=partial.cumulative_executed_base_quantity,
            cumulative_quote_quantity=partial.cumulative_quote_quantity,
        )
        reader = FakeReader()
        reader.query = OrderQueryResult(QueryResultKind.FOUND, replace(partial, **{field: lower}))
        reader.open_orders = (partial,)
        result = reconciler(value, reader).reconcile(ROOT)
        assert result.outcome is ExchangeReconciliationStatus.CONFLICT
        assert result.generation_after == 0
        assert ExchangeReconciliationReason.ORDER_FACT_CONFLICT in result.reasons
        value.close()


class FaultReader(FakeReader):
    def __init__(self, operation: str) -> None:
        super().__init__()
        self.operation = operation

    def list_open_orders(self, *, stream_key: MarketStreamKey) -> tuple[ExchangeOrder, ...]:
        if self.operation == "open":
            raise RuntimeError("open failed")
        return super().list_open_orders(stream_key=stream_key)

    def read_trade_history(
        self, *, stream_key: MarketStreamKey, start_at_ms: int
    ) -> ExchangeTradeHistory:
        if self.operation == "recent":
            raise RuntimeError("recent failed")
        return super().read_trade_history(stream_key=stream_key, start_at_ms=start_at_ms)

    def read_balances(self, assets: tuple[str, ...]) -> tuple[AccountBalance, ...]:
        if self.operation == "balances":
            raise RuntimeError("balances failed")
        if self.operation == "balance-scope":
            return (AccountBalance("BTC", D(0), D(0)),)
        return super().read_balances(assets)

    def read_instrument_rules(self, stream_key: MarketStreamKey) -> ExchangeInstrumentRules:
        if self.operation == "rules":
            raise RuntimeError("rules failed")
        return super().read_instrument_rules(stream_key)


@pytest.mark.parametrize(
    ("operation", "reason", "outcome"),
    [
        (
            "open",
            ExchangeReconciliationReason.ORDER_QUERY_FAILED,
            ExchangeReconciliationStatus.REQUIRED,
        ),
        (
            "recent",
            ExchangeReconciliationReason.TRADE_HISTORY_FAILED,
            ExchangeReconciliationStatus.FAILED,
        ),
        (
            "balances",
            ExchangeReconciliationReason.BALANCE_QUERY_FAILED,
            ExchangeReconciliationStatus.REQUIRED,
        ),
        (
            "balance-scope",
            ExchangeReconciliationReason.BALANCE_SCOPE_INVALID,
            ExchangeReconciliationStatus.CONFLICT,
        ),
        (
            "rules",
            ExchangeReconciliationReason.INSTRUMENT_RULES_QUERY_FAILED,
            ExchangeReconciliationStatus.FAILED,
        ),
    ],
)
def test_reader_boundary_failures_are_typed(tmp_path: Path, operation, reason, outcome) -> None:
    value = store(tmp_path / f"reader-{operation}.sqlite3")
    result = reconciler(value, FaultReader(operation)).reconcile(ROOT)
    assert result.outcome is outcome
    assert reason in result.reasons
    value.close()


def test_duplicate_open_order_handling_is_deterministic(tmp_path: Path) -> None:
    value = store(tmp_path / "duplicates.sqlite3")
    prepare(value)
    value.begin_submission(COMMAND, occurred_at_ms=110)
    reader = FakeReader()
    acknowledged = remote(ExchangeOrderStatus.ACKNOWLEDGED)
    reader.query = OrderQueryResult(QueryResultKind.FOUND, acknowledged)
    reader.open_orders = (acknowledged, acknowledged)
    exact = reconciler(value, reader).reconcile(ROOT)
    assert exact.outcome is ExchangeReconciliationStatus.SAFE
    reader.open_orders = (
        acknowledged,
        replace(acknowledged, latest_exchange_update_ms=999),
    )
    conflicting = reconciler(value, reader, iter(("r-duplicate",))).reconcile(ROOT)
    assert conflicting.outcome is ExchangeReconciliationStatus.CONFLICT
    assert ExchangeReconciliationReason.DUPLICATE_OPEN_ORDER in conflicting.reasons
    value.close()


def test_simultaneous_reason_order_is_stable_and_unique(tmp_path: Path) -> None:
    reason_results = []
    for ordinal in range(2):
        value = store(tmp_path / f"reason-order-{ordinal}.sqlite3")
        prepare(value)
        value.begin_submission(COMMAND, occurred_at_ms=110)
        reader = FakeReader()
        unknown_order = remote(
            ExchangeOrderStatus.ACKNOWLEDGED,
            durable_command_key="8" * 64,
            client_order_id="ctt1t-abcdefghijklmnopqrstuvwxy1",
            exchange_order_id="unknown-order",
        )
        unknown_trade = replace(
            trade(), identity=replace(trade().identity, exchange_order_id="unknown-order")
        )
        reader.open_orders = (unknown_order,)
        reader.recent_trades = (unknown_trade,)
        reader.rules = replace(RULES, min_notional=D(6))
        result = reconciler(value, reader).reconcile(ROOT)
        assert result.outcome is ExchangeReconciliationStatus.CONFLICT
        assert result.reasons == tuple(sorted(set(result.reasons), key=lambda reason: reason.value))
        reason_results.append(result.reasons)
        value.close()
    assert reason_results[0] == reason_results[1]


def test_terminal_order_shown_open_conflicts(tmp_path: Path) -> None:
    value = store(tmp_path / "terminal-open.sqlite3")
    prepare(value)
    value.begin_submission(COMMAND, occurred_at_ms=110)
    filled = remote(ExchangeOrderStatus.FILLED)
    value.record_status(
        COMMAND,
        status=filled.status,
        occurred_at_ms=130,
        reason_code="test",
        exchange_order_id=filled.exchange_order_id,
        cumulative_executed_base_quantity=filled.cumulative_executed_base_quantity,
        cumulative_quote_quantity=filled.cumulative_quote_quantity,
    )
    value.persist_trades(COMMAND, (trade(),))
    reader = FakeReader()
    reader.open_orders = (filled,)
    reader.recent_trades = (trade(),)
    result = reconciler(value, reader).reconcile(ROOT)
    assert result.orders_examined == 0
    assert result.outcome is ExchangeReconciliationStatus.CONFLICT
    assert ExchangeReconciliationReason.TERMINAL_ORDER_SHOWN_OPEN in result.reasons
    value.close()


def test_persistence_exact_replay_is_idempotent_and_conflicting_replay_fails(
    tmp_path: Path,
) -> None:
    value = store(tmp_path / "replay.sqlite3")
    result = reconciler(value, FakeReader()).reconcile(ROOT)
    record = PersistentExchangeReconciliationRun(
        result.reconciliation_id,
        ROOT,
        SESSION,
        "account-m47",
        0,
        1,
        ExchangeReconciliationStatus.SAFE,
        0,
        0,
        0,
        0,
        2,
        (),
        result.started_at_ms,
        result.completed_at_ms,
    )
    assert value.record_reconciliation_run(record).generation == 1
    with pytest.raises(PersistenceConflictError, match="conflicting"):
        value.record_reconciliation_run(replace(record, balances_examined=1))
    value.close()


def test_reconciliation_persistence_stale_lineage_begin_and_closed_fail_closed(
    tmp_path: Path,
) -> None:
    value = store(tmp_path / "persistence-errors.sqlite3")
    result = reconciler(value, FakeReader()).reconcile(ROOT)
    stale = PersistentExchangeReconciliationRun(
        "stale-run",
        ROOT,
        SESSION,
        "account-m47",
        0,
        1,
        ExchangeReconciliationStatus.SAFE,
        0,
        0,
        0,
        0,
        2,
        (),
        result.completed_at_ms + 1,
        result.completed_at_ms + 1,
    )
    with pytest.raises(PersistenceConflictError, match="stale"):
        value.record_reconciliation_run(stale)
    wrong_lineage = PersistentExchangeReconciliationRun(
        "wrong-lineage",
        ROOT,
        "wrong-session",
        "account-m47",
        1,
        1,
        ExchangeReconciliationStatus.FAILED,
        0,
        0,
        0,
        0,
        0,
        (ExchangeReconciliationReason.ORDER_QUERY_FAILED.value,),
        result.completed_at_ms + 2,
        result.completed_at_ms + 2,
    )
    with pytest.raises(PersistenceConflictError, match="lineage"):
        value.record_reconciliation_run(wrong_lineage)
    value._connection.execute("BEGIN IMMEDIATE")
    valid_failure = replace(wrong_lineage, reconciliation_id="begin-failure", session_id=SESSION)
    with pytest.raises(PersistenceTransactionError, match="transaction"):
        value.record_reconciliation_run(valid_failure)
    value.close()
    with pytest.raises(PersistenceConnectionError, match="closed"):
        value.read_reconciliation_state(ROOT)


@pytest.mark.parametrize(
    ("persistence", "reader", "kwargs"),
    [
        (object(), FakeReader(), {}),
        (None, object(), {}),
        (None, FakeReader(), {"expected_rules": object()}),
    ],
)
def test_reconciler_rejects_invalid_dependencies(
    tmp_path: Path, persistence, reader, kwargs
) -> None:
    value = store(tmp_path / "constructor.sqlite3")
    actual = value if persistence is None else persistence
    with pytest.raises(TypeError):
        ExchangeReconciler(
            actual,
            reader,
            expected_rules=kwargs.get("expected_rules", RULES),
            timestamp_provider=lambda: 1,
            reconciliation_id_factory=lambda: "id",
        )
    value.close()


def test_reconciler_rejects_invalid_factories_assets_and_outputs(tmp_path: Path) -> None:
    value = store(tmp_path / "invalid-inputs.sqlite3")
    for timestamp, factory, assets in (
        (None, lambda: "id", ("BTC", "USDT")),
        (lambda: 1, None, ("BTC", "USDT")),
        (lambda: 1, lambda: "id", ()),
        (lambda: 1, lambda: "id", ("BTC", "BTC")),
    ):
        with pytest.raises(TypeError):
            ExchangeReconciler(
                value,
                FakeReader(),
                expected_rules=RULES,
                timestamp_provider=timestamp,  # type: ignore[arg-type]
                reconciliation_id_factory=factory,  # type: ignore[arg-type]
                balance_assets=assets,
            )
    for timestamp, factory in ((lambda: -1, lambda: "id"), (lambda: 1, lambda: "")):
        service = ExchangeReconciler(
            value,
            FakeReader(),
            expected_rules=RULES,
            timestamp_provider=timestamp,
            reconciliation_id_factory=factory,
        )
        with pytest.raises(TypeError):
            service.reconcile(ROOT)
    value.close()


def test_reconciliation_result_cross_field_invariants(tmp_path: Path) -> None:
    value = store(tmp_path / "result-invariants.sqlite3")
    result = reconciler(value, FakeReader()).reconcile(ROOT)
    assert result.submission_ready is False
    invalid = (
        {"generation_after": result.generation_before},
        {"reasons": (ExchangeReconciliationReason.UNKNOWN_TRADE,)},
        {
            "outcome": ExchangeReconciliationStatus.FAILED,
            "generation_after": result.generation_after,
            "reasons": (ExchangeReconciliationReason.ORDER_QUERY_FAILED,),
        },
        {"orders_examined": -1},
        {"orders_advanced": result.orders_examined + 1},
        {"completed_at_ms": result.started_at_ms - 1},
        {"reconciliation_id": ""},
    )
    for changes in invalid:
        with pytest.raises((TypeError, ValueError)):
            replace(result, **changes)
    with pytest.raises((TypeError, ValueError)):
        ExchangeReconciliationResult(
            "id",
            ROOT,
            SESSION,
            "account-m47",
            0,
            0,
            "SAFE",  # type: ignore[arg-type]
            0,
            0,
            0,
            0,
            0,
            (),
            1,
            1,
        )
    value.close()


def test_status_regression_and_trade_reader_failure_are_typed(tmp_path: Path) -> None:
    value = store(tmp_path / "fact-failures.sqlite3")
    prepare(value)
    value.begin_submission(COMMAND, occurred_at_ms=110)
    partial = remote(ExchangeOrderStatus.PARTIALLY_FILLED)
    value.record_status(
        COMMAND,
        status=partial.status,
        occurred_at_ms=120,
        reason_code="test-partial",
        exchange_order_id=partial.exchange_order_id,
        acknowledged_at_ms=partial.acknowledged_at_ms,
        cumulative_executed_base_quantity=partial.cumulative_executed_base_quantity,
        cumulative_quote_quantity=partial.cumulative_quote_quantity,
    )
    reader = FakeReader()
    reader.query = OrderQueryResult(QueryResultKind.FOUND, remote(ExchangeOrderStatus.ACKNOWLEDGED))
    reader.open_orders = (partial,)
    regression = reconciler(value, reader).reconcile(ROOT)
    assert regression.outcome is ExchangeReconciliationStatus.CONFLICT
    assert ExchangeReconciliationReason.ORDER_FACT_CONFLICT in regression.reasons

    class TradeFailureReader(FakeReader):
        def list_order_trades(self, *, stream_key, exchange_order_id):
            raise RuntimeError("trade query failed")

    trade_reader = TradeFailureReader()
    trade_reader.query = OrderQueryResult(
        QueryResultKind.FOUND,
        remote(
            ExchangeOrderStatus.PARTIALLY_FILLED,
            cumulative_executed_base_quantity=D("0.05"),
            cumulative_quote_quantity=D("5000"),
        ),
    )
    assert trade_reader.query.order is not None
    trade_reader.open_orders = (trade_reader.query.order,)
    failed = reconciler(value, trade_reader, iter(("r-trade-failure",))).reconcile(ROOT)
    assert failed.outcome is ExchangeReconciliationStatus.FAILED
    assert ExchangeReconciliationReason.TRADE_QUERY_FAILED in failed.reasons
    value.close()


def test_open_order_identity_conflict_is_detected(tmp_path: Path) -> None:
    value = store(tmp_path / "open-identity.sqlite3")
    prepare(value)
    value.begin_submission(COMMAND, occurred_at_ms=110)
    reader = FakeReader()
    acknowledged = remote(ExchangeOrderStatus.ACKNOWLEDGED)
    reader.query = OrderQueryResult(QueryResultKind.FOUND, acknowledged)
    reader.open_orders = (replace(acknowledged, side=ExchangeOrderSide.SELL),)
    result = reconciler(value, reader).reconcile(ROOT)
    assert result.outcome is ExchangeReconciliationStatus.CONFLICT
    assert ExchangeReconciliationReason.ORDER_FACT_CONFLICT in result.reasons
    value.close()


def test_reconciliation_performance_sanity_500_orders_2000_trades(tmp_path: Path) -> None:
    value = store(tmp_path / "performance.sqlite3")
    factory = DeterministicClientOrderIdFactory()
    all_trades: list[ExchangeTrade] = []
    for index in range(500):
        durable_key = f"{index + 1:064x}"
        client_id = factory.create_client_order_id(
            environment=ExecutionEnvironment.TESTNET,
            venue="binance-spot",
            account_fingerprint="account-m47",
            durable_command_key=durable_key,
        )
        value.prepare_order(
            execution_root_id=ROOT,
            session_id=SESSION,
            command=command(durable_key),
            client_order_id=client_id,
            occurred_at_ms=100 + index,
        )
        value.begin_submission(durable_key, occurred_at_ms=1_000 + index)
        exchange_order_id = str(10_000_000_000_000_000_000 + index)
        value.record_status(
            durable_key,
            status=ExchangeOrderStatus.FILLED,
            occurred_at_ms=2_000 + index,
            reason_code="performance-fixture",
            exchange_order_id=exchange_order_id,
            acknowledged_at_ms=1_000 + index,
            cumulative_executed_base_quantity=D("0.1"),
            cumulative_quote_quantity=D("10000"),
        )
        order_trades = tuple(
            ExchangeTrade(
                ExchangeTradeIdentity(
                    "binance-spot",
                    ExecutionEnvironment.TESTNET,
                    "account-m47",
                    "BTCUSDT",
                    exchange_order_id,
                    f"{index}-{ordinal}",
                ),
                client_id,
                KEY,
                ExchangeOrderSide.BUY,
                D("0.025"),
                D("100000"),
                D("2500"),
                D("0.00001"),
                "BNB",
                3_000 + index,
            )
            for ordinal in range(4)
        )
        value.persist_trades(durable_key, order_trades)
        all_trades.extend(order_trades)
    reader = FakeReader()
    reader.recent_trades = tuple(reversed(all_trades))
    started = time.perf_counter()
    result = reconciler(value, reader, iter(("performance-run",))).reconcile(ROOT)
    elapsed = time.perf_counter() - started
    assert result.outcome is ExchangeReconciliationStatus.SAFE
    assert result.orders_examined == 0 and result.generation_after == 1
    assert elapsed < 10
    value.close()
