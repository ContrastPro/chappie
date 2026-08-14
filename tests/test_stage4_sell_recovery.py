import hashlib
import sqlite3
import threading
from pathlib import Path

import pytest

from app.exchange_execution import (
    Stage4SellRecoveryAuthorization,
    Stage4SellRecoveryEventType,
    Stage4SellRecoveryExchangeState,
    Stage4SellRecoveryReview,
    evaluate_stage4_sell_recovery,
)
from app.exchange_execution.exceptions import ExchangeCapabilityError
from app.exchange_execution.stage4_sell_recovery import Stage4SellRecoveryError, reason_set_digest
from app.persistence import SQLiteExchangePersistence, SQLitePersistenceConfig
from app.persistence.exceptions import (
    PersistenceIntegrityError,
    PersistenceMigrationError,
    PersistenceSchemaError,
    Stage4ManagedSellRestartRecoveryOnlyError,
    Stage4SellRecoveryConflictError,
    Stage4SellRecoveryPreconditionError,
)
from app.persistence.sqlite import SQLiteTradingPersistence
from tests.test_stage4_managed_sell_authority import predecessor, reviewed


def candidate(path: Path, *, failure_injector=None) -> tuple[SQLiteExchangePersistence, str]:
    initial = predecessor(path)
    initial.close()
    store = SQLiteExchangePersistence(
        SQLitePersistenceConfig(path, expected_schema_version=8),
        failure_injector=failure_injector,
    )
    receipt = store.authorize_stage4_managed_sell_command(reviewed(store))
    return store, receipt.durable_command_key


def authorization() -> Stage4SellRecoveryAuthorization:
    return Stage4SellRecoveryAuthorization.from_environment(
        {"STAGE4_SELL_RECOVERY_AUTHORIZED": "YES"}
    )


def recovery_review(
    *,
    exchange_state: Stage4SellRecoveryExchangeState = Stage4SellRecoveryExchangeState.ABSENCE_PROVEN,
    risk_compatible: bool = True,
    configuration_compatible: bool = True,
    managed_ownership_compatible: bool = True,
    operator_approves_continuation: bool = True,
) -> Stage4SellRecoveryReview:
    return Stage4SellRecoveryReview(
        snapshot_digest="c" * 64,
        current_rules_fingerprint="a" * 64,
        exchange_state=exchange_state,
        risk_compatible=risk_compatible,
        configuration_compatible=configuration_compatible,
        managed_ownership_compatible=managed_ownership_compatible,
        operator_approves_continuation=operator_approves_continuation,
    )


def test_recovery_authorization_is_distinct_and_exact() -> None:
    with pytest.raises(ExchangeCapabilityError):
        Stage4SellRecoveryAuthorization()
    with pytest.raises(Stage4SellRecoveryError):
        Stage4SellRecoveryAuthorization.from_environment({"STAGE4_MANAGED_SELL_AUTHORIZED": "YES"})
    with pytest.raises(Stage4SellRecoveryError):
        Stage4SellRecoveryAuthorization.from_environment({"M4_10_PHASE3_AUTHORIZED": "YES"})
    with pytest.raises(Stage4SellRecoveryError):
        Stage4SellRecoveryAuthorization.from_environment({"STAGE4_SELL_RECOVERY_AUTHORIZED": "yes"})
    capability = authorization()
    capability._consume()
    with pytest.raises(ExchangeCapabilityError):
        capability._consume()
    with pytest.raises(Stage4SellRecoveryError):
        reason_set_digest(("B", "A"))
    with pytest.raises(Stage4SellRecoveryError):
        reason_set_digest(("FABRICATED_REASON",))


def test_persistence_submission_primitive_cannot_bypass_recovery_boundary(
    tmp_path: Path,
) -> None:
    path = tmp_path / "direct-bypass.sqlite3"
    store, key = candidate(path)
    with pytest.raises(Stage4ManagedSellRestartRecoveryOnlyError):
        store.begin_submission(key, occurred_at_ms=5000)
    store.record_stage4_sell_recovery_required(
        key,
        reasons=("CLAIM_FAILURE",),
        snapshot_stage="FINAL_GATE",
        snapshot_digest=None,
        occurred_at_ms=5001,
    )
    with pytest.raises(Stage4ManagedSellRestartRecoveryOnlyError):
        store.begin_submission(key, occurred_at_ms=5002)
    store.close()
    restarted = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=8))
    with pytest.raises(Stage4ManagedSellRestartRecoveryOnlyError):
        restarted.begin_submission(key, occurred_at_ms=5003)
    assert (
        restarted._connection.execute(
            "SELECT count(*) FROM exchange_submission_attempts WHERE durable_command_key=?", (key,)
        ).fetchone()[0]
        == 0
    )
    restarted.close()


def test_recovery_required_is_append_only_idempotent_and_recovery_only(tmp_path: Path) -> None:
    store, key = candidate(tmp_path / "recovery.sqlite3")
    first = store.record_stage4_sell_recovery_required(
        key,
        reasons=("SNAPSHOT_PRICE_CHANGED",),
        snapshot_stage="SNAPSHOT_B",
        snapshot_digest="a" * 64,
        occurred_at_ms=5000,
    )
    replay = store.record_stage4_sell_recovery_required(
        key,
        reasons=("SNAPSHOT_PRICE_CHANGED",),
        snapshot_stage="SNAPSHOT_B",
        snapshot_digest="a" * 64,
        occurred_at_ms=6000,
    )
    assert replay.recovery_event_id == first.recovery_event_id
    payload = store._connection.execute(
        "SELECT payload FROM exchange_managed_sell_recovery_events"
    ).fetchone()[0]
    assert all(
        forbidden not in payload.lower()
        for forbidden in ("api_key", "secret", "signature", "authorization", "raw_response")
    )
    assert store.load_order(key).order.status.value == "PREPARED"
    assert (
        store._connection.execute(
            "SELECT count(*) FROM exchange_submission_attempts WHERE durable_command_key=?", (key,)
        ).fetchone()[0]
        == 0
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._connection.execute(
            "UPDATE exchange_managed_sell_recovery_events SET block_reason_code='x'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._connection.execute("DELETE FROM exchange_managed_sell_recovery_events")
    store.close()


def test_one_audited_disposition_and_no_submission_capability(tmp_path: Path) -> None:
    store, key = candidate(tmp_path / "decision.sqlite3")
    store.record_stage4_sell_recovery_required(
        key,
        reasons=("FINAL_GATE_REJECTED",),
        snapshot_stage="FINAL_GATE",
        snapshot_digest="b" * 64,
        occurred_at_ms=5000,
    )
    event = store.decide_stage4_sell_recovery(
        key,
        decision=Stage4SellRecoveryEventType.CONTINUATION_ALLOWED,
        reasons=("VERIFIED_NO_EXCHANGE_ORDER",),
        snapshot_digest="c" * 64,
        current_rules_fingerprint="a" * 64,
        occurred_at_ms=6000,
        authorization=authorization(),
    )
    assert event.event_type is Stage4SellRecoveryEventType.CONTINUATION_ALLOWED
    assert not hasattr(event, "claim")
    with pytest.raises(Stage4SellRecoveryConflictError):
        store.decide_stage4_sell_recovery(
            key,
            decision=Stage4SellRecoveryEventType.DISPOSITION_REQUIRED,
            reasons=("MANUAL_DISPOSITION",),
            snapshot_digest="d" * 64,
            current_rules_fingerprint="a" * 64,
            occurred_at_ms=7000,
            authorization=authorization(),
        )
    assert (
        store._connection.execute(
            "SELECT count(*) FROM exchange_orders WHERE side='SELL'"
        ).fetchone()[0]
        == 1
    )
    assert (
        store._connection.execute(
            "SELECT count(*) FROM exchange_submission_attempts WHERE durable_command_key=?", (key,)
        ).fetchone()[0]
        == 0
    )
    store.close()


def test_high_level_recovery_review_allows_only_a_non_capability_decision(tmp_path: Path) -> None:
    store, key = candidate(tmp_path / "service-continuation.sqlite3")
    store.record_stage4_sell_recovery_required(
        key,
        reasons=("FINAL_GATE_REJECTED",),
        snapshot_stage="FINAL_GATE",
        snapshot_digest="b" * 64,
        occurred_at_ms=5000,
    )
    event = evaluate_stage4_sell_recovery(
        store, key, review=recovery_review(), occurred_at_ms=6000, authorization=authorization()
    )
    assert event.event_type is Stage4SellRecoveryEventType.CONTINUATION_ALLOWED
    assert not hasattr(event, "claim")
    assert store.load_order(key).order.status.value == "PREPARED"
    assert (
        store._connection.execute(
            "SELECT count(*) FROM exchange_submission_attempts WHERE durable_command_key=?", (key,)
        ).fetchone()[0]
        == 0
    )
    store.close()


@pytest.mark.parametrize(
    "review",
    [
        recovery_review(exchange_state=Stage4SellRecoveryExchangeState.UNKNOWN),
        recovery_review(exchange_state=Stage4SellRecoveryExchangeState.ORDER_PRESENT),
        recovery_review(risk_compatible=False),
        recovery_review(configuration_compatible=False),
        recovery_review(managed_ownership_compatible=False),
        recovery_review(operator_approves_continuation=False),
    ],
)
def test_high_level_recovery_review_fails_closed_to_disposition(
    tmp_path: Path, review: Stage4SellRecoveryReview
) -> None:
    store, key = candidate(tmp_path / f"service-{review.reasons[0]}.sqlite3")
    store.record_stage4_sell_recovery_required(
        key,
        reasons=("FINAL_GATE_REJECTED",),
        snapshot_stage="FINAL_GATE",
        snapshot_digest="b" * 64,
        occurred_at_ms=5000,
    )
    event = evaluate_stage4_sell_recovery(
        store, key, review=review, occurred_at_ms=6000, authorization=authorization()
    )
    assert event.event_type is Stage4SellRecoveryEventType.DISPOSITION_REQUIRED
    assert event.block_reason_code == review.reasons[0]
    assert store.load_order(key).order.status.value == "PREPARED"
    store.close()


@pytest.mark.parametrize("mutation", ["portfolio", "session", "reconciliation", "rules"])
def test_continuation_decision_fails_closed_on_drift(tmp_path: Path, mutation: str) -> None:
    store, key = candidate(tmp_path / f"{mutation}.sqlite3")
    store.record_stage4_sell_recovery_required(
        key,
        reasons=("FINAL_GATE_REJECTED",),
        snapshot_stage="FINAL_GATE",
        snapshot_digest="b" * 64,
        occurred_at_ms=5000,
    )
    rules = "a" * 64
    if mutation == "portfolio":
        store._connection.execute("UPDATE exchange_portfolio_current SET portfolio_version=2")
    elif mutation == "session":
        store._connection.execute("UPDATE exchange_sessions SET submission_count=2")
    elif mutation == "reconciliation":
        store._connection.execute("UPDATE exchange_reconciliation_current SET status='REQUIRED'")
    else:
        rules = "f" * 64
    store._connection.commit()
    with pytest.raises(Stage4SellRecoveryPreconditionError):
        store.decide_stage4_sell_recovery(
            key,
            decision=Stage4SellRecoveryEventType.CONTINUATION_ALLOWED,
            reasons=("VERIFIED_NO_EXCHANGE_ORDER",),
            snapshot_digest="c" * 64,
            current_rules_fingerprint=rules,
            occurred_at_ms=6000,
            authorization=authorization(),
        )
    store.close()


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt, SystemExit, GeneratorExit])
@pytest.mark.parametrize(
    "boundary", ["stage4_sell_recovery_before_insert", "stage4_sell_recovery_after_insert"]
)
def test_recovery_event_precommit_crash_rolls_back(
    tmp_path: Path, failure: type[BaseException], boundary: str
) -> None:
    path = tmp_path / f"{failure.__name__}-{boundary}.sqlite3"

    def fail(name: str) -> None:
        if name == boundary:
            raise failure(name)

    store, key = candidate(path, failure_injector=fail)
    with pytest.raises(failure):
        store.record_stage4_sell_recovery_required(
            key,
            reasons=("CLAIM_FAILURE",),
            snapshot_stage="FINAL_GATE",
            snapshot_digest=None,
            occurred_at_ms=5000,
        )
    store.close()
    reopened = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=8))
    assert (
        reopened._connection.execute(
            "SELECT count(*) FROM exchange_managed_sell_recovery_events"
        ).fetchone()[0]
        == 0
    )
    reopened.close()


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt, SystemExit, GeneratorExit])
def test_recovery_event_postcommit_crash_is_durable(
    tmp_path: Path, failure: type[BaseException]
) -> None:
    path = tmp_path / f"postcommit-{failure.__name__}.sqlite3"

    def fail(name: str) -> None:
        if name == "stage4_sell_recovery_after_commit":
            raise failure(name)

    store, key = candidate(path, failure_injector=fail)
    with pytest.raises(failure):
        store.record_stage4_sell_recovery_required(
            key,
            reasons=("CLAIM_FAILURE",),
            snapshot_stage="FINAL_GATE",
            snapshot_digest=None,
            occurred_at_ms=5000,
        )
    store.close()
    reopened = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=8))
    assert (
        reopened._connection.execute(
            "SELECT count(*) FROM exchange_managed_sell_recovery_events"
        ).fetchone()[0]
        == 1
    )
    assert reopened.recover_local().reconciliation_required
    reopened.close()


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt, SystemExit, GeneratorExit])
@pytest.mark.parametrize(
    ("boundary", "expected_count"),
    [
        ("stage4_sell_recovery_before_insert", 1),
        ("stage4_sell_recovery_after_insert", 1),
        ("stage4_sell_recovery_after_commit", 2),
    ],
)
def test_disposition_crash_matrix_is_atomic(
    tmp_path: Path,
    failure: type[BaseException],
    boundary: str,
    expected_count: int,
) -> None:
    path = tmp_path / f"decision-{boundary}-{failure.__name__}.sqlite3"
    initial, key = candidate(path)
    initial.record_stage4_sell_recovery_required(
        key,
        reasons=("CLAIM_FAILURE",),
        snapshot_stage="FINAL_GATE",
        snapshot_digest=None,
        occurred_at_ms=5000,
    )
    initial.close()

    def fail(name: str) -> None:
        if name == boundary:
            raise failure(name)

    store = SQLiteExchangePersistence(
        SQLitePersistenceConfig(path, expected_schema_version=8), failure_injector=fail
    )
    with pytest.raises(failure):
        store.decide_stage4_sell_recovery(
            key,
            decision=Stage4SellRecoveryEventType.DISPOSITION_REQUIRED,
            reasons=("MANUAL_DISPOSITION",),
            snapshot_digest="f" * 64,
            current_rules_fingerprint="a" * 64,
            occurred_at_ms=6000,
            authorization=authorization(),
        )
    store.close()
    observed = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=8))
    assert (
        observed._connection.execute(
            "SELECT count(*) FROM exchange_managed_sell_recovery_events"
        ).fetchone()[0]
        == expected_count
    )
    assert (
        observed._connection.execute(
            "SELECT count(*) FROM exchange_submission_attempts WHERE durable_command_key=?", (key,)
        ).fetchone()[0]
        == 0
    )
    observed.close()


def test_concurrent_recovery_actors_create_one_event(tmp_path: Path) -> None:
    path = tmp_path / "concurrent.sqlite3"
    store, key = candidate(path)
    store.close()
    outcomes: list[str] = []

    def actor() -> None:
        local = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=8))
        try:
            event = local.record_stage4_sell_recovery_required(
                key,
                reasons=("FINAL_GATE_REJECTED",),
                snapshot_stage="FINAL_GATE",
                snapshot_digest="e" * 64,
                occurred_at_ms=5000,
            )
            outcomes.append("observer" if event.replayed else "winner")
        finally:
            local.close()

    threads = [threading.Thread(target=actor) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    observed = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=8))
    assert sorted(outcomes) == ["observer", "winner"]
    assert (
        observed._connection.execute(
            "SELECT count(*) FROM exchange_managed_sell_recovery_events"
        ).fetchone()[0]
        == 1
    )
    assert (
        observed._connection.execute(
            "SELECT count(*) FROM exchange_orders WHERE side='SELL'"
        ).fetchone()[0]
        == 1
    )
    observed.close()


def test_concurrent_dispositions_have_one_winner_and_no_sell_opportunity(tmp_path: Path) -> None:
    path = tmp_path / "concurrent-disposition.sqlite3"
    store, key = candidate(path)
    store.record_stage4_sell_recovery_required(
        key,
        reasons=("CLAIM_FAILURE",),
        snapshot_stage="FINAL_GATE",
        snapshot_digest=None,
        occurred_at_ms=5000,
    )
    store.close()
    outcomes: list[str] = []

    def actor(decision: Stage4SellRecoveryEventType) -> None:
        local = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=8))
        try:
            event = local.decide_stage4_sell_recovery(
                key,
                decision=decision,
                reasons=(
                    "VERIFIED_NO_EXCHANGE_ORDER"
                    if decision is Stage4SellRecoveryEventType.CONTINUATION_ALLOWED
                    else "MANUAL_DISPOSITION",
                ),
                snapshot_digest=(
                    "a" if decision is Stage4SellRecoveryEventType.CONTINUATION_ALLOWED else "b"
                )
                * 64,
                current_rules_fingerprint="a" * 64,
                occurred_at_ms=6000,
                authorization=authorization(),
            )
            outcomes.append(f"winner:{event.event_type.value}")
        except Stage4SellRecoveryConflictError:
            outcomes.append("conflict")
        finally:
            local.close()

    threads = [
        threading.Thread(target=actor, args=(Stage4SellRecoveryEventType.CONTINUATION_ALLOWED,)),
        threading.Thread(target=actor, args=(Stage4SellRecoveryEventType.DISPOSITION_REQUIRED,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count("conflict") == 1
    assert sum(value.startswith("winner:") for value in outcomes) == 1
    observed = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=8))
    assert (
        observed._connection.execute(
            "SELECT count(*) FROM exchange_managed_sell_recovery_events"
        ).fetchone()[0]
        == 2
    )
    assert (
        observed._connection.execute(
            "SELECT count(*) FROM exchange_submission_attempts WHERE durable_command_key=?", (key,)
        ).fetchone()[0]
        == 0
    )
    observed.close()


def test_schema_v8_required_and_recovery_integrity_is_validated(tmp_path: Path) -> None:
    old, key = candidate(tmp_path / "schema.sqlite3")
    old.record_stage4_sell_recovery_required(
        key,
        reasons=("CLAIM_FAILURE",),
        snapshot_stage="FINAL_GATE",
        snapshot_digest=None,
        occurred_at_ms=5000,
    )
    old.close()
    v7 = tmp_path / "v7.sqlite3"
    predecessor(v7).close()
    legacy = SQLiteExchangePersistence(SQLitePersistenceConfig(v7, expected_schema_version=7))
    try:
        with pytest.raises(PersistenceIntegrityError, match="schema version 8"):
            legacy.record_stage4_sell_recovery_required(
                "c" * 64,
                reasons=("CLAIM_FAILURE",),
                snapshot_stage="FINAL_GATE",
                snapshot_digest=None,
                occurred_at_ms=1,
            )
    finally:
        legacy.close()


def test_failed_migration_008_rolls_back_to_valid_v7(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "migration-failure.sqlite3"
    predecessor(path).close()
    original = SQLiteTradingPersistence._migration

    def broken(name: str = "001_initial.sql") -> tuple[str, str]:
        if name == "008_stage4_sell_recovery.sql":
            sql = "CREATE TABLE should_rollback(value TEXT); INVALID SQL;"
            return sql, hashlib.sha256(sql.encode()).hexdigest()
        return original(name)

    monkeypatch.setattr(SQLiteTradingPersistence, "_migration", staticmethod(broken))
    with pytest.raises(PersistenceMigrationError):
        SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=8))
    monkeypatch.undo()
    reopened = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=7))
    assert (
        reopened._connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
        == 7
    )
    assert (
        reopened._connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE name='should_rollback'"
        ).fetchone()[0]
        == 0
    )
    reopened.close()


def test_v7_to_v8_preserves_prepared_graph_and_repeated_open_is_idempotent(
    tmp_path: Path,
) -> None:
    path = tmp_path / "upgrade.sqlite3"
    v7 = predecessor(path)
    receipt = v7.authorize_stage4_managed_sell_command(reviewed(v7))
    before = v7._connection.execute(
        "SELECT status,client_order_id,original_quantity FROM exchange_orders "
        "WHERE durable_command_key=?",
        (receipt.durable_command_key,),
    ).fetchone()
    v7.close()
    for _ in range(2):
        upgraded = SQLiteExchangePersistence(
            SQLitePersistenceConfig(path, expected_schema_version=8)
        )
        assert tuple(
            upgraded._connection.execute(
                "SELECT status,client_order_id,original_quantity FROM exchange_orders "
                "WHERE durable_command_key=?",
                (receipt.durable_command_key,),
            ).fetchone()
        ) == tuple(before)
        assert (
            upgraded._connection.execute(
                "SELECT count(*) FROM exchange_submission_attempts WHERE durable_command_key=?",
                (receipt.durable_command_key,),
            ).fetchone()[0]
            == 0
        )
        assert (
            upgraded._connection.execute(
                "SELECT count(*) FROM exchange_managed_sell_recovery_events"
            ).fetchone()[0]
            == 0
        )
        upgraded.close()


def test_migration_008_checksum_mismatch_and_newer_schema_fail_closed(tmp_path: Path) -> None:
    mismatch = tmp_path / "mismatch.sqlite3"
    store = SQLiteExchangePersistence(SQLitePersistenceConfig(mismatch, expected_schema_version=8))
    store._connection.execute(
        "UPDATE schema_migrations SET checksum=? WHERE version=8", ("0" * 64,)
    )
    store._connection.commit()
    store.close()
    with pytest.raises(PersistenceMigrationError, match="checksum"):
        SQLiteExchangePersistence(SQLitePersistenceConfig(mismatch, expected_schema_version=8))

    newer = tmp_path / "newer.sqlite3"
    store = SQLiteExchangePersistence(SQLitePersistenceConfig(newer, expected_schema_version=8))
    store._connection.execute(
        "INSERT INTO schema_migrations VALUES(9,'009_future.sql',?,0)", ("9" * 64,)
    )
    store._connection.commit()
    store.close()
    with pytest.raises(PersistenceSchemaError, match="newer"):
        SQLiteExchangePersistence(SQLitePersistenceConfig(newer, expected_schema_version=8))
