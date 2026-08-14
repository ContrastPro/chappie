import copy
import hashlib
import json
import pickle
import sqlite3
import threading
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from app.exchange_execution import (
    ExchangeCommand,
    ExchangeInstrumentRules,
    ExchangeOrderSide,
    ExchangeOrderStatus,
    ExchangeTrade,
    ExchangeTradeIdentity,
    ExecutionEnvironment,
    PendingExposure,
    ReviewedStage4ManagedSellRequest,
    Stage4ManagedSellAuthority,
    Stage4ManagedSellAuthorityEvent,
    Stage4ManagedSellProcessClaim,
    Stage4ManagedSellRiskEnvelope,
    SubmissionObservation,
    TransportOutcomeKind,
    derive_stage4_sell_economic_slot,
)
from app.exchange_execution.durable import DurableExchangeOrderLifecycle
from app.exchange_execution.exceptions import ExchangeCapabilityError
from app.exchange_execution.stage4_sell import Stage4ManagedSellReviewError
from app.exchange_runtime.settlement import SQLiteExchangeSettlement
from app.exchange_runtime.submission import (
    PreSubmitBlockReason,
)
from app.exchange_runtime.submission import (
    TestnetPreSubmitGate as PreSubmitGate,
)
from app.market.models import MarketStreamKey
from app.persistence import SQLiteExchangePersistence, SQLitePersistenceConfig, canonical_sha256
from app.persistence.exceptions import (
    PersistenceIntegrityError,
    PersistenceMigrationError,
    Stage4ManagedInventoryMismatchError,
    Stage4ManagedSellAuthorityError,
    Stage4ManagedSellStalePredecessorError,
)
from app.persistence.sqlite import SQLiteTradingPersistence
from app.risk.exchange import (
    ExchangeReconciliationStatus,
    ExchangeRiskAssessment,
    ExchangeRiskContext,
    ExchangeRiskOutcome,
    ReconciledAccountState,
)
from app.tools import testnet_phase4_remediate_zero_fee as remediation
from app.trading.models import RiskDecisionOutcome
from tests.test_testnet_phase4 import run_local_remediation, zero_fee_blocked_database

D = Decimal

MIGRATION_HASHES = {
    "001_initial.sql": "1c44bf2c187c26b01837787b50bca5bf952a7cd9fb9ed17b8377952e090fa433",
    "002_exchange_execution.sql": "c833d949fd1fab7b4a90990ff3a1a743be655ada7b1fd162a51866a478f7040c",
    "003_exchange_reconciliation.sql": "05e19fb9ee43f992f6efb15cab537b253d27aa2ea636024cc5743852cf0a4bb1",
    "004_exchange_settlement.sql": "4fb52f23e9c4fde653d26170a88c6d8e006062653188b3380ac57a0fec410a4e",
    "005_phase3_smoke_authority.sql": "57c1422ffa74e7f0975ba2d271793088da591ab843f4380d79e1bbcb7d8f5345",
    "006_zero_commission_resolution.sql": "7b6543b489fd9ca67435093e3e34f5fde48101803e65cd502ae5c1004af80f1d",
    "007_stage4_managed_sell_authority.sql": "5133bf077d65093820e111deafd5332b83b15d3bc808ab43f5aaf8ad5e4d32fa",
    "008_stage4_sell_recovery.sql": "ebefdea2ebceb9d43581c6f21303129263a5b6bb4a94b40c01b497941f77d92b",
    "009_stage4_sell_disposition.sql": "0efe056e055f4b5a925309d5dffdb5a5485cab60169457a60fd8249e83c0b636",
}


def test_migration_contract_hashes_are_stable() -> None:
    root = Path("app/persistence/migrations")
    assert {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.glob("*.sql"))
    } == MIGRATION_HASHES


def test_failed_migration_007_rolls_back_and_leaves_valid_v6(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "migration-failure.sqlite3"
    v6 = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=6))
    v6.close()
    original = SQLiteTradingPersistence._migration

    def broken(name: str = "001_initial.sql") -> tuple[str, str]:
        if name == "007_stage4_managed_sell_authority.sql":
            sql = "CREATE TABLE should_rollback(value TEXT); INVALID SQL;"
            return sql, hashlib.sha256(sql.encode()).hexdigest()
        return original(name)

    monkeypatch.setattr(SQLiteTradingPersistence, "_migration", staticmethod(broken))
    with pytest.raises(PersistenceMigrationError):
        SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=7))
    monkeypatch.undo()
    reopened = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=6))
    assert (
        reopened._connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
        == 6
    )
    assert (
        reopened._connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE name='should_rollback'"
        ).fetchone()[0]
        == 0
    )
    reopened.close()


def predecessor(path: Path) -> SQLiteExchangePersistence:
    zero_fee_blocked_database(path)
    run_local_remediation(path)
    return SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=7))


def reviewed(store: SQLiteExchangePersistence, **changes) -> ReviewedStage4ManagedSellRequest:
    root = store._connection.execute("SELECT * FROM exchange_execution_roots").fetchone()
    session = store._connection.execute("SELECT * FROM exchange_sessions").fetchone()
    current = store._connection.execute("SELECT * FROM exchange_portfolio_current").fetchone()
    state = json.loads(current["state_payload"])
    quantity = D(state["base_balance"]["$decimal"])
    source = store._connection.execute(
        "SELECT o.durable_command_key,t.exchange_trade_id FROM exchange_orders o "
        "JOIN exchange_trades t ON t.durable_command_key=o.durable_command_key WHERE o.side='BUY'"
    ).fetchone()
    rules = ExchangeInstrumentRules(
        MarketStreamKey("BTCUSDT", root["interval"]),
        D("0.00001"),
        D("0.00001"),
        D("5"),
        "a" * 64,
        D("1000"),
    )
    notional = D("10")
    risk = "b" * 64
    command = ExchangeCommand(
        "c" * 64,
        "d" * 64,
        "e" * 64,
        rules.stream_key,
        ExchangeOrderSide.SELL,
        quantity,
        notional / quantity,
        D(0),
        notional,
        risk,
        rules.rules_version,
        root["configuration_fingerprint"],
        4000,
        "stage4-sell-test",
    )
    settlement_values = {
        "portfolio_version": current["portfolio_version"],
        "portfolio_digest": current["state_digest"],
        "reconciliation_generation": 2,
        "pending": 0,
        "blocked": 0,
        "status": "SYNCHRONIZED",
    }
    envelope = Stage4ManagedSellRiskEnvelope(
        quantity,
        notional,
        D(session["submitted_notional"]),
        D(session["executed_turnover"]),
    )
    values = {
        "authority_id": "stage4-sell-authority",
        "operator_run_id": "stage4-sell-run",
        "execution_root_id": root["execution_root_id"],
        "session_id": session["session_id"],
        "account_fingerprint": root["account_fingerprint"],
        "portfolio_id": current["portfolio_id"],
        "portfolio_version": current["portfolio_version"],
        "portfolio_digest": current["state_digest"],
        "exact_managed_quantity": quantity,
        "expected_submission_count": session["submission_count"],
        "permitted_submission_count": session["submission_count"] + 1,
        "control_generation": 0,
        "reconciliation_generation": 2,
        "settlement_state_digest": canonical_sha256(
            settlement_values, algorithm_version="stage4-sell-settlement-state/v1"
        ),
        "source_buy_command_key": source["durable_command_key"],
        "source_buy_trade_id": source["exchange_trade_id"],
        "economic_slot_key": derive_stage4_sell_economic_slot(
            root["execution_root_id"],
            root["account_fingerprint"],
            current["portfolio_id"],
            current["portfolio_version"],
            current["state_digest"],
            source["exchange_trade_id"],
        ),
        "rules": rules,
        "risk_fingerprint": risk,
        "config_fingerprint": root["configuration_fingerprint"],
        "ownership_scope_digest": "f" * 64,
        "command": command,
        "envelope": envelope,
        "created_at_ms": 4000,
    }
    return ReviewedStage4ManagedSellRequest(**{**values, **changes})


def test_schema_v7_authority_is_atomic_deterministic_and_recovery_only(tmp_path: Path) -> None:
    store = predecessor(tmp_path / "sell.sqlite3")
    request = reviewed(store)
    receipt = store.authorize_stage4_managed_sell_command(request)
    assert receipt.claim is not None
    assert receipt.client_order_id.startswith("ctt1t-")
    assert (
        store.load_order(request.command.durable_command_key).order.side is ExchangeOrderSide.SELL
    )
    assert store.recover_local().reconciliation_required[0].order.side is ExchangeOrderSide.SELL
    with pytest.raises(TypeError):
        copy.copy(receipt.claim)
    with pytest.raises(TypeError):
        copy.deepcopy(receipt.claim)
    with pytest.raises(TypeError):
        pickle.dumps(receipt.claim)
    store.close()

    reopened = SQLiteExchangePersistence(
        SQLitePersistenceConfig(tmp_path / "sell.sqlite3", expected_schema_version=7)
    )
    observed = reopened.authorize_stage4_managed_sell_command(reviewed(reopened))
    assert observed.recovered and observed.claim is None
    assert len(reopened.list_orders()) == 2
    reopened.close()


def test_managed_inventory_and_full_position_are_fail_closed(tmp_path: Path) -> None:
    store = predecessor(tmp_path / "inventory.sqlite3")
    request = reviewed(store)
    with pytest.raises(Stage4ManagedInventoryMismatchError):
        store.authorize_stage4_managed_sell_command(
            replace(
                request,
                exact_managed_quantity=D("0.00015"),
                command=replace(
                    request.command, base_quantity=D("0.00015"), approved_notional=D("9.375")
                ),
                envelope=replace(
                    request.envelope, quantity=D("0.00015"), reviewed_notional=D("9.375")
                ),
            )
        )
    with pytest.raises(Stage4ManagedSellReviewError, match="maxQty"):
        replace(
            request,
            rules=replace(request.rules, max_quantity=D("0.00015")),
            command=replace(
                request.command, instrument_rules_fingerprint=request.rules.rules_version
            ),
        )
    store.close()


@pytest.mark.parametrize("quantity", ["0.00015999", "0.00015", "0.00017", "1.00016"])
def test_every_nonexact_managed_sell_quantity_is_rejected(tmp_path: Path, quantity: str) -> None:
    store = predecessor(tmp_path / f"nonexact-{quantity}.sqlite3")
    request = reviewed(store)
    candidate = D(quantity)
    notional = candidate * request.command.reference_price
    with pytest.raises((Stage4ManagedSellReviewError, Stage4ManagedInventoryMismatchError)):
        store.authorize_stage4_managed_sell_command(
            replace(
                request,
                exact_managed_quantity=candidate,
                command=replace(
                    request.command, base_quantity=candidate, approved_notional=notional
                ),
                envelope=replace(request.envelope, quantity=candidate, reviewed_notional=notional),
            )
        )
    assert (
        store._connection.execute(
            "SELECT count(*) FROM exchange_orders WHERE side='SELL'"
        ).fetchone()[0]
        == 0
    )
    store.close()


@pytest.mark.parametrize("lineage", ["root", "session", "portfolio", "account"])
def test_copied_balances_cannot_create_a_new_sell_lineage(tmp_path: Path, lineage: str) -> None:
    store = predecessor(tmp_path / f"lineage-{lineage}.sqlite3")
    request = reviewed(store)
    changes: dict[str, object] = {}
    if lineage == "root":
        changes["execution_root_id"] = "copied-root"
    elif lineage == "session":
        changes["session_id"] = "copied-session"
    elif lineage == "portfolio":
        changes["portfolio_id"] = "copied-portfolio"
    else:
        changes["account_fingerprint"] = "copied-account"
    root = changes.get("execution_root_id", request.execution_root_id)
    account = changes.get("account_fingerprint", request.account_fingerprint)
    portfolio_id = changes.get("portfolio_id", request.portfolio_id)
    changes["economic_slot_key"] = derive_stage4_sell_economic_slot(
        root,  # type: ignore[arg-type]
        account,  # type: ignore[arg-type]
        portfolio_id,  # type: ignore[arg-type]
        request.portfolio_version,
        request.portfolio_digest,
        request.source_buy_trade_id,
    )
    with pytest.raises(Stage4ManagedSellStalePredecessorError):
        store.authorize_stage4_managed_sell_command(replace(request, **changes))
    assert (
        store._connection.execute(
            "SELECT count(*) FROM exchange_orders WHERE side='SELL'"
        ).fetchone()[0]
        == 0
    )
    store.close()


def test_sell_domain_models_reject_every_broader_or_stale_shape(tmp_path: Path) -> None:
    store = predecessor(tmp_path / "domain.sqlite3")
    request = reviewed(store)
    invalid_envelopes = (
        {"quantity": "0.1"},
        {"reviewed_notional": D(0)},
        {"max_new_in_flight": 2},
        {"side": "BUY"},
    )
    for changes in invalid_envelopes:
        with pytest.raises(Stage4ManagedSellReviewError):
            replace(request.envelope, **changes)
    invalid_requests = (
        {"portfolio_version": True},
        {"permitted_submission_count": request.expected_submission_count + 2},
        {"rules": object()},
        {"command": replace(request.command, side=ExchangeOrderSide.BUY)},
        {"command": replace(request.command, base_quantity=D("0.00015"))},
        {
            "exact_managed_quantity": D("0.000165"),
            "command": replace(request.command, base_quantity=D("0.000165")),
            "envelope": replace(request.envelope, quantity=D("0.000165")),
        },
        {"command": replace(request.command, approved_notional=D(1))},
        {"command": replace(request.command, instrument_rules_fingerprint="0" * 64)},
        {"command": replace(request.command, risk_authorization_fingerprint="0" * 64)},
        {"command": replace(request.command, execution_configuration_fingerprint="0" * 64)},
        {"economic_slot_key": "0" * 64},
        {"policy_version": "unsupported"},
    )
    for changes in invalid_requests:
        with pytest.raises((Stage4ManagedSellReviewError, TypeError)):
            replace(request, **changes)
    with pytest.raises(Stage4ManagedSellReviewError):
        derive_stage4_sell_economic_slot(
            request.execution_root_id,
            request.account_fingerprint,
            request.portfolio_id,
            0,
            request.portfolio_digest,
            request.source_buy_trade_id,
        )

    authority = Stage4ManagedSellAuthority(
        request, request.command.durable_command_key, 4000, "a" * 64
    )
    assert authority.reviewed_request is request
    with pytest.raises(Stage4ManagedSellReviewError):
        Stage4ManagedSellAuthority(object(), "a" * 64, 1, "b" * 64)  # type: ignore[arg-type]
    with pytest.raises(Stage4ManagedSellReviewError):
        Stage4ManagedSellAuthority(request, request.command.durable_command_key, 3999, "a" * 64)
    event = Stage4ManagedSellAuthorityEvent(
        "event", request.authority_id, request.command.durable_command_key, "run", 4000, "a" * 64
    )
    assert event.event_type == "CONSUMED"
    with pytest.raises(Stage4ManagedSellReviewError):
        replace(event, event_ordinal=2)
    with pytest.raises(ExchangeCapabilityError):
        Stage4ManagedSellProcessClaim(request)
    claim = Stage4ManagedSellProcessClaim._issue(request)
    claim._consume()
    with pytest.raises(ExchangeCapabilityError):
        claim._consume()
    with pytest.raises(TypeError):
        claim.__getstate__()
    store.close()


def test_session_predecessor_and_economic_slot_are_single_use(tmp_path: Path) -> None:
    store = predecessor(tmp_path / "slot.sqlite3")
    request = reviewed(store)
    for predecessor_count in (0, 2):
        with pytest.raises(Stage4ManagedSellReviewError):
            replace(
                request,
                expected_submission_count=predecessor_count,
                permitted_submission_count=predecessor_count + 1,
            )
    store.authorize_stage4_managed_sell_command(request)
    conflict = replace(
        request,
        authority_id="other-authority",
        operator_run_id="other-run",
        command=replace(
            request.command,
            durable_command_key="1" * 64,
            stable_decision_key="2" * 64,
            decision_slot_key="3" * 64,
        ),
    )
    with pytest.raises(Stage4ManagedSellAuthorityError):
        store.authorize_stage4_managed_sell_command(conflict)
    store.close()


def test_stale_reviewed_count_cannot_follow_a_durable_count_change(tmp_path: Path) -> None:
    store = predecessor(tmp_path / "stale-count.sqlite3")
    request = reviewed(store)
    store._connection.execute(
        "UPDATE exchange_sessions SET submission_count=2 WHERE session_id=?",
        (request.session_id,),
    )
    store._connection.commit()
    with pytest.raises(Stage4ManagedSellStalePredecessorError, match="session count"):
        store.authorize_stage4_managed_sell_command(request)
    assert (
        store._connection.execute(
            "SELECT count(*) FROM exchange_orders WHERE side='SELL'"
        ).fetchone()[0]
        == 0
    )
    store.close()


@pytest.mark.parametrize(
    "boundary",
    [
        "stage4_sell_before_authority",
        "stage4_sell_after_authority",
        "stage4_sell_after_command",
        "stage4_sell_after_prepared_event",
        "stage4_sell_after_reservation",
        "stage4_sell_after_linkage",
        "stage4_sell_after_audit",
        "stage4_sell_before_commit",
    ],
)
@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt, SystemExit, GeneratorExit])
def test_precommit_baseexception_rolls_back(tmp_path: Path, boundary: str, error_type) -> None:
    path = tmp_path / f"{boundary}-{error_type.__name__}.sqlite3"
    base = predecessor(path)
    request = reviewed(base)
    base.close()

    def fail(name: str) -> None:
        if name == boundary:
            raise error_type(name)

    store = SQLiteExchangePersistence(
        SQLitePersistenceConfig(path, expected_schema_version=7), failure_injector=fail
    )
    with pytest.raises(error_type):
        store.authorize_stage4_managed_sell_command(request)
    store.close()
    reopened = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=7))
    assert (
        reopened._connection.execute(
            "SELECT count(*) FROM exchange_managed_sell_authorities"
        ).fetchone()[0]
        == 0
    )
    assert reopened._connection.execute("SELECT count(*) FROM exchange_orders").fetchone()[0] == 1
    reopened.close()


@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt, SystemExit, GeneratorExit])
def test_postcommit_acknowledgement_loss_keeps_one_recovery_only_sell(
    tmp_path: Path, error_type
) -> None:
    path = tmp_path / f"postcommit-{error_type.__name__}.sqlite3"
    base = predecessor(path)
    request = reviewed(base)
    base.close()

    def fail(name: str) -> None:
        if name == "stage4_sell_after_commit":
            raise error_type(name)

    store = SQLiteExchangePersistence(
        SQLitePersistenceConfig(path, expected_schema_version=7), failure_injector=fail
    )
    with pytest.raises(error_type):
        store.authorize_stage4_managed_sell_command(request)
    store.close()
    reopened = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=7))
    receipt = reopened.authorize_stage4_managed_sell_command(request)
    assert receipt.recovered and receipt.claim is None
    assert (
        reopened._connection.execute(
            "SELECT count(*) FROM exchange_managed_sell_authorities"
        ).fetchone()[0]
        == 1
    )
    assert (
        reopened._connection.execute(
            "SELECT count(*) FROM exchange_orders WHERE side='SELL'"
        ).fetchone()[0]
        == 1
    )
    reopened.close()


def test_two_sqlite_actors_observe_one_economic_slot_and_one_reservation(tmp_path: Path) -> None:
    path = tmp_path / "concurrent.sqlite3"
    base = predecessor(path)
    request = reviewed(base)
    base.close()
    barrier = threading.Barrier(2)
    receipts = []
    failures = []

    def actor() -> None:
        store = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=7))
        try:
            barrier.wait(timeout=5)
            receipts.append(store.authorize_stage4_managed_sell_command(request))
        except (
            PersistenceIntegrityError,
            Stage4ManagedSellAuthorityError,
            sqlite3.Error,
            threading.BrokenBarrierError,
        ) as error:
            failures.append(error)
        finally:
            store.close()

    actors = [threading.Thread(target=actor) for _ in range(2)]
    for actor_thread in actors:
        actor_thread.start()
    for actor_thread in actors:
        actor_thread.join(timeout=10)
    assert all(not actor_thread.is_alive() for actor_thread in actors)
    assert failures == []
    assert len(receipts) == 2
    assert sum(receipt.claim is not None for receipt in receipts) == 1
    assert sum(receipt.recovered and receipt.claim is None for receipt in receipts) == 1
    observed = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=7))
    assert (
        observed._connection.execute(
            "SELECT count(*) FROM exchange_managed_sell_authorities"
        ).fetchone()[0]
        == 1
    )
    assert (
        observed._connection.execute(
            "SELECT count(*) FROM exchange_orders WHERE side='SELL' AND terminal=0"
        ).fetchone()[0]
        == 1
    )
    assert (
        observed._connection.execute(
            "SELECT count(DISTINCT client_order_id) FROM exchange_orders WHERE side='SELL'"
        ).fetchone()[0]
        == 1
    )
    observed.close()


@pytest.mark.parametrize(
    "mutated_field",
    [
        None,
        "authority_id",
        "command_key",
        "execution_root_id",
        "session_id",
        "account_fingerprint",
        "portfolio_id",
        "portfolio_version",
        "portfolio_digest",
        "control_generation",
        "reconciliation_generation",
        "settlement_state_digest",
        "economic_slot_key",
        "rules_fingerprint",
        "risk_fingerprint",
        "config_fingerprint",
        "envelope_digest",
        "ownership_scope_digest",
        "environment",
    ],
)
def test_final_gate_allows_only_the_exact_live_single_use_sell_claim(
    tmp_path: Path, mutated_field: str | None
) -> None:
    store = predecessor(tmp_path / "gate.sqlite3")
    request = reviewed(store)
    receipt = store.authorize_stage4_managed_sell_command(request)
    assert receipt.claim is not None

    class OfflineGateway:
        calls = 0

        def submit_market_order(self, submission):
            self.calls += 1
            assert (
                store.load_order(submission.durable_command_key).order.status
                is ExchangeOrderStatus.SUBMITTING
            )
            assert store.load_session(receipt.claim.session_id).submission_count == 2
            return SubmissionObservation(
                TransportOutcomeKind.RESPONSE_RECEIVED,
                submission.client_order_id,
                "offline-sell-order",
                ExchangeOrderStatus.ACKNOWLEDGED,
                4001,
            )

    gateway = OfflineGateway()
    lifecycle = DurableExchangeOrderLifecycle(store, gateway, timestamp_provider=lambda: 4000)
    gate = PreSubmitGate(
        store,
        lifecycle,
        execution_root_id=request.execution_root_id,
        max_authorization_age_ms=1000,
    )
    session = store.load_session(request.session_id)
    context = ExchangeRiskContext(
        ReconciledAccountState(
            request.account_fingerprint,
            request.command.stream_key,
            "BTC",
            "USDT",
            request.exact_managed_quantity,
            D(0),
            D("14.890736"),
            D(0),
            ExchangeReconciliationStatus.SAFE,
            request.reconciliation_generation,
        ),
        PendingExposure(),
        session,
        None,
        0,
        0,
        request.control_generation,
    )
    assessment = ExchangeRiskAssessment(
        ExchangeRiskOutcome.APPROVED,
        RiskDecisionOutcome.APPROVED,
        "stage4-risk",
        request.session_id,
        (),
        None,
    )
    store.recover_local()
    assert context.execution_session == store.load_session(request.session_id)
    if mutated_field is not None:
        current = getattr(receipt.claim, mutated_field)
        wrong = current + 1 if isinstance(current, int) else "wrong"
        object.__setattr__(receipt.claim, mutated_field, wrong)
    first = gate.authorize(
        request.command.durable_command_key,
        assessment=assessment,
        context=context,
        capability=None,
        expected_rules_fingerprint=request.rules.rules_version,
        now_ms=4000,
        stage4_sell_claim=receipt.claim,
    )
    if mutated_field is not None:
        assert not first.authorized
        assert PreSubmitBlockReason.CONTROL_UNSAFE in first.reasons
        assert PreSubmitBlockReason.CAPABILITY_INVALID in first.reasons
        assert gateway.calls == 0
        store.close()
        return
    assert first.authorized
    assert first.authorization is not None
    lifecycle._submit_authorized(first.authorization)
    assert gateway.calls == 1
    assert store.load_session(request.session_id).submission_count == 2
    assert (
        store._connection.execute("SELECT count(*) FROM exchange_submission_attempts").fetchone()[0]
        == 2
    )
    replay = gate.authorize(
        request.command.durable_command_key,
        assessment=assessment,
        context=context,
        capability=None,
        expected_rules_fingerprint=request.rules.rules_version,
        now_ms=4000,
        stage4_sell_claim=receipt.claim,
    )
    assert PreSubmitBlockReason.CONTROL_UNSAFE in replay.reasons
    assert PreSubmitBlockReason.CAPABILITY_INVALID in replay.reasons
    store.record_status(
        request.command.durable_command_key,
        status=ExchangeOrderStatus.FILLED,
        occurred_at_ms=4010,
        reason_code="offline_authoritative_fill",
        exchange_order_id="offline-sell-order",
        acknowledged_at_ms=4001,
        cumulative_executed_base_quantity=request.exact_managed_quantity,
        cumulative_quote_quantity=D("10.4"),
        exchange_timestamp_ms=4010,
    )
    trade = ExchangeTrade(
        ExchangeTradeIdentity(
            "binance-spot",
            ExecutionEnvironment.TESTNET,
            request.account_fingerprint,
            "BTCUSDT",
            "offline-sell-order",
            "offline-sell-trade",
        ),
        receipt.client_order_id,
        request.command.stream_key,
        ExchangeOrderSide.SELL,
        request.exact_managed_quantity,
        D("65000"),
        D("10.4"),
        D(0),
        "BTC",
        4010,
    )
    store.persist_trades(request.command.durable_command_key, (trade,))
    managed, _ = remediation._managed_portfolio(store)
    settlement = SQLiteExchangeSettlement(
        store,
        managed,
        execution_root_id=request.execution_root_id,
        session_id=request.session_id,
        config_fingerprint=request.config_fingerprint,
        timestamp_provider=lambda: 4020,
    )
    settlement.initialize()
    result = settlement.settle_pending()
    assert result.status.value == "SYNCHRONIZED"
    assert managed.snapshot.base_balance == 0
    assert managed.snapshot.position is None
    assert managed.snapshot.quote_balance == D("25.290736")
    assert managed.snapshot.realised_pnl == D("0.290736")
    assert managed.snapshot.turnover == D("20.509264")
    assert managed.snapshot.total_fees == 0
    expected_snapshot = managed.snapshot
    store.close()

    reopened = SQLiteExchangePersistence(
        SQLitePersistenceConfig(tmp_path / "gate.sqlite3", expected_schema_version=7)
    )
    assert reopened.recover_local().reconciliation_required == ()
    recovered, _ = remediation._managed_portfolio(reopened)
    fresh = SQLiteExchangeSettlement(
        reopened,
        recovered,
        execution_root_id=request.execution_root_id,
        session_id=request.session_id,
        config_fingerprint=request.config_fingerprint,
        timestamp_provider=lambda: 4030,
    )
    fresh.initialize()
    assert recovered.snapshot == expected_snapshot
    assert fresh.settle_pending().settled_count == 0
    reopened.close()


def test_sql_constraints_and_append_only_triggers(tmp_path: Path) -> None:
    store = predecessor(tmp_path / "constraints.sqlite3")
    request = reviewed(store)
    store.authorize_stage4_managed_sell_command(request)
    with pytest.raises(sqlite3.IntegrityError):
        store._connection.execute(
            "UPDATE exchange_managed_sell_authorities SET side='BUY' WHERE authority_id=?",
            (request.authority_id,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        store._connection.execute(
            "DELETE FROM exchange_managed_sell_authorities WHERE authority_id=?",
            (request.authority_id,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        store._connection.execute(
            "UPDATE exchange_managed_sell_authority_events SET event_type='CONSUMED' "
            "WHERE authority_id=?",
            (request.authority_id,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        store._connection.execute(
            "DELETE FROM exchange_managed_sell_authority_events WHERE authority_id=?",
            (request.authority_id,),
        )
    store._connection.execute("DROP TRIGGER exchange_managed_sell_authorities_no_update")
    for column, value in (
        ("environment", "'live'"),
        ("venue", "'other'"),
        ("side", "'BUY'"),
        ("permitted_submission_count", "expected_submission_count+2"),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            store._connection.execute(
                f"UPDATE exchange_managed_sell_authorities SET {column}={value} "
                "WHERE authority_id=?",
                (request.authority_id,),
            )
    with pytest.raises(sqlite3.IntegrityError):
        store._connection.execute(
            "INSERT INTO exchange_managed_sell_authority_events "
            "SELECT NULL,event_id||'-broken','missing-authority',event_ordinal,event_type,"
            "durable_command_key,operator_run_id,occurred_at_ms,payload,payload_digest "
            "FROM exchange_managed_sell_authority_events WHERE authority_id=?",
            (request.authority_id,),
        )
    store.close()


def test_recovery_rejects_corrupted_sell_authority(tmp_path: Path) -> None:
    store = predecessor(tmp_path / "corrupt.sqlite3")
    request = reviewed(store)
    store.authorize_stage4_managed_sell_command(request)
    store._connection.execute("DROP TRIGGER exchange_managed_sell_authorities_no_update")
    store._connection.execute(
        "UPDATE exchange_managed_sell_authorities SET payload_digest=? WHERE authority_id=?",
        ("0" * 64, request.authority_id),
    )
    store._connection.commit()
    with pytest.raises(PersistenceIntegrityError):
        store.recover_local()
    store.close()


@pytest.mark.parametrize(
    ("table", "column", "value", "trigger"),
    [
        ("exchange_managed_sell_authorities", "economic_slot_key", "0" * 64, "authority"),
        ("exchange_managed_sell_authorities", "exact_managed_quantity", "0.00015", "authority"),
        (
            "exchange_managed_sell_authorities",
            "predecessor_portfolio_digest",
            "0" * 64,
            "authority",
        ),
        ("exchange_managed_sell_authorities", "control_generation", 1, "authority"),
        ("exchange_managed_sell_authorities", "reconciliation_generation", 3, "authority"),
        ("exchange_managed_sell_authorities", "rules_fingerprint", "0" * 64, "authority"),
        ("exchange_managed_sell_authorities", "risk_fingerprint", "0" * 64, "authority"),
        ("exchange_managed_sell_authorities", "config_fingerprint", "0" * 64, "authority"),
        ("exchange_managed_sell_authority_events", "payload_digest", "0" * 64, "event"),
        ("exchange_orders", "client_order_id", "ctt1t-aaaaaaaaaaaaaaaaaaaaaaaaaa", None),
    ],
)
def test_recovery_corruption_matrix_fails_closed(
    tmp_path: Path, table: str, column: str, value, trigger: str | None
) -> None:
    store = predecessor(tmp_path / f"corrupt-{column}.sqlite3")
    request = reviewed(store)
    store.authorize_stage4_managed_sell_command(request)
    if trigger == "authority":
        store._connection.execute("DROP TRIGGER exchange_managed_sell_authorities_no_update")
    elif trigger == "event":
        store._connection.execute("DROP TRIGGER exchange_managed_sell_authority_events_no_update")
    key = "authority_id" if table != "exchange_orders" else "durable_command_key"
    store._connection.execute(
        f"UPDATE {table} SET {column}=? WHERE {key}=?",
        (
            value,
            request.authority_id if key == "authority_id" else request.command.durable_command_key,
        ),
    )
    store._connection.commit()
    with pytest.raises(PersistenceIntegrityError):
        store.recover_local()
    store.close()
