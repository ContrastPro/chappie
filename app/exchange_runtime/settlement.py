import json
import sqlite3
from collections.abc import Callable
from decimal import localcontext

from app.exchange_execution.models import require_hash, require_text
from app.exchange_runtime.exceptions import ExchangeSettlementError
from app.exchange_runtime.models import (
    ExchangeSettlementResult,
    ExchangeSettlementStatus,
)
from app.execution.models import Fill, FillSide
from app.market.models import MarketStreamKey
from app.persistence.canonical import (
    canonical_json,
    canonical_sha256,
    deserialize_decimal,
    serialize_decimal,
)
from app.persistence.exceptions import PersistenceIntegrityError
from app.persistence.exchange import SQLiteExchangePersistence
from app.persistence.exchange_models import ExchangeReconciliationStatus
from app.persistence.runtime import snapshot_to_state, state_to_snapshot
from app.persistence.sqlite import deserialize_portfolio_state
from app.portfolio import Portfolio


class SQLiteExchangeSettlement:
    """Atomic exchange-trade to managed-Portfolio settlement; no exchange I/O."""

    def __init__(
        self,
        persistence: SQLiteExchangePersistence,
        portfolio: Portfolio,
        *,
        execution_root_id: str,
        session_id: str,
        config_fingerprint: str,
        timestamp_provider: Callable[[], int],
        failure_injector: Callable[[str], None] | None = None,
    ) -> None:
        if not isinstance(persistence, SQLiteExchangePersistence):
            raise TypeError("persistence must be SQLiteExchangePersistence")
        if persistence._trading.config.expected_schema_version < 4:
            raise ExchangeSettlementError("settlement requires schema version 4")
        if not isinstance(portfolio, Portfolio):
            raise TypeError("portfolio must be Portfolio")
        if not callable(timestamp_provider):
            raise TypeError("timestamp_provider must be callable")
        require_text(execution_root_id, "execution_root_id")
        require_text(session_id, "session_id")
        require_hash(config_fingerprint, "config_fingerprint")
        bootstrap = persistence.load_bootstrap(execution_root_id)
        if bootstrap.session.session_id != session_id:
            raise ExchangeSettlementError("settlement session lineage conflicts")
        self._persistence = persistence
        self._connection = persistence._connection
        self._portfolio = portfolio
        self._root_id = execution_root_id
        self._session_id = session_id
        self._fingerprint = config_fingerprint
        self._timestamp = timestamp_provider
        self._failure = failure_injector
        self._degraded = False

    def initialize(self) -> None:
        state = snapshot_to_state(self._portfolio.snapshot)
        payload = state.canonical_payload
        connection = self._connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state_digest,config_fingerprint FROM exchange_portfolio_roots "
                "WHERE portfolio_id=?",
                (state.portfolio_id,),
            ).fetchone()
            if row is not None:
                if tuple(row) != (state.digest, self._fingerprint):
                    raise ExchangeSettlementError("exchange Portfolio root conflicts")
                connection.rollback()
                self.restore_memory()
                return
            connection.execute(
                "INSERT INTO exchange_portfolio_roots VALUES(?,?,?,?,?,?,?)",
                (
                    state.portfolio_id,
                    self._root_id,
                    payload,
                    state.digest,
                    self._fingerprint,
                    self._now(),
                    "exchange-portfolio-root/v1",
                ),
            )
            connection.execute(
                "INSERT INTO exchange_portfolio_current VALUES(?,?,?,?,NULL)",
                (state.portfolio_id, state.version, payload, state.digest),
            )
            connection.commit()
        except sqlite3.Error as error:
            connection.rollback()
            raise ExchangeSettlementError("exchange Portfolio initialization failed") from error
        except BaseException:
            connection.rollback()
            raise

    def restore_memory(self) -> None:
        if self._degraded:
            raise ExchangeSettlementError(
                "settlement runtime is degraded after committed memory synchronization failure"
            )
        self.validate_authority()
        row = self._current_row()
        state = self._deserialize_state(row["state_payload"])
        if state.digest != row["state_digest"] or state.version != row["portfolio_version"]:
            raise ExchangeSettlementError("exchange Portfolio current digest mismatch")
        preview_state = state_to_snapshot(state)
        if self._portfolio.snapshot != preview_state:
            self._portfolio.synchronize_recovered_state(preview_state)

    def validate_authority(self) -> None:
        """Prove the complete trade/settlement/Portfolio/session recovery chain."""
        if self._degraded:
            raise ExchangeSettlementError(
                "settlement runtime is degraded after committed memory synchronization failure"
            )
        root = self._connection.execute(
            "SELECT * FROM exchange_portfolio_roots WHERE execution_root_id=?",
            (self._root_id,),
        ).fetchone()
        if root is None:
            raise ExchangeSettlementError("exchange Portfolio root is missing")
        root_state = self._deserialize_state(root["state_payload"])
        if (
            root_state.digest != root["state_digest"]
            or root["config_fingerprint"] != self._fingerprint
        ):
            raise ExchangeSettlementError("exchange Portfolio root authority mismatch")
        rows = self._connection.execute(
            "SELECT s.*,t.quote_quantity,t.payload_digest AS trade_digest,o.execution_root_id "
            "FROM exchange_trade_settlements s JOIN exchange_trades t ON "
            "t.venue=s.venue AND t.environment=s.environment AND "
            "t.account_fingerprint=s.account_fingerprint AND t.symbol=s.symbol AND "
            "t.exchange_trade_id=s.exchange_trade_id JOIN exchange_orders o ON "
            "o.durable_command_key=s.durable_command_key WHERE o.execution_root_id=? "
            "ORDER BY s.settlement_seq",
            (self._root_id,),
        ).fetchall()
        economic_rows = []
        for row in rows:
            record = json.loads(row["record_payload"])
            if (
                canonical_sha256(record, algorithm_version=row["model_version"])
                != row["record_digest"]
            ):
                raise ExchangeSettlementError("settlement record digest mismatch")
            if (
                record["settlement_id"] != row["settlement_id"]
                or record["exchange_trade_id"] != row["exchange_trade_id"]
                or record["durable_command_key"] != row["durable_command_key"]
                or record["reconciliation_id"] != row["reconciliation_id"]
                or record["reconciliation_generation"] != row["reconciliation_generation"]
                or record["status"] != row["status"]
            ):
                raise ExchangeSettlementError("settlement record linkage mismatch")
            if not self._coverage_row_is_complete(
                row["reconciliation_id"], row["reconciliation_generation"]
            ):
                raise ExchangeSettlementError("settlement reconciliation coverage is invalid")
            if row["status"] == "SETTLED":
                economic_rows.append(row)
        if self._persistence._trading.config.expected_schema_version >= 6:
            resolutions = self._connection.execute(
                "SELECT r.*,t.quote_quantity,t.commission,t.commission_asset,"
                "t.payload_digest AS trade_digest,o.execution_root_id,b.status AS blocked_status,"
                "b.exchange_trade_id AS blocked_trade_id,b.durable_command_key AS blocked_command "
                "FROM exchange_trade_settlement_resolutions r "
                "JOIN exchange_trade_settlements b ON b.settlement_id=r.blocked_settlement_id "
                "JOIN exchange_trades t ON t.venue=r.venue AND t.environment=r.environment AND "
                "t.account_fingerprint=r.account_fingerprint AND t.symbol=r.symbol AND "
                "t.exchange_trade_id=r.exchange_trade_id JOIN exchange_orders o ON "
                "o.durable_command_key=r.durable_command_key WHERE o.execution_root_id=?",
                (self._root_id,),
            ).fetchall()
            for row in resolutions:
                record = json.loads(row["record_payload"])
                expected_record = {
                    "resolution_id": row["resolution_id"],
                    "fill_id": row["fill_id"],
                    "blocked_settlement_id": row["blocked_settlement_id"],
                    "portfolio_id": row["portfolio_id"],
                    "session_id": row["session_id"],
                    "exchange_trade_id": row["exchange_trade_id"],
                    "durable_command_key": row["durable_command_key"],
                    "reconciliation_id": row["reconciliation_id"],
                    "reconciliation_generation": row["reconciliation_generation"],
                    "policy_id": row["policy_id"],
                    "reason_code": row["reason_code"],
                    "version_before": row["portfolio_version_before"],
                    "version_after": row["portfolio_version_after"],
                    "created_at_ms": row["created_at_ms"],
                    "model_version": row["model_version"],
                }
                if (
                    canonical_json(record) != row["record_payload"]
                    or canonical_sha256(record, algorithm_version=row["model_version"])
                    != row["record_digest"]
                    or record != expected_record
                    or row["blocked_status"] != "BLOCKED_UNSUPPORTED_FEE"
                    or row["blocked_trade_id"] != row["exchange_trade_id"]
                    or row["blocked_command"] != row["durable_command_key"]
                    or row["portfolio_id"] != root["portfolio_id"]
                    or row["session_id"] != self._session_id
                    or row["execution_root_id"] != self._root_id
                    or row["policy_id"] != "zero-commission-asset-neutral/v1"
                    or row["reason_code"] != "exact_zero_commission_asset_neutral"
                    or not deserialize_decimal(row["commission"]).is_zero()
                ):
                    raise ExchangeSettlementError("settlement resolution authority mismatch")
                if not self._coverage_row_is_complete(
                    row["reconciliation_id"], row["reconciliation_generation"]
                ):
                    raise ExchangeSettlementError("settlement resolution coverage is invalid")
                economic_rows.append(row)
        version = root_state.version
        latest_payload = root["state_payload"]
        latest_digest = root["state_digest"]
        turnover = deserialize_decimal("0")
        for row in sorted(economic_rows, key=lambda value: value["portfolio_version_after"]):
            if row["portfolio_version_before"] != version or row["before_digest"] != latest_digest:
                raise ExchangeSettlementError("settlement Portfolio version chain is broken")
            state = self._deserialize_state(row["after_state_payload"])
            if (
                state.digest != row["after_state_digest"]
                or state.version != row["portfolio_version_after"]
            ):
                raise ExchangeSettlementError("settlement Portfolio state mismatch")
            version = state.version
            latest_payload = row["after_state_payload"]
            latest_digest = row["after_state_digest"]
            turnover += deserialize_decimal(row["quote_quantity"])
        current = self._connection.execute(
            "SELECT * FROM exchange_portfolio_current WHERE portfolio_id=?",
            (root["portfolio_id"],),
        ).fetchone()
        if current is None or (
            current["portfolio_version"] != version
            or current["state_payload"] != latest_payload
            or current["state_digest"] != latest_digest
        ):
            raise ExchangeSettlementError("settlement/current Portfolio authority mismatch")
        session = self._persistence.load_session(self._session_id)
        if session.cumulative_executed_turnover != turnover:
            raise ExchangeSettlementError("settlement/session turnover authority mismatch")

    def settle_pending(self) -> ExchangeSettlementResult:
        self.restore_memory()
        reconciliation = self._persistence.read_reconciliation_state(self._root_id)
        if (
            reconciliation.status is not ExchangeReconciliationStatus.SAFE
            or reconciliation.latest_reconciliation_id is None
            or not self._coverage_is_complete(reconciliation)
        ):
            return ExchangeSettlementResult(
                ExchangeSettlementStatus.PENDING,
                0,
                0,
                self._portfolio.snapshot.version,
            )
        rows = self._connection.execute(
            "SELECT t.*,o.session_id,o.stable_decision_key FROM exchange_trades t "
            "JOIN exchange_orders o ON o.durable_command_key=t.durable_command_key "
            "LEFT JOIN exchange_trade_settlements s ON s.venue=t.venue AND "
            "s.environment=t.environment AND s.account_fingerprint=t.account_fingerprint "
            "AND s.symbol=t.symbol AND s.exchange_trade_id=t.exchange_trade_id "
            "WHERE o.execution_root_id=? AND s.settlement_id IS NULL "
            "ORDER BY t.traded_at_ms,t.exchange_trade_id",
            (self._root_id,),
        ).fetchall()
        settled = blocked = 0
        last_id = None
        for row in rows:
            settlement_id = canonical_sha256(
                {
                    "venue": row["venue"],
                    "environment": row["environment"],
                    "account": row["account_fingerprint"],
                    "symbol": row["symbol"],
                    "trade": row["exchange_trade_id"],
                },
                algorithm_version="exchange-settlement-id/v1",
            )
            if row["commission_asset"] != self._portfolio.snapshot.quote_asset:
                self._record_blocked(row, settlement_id, reconciliation)
                blocked += 1
            else:
                fill = self._fill(row, settlement_id)
                preview = self._portfolio.preview_fill(fill, fill.price)
                self._commit_settlement(row, settlement_id, fill, preview, reconciliation)
                try:
                    self._portfolio.apply_committed_transition(preview)
                except BaseException:
                    self._degraded = True
                    raise
                settled += 1
            last_id = settlement_id
        resolved = self._resolve_zero_commission_blocks(reconciliation)
        total_blocked = self._unresolved_blocked_count()
        pending = self._connection.execute(
            "SELECT count(*) FROM exchange_trades t JOIN exchange_orders o ON "
            "o.durable_command_key=t.durable_command_key LEFT JOIN exchange_trade_settlements s "
            "ON s.venue=t.venue AND s.environment=t.environment AND "
            "s.account_fingerprint=t.account_fingerprint AND s.symbol=t.symbol AND "
            "s.exchange_trade_id=t.exchange_trade_id WHERE o.execution_root_id=? "
            "AND s.settlement_id IS NULL",
            (self._root_id,),
        ).fetchone()[0]
        status = (
            ExchangeSettlementStatus.BLOCKED_UNSUPPORTED_FEE
            if total_blocked
            else ExchangeSettlementStatus.PENDING
            if pending
            else ExchangeSettlementStatus.SYNCHRONIZED
        )
        self.validate_authority()
        return ExchangeSettlementResult(
            status,
            settled,
            blocked,
            self._portfolio.snapshot.version,
            last_id,
            resolved,
        )

    def _unresolved_blocked_count(self) -> int:
        if self._persistence._trading.config.expected_schema_version < 6:
            return self._connection.execute(
                "SELECT count(*) FROM exchange_trade_settlements WHERE portfolio_id=? "
                "AND status='BLOCKED_UNSUPPORTED_FEE'",
                (self._portfolio.snapshot.portfolio_id,),
            ).fetchone()[0]
        return self._connection.execute(
            "SELECT count(*) FROM exchange_trade_settlements b "
            "LEFT JOIN exchange_trade_settlement_resolutions r "
            "ON r.blocked_settlement_id=b.settlement_id WHERE b.portfolio_id=? "
            "AND b.status='BLOCKED_UNSUPPORTED_FEE' AND r.resolution_id IS NULL",
            (self._portfolio.snapshot.portfolio_id,),
        ).fetchone()[0]

    def _resolve_zero_commission_blocks(self, reconciliation) -> int:
        if self._persistence._trading.config.expected_schema_version < 6:
            return 0
        rows = self._connection.execute(
            "SELECT b.settlement_seq,b.settlement_id,b.portfolio_id,b.session_id,b.venue,"
            "b.environment,b.account_fingerprint,b.symbol,b.exchange_trade_id,t.exchange_order_id,"
            "b.durable_command_key,t.client_order_id,t.interval,t.side,t.quantity,t.price,"
            "t.quote_quantity,t.commission,t.commission_asset,t.traded_at_ms,"
            "o.stable_decision_key FROM exchange_trade_settlements b "
            "JOIN exchange_trades t ON t.venue=b.venue AND t.environment=b.environment AND "
            "t.account_fingerprint=b.account_fingerprint AND t.symbol=b.symbol AND "
            "t.exchange_trade_id=b.exchange_trade_id JOIN exchange_orders o ON "
            "o.durable_command_key=b.durable_command_key "
            "LEFT JOIN exchange_trade_settlement_resolutions r "
            "ON r.blocked_settlement_id=b.settlement_id WHERE b.portfolio_id=? "
            "AND b.status='BLOCKED_UNSUPPORTED_FEE' AND r.resolution_id IS NULL "
            "ORDER BY b.settlement_seq",
            (self._portfolio.snapshot.portfolio_id,),
        ).fetchall()
        resolved = 0
        for row in rows:
            commission = deserialize_decimal(row["commission"])
            if not commission.is_zero():
                continue
            resolution_id = canonical_sha256(
                {
                    "blocked_settlement_id": row["settlement_id"],
                    "policy": "zero-commission-asset-neutral/v1",
                },
                algorithm_version="exchange-settlement-resolution-id/v1",
            )
            fill_id = canonical_sha256(
                {"resolution_id": resolution_id, "exchange_trade_id": row["exchange_trade_id"]},
                algorithm_version="exchange-resolution-fill-id/v1",
            )
            fill = self._fill(row, fill_id, fee_asset=self._portfolio.snapshot.quote_asset)
            preview = self._portfolio.preview_fill(fill, fill.price)
            committed = self._commit_zero_resolution(
                row, resolution_id, fill_id, preview, reconciliation
            )
            if not committed:
                continue
            try:
                self._portfolio.apply_committed_transition(preview)
            except BaseException:
                self._degraded = True
                raise
            resolved += 1
        return resolved

    def _commit_zero_resolution(self, row, resolution_id, fill_id, preview, reconciliation) -> bool:
        before = snapshot_to_state(preview.before)
        after = snapshot_to_state(preview.after)
        created_at_ms = self._now()
        record = {
            "resolution_id": resolution_id,
            "fill_id": fill_id,
            "blocked_settlement_id": row["settlement_id"],
            "portfolio_id": before.portfolio_id,
            "session_id": row["session_id"],
            "exchange_trade_id": row["exchange_trade_id"],
            "durable_command_key": row["durable_command_key"],
            "reconciliation_id": reconciliation.latest_reconciliation_id,
            "reconciliation_generation": reconciliation.generation,
            "policy_id": "zero-commission-asset-neutral/v1",
            "reason_code": "exact_zero_commission_asset_neutral",
            "version_before": before.version,
            "version_after": after.version,
            "created_at_ms": created_at_ms,
            "model_version": "exchange-settlement-resolution/v1",
        }
        payload = canonical_json(record)
        digest = canonical_sha256(record, algorithm_version=record["model_version"])
        connection = self._connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._hit("resolution_before_record")
            blocked = connection.execute(
                "SELECT status,portfolio_id,session_id,exchange_trade_id,durable_command_key "
                "FROM exchange_trade_settlements WHERE settlement_id=?",
                (row["settlement_id"],),
            ).fetchone()
            if blocked is None or tuple(blocked) != (
                "BLOCKED_UNSUPPORTED_FEE",
                before.portfolio_id,
                self._session_id,
                row["exchange_trade_id"],
                row["durable_command_key"],
            ):
                raise ExchangeSettlementError("blocked settlement resolution lineage conflicts")
            current_reconciliation = self._persistence.read_reconciliation_state(self._root_id)
            if (
                current_reconciliation.status is not ExchangeReconciliationStatus.SAFE
                or current_reconciliation.latest_reconciliation_id
                != reconciliation.latest_reconciliation_id
                or current_reconciliation.generation != reconciliation.generation
                or not self._coverage_is_complete(current_reconciliation)
            ):
                raise ExchangeSettlementError("resolution reconciliation authority changed")
            cursor = connection.execute(
                "INSERT OR IGNORE INTO exchange_trade_settlement_resolutions "
                "VALUES(NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    resolution_id,
                    fill_id,
                    row["settlement_id"],
                    before.portfolio_id,
                    row["session_id"],
                    row["venue"],
                    row["environment"],
                    row["account_fingerprint"],
                    row["symbol"],
                    row["exchange_trade_id"],
                    row["durable_command_key"],
                    reconciliation.latest_reconciliation_id,
                    reconciliation.generation,
                    "zero-commission-asset-neutral/v1",
                    "exact_zero_commission_asset_neutral",
                    before.version,
                    after.version,
                    before.digest,
                    after.canonical_payload,
                    after.digest,
                    created_at_ms,
                    "exchange-settlement-resolution/v1",
                    payload,
                    digest,
                ),
            )
            if cursor.rowcount == 0:
                connection.rollback()
                self.restore_memory()
                return False
            self._hit("resolution_after_record")
            self._hit("resolution_before_current")
            updated = connection.execute(
                "UPDATE exchange_portfolio_current SET portfolio_version=?,state_payload=?,"
                "state_digest=?,last_settlement_seq=? WHERE portfolio_id=? AND "
                "portfolio_version=? AND state_digest=?",
                (
                    after.version,
                    after.canonical_payload,
                    after.digest,
                    row["settlement_seq"],
                    before.portfolio_id,
                    before.version,
                    before.digest,
                ),
            )
            if updated.rowcount != 1:
                raise ExchangeSettlementError("resolution Portfolio version conflict")
            self._hit("resolution_after_current")
            self._hit("resolution_before_turnover")
            session = self._persistence.load_session(row["session_id"])
            turnover = session.cumulative_executed_turnover + deserialize_decimal(
                row["quote_quantity"]
            )
            updated = connection.execute(
                "UPDATE exchange_sessions SET executed_turnover=?,generation=generation+1 "
                "WHERE session_id=? AND generation=?",
                (
                    serialize_decimal(turnover),
                    row["session_id"],
                    self._session_generation(row["session_id"]),
                ),
            )
            if updated.rowcount != 1:
                raise ExchangeSettlementError("resolution session generation conflict")
            self._hit("resolution_after_turnover")
            self._hit("resolution_before_commit")
            connection.commit()
            self._hit("resolution_after_commit")
            return True
        except BaseException:
            connection.rollback()
            raise

    def _coverage_is_complete(self, reconciliation) -> bool:
        return self._coverage_row_is_complete(
            reconciliation.latest_reconciliation_id, reconciliation.generation
        )

    def _coverage_row_is_complete(self, reconciliation_id, generation) -> bool:
        row = self._connection.execute(
            "SELECT c.*,r.started_at_ms FROM exchange_reconciliation_coverage c "
            "JOIN exchange_reconciliation_runs r ON r.reconciliation_id=c.reconciliation_id "
            "WHERE c.reconciliation_id=?",
            (reconciliation_id,),
        ).fetchone()
        if row is None:
            return False
        root = self._persistence.load_bootstrap(self._root_id).root
        evidence = {
            "reconciliation_id": row["reconciliation_id"],
            "execution_root_id": row["execution_root_id"],
            "session_id": row["session_id"],
            "account_fingerprint": row["account_fingerprint"],
            "generation": row["generation"],
            "coverage_start_ms": row["coverage_start_ms"],
            "coverage_end_ms": row["coverage_end_ms"],
            "lower_bound_complete": bool(row["lower_bound_complete"]),
            "upper_bound_complete": bool(row["upper_bound_complete"]),
            "trade_scope": row["trade_scope"],
            "algorithm_version": row["algorithm_version"],
        }
        return (
            row["execution_root_id"] == self._root_id
            and row["session_id"] == self._session_id
            and row["account_fingerprint"] == root.account_fingerprint
            and row["generation"] == generation
            and row["coverage_start_ms"] <= root.created_at_ms
            and row["coverage_end_ms"] >= row["started_at_ms"]
            and bool(row["lower_bound_complete"])
            and bool(row["upper_bound_complete"])
            and row["trade_scope"] == root.stream_key.symbol
            and row["algorithm_version"] == "exchange-trade-coverage/v1"
            and row["evidence_payload"] == canonical_json(evidence)
            and row["evidence_digest"]
            == canonical_sha256(evidence, algorithm_version=row["algorithm_version"])
        )

    def _fill(self, row: sqlite3.Row, settlement_id: str, *, fee_asset: str | None = None) -> Fill:
        quantity = deserialize_decimal(row["quantity"])
        quote = deserialize_decimal(row["quote_quantity"])
        fee = deserialize_decimal(row["commission"])
        with localcontext() as context:
            context.prec = max(
                80, len(quantity.as_tuple().digits) + len(quote.as_tuple().digits) + 32
            )
            price = quote / quantity
        side = FillSide(row["side"])
        return Fill(
            fill_id=settlement_id,
            order_id=row["exchange_order_id"],
            risk_decision_id=row["stable_decision_key"],
            intent_id=row["durable_command_key"],
            stream_key=MarketStreamKey(row["symbol"], row["interval"]),
            side=side,
            quantity=quantity,
            price=price,
            gross_notional=quote,
            fee=fee,
            fee_asset=row["commission_asset"] if fee_asset is None else fee_asset,
            net_quote_change=-(quote + fee) if side is FillSide.BUY else quote - fee,
            net_base_change=quantity if side is FillSide.BUY else -quantity,
            filled_at_ms=row["traded_at_ms"],
            correlation_id=row["client_order_id"],
        )

    def _record_blocked(self, row, settlement_id, reconciliation) -> None:
        current = snapshot_to_state(self._portfolio.snapshot)
        self._insert_record(row, settlement_id, None, current, None, reconciliation)

    def _commit_settlement(self, row, settlement_id, fill, preview, reconciliation) -> None:
        before = snapshot_to_state(preview.before)
        after = snapshot_to_state(preview.after)
        self._insert_record(row, settlement_id, fill, before, after, reconciliation)

    def _insert_record(self, row, settlement_id, fill, before, after, reconciliation) -> None:
        status = "SETTLED" if fill is not None else "BLOCKED_UNSUPPORTED_FEE"
        record = {
            "settlement_id": settlement_id,
            "portfolio_id": before.portfolio_id,
            "session_id": row["session_id"],
            "exchange_trade_id": row["exchange_trade_id"],
            "durable_command_key": row["durable_command_key"],
            "reconciliation_id": reconciliation.latest_reconciliation_id,
            "reconciliation_generation": reconciliation.generation,
            "fill_id": None if fill is None else fill.fill_id,
            "status": status,
            "version_before": before.version,
            "version_after": before.version if after is None else after.version,
            "settled_at_ms": self._now(),
            "model_version": "exchange-settlement/v1",
        }
        payload = canonical_json(record)
        digest = canonical_sha256(record, algorithm_version="exchange-settlement/v1")
        connection = self._connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "INSERT OR IGNORE INTO exchange_trade_settlements "
                "VALUES(NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    settlement_id,
                    before.portfolio_id,
                    row["session_id"],
                    row["venue"],
                    row["environment"],
                    row["account_fingerprint"],
                    row["symbol"],
                    row["exchange_trade_id"],
                    row["durable_command_key"],
                    reconciliation.latest_reconciliation_id,
                    reconciliation.generation,
                    None if fill is None else fill.fill_id,
                    status,
                    before.version,
                    before.version if after is None else after.version,
                    before.digest,
                    None if after is None else after.canonical_payload,
                    None if after is None else after.digest,
                    record["settled_at_ms"],
                    "exchange-settlement/v1",
                    payload,
                    digest,
                ),
            )
            if cursor.rowcount == 0:
                connection.rollback()
                return
            self._hit("settlement_after_record")
            if after is not None:
                seq = cursor.lastrowid
                updated = connection.execute(
                    "UPDATE exchange_portfolio_current SET portfolio_version=?,state_payload=?,"
                    "state_digest=?,last_settlement_seq=? WHERE portfolio_id=? AND portfolio_version=?",
                    (
                        after.version,
                        after.canonical_payload,
                        after.digest,
                        seq,
                        after.portfolio_id,
                        before.version,
                    ),
                )
                if updated.rowcount != 1:
                    raise ExchangeSettlementError("exchange Portfolio version conflict")
                self._hit("settlement_after_current")
                session = self._persistence.load_session(row["session_id"])
                turnover = session.cumulative_executed_turnover + deserialize_decimal(
                    row["quote_quantity"]
                )
                updated_session = connection.execute(
                    "UPDATE exchange_sessions SET executed_turnover=?,generation=generation+1 "
                    "WHERE session_id=? AND generation=?",
                    (
                        serialize_decimal(turnover),
                        row["session_id"],
                        self._session_generation(row["session_id"]),
                    ),
                )
                if updated_session.rowcount != 1:
                    raise ExchangeSettlementError("exchange session generation conflict")
                self._hit("settlement_after_turnover")
            self._hit("settlement_before_commit")
            connection.commit()
            self._hit("settlement_after_commit")
        except BaseException:
            connection.rollback()
            raise

    def _session_generation(self, session_id: str) -> int:
        return self._connection.execute(
            "SELECT generation FROM exchange_sessions WHERE session_id=?", (session_id,)
        ).fetchone()[0]

    def _current_row(self) -> sqlite3.Row:
        row = self._connection.execute(
            "SELECT * FROM exchange_portfolio_current WHERE portfolio_id=?",
            (self._portfolio.snapshot.portfolio_id,),
        ).fetchone()
        if row is None:
            raise ExchangeSettlementError("exchange Portfolio current is missing")
        return row

    @staticmethod
    def _deserialize_state(payload: str):
        try:
            return deserialize_portfolio_state(payload)
        except PersistenceIntegrityError as error:
            raise ExchangeSettlementError("persisted Portfolio state is invalid") from error

    def _now(self) -> int:
        value = self._timestamp()
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ExchangeSettlementError("timestamp provider returned invalid value")
        return value

    def _hit(self, boundary: str) -> None:
        if self._failure is not None:
            self._failure(boundary)
