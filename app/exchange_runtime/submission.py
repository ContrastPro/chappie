import json
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from app.exchange_execution import (
    DurableExchangeOrderLifecycle,
    ExchangeOrderStatus,
    ExecutionEnvironment,
    PendingExposure,
    Phase3ProcessClaim,
    Stage4ManagedSellProcessClaim,
    SubmissionCapability,
    aggregate_pending_exposure,
)
from app.exchange_execution.ports import PreSubmitAuthorization
from app.exchange_runtime.exceptions import ExchangeSettlementError
from app.exchange_runtime.settlement import SQLiteExchangeSettlement
from app.persistence.exceptions import PersistenceIntegrityError
from app.persistence.exchange import SQLiteExchangePersistence
from app.persistence.exchange_models import (
    ExchangeReconciliationStatus,
    LocalExchangeRecoveryStatus,
)
from app.risk.exchange import (
    ExchangeRiskAssessment,
    ExchangeRiskContext,
    evaluate_exchange_submission_safety,
)


class PreSubmitBlockReason(StrEnum):
    MODE_NOT_TESTNET = "MODE_NOT_TESTNET"
    ORDER_NOT_PREPARED = "ORDER_NOT_PREPARED"
    LINEAGE_MISMATCH = "LINEAGE_MISMATCH"
    RISK_CONTEXT_STALE = "RISK_CONTEXT_STALE"
    RISK_BLOCKED = "RISK_BLOCKED"
    CAPABILITY_INVALID = "CAPABILITY_INVALID"
    RECONCILIATION_UNSAFE = "RECONCILIATION_UNSAFE"
    CONTROL_UNSAFE = "CONTROL_UNSAFE"
    SETTLEMENT_UNSAFE = "SETTLEMENT_UNSAFE"
    PENDING_ORDER_LIMIT = "PENDING_ORDER_LIMIT"
    IN_FLIGHT_LIMIT = "IN_FLIGHT_LIMIT"
    RULES_MISMATCH = "RULES_MISMATCH"
    AUTHORIZATION_STALE = "AUTHORIZATION_STALE"
    LOCAL_RECOVERY_UNSAFE = "LOCAL_RECOVERY_UNSAFE"
    PORTFOLIO_RECOVERY_UNSAFE = "PORTFOLIO_RECOVERY_UNSAFE"


@dataclass(frozen=True, slots=True)
class PreSubmitGateResult:
    authorization: PreSubmitAuthorization | None
    reasons: tuple[PreSubmitBlockReason, ...]

    @property
    def authorized(self) -> bool:
        return self.authorization is not None

    def __post_init__(self) -> None:
        if tuple(sorted(set(self.reasons), key=lambda value: value.value)) != self.reasons:
            raise ValueError("pre-submit reasons must be sorted and unique")
        if self.authorized == bool(self.reasons):
            raise ValueError("authorization and block reasons are mutually exclusive")


class TestnetPreSubmitGate:
    """Single synchronous authority immediately preceding the durable side effect."""

    def __init__(
        self,
        persistence: SQLiteExchangePersistence,
        lifecycle: DurableExchangeOrderLifecycle,
        *,
        execution_root_id: str,
        max_authorization_age_ms: int,
        settlement: SQLiteExchangeSettlement | None = None,
    ) -> None:
        if not isinstance(persistence, SQLiteExchangePersistence):
            raise TypeError("persistence must be SQLiteExchangePersistence")
        if not isinstance(lifecycle, DurableExchangeOrderLifecycle):
            raise TypeError("lifecycle must be DurableExchangeOrderLifecycle")
        if (
            isinstance(max_authorization_age_ms, bool)
            or not isinstance(max_authorization_age_ms, int)
            or max_authorization_age_ms <= 0
        ):
            raise TypeError("max_authorization_age_ms must be positive")
        self._persistence = persistence
        self._lifecycle = lifecycle
        self._root_id = execution_root_id
        self._max_age = max_authorization_age_ms
        self._settlement = settlement

    def authorize(
        self,
        durable_command_key: str,
        *,
        assessment: ExchangeRiskAssessment,
        context: ExchangeRiskContext,
        capability: SubmissionCapability | None,
        expected_rules_fingerprint: str,
        now_ms: int,
        phase3_claim: Phase3ProcessClaim | None = None,
        stage4_sell_claim: Stage4ManagedSellProcessClaim | None = None,
    ) -> PreSubmitGateResult:
        """Reread every mutable durable gate; never performs network I/O or mutation."""
        reasons: set[PreSubmitBlockReason] = set()
        try:
            recovery = self._persistence.recover_local()
        except PersistenceIntegrityError:
            return PreSubmitGateResult(None, (PreSubmitBlockReason.LOCAL_RECOVERY_UNSAFE,))
        if recovery.status is not LocalExchangeRecoveryStatus.CLEAN:
            reasons.add(PreSubmitBlockReason.LOCAL_RECOVERY_UNSAFE)
        if self._settlement is not None:
            try:
                self._settlement.validate_authority()
            except ExchangeSettlementError:
                reasons.add(PreSubmitBlockReason.PORTFOLIO_RECOVERY_UNSAFE)
        durable = self._persistence.load_order(durable_command_key)
        bootstrap = self._persistence.load_bootstrap(self._root_id)
        reconciliation = self._persistence.read_reconciliation_state(self._root_id)
        orders = tuple(
            value
            for value in self._persistence.list_orders()
            if value.execution_root_id == self._root_id
        )
        session = self._persistence.load_session(durable.session_id)
        if durable.order.environment is not ExecutionEnvironment.TESTNET:
            reasons.add(PreSubmitBlockReason.MODE_NOT_TESTNET)
        if durable.order.status is not ExchangeOrderStatus.PREPARED:
            reasons.add(PreSubmitBlockReason.ORDER_NOT_PREPARED)
        if (
            durable.execution_root_id != self._root_id
            or durable.session_id != bootstrap.session.session_id
            or durable.order.account_fingerprint != bootstrap.root.account_fingerprint
            or assessment.session_id != durable.session_id
        ):
            reasons.add(PreSubmitBlockReason.LINEAGE_MISMATCH)
        if reconciliation.status is not ExchangeReconciliationStatus.SAFE:
            reasons.add(PreSubmitBlockReason.RECONCILIATION_UNSAFE)
        phase3_valid = self._phase3_claim_valid(
            phase3_claim, durable, bootstrap, reconciliation.generation
        )
        stage4_sell_valid = self._stage4_sell_claim_valid(
            stage4_sell_claim, durable, bootstrap, reconciliation.generation
        )
        exceptional_claim_valid = phase3_valid or stage4_sell_valid
        if bootstrap.control.reconciliation_generation != reconciliation.generation or (
            bootstrap.control.kill_switch.state.value != "CLEARED" and not exceptional_claim_valid
        ):
            reasons.add(PreSubmitBlockReason.CONTROL_UNSAFE)
        unsettled, blocked = self._settlement_counts()
        if unsettled or blocked:
            reasons.add(PreSubmitBlockReason.SETTLEMENT_UNSAFE)
        other_open = sum(
            not value.order.status.is_exchange_terminal
            and value.order.durable_command_key != durable_command_key
            for value in orders
        )
        if other_open:
            reasons.add(PreSubmitBlockReason.PENDING_ORDER_LIMIT)
        attempts = self._persistence._connection.execute(
            "SELECT count(*) FROM exchange_submission_attempts a JOIN exchange_orders o "
            "ON o.durable_command_key=a.durable_command_key WHERE o.execution_root_id=? "
            "AND o.status='SUBMITTING'",
            (self._root_id,),
        ).fetchone()[0]
        if attempts:
            reasons.add(PreSubmitBlockReason.IN_FLIGHT_LIMIT)
        command = json.loads(durable.command_payload)
        if command["instrument_rules_fingerprint"] != expected_rules_fingerprint:
            reasons.add(PreSubmitBlockReason.RULES_MISMATCH)
        authorized_at = command["authorized_at_ms"]
        if now_ms < authorized_at or now_ms - authorized_at > self._max_age:
            reasons.add(PreSubmitBlockReason.AUTHORIZATION_STALE)
        pending = aggregate_pending_exposure(
            _pending_exposure(value)
            for value in orders
            if value.order.durable_command_key != durable_command_key
            and not value.order.status.is_exchange_terminal
        )
        if (
            context.execution_session != session
            or context.pending_exposure != pending
            or context.non_terminal_order_count != other_open
            or context.in_flight_submission_count != attempts
            or context.account.reconciliation_generation != reconciliation.generation
        ):
            reasons.add(PreSubmitBlockReason.RISK_CONTEXT_STALE)
        safety = None
        if not exceptional_claim_valid:
            safety = evaluate_exchange_submission_safety(
                assessment,
                context,
                bootstrap.control.kill_switch,
                capability,
                environment=durable.order.environment,
                pending_control_safe=not other_open and not attempts,
            )
        if assessment.outcome.value != "APPROVED":
            reasons.add(PreSubmitBlockReason.RISK_BLOCKED)
        if not exceptional_claim_valid and (safety is None or not safety.authorized):
            reasons.add(PreSubmitBlockReason.CAPABILITY_INVALID)
        ordered = tuple(sorted(reasons, key=lambda value: value.value))
        if ordered:
            return PreSubmitGateResult(None, ordered)
        if phase3_valid and phase3_claim is not None:
            phase3_claim._consume()
        if stage4_sell_valid and stage4_sell_claim is not None:
            stage4_sell_claim._consume()
        return PreSubmitGateResult(
            PreSubmitAuthorization._issue(
                execution_root_id=self._root_id,
                session_id=durable.session_id,
                account_fingerprint=durable.order.account_fingerprint,
                durable_command_key=durable_command_key,
                client_order_id=durable.order.client_order_id,
                risk_decision_id=assessment.risk_decision_id,
                rules_fingerprint=expected_rules_fingerprint,
                reconciliation_generation=reconciliation.generation,
                control_generation=bootstrap.control.kill_switch.generation,
            ),
            (),
        )

    def _phase3_claim_valid(
        self, claim, durable, bootstrap, reconciliation_generation: int
    ) -> bool:
        if not isinstance(claim, Phase3ProcessClaim) or claim._consumed:
            return False
        if (
            claim.authority_id == ""
            or claim.durable_command_key != durable.order.durable_command_key
            or claim.execution_root_id != self._root_id
            or claim.session_id != durable.session_id
            or claim.account_fingerprint != durable.order.account_fingerprint
            or claim.control_generation != bootstrap.control.kill_switch.generation
            or claim.reconciliation_generation != reconciliation_generation
        ):
            return False
        command = json.loads(durable.command_payload)
        row = self._persistence._connection.execute(
            "SELECT 1 FROM exchange_smoke_authorities WHERE authority_id=? "
            "AND consumed_durable_command_key=? AND execution_root_id=? AND session_id=? "
            "AND account_fingerprint=? AND control_generation=? AND reconciliation_generation=? "
            "AND economic_slot_key=? AND risk_envelope_digest=? AND balance_scope_digest=? "
            "AND rules_fingerprint=? AND config_fingerprint=? AND environment='testnet' "
            "AND managed_portfolio_id=? AND venue='binance-spot' AND symbol='BTCUSDT' "
            "AND side='BUY' AND status='CONSUMED'",
            (
                claim.authority_id,
                claim.durable_command_key,
                claim.execution_root_id,
                claim.session_id,
                claim.account_fingerprint,
                claim.control_generation,
                claim.reconciliation_generation,
                claim.economic_slot_key,
                claim.envelope_digest,
                claim.balance_scope_digest,
                claim.rules_fingerprint,
                claim.config_fingerprint,
                claim.portfolio_id,
            ),
        ).fetchone()
        return row is not None and (
            command["instrument_rules_fingerprint"] == claim.rules_fingerprint
            and command["execution_configuration_fingerprint"] == claim.config_fingerprint
        )

    def _stage4_sell_claim_valid(
        self, claim, durable, bootstrap, reconciliation_generation: int
    ) -> bool:
        if not isinstance(claim, Stage4ManagedSellProcessClaim) or claim._consumed:
            return False
        if (
            claim.command_key != durable.order.durable_command_key
            or claim.execution_root_id != self._root_id
            or claim.session_id != durable.session_id
            or claim.account_fingerprint != durable.order.account_fingerprint
            or durable.order.side.value != "SELL"
            or claim.environment is not ExecutionEnvironment.TESTNET
            or claim.control_generation != bootstrap.control.kill_switch.generation
            or claim.reconciliation_generation != reconciliation_generation
        ):
            return False
        command = json.loads(durable.command_payload)
        current = self._persistence._connection.execute(
            "SELECT portfolio_version,state_digest FROM exchange_portfolio_current "
            "WHERE portfolio_id=?",
            (claim.portfolio_id,),
        ).fetchone()
        session = self._persistence.load_session(claim.session_id)
        row = self._persistence._connection.execute(
            "SELECT 1 FROM exchange_managed_sell_authorities WHERE authority_id=? "
            "AND consumed_durable_command_key=? AND execution_root_id=? AND session_id=? "
            "AND account_fingerprint=? AND managed_portfolio_id=? "
            "AND predecessor_portfolio_version=? AND predecessor_portfolio_digest=? "
            "AND control_generation=? AND reconciliation_generation=? "
            "AND settlement_state_digest=? AND economic_slot_key=? "
            "AND rules_fingerprint=? AND risk_fingerprint=? AND config_fingerprint=? "
            "AND risk_envelope_digest=? AND ownership_scope_digest=? "
            "AND environment='testnet' AND venue='binance-spot' AND symbol='BTCUSDT' "
            "AND side='SELL' AND order_type='MARKET' AND status='CONSUMED'",
            (
                claim.authority_id,
                claim.command_key,
                claim.execution_root_id,
                claim.session_id,
                claim.account_fingerprint,
                claim.portfolio_id,
                claim.portfolio_version,
                claim.portfolio_digest,
                claim.control_generation,
                claim.reconciliation_generation,
                claim.settlement_state_digest,
                claim.economic_slot_key,
                claim.rules_fingerprint,
                claim.risk_fingerprint,
                claim.config_fingerprint,
                claim.envelope_digest,
                claim.ownership_scope_digest,
            ),
        ).fetchone()
        if self._persistence._trading.config.expected_schema_version >= 8:
            recovery = self._persistence._connection.execute(
                "SELECT count(*) FROM exchange_managed_sell_recovery_events WHERE authority_id=?",
                (claim.authority_id,),
            ).fetchone()[0]
            if recovery:
                return False
        return (
            row is not None
            and current is not None
            and tuple(current) == (claim.portfolio_version, claim.portfolio_digest)
            and session.submission_count + 1
            == self._persistence._connection.execute(
                "SELECT permitted_submission_count FROM exchange_managed_sell_authorities "
                "WHERE authority_id=?",
                (claim.authority_id,),
            ).fetchone()[0]
            and command["instrument_rules_fingerprint"] == claim.rules_fingerprint
            and command["risk_authorization_fingerprint"] == claim.risk_fingerprint
            and command["execution_configuration_fingerprint"] == claim.config_fingerprint
        )

    def authorize_and_submit(self, *args, **kwargs):
        result = self.authorize(*args, **kwargs)
        if not result.authorized:
            return result, None
        assert result.authorization is not None
        return result, self._lifecycle._submit_authorized(result.authorization)

    def _settlement_counts(self) -> tuple[int, int]:
        blocked_sql = (
            "SELECT count(*) FROM exchange_trade_settlements s JOIN exchange_orders o ON "
            "o.durable_command_key=s.durable_command_key LEFT JOIN "
            "exchange_trade_settlement_resolutions r ON "
            "r.blocked_settlement_id=s.settlement_id WHERE o.execution_root_id=? "
            "AND s.status='BLOCKED_UNSUPPORTED_FEE' AND r.resolution_id IS NULL"
            if self._persistence._trading.config.expected_schema_version >= 6
            else "SELECT count(*) FROM exchange_trade_settlements s JOIN exchange_orders o ON "
            "o.durable_command_key=s.durable_command_key WHERE o.execution_root_id=? "
            "AND s.status='BLOCKED_UNSUPPORTED_FEE'"
        )
        row = self._persistence._connection.execute(
            "SELECT "
            "(SELECT count(*) FROM exchange_trades t JOIN exchange_orders o ON "
            "o.durable_command_key=t.durable_command_key LEFT JOIN exchange_trade_settlements s "
            "ON s.venue=t.venue AND s.environment=t.environment AND "
            "s.account_fingerprint=t.account_fingerprint AND s.symbol=t.symbol AND "
            "s.exchange_trade_id=t.exchange_trade_id WHERE o.execution_root_id=? "
            "AND s.settlement_id IS NULL),(" + blocked_sql + ")",
            (self._root_id, self._root_id),
        ).fetchone()
        return row[0], row[1]


def _pending_exposure(durable) -> PendingExposure:
    order = durable.order
    remaining = order.original_base_quantity - order.cumulative_executed_base_quantity
    return PendingExposure(
        durable.approved_notional if order.side.value == "BUY" else Decimal(0),
        remaining if order.side.value == "SELL" else Decimal(0),
        durable.approved_notional,
        order.original_base_quantity,
        order.cumulative_executed_base_quantity,
        remaining,
        1,
        int(order.status is ExchangeOrderStatus.SUBMITTING),
    )
