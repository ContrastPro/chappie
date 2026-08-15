import sqlite3
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from app.exchange_execution import (
    ExchangeOrderStatus,
    Stage4SellDispositionAuthorization,
    derive_stage4_sell_successor_economic_slot,
)
from app.exchange_execution.stage4_sell import Stage4ManagedSellReviewError
from app.persistence import SQLiteExchangePersistence, SQLitePersistenceConfig
from app.persistence.exceptions import (
    PersistenceIntegrityError,
    Stage4ManagedSellAuthorityError,
    Stage4ManagedSellStalePredecessorError,
)
from tests.test_stage4_managed_sell_authority import predecessor, reviewed


def _disposed_predecessor(path: Path) -> tuple[SQLiteExchangePersistence, object]:
    store = predecessor(path)
    store.close()
    store = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=9))
    original = reviewed(store)
    store.authorize_stage4_managed_sell_command(original)
    store.dispose_stage4_sell_no_execution(
        original.command.durable_command_key,
        reason_code="mvp-safe-disposition-before-resell",
        occurred_at_ms=5000,
        authorization=Stage4SellDispositionAuthorization.from_environment(
            {"STAGE4_SELL_DISPOSITION_AUTHORIZED": "YES"}
        ),
    )
    store.close()
    return (
        SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=10)),
        original,
    )


def _successor(store: SQLiteExchangePersistence, predecessor_request):
    row = store._connection.execute(
        "SELECT authority_id,economic_slot_key FROM exchange_managed_sell_authorities "
        "WHERE consumed_durable_command_key=?",
        (predecessor_request.command.durable_command_key,),
    ).fetchone()
    command = replace(
        predecessor_request.command,
        durable_command_key="1" * 64,
        stable_decision_key="2" * 64,
        decision_slot_key="3" * 64,
        authorized_at_ms=6000,
        correlation_id="stage4-sell-successor-test",
    )
    return row["authority_id"], replace(
        predecessor_request,
        authority_id="stage4-sell-successor-authority",
        operator_run_id="stage4-sell-successor-run",
        command=command,
        created_at_ms=6000,
    )


def _economic_snapshot(store: SQLiteExchangePersistence) -> tuple[object, ...]:
    portfolio = store._connection.execute(
        "SELECT portfolio_version,state_payload,state_digest FROM exchange_portfolio_current"
    ).fetchone()
    session = store._connection.execute(
        "SELECT submission_count,submitted_notional,executed_turnover FROM exchange_sessions"
    ).fetchone()
    reconciliation = store._connection.execute(
        "SELECT status,generation,state_payload,state_digest FROM exchange_reconciliation_current"
    ).fetchone()
    settlements = store._connection.execute(
        "SELECT count(*) FROM exchange_trade_settlements WHERE status != 'SETTLED'"
    ).fetchone()[0]
    return tuple(portfolio) + tuple(session) + tuple(reconciliation) + (settlements,)


def test_generation_one_successor_is_atomic_and_prepared(tmp_path: Path) -> None:
    store, original = _disposed_predecessor(tmp_path / "successor.sqlite3")
    predecessor_authority_id, request = _successor(store, original)
    before = _economic_snapshot(store)
    receipt = store.authorize_stage4_managed_sell_successor_command(
        predecessor_authority_id, request
    )

    old = store.load_order(original.command.durable_command_key)
    new = store.load_order(request.command.durable_command_key)
    lineage = store._connection.execute("SELECT * FROM exchange_managed_sell_successors").fetchone()
    assert old.order.status is ExchangeOrderStatus.DISPOSED_NO_EXECUTION
    assert new.order.status is ExchangeOrderStatus.PREPARED
    assert receipt.client_order_id != old.order.client_order_id
    assert _economic_snapshot(store) == before
    assert lineage["predecessor_generation"] == 0
    assert lineage["successor_generation"] == 1
    assert lineage["successor_durable_command_key"] == request.command.durable_command_key
    assert (
        store._connection.execute(
            "SELECT count(*) FROM exchange_submission_attempts a JOIN exchange_orders o "
            "ON o.durable_command_key=a.durable_command_key WHERE o.side='SELL'"
        ).fetchone()[0]
        == 0
    )
    assert (
        store._connection.execute(
            "SELECT count(*) FROM exchange_trades WHERE side='SELL'"
        ).fetchone()[0]
        == 0
    )
    store.close()


def test_successor_requires_disposed_unsubmitted_generation_zero(tmp_path: Path) -> None:
    store = predecessor(tmp_path / "not-disposed.sqlite3")
    original = reviewed(store)
    store.authorize_stage4_managed_sell_command(original)
    store.close()
    store = SQLiteExchangePersistence(
        SQLitePersistenceConfig(tmp_path / "not-disposed.sqlite3", expected_schema_version=10)
    )
    row = store._connection.execute(
        "SELECT authority_id FROM exchange_managed_sell_authorities"
    ).fetchone()
    request = replace(
        reviewed(store),
        authority_id="stage4-sell-successor-authority",
        operator_run_id="stage4-sell-successor-run",
        command=replace(
            reviewed(store).command,
            durable_command_key="1" * 64,
            stable_decision_key="2" * 64,
            decision_slot_key="3" * 64,
        ),
    )
    with pytest.raises(Stage4ManagedSellStalePredecessorError):
        store.authorize_stage4_managed_sell_successor_command(row["authority_id"], request)
    store.close()


def test_successor_generation_is_fixed_and_unique(tmp_path: Path) -> None:
    store, original = _disposed_predecessor(tmp_path / "fixed.sqlite3")
    predecessor_authority_id, request = _successor(store, original)
    with pytest.raises(Stage4ManagedSellReviewError):
        replace(request, successor_generation=2)
    store.authorize_stage4_managed_sell_successor_command(predecessor_authority_id, request)
    with pytest.raises(Stage4ManagedSellAuthorityError):
        store.authorize_stage4_managed_sell_successor_command(
            predecessor_authority_id,
            replace(
                request,
                authority_id="stage4-sell-successor-conflict",
                operator_run_id="stage4-sell-successor-conflict-run",
                command=replace(
                    request.command,
                    durable_command_key="4" * 64,
                    stable_decision_key="5" * 64,
                    decision_slot_key="6" * 64,
                ),
            ),
        )
    store.close()


def test_only_dedicated_boundary_can_select_successor_lineage(tmp_path: Path) -> None:
    store, original = _disposed_predecessor(tmp_path / "dedicated-boundary.sqlite3")
    predecessor_authority_id, request = _successor(store, original)
    predecessor_slot = store._connection.execute(
        "SELECT economic_slot_key FROM exchange_managed_sell_authorities WHERE authority_id=?",
        (predecessor_authority_id,),
    ).fetchone()[0]
    caller_selected = replace(
        request,
        predecessor_authority_id=predecessor_authority_id,
        predecessor_economic_slot_key=predecessor_slot,
        successor_generation=1,
        economic_slot_key=derive_stage4_sell_successor_economic_slot(predecessor_slot, 1),
    )
    with pytest.raises(PersistenceIntegrityError):
        store.authorize_stage4_managed_sell_command(caller_selected)
    with pytest.raises(PersistenceIntegrityError):
        store.authorize_stage4_managed_sell_successor_command(
            predecessor_authority_id, caller_selected
        )
    store.close()


def test_successor_slot_is_deterministic_and_not_predecessor_slot(tmp_path: Path) -> None:
    store, original = _disposed_predecessor(tmp_path / "derived.sqlite3")
    predecessor_authority_id, request = _successor(store, original)
    receipt = store.authorize_stage4_managed_sell_successor_command(
        predecessor_authority_id, request
    )
    old_slot = store._connection.execute(
        "SELECT economic_slot_key FROM exchange_managed_sell_authorities WHERE authority_id=?",
        (predecessor_authority_id,),
    ).fetchone()[0]
    new_slot = store._connection.execute(
        "SELECT economic_slot_key FROM exchange_managed_sell_authorities WHERE authority_id=?",
        (receipt.authority_id,),
    ).fetchone()[0]
    assert old_slot != new_slot
    store.close()


def test_successor_concurrent_creation_has_one_durable_lineage(tmp_path: Path) -> None:
    path = tmp_path / "concurrent.sqlite3"
    store, original = _disposed_predecessor(path)
    predecessor_authority_id, request = _successor(store, original)
    store.close()
    barrier = threading.Barrier(2)
    receipts = []
    failures = []

    def actor() -> None:
        actor_store = SQLiteExchangePersistence(
            SQLitePersistenceConfig(path, expected_schema_version=10)
        )
        try:
            barrier.wait(timeout=5)
            receipts.append(
                actor_store.authorize_stage4_managed_sell_successor_command(
                    predecessor_authority_id, request
                )
            )
        except Exception as error:  # noqa: BLE001 - concurrent SQLite boundary assertion
            failures.append(error)
        finally:
            actor_store.close()

    threads = [threading.Thread(target=actor) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads)
    assert failures == []
    assert len(receipts) == 2
    assert sum(receipt.claim is not None for receipt in receipts) == 1
    assert sum(receipt.recovered and receipt.claim is None for receipt in receipts) == 1
    observed = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=10))
    assert (
        observed._connection.execute(
            "SELECT count(*) FROM exchange_managed_sell_successors"
        ).fetchone()[0]
        == 1
    )
    assert (
        observed._connection.execute(
            "SELECT count(*) FROM exchange_orders WHERE side='SELL'"
        ).fetchone()[0]
        == 2
    )
    observed.close()


def test_successor_journal_is_append_only(tmp_path: Path) -> None:
    store, original = _disposed_predecessor(tmp_path / "append-only.sqlite3")
    predecessor_authority_id, request = _successor(store, original)
    store.authorize_stage4_managed_sell_successor_command(predecessor_authority_id, request)
    with pytest.raises(sqlite3.IntegrityError):
        store._connection.execute(
            "UPDATE exchange_managed_sell_successors SET successor_generation=0"
        )
    with pytest.raises(sqlite3.IntegrityError):
        store._connection.execute("DELETE FROM exchange_managed_sell_successors")
    store.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("payload", "{}"),
        ("payload_digest", "0" * 64),
        ("predecessor_authority_id", "different-predecessor"),
        ("successor_durable_command_key", "f" * 64),
    ],
)
def test_successor_replay_fails_closed_on_corrupt_lineage(
    tmp_path: Path, field: str, value: str
) -> None:
    store, original = _disposed_predecessor(tmp_path / f"corrupt-{field}.sqlite3")
    predecessor_authority_id, request = _successor(store, original)
    store.authorize_stage4_managed_sell_successor_command(predecessor_authority_id, request)
    store._connection.execute("PRAGMA foreign_keys=OFF")
    store._connection.execute("DROP TRIGGER exchange_managed_sell_successors_no_update")
    store._connection.execute(f"UPDATE exchange_managed_sell_successors SET {field}=?", (value,))
    with pytest.raises(PersistenceIntegrityError):
        store.authorize_stage4_managed_sell_successor_command(predecessor_authority_id, request)
    store.close()


def test_successor_replay_fails_closed_when_lineage_is_missing(tmp_path: Path) -> None:
    store, original = _disposed_predecessor(tmp_path / "missing-lineage.sqlite3")
    predecessor_authority_id, request = _successor(store, original)
    store.authorize_stage4_managed_sell_successor_command(predecessor_authority_id, request)
    store._connection.execute("DROP TRIGGER exchange_managed_sell_successors_no_delete")
    store._connection.execute("DELETE FROM exchange_managed_sell_successors")
    with pytest.raises(PersistenceIntegrityError):
        store.authorize_stage4_managed_sell_successor_command(predecessor_authority_id, request)
    store.close()


def test_successor_precommit_failure_rolls_back_everything(tmp_path: Path) -> None:
    path = tmp_path / "rollback.sqlite3"
    store, original = _disposed_predecessor(path)
    predecessor_authority_id, request = _successor(store, original)
    store.close()

    def fail(name: str) -> None:
        if name == "stage4_sell_after_audit":
            raise RuntimeError(name)

    broken = SQLiteExchangePersistence(
        SQLitePersistenceConfig(path, expected_schema_version=10), failure_injector=fail
    )
    with pytest.raises(RuntimeError):
        broken.authorize_stage4_managed_sell_successor_command(predecessor_authority_id, request)
    broken.close()
    observed = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=10))
    assert (
        observed._connection.execute(
            "SELECT count(*) FROM exchange_managed_sell_successors"
        ).fetchone()[0]
        == 0
    )
    assert (
        observed._connection.execute(
            "SELECT count(*) FROM exchange_orders WHERE side='SELL'"
        ).fetchone()[0]
        == 1
    )
    assert (
        observed.load_order(original.command.durable_command_key).order.status
        is ExchangeOrderStatus.DISPOSED_NO_EXECUTION
    )
    observed.close()
