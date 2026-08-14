from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from app.exchange_execution import (
    ExchangeOrderStatus,
    Stage4SellDispositionAuthorization,
    Stage4SellDispositionError,
    Stage4SellRecoveryEventType,
    dispose_stage4_sell_no_execution,
)
from app.exchange_execution.exceptions import ExchangeCapabilityError, ExchangeOrderStateError
from app.exchange_execution.stage4_sell_continuation import Stage4SellContinuationAuthorization
from app.exchange_execution.stage4_sell_recovery import Stage4SellRecoveryAuthorization
from app.persistence import SQLiteExchangePersistence, SQLitePersistenceConfig
from app.persistence.exceptions import (
    PersistenceConflictError,
    PersistenceIntegrityError,
    Stage4SellRecoveryPreconditionError,
)
from app.tools.testnet_stage4_sell import AUTHORIZATION_VALUE as SELL_AUTHORIZATION_VALUE
from tests.test_stage4_managed_sell_authority import predecessor, reviewed


def prepared_disposition(
    path: Path, *, failure_injector=None
) -> tuple[SQLiteExchangePersistence, str]:
    initial = predecessor(path)
    initial.close()
    store = SQLiteExchangePersistence(
        SQLitePersistenceConfig(path, expected_schema_version=9), failure_injector=failure_injector
    )
    receipt = store.authorize_stage4_managed_sell_command(reviewed(store))
    key = receipt.durable_command_key
    store.record_stage4_sell_recovery_required(
        key,
        reasons=("FINAL_GATE_REJECTED",),
        snapshot_stage="FINAL_GATE",
        snapshot_digest="b" * 64,
        occurred_at_ms=5_000,
    )
    store.decide_stage4_sell_recovery(
        key,
        decision=Stage4SellRecoveryEventType.DISPOSITION_REQUIRED,
        reasons=("MANUAL_DISPOSITION",),
        snapshot_digest="c" * 64,
        current_rules_fingerprint="a" * 64,
        occurred_at_ms=6_000,
        authorization=Stage4SellRecoveryAuthorization.from_environment(
            {"STAGE4_SELL_RECOVERY_AUTHORIZED": "YES"}
        ),
    )
    return store, key


def authorization() -> Stage4SellDispositionAuthorization:
    return Stage4SellDispositionAuthorization.from_environment(
        {"STAGE4_SELL_DISPOSITION_AUTHORIZED": "YES"}
    )


def test_disposition_is_terminal_idempotent_and_append_only(tmp_path: Path) -> None:
    store, key = prepared_disposition(tmp_path / "dispose.sqlite3")
    before_portfolio = store._connection.execute(
        "SELECT portfolio_version,state_digest FROM exchange_portfolio_current"
    ).fetchone()
    before_session = store._connection.execute(
        "SELECT submission_count,submitted_notional,executed_turnover FROM exchange_sessions"
    ).fetchone()
    result = dispose_stage4_sell_no_execution(
        store,
        key,
        reason_code="operator_disposition",
        occurred_at_ms=7_000,
        authorization=authorization(),
    )
    disposed = store.load_order(key)
    assert disposed.order.status is ExchangeOrderStatus.DISPOSED_NO_EXECUTION
    assert store.restart_disposition(disposed).value == "TERMINAL"
    assert (
        store._connection.execute(
            "SELECT portfolio_version,state_digest FROM exchange_portfolio_current"
        ).fetchone()
        == before_portfolio
    )
    assert (
        store._connection.execute(
            "SELECT submission_count,submitted_notional,executed_turnover FROM exchange_sessions"
        ).fetchone()
        == before_session
    )
    replay = dispose_stage4_sell_no_execution(
        store,
        key,
        reason_code="operator_disposition",
        occurred_at_ms=7_001,
        authorization=authorization(),
    )
    assert replay.disposition_id == result.disposition_id and replay.replayed
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._connection.execute("DELETE FROM exchange_managed_sell_dispositions")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._connection.execute(
            "UPDATE exchange_managed_sell_dispositions SET reason_code='replacement'"
        )
    store.close()


def test_disposition_requires_the_durable_recovery_decision(tmp_path: Path) -> None:
    path = tmp_path / "missing.sqlite3"
    legacy = predecessor(path)
    legacy.close()
    store = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=9))
    receipt = store.authorize_stage4_managed_sell_command(reviewed(store))
    with pytest.raises(
        (
            Stage4SellRecoveryPreconditionError,
            PersistenceIntegrityError,
            ExchangeOrderStateError,
        )
    ):
        dispose_stage4_sell_no_execution(
            store,
            receipt.durable_command_key,
            reason_code="operator_disposition",
            occurred_at_ms=7_000,
            authorization=authorization(),
        )
    store.close()


@pytest.mark.parametrize(
    "mutation",
    [
        "attempt",
        "submitting",
        "exchange_id",
        "portfolio",
        "ownership",
        "session",
        "trade",
        "settlement",
    ],
)
def test_disposition_fails_closed_on_durable_drift(tmp_path: Path, mutation: str) -> None:
    store, key = prepared_disposition(tmp_path / f"{mutation}.sqlite3")
    if mutation == "attempt":
        store._connection.execute(
            "INSERT INTO exchange_submission_attempts VALUES(?,?,?,?,?,?,?)",
            ("f" * 64, key, 1, store.load_order(key).order.client_order_id, 6_500, "10", "e" * 64),
        )
    elif mutation == "submitting":
        store._connection.execute(
            "INSERT INTO exchange_order_events VALUES(NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "d" * 64,
                key,
                99,
                "PREPARED",
                "SUBMITTING",
                "TRANSPORT",
                "test",
                None,
                "0",
                "0",
                None,
                6_500,
                "{}",
                "c" * 64,
            ),
        )
    elif mutation == "exchange_id":
        store._connection.execute(
            "UPDATE exchange_orders SET exchange_order_id='unexpected' WHERE durable_command_key=?",
            (key,),
        )
    elif mutation == "portfolio":
        store._connection.execute("UPDATE exchange_portfolio_current SET portfolio_version=2")
    elif mutation == "ownership":
        portfolio = store._connection.execute(
            "SELECT state_payload FROM exchange_portfolio_current"
        ).fetchone()
        state = json.loads(portfolio[0])
        state["base_balance"]["$decimal"] = "0.00015"
        store._connection.execute(
            "UPDATE exchange_portfolio_current SET state_payload=?",
            (json.dumps(state, sort_keys=True, separators=(",", ":")),),
        )
    elif mutation == "session":
        store._connection.execute("UPDATE exchange_sessions SET submission_count=2")
    else:
        trade_key = key
        if mutation == "settlement":
            trade_key = store._connection.execute(
                "SELECT source_buy_command_key FROM exchange_managed_sell_authorities "
                "WHERE consumed_durable_command_key=?",
                (key,),
            ).fetchone()[0]
        order = store.load_order(trade_key).order
        store._connection.execute(
            "INSERT INTO exchange_trades(venue,environment,account_fingerprint,symbol,"
            "exchange_order_id,exchange_trade_id,durable_command_key,client_order_id,interval,side,"
            "quantity,price,quote_quantity,commission,commission_asset,traded_at_ms,is_maker,"
            "settlement_status,payload,payload_digest) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "binance-spot",
                "testnet",
                order.account_fingerprint,
                "BTCUSDT",
                "synthetic-order",
                f"synthetic-{mutation}",
                trade_key,
                order.client_order_id,
                "1m",
                order.side.value,
                "0.00001",
                "1",
                "0.00001",
                "0",
                "USDT",
                6_500,
                None,
                "PENDING",
                "{}",
                "f" * 64,
            ),
        )
    store._connection.commit()
    with pytest.raises(
        (
            Stage4SellRecoveryPreconditionError,
            PersistenceIntegrityError,
            ExchangeOrderStateError,
        )
    ):
        dispose_stage4_sell_no_execution(
            store,
            key,
            reason_code="operator_disposition",
            occurred_at_ms=7_000,
            authorization=authorization(),
        )
    assert (
        store._connection.execute(
            "SELECT status FROM exchange_orders WHERE durable_command_key=?", (key,)
        ).fetchone()[0]
        == "PREPARED"
    )
    store.close()


@pytest.mark.parametrize("boundary", ["stage4_sell_disposition_before_commit"])
def test_disposition_baseexception_rolls_back(tmp_path: Path, boundary: str) -> None:
    path = tmp_path / "rollback.sqlite3"
    store, key = prepared_disposition(path)
    store.close()
    crashing = SQLiteExchangePersistence(
        SQLitePersistenceConfig(path, expected_schema_version=9),
        failure_injector=lambda value: (
            (_ for _ in ()).throw(KeyboardInterrupt()) if value == boundary else None
        ),
    )
    with pytest.raises(KeyboardInterrupt):
        dispose_stage4_sell_no_execution(
            crashing,
            key,
            reason_code="operator_disposition",
            occurred_at_ms=7_000,
            authorization=authorization(),
        )
    crashing.close()
    reopened = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=9))
    assert reopened.load_order(key).order.status.value == "PREPARED"
    assert (
        reopened._connection.execute(
            "SELECT count(*) FROM exchange_managed_sell_dispositions"
        ).fetchone()[0]
        == 0
    )
    reopened.close()


def test_disposition_rejects_conflicts_and_terminal_order_cannot_submit(tmp_path: Path) -> None:
    store, key = prepared_disposition(tmp_path / "conflict.sqlite3")
    dispose_stage4_sell_no_execution(
        store,
        key,
        reason_code="operator_disposition",
        occurred_at_ms=7_000,
        authorization=authorization(),
    )
    with pytest.raises(PersistenceConflictError):
        dispose_stage4_sell_no_execution(
            store,
            key,
            reason_code="different_reason",
            occurred_at_ms=7_001,
            authorization=authorization(),
        )
    with pytest.raises(PersistenceConflictError):
        store.begin_submission(key, occurred_at_ms=7_002)
    store.close()


def test_generic_status_transition_cannot_bypass_disposition_service(tmp_path: Path) -> None:
    store, key = prepared_disposition(tmp_path / "generic-bypass.sqlite3")
    with pytest.raises(PersistenceConflictError, match="ADR-069"):
        store.record_status(
            key,
            status=ExchangeOrderStatus.DISPOSED_NO_EXECUTION,
            occurred_at_ms=7_000,
            reason_code="bypass",
        )
    assert store.load_order(key).order.status is ExchangeOrderStatus.PREPARED
    store.close()


def test_authorization_is_separate_and_consumed_before_mutation(tmp_path: Path) -> None:
    first, first_key = prepared_disposition(tmp_path / "authorization-first.sqlite3")
    second, second_key = prepared_disposition(tmp_path / "authorization-second.sqlite3")
    with pytest.raises(Stage4SellDispositionError):
        dispose_stage4_sell_no_execution(
            first,
            first_key,
            reason_code="operator_disposition",
            occurred_at_ms=7_000,
            authorization=Stage4SellRecoveryAuthorization.from_environment(
                {"STAGE4_SELL_RECOVERY_AUTHORIZED": "YES"}
            ),  # type: ignore[arg-type]
        )
    with pytest.raises(Stage4SellDispositionError):
        dispose_stage4_sell_no_execution(
            first,
            first_key,
            reason_code="operator_disposition",
            occurred_at_ms=7_000,
            authorization=SELL_AUTHORIZATION_VALUE,  # type: ignore[arg-type]
        )
    with pytest.raises(Stage4SellDispositionError):
        dispose_stage4_sell_no_execution(
            first,
            first_key,
            reason_code="operator_disposition",
            occurred_at_ms=7_000,
            authorization=Stage4SellContinuationAuthorization.from_environment(
                {"STAGE4_SELL_CONTINUATION_AUTHORIZED": "YES"}
            ),  # type: ignore[arg-type]
        )
    ceremony = authorization()
    dispose_stage4_sell_no_execution(
        first,
        first_key,
        reason_code="operator_disposition",
        occurred_at_ms=7_000,
        authorization=ceremony,
    )
    with pytest.raises(ExchangeCapabilityError, match="already consumed"):
        dispose_stage4_sell_no_execution(
            second,
            second_key,
            reason_code="operator_disposition",
            occurred_at_ms=7_000,
            authorization=ceremony,
        )
    assert second.load_order(second_key).order.status is ExchangeOrderStatus.PREPARED
    first.close()
    second.close()


@pytest.mark.parametrize("field", ["payload", "payload_digest"])
def test_disposition_replay_and_restart_reject_journal_corruption(
    tmp_path: Path, field: str
) -> None:
    path = tmp_path / f"corrupt-{field}.sqlite3"
    store, key = prepared_disposition(path)
    dispose_stage4_sell_no_execution(
        store,
        key,
        reason_code="operator_disposition",
        occurred_at_ms=7_000,
        authorization=authorization(),
    )
    store._connection.execute("DROP TRIGGER exchange_managed_sell_dispositions_no_update")
    store._connection.execute(
        f"UPDATE exchange_managed_sell_dispositions SET {field}=?",
        ("{}" if field == "payload" else "0" * 64,),
    )
    store._connection.commit()
    with pytest.raises(PersistenceIntegrityError):
        dispose_stage4_sell_no_execution(
            store,
            key,
            reason_code="operator_disposition",
            occurred_at_ms=7_001,
            authorization=authorization(),
        )
    store.close()
    reopened = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=9))
    with pytest.raises(PersistenceIntegrityError):
        reopened.recover_local()
    reopened.close()


def test_concurrent_identical_disposition_has_one_durable_record(tmp_path: Path) -> None:
    path = tmp_path / "concurrent.sqlite3"
    initial, key = prepared_disposition(path)
    initial.close()
    barrier = threading.Barrier(2)
    results: list[bool] = []
    failures: list[BaseException] = []

    def worker() -> None:
        store = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=9))
        try:
            barrier.wait()
            result = dispose_stage4_sell_no_execution(
                store,
                key,
                reason_code="operator_disposition",
                occurred_at_ms=7_000,
                authorization=authorization(),
            )
            results.append(result.replayed)
        except BaseException as error:  # noqa: BLE001 - thread failures are asserted below.
            failures.append(error)
        finally:
            store.close()

    first = threading.Thread(target=worker)
    second = threading.Thread(target=worker)
    first.start()
    second.start()
    first.join()
    second.join()
    assert not failures
    assert sorted(results) == [False, True]
    reopened = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=9))
    assert (
        reopened._connection.execute(
            "SELECT count(*) FROM exchange_managed_sell_dispositions"
        ).fetchone()[0]
        == 1
    )
    assert reopened.load_order(key).order.status is ExchangeOrderStatus.DISPOSED_NO_EXECUTION
    reopened.close()
