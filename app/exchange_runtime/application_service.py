import json
from dataclasses import dataclass
from decimal import Decimal

from app.exchange_execution import (
    DeterministicClientOrderIdFactory,
    DurableExchangeOrderLifecycle,
    ExchangeCommand,
    ExchangeOrderSide,
    ExecutionEnvironment,
    PendingExposure,
    SubmissionCapability,
    aggregate_pending_exposure,
    create_client_order_id,
)
from app.exchange_runtime.submission import (
    PreSubmitGateResult,
    TestnetPreSubmitGate,
    _pending_exposure,
)
from app.market.models import MarketStreamKey
from app.persistence.canonical import canonical_sha256
from app.persistence.exceptions import PersistenceConflictError
from app.persistence.exchange import SQLiteExchangePersistence
from app.persistence.keys import (
    DurableCommandKeyInput,
    StableDecisionKeyInput,
    decision_slot_key,
    durable_command_key,
    fingerprint_strategy_input,
    stable_decision_key,
)
from app.portfolio import Portfolio
from app.risk import RiskManager
from app.risk.exchange import (
    ExchangeReconciliationStatus,
    ExchangeRiskAssessment,
    ExchangeRiskContext,
    ExchangeRiskPolicy,
    ReconciledAccountState,
    evaluate_exchange_risk,
)
from app.trading.models import MarketSnapshot, RiskDecisionOutcome
from app.trading.pipeline import DecisionPipelineResult, PipelineOutcome


@dataclass(frozen=True, slots=True)
class TestnetExecutionApplicationResult:
    risk_outcome: RiskDecisionOutcome
    assessment: ExchangeRiskAssessment | None
    gate: PreSubmitGateResult | None
    durable_command_key: str | None


class TestnetExecutionApplicationService:
    """Authoritative decision-to-durable-Testnet-order application boundary."""

    def __init__(
        self,
        persistence: SQLiteExchangePersistence,
        portfolio: Portfolio,
        financial_risk: RiskManager,
        exchange_policy: ExchangeRiskPolicy,
        lifecycle: DurableExchangeOrderLifecycle,
        gate: TestnetPreSubmitGate,
        *,
        execution_root_id: str,
        capability: SubmissionCapability,
        exchange_name: str,
        strategy_config_fingerprint: str,
        planner_name: str,
        planner_version: str,
        planner_config_fingerprint: str,
        snapshot_policy_fingerprint: str,
        risk_policy_fingerprint: str,
        instrument_rules_fingerprint: str,
        execution_config_fingerprint: str,
    ) -> None:
        self._persistence = persistence
        self._portfolio = portfolio
        self._financial_risk = financial_risk
        self._exchange_policy = exchange_policy
        self._lifecycle = lifecycle
        self._gate = gate
        self._root_id = execution_root_id
        self._capability = capability
        self._exchange_name = exchange_name
        self._strategy_fingerprint = strategy_config_fingerprint
        self._planner_name = planner_name
        self._planner_version = planner_version
        self._planner_fingerprint = planner_config_fingerprint
        self._snapshot_fingerprint = snapshot_policy_fingerprint
        self._risk_fingerprint = risk_policy_fingerprint
        self._rules_fingerprint = instrument_rules_fingerprint
        self._execution_fingerprint = execution_config_fingerprint
        self._client_ids = DeterministicClientOrderIdFactory()

    def execute(
        self, decision: DecisionPipelineResult, snapshot: MarketSnapshot
    ) -> TestnetExecutionApplicationResult:
        if (
            decision.snapshot_id != snapshot.snapshot_id
            or decision.stream_key != snapshot.stream_key
        ):
            raise ValueError("Testnet decision/snapshot identity mismatch")
        if decision.outcome is not PipelineOutcome.INTENT_CREATED or decision.order_intent is None:
            raise ValueError("authoritative execution requires an executable intent")
        latest = snapshot.latest_closed_candle
        if latest is None:
            raise ValueError("executable decision has no closed candle")
        valuation = self._portfolio.preview_mark_to_market(
            latest.close, snapshot.as_of_ms, snapshot.correlation_cycle_id
        ).after
        financial = self._financial_risk.evaluate(decision.order_intent, valuation, snapshot)
        if financial.outcome is RiskDecisionOutcome.REJECTED:
            return TestnetExecutionApplicationResult(financial.outcome, None, None, None)
        approved = financial.approved_order
        if approved is None:
            raise ValueError("approved Risk decision has no approved economics")
        stable_input = StableDecisionKeyInput(
            self._exchange_name,
            snapshot.stream_key.symbol,
            snapshot.stream_key.interval,
            latest,
            fingerprint_strategy_input(snapshot.closed_candles),
            decision.strategy_name,
            decision.strategy_version,
            self._strategy_fingerprint,
            self._planner_name,
            self._planner_version,
            self._planner_fingerprint,
            self._snapshot_fingerprint,
        )
        stable_key = stable_decision_key(stable_input)
        slot_key = decision_slot_key(stable_input)
        side = ExchangeOrderSide(approved.side.value)
        command_key = durable_command_key(
            DurableCommandKeyInput(
                stable_key,
                decision.order_intent.action.value,
                financial.requested_target_exposure,
                side.value,
                approved.quantity,
                financial.approved_target_exposure,
                approved.reference_price,
                approved.worst_case_price,
                financial.outcome.value,
                financial.modification_reasons,
                self._risk_fingerprint,
                self._rules_fingerprint,
                self._execution_fingerprint,
            )
        )
        for durable in self._persistence.list_orders():
            persisted = json.loads(durable.command_payload)
            if persisted.get("decision_slot_key") == slot_key and (
                durable.stable_decision_key != stable_key
                or durable.order.durable_command_key != command_key
            ):
                raise PersistenceConflictError("durable exchange decision slot conflicts")
        risk_authorization = canonical_sha256(
            {
                "outcome": financial.outcome,
                "approved_side": side,
                "approved_quantity": approved.quantity,
                "approved_target_exposure": financial.approved_target_exposure,
                "reference_price": approved.reference_price,
                "worst_case_price": approved.worst_case_price,
            },
            algorithm_version="exchange-risk-authorization/v1",
        )
        command = ExchangeCommand(
            command_key,
            stable_key,
            slot_key,
            snapshot.stream_key,
            side,
            approved.quantity,
            approved.reference_price,
            financial.approved_target_exposure,
            approved.gross_notional,
            risk_authorization,
            self._rules_fingerprint,
            self._execution_fingerprint,
            decision.evaluated_at_ms,
            decision.correlation_id,
        )
        bootstrap = self._persistence.load_bootstrap(self._root_id)
        client_id = create_client_order_id(
            self._client_ids,
            environment=ExecutionEnvironment.TESTNET,
            venue=bootstrap.root.venue,
            account_fingerprint=bootstrap.root.account_fingerprint,
            durable_command_key=command_key,
        )
        existing = next(
            (
                value
                for value in self._persistence.list_orders()
                if value.order.durable_command_key == command_key
            ),
            None,
        )
        if existing is not None:
            if (
                existing.stable_decision_key != stable_key
                or existing.order.client_order_id != client_id
                or existing.execution_root_id != self._root_id
                or existing.session_id != bootstrap.session.session_id
            ):
                raise ValueError("durable command identity conflicts")
            return TestnetExecutionApplicationResult(financial.outcome, None, None, command_key)
        context = self._current_context(snapshot.stream_key)
        assessment = evaluate_exchange_risk(financial, valuation, context, self._exchange_policy)
        if assessment.outcome.value != "APPROVED":
            return TestnetExecutionApplicationResult(financial.outcome, assessment, None, None)
        self._lifecycle.prepare(
            execution_root_id=self._root_id,
            session_id=bootstrap.session.session_id,
            command=command,
            client_order_id=client_id,
        )
        gate, _ = self._gate.authorize_and_submit(
            command_key,
            assessment=assessment,
            context=context,
            capability=self._capability,
            expected_rules_fingerprint=self._rules_fingerprint,
            now_ms=decision.evaluated_at_ms,
        )
        return TestnetExecutionApplicationResult(financial.outcome, assessment, gate, command_key)

    def _current_context(self, stream_key: MarketStreamKey) -> ExchangeRiskContext:
        bootstrap = self._persistence.load_bootstrap(self._root_id)
        reconciliation = self._persistence.read_reconciliation_state(self._root_id)
        durable_orders = tuple(
            value
            for value in self._persistence.list_orders()
            if value.execution_root_id == self._root_id and not value.order.status.is_terminal
        )
        orders = tuple(value.order for value in durable_orders)
        pending = (
            aggregate_pending_exposure(_pending_exposure(value) for value in durable_orders)
            if orders
            else PendingExposure()
        )
        attempts = self._persistence._connection.execute(
            "SELECT count(*) FROM exchange_submission_attempts a JOIN exchange_orders o "
            "ON o.durable_command_key=a.durable_command_key WHERE o.execution_root_id=? "
            "AND o.status='SUBMITTING'",
            (self._root_id,),
        ).fetchone()[0]
        snapshot = self._portfolio.snapshot
        account = ReconciledAccountState(
            bootstrap.root.account_fingerprint,
            stream_key,
            snapshot.base_asset,
            snapshot.quote_asset,
            snapshot.base_balance,
            Decimal(0),
            snapshot.quote_balance,
            Decimal(0),
            ExchangeReconciliationStatus(reconciliation.status.value),
            reconciliation.generation,
        )
        buy = sum(
            (
                order.original_base_quantity - order.cumulative_executed_base_quantity
                for order in orders
                if order.side is ExchangeOrderSide.BUY
            ),
            Decimal(0),
        )
        sell = sum(
            (
                order.original_base_quantity - order.cumulative_executed_base_quantity
                for order in orders
                if order.side is ExchangeOrderSide.SELL
            ),
            Decimal(0),
        )
        return ExchangeRiskContext(
            account,
            pending,
            bootstrap.session,
            None,
            len(orders),
            attempts,
            bootstrap.control.kill_switch.generation,
            buy,
            sell,
        )
