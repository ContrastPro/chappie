import sqlite3
from contextlib import closing
from dataclasses import replace
from decimal import Decimal, getcontext
from hashlib import sha256
from pathlib import Path

import pytest

from app.market.models import Candle, CandleSnapshot, MarketStreamKey
from app.persistence.canonical import (
    canonical_json,
    canonical_sequence,
    canonical_sha256,
    deserialize_decimal,
    serialize_decimal,
)
from app.persistence.exceptions import (
    PersistenceConflictError,
    PersistenceConnectionError,
    PersistenceDecisionSlotConflictError,
    PersistenceIntegrityError,
    PersistenceMigrationError,
    PersistenceSchemaError,
    PersistenceSerializationError,
    PersistenceTransactionError,
    PersistenceWriterConflictError,
)
from app.persistence.keys import (
    DurableCommandKeyInput,
    EmaStrategyFingerprintInput,
    InstrumentRulesFingerprintInput,
    PaperExecutionFingerprintInput,
    PlannerFingerprintInput,
    RiskPolicyFingerprintInput,
    SnapshotPolicyFingerprintInput,
    StableDecisionKeyInput,
    decision_slot_key,
    durable_command_key,
    fingerprint_configuration,
    fingerprint_ema_strategy,
    fingerprint_instrument_rules,
    fingerprint_paper_execution,
    fingerprint_planner,
    fingerprint_risk_policy,
    fingerprint_snapshot_policy,
    fingerprint_strategy_input,
    stable_decision_key,
)
from app.persistence.models import (
    CycleOutcome,
    InitialPortfolioRecord,
    PersistentCommandRecord,
    PersistentDecisionRecord,
    PersistentFillRecord,
    PortfolioState,
    PortfolioTransitionRecord,
    PositionState,
    RecoveryResult,
    RecoveryStatus,
    SQLitePersistenceConfig,
)
from app.persistence.sqlite import SQLiteTradingPersistence

KEY = MarketStreamKey("BTCUSDT", "1m")
DECISION = "d" * 64
SLOT = "b" * 64
COMMAND = "c" * 64
PAYLOAD_DIGEST = sha256(b"{}").hexdigest()
CONFIG_FINGERPRINT = "9" * 64


def candle(index: int = 0, close: str = "100") -> CandleSnapshot:
    open_ms = 1_700_000_000_000 + index * 60_000
    return CandleSnapshot.from_candle(
        Candle(
            open_time_ms=open_ms,
            close_time_ms=open_ms + 59_999,
            open=Decimal(100),
            high=Decimal(101),
            low=Decimal(99),
            close=Decimal(close),
            volume=Decimal(10),
        )
    )


def state(version: int = 1, *, quote: str = "1000", as_of_ms: int = 1) -> PortfolioState:
    equity = Decimal(quote)
    return PortfolioState(
        portfolio_id="paper-main",
        stream_key=KEY,
        base_asset="BTC",
        quote_asset="USDT",
        quote_balance=equity,
        base_balance=Decimal(0),
        position=None,
        realised_pnl=Decimal(0),
        unrealised_pnl=Decimal(0),
        total_fees=Decimal(0),
        initial_equity=Decimal(1000),
        turnover=Decimal(0),
        trade_count=0,
        applied_fill_count=0,
        peak_equity=Decimal(1000),
        current_equity=equity,
        drawdown=Decimal(1000) - equity,
        max_drawdown=Decimal(1000) - equity,
        mark_price=None,
        as_of_ms=as_of_ms,
        version=version,
    )


def initial_state() -> PortfolioState:
    return state(0, as_of_ms=0)


def initialize(adapter: SQLiteTradingPersistence) -> None:
    assert adapter.initialize_portfolio(
        InitialPortfolioRecord(initial_state(), CONFIG_FINGERPRINT, 0)
    )


def test_exclusive_runtime_writer_rejects_second_instance(tmp_path: Path) -> None:
    database = tmp_path / "exclusive.sqlite3"
    first = SQLiteTradingPersistence(
        SQLitePersistenceConfig(database, busy_timeout_ms=1, exclusive_writer=True)
    )
    with pytest.raises(PersistenceWriterConflictError):
        SQLiteTradingPersistence(
            SQLitePersistenceConfig(database, busy_timeout_ms=1, exclusive_writer=True)
        )
    first.close()
    reopened = SQLiteTradingPersistence(
        SQLitePersistenceConfig(database, busy_timeout_ms=1, exclusive_writer=True)
    )
    reopened.close()


def test_runtime_checkpoint_and_projection_rebuild_are_explicit() -> None:
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(":memory:"))
    seq = commit_valuation(adapter)
    assert adapter.create_checkpoint(state(), seq) is True
    assert adapter.create_checkpoint(state(), seq) is False
    assert adapter.status.latest_checkpoint_version == 1
    assert adapter.create_checkpoint(initial_state(), 0) is False
    with adapter.begin_cycle() as transaction:
        with pytest.raises(PersistenceTransactionError, match="checkpoint"):
            adapter.create_checkpoint(state(), seq)
        transaction.rollback()
    with pytest.raises(PersistenceConflictError, match="current"):
        adapter.create_checkpoint(state(1, quote="999"), seq)

    adapter._connection.execute("DELETE FROM portfolio_current WHERE portfolio_id='paper-main'")
    adapter.rebuild_portfolio_current(state(), seq)
    assert adapter.load_current("paper-main") == state()
    with pytest.raises(PersistenceConflictError, match="never auto-repaired"):
        adapter.rebuild_portfolio_current(state(), seq)
    adapter._connection.execute("DELETE FROM portfolio_current WHERE portfolio_id='paper-main'")
    with pytest.raises(PersistenceConflictError, match="journal head"):
        adapter.rebuild_portfolio_current(state(), seq + 1)
    adapter.close()


def decision_record(*, key: str = DECISION, slot: str = SLOT, payload: str = "{}"):
    return PersistentDecisionRecord(key, slot, payload, sha256(payload.encode()).hexdigest(), 1)


def command_record(*, key: str = COMMAND, decision: str = DECISION, payload: str = "{}"):
    return PersistentCommandRecord(key, decision, payload, sha256(payload.encode()).hexdigest())


def fill_record(*, fill_id: str = "fill-1", command: str = COMMAND):
    return PersistentFillRecord(
        fill_id=fill_id,
        durable_command_key=command,
        order_id="order-1",
        risk_decision_id="risk-1",
        intent_id="intent-1",
        stream_key=KEY,
        side="BUY",
        quantity=Decimal("0.1"),
        price=Decimal(100),
        gross_notional=Decimal(10),
        fee=Decimal("0.01"),
        fee_asset="USDT",
        net_quote_change=Decimal("-10.01"),
        net_base_change=Decimal("0.1"),
        filled_at_ms=1,
    )


def transition(after: PortfolioState | None = None, *, before_digest: str | None = None):
    result = after or state()
    return PortfolioTransitionRecord(
        portfolio_id=result.portfolio_id,
        transition_type="VALUATION",
        version_before=result.version - 1,
        version_after=result.version,
        before_digest=before_digest or initial_state().digest,
        after_state=result,
        domain_timestamp_ms=result.as_of_ms,
        stable_decision_key=DECISION,
    )


def commit_valuation(adapter: SQLiteTradingPersistence, after: PortfolioState | None = None):
    result = after or state()
    if adapter.load_initial(result.portfolio_id) is None:
        initialize(adapter)
    with adapter.begin_cycle() as tx:
        tx.claim_decision(
            decision_record(
                key=DECISION if result.version == 1 else "e" * 64,
                slot=SLOT if result.version == 1 else "f" * 64,
            )
        )
        record = transition(
            result,
            before_digest=initial_state().digest
            if result.version == 1
            else state(result.version - 1).digest,
        )
        if result.version > 1:
            record = replace(record, stable_decision_key="e" * 64)
        seq = tx.record_portfolio_transition(record)
        tx.set_portfolio_current(result, seq)
        tx.mark_terminal(record.stable_decision_key, CycleOutcome.HOLD)
        tx.commit()
    return seq


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal(0), "0"),
        (Decimal("-0"), "0"),
        (Decimal(1), "1"),
        (Decimal("1.00"), "1"),
        (Decimal("0.00000001"), "0.00000001"),
        (Decimal("1E-30"), "0." + "0" * 29 + "1"),
        (Decimal("1E+30"), "1" + "0" * 30),
        (Decimal("123456789.123456789123456789"), "123456789.123456789123456789"),
    ],
)
def test_decimal_canonical_round_trip(value: Decimal, expected: str) -> None:
    original_precision = getcontext().prec
    getcontext().prec = 2
    try:
        assert serialize_decimal(value) == expected
        assert deserialize_decimal(expected) == value
    finally:
        getcontext().prec = original_precision


@pytest.mark.parametrize("value", [1.0, Decimal("NaN"), Decimal("Infinity"), "1.0"])
def test_decimal_rejects_non_decimal_or_non_finite(value: object) -> None:
    with pytest.raises(PersistenceSerializationError):
        serialize_decimal(value)  # type: ignore[arg-type]


def test_canonical_json_and_fingerprint_are_semantic_and_ordered() -> None:
    left = {"b": Decimal("1.0"), "a": [1, 2]}
    right = {"a": [1, 2], "b": Decimal("1.00")}
    assert canonical_json(left) == canonical_json(right)
    assert canonical_sha256(left, algorithm_version="v1") == canonical_sha256(
        right, algorithm_version="v1"
    )
    assert canonical_sha256(left, algorithm_version="v1") != canonical_sha256(
        {"a": [2, 1], "b": Decimal(1)}, algorithm_version="v1"
    )
    with pytest.raises(PersistenceSerializationError):
        canonical_json({"unknown": object()})


def key_input(*, strategy_input: str | None = None) -> StableDecisionKeyInput:
    candles = (candle(0), candle(1))
    config = fingerprint_configuration("strategy", {"fast": 12, "slow": 26})
    return StableDecisionKeyInput(
        exchange="binance-spot",
        symbol="BTCUSDT",
        interval="1m",
        latest_closed_candle=candles[-1],
        strategy_input_fingerprint=strategy_input or fingerprint_strategy_input(candles),
        strategy_name="ema",
        strategy_version="1",
        strategy_config_fingerprint=config,
        planner_name="target",
        planner_version="1",
        planner_config_fingerprint=fingerprint_configuration("planner", {"target": Decimal(1)}),
        snapshot_policy_fingerprint=fingerprint_configuration("snapshot", {"stale_ms": 60_000}),
    )


def test_strategy_and_decision_keys_detect_data_but_slot_does_not() -> None:
    first = key_input()
    changed_fingerprint = fingerprint_strategy_input((candle(0, "99"), candle(1)))
    changed = key_input(strategy_input=changed_fingerprint)
    assert stable_decision_key(first) != stable_decision_key(changed)
    assert decision_slot_key(first) == decision_slot_key(changed)
    assert fingerprint_strategy_input((candle(0), candle(1))) != fingerprint_strategy_input(
        (candle(1), candle(0))
    )


def test_durable_command_key_is_deterministic_and_sensitive() -> None:
    value = DurableCommandKeyInput(
        stable_decision_key=stable_decision_key(key_input()),
        intent_action="enter_long",
        requested_target_exposure=Decimal("1.0"),
        approved_side="BUY",
        approved_quantity=Decimal("0.1"),
        approved_target_exposure=Decimal(1),
        reference_price=Decimal(100),
        worst_case_price=Decimal(101),
        risk_outcome="approved",
        modification_reasons=(),
        risk_policy_fingerprint="r" * 64,
        instrument_rules_fingerprint="i" * 64,
        execution_config_fingerprint="x" * 64,
    )
    assert durable_command_key(value) == durable_command_key(
        replace(value, requested_target_exposure=Decimal("1.00"))
    )
    assert durable_command_key(value) != durable_command_key(
        replace(value, approved_quantity=Decimal("0.2"))
    )


def test_new_database_migrates_reopens_and_has_safe_pragmas(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(path))
    assert adapter.status.schema_version == 1
    assert adapter.status.journal_mode.lower() == "wal"
    assert adapter.status.synchronous_mode == 2
    assert adapter.status.foreign_keys
    adapter.close()
    reopened = SQLiteTradingPersistence(SQLitePersistenceConfig(path))
    assert reopened._connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 1
    reopened.close()


def test_migration_checksum_mismatch_fails(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(path))
    adapter._connection.execute("UPDATE schema_migrations SET checksum=?", ("f" * 64,))
    adapter.close()
    with pytest.raises(PersistenceMigrationError):
        SQLiteTradingPersistence(SQLitePersistenceConfig(path))


def test_failed_migration_rolls_back_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "failed.sqlite3"
    monkeypatch.setattr(
        SQLiteTradingPersistence,
        "_migration",
        staticmethod(lambda: ("CREATE TABLE partial(id INTEGER); BROKEN SQL;", "a" * 64)),
    )
    with pytest.raises(PersistenceMigrationError):
        SQLiteTradingPersistence(SQLitePersistenceConfig(path))
    with closing(sqlite3.connect(path)) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE name='partial'"
            ).fetchone()[0]
            == 0
        )


def test_transaction_rolls_back_at_each_uncommitted_boundary(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(path))
    with adapter.begin_cycle() as tx:
        tx.claim_decision(decision_record())
    assert adapter._connection.execute("SELECT count(*) FROM paper_decisions").fetchone()[0] == 0

    with pytest.raises(RuntimeError), adapter.begin_cycle() as tx:
        tx.claim_decision(decision_record())
        tx.record_command(command_record())
        tx.record_fill(fill_record())
        raise RuntimeError("crash")
    adapter.close()
    fresh = SQLiteTradingPersistence(SQLitePersistenceConfig(path))
    for table in ("paper_decisions", "paper_commands", "fills", "journal_entries"):
        assert fresh._connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    fresh.close()


def test_commit_is_atomic_and_durable_across_restart(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(path))
    commit_valuation(adapter)
    adapter.close()
    fresh = SQLiteTradingPersistence(SQLitePersistenceConfig(path))
    assert fresh.load_current("paper-main") == state()
    assert fresh.recover("paper-main").status is RecoveryStatus.RECOVERED
    assert (
        fresh._connection.execute("SELECT terminal_outcome FROM paper_decisions").fetchone()[0]
        == "HOLD"
    )
    fresh.close()


def test_duplicate_decision_and_slot_conflicts() -> None:
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(":memory:"))
    with adapter.begin_cycle() as tx:
        assert tx.claim_decision(decision_record())
        assert not tx.claim_decision(decision_record())
        with pytest.raises(PersistenceConflictError):
            tx.claim_decision(decision_record(payload='{"changed":true}'))
        with pytest.raises(PersistenceConflictError):
            tx.claim_decision(decision_record(key="e" * 64))
    adapter.close()


def test_one_fill_per_command_is_enforced_by_sqlite() -> None:
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(":memory:"))
    with adapter.begin_cycle() as tx:
        tx.claim_decision(decision_record())
        tx.record_command(command_record())
        assert tx.record_fill(fill_record())
        assert not tx.record_fill(fill_record())
        with pytest.raises(PersistenceConflictError):
            tx.record_fill(fill_record(fill_id="fill-2"))
    adapter.close()


def test_fill_round_trip_is_lossless() -> None:
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(":memory:"))
    initialize(adapter)
    expected = fill_record()
    with adapter.begin_cycle() as tx:
        tx.claim_decision(decision_record())
        tx.record_command(command_record())
        tx.record_fill(expected)
        seq = tx.record_portfolio_transition(
            replace(transition(), durable_command_key=COMMAND, fill_id=expected.fill_id)
        )
        tx.set_portfolio_current(state(), seq)
        tx.mark_terminal(DECISION, CycleOutcome.EXECUTED)
        tx.commit()
    assert adapter.load_fill(COMMAND) == expected
    assert adapter._connection.execute("SELECT typeof(quantity) FROM fills").fetchone()[0] == "text"
    adapter.close()


def test_nested_transaction_is_rejected() -> None:
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(":memory:"))
    with adapter.begin_cycle(), pytest.raises(PersistenceTransactionError):
        adapter.begin_cycle()
    adapter.close()


def test_two_connections_enforce_single_writer(tmp_path: Path) -> None:
    config = SQLitePersistenceConfig(tmp_path / "paper.sqlite3", busy_timeout_ms=1)
    first = SQLiteTradingPersistence(config)
    second = SQLiteTradingPersistence(config)
    with first.begin_cycle(), pytest.raises(PersistenceWriterConflictError):
        second.begin_cycle()
    with second.begin_cycle() as tx:
        tx.rollback()
    first.close()
    second.close()


def test_checkpoint_is_append_only_and_backup_recovers(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    backup = tmp_path / "backup.sqlite3"
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(source))
    initialize(adapter)
    result = state()
    with adapter.begin_cycle() as tx:
        tx.claim_decision(decision_record())
        seq = tx.record_portfolio_transition(transition(result))
        tx.set_portfolio_current(result, seq)
        assert tx.create_checkpoint(result, seq)
        assert not tx.create_checkpoint(result, seq)
        tx.mark_terminal(DECISION, CycleOutcome.HOLD)
        tx.commit()
    adapter.create_backup(backup)
    adapter.close()
    copied = SQLiteTradingPersistence(SQLitePersistenceConfig(backup))
    recovered = copied.recover("paper-main")
    assert copied.integrity_check() == ("ok",)
    assert recovered.status is RecoveryStatus.CLEAN
    assert recovered.portfolio_state == result
    copied.close()


def test_recovery_fails_closed_on_wrong_current_state() -> None:
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(":memory:"))
    commit_valuation(adapter)
    adapter._connection.execute(
        "UPDATE portfolio_current SET state_digest=? WHERE portfolio_id=?",
        ("f" * 64, "paper-main"),
    )
    result = adapter.recover("paper-main")
    assert result.status is RecoveryStatus.RECOVERY_REQUIRED
    assert result.portfolio_state is None
    adapter.close()


def test_recovery_uses_previous_checkpoint_when_latest_is_corrupt() -> None:
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(":memory:"))
    initialize(adapter)
    result = state()
    with adapter.begin_cycle() as tx:
        tx.claim_decision(decision_record())
        seq = tx.record_portfolio_transition(transition(result))
        tx.set_portfolio_current(result, seq)
        tx.create_checkpoint(result, seq)
        tx.mark_terminal(DECISION, CycleOutcome.HOLD)
        tx.commit()
    adapter._connection.execute(
        "UPDATE portfolio_checkpoints SET state_digest=? WHERE portfolio_id=?",
        ("f" * 64, "paper-main"),
    )
    recovered = adapter.recover("paper-main")
    assert recovered.status is RecoveryStatus.RECOVERED
    assert recovered.portfolio_state == result
    adapter.close()


def test_empty_database_recovery_is_clean_without_inventing_balance() -> None:
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(":memory:"))
    recovered = adapter.recover("paper-main")
    assert recovered.status is RecoveryStatus.CLEAN
    assert recovered.portfolio_state is None
    adapter.close()


def test_commit_requires_terminal_cycle_and_current_projection() -> None:
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(":memory:"))
    tx = adapter.begin_cycle()
    tx.claim_decision(decision_record())
    with pytest.raises(PersistenceTransactionError, match="terminal"):
        tx.commit()
    assert adapter._connection.execute("SELECT count(*) FROM paper_decisions").fetchone()[0] == 0

    tx = adapter.begin_cycle()
    tx.claim_decision(decision_record())
    tx.record_portfolio_transition(transition())
    tx.mark_terminal(DECISION, CycleOutcome.HOLD)
    with pytest.raises(PersistenceTransactionError, match="portfolio_current"):
        tx.commit()
    assert adapter._connection.execute("SELECT count(*) FROM journal_entries").fetchone()[0] == 0
    adapter.close()


def test_conflicting_same_fill_id_is_not_treated_as_duplicate() -> None:
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(":memory:"))
    with adapter.begin_cycle() as tx:
        tx.claim_decision(decision_record())
        tx.record_command(command_record())
        tx.record_fill(fill_record())
        with pytest.raises(PersistenceConflictError):
            tx.record_fill(replace(fill_record(), fee=Decimal("0.02")))
    adapter.close()


def test_restart_duplicate_command_cannot_create_second_fill(tmp_path: Path) -> None:
    path = tmp_path / "restart.sqlite3"
    first = SQLiteTradingPersistence(SQLitePersistenceConfig(path))
    initialize(first)
    with first.begin_cycle() as tx:
        tx.claim_decision(decision_record())
        tx.record_command(command_record())
        tx.record_fill(fill_record())
        seq = tx.record_portfolio_transition(
            replace(transition(), durable_command_key=COMMAND, fill_id="fill-1")
        )
        tx.set_portfolio_current(state(), seq)
        tx.mark_terminal(DECISION, CycleOutcome.EXECUTED)
        tx.commit()
    first.close()

    second = SQLiteTradingPersistence(SQLitePersistenceConfig(path))
    with second.begin_cycle() as tx:
        assert not tx.claim_decision(decision_record())
        assert not tx.record_command(command_record())
        with pytest.raises(PersistenceConflictError):
            tx.record_fill(fill_record(fill_id="fill-after-restart"))
    assert second._connection.execute("SELECT count(*) FROM fills").fetchone()[0] == 1
    second.close()


def test_decision_slot_correction_after_restart_requires_recovery(tmp_path: Path) -> None:
    path = tmp_path / "slot.sqlite3"
    first = SQLiteTradingPersistence(SQLitePersistenceConfig(path))
    with first.begin_cycle() as tx:
        tx.claim_decision(decision_record())
        tx.mark_terminal(DECISION, CycleOutcome.HOLD)
        tx.commit()
    first.close()
    second = SQLiteTradingPersistence(SQLitePersistenceConfig(path))
    with second.begin_cycle() as tx, pytest.raises(PersistenceDecisionSlotConflictError):
        tx.claim_decision(decision_record(key="e" * 64))
    assert second._connection.execute("SELECT count(*) FROM paper_decisions").fetchone()[0] == 1
    second.close()


def test_journal_is_append_only_at_database_level() -> None:
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(":memory:"))
    commit_valuation(adapter)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        adapter._connection.execute("UPDATE journal_entries SET entry_type='FILL'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        adapter._connection.execute("DELETE FROM journal_entries")
    adapter.close()


def test_recovery_fails_closed_on_malformed_decimal_and_journal_digest() -> None:
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(":memory:"))
    commit_valuation(adapter)
    adapter._connection.execute(
        "UPDATE portfolio_current SET state_payload=replace(state_payload,'1000','NaN')"
    )
    assert adapter.recover("paper-main").status is RecoveryStatus.RECOVERY_REQUIRED
    adapter.close()


def test_recovery_fails_closed_on_foreign_key_corruption(tmp_path: Path) -> None:
    path = tmp_path / "foreign-key.sqlite3"
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(path))
    adapter._connection.execute("PRAGMA foreign_keys=OFF")
    adapter._connection.execute(
        "INSERT INTO paper_commands VALUES(?,?,?,?)",
        (COMMAND, DECISION, "{}", PAYLOAD_DIGEST),
    )
    assert any(item.startswith("foreign_key_violation") for item in adapter.integrity_check())
    assert adapter.recover("paper-main").status is RecoveryStatus.RECOVERY_REQUIRED
    adapter.close()

    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(":memory:"))
    commit_valuation(adapter)
    adapter._connection.execute("DROP TRIGGER journal_entries_no_update")
    adapter._connection.execute("UPDATE journal_entries SET after_state_digest=?", ("f" * 64,))
    assert adapter.recover("paper-main").status is RecoveryStatus.RECOVERY_REQUIRED
    adapter.close()


def test_schema_has_no_financial_real_columns() -> None:
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(":memory:"))
    financial_tables = ("fills", "journal_entries", "portfolio_current", "portfolio_checkpoints")
    types = {
        row[2].upper()
        for table in financial_tables
        for row in adapter._connection.execute(f"PRAGMA table_info({table})")
    }
    assert "REAL" not in types
    adapter.close()


def test_newer_schema_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "newer.sqlite3"
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(path))
    adapter._connection.execute(
        "INSERT INTO schema_migrations VALUES(2,'002_future.sql',?,0)", ("f" * 64,)
    )
    adapter.close()
    with pytest.raises(PersistenceSchemaError):
        SQLiteTradingPersistence(SQLitePersistenceConfig(path))


def test_canonical_rejects_invalid_text_and_supports_enum_and_sequences() -> None:
    assert canonical_json({"outcome": CycleOutcome.HOLD, "items": (True, None, 1)})
    assert canonical_sequence([1, 2]) == (1, 2)
    for value in ("", b"x"):
        with pytest.raises(PersistenceSerializationError):
            canonical_sequence(value)  # type: ignore[arg-type]
    for value in (None, "", "01", "1.0", "NaN", "bad"):
        with pytest.raises(PersistenceSerializationError):
            deserialize_decimal(value)  # type: ignore[arg-type]
    with pytest.raises(PersistenceSerializationError, match="mapping keys"):
        canonical_json({1: "bad"})
    with pytest.raises(PersistenceSerializationError, match="algorithm_version"):
        canonical_sha256({}, algorithm_version="")


def test_key_helpers_reject_wrong_types_and_invalid_inputs() -> None:
    with pytest.raises(PersistenceSerializationError):
        fingerprint_configuration("", {})
    with pytest.raises(PersistenceSerializationError):
        fingerprint_strategy_input(())
    with pytest.raises(PersistenceSerializationError):
        fingerprint_strategy_input((object(),))  # type: ignore[arg-type]
    with pytest.raises(PersistenceSerializationError):
        stable_decision_key(object())  # type: ignore[arg-type]
    with pytest.raises(PersistenceSerializationError):
        decision_slot_key(object())  # type: ignore[arg-type]
    with pytest.raises(PersistenceSerializationError):
        durable_command_key(object())  # type: ignore[arg-type]
    with pytest.raises(PersistenceSerializationError):
        stable_decision_key(replace(key_input(), exchange=""))
    with pytest.raises(PersistenceSerializationError):
        fingerprint_ema_strategy(object())  # type: ignore[arg-type]


def test_persistence_configuration_validation(tmp_path: Path) -> None:
    from app.persistence.exceptions import PersistenceConfigurationError

    invalid = (
        {"database_path": ""},
        {"database_path": tmp_path},
        {"database_path": ":memory:", "busy_timeout_ms": 0},
        {"database_path": ":memory:", "checkpoint_interval_transitions": True},
        {"database_path": ":memory:", "expected_schema_version": 5},
    )
    for values in invalid:
        with pytest.raises(PersistenceConfigurationError):
            SQLitePersistenceConfig(**values)  # type: ignore[arg-type]


def position_state() -> PositionState:
    return PositionState(
        quantity=Decimal(1),
        cost_basis=Decimal(100),
        average_entry_price=Decimal(100),
        realised_pnl=Decimal(0),
        unrealised_pnl=Decimal(10),
        last_mark_price=Decimal(110),
        opened_at_ms=1,
        updated_at_ms=2,
    )


def invested_state() -> PortfolioState:
    position = position_state()
    return PortfolioState(
        portfolio_id="paper-main",
        stream_key=KEY,
        base_asset="BTC",
        quote_asset="USDT",
        quote_balance=Decimal(900),
        base_balance=Decimal(1),
        position=position,
        realised_pnl=Decimal(0),
        unrealised_pnl=Decimal(10),
        total_fees=Decimal(0),
        initial_equity=Decimal(1000),
        turnover=Decimal(100),
        trade_count=1,
        applied_fill_count=1,
        peak_equity=Decimal(1010),
        current_equity=Decimal(1010),
        drawdown=Decimal(0),
        max_drawdown=Decimal(0),
        mark_price=Decimal(110),
        as_of_ms=2,
        version=1,
        correlation_id="cycle",
    )


def test_position_and_invested_portfolio_state_round_trip() -> None:
    expected = invested_state()
    assert expected.position == position_state()
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(":memory:"))
    initialize(adapter)
    with adapter.begin_cycle() as tx:
        tx.claim_decision(decision_record())
        record = transition(expected)
        seq = tx.record_portfolio_transition(record)
        tx.set_portfolio_current(expected, seq)
        tx.mark_terminal(DECISION, CycleOutcome.HOLD)
        tx.commit()
    assert adapter.load_current("paper-main") == expected
    adapter.close()


@pytest.mark.parametrize(
    "overrides",
    [
        {"portfolio_id": ""},
        {"stream_key": "BTCUSDT"},
        {"quote_balance": Decimal(-1)},
        {"trade_count": 1},
        {"base_balance": Decimal(1)},
        {"current_equity": Decimal(999)},
        {"peak_equity": Decimal(999)},
        {"drawdown": Decimal(1)},
        {"max_drawdown": Decimal(-1)},
        {"correlation_id": ""},
    ],
)
def test_portfolio_state_rejects_invalid_cross_field_values(overrides: dict[str, object]) -> None:
    from app.persistence.exceptions import PersistenceIntegrityError

    with pytest.raises(PersistenceIntegrityError):
        replace(state(), **overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"quantity": Decimal(0)},
        {"cost_basis": Decimal(-1)},
        {"realised_pnl": Decimal("NaN")},
        {"opened_at_ms": 3},
    ],
)
def test_position_state_rejects_invalid_values(overrides: dict[str, object]) -> None:
    from app.persistence.exceptions import PersistenceIntegrityError

    with pytest.raises(PersistenceIntegrityError):
        replace(position_state(), **overrides)


def test_recovery_result_is_immutable_and_rejects_usable_required_state() -> None:
    from app.persistence.exceptions import PersistenceIntegrityError

    result = RecoveryResult(RecoveryStatus.CLEAN, None, None, 0, 0, metadata={"safe": True})
    with pytest.raises(TypeError):
        result.metadata["safe"] = False  # type: ignore[index]
    with pytest.raises(PersistenceIntegrityError):
        RecoveryResult(RecoveryStatus.RECOVERY_REQUIRED, state(), None, 0, 0)


class FakeRecoveryView:
    def __init__(
        self,
        *,
        current: PortfolioState | None = None,
        transitions: tuple[PortfolioTransitionRecord, ...] = (),
        integrity: tuple[str, ...] = ("ok",),
        issues: tuple[str, ...] = (),
    ) -> None:
        self.current = current
        self.transitions = transitions
        self.integrity = integrity
        self.issues = issues
        self.initial = (
            InitialPortfolioRecord(initial_state(), CONFIG_FINGERPRINT, 0)
            if current is not None or transitions
            else None
        )

    def load_initial(self, portfolio_id: str):
        return self.initial

    def load_current(self, portfolio_id: str):
        return self.current

    def load_checkpoints(self, portfolio_id: str):
        return ()

    def load_transitions(self, portfolio_id: str):
        return self.transitions

    def integrity_check(self):
        return self.integrity

    def reconciliation_issues(self):
        return self.issues


def test_recovery_coordinator_fail_closed_branches() -> None:
    from app.persistence.exceptions import PersistenceReconciliationError
    from app.persistence.recovery import RecoveryCoordinator

    coordinator = RecoveryCoordinator()
    with pytest.raises(PersistenceReconciliationError):
        coordinator.recover(object(), "paper-main")  # type: ignore[arg-type]
    with pytest.raises(PersistenceReconciliationError):
        coordinator.recover(FakeRecoveryView(), "")
    assert (
        coordinator.recover(FakeRecoveryView(integrity=("corrupt",)), "paper-main").status
        is RecoveryStatus.RECOVERY_REQUIRED
    )
    assert (
        coordinator.recover(
            FakeRecoveryView(current=state(), issues=("bad lineage",)), "paper-main"
        ).status
        is RecoveryStatus.RECOVERY_REQUIRED
    )
    assert (
        coordinator.recover(FakeRecoveryView(current=state()), "paper-main").status
        is RecoveryStatus.RECOVERY_REQUIRED
    )
    missing_projection = coordinator.recover(
        FakeRecoveryView(transitions=(transition(),)), "paper-main"
    )
    assert missing_projection.status is RecoveryStatus.RECOVERED
    assert missing_projection.portfolio_state == state()


def test_recovery_coordinator_detects_version_digest_and_current_mismatch() -> None:
    from app.persistence.recovery import RecoveryCoordinator

    coordinator = RecoveryCoordinator()
    first = transition()
    gap = replace(first, version_before=2, version_after=3, after_state=replace(state(), version=3))
    assert (
        coordinator.recover(
            FakeRecoveryView(current=gap.after_state, transitions=(gap,)), "paper-main"
        ).status
        is RecoveryStatus.RECOVERY_REQUIRED
    )
    second_state = state(2, as_of_ms=2)
    bad_digest = transition(second_state, before_digest="f" * 64)
    assert (
        coordinator.recover(
            FakeRecoveryView(current=second_state, transitions=(first, bad_digest)), "paper-main"
        ).status
        is RecoveryStatus.RECOVERY_REQUIRED
    )
    assert (
        coordinator.recover(
            FakeRecoveryView(current=state(1, quote="999"), transitions=(first,)), "paper-main"
        ).status
        is RecoveryStatus.RECOVERY_REQUIRED
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"position": object()},
        {"base_balance": Decimal(2)},
        {"unrealised_pnl": Decimal(9)},
        {"mark_price": Decimal(109)},
        {"as_of_ms": 3},
        {"current_equity": Decimal(1009)},
    ],
)
def test_invested_portfolio_rejects_inconsistent_position(overrides: dict[str, object]) -> None:
    from app.persistence.exceptions import PersistenceIntegrityError

    with pytest.raises(PersistenceIntegrityError):
        replace(invested_state(), **overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"stream_key": "BTCUSDT"},
        {"side": "SHORT"},
        {"quantity": Decimal(0)},
        {"fee": Decimal(-1)},
        {"dust_liquidation": 1},
        {"correlation_id": ""},
    ],
)
def test_persistent_fill_rejects_invalid_values(overrides: dict[str, object]) -> None:
    from app.persistence.exceptions import PersistenceIntegrityError

    with pytest.raises(PersistenceIntegrityError):
        replace(fill_record(), **overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"version_after": 3},
        {"after_state": state(2)},
        {"portfolio_id": "different"},
        {"durable_command_key": "invalid"},
    ],
)
def test_transition_rejects_invalid_chain(overrides: dict[str, object]) -> None:
    from app.persistence.exceptions import PersistenceIntegrityError

    with pytest.raises(PersistenceIntegrityError):
        replace(transition(), **overrides)


def test_transaction_duplicate_and_terminal_conflict_branches() -> None:
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(":memory:"))
    with adapter.begin_cycle() as tx:
        tx.claim_decision(decision_record())
        tx.record_command(command_record())
        assert not tx.record_command(command_record())
        with pytest.raises(PersistenceConflictError):
            tx.record_command(command_record(payload='{"different":true}'))
        tx.mark_terminal(DECISION, CycleOutcome.HOLD)
        tx.mark_terminal(DECISION, CycleOutcome.HOLD)
        with pytest.raises(PersistenceConflictError):
            tx.mark_terminal(DECISION, CycleOutcome.EXECUTED)
    adapter.close()


def test_transaction_rejects_use_after_commit_and_noncontiguous_current() -> None:
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(":memory:"))
    commit_valuation(adapter)
    tx = adapter.begin_cycle()
    tx.claim_decision(decision_record(key="e" * 64, slot="f" * 64))
    gap_state = state(3, as_of_ms=3)
    gap = replace(
        transition(gap_state),
        stable_decision_key="e" * 64,
        version_before=2,
        version_after=3,
    )
    seq = tx.record_portfolio_transition(gap)
    with pytest.raises(PersistenceConflictError, match="current state"):
        tx.set_portfolio_current(gap_state, seq)
    tx.rollback()

    tx = adapter.begin_cycle()
    tx.claim_decision(decision_record(key="e" * 64, slot="f" * 64))
    tx.mark_terminal("e" * 64, CycleOutcome.HOLD)
    tx.commit()
    with pytest.raises(PersistenceTransactionError, match="no longer active"):
        tx.rollback()
    adapter.close()


def test_checkpoint_conflict_and_adapter_lifecycle() -> None:
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(":memory:"))
    initialize(adapter)
    with adapter.begin_cycle() as tx:
        tx.claim_decision(decision_record())
        seq = tx.record_portfolio_transition(transition())
        tx.set_portfolio_current(state(), seq)
        tx.create_checkpoint(state(), seq)
        with pytest.raises(PersistenceConflictError):
            tx.create_checkpoint(state(), seq + 1)
    assert adapter.load_fill(COMMAND) is None
    adapter.begin_cycle()
    adapter.close()
    assert not adapter._transaction_active
    with pytest.raises(PersistenceConnectionError):
        adapter.begin_cycle()
    adapter.close()


def test_reconciliation_issue_queries_cover_invalid_lifecycle() -> None:
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(":memory:"))
    adapter._connection.execute(
        "INSERT INTO paper_decisions VALUES(?,?,?,?,?,NULL)",
        (DECISION, SLOT, "{}", PAYLOAD_DIGEST, 1),
    )
    assert "visible non-terminal decision" in adapter.reconciliation_issues()
    adapter._connection.execute(
        "UPDATE paper_decisions SET terminal_outcome='EXECUTED' WHERE stable_decision_key=?",
        (DECISION,),
    )
    assert "executed decision has no Fill" in adapter.reconciliation_issues()
    adapter._connection.execute(
        "INSERT INTO paper_commands VALUES(?,?,?,?)", (COMMAND, DECISION, "{}", PAYLOAD_DIGEST)
    )
    record = fill_record()
    adapter._connection.execute(
        "INSERT INTO fills VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            record.fill_id,
            record.durable_command_key,
            record.order_id,
            record.risk_decision_id,
            record.intent_id,
            KEY.symbol,
            KEY.interval,
            record.side,
            "0.1",
            "100",
            "10",
            "0.01",
            record.fee_asset,
            "-10.01",
            "0.1",
            1,
            0,
            None,
            "fill/v1",
        ),
    )
    adapter._connection.execute(
        "UPDATE paper_decisions SET terminal_outcome='HOLD' WHERE stable_decision_key=?",
        (DECISION,),
    )
    assert "non-executed decision has a Fill" in adapter.reconciliation_issues()
    adapter.close()


def test_adapter_context_manager_closes_connection() -> None:
    with SQLiteTradingPersistence(SQLitePersistenceConfig(":memory:")) as adapter:
        assert adapter.status.opened
    assert adapter._closed


@pytest.mark.parametrize(
    "stage",
    ["before_begin", "decision", "command", "fill", "transition", "current", "before_commit"],
)
def test_every_precommit_crash_point_rolls_back_fully(tmp_path: Path, stage: str) -> None:
    path = tmp_path / f"crash-{stage}.sqlite3"
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(path))
    initialize(adapter)
    if stage != "before_begin":
        tx = adapter.begin_cycle()
        tx.claim_decision(decision_record())
        if stage not in {"decision"}:
            tx.record_command(command_record())
        if stage not in {"decision", "command"}:
            tx.record_fill(fill_record())
        if stage in {"transition", "current", "before_commit"}:
            seq = tx.record_portfolio_transition(
                replace(transition(), durable_command_key=COMMAND, fill_id="fill-1")
            )
        if stage in {"current", "before_commit"}:
            tx.set_portfolio_current(state(), seq)
        if stage == "before_commit":
            tx.mark_terminal(DECISION, CycleOutcome.EXECUTED)
    adapter.close()

    fresh = SQLiteTradingPersistence(SQLitePersistenceConfig(path))
    for table in ("paper_decisions", "paper_commands", "fills", "journal_entries"):
        assert fresh._connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert fresh.load_current("paper-main") == initial_state()
    with fresh.begin_cycle() as retry:
        assert retry.claim_decision(decision_record())
        retry.mark_terminal(DECISION, CycleOutcome.HOLD)
        retry.commit()
    fresh.close()


@pytest.mark.parametrize(
    ("outcome", "command", "fill"),
    [
        (CycleOutcome.EXECUTED, True, False),
        (CycleOutcome.HOLD, True, False),
        (CycleOutcome.PLANNER_DECLINED, True, False),
        (CycleOutcome.RISK_REJECTED, True, True),
        (CycleOutcome.NOT_EXECUTED, False, False),
        (CycleOutcome.NOT_EXECUTED, True, True),
    ],
)
def test_terminal_outcome_rejects_impossible_lineage(
    outcome: CycleOutcome, command: bool, fill: bool
) -> None:
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(":memory:"))
    initialize(adapter)
    tx = adapter.begin_cycle()
    tx.claim_decision(decision_record())
    if command:
        tx.record_command(command_record())
    if fill:
        tx.record_fill(fill_record())
    tx.mark_terminal(DECISION, outcome)
    with pytest.raises(PersistenceTransactionError):
        tx.commit()
    assert adapter._connection.execute("SELECT count(*) FROM paper_decisions").fetchone()[0] == 0
    adapter.close()


@pytest.mark.parametrize(
    ("outcome", "with_command"),
    [
        (CycleOutcome.HOLD, False),
        (CycleOutcome.PLANNER_DECLINED, False),
        (CycleOutcome.RISK_REJECTED, False),
        (CycleOutcome.NOT_EXECUTED, True),
    ],
)
def test_noop_terminal_cycle_preserves_portfolio_version(
    outcome: CycleOutcome, with_command: bool
) -> None:
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(":memory:"))
    initialize(adapter)
    with adapter.begin_cycle() as tx:
        tx.claim_decision(decision_record())
        if with_command:
            tx.record_command(command_record())
        tx.mark_terminal(DECISION, outcome)
        tx.commit()
    assert adapter.load_current("paper-main") == initial_state()
    assert adapter.recover("paper-main").status is RecoveryStatus.CLEAN
    adapter.close()


def test_two_effective_transitions_commit_as_gap_free_single_cycle() -> None:
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(":memory:"))
    initialize(adapter)
    first = state(1, quote="999", as_of_ms=1)
    second = state(2, quote="998", as_of_ms=2)
    with adapter.begin_cycle() as tx:
        tx.claim_decision(decision_record())
        first_seq = tx.record_portfolio_transition(transition(first))
        second_seq = tx.record_portfolio_transition(transition(second, before_digest=first.digest))
        assert second_seq > first_seq
        tx.set_portfolio_current(second, second_seq)
        tx.mark_terminal(DECISION, CycleOutcome.HOLD)
        tx.commit()
    recovered = adapter.recover("paper-main")
    assert recovered.status is RecoveryStatus.RECOVERED
    assert recovered.portfolio_state == second
    assert recovered.replayed_transitions == 2
    adapter.close()


def test_initial_portfolio_identity_is_idempotent_and_mismatch_fails_closed() -> None:
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(":memory:"))
    record = InitialPortfolioRecord(initial_state(), CONFIG_FINGERPRINT, 0)
    assert adapter.initialize_portfolio(record)
    assert not adapter.initialize_portfolio(record)
    with pytest.raises(PersistenceConflictError):
        adapter.initialize_portfolio(replace(record, config_fingerprint="8" * 64))
    assert (
        adapter.recover("paper-main", expected_stream_key=MarketStreamKey("ETHUSDT", "1m")).status
        is RecoveryStatus.RECOVERY_REQUIRED
    )
    assert (
        adapter.recover("paper-main", expected_config_fingerprint="8" * 64).status
        is RecoveryStatus.RECOVERY_REQUIRED
    )
    adapter.close()


def test_recovery_is_deterministic_and_read_only() -> None:
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(":memory:"))
    commit_valuation(adapter)
    before = adapter.status
    first = adapter.recover("paper-main")
    second = adapter.recover("paper-main")
    assert first == second
    assert adapter.status == before
    adapter.close()


def test_checkpoint_fallback_reports_each_corrupt_version() -> None:
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(":memory:"))
    initialize(adapter)
    previous = initial_state()
    for version, key_char, slot_char in ((1, "d", "a"), (2, "e", "b"), (3, "f", "c")):
        current = state(version, quote=str(1000 - version), as_of_ms=version)
        decision_key = key_char * 64
        with adapter.begin_cycle() as tx:
            tx.claim_decision(decision_record(key=decision_key, slot=slot_char * 64))
            seq = tx.record_portfolio_transition(
                replace(
                    transition(current, before_digest=previous.digest),
                    stable_decision_key=decision_key,
                )
            )
            tx.set_portfolio_current(current, seq)
            tx.create_checkpoint(current, seq)
            tx.mark_terminal(decision_key, CycleOutcome.HOLD)
            tx.commit()
        previous = current

    for corrupt_version, expected_checkpoint in ((3, 2), (2, 1), (1, None)):
        adapter._connection.execute(
            "UPDATE portfolio_checkpoints SET state_digest=? "
            "WHERE portfolio_id=? AND portfolio_version=?",
            (str(corrupt_version) * 64, "paper-main", corrupt_version),
        )
        recovered = adapter.recover("paper-main")
        assert recovered.status is RecoveryStatus.RECOVERED
        assert recovered.checkpoint_version == expected_checkpoint
        assert f"checkpoint_skipped:version={corrupt_version}" in recovered.diagnostics
    adapter.close()


def test_adapter_lifecycle_rejects_closed_and_active_backup(tmp_path: Path) -> None:
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(tmp_path / "source.sqlite3"))
    initialize(adapter)
    with adapter.begin_cycle(), pytest.raises(PersistenceTransactionError, match="backup"):
        adapter.create_backup(tmp_path / "forbidden.sqlite3")
    adapter.close()
    adapter.close()
    operations = (
        lambda: adapter.load_initial("paper-main"),
        lambda: adapter.load_current("paper-main"),
        lambda: adapter.load_transitions("paper-main"),
        lambda: adapter.load_checkpoints("paper-main"),
        lambda: adapter.integrity_check(),
        lambda: adapter.recover("paper-main"),
        lambda: adapter.create_backup(tmp_path / "closed.sqlite3"),
        lambda: adapter.status,
    )
    for operation in operations:
        with pytest.raises(PersistenceConnectionError):
            operation()


def test_decimal_pathological_exponents_use_bounded_canonical_scientific_notation() -> None:
    huge = Decimal("1E+1000000")
    tiny = Decimal("1E-1000000")
    assert serialize_decimal(huge) == "1E+1000000"
    assert serialize_decimal(tiny) == "1E-1000000"
    assert len(serialize_decimal(huge)) < 32
    assert deserialize_decimal(serialize_decimal(huge)) == huge
    assert deserialize_decimal(serialize_decimal(tiny)) == tiny


def test_canonical_json_keeps_scalar_types_unambiguous() -> None:
    from enum import Enum, StrEnum

    class Value(StrEnum):
        ONE = "one"

    class Number(Enum):
        ONE = 1

    encoded = {
        "decimal": canonical_json({"a": Decimal(1)}),
        "string": canonical_json({"a": "1"}),
        "integer": canonical_json({"a": 1}),
        "boolean": canonical_json({"a": True}),
        "none": canonical_json({"a": None}),
    }
    assert len(set(encoded.values())) == len(encoded)
    assert canonical_json({"a": Value.ONE}) == canonical_json({"a": "one"})
    assert canonical_json({"a": Number.ONE}) == canonical_json({"a": 1})
    assert canonical_json({"a": [1, 2]}) == canonical_json({"a": (1, 2)})


def test_key_domains_separate_identical_semantic_payloads() -> None:
    payload = {"value": Decimal(1)}
    domains = (
        "stable-decision/v1",
        "decision-slot/v1",
        "paper-command/v1",
        "strategy-input/v1",
        "instrument-rules/v1",
    )
    digests = {canonical_sha256(payload, algorithm_version=domain) for domain in domains}
    assert len(digests) == len(domains)


def test_strategy_input_fingerprint_covers_full_ordered_history() -> None:
    first = candle(0)
    middle = candle(1)
    latest = candle(2)
    baseline = fingerprint_strategy_input((first, middle, latest))
    scale_equivalent = tuple(
        replace(
            item,
            open=Decimal(f"{item.open}.00"),
            high=Decimal(f"{item.high}.00"),
            low=Decimal(f"{item.low}.00"),
            close=Decimal(f"{item.close}.00"),
            volume=Decimal(f"{item.volume}.00"),
        )
        for item in (first, middle, latest)
    )
    assert fingerprint_strategy_input(scale_equivalent) == baseline
    assert (
        fingerprint_strategy_input((replace(first, close=Decimal(99)), middle, latest)) != baseline
    )
    assert fingerprint_strategy_input((middle, first, latest)) != baseline
    assert fingerprint_strategy_input((middle, latest)) != baseline
    assert fingerprint_strategy_input((first, middle, latest, candle(3))) != baseline


def test_explicit_configuration_fingerprints_cover_every_behavior_field() -> None:
    inputs = (
        (
            fingerprint_ema_strategy,
            EmaStrategyFingerprintInput(12, 26, "ema_crossover", "1.0.0"),
        ),
        (
            fingerprint_planner,
            PlannerFingerprintInput(Decimal(1), "long_only", "1.0.0"),
        ),
        (
            fingerprint_risk_policy,
            RiskPolicyFingerprintInput(
                Decimal(100),
                Decimal(500),
                Decimal(1),
                Decimal(10),
                Decimal("0.001"),
                Decimal(5),
                True,
                "long_only_spot",
                "1.0.0",
            ),
        ),
        (
            fingerprint_instrument_rules,
            InstrumentRulesFingerprintInput(
                "BTCUSDT",
                "1m",
                "BTC",
                "USDT",
                Decimal("0.01"),
                Decimal("0.00001"),
                Decimal("0.00001"),
                Decimal(10),
                8,
                8,
                "binance-v1",
            ),
        ),
        (
            fingerprint_paper_execution,
            PaperExecutionFingerprintInput(Decimal("0.001"), 0, "paper", "1.0.0"),
        ),
        (
            fingerprint_snapshot_policy,
            SnapshotPolicyFingerprintInput(60_000, "market-snapshot/v1"),
        ),
    )
    for builder, value in inputs:
        baseline = builder(value)  # type: ignore[arg-type]
        for field_name in value.__dataclass_fields__:
            field_value = getattr(value, field_name)
            if isinstance(field_value, bool):
                changed = not field_value
            elif isinstance(field_value, Decimal):
                changed = field_value + Decimal("0.0000001")
            elif isinstance(field_value, int):
                changed = field_value + 1
            else:
                changed = f"{field_value}-changed"
            assert builder(replace(value, **{field_name: changed})) != baseline  # type: ignore[arg-type]


def _insert_fill_sql(
    connection: sqlite3.Connection, *, fill_id: str, command_key: str = COMMAND
) -> None:
    record = fill_record(fill_id=fill_id, command=command_key)
    connection.execute(
        "INSERT INTO fills VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            record.fill_id,
            record.durable_command_key,
            record.order_id,
            record.risk_decision_id,
            record.intent_id,
            KEY.symbol,
            KEY.interval,
            record.side,
            "0.1",
            "100",
            "10",
            "0.01",
            record.fee_asset,
            "-10.01",
            "0.1",
            1,
            0,
            None,
            "fill/v1",
        ),
    )


def test_sqlite_itself_enforces_critical_uniqueness_and_checks() -> None:
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(":memory:"))
    connection = adapter._connection
    connection.execute(
        "INSERT INTO paper_decisions VALUES(?,?,?,?,?,?)",
        (DECISION, SLOT, "{}", PAYLOAD_DIGEST, 1, "EXECUTED"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO paper_decisions VALUES(?,?,?,?,?,?)",
            ("e" * 64, SLOT, "{}", PAYLOAD_DIGEST, 1, "HOLD"),
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO paper_decisions VALUES(?,?,?,?,?,?)",
            ("e" * 64, "f" * 64, "{}", PAYLOAD_DIGEST, 1, "INVALID"),
        )
    connection.execute(
        "INSERT INTO paper_commands VALUES(?,?,?,?)", (COMMAND, DECISION, "{}", PAYLOAD_DIGEST)
    )
    _insert_fill_sql(connection, fill_id="fill-sql-1")
    with pytest.raises(sqlite3.IntegrityError):
        _insert_fill_sql(connection, fill_id="fill-sql-2")
    adapter.close()


def test_file_pragmas_and_migration_checksum_are_verified(tmp_path: Path) -> None:
    adapter = SQLiteTradingPersistence(
        SQLitePersistenceConfig(tmp_path / "pragma.sqlite3", busy_timeout_ms=1_234)
    )
    assert adapter._connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert adapter._connection.execute("PRAGMA synchronous").fetchone()[0] == 2
    assert adapter._connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert adapter._connection.execute("PRAGMA busy_timeout").fetchone()[0] == 1_234
    _, expected_checksum = adapter._migration()
    stored_checksum = adapter._connection.execute(
        "SELECT checksum FROM schema_migrations WHERE version=1"
    ).fetchone()[0]
    assert stored_checksum == expected_checksum
    adapter.close()


def test_backup_failure_closes_destination_and_source_remains_usable(tmp_path: Path) -> None:
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(tmp_path / "source.sqlite3"))
    initialize(adapter)
    destination_directory = tmp_path / "destination-directory"
    destination_directory.mkdir()
    with pytest.raises(PersistenceConnectionError):
        adapter.create_backup(destination_directory)
    with adapter.begin_cycle() as tx:
        tx.claim_decision(decision_record())
        tx.mark_terminal(DECISION, CycleOutcome.HOLD)
        tx.commit()
    assert adapter._connection.execute("SELECT count(*) FROM paper_decisions").fetchone()[0] == 1
    adapter.close()


def test_checkpoint_never_hides_earlier_journal_corruption() -> None:
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(":memory:"))
    initialize(adapter)
    with adapter.begin_cycle() as tx:
        tx.claim_decision(decision_record())
        seq = tx.record_portfolio_transition(transition())
        tx.set_portfolio_current(state(), seq)
        tx.create_checkpoint(state(), seq)
        tx.mark_terminal(DECISION, CycleOutcome.HOLD)
        tx.commit()
    adapter._connection.execute("DROP TRIGGER journal_entries_no_update")
    adapter._connection.execute(
        "UPDATE journal_entries SET before_digest=? WHERE journal_seq=?", ("f" * 64, seq)
    )
    assert adapter.recover("paper-main").status is RecoveryStatus.RECOVERY_REQUIRED
    adapter.close()


def test_missing_current_projection_is_reconstructed_read_only_from_journal() -> None:
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(":memory:"))
    commit_valuation(adapter)
    adapter._connection.execute("DELETE FROM portfolio_current WHERE portfolio_id='paper-main'")
    recovered = adapter.recover("paper-main")
    assert recovered.status is RecoveryStatus.RECOVERED
    assert recovered.portfolio_state == state()
    assert adapter.load_current("paper-main") is None
    adapter.close()


def test_cross_stream_fill_transition_lineage_fails_closed() -> None:
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(":memory:"))
    initialize(adapter)
    wrong_stream_fill = replace(fill_record(), stream_key=MarketStreamKey("ETHUSDT", "1m"))
    with adapter.begin_cycle() as tx:
        tx.claim_decision(decision_record())
        tx.record_command(command_record())
        tx.record_fill(wrong_stream_fill)
        seq = tx.record_portfolio_transition(
            replace(transition(), durable_command_key=COMMAND, fill_id=wrong_stream_fill.fill_id)
        )
        tx.set_portfolio_current(state(), seq)
        tx.mark_terminal(DECISION, CycleOutcome.EXECUTED)
        tx.commit()
    result = adapter.recover("paper-main")
    assert result.status is RecoveryStatus.RECOVERY_REQUIRED
    assert "stream" in result.diagnostics[0]
    adapter.close()


def test_initial_and_checkpoint_models_reject_identity_mismatch() -> None:
    from app.persistence.exceptions import PersistenceIntegrityError
    from app.persistence.models import PortfolioCheckpoint

    with pytest.raises(PersistenceIntegrityError):
        InitialPortfolioRecord(state(), CONFIG_FINGERPRINT, 0)
    with pytest.raises(PersistenceIntegrityError):
        InitialPortfolioRecord(initial_state(), "invalid", 0)
    with pytest.raises(PersistenceIntegrityError):
        PortfolioCheckpoint("other", 1, state(), state().digest, 1)
    with pytest.raises(PersistenceIntegrityError):
        PortfolioCheckpoint("paper-main", 2, state(), state().digest, 1)


def test_recovery_wraps_unexpected_view_failure_and_rejects_missing_root() -> None:
    from app.persistence.exceptions import PersistenceReconciliationError
    from app.persistence.recovery import RecoveryCoordinator

    class BrokenView(FakeRecoveryView):
        def integrity_check(self):
            raise ValueError("unexpected")

    with pytest.raises(PersistenceReconciliationError) as captured:
        RecoveryCoordinator().recover(BrokenView(), "paper-main")
    assert isinstance(captured.value.__cause__, ValueError)

    view = FakeRecoveryView(current=state())
    view.initial = None
    assert (
        RecoveryCoordinator().recover(view, "paper-main").status is RecoveryStatus.RECOVERY_REQUIRED
    )


def test_portfolio_current_requires_exact_contiguous_transition_batch() -> None:
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(":memory:"))
    initialize(adapter)
    tx = adapter.begin_cycle()
    tx.claim_decision(decision_record())
    with pytest.raises(PersistenceConflictError, match="requires a transition"):
        tx.set_portfolio_current(state(), 1)
    tx.rollback()

    tx = adapter.begin_cycle()
    tx.claim_decision(decision_record())
    first = transition()
    first_seq = tx.record_portfolio_transition(first)
    wrong_second = replace(
        transition(state(2, quote="999", as_of_ms=2), before_digest="f" * 64),
        version_before=1,
        version_after=2,
    )
    second_seq = tx.record_portfolio_transition(wrong_second)
    with pytest.raises(PersistenceConflictError, match="batch"):
        tx.set_portfolio_current(wrong_second.after_state, second_seq)
    tx.rollback()

    tx = adapter.begin_cycle()
    tx.claim_decision(decision_record())
    seq = tx.record_portfolio_transition(transition())
    with pytest.raises(PersistenceConflictError, match="last transition"):
        tx.set_portfolio_current(state(1, quote="999"), seq)
    tx.rollback()
    assert first_seq < second_seq
    adapter.close()


def test_typed_adapter_errors_preserve_sqlite_causes() -> None:
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(":memory:"))
    tx = adapter.begin_cycle()
    tx.claim_decision(decision_record())
    tx.mark_terminal(DECISION, CycleOutcome.HOLD)
    adapter._connection.close()
    with pytest.raises(PersistenceTransactionError) as captured:
        tx.commit()
    assert isinstance(captured.value.__cause__, sqlite3.Error)

    with pytest.raises(PersistenceConnectionError):
        SQLiteTradingPersistence(object())  # type: ignore[arg-type]


def test_initialize_rejects_wrong_type_and_active_transaction() -> None:
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(":memory:"))
    with pytest.raises(PersistenceIntegrityError):
        adapter.initialize_portfolio(object())  # type: ignore[arg-type]
    with adapter.begin_cycle(), pytest.raises(PersistenceTransactionError):
        adapter.initialize_portfolio(InitialPortfolioRecord(initial_state(), CONFIG_FINGERPRINT, 0))
    adapter.close()


def test_checkpoint_bad_journal_link_is_skipped_with_diagnostic() -> None:
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(":memory:"))
    initialize(adapter)
    with adapter.begin_cycle() as tx:
        tx.claim_decision(decision_record())
        seq = tx.record_portfolio_transition(transition())
        tx.set_portfolio_current(state(), seq)
        tx.create_checkpoint(state(), seq)
        tx.mark_terminal(DECISION, CycleOutcome.HOLD)
        tx.commit()
    adapter._connection.execute("PRAGMA foreign_keys=OFF")
    adapter._connection.execute(
        "UPDATE portfolio_checkpoints SET last_journal_seq=? WHERE portfolio_id=?",
        (999, "paper-main"),
    )
    assert adapter.load_checkpoints("paper-main") == ()
    assert adapter.checkpoint_diagnostics("paper-main") == ("checkpoint_skipped:version=1",)
    adapter.close()


def test_initialize_writer_conflict_and_broken_connection_are_typed(tmp_path: Path) -> None:
    config = SQLitePersistenceConfig(tmp_path / "initialize-lock.sqlite3", busy_timeout_ms=1)
    first = SQLiteTradingPersistence(config)
    second = SQLiteTradingPersistence(config)
    with first.begin_cycle(), pytest.raises(PersistenceWriterConflictError):
        second.initialize_portfolio(InitialPortfolioRecord(initial_state(), CONFIG_FINGERPRINT, 0))
    first.close()
    second.close()

    broken = SQLiteTradingPersistence(SQLitePersistenceConfig(":memory:"))
    broken._connection.close()
    with pytest.raises(PersistenceTransactionError) as captured:
        broken.begin_cycle()
    assert isinstance(captured.value.__cause__, sqlite3.Error)


def test_sql_execution_error_is_wrapped_with_cause() -> None:
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(":memory:"))
    adapter._connection.execute("DROP TABLE paper_decisions")
    with adapter.begin_cycle() as tx, pytest.raises(PersistenceTransactionError) as captured:
        tx.claim_decision(decision_record())
    assert isinstance(captured.value.__cause__, sqlite3.Error)
    adapter.close()


def test_root_digest_and_stored_payload_digest_corruption_fail_closed() -> None:
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(":memory:"))
    initialize(adapter)
    adapter._connection.execute(
        "UPDATE portfolio_roots SET state_digest=? WHERE portfolio_id=?",
        ("f" * 64, "paper-main"),
    )
    with pytest.raises(PersistenceIntegrityError, match="initial Portfolio digest"):
        adapter.load_initial("paper-main")
    assert adapter.recover("paper-main").status is RecoveryStatus.RECOVERY_REQUIRED
    adapter.close()

    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(":memory:"))
    initialize(adapter)
    with adapter.begin_cycle() as tx:
        tx.claim_decision(decision_record())
        tx.mark_terminal(DECISION, CycleOutcome.HOLD)
        tx.commit()
    adapter._connection.execute(
        "UPDATE paper_decisions SET payload_digest=? WHERE stable_decision_key=?",
        ("f" * 64, DECISION),
    )
    issues = adapter.reconciliation_issues()
    assert issues == (f"payload_digest_mismatch:paper_decisions:key={DECISION[:12]}",)
    assert adapter.recover("paper-main").status is RecoveryStatus.RECOVERY_REQUIRED
    adapter.close()


def test_reconciliation_detects_terminal_and_current_linkage_corruption() -> None:
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(":memory:"))
    initialize(adapter)
    connection = adapter._connection
    connection.execute(
        "INSERT INTO paper_decisions VALUES(?,?,?,?,?,?)",
        (DECISION, SLOT, "{}", PAYLOAD_DIGEST, 1, "EXECUTED"),
    )
    connection.execute(
        "INSERT INTO paper_commands VALUES(?,?,?,?)", (COMMAND, DECISION, "{}", PAYLOAD_DIGEST)
    )
    _insert_fill_sql(connection, fill_id="orphan-transition-fill")
    assert "executed Fill has no Portfolio transition" in adapter.reconciliation_issues()
    connection.execute("DELETE FROM fills")
    connection.execute("DELETE FROM paper_commands")
    connection.execute(
        "UPDATE paper_decisions SET terminal_outcome='NOT_EXECUTED' WHERE stable_decision_key=?",
        (DECISION,),
    )
    assert "NOT_EXECUTED decision has no command" in adapter.reconciliation_issues()
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute(
        "UPDATE portfolio_current SET portfolio_version=1,last_journal_seq=999 "
        "WHERE portfolio_id='paper-main'"
    )
    assert "portfolio_current journal linkage mismatch" in adapter.reconciliation_issues()
    adapter.close()


def test_constructor_and_write_errors_are_typed_with_causes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        raise sqlite3.OperationalError("open failed")

    monkeypatch.setattr("app.persistence.sqlite.sqlite3.connect", fail_connect)
    with pytest.raises(PersistenceConnectionError) as open_error:
        SQLiteTradingPersistence(SQLitePersistenceConfig(":memory:"))
    assert isinstance(open_error.value.__cause__, sqlite3.OperationalError)
    monkeypatch.undo()

    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(":memory:"))
    with adapter.begin_cycle() as tx, pytest.raises(PersistenceTransactionError) as write_error:
        tx._execute("BROKEN SQL", ())
    assert isinstance(write_error.value.__cause__, sqlite3.OperationalError)
    adapter.close()


def test_initialize_wraps_non_locking_sqlite_failure() -> None:
    adapter = SQLiteTradingPersistence(SQLitePersistenceConfig(":memory:"))
    adapter._connection.execute("DROP TABLE portfolio_roots")
    with pytest.raises(PersistenceTransactionError, match="initialization") as captured:
        adapter.initialize_portfolio(InitialPortfolioRecord(initial_state(), CONFIG_FINGERPRINT, 0))
    assert isinstance(captured.value.__cause__, sqlite3.OperationalError)
    adapter.close()
