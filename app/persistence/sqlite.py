import hashlib
import json
import sqlite3
from contextlib import closing
from importlib import resources
from pathlib import Path
from types import TracebackType
from typing import Self

from app.market.models import MarketStreamKey
from app.persistence.canonical import deserialize_decimal, serialize_decimal
from app.persistence.exceptions import (
    PersistenceCheckpointError,
    PersistenceConflictError,
    PersistenceConnectionError,
    PersistenceDecisionSlotConflictError,
    PersistenceIntegrityError,
    PersistenceMigrationError,
    PersistenceSchemaError,
    PersistenceTransactionError,
    PersistenceWriterConflictError,
)
from app.persistence.models import (
    CycleOutcome,
    DurableDecisionState,
    InitialPortfolioRecord,
    PersistenceStatus,
    PersistentCommandRecord,
    PersistentDecisionRecord,
    PersistentFillRecord,
    PortfolioCheckpoint,
    PortfolioState,
    PortfolioTransitionRecord,
    PositionState,
    SQLitePersistenceConfig,
)

MIGRATION_NAMES = (
    "001_initial.sql",
    "002_exchange_execution.sql",
    "003_exchange_reconciliation.sql",
    "004_exchange_settlement.sql",
    "005_phase3_smoke_authority.sql",
    "006_zero_commission_resolution.sql",
    "007_stage4_managed_sell_authority.sql",
    "008_stage4_sell_recovery.sql",
)


def _state_from_payload(payload: str) -> PortfolioState:
    try:
        raw = json.loads(payload)
        position_raw = raw["position"]
        position = None
        if position_raw is not None:
            position = PositionState(
                quantity=deserialize_decimal(position_raw["quantity"]["$decimal"]),
                cost_basis=deserialize_decimal(position_raw["cost_basis"]["$decimal"]),
                average_entry_price=deserialize_decimal(
                    position_raw["average_entry_price"]["$decimal"]
                ),
                realised_pnl=deserialize_decimal(position_raw["realised_pnl"]["$decimal"]),
                unrealised_pnl=deserialize_decimal(position_raw["unrealised_pnl"]["$decimal"]),
                last_mark_price=deserialize_decimal(position_raw["last_mark_price"]["$decimal"]),
                opened_at_ms=position_raw["opened_at_ms"],
                updated_at_ms=position_raw["updated_at_ms"],
            )
        decimal_names = (
            "quote_balance",
            "base_balance",
            "realised_pnl",
            "unrealised_pnl",
            "total_fees",
            "initial_equity",
            "turnover",
            "peak_equity",
            "current_equity",
            "drawdown",
            "max_drawdown",
        )
        values = {name: deserialize_decimal(raw[name]["$decimal"]) for name in decimal_names}
        mark_raw = raw["mark_price"]
        return PortfolioState(
            portfolio_id=raw["portfolio_id"],
            stream_key=MarketStreamKey(raw["symbol"], raw["interval"]),
            base_asset=raw["base_asset"],
            quote_asset=raw["quote_asset"],
            position=position,
            mark_price=(None if mark_raw is None else deserialize_decimal(mark_raw["$decimal"])),
            trade_count=raw["trade_count"],
            applied_fill_count=raw["applied_fill_count"],
            as_of_ms=raw["as_of_ms"],
            version=raw["version"],
            correlation_id=raw["correlation_id"],
            **values,
        )
    except PersistenceIntegrityError:
        raise
    except Exception as error:
        raise PersistenceIntegrityError("invalid persisted Portfolio state") from error


def deserialize_portfolio_state(payload: str) -> PortfolioState:
    """Decode a canonical durable Portfolio state for sibling persistence adapters."""
    return _state_from_payload(payload)


class SQLitePaperCycleTransaction:
    def __init__(self, adapter: "SQLiteTradingPersistence") -> None:
        self._adapter = adapter
        self._connection = adapter._connection
        self._active = True
        self._committed = False
        self._terminal_marked = False
        self._transition_recorded = False
        self._current_set = False
        self._command_recorded = False
        self._fill_recorded = False
        self._terminal_outcome: CycleOutcome | None = None
        self._transitions: list[tuple[int, PortfolioTransitionRecord]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._active:
            self.rollback()

    def _require_active(self) -> None:
        if not self._active:
            raise PersistenceTransactionError("transaction is no longer active")

    def _safe_rollback(self) -> None:
        try:
            self._connection.rollback()
        except sqlite3.Error:
            pass

    def claim_decision(self, record: PersistentDecisionRecord) -> bool:
        self._require_active()
        row = self._fetchone(
            "SELECT decision_slot_key,payload,payload_digest FROM paper_decisions "
            "WHERE stable_decision_key=?",
            (record.stable_decision_key,),
        )
        if row is not None:
            if tuple(row) == (record.decision_slot_key, record.payload, record.payload_digest):
                return False
            raise PersistenceConflictError("stable decision key has conflicting payload")
        slot = self._fetchone(
            "SELECT stable_decision_key FROM paper_decisions WHERE decision_slot_key=?",
            (record.decision_slot_key,),
        )
        if slot is not None:
            raise PersistenceDecisionSlotConflictError(
                "decision slot contains different market input"
            )
        self._execute(
            "INSERT INTO paper_decisions VALUES(?,?,?,?,?,NULL)",
            (
                record.stable_decision_key,
                record.decision_slot_key,
                record.payload,
                record.payload_digest,
                record.created_at_ms,
            ),
        )
        return True

    def record_command(self, record: PersistentCommandRecord) -> bool:
        self._require_active()
        row = self._fetchone(
            "SELECT stable_decision_key,payload,payload_digest FROM paper_commands "
            "WHERE durable_command_key=?",
            (record.durable_command_key,),
        )
        if row is not None:
            if tuple(row) == (record.stable_decision_key, record.payload, record.payload_digest):
                self._command_recorded = True
                return False
            raise PersistenceConflictError("durable command key has conflicting payload")
        self._execute(
            "INSERT INTO paper_commands VALUES(?,?,?,?)",
            (
                record.durable_command_key,
                record.stable_decision_key,
                record.payload,
                record.payload_digest,
            ),
        )
        self._command_recorded = True
        return True

    def record_fill(self, record: PersistentFillRecord) -> bool:
        self._require_active()
        existing = self._fetchone(
            "SELECT fill_id FROM fills WHERE durable_command_key=? OR fill_id=?",
            (record.durable_command_key, record.fill_id),
        )
        if existing is not None:
            persisted = self._adapter.load_fill(record.durable_command_key)
            if persisted == record:
                self._fill_recorded = True
                return False
            raise PersistenceConflictError("durable command already has a different Fill")
        self._execute(
            "INSERT INTO fills VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                record.fill_id,
                record.durable_command_key,
                record.order_id,
                record.risk_decision_id,
                record.intent_id,
                record.stream_key.symbol,
                record.stream_key.interval,
                record.side,
                serialize_decimal(record.quantity),
                serialize_decimal(record.price),
                serialize_decimal(record.gross_notional),
                serialize_decimal(record.fee),
                record.fee_asset,
                serialize_decimal(record.net_quote_change),
                serialize_decimal(record.net_base_change),
                record.filled_at_ms,
                int(record.dust_liquidation),
                record.correlation_id,
                record.model_version,
            ),
        )
        self._fill_recorded = True
        return True

    def record_portfolio_transition(self, record: PortfolioTransitionRecord) -> int:
        self._require_active()
        cursor = self._execute(
            "INSERT INTO journal_entries(entry_type,portfolio_id,stable_decision_key,"
            "durable_command_key,fill_id,portfolio_version_before,portfolio_version_after,"
            "before_digest,after_state_payload,after_state_digest,domain_timestamp_ms,model_version)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                record.transition_type,
                record.portfolio_id,
                record.stable_decision_key,
                record.durable_command_key,
                record.fill_id,
                record.version_before,
                record.version_after,
                record.before_digest,
                record.after_state.canonical_payload,
                record.after_state.digest,
                record.domain_timestamp_ms,
                record.model_version,
            ),
        )
        self._transition_recorded = True
        journal_seq = int(cursor.lastrowid)
        self._transitions.append((journal_seq, record))
        return journal_seq

    def set_portfolio_current(self, state: PortfolioState, journal_seq: int) -> None:
        self._require_active()
        existing = self._fetchone(
            "SELECT portfolio_version,state_digest FROM portfolio_current WHERE portfolio_id=?",
            (state.portfolio_id,),
        )
        if not self._transitions:
            raise PersistenceConflictError("portfolio_current requires a transition")
        last_seq, last_transition = self._transitions[-1]
        expected_before = existing[0] if existing is not None else 0
        if self._transitions[0][1].version_before != expected_before:
            raise PersistenceConflictError("portfolio transition does not continue current state")
        for (_, previous), (_, following) in zip(
            self._transitions, self._transitions[1:], strict=False
        ):
            if (
                following.version_before != previous.version_after
                or following.before_digest != previous.after_state.digest
            ):
                raise PersistenceConflictError("portfolio transition batch is not contiguous")
        if state != last_transition.after_state or journal_seq != last_seq:
            raise PersistenceConflictError("portfolio_current must match the last transition")
        self._execute(
            "INSERT INTO portfolio_current VALUES(?,?,?,?,?) ON CONFLICT(portfolio_id) DO UPDATE "
            "SET portfolio_version=excluded.portfolio_version,state_payload=excluded.state_payload,"
            "state_digest=excluded.state_digest,last_journal_seq=excluded.last_journal_seq",
            (state.portfolio_id, state.version, state.canonical_payload, state.digest, journal_seq),
        )
        self._current_set = True

    def create_checkpoint(self, state: PortfolioState, journal_seq: int) -> bool:
        self._require_active()
        try:
            self._execute(
                "INSERT INTO portfolio_checkpoints VALUES(?,?,?,?,?,?)",
                (
                    state.portfolio_id,
                    state.version,
                    state.canonical_payload,
                    state.digest,
                    journal_seq,
                    "portfolio-checkpoint/v1",
                ),
            )
        except PersistenceConflictError:
            row = self._fetchone(
                "SELECT state_digest,last_journal_seq FROM portfolio_checkpoints "
                "WHERE portfolio_id=? AND portfolio_version=?",
                (state.portfolio_id, state.version),
            )
            if row is not None and tuple(row) == (state.digest, journal_seq):
                return False
            raise
        return True

    def mark_terminal(self, stable_decision_key: str, outcome: CycleOutcome) -> None:
        self._require_active()
        cursor = self._execute(
            "UPDATE paper_decisions SET terminal_outcome=? "
            "WHERE stable_decision_key=? AND terminal_outcome IS NULL",
            (outcome.value, stable_decision_key),
        )
        if cursor.rowcount != 1:
            row = self._fetchone(
                "SELECT terminal_outcome FROM paper_decisions WHERE stable_decision_key=?",
                (stable_decision_key,),
            )
            if row is None or row[0] != outcome.value:
                raise PersistenceConflictError("decision terminal outcome conflict")
        self._terminal_marked = True
        self._terminal_outcome = outcome

    def commit(self) -> None:
        self._require_active()
        if not self._terminal_marked:
            self.rollback()
            raise PersistenceTransactionError("financial cycle must be terminal before commit")
        if self._transition_recorded and not self._current_set:
            self.rollback()
            raise PersistenceTransactionError(
                "portfolio transition requires portfolio_current in the same transaction"
            )
        outcome = self._terminal_outcome
        if outcome is CycleOutcome.EXECUTED:
            if not (
                self._command_recorded
                and self._fill_recorded
                and self._transition_recorded
                and self._current_set
            ):
                self.rollback()
                raise PersistenceTransactionError(
                    "EXECUTED requires command, Fill and committed Portfolio transition"
                )
        elif outcome in {
            CycleOutcome.HOLD,
            CycleOutcome.PLANNER_DECLINED,
            CycleOutcome.RISK_REJECTED,
        } and (self._command_recorded or self._fill_recorded):
            self.rollback()
            raise PersistenceTransactionError(f"{outcome.value} must not contain command or Fill")
        elif outcome is CycleOutcome.NOT_EXECUTED and (
            not self._command_recorded or self._fill_recorded
        ):
            self.rollback()
            raise PersistenceTransactionError("NOT_EXECUTED requires command and forbids Fill")
        try:
            self._connection.commit()
        except sqlite3.Error as error:
            self._safe_rollback()
            raise PersistenceTransactionError("financial transaction commit failed") from error
        finally:
            self._active = False
            self._adapter._transaction_active = False
        self._committed = True

    def rollback(self) -> None:
        self._require_active()
        try:
            self._safe_rollback()
        finally:
            self._active = False
            self._adapter._transaction_active = False

    def _execute(self, statement: str, parameters: tuple[object, ...]) -> sqlite3.Cursor:
        try:
            return self._connection.execute(statement, parameters)
        except sqlite3.IntegrityError as error:
            raise PersistenceConflictError("SQLite integrity constraint rejected record") from error
        except sqlite3.Error as error:
            raise PersistenceTransactionError("SQLite financial write failed") from error

    def _fetchone(self, statement: str, parameters: tuple[object, ...]) -> sqlite3.Row | None:
        try:
            return self._connection.execute(statement, parameters).fetchone()
        except sqlite3.Error as error:
            raise PersistenceTransactionError("SQLite financial read failed") from error


class SQLiteTradingPersistence:
    """Synchronous single-writer SQLite persistence adapter."""

    def __init__(self, config: SQLitePersistenceConfig) -> None:
        if not isinstance(config, SQLitePersistenceConfig):
            raise PersistenceConnectionError("config must be SQLitePersistenceConfig")
        self.config = config
        self._transaction_active = False
        self._closed = False
        path = str(config.database_path)
        try:
            if path != ":memory:":
                resolved_path = Path(path).expanduser().resolve()
                resolved_path.parent.mkdir(parents=True, exist_ok=True)
                path = str(resolved_path)
            self._database_path = path
            self._connection = sqlite3.connect(
                path, timeout=config.busy_timeout_ms / 1000, isolation_level=None
            )
            self._connection.row_factory = sqlite3.Row
            self._configure()
            self._migrate()
            if config.exclusive_writer:
                self._acquire_runtime_ownership()
        except (
            PersistenceConnectionError,
            PersistenceMigrationError,
            PersistenceSchemaError,
            PersistenceWriterConflictError,
        ):
            if hasattr(self, "_connection"):
                self._connection.close()
            raise
        except (OSError, sqlite3.Error) as error:
            if hasattr(self, "_connection"):
                self._connection.close()
            if config.exclusive_writer and "locked" in str(error).lower():
                raise PersistenceWriterConflictError(
                    "another application owns the paper persistence database"
                ) from error
            raise PersistenceConnectionError("failed to open SQLite persistence") from error
        except BaseException:
            if hasattr(self, "_connection"):
                self._connection.close()
            raise

    def _require_open(self) -> None:
        if self._closed:
            raise PersistenceConnectionError("persistence is closed")

    def _configure(self) -> None:
        connection = self._connection
        mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={self.config.busy_timeout_ms}")
        if mode.lower() not in {"wal", "memory"}:
            raise PersistenceConnectionError("SQLite WAL mode was not applied")
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise PersistenceConnectionError("SQLite foreign keys were not enabled")

    @staticmethod
    def _migration(name: str = MIGRATION_NAMES[0]) -> tuple[str, str]:
        sql = (
            resources.files("app.persistence.migrations").joinpath(name).read_text(encoding="utf-8")
        )
        return sql, hashlib.sha256(sql.encode("utf-8")).hexdigest()

    def _migrate(self) -> None:
        tables = self._connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        migrations = []
        for version, name in enumerate(MIGRATION_NAMES, start=1):
            if version > self.config.expected_schema_version:
                continue
            loaded = self._migration() if version == 1 else self._migration(name)
            migrations.append((version, name, *loaded))
        rows: list[sqlite3.Row] = []
        if tables is not None:
            rows = self._connection.execute(
                "SELECT version,name,checksum FROM schema_migrations ORDER BY version"
            ).fetchall()
            if rows and rows[-1][0] > self.config.expected_schema_version:
                raise PersistenceSchemaError("database schema is newer than supported code")
            expected = {version: (name, checksum) for version, name, _, checksum in migrations}
            for row in rows:
                contract = expected.get(row[0])
                if contract is not None and (row[1], row[2]) != contract:
                    raise PersistenceMigrationError("migration checksum mismatch")
        try:
            applied = {row[0] for row in rows}
            for version, name, sql, checksum in migrations:
                if version in applied:
                    continue
                escaped_name = name.replace("'", "''")
                script = (
                    "BEGIN IMMEDIATE;\n"
                    + sql
                    + "\nINSERT INTO schema_migrations(version,name,checksum,applied_at_ms) "
                    + f"VALUES({version},'{escaped_name}','{checksum}',0);\nCOMMIT;"
                )
                self._connection.executescript(script)
        except sqlite3.Error as error:
            try:
                self._connection.rollback()
            except sqlite3.Error:
                pass
            raise PersistenceMigrationError("SQLite migration failed") from error

    def _acquire_runtime_ownership(self) -> None:
        """Acquire a process-lifetime SQLite exclusive lock for application runtime."""
        try:
            mode = self._connection.execute("PRAGMA locking_mode=EXCLUSIVE").fetchone()[0]
            if str(mode).lower() != "exclusive":
                raise PersistenceWriterConflictError("exclusive writer mode unavailable")
            self._connection.execute("BEGIN IMMEDIATE")
            self._connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()
            self._connection.commit()
        except sqlite3.Error as error:
            self._connection.rollback()
            raise PersistenceWriterConflictError(
                "another application owns the paper persistence database"
            ) from error

    def begin_cycle(self) -> SQLitePaperCycleTransaction:
        self._require_open()
        if self._transaction_active:
            raise PersistenceTransactionError("nested financial transaction is forbidden")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as error:
            if "locked" in str(error).lower() or "busy" in str(error).lower():
                raise PersistenceWriterConflictError(
                    "financial writer lock is unavailable"
                ) from error
            raise PersistenceTransactionError("cannot begin financial transaction") from error
        except sqlite3.Error as error:
            raise PersistenceTransactionError("cannot begin financial transaction") from error
        self._transaction_active = True
        return SQLitePaperCycleTransaction(self)

    def initialize_portfolio(self, record: InitialPortfolioRecord) -> bool:
        self._require_open()
        if not isinstance(record, InitialPortfolioRecord):
            raise PersistenceIntegrityError("record must be InitialPortfolioRecord")
        if self._transaction_active:
            raise PersistenceTransactionError("cannot initialize during financial transaction")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            existing = self._connection.execute(
                "SELECT state_payload,state_digest,config_fingerprint,created_at_ms "
                "FROM portfolio_roots WHERE portfolio_id=?",
                (record.state.portfolio_id,),
            ).fetchone()
            expected = (
                record.state.canonical_payload,
                record.state.digest,
                record.config_fingerprint,
                record.created_at_ms,
            )
            if existing is not None:
                if tuple(existing) != expected:
                    raise PersistenceConflictError("initial Portfolio identity conflict")
                self._connection.rollback()
                return False
            self._connection.execute(
                "INSERT INTO portfolio_roots VALUES(?,?,?,?,?)",
                (record.state.portfolio_id, *expected),
            )
            self._connection.execute(
                "INSERT INTO portfolio_current VALUES(?,?,?,?,NULL)",
                (
                    record.state.portfolio_id,
                    0,
                    record.state.canonical_payload,
                    record.state.digest,
                ),
            )
            self._connection.commit()
            return True
        except PersistenceConflictError:
            self._connection.rollback()
            raise
        except sqlite3.OperationalError as error:
            self._connection.rollback()
            if "locked" in str(error).lower() or "busy" in str(error).lower():
                raise PersistenceWriterConflictError("writer lock unavailable") from error
            raise PersistenceTransactionError("Portfolio initialization failed") from error
        except sqlite3.Error as error:
            self._connection.rollback()
            raise PersistenceTransactionError("Portfolio initialization failed") from error

    def load_initial(self, portfolio_id: str) -> InitialPortfolioRecord | None:
        self._require_open()
        row = self._connection.execute(
            "SELECT state_payload,state_digest,config_fingerprint,created_at_ms "
            "FROM portfolio_roots WHERE portfolio_id=?",
            (portfolio_id,),
        ).fetchone()
        if row is None:
            return None
        state = _state_from_payload(row[0])
        if state.digest != row[1]:
            raise PersistenceIntegrityError("initial Portfolio digest mismatch")
        return InitialPortfolioRecord(state, row[2], row[3])

    def load_current(self, portfolio_id: str) -> PortfolioState | None:
        self._require_open()
        row = self._connection.execute(
            "SELECT state_payload,state_digest FROM portfolio_current WHERE portfolio_id=?",
            (portfolio_id,),
        ).fetchone()
        if row is None:
            return None
        state = _state_from_payload(row[0])
        if state.digest != row[1]:
            raise PersistenceIntegrityError("portfolio current digest mismatch")
        return state

    def load_checkpoints(self, portfolio_id: str) -> tuple[PortfolioCheckpoint, ...]:
        self._require_open()
        rows = self._connection.execute(
            "SELECT portfolio_version,state_payload,state_digest,last_journal_seq,model_version "
            "FROM portfolio_checkpoints WHERE portfolio_id=? ORDER BY portfolio_version DESC",
            (portfolio_id,),
        ).fetchall()
        result = []
        for row in rows:
            try:
                state = _state_from_payload(row[1])
                if state.digest != row[2]:
                    raise PersistenceCheckpointError("checkpoint digest mismatch")
                journal = self._connection.execute(
                    "SELECT portfolio_version_after,after_state_digest FROM journal_entries "
                    "WHERE journal_seq=? AND portfolio_id=?",
                    (row[3], portfolio_id),
                ).fetchone()
                if journal is None or tuple(journal) != (row[0], row[2]):
                    raise PersistenceCheckpointError("checkpoint journal linkage mismatch")
                result.append(
                    PortfolioCheckpoint(portfolio_id, row[0], state, row[2], row[3], row[4])
                )
            except (PersistenceIntegrityError, PersistenceCheckpointError):
                continue
        return tuple(result)

    def checkpoint_diagnostics(self, portfolio_id: str) -> tuple[str, ...]:
        self._require_open()
        rows = self._connection.execute(
            "SELECT portfolio_version,state_payload,state_digest FROM portfolio_checkpoints "
            "WHERE portfolio_id=? ORDER BY portfolio_version DESC",
            (portfolio_id,),
        ).fetchall()
        diagnostics: list[str] = []
        for row in rows:
            try:
                state = _state_from_payload(row[1])
                if state.digest != row[2]:
                    raise PersistenceCheckpointError("checkpoint digest mismatch")
                journal = self._connection.execute(
                    "SELECT portfolio_version_after,after_state_digest FROM journal_entries "
                    "WHERE journal_seq=(SELECT last_journal_seq FROM portfolio_checkpoints "
                    "WHERE portfolio_id=? AND portfolio_version=?)",
                    (portfolio_id, row[0]),
                ).fetchone()
                if journal is None or tuple(journal) != (row[0], row[2]):
                    raise PersistenceCheckpointError("checkpoint journal linkage mismatch")
            except (PersistenceIntegrityError, PersistenceCheckpointError):
                diagnostics.append(f"checkpoint_skipped:version={row[0]}")
        return tuple(diagnostics)

    def load_transitions(self, portfolio_id: str) -> tuple[PortfolioTransitionRecord, ...]:
        self._require_open()
        rows = self._connection.execute(
            "SELECT * FROM journal_entries WHERE portfolio_id=? ORDER BY journal_seq",
            (portfolio_id,),
        ).fetchall()
        result = []
        for row in rows:
            state = _state_from_payload(row["after_state_payload"])
            if state.digest != row["after_state_digest"]:
                raise PersistenceIntegrityError("journal state digest mismatch")
            result.append(
                PortfolioTransitionRecord(
                    portfolio_id=row["portfolio_id"],
                    transition_type=row["entry_type"],
                    version_before=row["portfolio_version_before"],
                    version_after=row["portfolio_version_after"],
                    before_digest=row["before_digest"],
                    after_state=state,
                    domain_timestamp_ms=row["domain_timestamp_ms"],
                    stable_decision_key=row["stable_decision_key"],
                    durable_command_key=row["durable_command_key"],
                    fill_id=row["fill_id"],
                    model_version=row["model_version"],
                )
            )
        return tuple(result)

    def integrity_check(self) -> tuple[str, ...]:
        self._require_open()
        database_results = tuple(
            row[0] for row in self._connection.execute("PRAGMA integrity_check")
        )
        foreign_key_rows = self._connection.execute("PRAGMA foreign_key_check").fetchall()
        if not foreign_key_rows:
            return database_results
        violations = tuple(
            f"foreign_key_violation:{row[0]}:{row[1]}:{row[2]}" for row in foreign_key_rows
        )
        return database_results + violations

    def reconciliation_issues(self) -> tuple[str, ...]:
        self._require_open()
        issues: list[str] = []
        for table, key_column in (
            ("paper_decisions", "stable_decision_key"),
            ("paper_commands", "durable_command_key"),
        ):
            rows = self._connection.execute(
                f"SELECT {key_column},payload,payload_digest FROM {table}"
            ).fetchall()
            for row in rows:
                expected = hashlib.sha256(row[1].encode("utf-8")).hexdigest()
                if row[2] != expected:
                    issues.append(f"payload_digest_mismatch:{table}:key={row[0][:12]}")
        if self._connection.execute(
            "SELECT 1 FROM paper_decisions WHERE terminal_outcome IS NULL LIMIT 1"
        ).fetchone():
            issues.append("visible non-terminal decision")
        if self._connection.execute(
            "SELECT 1 FROM paper_decisions d "
            "LEFT JOIN paper_commands c ON c.stable_decision_key=d.stable_decision_key "
            "LEFT JOIN fills f ON f.durable_command_key=c.durable_command_key "
            "WHERE d.terminal_outcome='EXECUTED' AND f.fill_id IS NULL LIMIT 1"
        ).fetchone():
            issues.append("executed decision has no Fill")
        if self._connection.execute(
            "SELECT 1 FROM paper_decisions d "
            "JOIN paper_commands c ON c.stable_decision_key=d.stable_decision_key "
            "JOIN fills f ON f.durable_command_key=c.durable_command_key "
            "LEFT JOIN journal_entries j ON j.fill_id=f.fill_id "
            "WHERE d.terminal_outcome='EXECUTED' AND j.journal_seq IS NULL LIMIT 1"
        ).fetchone():
            issues.append("executed Fill has no Portfolio transition")
        if self._connection.execute(
            "SELECT 1 FROM paper_decisions d "
            "JOIN paper_commands c ON c.stable_decision_key=d.stable_decision_key "
            "JOIN fills f ON f.durable_command_key=c.durable_command_key "
            "WHERE d.terminal_outcome!='EXECUTED' LIMIT 1"
        ).fetchone():
            issues.append("non-executed decision has a Fill")
        if self._connection.execute(
            "SELECT 1 FROM paper_decisions d JOIN paper_commands c "
            "ON c.stable_decision_key=d.stable_decision_key "
            "WHERE d.terminal_outcome IN ('HOLD','PLANNER_DECLINED','RISK_REJECTED') LIMIT 1"
        ).fetchone():
            issues.append("non-command terminal decision has a command")
        if self._connection.execute(
            "SELECT 1 FROM paper_decisions d LEFT JOIN paper_commands c "
            "ON c.stable_decision_key=d.stable_decision_key "
            "WHERE d.terminal_outcome='NOT_EXECUTED' AND c.durable_command_key IS NULL LIMIT 1"
        ).fetchone():
            issues.append("NOT_EXECUTED decision has no command")
        if self._connection.execute(
            "SELECT 1 FROM portfolio_current pc LEFT JOIN journal_entries j "
            "ON j.journal_seq=pc.last_journal_seq AND j.portfolio_id=pc.portfolio_id "
            "WHERE (pc.portfolio_version=0 AND pc.last_journal_seq IS NOT NULL) OR "
            "(pc.portfolio_version>0 AND (j.journal_seq IS NULL OR "
            "j.portfolio_version_after!=pc.portfolio_version OR "
            "j.after_state_digest!=pc.state_digest)) LIMIT 1"
        ).fetchone():
            issues.append("portfolio_current journal linkage mismatch")
        rows = self._connection.execute(
            "SELECT f.symbol,f.interval,j.after_state_payload FROM fills f "
            "JOIN journal_entries j ON j.fill_id=f.fill_id"
        ).fetchall()
        for row in rows:
            try:
                state = _state_from_payload(row[2])
            except PersistenceIntegrityError:
                continue
            if (row[0], row[1]) != (state.stream_key.symbol, state.stream_key.interval):
                issues.append("Fill stream differs from Portfolio transition stream")
        return tuple(issues)

    def load_fill(self, durable_command_key: str) -> PersistentFillRecord | None:
        self._require_open()
        row = self._connection.execute(
            "SELECT * FROM fills WHERE durable_command_key=?", (durable_command_key,)
        ).fetchone()
        if row is None:
            return None
        return PersistentFillRecord(
            fill_id=row["fill_id"],
            durable_command_key=row["durable_command_key"],
            order_id=row["order_id"],
            risk_decision_id=row["risk_decision_id"],
            intent_id=row["intent_id"],
            stream_key=MarketStreamKey(row["symbol"], row["interval"]),
            side=row["side"],
            quantity=deserialize_decimal(row["quantity"]),
            price=deserialize_decimal(row["price"]),
            gross_notional=deserialize_decimal(row["gross_notional"]),
            fee=deserialize_decimal(row["fee"]),
            fee_asset=row["fee_asset"],
            net_quote_change=deserialize_decimal(row["net_quote_change"]),
            net_base_change=deserialize_decimal(row["net_base_change"]),
            filled_at_ms=row["filled_at_ms"],
            dust_liquidation=bool(row["dust_liquidation"]),
            correlation_id=row["correlation_id"],
            model_version=row["model_version"],
        )

    def load_terminal_decision(self, stable_decision_key: str) -> DurableDecisionState | None:
        self._require_open()
        row = self._connection.execute(
            "SELECT decision_slot_key,terminal_outcome FROM paper_decisions "
            "WHERE stable_decision_key=?",
            (stable_decision_key,),
        ).fetchone()
        if row is None or row[1] is None:
            return None
        journal_seq = self._connection.execute(
            "SELECT COALESCE(MAX(journal_seq),0) FROM journal_entries WHERE stable_decision_key=?",
            (stable_decision_key,),
        ).fetchone()[0]
        return DurableDecisionState(
            stable_decision_key,
            row[0],
            CycleOutcome(row[1]),
            int(journal_seq),
        )

    def create_checkpoint(self, state: PortfolioState, journal_seq: int) -> bool:
        self._require_open()
        if state.version == 0 or journal_seq <= 0:
            return False
        if self._transaction_active:
            raise PersistenceTransactionError("checkpoint is forbidden during cycle transaction")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            current = self.load_current(state.portfolio_id)
            if current != state:
                raise PersistenceConflictError("checkpoint state is not portfolio_current")
            existing = self._connection.execute(
                "SELECT state_digest,last_journal_seq FROM portfolio_checkpoints "
                "WHERE portfolio_id=? AND portfolio_version=?",
                (state.portfolio_id, state.version),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != (state.digest, journal_seq):
                    raise PersistenceConflictError("checkpoint conflicts with existing version")
                self._connection.rollback()
                return False
            self._connection.execute(
                "INSERT INTO portfolio_checkpoints VALUES(?,?,?,?,?,?)",
                (
                    state.portfolio_id,
                    state.version,
                    state.canonical_payload,
                    state.digest,
                    journal_seq,
                    "portfolio-checkpoint/v1",
                ),
            )
            self._connection.commit()
            return True
        except PersistenceConflictError:
            self._connection.rollback()
            raise
        except sqlite3.Error as error:
            self._connection.rollback()
            raise PersistenceTransactionError("checkpoint creation failed") from error

    def rebuild_portfolio_current(self, state: PortfolioState, journal_seq: int) -> None:
        """Repair only a missing materialized projection from verified authority."""
        self._require_open()
        if self._transaction_active:
            raise PersistenceTransactionError("projection repair cannot run in cycle transaction")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            if (
                self._connection.execute(
                    "SELECT 1 FROM portfolio_current WHERE portfolio_id=?", (state.portfolio_id,)
                ).fetchone()
                is not None
            ):
                raise PersistenceConflictError("existing portfolio_current is never auto-repaired")
            row = self._connection.execute(
                "SELECT portfolio_version_after,after_state_digest FROM journal_entries "
                "WHERE journal_seq=? AND portfolio_id=?",
                (journal_seq, state.portfolio_id),
            ).fetchone()
            if row is None or tuple(row) != (state.version, state.digest):
                raise PersistenceConflictError("repair state is not authoritative journal head")
            self._connection.execute(
                "INSERT INTO portfolio_current VALUES(?,?,?,?,?)",
                (
                    state.portfolio_id,
                    state.version,
                    state.canonical_payload,
                    state.digest,
                    journal_seq,
                ),
            )
            self._connection.commit()
        except PersistenceConflictError:
            self._connection.rollback()
            raise
        except sqlite3.Error as error:
            self._connection.rollback()
            raise PersistenceTransactionError("portfolio_current rebuild failed") from error

    def recover(
        self,
        portfolio_id: str,
        *,
        expected_stream_key: MarketStreamKey | None = None,
        expected_config_fingerprint: str | None = None,
    ):
        from app.persistence.recovery import RecoveryCoordinator

        return RecoveryCoordinator().recover(
            self,
            portfolio_id,
            expected_stream_key=expected_stream_key,
            expected_config_fingerprint=expected_config_fingerprint,
        )

    def create_backup(self, destination: str | Path) -> None:
        self._require_open()
        if self._transaction_active:
            raise PersistenceTransactionError("backup is forbidden during financial transaction")
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with closing(sqlite3.connect(target)) as output:
                self._connection.backup(output)
        except sqlite3.Error as error:
            raise PersistenceConnectionError("SQLite online backup failed") from error

    @property
    def status(self) -> PersistenceStatus:
        self._require_open()
        last = self._connection.execute(
            "SELECT COALESCE(MAX(journal_seq),0) FROM journal_entries"
        ).fetchone()[0]
        latest = self._connection.execute(
            "SELECT MAX(portfolio_version) FROM portfolio_current"
        ).fetchone()[0]
        checkpoint = self._connection.execute(
            "SELECT MAX(portfolio_version) FROM portfolio_checkpoints"
        ).fetchone()[0]
        return PersistenceStatus(
            opened=not self._closed,
            schema_version=self.config.expected_schema_version,
            journal_mode=self._connection.execute("PRAGMA journal_mode").fetchone()[0],
            synchronous_mode=self._connection.execute("PRAGMA synchronous").fetchone()[0],
            foreign_keys=bool(self._connection.execute("PRAGMA foreign_keys").fetchone()[0]),
            last_journal_seq=last,
            latest_portfolio_version=latest,
            latest_checkpoint_version=checkpoint,
            database_path=self._database_path,
        )

    def close(self) -> None:
        if self._closed:
            return
        if self._transaction_active:
            self._connection.rollback()
            self._transaction_active = False
        self._connection.close()
        self._closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
