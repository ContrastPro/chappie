import copy
import pickle
import sqlite3
from dataclasses import replace
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from app.exchange_execution import (
    DeterministicClientOrderIdFactory,
    DurableExchangeOrderLifecycle,
    ExchangeCommand,
    ExchangeOrderSide,
    ExecutionEnvironment,
    ExecutionSession,
    ExecutionSessionState,
    PendingExposure,
    Phase3BalanceScope,
    Phase3RiskEnvelope,
    ReviewedPhase3SmokeRequest,
    derive_phase3_economic_slot,
)
from app.exchange_execution.exceptions import ExchangeCapabilityError
from app.exchange_execution.phase3 import Phase3ProcessClaim
from app.exchange_runtime.submission import TestnetPreSubmitGate as Phase3PreSubmitGate
from app.market.models import MarketStreamKey
from app.persistence import (
    ExchangeExecutionRoot,
    ExchangeReconciliationStatus,
    PersistentExchangeReconciliationRun,
    SQLiteExchangePersistence,
    SQLitePersistenceConfig,
    SQLiteTradingPersistence,
)
from app.persistence.exceptions import (
    PersistenceConflictError,
    PersistenceIntegrityError,
    PersistenceMigrationError,
)
from app.risk.exchange import (
    ExchangeReconciliationStatus as RiskReconciliationStatus,
)
from app.risk.exchange import (
    ExchangeRiskAssessment,
    ExchangeRiskContext,
    ExchangeRiskOutcome,
    ReconciledAccountState,
)
from app.trading.models import RiskDecisionOutcome

D = Decimal
KEY = MarketStreamKey("BTCUSDT", "1m")
ROOT = "phase3-root"
SESSION = "phase3-session"
ACCOUNT = "phase3-account"
CONFIG = "9" * 64
COMMAND = "a" * 64
CLIENT = DeterministicClientOrderIdFactory().create_client_order_id(
    environment=ExecutionEnvironment.TESTNET,
    venue="binance-spot",
    account_fingerprint=ACCOUNT,
    durable_command_key=COMMAND,
)


def request(**changes: object) -> ReviewedPhase3SmokeRequest:
    command = ExchangeCommand(
        COMMAND,
        "b" * 64,
        "c" * 64,
        KEY,
        ExchangeOrderSide.BUY,
        D("0.00016"),
        D("63849.22"),
        D("0.5"),
        D("10.21587520"),
        "d" * 64,
        "e" * 64,
        CONFIG,
        3,
    )
    values = {
        "authority_id": "phase3-authority",
        "operator_run_id": "phase3-operator-run",
        "execution_root_id": ROOT,
        "session_id": SESSION,
        "account_fingerprint": ACCOUNT,
        "control_generation": 0,
        "reconciliation_generation": 1,
        "economic_slot_key": derive_phase3_economic_slot(ROOT, ACCOUNT),
        "client_order_id": CLIENT,
        "command": command,
        "envelope": Phase3RiskEnvelope(assumptions_fingerprint="f" * 64),
        "balance_scope": Phase3BalanceScope(ACCOUNT, D(1), D(0), D(10000), D(0), D(25), 3),
        "portfolio_id": "phase3-managed-portfolio",
        "created_at_ms": 3,
    }
    return ReviewedPhase3SmokeRequest(**(values | changes))  # type: ignore[arg-type]


def store(path: Path, failure_injector=None) -> SQLiteExchangePersistence:
    value = SQLiteExchangePersistence(
        SQLitePersistenceConfig(path, expected_schema_version=5),
        failure_injector=failure_injector,
    )
    if not value._connection.execute("SELECT 1 FROM exchange_execution_roots").fetchone():
        value.initialize_execution(
            ExchangeExecutionRoot(ROOT, ACCOUNT, KEY, CONFIG, 1),
            ExecutionSession(SESSION, ACCOUNT, 1, ExecutionSessionState.ACTIVE),
            audit_identifier="phase3-test",
        )
        value.record_reconciliation_run(
            PersistentExchangeReconciliationRun(
                "phase3-safe",
                ROOT,
                SESSION,
                ACCOUNT,
                0,
                1,
                ExchangeReconciliationStatus.SAFE,
                0,
                0,
                0,
                0,
                2,
                (),
                1,
                2,
                0,
                2,
                True,
                True,
                "BTCUSDT",
            )
        )
    return value


def test_atomic_claim_duplicate_and_restart_are_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "phase3.sqlite3"
    value = store(path)
    receipt = value.authorize_phase3_smoke_command(request())
    assert receipt.claim is not None
    assert value.authorize_phase3_smoke_command(request()).claim is receipt.claim
    assert value.recover_local().prepared == ()
    assert len(value.recover_local().reconciliation_required) == 1
    portfolio = value._connection.execute(
        "SELECT state_payload FROM exchange_portfolio_current"
    ).fetchone()[0]
    assert '"base_balance":{"$decimal":"0"}' in portfolio
    assert '"quote_balance":{"$decimal":"25"}' in portfolio
    with pytest.raises(TypeError):
        copy.copy(receipt.claim)
    with pytest.raises(TypeError):
        pickle.dumps(receipt.claim)
    value.close()

    reopened = store(path)
    recovered = reopened.authorize_phase3_smoke_command(request())
    assert recovered.recovered and recovered.claim is None
    assert reopened.list_submit_eligible() == ()
    reopened.close()


def test_sell_broader_envelope_and_insufficient_quote_are_rejected() -> None:
    with pytest.raises(ValueError, match="25/12/13"):
        Phase3RiskEnvelope(max_order_notional=D(13), assumptions_fingerprint="f" * 64)
    with pytest.raises(ValueError, match="cover"):
        Phase3BalanceScope(ACCOUNT, D(1), D(0), D(24), D(0), D(25), 3)
    sell = replace(request().command, side=ExchangeOrderSide.SELL)
    with pytest.raises(ValueError, match="SELL"):
        request(command=sell)


def test_all_reviewed_model_guards_and_claim_single_use() -> None:
    with pytest.raises(ValueError, match="count"):
        Phase3RiskEnvelope(max_submissions=2, assumptions_fingerprint="f" * 64)
    with pytest.raises(ValueError, match="BUY BTCUSDT"):
        Phase3RiskEnvelope(side="SELL", assumptions_fingerprint="f" * 64)
    with pytest.raises(ValueError, match="non-negative"):
        Phase3BalanceScope(ACCOUNT, D(-1), D(0), D(10000), D(0), D(25), 3)
    with pytest.raises(ValueError, match="allocation"):
        Phase3BalanceScope(ACCOUNT, D(1), D(0), D(10000), D(0), D(24), 3)
    with pytest.raises(ValueError, match="ownership"):
        Phase3BalanceScope(ACCOUNT, D(1), D(0), D(10000), D(0), D(25), 3, "full-account-owned")
    with pytest.raises(ValueError, match="policy"):
        request(policy_version="phase3/unknown")
    with pytest.raises(ValueError, match="generations"):
        request(control_generation=-1)
    with pytest.raises(ValueError, match="notional"):
        request(command=replace(request().command, approved_notional=D(13)))
    with pytest.raises(ValueError, match="balance/account"):
        request(
            balance_scope=Phase3BalanceScope(
                "different-account", D(1), D(0), D(10000), D(0), D(25), 3
            )
        )
    with pytest.raises(ValueError, match="economic slot"):
        request(economic_slot_key="7" * 64)
    with pytest.raises(ExchangeCapabilityError, match="only be issued"):
        Phase3ProcessClaim(request())
    claim = Phase3ProcessClaim._issue(request())
    with pytest.raises(TypeError):
        copy.deepcopy(claim)
    claim._consume()
    with pytest.raises(ExchangeCapabilityError, match="already"):
        claim._consume()


def test_decimal_identity_is_context_independent_and_bounded() -> None:
    baseline = Phase3RiskEnvelope(assumptions_fingerprint="f" * 64).digest
    with localcontext() as context:
        context.prec = 2
        assert Phase3RiskEnvelope(assumptions_fingerprint="f" * 64).digest == baseline
    negative_zero = Phase3BalanceScope(ACCOUNT, D("-0"), D("0E+999999"), D(25), D(0), D(25), 3)
    assert (
        negative_zero.digest
        == Phase3BalanceScope(ACCOUNT, D(0), D(0), D(25), D(0), D(25), 3).digest
    )
    with pytest.raises(ValueError, match="non-negative"):
        Phase3BalanceScope(ACCOUNT, D("NaN"), D(0), D(25), D(0), D(25), 3)


@pytest.mark.parametrize(
    "boundary",
    [
        "phase3_before_authority",
        "phase3_after_managed_portfolio",
        "phase3_after_authority",
        "phase3_after_command",
        "phase3_after_prepared_order",
        "phase3_after_prepared_event",
        "phase3_after_reservation",
        "phase3_after_linkage",
        "phase3_after_consumed",
        "phase3_after_audit_event",
        "phase3_before_commit",
    ],
)
@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt, SystemExit, GeneratorExit])
def test_every_precommit_baseexception_rolls_back(tmp_path: Path, boundary: str, failure) -> None:
    path = tmp_path / f"{boundary}-{failure.__name__}.sqlite3"

    def inject(value: str) -> None:
        if value == boundary:
            raise failure("crash")

    value = store(path, inject)
    with pytest.raises(failure):
        value.authorize_phase3_smoke_command(request())
    assert (
        value._connection.execute("SELECT count(*) FROM exchange_smoke_authorities").fetchone()[0]
        == 0
    )
    assert (
        value._connection.execute("SELECT count(*) FROM exchange_portfolio_roots").fetchone()[0]
        == 0
    )
    assert value._connection.execute("SELECT count(*) FROM exchange_orders").fetchone()[0] == 0
    value.close()


def test_postcommit_ack_loss_leaves_exactly_one_pair(tmp_path: Path) -> None:
    path = tmp_path / "ack-loss.sqlite3"

    def inject(value: str) -> None:
        if value == "phase3_after_commit":
            raise KeyboardInterrupt("ack lost")

    value = store(path, inject)
    with pytest.raises(KeyboardInterrupt):
        value.authorize_phase3_smoke_command(request())
    value.close()
    reopened = store(path)
    assert (
        reopened._connection.execute("SELECT count(*) FROM exchange_smoke_authorities").fetchone()[
            0
        ]
        == 1
    )
    assert reopened._connection.execute("SELECT count(*) FROM exchange_orders").fetchone()[0] == 1
    assert reopened.authorize_phase3_smoke_command(request()).claim is None
    reopened.close()


def test_sql_constraints_append_only_and_recovery_digest(tmp_path: Path) -> None:
    value = store(tmp_path / "constraints.sqlite3")
    value.authorize_phase3_smoke_command(request())
    with pytest.raises(sqlite3.IntegrityError):
        value._connection.execute("UPDATE exchange_smoke_authorities SET status='CONSUMED'")
    with pytest.raises(sqlite3.IntegrityError):
        value._connection.execute("DELETE FROM exchange_smoke_authorities")
    with pytest.raises(sqlite3.IntegrityError):
        value._connection.execute(
            "UPDATE exchange_smoke_authority_events SET event_type='CONSUMED'"
        )
    with pytest.raises(sqlite3.IntegrityError):
        value._connection.execute("DELETE FROM exchange_smoke_authority_events")
    value._connection.execute("DROP TRIGGER exchange_smoke_authorities_no_update")
    value._connection.execute("UPDATE exchange_smoke_authorities SET payload_digest=?", ("0" * 64,))
    with pytest.raises(PersistenceIntegrityError, match="digest"):
        value.recover_local()
    value.close()


def test_conflicting_economic_slot_is_typed(tmp_path: Path) -> None:
    value = store(tmp_path / "slot.sqlite3")
    value.authorize_phase3_smoke_command(request())
    with pytest.raises(PersistenceConflictError):
        value.authorize_phase3_smoke_command(
            request(authority_id="other-authority", operator_run_id="other-run")
        )
    value.close()


def test_client_identity_and_managed_portfolio_conflicts_fail_before_authority(
    tmp_path: Path,
) -> None:
    value = store(tmp_path / "identity.sqlite3")
    with pytest.raises(PersistenceConflictError, match="client order identity"):
        value.authorize_phase3_smoke_command(
            request(client_order_id="ctt1t-abcdefghijklmnopqrstuvwxyz")
        )
    assert (
        value._connection.execute("SELECT count(*) FROM exchange_smoke_authorities").fetchone()[0]
        == 0
    )
    value._connection.execute(
        "INSERT INTO exchange_portfolio_roots VALUES(?,?,?,?,?,?,?)",
        ("wrong", ROOT, "{}", "0" * 64, CONFIG, 3, "exchange-portfolio-root/v1"),
    )
    with pytest.raises(PersistenceConflictError, match="Portfolio"):
        value.authorize_phase3_smoke_command(request())
    assert (
        value._connection.execute("SELECT count(*) FROM exchange_smoke_authorities").fetchone()[0]
        == 0
    )
    value.close()


class NeverGateway:
    def submit_market_order(self, request):
        raise AssertionError("adversarial gate test reached gateway")


def test_phase3_gate_claim_binds_every_durable_identity_and_is_single_use(tmp_path: Path) -> None:
    value = store(tmp_path / "gate.sqlite3")
    receipt = value.authorize_phase3_smoke_command(request())
    assert receipt.claim is not None
    durable = value.load_order(COMMAND)
    lifecycle = DurableExchangeOrderLifecycle(value, NeverGateway(), timestamp_provider=lambda: 4)
    gate = Phase3PreSubmitGate(
        value, lifecycle, execution_root_id=ROOT, max_authorization_age_ms=100
    )
    bootstrap = value.load_bootstrap(ROOT)
    assert gate._phase3_claim_valid(receipt.claim, durable, bootstrap, 1)
    for field, wrong in (
        ("authority_id", "wrong"),
        ("durable_command_key", "0" * 64),
        ("execution_root_id", "wrong"),
        ("session_id", "wrong"),
        ("account_fingerprint", "wrong"),
        ("control_generation", 1),
        ("reconciliation_generation", 2),
        ("economic_slot_key", "0" * 64),
        ("envelope_digest", "0" * 64),
        ("balance_scope_digest", "0" * 64),
        ("rules_fingerprint", "0" * 64),
        ("config_fingerprint", "0" * 64),
        ("portfolio_id", "wrong"),
    ):
        original = getattr(receipt.claim, field)
        setattr(receipt.claim, field, wrong)
        assert not gate._phase3_claim_valid(receipt.claim, durable, bootstrap, 1)
        setattr(receipt.claim, field, original)
    context = ExchangeRiskContext(
        ReconciledAccountState(
            ACCOUNT,
            KEY,
            "BTC",
            "USDT",
            D(0),
            D(0),
            D(25),
            D(0),
            RiskReconciliationStatus.SAFE,
            1,
        ),
        PendingExposure(),
        value.load_session(SESSION),
        None,
        0,
        0,
        0,
    )
    assessment = ExchangeRiskAssessment(
        ExchangeRiskOutcome.APPROVED,
        RiskDecisionOutcome.APPROVED,
        "phase3-risk",
        SESSION,
        (),
        None,
    )
    authorized = gate.authorize(
        COMMAND,
        assessment=assessment,
        context=context,
        capability=None,
        expected_rules_fingerprint="e" * 64,
        now_ms=4,
        phase3_claim=receipt.claim,
    )
    assert authorized.authorized
    assert receipt.claim._consumed
    blocked = gate.authorize(
        COMMAND,
        assessment=assessment,
        context=context,
        capability=None,
        expected_rules_fingerprint="e" * 64,
        now_ms=4,
        phase3_claim=receipt.claim,
    )
    assert not blocked.authorized
    assert not gate._phase3_claim_valid(receipt.claim, durable, bootstrap, 1)
    replay = value.authorize_phase3_smoke_command(request())
    assert replay.claim is receipt.claim and replay.claim._consumed
    value.close()


def test_two_preopened_actors_cannot_create_a_second_authority(tmp_path: Path) -> None:
    path = tmp_path / "actors.sqlite3"
    bootstrap = store(path)
    bootstrap.close()
    first = store(path)
    second = store(path)
    winner = first.authorize_phase3_smoke_command(request())
    loser = second.authorize_phase3_smoke_command(request())
    assert winner.claim is not None
    assert loser.recovered and loser.claim is None
    assert (
        second._connection.execute("SELECT count(*) FROM exchange_smoke_authorities").fetchone()[0]
        == 1
    )
    assert second._connection.execute("SELECT count(*) FROM exchange_orders").fetchone()[0] == 1
    first.close()
    second.close()


def test_v4_to_v5_upgrade_is_additive_idempotent_and_failure_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "upgrade.sqlite3"
    legacy = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=4))
    legacy.initialize_execution(
        ExchangeExecutionRoot(ROOT, ACCOUNT, KEY, CONFIG, 1),
        ExecutionSession(SESSION, ACCOUNT, 1, ExecutionSessionState.ACTIVE),
        audit_identifier="legacy",
    )
    legacy.close()
    upgraded = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=5))
    assert upgraded.load_bootstrap(ROOT).root.account_fingerprint == ACCOUNT
    assert (
        upgraded._connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
        == 5
    )
    upgraded.close()
    reopened = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=5))
    assert (
        reopened._connection.execute(
            "SELECT count(*) FROM schema_migrations WHERE version=5"
        ).fetchone()[0]
        == 1
    )
    reopened.close()

    failed_path = tmp_path / "failed-upgrade.sqlite3"
    old = SQLiteExchangePersistence(SQLitePersistenceConfig(failed_path, expected_schema_version=4))
    old.close()
    original = SQLiteTradingPersistence._migration

    def broken(name="001_initial.sql"):
        if name == "005_phase3_smoke_authority.sql":
            return "CREATE TABLE broken(;", "0" * 64
        return original(name)

    monkeypatch.setattr(SQLiteTradingPersistence, "_migration", staticmethod(broken))
    with pytest.raises(PersistenceMigrationError):
        SQLiteExchangePersistence(SQLitePersistenceConfig(failed_path, expected_schema_version=5))
    monkeypatch.setattr(SQLiteTradingPersistence, "_migration", staticmethod(original))
    still_v4 = SQLiteExchangePersistence(
        SQLitePersistenceConfig(failed_path, expected_schema_version=4)
    )
    assert (
        still_v4._connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
        == 4
    )
    still_v4.close()


def test_v5_to_v6_upgrade_is_additive_idempotent_and_failure_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "upgrade-v6.sqlite3"
    legacy = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=5))
    legacy.close()
    upgraded = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=6))
    assert (
        upgraded._connection.execute(
            "SELECT count(*) FROM schema_migrations WHERE version=6"
        ).fetchone()[0]
        == 1
    )
    assert (
        upgraded._connection.execute(
            "SELECT count(*) FROM exchange_trade_settlement_resolutions"
        ).fetchone()[0]
        == 0
    )
    upgraded.close()
    reopened = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=6))
    assert (
        reopened._connection.execute(
            "SELECT count(*) FROM schema_migrations WHERE version=6"
        ).fetchone()[0]
        == 1
    )
    reopened.close()

    failed_path = tmp_path / "failed-v6.sqlite3"
    old = SQLiteExchangePersistence(SQLitePersistenceConfig(failed_path, expected_schema_version=5))
    old.close()
    original = SQLiteTradingPersistence._migration

    def broken(name="001_initial.sql"):
        if name == "006_zero_commission_resolution.sql":
            return "CREATE TABLE broken(;", "0" * 64
        return original(name)

    monkeypatch.setattr(SQLiteTradingPersistence, "_migration", staticmethod(broken))
    with pytest.raises(PersistenceMigrationError):
        SQLiteExchangePersistence(SQLitePersistenceConfig(failed_path, expected_schema_version=6))
    monkeypatch.setattr(SQLiteTradingPersistence, "_migration", staticmethod(original))
    still_v5 = SQLiteExchangePersistence(
        SQLitePersistenceConfig(failed_path, expected_schema_version=5)
    )
    assert (
        still_v5._connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
        == 5
    )
    still_v5.close()


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("payload_digest", "0" * 64, "digest"),
        ("risk_envelope_payload", "{}", "broader"),
        ("risk_envelope_digest", "0" * 64, "payload/column"),
        ("balance_scope_payload", "{}", "balance scope"),
        ("balance_scope_digest", "0" * 64, "payload/column"),
        ("economic_slot_key", "0" * 64, "payload/column"),
        ("rules_fingerprint", "0" * 64, "payload/column"),
        ("config_fingerprint", "0" * 64, "payload/column"),
    ],
)
def test_authority_corruption_matrix_fails_closed(
    tmp_path: Path, column: str, value: str, message: str
) -> None:
    store_value = store(tmp_path / f"corrupt-{column}.sqlite3")
    store_value.authorize_phase3_smoke_command(request())
    store_value._connection.execute("DROP TRIGGER exchange_smoke_authorities_no_update")
    store_value._connection.execute(f"UPDATE exchange_smoke_authorities SET {column}=?", (value,))
    with pytest.raises(PersistenceIntegrityError, match=message):
        store_value.recover_local()
    store_value.close()


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("payload_digest", "0" * 64, "event digest"),
        ("operator_run_id", "wrong-run", "event linkage"),
    ],
)
def test_authority_event_corruption_fails_closed(
    tmp_path: Path, column: str, value: str, message: str
) -> None:
    store_value = store(tmp_path / f"event-{column}.sqlite3")
    store_value.authorize_phase3_smoke_command(request())
    store_value._connection.execute("DROP TRIGGER exchange_smoke_authority_events_no_update")
    store_value._connection.execute(
        f"UPDATE exchange_smoke_authority_events SET {column}=?", (value,)
    )
    with pytest.raises(PersistenceIntegrityError, match=message):
        store_value.recover_local()
    store_value.close()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"control_generation": 1}, "control"),
        ({"reconciliation_generation": 0}, "reconciliation"),
        ({"session_id": "wrong-session"}, "lineage"),
        ({"account_fingerprint": "wrong-account"}, "lineage"),
    ],
)
def test_stale_generation_and_lineage_fail_before_any_write(
    tmp_path: Path, change: dict[str, object], message: str
) -> None:
    store_value = store(tmp_path / f"stale-{message}-{len(change)}.sqlite3")
    if "account_fingerprint" in change:
        account = str(change["account_fingerprint"])
        change = {
            **change,
            "economic_slot_key": derive_phase3_economic_slot(ROOT, account),
            "balance_scope": Phase3BalanceScope(account, D(1), D(0), D(10000), D(0), D(25), 3),
            "client_order_id": DeterministicClientOrderIdFactory().create_client_order_id(
                environment=ExecutionEnvironment.TESTNET,
                venue="binance-spot",
                account_fingerprint=account,
                durable_command_key=COMMAND,
            ),
        }
    candidate = request(**change)
    with pytest.raises(PersistenceConflictError, match=message):
        store_value.authorize_phase3_smoke_command(candidate)
    assert (
        store_value._connection.execute("SELECT count(*) FROM exchange_orders").fetchone()[0] == 0
    )
    assert (
        store_value._connection.execute("SELECT count(*) FROM exchange_portfolio_roots").fetchone()[
            0
        ]
        == 0
    )
    store_value.close()
