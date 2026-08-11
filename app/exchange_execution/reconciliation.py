from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

from app.exchange_execution.models import (
    ExchangeOrder,
    ExchangeOrderEventSource,
    ExchangeOrderStatus,
    QueryResultKind,
)
from app.exchange_execution.ports import ExchangeAccountReader, ExchangeInstrumentRules
from app.persistence.exceptions import PersistenceConflictError, PersistenceIntegrityError
from app.persistence.exchange_models import (
    ExchangeReconciliationStatus,
    PersistentExchangeReconciliationRun,
    TradeCompletenessStatus,
)
from app.persistence.ports import ExchangeLifecyclePersistence


class ExchangeReconciliationReason(StrEnum):
    ORDER_NOT_FOUND = "ORDER_NOT_FOUND"
    ORDER_QUERY_FAILED = "ORDER_QUERY_FAILED"
    ORDER_FACT_CONFLICT = "ORDER_FACT_CONFLICT"
    ORDER_STATUS_AMBIGUOUS = "ORDER_STATUS_AMBIGUOUS"
    TRADE_QUERY_FAILED = "TRADE_QUERY_FAILED"
    TRADE_FACT_CONFLICT = "TRADE_FACT_CONFLICT"
    TRADE_HISTORY_FAILED = "TRADE_HISTORY_FAILED"
    TRADE_HISTORY_INCOMPLETE = "TRADE_HISTORY_INCOMPLETE"
    TRADE_INCOMPLETE = "TRADE_INCOMPLETE"
    UNKNOWN_OPEN_ORDER = "UNKNOWN_OPEN_ORDER"
    DUPLICATE_OPEN_ORDER = "DUPLICATE_OPEN_ORDER"
    TERMINAL_ORDER_SHOWN_OPEN = "TERMINAL_ORDER_SHOWN_OPEN"
    EXPECTED_OPEN_ORDER_MISSING = "EXPECTED_OPEN_ORDER_MISSING"
    UNKNOWN_TRADE = "UNKNOWN_TRADE"
    BALANCE_QUERY_FAILED = "BALANCE_QUERY_FAILED"
    BALANCE_SCOPE_INVALID = "BALANCE_SCOPE_INVALID"
    INSTRUMENT_RULES_QUERY_FAILED = "INSTRUMENT_RULES_QUERY_FAILED"
    INSTRUMENT_RULES_MISMATCH = "INSTRUMENT_RULES_MISMATCH"


@dataclass(frozen=True, slots=True)
class ExchangeReconciliationResult:
    reconciliation_id: str
    execution_root_id: str
    session_id: str
    account_fingerprint: str
    generation_before: int
    generation_after: int
    outcome: ExchangeReconciliationStatus
    orders_examined: int
    orders_advanced: int
    trades_persisted: int
    open_orders_examined: int
    balances_examined: int
    reasons: tuple[ExchangeReconciliationReason, ...]
    started_at_ms: int
    completed_at_ms: int

    def __post_init__(self) -> None:
        for value, name in (
            (self.reconciliation_id, "reconciliation_id"),
            (self.execution_root_id, "execution_root_id"),
            (self.session_id, "session_id"),
            (self.account_fingerprint, "account_fingerprint"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty")
        for name in (
            "generation_before",
            "generation_after",
            "orders_examined",
            "orders_advanced",
            "trades_persisted",
            "open_orders_examined",
            "balances_examined",
            "started_at_ms",
            "completed_at_ms",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be non-negative")
        if not isinstance(self.outcome, ExchangeReconciliationStatus):
            raise TypeError("outcome must be ExchangeReconciliationStatus")
        expected = self.generation_before + (self.outcome is ExchangeReconciliationStatus.SAFE)
        if self.generation_after != expected:
            raise ValueError("result generation transition is invalid")
        if self.completed_at_ms < self.started_at_ms:
            raise ValueError("result completion precedes start")
        if not isinstance(self.reasons, tuple) or any(
            not isinstance(value, ExchangeReconciliationReason) for value in self.reasons
        ):
            raise ValueError("reasons must contain ExchangeReconciliationReason values")
        if tuple(sorted(set(self.reasons), key=lambda value: value.value)) != self.reasons:
            raise ValueError("reasons must be sorted and unique")
        if (self.outcome is ExchangeReconciliationStatus.SAFE) != (not self.reasons):
            raise ValueError("SAFE requires no reasons and unsafe outcomes require reasons")
        if self.orders_advanced > self.orders_examined:
            raise ValueError("orders_advanced cannot exceed orders_examined")

    @property
    def submission_ready(self) -> bool:
        """M4.7 never proves settlement or mints process-local submission authority."""
        return False


class ExchangeReconciler:
    """Read-only exchange reconciliation with durable, atomic SAFE generations."""

    _OPEN: ClassVar[frozenset[ExchangeOrderStatus]] = frozenset(
        {ExchangeOrderStatus.ACKNOWLEDGED, ExchangeOrderStatus.PARTIALLY_FILLED}
    )

    def __init__(
        self,
        persistence: ExchangeLifecyclePersistence,
        reader: ExchangeAccountReader,
        *,
        expected_rules: ExchangeInstrumentRules,
        timestamp_provider: Callable[[], int],
        reconciliation_id_factory: Callable[[], str],
        balance_assets: tuple[str, ...] = ("BTC", "USDT"),
    ) -> None:
        if not isinstance(persistence, ExchangeLifecyclePersistence):
            raise TypeError("persistence must implement ExchangeLifecyclePersistence")
        if not isinstance(reader, ExchangeAccountReader):
            raise TypeError("reader must implement ExchangeAccountReader")
        if not isinstance(expected_rules, ExchangeInstrumentRules):
            raise TypeError("expected_rules must be ExchangeInstrumentRules")
        if not callable(timestamp_provider) or not callable(reconciliation_id_factory):
            raise TypeError("reconciliation factories must be callable")
        if not balance_assets or len(set(balance_assets)) != len(balance_assets):
            raise TypeError("balance_assets must be unique and non-empty")
        self._persistence = persistence
        self._reader = reader
        self._expected_rules = expected_rules
        self._timestamp_provider = timestamp_provider
        self._id_factory = reconciliation_id_factory
        self._balance_assets = balance_assets

    def reconcile(self, execution_root_id: str) -> ExchangeReconciliationResult:
        started = self._timestamp()
        reconciliation_id = self._id()
        bootstrap = self._persistence.load_bootstrap(execution_root_id)
        state = self._persistence.read_reconciliation_state(execution_root_id)
        all_orders = tuple(
            value
            for value in self._persistence.list_orders()
            if value.execution_root_id == execution_root_id
        )
        known_by_client = {value.order.client_order_id: value for value in all_orders}
        known_exchange_ids = {
            value.order.exchange_order_id
            for value in all_orders
            if value.order.exchange_order_id is not None
        }
        candidates = tuple(
            value
            for value in all_orders
            if value.order.status is not ExchangeOrderStatus.PREPARED
            and (
                not value.order.status.is_exchange_terminal
                or self._persistence.trade_completeness(value.order.durable_command_key)
                is not TradeCompletenessStatus.COMPLETE
            )
        )
        reasons: set[ExchangeReconciliationReason] = set()
        advanced = 0
        persisted_trades = 0
        observed_by_client: dict[str, ExchangeOrder] = {}
        failed = False

        for durable in candidates:
            local = durable.order
            try:
                query = self._reader.query_order(
                    stream_key=local.stream_key, client_order_id=local.client_order_id
                )
            except Exception:  # noqa: BLE001 - reader boundary normalizes adapter failures
                reasons.add(ExchangeReconciliationReason.ORDER_QUERY_FAILED)
                failed = True
                continue
            if query.kind is QueryResultKind.AMBIGUOUS:
                reasons.add(ExchangeReconciliationReason.ORDER_STATUS_AMBIGUOUS)
                continue
            if query.kind is QueryResultKind.NOT_FOUND:
                reasons.add(ExchangeReconciliationReason.ORDER_NOT_FOUND)
                if (
                    not local.status.is_exchange_terminal
                    and local.status is not ExchangeOrderStatus.RECONCILIATION_REQUIRED
                ):
                    try:
                        self._persistence.record_status(
                            local.durable_command_key,
                            status=ExchangeOrderStatus.RECONCILIATION_REQUIRED,
                            occurred_at_ms=self._timestamp(),
                            reason_code="reconciliation_order_not_found",
                            source=ExchangeOrderEventSource.RECONCILIATION,
                        )
                        advanced += 1
                    except (PersistenceConflictError, PersistenceIntegrityError):
                        reasons.add(ExchangeReconciliationReason.ORDER_FACT_CONFLICT)
                continue
            remote = query.order
            if remote is None or not self._same_identity(local, remote):
                reasons.add(ExchangeReconciliationReason.ORDER_FACT_CONFLICT)
                continue
            observed_by_client[remote.client_order_id] = remote
            if remote.exchange_order_id is not None:
                known_exchange_ids.add(remote.exchange_order_id)
            try:
                current = self._persistence.record_status(
                    local.durable_command_key,
                    status=remote.status,
                    occurred_at_ms=self._timestamp(),
                    reason_code="reconciliation_exchange_fact",
                    exchange_order_id=remote.exchange_order_id,
                    acknowledged_at_ms=remote.acknowledged_at_ms,
                    cumulative_executed_base_quantity=remote.cumulative_executed_base_quantity,
                    cumulative_quote_quantity=remote.cumulative_quote_quantity,
                    exchange_timestamp_ms=remote.latest_exchange_update_ms,
                    source=ExchangeOrderEventSource.RECONCILIATION,
                )
                if current.version != durable.version:
                    advanced += 1
            except Exception:  # noqa: BLE001 - persistence/domain failures become conflict
                reasons.add(ExchangeReconciliationReason.ORDER_FACT_CONFLICT)
                continue
            if remote.exchange_order_id is not None and (
                remote.cumulative_executed_base_quantity > 0
                or self._persistence.trade_completeness(local.durable_command_key)
                is TradeCompletenessStatus.INCOMPLETE
            ):
                try:
                    before_count = self._persistence.trade_count(local.durable_command_key)
                    trades = self._reader.list_order_trades(
                        stream_key=remote.stream_key,
                        exchange_order_id=remote.exchange_order_id,
                    )
                    self._persistence.persist_trades(local.durable_command_key, trades)
                    persisted_trades += (
                        self._persistence.trade_count(local.durable_command_key) - before_count
                    )
                except PersistenceConflictError:
                    reasons.add(ExchangeReconciliationReason.TRADE_FACT_CONFLICT)
                except Exception:  # noqa: BLE001 - reader boundary normalizes adapter failures
                    reasons.add(ExchangeReconciliationReason.TRADE_QUERY_FAILED)
                    failed = True
            completeness = self._persistence.trade_completeness(local.durable_command_key)
            if completeness is TradeCompletenessStatus.INCOMPLETE:
                reasons.add(ExchangeReconciliationReason.TRADE_INCOMPLETE)
            elif completeness is TradeCompletenessStatus.CONFLICT:
                reasons.add(ExchangeReconciliationReason.TRADE_FACT_CONFLICT)

        open_count = self._inspect_open_orders(
            bootstrap.root.stream_key, known_by_client, observed_by_client, reasons
        )
        try:
            history = self._reader.read_trade_history(
                stream_key=bootstrap.root.stream_key,
                start_at_ms=bootstrap.root.created_at_ms,
            )
            if any(
                trade.identity.exchange_order_id not in known_exchange_ids
                for trade in history.trades
            ):
                reasons.add(ExchangeReconciliationReason.UNKNOWN_TRADE)
            if (
                not history.complete
                or history.coverage_start_ms > bootstrap.root.created_at_ms
                or history.coverage_end_ms < started
            ):
                reasons.add(ExchangeReconciliationReason.TRADE_HISTORY_INCOMPLETE)
        except Exception:  # noqa: BLE001 - reader boundary normalizes adapter failures
            reasons.add(ExchangeReconciliationReason.TRADE_HISTORY_FAILED)
            failed = True
        balance_count = self._inspect_balances(reasons)
        try:
            if (
                self._reader.read_instrument_rules(bootstrap.root.stream_key)
                != self._expected_rules
            ):
                reasons.add(ExchangeReconciliationReason.INSTRUMENT_RULES_MISMATCH)
        except Exception:  # noqa: BLE001 - reader boundary normalizes adapter failures
            reasons.add(ExchangeReconciliationReason.INSTRUMENT_RULES_QUERY_FAILED)
            failed = True

        conflict_reasons = {
            ExchangeReconciliationReason.ORDER_FACT_CONFLICT,
            ExchangeReconciliationReason.TRADE_FACT_CONFLICT,
            ExchangeReconciliationReason.UNKNOWN_OPEN_ORDER,
            ExchangeReconciliationReason.UNKNOWN_TRADE,
            ExchangeReconciliationReason.DUPLICATE_OPEN_ORDER,
            ExchangeReconciliationReason.TERMINAL_ORDER_SHOWN_OPEN,
            ExchangeReconciliationReason.INSTRUMENT_RULES_MISMATCH,
            ExchangeReconciliationReason.BALANCE_SCOPE_INVALID,
        }
        if reasons & conflict_reasons:
            outcome = ExchangeReconciliationStatus.CONFLICT
        elif failed:
            outcome = ExchangeReconciliationStatus.FAILED
        elif reasons:
            outcome = ExchangeReconciliationStatus.REQUIRED
        else:
            outcome = ExchangeReconciliationStatus.SAFE
        generation_after = state.generation + (outcome is ExchangeReconciliationStatus.SAFE)
        completed = self._timestamp()
        ordered = tuple(sorted(reasons, key=lambda value: value.value))
        record = PersistentExchangeReconciliationRun(
            reconciliation_id,
            execution_root_id,
            bootstrap.session.session_id,
            bootstrap.root.account_fingerprint,
            state.generation,
            generation_after,
            outcome,
            len(candidates),
            advanced,
            persisted_trades,
            open_count,
            balance_count,
            tuple(value.value for value in ordered),
            started,
            completed,
        )
        durable_state = self._persistence.record_reconciliation_run(record)
        return ExchangeReconciliationResult(
            reconciliation_id,
            execution_root_id,
            bootstrap.session.session_id,
            bootstrap.root.account_fingerprint,
            state.generation,
            durable_state.generation,
            outcome,
            len(candidates),
            advanced,
            persisted_trades,
            open_count,
            balance_count,
            ordered,
            started,
            completed,
        )

    def _inspect_open_orders(self, stream_key, known, observed, reasons) -> int:
        try:
            open_orders = self._reader.list_open_orders(stream_key=stream_key)
        except Exception:  # noqa: BLE001 - reader boundary normalizes adapter failures
            reasons.add(ExchangeReconciliationReason.ORDER_QUERY_FAILED)
            return 0
        seen: dict[str, ExchangeOrder] = {}
        for remote in open_orders:
            previous = seen.get(remote.client_order_id)
            if previous is not None:
                if previous != remote:
                    reasons.add(ExchangeReconciliationReason.DUPLICATE_OPEN_ORDER)
                continue
            seen[remote.client_order_id] = remote
            local = known.get(remote.client_order_id)
            if local is None:
                reasons.add(ExchangeReconciliationReason.UNKNOWN_OPEN_ORDER)
            elif local.order.status.is_exchange_terminal:
                reasons.add(ExchangeReconciliationReason.TERMINAL_ORDER_SHOWN_OPEN)
            elif not self._same_identity(local.order, remote):
                reasons.add(ExchangeReconciliationReason.ORDER_FACT_CONFLICT)
            direct = observed.get(remote.client_order_id)
            if direct is not None and (direct.status.is_exchange_terminal or direct != remote):
                reasons.add(ExchangeReconciliationReason.ORDER_FACT_CONFLICT)
        for client_id, remote in observed.items():
            if remote.status in self._OPEN and client_id not in seen:
                reasons.add(ExchangeReconciliationReason.EXPECTED_OPEN_ORDER_MISSING)
        return len(open_orders)

    def _inspect_balances(self, reasons: set[ExchangeReconciliationReason]) -> int:
        try:
            balances = self._reader.read_balances(self._balance_assets)
        except Exception:  # noqa: BLE001 - reader boundary normalizes adapter failures
            reasons.add(ExchangeReconciliationReason.BALANCE_QUERY_FAILED)
            return 0
        if tuple(value.asset for value in balances) != self._balance_assets:
            reasons.add(ExchangeReconciliationReason.BALANCE_SCOPE_INVALID)
        return len(balances)

    @staticmethod
    def _same_identity(local: ExchangeOrder, remote: ExchangeOrder) -> bool:
        return (
            local.durable_command_key == remote.durable_command_key
            and local.client_order_id == remote.client_order_id
            and local.account_fingerprint == remote.account_fingerprint
            and local.stream_key == remote.stream_key
            and local.side is remote.side
            and local.original_base_quantity == remote.original_base_quantity
            and local.environment is remote.environment
            and local.venue == remote.venue
            and (
                local.exchange_order_id is None
                or local.exchange_order_id == remote.exchange_order_id
            )
        )

    def _timestamp(self) -> int:
        value = self._timestamp_provider()
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise TypeError("timestamp provider must return a non-negative integer")
        return value

    def _id(self) -> str:
        value = self._id_factory()
        if not isinstance(value, str) or not value.strip():
            raise TypeError("reconciliation ID factory must return non-empty text")
        return value
