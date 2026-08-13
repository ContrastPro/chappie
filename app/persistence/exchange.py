import json
import sqlite3
from collections.abc import Callable
from dataclasses import asdict
from decimal import Decimal, localcontext
from typing import Self

from app.exchange_execution.factories import DeterministicClientOrderIdFactory
from app.exchange_execution.lifecycle import transition_order
from app.exchange_execution.models import (
    ExchangeCommand,
    ExchangeOrder,
    ExchangeOrderEventSource,
    ExchangeOrderSide,
    ExchangeOrderStatus,
    ExchangeTrade,
    ExecutionEnvironment,
    ExecutionSession,
    ExecutionSessionState,
    KillSwitch,
    KillSwitchState,
)
from app.exchange_execution.phase3 import (
    Phase3ClaimReceipt,
    Phase3ProcessClaim,
    ReviewedPhase3SmokeRequest,
    derive_phase3_economic_slot,
)
from app.market.models import MarketStreamKey
from app.persistence.canonical import (
    canonical_json,
    canonical_sha256,
    deserialize_decimal,
    serialize_decimal,
)
from app.persistence.exceptions import (
    PersistenceConflictError,
    PersistenceIntegrityError,
    PersistenceTransactionError,
    Phase3AuthorityConflictError,
    Phase3LineageError,
    Phase3StaleControlError,
    Phase3StaleReconciliationError,
)
from app.persistence.exchange_models import (
    DurableExchangeOrder,
    ExchangeControlState,
    ExchangeExecutionRoot,
    ExchangeReconciliationState,
    ExchangeReconciliationStatus,
    ExecutionBootstrap,
    LocalExchangeRecoveryResult,
    LocalExchangeRecoveryStatus,
    PersistentExchangeReconciliationRun,
    RestartDisposition,
    TradeCompletenessStatus,
    TradeSettlementStatus,
)
from app.persistence.models import PortfolioState, SQLitePersistenceConfig
from app.persistence.sqlite import SQLiteTradingPersistence


def _order_payload(order: ExchangeOrder) -> dict[str, object]:
    return {
        "durable_command_key": order.durable_command_key,
        "client_order_id": order.client_order_id,
        "account_fingerprint": order.account_fingerprint,
        "symbol": order.stream_key.symbol,
        "interval": order.stream_key.interval,
        "side": order.side,
        "original_base_quantity": order.original_base_quantity,
        "status": order.status,
        "created_at_ms": order.created_at_ms,
        "exchange_order_id": order.exchange_order_id,
        "cumulative_executed_base_quantity": order.cumulative_executed_base_quantity,
        "cumulative_quote_quantity": order.cumulative_quote_quantity,
        "submitted_at_ms": order.submitted_at_ms,
        "acknowledged_at_ms": order.acknowledged_at_ms,
        "latest_exchange_update_ms": order.latest_exchange_update_ms,
        "environment": order.environment,
        "venue": order.venue,
        "model_version": order.model_version,
    }


def _order_digest(order: ExchangeOrder) -> str:
    return canonical_sha256(_order_payload(order), algorithm_version=order.model_version)


def _add_decimal(left: Decimal, right: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = max(50, len(left.as_tuple().digits) + len(right.as_tuple().digits) + 16)
        return left + right


def _sum_decimals(values: list[Decimal]) -> Decimal:
    result = Decimal(0)
    for value in values:
        result = _add_decimal(result, value)
    return result


def _command_payload(command: ExchangeCommand) -> str:
    values = {name: getattr(command, name) for name in command.__dataclass_fields__}
    values["stream_key"] = {
        "symbol": command.stream_key.symbol,
        "interval": command.stream_key.interval,
    }
    return canonical_json(values)


class SQLiteExchangePersistence:
    """Schema-v2+ exchange persistence; synchronous and single-writer."""

    def __init__(
        self,
        config: SQLitePersistenceConfig,
        *,
        failure_injector: Callable[[str], None] | None = None,
    ) -> None:
        if not isinstance(
            config, SQLitePersistenceConfig
        ) or config.expected_schema_version not in {2, 3, 4, 5}:
            raise PersistenceIntegrityError(
                "exchange persistence requires schema version 2, 3, 4, or 5"
            )
        self._trading = SQLiteTradingPersistence(config)
        self._connection = self._trading._connection
        if failure_injector is not None and not callable(failure_injector):
            self._trading.close()
            raise PersistenceIntegrityError("failure_injector must be callable")
        self._failure_injector = failure_injector
        self._phase3_claims: dict[str, Phase3ProcessClaim] = {}

    def _hit(self, boundary: str) -> None:
        if self._failure_injector is not None:
            self._failure_injector(boundary)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self._trading.close()

    def initialize_execution(
        self, root: ExchangeExecutionRoot, session: ExecutionSession, *, audit_identifier: str
    ) -> ExecutionBootstrap:
        if not isinstance(root, ExchangeExecutionRoot) or not isinstance(session, ExecutionSession):
            raise PersistenceIntegrityError("root and session have invalid types")
        if session.account_fingerprint != root.account_fingerprint:
            raise PersistenceConflictError("session account lineage mismatch")
        if session.state is not ExecutionSessionState.ACTIVE:
            raise PersistenceConflictError("initial execution session must be ACTIVE")
        control = ExchangeControlState(
            root.execution_root_id,
            KillSwitch(KillSwitchState.ACTIVE, 0, "safe_default", root.created_at_ms),
            0,
            audit_identifier,
        )
        connection = self._connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT state_digest FROM exchange_execution_roots WHERE execution_root_id=?",
                (root.execution_root_id,),
            ).fetchone()
            if existing is not None:
                if existing[0] != root.digest:
                    raise PersistenceConflictError("exchange execution root conflicts")
                if self._trading.config.expected_schema_version >= 3:
                    reconciliation = connection.execute(
                        "SELECT 1 FROM exchange_reconciliation_current WHERE execution_root_id=?",
                        (root.execution_root_id,),
                    ).fetchone()
                    if reconciliation is None:
                        self._insert_initial_reconciliation_state(
                            root.execution_root_id, root.created_at_ms
                        )
                        connection.commit()
                    else:
                        connection.rollback()
                else:
                    connection.rollback()
                return self.load_bootstrap(root.execution_root_id)
            unresolved = connection.execute(
                "SELECT 1 FROM exchange_orders WHERE terminal=0 LIMIT 1"
            ).fetchone()
            if unresolved is not None:
                raise PersistenceConflictError("unresolved orders block a new execution root")
            connection.execute(
                "INSERT INTO exchange_execution_roots VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    root.execution_root_id,
                    root.venue,
                    root.environment.value,
                    root.account_fingerprint,
                    root.stream_key.symbol,
                    root.stream_key.interval,
                    root.model_version,
                    root.configuration_fingerprint,
                    root.created_at_ms,
                    root.digest,
                ),
            )
            connection.execute(
                "INSERT INTO exchange_sessions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    session.session_id,
                    root.execution_root_id,
                    session.account_fingerprint,
                    session.state.value,
                    session.authorized_at_ms,
                    serialize_decimal(session.cumulative_submitted_notional),
                    serialize_decimal(session.cumulative_executed_turnover),
                    session.submission_count,
                    session.execution_failure_count,
                    0,
                    session.model_version,
                ),
            )
            connection.execute(
                "INSERT INTO exchange_control_state VALUES(?,?,?,?,?,?,?,?)",
                (
                    root.execution_root_id,
                    control.kill_switch.state.value,
                    control.kill_switch.generation,
                    control.reconciliation_generation,
                    control.kill_switch.reason_code,
                    control.audit_identifier,
                    control.kill_switch.changed_at_ms,
                    control.kill_switch.model_version,
                ),
            )
            if self._trading.config.expected_schema_version >= 3:
                self._insert_initial_reconciliation_state(
                    root.execution_root_id, root.created_at_ms
                )
            connection.commit()
            return ExecutionBootstrap(root, session, control)
        except (PersistenceConflictError, sqlite3.Error) as error:
            connection.rollback()
            if isinstance(error, PersistenceConflictError):
                raise
            raise PersistenceTransactionError("execution initialization failed") from error

    def load_bootstrap(self, execution_root_id: str) -> ExecutionBootstrap:
        root_row = self._connection.execute(
            "SELECT * FROM exchange_execution_roots WHERE execution_root_id=?",
            (execution_root_id,),
        ).fetchone()
        if root_row is None:
            raise PersistenceIntegrityError("exchange execution root is missing")
        try:
            root = ExchangeExecutionRoot(
                root_row["execution_root_id"],
                root_row["account_fingerprint"],
                MarketStreamKey(root_row["symbol"], root_row["interval"]),
                root_row["configuration_fingerprint"],
                root_row["created_at_ms"],
                venue=root_row["venue"],
                environment=ExecutionEnvironment(root_row["environment"]),
                model_version=root_row["model_version"],
            )
        except Exception as error:
            raise PersistenceIntegrityError("exchange execution root is invalid") from error
        if root.digest != root_row["state_digest"]:
            raise PersistenceIntegrityError("exchange execution root digest mismatch")
        session_row = self._connection.execute(
            "SELECT * FROM exchange_sessions WHERE execution_root_id=? AND state='ACTIVE'",
            (execution_root_id,),
        ).fetchone()
        if session_row is None:
            raise PersistenceIntegrityError("active execution session is missing")
        session = self._session(session_row)
        return ExecutionBootstrap(root, session, self.read_control_state(execution_root_id))

    def prepare_order(
        self,
        *,
        execution_root_id: str,
        session_id: str,
        command: ExchangeCommand,
        client_order_id: str,
        occurred_at_ms: int,
    ) -> DurableExchangeOrder:
        bootstrap = self.load_bootstrap(execution_root_id)
        if bootstrap.session.session_id != session_id:
            raise PersistenceConflictError("order session lineage mismatch")
        order = ExchangeOrder(
            command.durable_command_key,
            client_order_id,
            bootstrap.root.account_fingerprint,
            command.stream_key,
            command.side,
            command.base_quantity,
            ExchangeOrderStatus.PREPARED,
            occurred_at_ms,
        )
        command_payload = _command_payload(command)
        connection = self._connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM exchange_orders WHERE durable_command_key=? OR client_order_id=?",
                (command.durable_command_key, client_order_id),
            ).fetchone()
            if existing is not None:
                durable = self._durable(existing)
                if (
                    durable.stable_decision_key == command.stable_decision_key
                    and durable.command_payload == command_payload
                    and durable.session_id == session_id
                    and durable.execution_root_id == execution_root_id
                    and durable.order.client_order_id == client_order_id
                    and durable.order.account_fingerprint == bootstrap.root.account_fingerprint
                    and durable.order.status is ExchangeOrderStatus.PREPARED
                ):
                    connection.rollback()
                    return durable
                raise PersistenceConflictError("durable exchange order identity conflicts")
            state_payload = canonical_json(_order_payload(order))
            digest = _order_digest(order)
            connection.execute(
                "INSERT INTO exchange_orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    order.durable_command_key,
                    command.stable_decision_key,
                    order.client_order_id,
                    execution_root_id,
                    session_id,
                    order.account_fingerprint,
                    order.venue,
                    order.environment.value,
                    order.stream_key.symbol,
                    order.stream_key.interval,
                    order.side.value,
                    serialize_decimal(order.original_base_quantity),
                    serialize_decimal(command.approved_notional),
                    order.status.value,
                    None,
                    "0",
                    "0",
                    order.created_at_ms,
                    None,
                    None,
                    None,
                    0,
                    1,
                    command_payload,
                    state_payload,
                    digest,
                ),
            )
            self._hit("prepare_after_projection")
            self._append_event(
                order,
                None,
                1,
                ExchangeOrderEventSource.LOCAL_PREPARATION,
                "prepared",
                occurred_at_ms,
            )
            self._hit("prepare_after_event")
            self._hit("prepare_before_commit")
            connection.commit()
            self._hit("prepare_after_commit")
            return self.load_order(order.durable_command_key)
        except (PersistenceConflictError, sqlite3.Error) as error:
            connection.rollback()
            if isinstance(error, PersistenceConflictError):
                raise
            raise PersistenceTransactionError("order preparation failed") from error
        except BaseException:
            connection.rollback()
            raise

    def authorize_phase3_smoke_command(
        self, request: ReviewedPhase3SmokeRequest
    ) -> Phase3ClaimReceipt:
        """Atomically consume one reviewed authority and prepare its sole command."""
        if self._trading.config.expected_schema_version != 5:
            raise PersistenceIntegrityError("Phase 3 authority requires schema version 5")
        if not isinstance(request, ReviewedPhase3SmokeRequest):
            raise PersistenceIntegrityError("invalid reviewed Phase 3 request")
        command = request.command
        expected_client_id = DeterministicClientOrderIdFactory().create_client_order_id(
            environment=ExecutionEnvironment.TESTNET,
            venue="binance-spot",
            account_fingerprint=request.account_fingerprint,
            durable_command_key=command.durable_command_key,
        )
        if request.client_order_id != expected_client_id:
            raise PersistenceConflictError("Phase 3 client order identity is not deterministic")
        order = ExchangeOrder(
            command.durable_command_key,
            request.client_order_id,
            request.account_fingerprint,
            command.stream_key,
            command.side,
            command.base_quantity,
            ExchangeOrderStatus.PREPARED,
            request.created_at_ms,
        )
        envelope_payload = canonical_json(asdict(request.envelope))
        balance_payload = canonical_json(asdict(request.balance_scope))
        authority_values = {
            "authority_id": request.authority_id,
            "execution_root_id": request.execution_root_id,
            "session_id": request.session_id,
            "account_fingerprint": request.account_fingerprint,
            "managed_portfolio_id": request.portfolio_id,
            "venue": "binance-spot",
            "environment": "testnet",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "control_generation": request.control_generation,
            "reconciliation_generation": request.reconciliation_generation,
            "operator_run_id": request.operator_run_id,
            "economic_slot_key": request.economic_slot_key,
            "policy_version": request.policy_version,
            "rules_fingerprint": command.instrument_rules_fingerprint,
            "config_fingerprint": command.execution_configuration_fingerprint,
            "risk_envelope_digest": request.envelope.digest,
            "balance_scope_digest": request.balance_scope.digest,
            "consumed_durable_command_key": command.durable_command_key,
            "created_at_ms": request.created_at_ms,
            "consumed_at_ms": request.created_at_ms,
            "status": "CONSUMED",
        }
        authority_payload = canonical_json(authority_values)
        authority_digest = canonical_sha256(
            authority_values, algorithm_version=request.policy_version
        )
        connection = self._connection
        committed = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT authority_id,consumed_durable_command_key,payload_digest "
                "FROM exchange_smoke_authorities WHERE authority_id=? OR economic_slot_key=? "
                "OR consumed_durable_command_key=? OR (operator_run_id=? AND execution_root_id=?)",
                (
                    request.authority_id,
                    request.economic_slot_key,
                    command.durable_command_key,
                    request.operator_run_id,
                    request.execution_root_id,
                ),
            ).fetchone()
            if existing is not None:
                if (
                    existing[0] != request.authority_id
                    or existing[1] != command.durable_command_key
                    or existing[2] != authority_digest
                ):
                    raise Phase3AuthorityConflictError("Phase 3 authority identity conflicts")
                self._validate_phase3_authorities()
                connection.rollback()
                claim = self._phase3_claims.get(request.authority_id)
                return Phase3ClaimReceipt(
                    request.authority_id,
                    command.durable_command_key,
                    request.client_order_id,
                    claim,
                    recovered=claim is None,
                )
            root = connection.execute(
                "SELECT account_fingerprint,configuration_fingerprint FROM exchange_execution_roots WHERE execution_root_id=?",
                (request.execution_root_id,),
            ).fetchone()
            session = connection.execute(
                "SELECT execution_root_id,account_fingerprint,state,submission_count,submitted_notional,executed_turnover FROM exchange_sessions WHERE session_id=?",
                (request.session_id,),
            ).fetchone()
            control = connection.execute(
                "SELECT kill_switch_state,control_generation,reconciliation_generation FROM exchange_control_state WHERE execution_root_id=?",
                (request.execution_root_id,),
            ).fetchone()
            reconciliation = connection.execute(
                "SELECT status,generation FROM exchange_reconciliation_current WHERE execution_root_id=?",
                (request.execution_root_id,),
            ).fetchone()
            if (
                root is None
                or session is None
                or root[0] != request.account_fingerprint
                or tuple(session[:3])
                != (request.execution_root_id, request.account_fingerprint, "ACTIVE")
            ):
                raise Phase3LineageError("Phase 3 root/session/account lineage mismatch")
            if root[1] != command.execution_configuration_fingerprint:
                raise Phase3LineageError("Phase 3 configuration fingerprint mismatch")
            if (
                control is None
                or control[0] != "ACTIVE"
                or control[1] != request.control_generation
            ):
                raise Phase3StaleControlError("Phase 3 ACTIVE control generation is stale")
            if (
                reconciliation is None
                or reconciliation[0] != "SAFE"
                or reconciliation[1] != request.reconciliation_generation
                or control[2] != request.reconciliation_generation
            ):
                raise Phase3StaleReconciliationError(
                    "Phase 3 SAFE reconciliation generation is stale"
                )
            if (
                session[3] != 0
                or deserialize_decimal(session[4]) != 0
                or deserialize_decimal(session[5]) != 0
            ):
                raise PersistenceConflictError("Phase 3 requires an unused execution session")
            counts = connection.execute(
                "SELECT count(*),sum(CASE WHEN terminal=0 THEN 1 ELSE 0 END) FROM exchange_orders WHERE execution_root_id=?",
                (request.execution_root_id,),
            ).fetchone()
            if counts[0] or counts[1]:
                raise PersistenceConflictError("Phase 3 requires an empty order lineage")
            managed_state = PortfolioState(
                portfolio_id=request.portfolio_id,
                stream_key=command.stream_key,
                base_asset="BTC",
                quote_asset="USDT",
                quote_balance=Decimal(25),
                base_balance=Decimal(0),
                position=None,
                realised_pnl=Decimal(0),
                unrealised_pnl=Decimal(0),
                total_fees=Decimal(0),
                initial_equity=Decimal(25),
                turnover=Decimal(0),
                trade_count=0,
                applied_fill_count=0,
                peak_equity=Decimal(25),
                current_equity=Decimal(25),
                drawdown=Decimal(0),
                max_drawdown=Decimal(0),
                mark_price=None,
                as_of_ms=request.created_at_ms,
                version=0,
                correlation_id=request.operator_run_id,
            )
            portfolio = connection.execute(
                "SELECT portfolio_id,state_digest,config_fingerprint FROM exchange_portfolio_roots "
                "WHERE execution_root_id=? OR portfolio_id=?",
                (request.execution_root_id, request.portfolio_id),
            ).fetchone()
            if portfolio is None:
                connection.execute(
                    "INSERT INTO exchange_portfolio_roots VALUES(?,?,?,?,?,?,?)",
                    (
                        request.portfolio_id,
                        request.execution_root_id,
                        managed_state.canonical_payload,
                        managed_state.digest,
                        command.execution_configuration_fingerprint,
                        request.created_at_ms,
                        "exchange-portfolio-root/v1",
                    ),
                )
                connection.execute(
                    "INSERT INTO exchange_portfolio_current VALUES(?,?,?,?,NULL)",
                    (
                        request.portfolio_id,
                        0,
                        managed_state.canonical_payload,
                        managed_state.digest,
                    ),
                )
            elif tuple(portfolio) != (
                request.portfolio_id,
                managed_state.digest,
                command.execution_configuration_fingerprint,
            ):
                raise PersistenceConflictError(
                    "Phase 3 managed Portfolio is not the reviewed zero-BTC/25-USDT root"
                )
            self._hit("phase3_after_managed_portfolio")
            self._hit("phase3_before_authority")
            connection.execute(
                "INSERT INTO exchange_smoke_authorities VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    request.authority_id,
                    request.execution_root_id,
                    request.session_id,
                    request.account_fingerprint,
                    request.portfolio_id,
                    "binance-spot",
                    "testnet",
                    "BTCUSDT",
                    "BUY",
                    request.control_generation,
                    request.reconciliation_generation,
                    request.operator_run_id,
                    request.economic_slot_key,
                    request.policy_version,
                    command.instrument_rules_fingerprint,
                    command.execution_configuration_fingerprint,
                    envelope_payload,
                    request.envelope.digest,
                    balance_payload,
                    request.balance_scope.digest,
                    command.durable_command_key,
                    request.created_at_ms,
                    request.created_at_ms,
                    "CONSUMED",
                    authority_payload,
                    authority_digest,
                ),
            )
            self._hit("phase3_after_authority")
            state_payload = canonical_json(_order_payload(order))
            connection.execute(
                "INSERT INTO exchange_orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    order.durable_command_key,
                    command.stable_decision_key,
                    order.client_order_id,
                    request.execution_root_id,
                    request.session_id,
                    request.account_fingerprint,
                    order.venue,
                    order.environment.value,
                    order.stream_key.symbol,
                    order.stream_key.interval,
                    order.side.value,
                    serialize_decimal(order.original_base_quantity),
                    serialize_decimal(command.approved_notional),
                    order.status.value,
                    None,
                    "0",
                    "0",
                    order.created_at_ms,
                    None,
                    None,
                    None,
                    0,
                    1,
                    _command_payload(command),
                    state_payload,
                    _order_digest(order),
                ),
            )
            self._hit("phase3_after_command")
            self._hit("phase3_after_prepared_order")
            self._append_event(
                order,
                None,
                1,
                ExchangeOrderEventSource.LOCAL_PREPARATION,
                "phase3_smoke_prepared",
                request.created_at_ms,
            )
            self._hit("phase3_after_prepared_event")
            self._hit("phase3_after_reservation")
            self._hit("phase3_after_linkage")
            self._hit("phase3_after_consumed")
            event_values = {
                "authority_id": request.authority_id,
                "event_ordinal": 1,
                "event_type": "CONSUMED",
                "durable_command_key": command.durable_command_key,
                "operator_run_id": request.operator_run_id,
                "occurred_at_ms": request.created_at_ms,
                "policy_version": request.policy_version,
            }
            event_payload = canonical_json(event_values)
            event_id = canonical_sha256(
                {"authority_id": request.authority_id, "event_ordinal": 1},
                algorithm_version="phase3-authority-event-id/v1",
            )
            connection.execute(
                "INSERT INTO exchange_smoke_authority_events VALUES(NULL,?,?,?,?,?,?,?,?,?)",
                (
                    event_id,
                    request.authority_id,
                    1,
                    "CONSUMED",
                    command.durable_command_key,
                    request.operator_run_id,
                    request.created_at_ms,
                    event_payload,
                    canonical_sha256(event_values, algorithm_version="phase3-authority-event/v1"),
                ),
            )
            self._hit("phase3_after_audit_event")
            self._hit("phase3_before_commit")
            connection.commit()
            committed = True
            claim = Phase3ProcessClaim._issue(request)
            self._phase3_claims[request.authority_id] = claim
            self._hit("phase3_after_commit")
            return Phase3ClaimReceipt(
                request.authority_id, command.durable_command_key, request.client_order_id, claim
            )
        except (PersistenceConflictError, sqlite3.IntegrityError) as error:
            if not committed:
                connection.rollback()
            if isinstance(error, PersistenceConflictError):
                raise
            raise Phase3AuthorityConflictError("Phase 3 authority constraint conflict") from error
        except sqlite3.Error as error:
            if not committed:
                connection.rollback()
            raise PersistenceTransactionError("Phase 3 authority transaction failed") from error
        except BaseException:
            if not committed:
                connection.rollback()
            raise

    def begin_submission(
        self, durable_command_key: str, *, occurred_at_ms: int
    ) -> DurableExchangeOrder:
        current = self.load_order(durable_command_key)
        if current.order.status is not ExchangeOrderStatus.PREPARED:
            raise PersistenceConflictError("only PREPARED order may begin its first submission")
        next_order = transition_order(
            current.order, status=ExchangeOrderStatus.SUBMITTING, submitted_at_ms=occurred_at_ms
        )
        attempt_payload = {
            "durable_command_key": durable_command_key,
            "client_order_id": current.order.client_order_id,
            "attempt_ordinal": 1,
            "approved_notional": current.approved_notional,
            "started_at_ms": occurred_at_ms,
        }
        attempt_id = canonical_sha256(attempt_payload, algorithm_version="submission-attempt/v1")
        connection = self._connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO exchange_submission_attempts VALUES(?,?,?,?,?,?,?)",
                (
                    attempt_id,
                    durable_command_key,
                    1,
                    current.order.client_order_id,
                    occurred_at_ms,
                    serialize_decimal(current.approved_notional),
                    canonical_sha256(attempt_payload, algorithm_version="submission-attempt/v1"),
                ),
            )
            self._hit("attempt_after_record")
            self._write_transition(
                current,
                next_order,
                ExchangeOrderEventSource.TRANSPORT,
                "submission_attempt_started",
                occurred_at_ms,
            )
            self._hit("attempt_after_order")
            connection.execute(
                "UPDATE exchange_sessions SET submitted_notional=?,submission_count="
                "submission_count+1,generation=generation+1 WHERE session_id=?",
                (
                    serialize_decimal(
                        _add_decimal(
                            self.load_session(current.session_id).cumulative_submitted_notional,
                            current.approved_notional,
                        )
                    ),
                    current.session_id,
                ),
            )
            self._hit("attempt_after_counters")
            self._hit("attempt_before_commit")
            connection.commit()
            self._hit("attempt_after_commit")
            return self.load_order(durable_command_key)
        except sqlite3.IntegrityError as error:
            connection.rollback()
            raise PersistenceConflictError("submission attempt already exists") from error
        except sqlite3.Error as error:
            connection.rollback()
            raise PersistenceTransactionError("submission attempt transaction failed") from error
        except BaseException:
            connection.rollback()
            raise

    def record_status(
        self,
        durable_command_key: str,
        *,
        status: ExchangeOrderStatus,
        occurred_at_ms: int,
        reason_code: str,
        exchange_order_id: str | None = None,
        acknowledged_at_ms: int | None = None,
        cumulative_executed_base_quantity: Decimal | None = None,
        cumulative_quote_quantity: Decimal | None = None,
        exchange_timestamp_ms: int | None = None,
        source: ExchangeOrderEventSource = ExchangeOrderEventSource.TRANSPORT,
        count_failure: bool = False,
    ) -> DurableExchangeOrder:
        current = self.load_order(durable_command_key)
        supplied = {
            "exchange_order_id": exchange_order_id,
            "acknowledged_at_ms": acknowledged_at_ms,
            "cumulative_executed_base_quantity": cumulative_executed_base_quantity,
            "cumulative_quote_quantity": cumulative_quote_quantity,
            "latest_exchange_update_ms": exchange_timestamp_ms,
        }
        if current.order.status is status and all(
            value is None or getattr(current.order, name) == value
            for name, value in supplied.items()
        ):
            return current
        if current.order.status is status and status is not ExchangeOrderStatus.PARTIALLY_FILLED:
            raise PersistenceConflictError("replayed order status has conflicting facts")
        changes: dict[str, object] = {"status": status}
        for name, value in supplied.items():
            if value is not None:
                changes[name] = value
        next_order = transition_order(current.order, **changes)
        connection = self._connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._write_transition(
                current, next_order, source, reason_code, occurred_at_ms, exchange_timestamp_ms
            )
            self._hit("status_after_order")
            if count_failure:
                connection.execute(
                    "UPDATE exchange_sessions SET execution_failure_count="
                    "execution_failure_count+1,generation=generation+1 WHERE session_id=?",
                    (current.session_id,),
                )
            self._hit("status_after_counters")
            self._hit("status_before_commit")
            connection.commit()
            self._hit("status_after_commit")
            return self.load_order(durable_command_key)
        except sqlite3.IntegrityError as error:
            connection.rollback()
            raise PersistenceConflictError(
                "order transition conflicts with durable identity"
            ) from error
        except sqlite3.Error as error:
            connection.rollback()
            raise PersistenceTransactionError("order transition transaction failed") from error
        except BaseException:
            connection.rollback()
            raise

    def persist_trades(
        self, durable_command_key: str, trades: tuple[ExchangeTrade, ...]
    ) -> tuple[ExchangeTrade, ...]:
        current = self.load_order(durable_command_key)
        if current.order.status is ExchangeOrderStatus.REJECTED and trades:
            raise PersistenceConflictError("REJECTED order cannot contain exchange trades")
        connection = self._connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            for trade in trades:
                if (
                    not isinstance(trade, ExchangeTrade)
                    or trade.client_order_id != current.order.client_order_id
                    or trade.identity.exchange_order_id != current.order.exchange_order_id
                    or trade.stream_key != current.order.stream_key
                    or trade.side is not current.order.side
                ):
                    raise PersistenceConflictError("trade/order identity conflict")
                trade_values = {
                    name: getattr(trade, name)
                    for name in trade.__dataclass_fields__
                    if name not in {"identity", "stream_key"}
                }
                trade_values["stream_key"] = {
                    "symbol": trade.stream_key.symbol,
                    "interval": trade.stream_key.interval,
                }
                payload = canonical_json(
                    {
                        "identity": {
                            name: getattr(trade.identity, name)
                            for name in trade.identity.__dataclass_fields__
                        },
                        **trade_values,
                    }
                )
                digest = canonical_sha256(payload, algorithm_version=trade.model_version)
                row = connection.execute(
                    "SELECT payload_digest FROM exchange_trades WHERE venue=? AND environment=? "
                    "AND account_fingerprint=? AND symbol=? AND exchange_trade_id=?",
                    (
                        trade.identity.venue,
                        trade.identity.environment.value,
                        trade.identity.account_fingerprint,
                        trade.identity.symbol,
                        trade.identity.exchange_trade_id,
                    ),
                ).fetchone()
                if row is not None:
                    if row[0] != digest:
                        raise PersistenceConflictError("exchange trade identity conflicts")
                    continue
                connection.execute(
                    "INSERT INTO exchange_trades VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        trade.identity.venue,
                        trade.identity.environment.value,
                        trade.identity.account_fingerprint,
                        trade.identity.symbol,
                        trade.identity.exchange_order_id,
                        trade.identity.exchange_trade_id,
                        durable_command_key,
                        trade.client_order_id,
                        trade.stream_key.interval,
                        trade.side.value,
                        serialize_decimal(trade.quantity),
                        serialize_decimal(trade.price),
                        serialize_decimal(trade.quote_quantity),
                        serialize_decimal(trade.commission),
                        trade.commission_asset,
                        trade.traded_at_ms,
                        None if trade.is_maker is None else int(trade.is_maker),
                        TradeSettlementStatus.PENDING.value,
                        payload,
                        digest,
                    ),
                )
            persisted_base = connection.execute(
                "SELECT quantity FROM exchange_trades WHERE durable_command_key=?",
                (durable_command_key,),
            ).fetchall()
            if _sum_decimals([deserialize_decimal(row[0]) for row in persisted_base]) > (
                current.order.cumulative_executed_base_quantity
            ):
                raise PersistenceConflictError("persisted trade quantity exceeds order authority")
            connection.commit()
            return trades
        except (PersistenceConflictError, sqlite3.Error) as error:
            connection.rollback()
            if isinstance(error, PersistenceConflictError):
                raise
            raise PersistenceTransactionError("exchange trade transaction failed") from error

    def load_order(self, durable_command_key: str) -> DurableExchangeOrder:
        row = self._connection.execute(
            "SELECT * FROM exchange_orders WHERE durable_command_key=?", (durable_command_key,)
        ).fetchone()
        if row is None:
            raise PersistenceIntegrityError("durable exchange order is missing")
        durable = self._durable(row)
        events = self._connection.execute(
            "SELECT order_version,new_status,payload,payload_digest FROM exchange_order_events "
            "WHERE durable_command_key=? ORDER BY order_version",
            (durable_command_key,),
        ).fetchall()
        if (
            not events
            or [row[0] for row in events] != list(range(1, durable.version + 1))
            or events[-1][1] != durable.order.status.value
        ):
            raise PersistenceIntegrityError("exchange order projection/event mismatch")
        for event in events:
            try:
                expected = canonical_sha256(
                    json.loads(event[2]), algorithm_version="exchange-order-event/v1"
                )
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise PersistenceIntegrityError(
                    "exchange order event payload is invalid"
                ) from error
            if expected != event[3]:
                raise PersistenceIntegrityError("exchange order event digest mismatch")
        if durable.state_digest != _order_digest(durable.order):
            raise PersistenceIntegrityError("exchange order state digest mismatch")
        lineage = self._connection.execute(
            "SELECT execution_root_id,account_fingerprint FROM exchange_sessions WHERE session_id=?",
            (durable.session_id,),
        ).fetchone()
        if lineage is None or tuple(lineage) != (
            durable.execution_root_id,
            durable.order.account_fingerprint,
        ):
            raise PersistenceIntegrityError("exchange order session lineage mismatch")
        return durable

    def list_orders(self) -> tuple[DurableExchangeOrder, ...]:
        keys = self._connection.execute(
            "SELECT durable_command_key FROM exchange_orders ORDER BY created_at_ms,durable_command_key"
        ).fetchall()
        return tuple(self.load_order(row[0]) for row in keys)

    def recover_local(self) -> LocalExchangeRecoveryResult:
        roots = self._connection.execute(
            "SELECT execution_root_id FROM exchange_execution_roots ORDER BY execution_root_id"
        ).fetchall()
        for root in roots:
            self.load_bootstrap(root[0])
            if self._trading.config.expected_schema_version >= 3:
                self.read_reconciliation_state(root[0])
        orders = self.list_orders()
        phase3_keys: set[str] = set()
        if self._trading.config.expected_schema_version >= 5:
            self._validate_phase3_authorities()
            phase3_keys = {
                row[0]
                for row in self._connection.execute(
                    "SELECT consumed_durable_command_key FROM exchange_smoke_authorities"
                ).fetchall()
            }
        prepared = tuple(
            v
            for v in orders
            if self.restart_disposition(v) is RestartDisposition.FIRST_SUBMISSION_ELIGIBLE
            and v.order.durable_command_key not in phase3_keys
        )
        reconciliation = tuple(
            v
            for v in orders
            if self.restart_disposition(v) is RestartDisposition.RECONCILIATION_REQUIRED
            or (
                v.order.status is ExchangeOrderStatus.PREPARED
                and v.order.durable_command_key in phase3_keys
            )
        )
        terminal = tuple(
            v for v in orders if self.restart_disposition(v) is RestartDisposition.TERMINAL
        )
        return LocalExchangeRecoveryResult(
            LocalExchangeRecoveryStatus.CLEAN, prepared, reconciliation, terminal
        )

    def _validate_phase3_authorities(self) -> None:
        rows = self._connection.execute(
            "SELECT a.*,o.execution_root_id AS order_root,o.session_id AS order_session,"
            "o.account_fingerprint AS order_account,o.side AS order_side,"
            "p.execution_root_id AS portfolio_root,p.state_digest AS portfolio_initial_digest,"
            "p.state_payload AS portfolio_initial_payload "
            "FROM exchange_smoke_authorities a LEFT JOIN exchange_orders o "
            "ON o.durable_command_key=a.consumed_durable_command_key "
            "LEFT JOIN exchange_portfolio_roots p ON p.portfolio_id=a.managed_portfolio_id "
            "ORDER BY a.authority_id"
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload"])
                expected_payload = {
                    name: row[name]
                    for name in (
                        "authority_id",
                        "execution_root_id",
                        "session_id",
                        "account_fingerprint",
                        "managed_portfolio_id",
                        "venue",
                        "environment",
                        "symbol",
                        "side",
                        "control_generation",
                        "reconciliation_generation",
                        "operator_run_id",
                        "economic_slot_key",
                        "policy_version",
                        "rules_fingerprint",
                        "config_fingerprint",
                        "risk_envelope_digest",
                        "balance_scope_digest",
                        "consumed_durable_command_key",
                        "created_at_ms",
                        "consumed_at_ms",
                        "status",
                    )
                }
                if (
                    canonical_json(payload) != row["payload"]
                    or canonical_sha256(payload, algorithm_version=row["policy_version"])
                    != row["payload_digest"]
                ):
                    raise PersistenceIntegrityError("Phase 3 authority payload digest mismatch")
                if payload != expected_payload:
                    raise PersistenceIntegrityError("Phase 3 authority payload/column mismatch")
                envelope = json.loads(row["risk_envelope_payload"])
                balance = json.loads(row["balance_scope_payload"])
                exact_envelope = {
                    "managed_quote_allocation": {"$decimal": "25"},
                    "max_order_notional": {"$decimal": "12"},
                    "max_submitted_notional": {"$decimal": "12"},
                    "max_turnover": {"$decimal": "12"},
                    "max_managed_position_notional": {"$decimal": "12"},
                    "min_retained_reserve": {"$decimal": "13"},
                    "max_submissions": 1,
                    "max_non_terminal": 1,
                    "max_in_flight": 1,
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "assumptions_fingerprint": row["rules_fingerprint"],
                }
                if envelope != exact_envelope:
                    # The assumptions digest may differ from the rules digest, but every
                    # financial/count/side field is fixed by ADR-064.
                    comparable = dict(envelope)
                    comparable["assumptions_fingerprint"] = row["rules_fingerprint"]
                    if comparable != exact_envelope:
                        raise PersistenceIntegrityError("Phase 3 envelope is broader than policy")
                if (
                    balance.get("account_fingerprint") != row["account_fingerprint"]
                    or balance.get("managed_quote_allocation") != {"$decimal": "25"}
                    or balance.get("interpretation")
                    != "btc-external-unmanaged;usdt-managed-allocation-only"
                    or deserialize_decimal(balance["usdt_free"]["$decimal"]) < Decimal(25)
                ):
                    raise PersistenceIntegrityError("Phase 3 balance scope violates policy")
                portfolio = json.loads(row["portfolio_initial_payload"])
                if (
                    row["portfolio_root"] != row["execution_root_id"]
                    or portfolio["version"] != 0
                    or portfolio["portfolio_id"] != row["managed_portfolio_id"]
                    or portfolio["base_balance"] != {"$decimal": "0"}
                    or portfolio["quote_balance"] != {"$decimal": "25"}
                    or portfolio["initial_equity"] != {"$decimal": "25"}
                    or portfolio["position"] is not None
                    or portfolio["turnover"] != {"$decimal": "0"}
                ):
                    raise PersistenceIntegrityError(
                        "Phase 3 managed Portfolio is not zero-BTC/25-USDT"
                    )
                if (
                    canonical_sha256(envelope, algorithm_version=row["policy_version"])
                    != row["risk_envelope_digest"]
                ):
                    raise PersistenceIntegrityError("Phase 3 envelope digest mismatch")
                if (
                    canonical_sha256(balance, algorithm_version="phase3-balance-scope/v1")
                    != row["balance_scope_digest"]
                ):
                    raise PersistenceIntegrityError("Phase 3 balance digest mismatch")
                if (
                    row["status"] != "CONSUMED"
                    or row["consumed_at_ms"] < row["created_at_ms"]
                    or tuple(row[name] for name in ("order_root", "order_session", "order_account"))
                    != tuple(
                        row[name]
                        for name in ("execution_root_id", "session_id", "account_fingerprint")
                    )
                    or row["order_side"] != "BUY"
                    or row["economic_slot_key"]
                    != derive_phase3_economic_slot(
                        row["execution_root_id"], row["account_fingerprint"]
                    )
                ):
                    raise PersistenceIntegrityError("Phase 3 authority linkage is invalid")
                events = self._connection.execute(
                    "SELECT * FROM exchange_smoke_authority_events WHERE authority_id=? ORDER BY event_ordinal",
                    (row["authority_id"],),
                ).fetchall()
                if len(events) != 1 or events[0]["event_ordinal"] != 1:
                    raise PersistenceIntegrityError("Phase 3 authority event continuity is invalid")
                event_payload = json.loads(events[0]["payload"])
                if (
                    canonical_json(event_payload) != events[0]["payload"]
                    or canonical_sha256(
                        event_payload, algorithm_version="phase3-authority-event/v1"
                    )
                    != events[0]["payload_digest"]
                ):
                    raise PersistenceIntegrityError("Phase 3 authority event digest mismatch")
                if (
                    events[0]["durable_command_key"] != row["consumed_durable_command_key"]
                    or events[0]["operator_run_id"] != row["operator_run_id"]
                ):
                    raise PersistenceIntegrityError("Phase 3 authority event linkage is invalid")
                command_row = self._connection.execute(
                    "SELECT command_payload FROM exchange_orders WHERE durable_command_key=?",
                    (row["consumed_durable_command_key"],),
                ).fetchone()
                command = json.loads(command_row[0])
                if (
                    command["instrument_rules_fingerprint"] != row["rules_fingerprint"]
                    or command["execution_configuration_fingerprint"] != row["config_fingerprint"]
                ):
                    raise PersistenceIntegrityError(
                        "Phase 3 authority command fingerprints conflict"
                    )
            except (json.JSONDecodeError, TypeError, KeyError) as error:
                raise PersistenceIntegrityError("Phase 3 authority is corrupted") from error

    @staticmethod
    def restart_disposition(value: DurableExchangeOrder) -> RestartDisposition:
        if value.order.status is ExchangeOrderStatus.PREPARED:
            return RestartDisposition.FIRST_SUBMISSION_ELIGIBLE
        if value.order.status.is_exchange_terminal:
            return RestartDisposition.TERMINAL
        return RestartDisposition.RECONCILIATION_REQUIRED

    def list_orders_requiring_reconciliation(self) -> tuple[DurableExchangeOrder, ...]:
        return self.recover_local().reconciliation_required

    def list_submit_eligible(self) -> tuple[DurableExchangeOrder, ...]:
        return self.recover_local().prepared

    def trade_completeness(self, durable_command_key: str) -> TradeCompletenessStatus:
        order = self.load_order(durable_command_key).order
        rows = self._connection.execute(
            "SELECT quantity FROM exchange_trades WHERE durable_command_key=?",
            (durable_command_key,),
        ).fetchall()
        total = _sum_decimals([deserialize_decimal(row[0]) for row in rows])
        if total > order.cumulative_executed_base_quantity:
            return TradeCompletenessStatus.CONFLICT
        if total < order.cumulative_executed_base_quantity:
            return TradeCompletenessStatus.INCOMPLETE
        return TradeCompletenessStatus.COMPLETE

    def trade_count(self, durable_command_key: str) -> int:
        self.load_order(durable_command_key)
        return self._connection.execute(
            "SELECT count(*) FROM exchange_trades WHERE durable_command_key=?",
            (durable_command_key,),
        ).fetchone()[0]

    def read_reconciliation_state(self, execution_root_id: str) -> ExchangeReconciliationState:
        self._trading._require_open()
        if self._trading.config.expected_schema_version < 3:
            raise PersistenceIntegrityError("reconciliation state requires schema version 3")
        row = self._connection.execute(
            "SELECT * FROM exchange_reconciliation_current WHERE execution_root_id=?",
            (execution_root_id,),
        ).fetchone()
        if row is None:
            raise PersistenceIntegrityError("exchange reconciliation state is missing")
        payload = {
            "execution_root_id": row["execution_root_id"],
            "status": row["status"],
            "generation": row["generation"],
            "latest_reconciliation_id": row["latest_reconciliation_id"],
            "updated_at_ms": row["updated_at_ms"],
            "model_version": row["model_version"],
        }
        if (
            canonical_json(payload) != row["state_payload"]
            or canonical_sha256(payload, algorithm_version=row["model_version"])
            != row["state_digest"]
        ):
            raise PersistenceIntegrityError("exchange reconciliation state digest mismatch")
        if row["model_version"] != "exchange-reconciliation-state/v1":
            raise PersistenceIntegrityError("unsupported reconciliation state model version")
        control = self.read_control_state(execution_root_id)
        if control.reconciliation_generation != row["generation"]:
            raise PersistenceIntegrityError("reconciliation/control generation mismatch")
        if row["latest_reconciliation_id"] is not None:
            self._validate_latest_reconciliation_run(row)
        try:
            return ExchangeReconciliationState(
                row["execution_root_id"],
                ExchangeReconciliationStatus(row["status"]),
                row["generation"],
                row["latest_reconciliation_id"],
                row["updated_at_ms"],
                row["state_digest"],
                row["model_version"],
            )
        except (ValueError, TypeError) as error:
            raise PersistenceIntegrityError("exchange reconciliation state is invalid") from error

    def record_reconciliation_run(
        self, record: PersistentExchangeReconciliationRun
    ) -> ExchangeReconciliationState:
        self._trading._require_open()
        if not isinstance(record, PersistentExchangeReconciliationRun):
            raise PersistenceIntegrityError("invalid reconciliation run")
        if (
            self._trading.config.expected_schema_version >= 4
            and record.outcome is ExchangeReconciliationStatus.SAFE
            and (
                record.coverage_start_ms is None
                or not record.lower_bound_complete
                or not record.upper_bound_complete
            )
        ):
            raise PersistenceIntegrityError(
                "schema-v4 SAFE reconciliation requires complete history coverage"
            )
        state = self.read_reconciliation_state(record.execution_root_id)
        result_values = {name: getattr(record, name) for name in record.__dataclass_fields__}
        if self._trading.config.expected_schema_version < 4:
            for name in (
                "coverage_start_ms",
                "coverage_end_ms",
                "lower_bound_complete",
                "upper_bound_complete",
                "trade_scope",
                "coverage_algorithm_version",
            ):
                result_values.pop(name)
        result_payload = canonical_json(result_values)
        result_digest = canonical_sha256(result_payload, algorithm_version=record.model_version)
        existing = self._connection.execute(
            "SELECT result_digest FROM exchange_reconciliation_runs WHERE reconciliation_id=?",
            (record.reconciliation_id,),
        ).fetchone()
        if existing is not None:
            if existing[0] != result_digest:
                raise PersistenceConflictError("reconciliation ID has conflicting result")
            return self.read_reconciliation_state(record.execution_root_id)
        if state.generation != record.generation_before:
            raise PersistenceConflictError("reconciliation generation is stale")
        bootstrap = self.load_bootstrap(record.execution_root_id)
        if (
            bootstrap.session.session_id != record.session_id
            or bootstrap.root.account_fingerprint != record.account_fingerprint
        ):
            raise PersistenceConflictError("reconciliation lineage mismatch")
        reasons_payload = canonical_json(record.reasons)
        state_payload = {
            "execution_root_id": record.execution_root_id,
            "status": record.outcome,
            "generation": record.generation_after,
            "latest_reconciliation_id": record.reconciliation_id,
            "updated_at_ms": record.completed_at_ms,
            "model_version": "exchange-reconciliation-state/v1",
        }
        state_json = canonical_json(state_payload)
        state_digest = canonical_sha256(
            state_payload, algorithm_version="exchange-reconciliation-state/v1"
        )
        connection = self._connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO exchange_reconciliation_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.reconciliation_id,
                    record.execution_root_id,
                    record.session_id,
                    record.account_fingerprint,
                    record.generation_before,
                    record.generation_after,
                    record.outcome.value,
                    record.orders_examined,
                    record.orders_advanced,
                    record.trades_persisted,
                    record.open_orders_examined,
                    record.balances_examined,
                    reasons_payload,
                    record.started_at_ms,
                    record.completed_at_ms,
                    record.model_version,
                    result_payload,
                    result_digest,
                ),
            )
            if (
                record.outcome is ExchangeReconciliationStatus.SAFE
                and self._trading.config.expected_schema_version >= 4
            ):
                evidence = {
                    "reconciliation_id": record.reconciliation_id,
                    "execution_root_id": record.execution_root_id,
                    "session_id": record.session_id,
                    "account_fingerprint": record.account_fingerprint,
                    "generation": record.generation_after,
                    "coverage_start_ms": record.coverage_start_ms,
                    "coverage_end_ms": record.coverage_end_ms,
                    "lower_bound_complete": record.lower_bound_complete,
                    "upper_bound_complete": record.upper_bound_complete,
                    "trade_scope": record.trade_scope,
                    "algorithm_version": record.coverage_algorithm_version,
                }
                evidence_payload = canonical_json(evidence)
                connection.execute(
                    "INSERT INTO exchange_reconciliation_coverage VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        record.reconciliation_id,
                        record.execution_root_id,
                        record.session_id,
                        record.account_fingerprint,
                        record.generation_after,
                        record.coverage_start_ms,
                        record.coverage_end_ms,
                        int(record.lower_bound_complete),
                        int(record.upper_bound_complete),
                        record.trade_scope,
                        record.coverage_algorithm_version,
                        evidence_payload,
                        canonical_sha256(
                            evidence, algorithm_version=record.coverage_algorithm_version
                        ),
                    ),
                )
            self._hit("reconciliation_after_run")
            cursor = connection.execute(
                "UPDATE exchange_reconciliation_current SET status=?,generation=?,"
                "latest_reconciliation_id=?,updated_at_ms=?,model_version=?,state_payload=?,"
                "state_digest=? WHERE execution_root_id=? AND generation=?",
                (
                    record.outcome.value,
                    record.generation_after,
                    record.reconciliation_id,
                    record.completed_at_ms,
                    "exchange-reconciliation-state/v1",
                    state_json,
                    state_digest,
                    record.execution_root_id,
                    record.generation_before,
                ),
            )
            if cursor.rowcount != 1:
                raise PersistenceConflictError("reconciliation generation race")
            self._hit("reconciliation_after_current")
            control_cursor = connection.execute(
                "UPDATE exchange_control_state SET reconciliation_generation=? "
                "WHERE execution_root_id=? AND reconciliation_generation=?",
                (record.generation_after, record.execution_root_id, record.generation_before),
            )
            if control_cursor.rowcount != 1:
                raise PersistenceConflictError("reconciliation control generation race")
            self._hit("reconciliation_after_control")
            self._hit("reconciliation_before_commit")
            connection.commit()
            self._hit("reconciliation_after_commit")
        except PersistenceConflictError:
            connection.rollback()
            raise
        except sqlite3.IntegrityError as error:
            connection.rollback()
            raise PersistenceConflictError("reconciliation run conflicts") from error
        except sqlite3.Error as error:
            connection.rollback()
            raise PersistenceTransactionError("reconciliation transaction failed") from error
        except BaseException:
            connection.rollback()
            raise
        return self.read_reconciliation_state(record.execution_root_id)

    def _insert_initial_reconciliation_state(
        self, execution_root_id: str, occurred_at_ms: int
    ) -> None:
        payload = {
            "execution_root_id": execution_root_id,
            "status": ExchangeReconciliationStatus.REQUIRED,
            "generation": 0,
            "latest_reconciliation_id": None,
            "updated_at_ms": occurred_at_ms,
            "model_version": "exchange-reconciliation-state/v1",
        }
        self._connection.execute(
            "INSERT INTO exchange_reconciliation_current VALUES(?,?,?,?,?,?,?,?)",
            (
                execution_root_id,
                ExchangeReconciliationStatus.REQUIRED.value,
                0,
                None,
                occurred_at_ms,
                "exchange-reconciliation-state/v1",
                canonical_json(payload),
                canonical_sha256(payload, algorithm_version="exchange-reconciliation-state/v1"),
            ),
        )

    def _validate_latest_reconciliation_run(self, state_row: sqlite3.Row) -> None:
        run = self._connection.execute(
            "SELECT * FROM exchange_reconciliation_runs WHERE reconciliation_id=?",
            (state_row["latest_reconciliation_id"],),
        ).fetchone()
        if run is None:
            raise PersistenceIntegrityError("latest reconciliation run is missing")
        coverage = (
            self._connection.execute(
                "SELECT * FROM exchange_reconciliation_coverage WHERE reconciliation_id=?",
                (state_row["latest_reconciliation_id"],),
            ).fetchone()
            if self._trading.config.expected_schema_version >= 4
            else None
        )
        try:
            column_values = {
                "reconciliation_id": run["reconciliation_id"],
                "execution_root_id": run["execution_root_id"],
                "session_id": run["session_id"],
                "account_fingerprint": run["account_fingerprint"],
                "generation_before": run["generation_before"],
                "generation_after": run["generation_after"],
                "outcome": run["outcome"],
                "orders_examined": run["orders_examined"],
                "orders_advanced": run["orders_advanced"],
                "trades_persisted": run["trades_persisted"],
                "open_orders_examined": run["open_orders_examined"],
                "balances_examined": run["balances_examined"],
                "reasons": json.loads(run["reasons_payload"]),
                "started_at_ms": run["started_at_ms"],
                "completed_at_ms": run["completed_at_ms"],
                "model_version": run["model_version"],
            }
            result_values = json.loads(run["result_payload"])
            if any(result_values.get(name) != value for name, value in column_values.items()):
                raise ValueError("reconciliation run columns conflict with payload")
            expected_payload = canonical_json(result_values)
            expected_digest = canonical_sha256(
                expected_payload, algorithm_version=run["model_version"]
            )
            if coverage is not None:
                expected_coverage = {
                    "coverage_start_ms": coverage["coverage_start_ms"],
                    "coverage_end_ms": coverage["coverage_end_ms"],
                    "lower_bound_complete": bool(coverage["lower_bound_complete"]),
                    "upper_bound_complete": bool(coverage["upper_bound_complete"]),
                    "trade_scope": coverage["trade_scope"],
                    "coverage_algorithm_version": coverage["algorithm_version"],
                }
                if any(
                    result_values.get(name) != value for name, value in expected_coverage.items()
                ):
                    raise ValueError("reconciliation coverage conflicts with run payload")
        except Exception as error:
            raise PersistenceIntegrityError("reconciliation run payload is invalid") from error
        if (
            run["model_version"] != "exchange-reconciliation-run/v1"
            or run["result_payload"] != expected_payload
            or run["result_digest"] != expected_digest
            or run["execution_root_id"] != state_row["execution_root_id"]
            or run["generation_after"] != state_row["generation"]
            or run["outcome"] != state_row["status"]
            or run["completed_at_ms"] != state_row["updated_at_ms"]
        ):
            raise PersistenceIntegrityError("latest reconciliation run/state mismatch")
        lineage = self._connection.execute(
            "SELECT execution_root_id,account_fingerprint FROM exchange_sessions "
            "WHERE session_id=?",
            (run["session_id"],),
        ).fetchone()
        if lineage is None or tuple(lineage) != (
            run["execution_root_id"],
            run["account_fingerprint"],
        ):
            raise PersistenceIntegrityError("reconciliation run session lineage mismatch")

    def read_control_state(self, execution_root_id: str) -> ExchangeControlState:
        row = self._connection.execute(
            "SELECT * FROM exchange_control_state WHERE execution_root_id=?", (execution_root_id,)
        ).fetchone()
        if row is None:
            raise PersistenceIntegrityError("exchange control state is missing")
        return ExchangeControlState(
            execution_root_id,
            KillSwitch(
                KillSwitchState(row["kill_switch_state"]),
                row["control_generation"],
                row["reason_code"],
                row["updated_at_ms"],
                row["model_version"],
            ),
            row["reconciliation_generation"],
            row["audit_identifier"],
        )

    def set_kill_switch(
        self,
        execution_root_id: str,
        *,
        state: KillSwitchState,
        expected_generation: int,
        reason_code: str,
        audit_identifier: str,
        occurred_at_ms: int,
    ) -> ExchangeControlState:
        current = self.read_control_state(execution_root_id)
        if not isinstance(state, KillSwitchState):
            raise PersistenceIntegrityError("kill-switch target state is invalid")
        if isinstance(expected_generation, bool) or not isinstance(expected_generation, int):
            raise PersistenceIntegrityError("expected kill-switch generation must be an integer")
        if expected_generation != current.kill_switch.generation:
            raise PersistenceConflictError("kill-switch generation is stale")
        if (
            not isinstance(reason_code, str)
            or not reason_code.strip()
            or not isinstance(audit_identifier, str)
            or not audit_identifier.strip()
        ):
            raise PersistenceIntegrityError("kill-switch transition requires reason and audit ID")
        next_generation = current.kill_switch.generation + 1
        cursor = self._connection.execute(
            "UPDATE exchange_control_state SET kill_switch_state=?,control_generation=?,"
            "reason_code=?,audit_identifier=?,updated_at_ms=? WHERE execution_root_id=? "
            "AND control_generation=?",
            (
                state.value,
                next_generation,
                reason_code,
                audit_identifier,
                occurred_at_ms,
                execution_root_id,
                expected_generation,
            ),
        )
        if cursor.rowcount != 1:
            raise PersistenceConflictError("kill-switch transition lost generation race")
        return self.read_control_state(execution_root_id)

    def load_session(self, session_id: str) -> ExecutionSession:
        row = self._connection.execute(
            "SELECT * FROM exchange_sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if row is None:
            raise PersistenceIntegrityError("execution session is missing")
        return self._session(row)

    @staticmethod
    def _session(row: sqlite3.Row) -> ExecutionSession:
        return ExecutionSession(
            row["session_id"],
            row["account_fingerprint"],
            row["authorized_at_ms"],
            ExecutionSessionState(row["state"]),
            deserialize_decimal(row["submitted_notional"]),
            deserialize_decimal(row["executed_turnover"]),
            row["submission_count"],
            row["execution_failure_count"],
            model_version=row["model_version"],
        )

    @staticmethod
    def _durable(row: sqlite3.Row) -> DurableExchangeOrder:
        order = ExchangeOrder(
            row["durable_command_key"],
            row["client_order_id"],
            row["account_fingerprint"],
            MarketStreamKey(row["symbol"], row["interval"]),
            ExchangeOrderSide(row["side"]),
            deserialize_decimal(row["original_quantity"]),
            ExchangeOrderStatus(row["status"]),
            row["created_at_ms"],
            row["exchange_order_id"],
            deserialize_decimal(row["cumulative_executed_base"]),
            deserialize_decimal(row["cumulative_quote"]),
            row["submitted_at_ms"],
            row["acknowledged_at_ms"],
            row["latest_exchange_update_ms"],
            ExecutionEnvironment(row["environment"]),
            row["venue"],
        )
        return DurableExchangeOrder(
            order,
            row["stable_decision_key"],
            row["execution_root_id"],
            row["session_id"],
            deserialize_decimal(row["approved_notional"]),
            row["command_payload"],
            row["state_digest"],
            row["version"],
        )

    def _write_transition(
        self,
        current: DurableExchangeOrder,
        order: ExchangeOrder,
        source: ExchangeOrderEventSource,
        reason_code: str,
        occurred_at_ms: int,
        exchange_timestamp_ms: int | None = None,
    ) -> None:
        version = current.version + 1
        payload = canonical_json(_order_payload(order))
        digest = _order_digest(order)
        cursor = self._connection.execute(
            "UPDATE exchange_orders SET status=?,exchange_order_id=?,cumulative_executed_base=?,"
            "cumulative_quote=?,submitted_at_ms=?,acknowledged_at_ms=?,latest_exchange_update_ms=?,"
            "terminal=?,version=?,state_payload=?,state_digest=? WHERE durable_command_key=? AND version=?",
            (
                order.status.value,
                order.exchange_order_id,
                serialize_decimal(order.cumulative_executed_base_quantity),
                serialize_decimal(order.cumulative_quote_quantity),
                order.submitted_at_ms,
                order.acknowledged_at_ms,
                order.latest_exchange_update_ms,
                int(order.status.is_exchange_terminal),
                version,
                payload,
                digest,
                order.durable_command_key,
                current.version,
            ),
        )
        if cursor.rowcount != 1:
            raise PersistenceConflictError("exchange order version conflict")
        self._append_event(
            order,
            current.order.status,
            version,
            source,
            reason_code,
            occurred_at_ms,
            exchange_timestamp_ms,
        )

    def _append_event(
        self,
        order: ExchangeOrder,
        previous: ExchangeOrderStatus | None,
        version: int,
        source: ExchangeOrderEventSource,
        reason_code: str,
        occurred_at_ms: int,
        exchange_timestamp_ms: int | None = None,
    ) -> None:
        event_payload = {
            "durable_command_key": order.durable_command_key,
            "order_version": version,
            "previous_status": previous,
            "new_status": order.status,
            "source": source,
            "reason_code": reason_code,
            "exchange_order_id": order.exchange_order_id,
            "cumulative_executed_base": order.cumulative_executed_base_quantity,
            "cumulative_quote": order.cumulative_quote_quantity,
            "exchange_timestamp_ms": exchange_timestamp_ms,
            "occurred_at_ms": occurred_at_ms,
        }
        payload = canonical_json(event_payload)
        digest = canonical_sha256(event_payload, algorithm_version="exchange-order-event/v1")
        event_id = canonical_sha256(
            {"durable_command_key": order.durable_command_key, "order_version": version},
            algorithm_version="exchange-order-event-id/v1",
        )
        self._connection.execute(
            "INSERT INTO exchange_order_events VALUES(NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                order.durable_command_key,
                version,
                None if previous is None else previous.value,
                order.status.value,
                source.value,
                reason_code,
                order.exchange_order_id,
                serialize_decimal(order.cumulative_executed_base_quantity),
                serialize_decimal(order.cumulative_quote_quantity),
                exchange_timestamp_ms,
                occurred_at_ms,
                payload,
                digest,
            ),
        )
