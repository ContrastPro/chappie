import json
import random

import httpx
import pytest
from test_m48_fullgraph_recovery import (
    KEY,
    NOW,
    RemoteExchange,
    build_process,
    drive_buy,
    durable_counts,
    initialize_and_clear,
)

from app.exchange_execution import ExchangeOrderStatus
from app.market.events import CandleClosed, CandleClosedReason, MarketEventSource
from app.market.models import CandleSnapshot
from app.persistence import SQLiteExchangePersistence, SQLitePersistenceConfig


def assert_durable_invariants(path, *, expected_submissions: int | None = None) -> None:
    store = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=4))
    assert store._connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert store._connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert store.recover_local().status.value == "CLEAN"
    orders = store.list_orders()
    assert len({item.order.durable_command_key for item in orders}) == len(orders)
    assert len({item.order.client_order_id for item in orders}) == len(orders)
    for item in orders:
        versions = tuple(
            row[0]
            for row in store._connection.execute(
                "SELECT order_version FROM exchange_order_events "
                "WHERE durable_command_key=? ORDER BY order_version",
                (item.order.durable_command_key,),
            )
        )
        assert versions == tuple(range(1, item.version + 1))
        attempts = store._connection.execute(
            "SELECT count(*) FROM exchange_submission_attempts WHERE durable_command_key=?",
            (item.order.durable_command_key,),
        ).fetchone()[0]
        assert attempts <= 1
    if expected_submissions is not None:
        assert (
            store._connection.execute(
                "SELECT count(*) FROM exchange_submission_attempts"
            ).fetchone()[0]
            == expected_submissions
        )
    for row in store._connection.execute(
        "SELECT result_payload FROM exchange_reconciliation_runs"
    ).fetchall():
        payload = json.loads(row[0])
        assert payload["generation_after"] >= payload["generation_before"]
        assert payload["generation_after"] <= payload["generation_before"] + 1
    store.close()


def create_ambiguous_order(path, remote: RemoteExchange) -> str:
    initialize_and_clear(path, remote)
    remote.not_found = True
    normal_handler = remote.handler

    def ambiguous_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            normal_handler(request)
            return httpx.Response(503, json={"code": -1000, "msg": "unknown"})
        return normal_handler(request)

    process, provider, client = build_process(path, remote, handler=ambiguous_handler)
    drive_buy(process)
    process.close()
    client.close()
    assert provider.close_calls == 1
    order, attempts, session = durable_counts(path)
    assert order.order.status is ExchangeOrderStatus.RECONCILIATION_REQUIRED
    assert attempts == session.submission_count == remote.submissions == 1
    return order.order.client_order_id


def test_twenty_not_found_full_graph_restarts_never_resubmit(tmp_path) -> None:
    path = tmp_path / "twenty-not-found.sqlite3"
    remote = RemoteExchange()
    client_id = create_ambiguous_order(path, remote)
    for _ in range(20):
        process, provider, client = build_process(path, remote)
        assert process.testnet is not None
        assert process.testnet.runtime_availability == "RECOVERY_ONLY"
        assert process.testnet.readiness.capability is None
        assert process.testnet.application_service is None
        assert process.testnet.consumer is None
        assert process.coordinator is None
        assert process.status_report().testnet_non_terminal_order_count == 1
        process.close()
        client.close()
        assert provider.close_calls == 1
    order, attempts, session = durable_counts(path)
    assert order.order.status is ExchangeOrderStatus.RECONCILIATION_REQUIRED
    assert order.order.client_order_id == client_id
    assert attempts == session.submission_count == remote.submissions == 1
    assert remote.queried_client_ids == [client_id] * 20
    assert_durable_invariants(path, expected_submissions=1)


@pytest.mark.parametrize(
    ("remote_status", "expected_status", "ready"),
    [
        ("NEW", ExchangeOrderStatus.ACKNOWLEDGED, False),
        ("PARTIALLY_FILLED", ExchangeOrderStatus.PARTIALLY_FILLED, False),
        ("FILLED", ExchangeOrderStatus.FILLED, True),
    ],
)
def test_not_found_then_found_resolves_original_lineage_without_resubmit(
    tmp_path, remote_status: str, expected_status: ExchangeOrderStatus, ready: bool
) -> None:
    path = tmp_path / f"not-found-then-{remote_status.lower()}.sqlite3"
    remote = RemoteExchange()
    client_id = create_ambiguous_order(path, remote)
    for _ in range(7):
        process, _, client = build_process(path, remote)
        assert process.testnet is not None
        assert process.testnet.runtime_availability == "RECOVERY_ONLY"
        process.close()
        client.close()
    remote.not_found = False
    if remote_status in {"FILLED", "PARTIALLY_FILLED"}:
        remote.set_executed(remote_status)
    else:
        remote.status = remote_status
    found, provider, client = build_process(path, remote)
    assert found.testnet is not None
    assert found.testnet.submission_ready is ready
    found.close()
    client.close()
    assert provider.close_calls == 1
    order, attempts, session = durable_counts(path)
    assert order.order.status is expected_status
    assert order.order.client_order_id == client_id
    assert attempts == session.submission_count == remote.submissions == 1
    assert remote.queried_client_ids == [client_id] * 8
    assert_durable_invariants(path, expected_submissions=1)


def test_duplicate_decision_one_hundred_times_and_after_dedup_eviction(tmp_path) -> None:
    path = tmp_path / "duplicate-one-process.sqlite3"
    remote = RemoteExchange()
    initialize_and_clear(path, remote)
    process, provider, client = build_process(path, remote)
    drive_buy(process)
    latest = process.cache.get_latest_candle(KEY)
    assert latest is not None and process.coordinator is not None
    duplicate = CandleClosed(
        "fullgraph-buy-event",
        NOW,
        KEY,
        "fullgraph-cycle",
        MarketEventSource.REPLAY,
        CandleSnapshot.from_candle(latest),
        CandleClosedReason.TIME_CONFIRMED,
    )
    for _ in range(99):
        process.coordinator.process_event(duplicate)
    assert remote.submissions == 1
    process.close()
    client.close()
    assert provider.close_calls == 1
    order, attempts, session = durable_counts(path)
    assert order.order.status is ExchangeOrderStatus.ACKNOWLEDGED
    assert attempts == session.submission_count == 1
    assert_durable_invariants(path, expected_submissions=1)


def test_durable_identity_survives_ten_fresh_process_registries(tmp_path) -> None:
    path = tmp_path / "duplicate-ten-processes.sqlite3"
    remote = RemoteExchange()
    initialize_and_clear(path, remote)
    first, _, first_client = build_process(path, remote)
    drive_buy(first)
    first.close()
    first_client.close()
    remote.set_executed("FILLED")
    recovered, _, recovered_client = build_process(path, remote)
    assert recovered.testnet is not None and recovered.testnet.submission_ready
    recovered.close()
    recovered_client.close()
    original, _, _ = durable_counts(path)
    client_id = original.order.client_order_id
    for _ in range(10):
        process, provider, client = build_process(path, remote)
        assert process.testnet is not None and process.testnet.submission_ready
        drive_buy(process)
        process.close()
        client.close()
        assert provider.close_calls == 1
    final, attempts, session = durable_counts(path)
    assert final.order.client_order_id == client_id
    assert attempts == session.submission_count == remote.submissions == 1
    assert_durable_invariants(path, expected_submissions=1)


def test_recovery_only_and_ready_twenty_restart_stress(tmp_path) -> None:
    blocked_path = tmp_path / "recovery-only-twenty.sqlite3"
    blocked_remote = RemoteExchange()
    capabilities = []
    for _ in range(20):
        process, provider, client = build_process(blocked_path, blocked_remote)
        assert process.testnet is not None
        assert process.testnet.runtime_availability == "RECOVERY_ONLY"
        assert process.testnet.readiness.capability is None
        assert process.coordinator is None
        process.close()
        client.close()
        assert provider.close_calls == 1
    assert blocked_remote.submissions == 0
    assert_durable_invariants(blocked_path, expected_submissions=0)

    ready_path = tmp_path / "ready-twenty.sqlite3"
    ready_remote = RemoteExchange()
    initialize_and_clear(ready_path, ready_remote)
    for _ in range(20):
        process, provider, client = build_process(ready_path, ready_remote)
        assert process.testnet is not None and process.testnet.submission_ready
        capability = process.testnet.readiness.capability
        assert capability is not None
        capabilities.append(capability)
        process.close()
        client.close()
        assert provider.close_calls == 1
    assert len({id(value) for value in capabilities}) == 20
    assert ready_remote.submissions == 0
    assert_durable_invariants(ready_path, expected_submissions=0)


def test_seeded_restart_schedule_preserves_submission_invariants(tmp_path) -> None:
    schedule = random.Random(49)
    for case in range(24):
        path = tmp_path / f"seeded-{case}.sqlite3"
        remote = RemoteExchange()
        client_id = create_ambiguous_order(path, remote)
        not_found_count = schedule.randint(1, 8)
        for _ in range(not_found_count):
            process, _, client = build_process(path, remote)
            assert process.testnet is not None
            assert process.testnet.runtime_availability == "RECOVERY_ONLY"
            process.close()
            client.close()
        if schedule.choice((True, False)):
            remote.not_found = False
            terminal = schedule.choice(("NEW", "PARTIALLY_FILLED", "FILLED"))
            if terminal in {"PARTIALLY_FILLED", "FILLED"}:
                remote.set_executed(terminal)
            else:
                remote.status = terminal
            process, _, client = build_process(path, remote)
            process.close()
            client.close()
        order, attempts, session = durable_counts(path)
        assert order.order.client_order_id == client_id
        assert attempts == session.submission_count == remote.submissions == 1
        assert_durable_invariants(path, expected_submissions=1)
