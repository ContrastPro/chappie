import json
import sqlite3
from collections.abc import Callable
from decimal import Decimal, localcontext
from typing import Self

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
)
from app.persistence.exchange_models import (
    DurableExchangeOrder,
    ExchangeControlState,
    ExchangeExecutionRoot,
    ExecutionBootstrap,
    LocalExchangeRecoveryResult,
    LocalExchangeRecoveryStatus,
    RestartDisposition,
    TradeCompletenessStatus,
    TradeSettlementStatus,
)
from app.persistence.models import SQLitePersistenceConfig
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
    """Schema-v2 exchange lifecycle persistence; synchronous and single-writer."""

    def __init__(
        self,
        config: SQLitePersistenceConfig,
        *,
        failure_injector: Callable[[str], None] | None = None,
    ) -> None:
        if not isinstance(config, SQLitePersistenceConfig) or config.expected_schema_version != 2:
            raise PersistenceIntegrityError("exchange persistence requires schema version 2")
        self._trading = SQLiteTradingPersistence(config)
        self._connection = self._trading._connection
        if failure_injector is not None and not callable(failure_injector):
            self._trading.close()
            raise PersistenceIntegrityError("failure_injector must be callable")
        self._failure_injector = failure_injector

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
        except Exception:
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
        except Exception:
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
        except Exception:
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
        orders = self.list_orders()
        prepared = tuple(
            v
            for v in orders
            if self.restart_disposition(v) is RestartDisposition.FIRST_SUBMISSION_ELIGIBLE
        )
        reconciliation = tuple(
            v
            for v in orders
            if self.restart_disposition(v) is RestartDisposition.RECONCILIATION_REQUIRED
        )
        terminal = tuple(
            v for v in orders if self.restart_disposition(v) is RestartDisposition.TERMINAL
        )
        return LocalExchangeRecoveryResult(
            LocalExchangeRecoveryStatus.CLEAN, prepared, reconciliation, terminal
        )

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
