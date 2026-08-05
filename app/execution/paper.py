from decimal import Decimal, DecimalException

from app.execution.exceptions import (
    ExecutionArithmeticError,
    ExecutionConfigurationError,
    ExecutionInvariantError,
    UnsupportedRiskDecisionError,
)
from app.execution.models import (
    ExecutionIdFactory,
    Fill,
    FillIdFactory,
    FillSide,
    PaperExecutionConfig,
    PaperExecutionResult,
    PaperExecutionStatus,
    create_execution_id,
    create_fill_id,
)
from app.portfolio.models import InstrumentRules, precise_add, precise_product, precise_subtract
from app.risk.models import ApprovedOrderParameters, RiskOrderSide, RiskSizingAudit
from app.trading.models import RiskDecision, RiskDecisionOutcome


class PaperExecutionProvider:
    """Stateless deterministic immediate-fill paper execution adapter."""

    def __init__(
        self,
        config: PaperExecutionConfig,
        instrument_rules: InstrumentRules,
        execution_id_factory: ExecutionIdFactory,
        fill_id_factory: FillIdFactory,
    ) -> None:
        if not isinstance(config, PaperExecutionConfig):
            raise ExecutionConfigurationError("config must be a PaperExecutionConfig")
        if not isinstance(instrument_rules, InstrumentRules):
            raise ExecutionConfigurationError("instrument_rules must be InstrumentRules")
        if not isinstance(execution_id_factory, ExecutionIdFactory):
            raise ExecutionConfigurationError(
                "execution_id_factory must implement ExecutionIdFactory"
            )
        if not isinstance(fill_id_factory, FillIdFactory):
            raise ExecutionConfigurationError("fill_id_factory must implement FillIdFactory")
        self._config = config
        self._instrument_rules = instrument_rules
        self._execution_id_factory = execution_id_factory
        self._fill_id_factory = fill_id_factory

    @property
    def name(self) -> str:
        return self._config.provider_name

    @property
    def version(self) -> str:
        return self._config.provider_version

    @property
    def configuration(self) -> PaperExecutionConfig:
        return self._config

    @property
    def instrument_rules(self) -> InstrumentRules:
        return self._instrument_rules

    def execute(self, decision: RiskDecision) -> PaperExecutionResult:
        if not isinstance(decision, RiskDecision):
            raise UnsupportedRiskDecisionError("decision must be a RiskDecision")
        execution_id = create_execution_id(self._execution_id_factory)
        completed_at_ms = decision.decided_at_ms + self._config.latency_ms
        if decision.outcome is RiskDecisionOutcome.REJECTED:
            if decision.approved_order is not None or decision.approved_target_exposure is not None:
                raise ExecutionInvariantError("rejected decision contains approved parameters")
            return self._result(
                execution_id,
                decision,
                PaperExecutionStatus.NOT_EXECUTED,
                None,
                completed_at_ms,
                "risk_rejected",
            )
        if decision.outcome not in {
            RiskDecisionOutcome.APPROVED,
            RiskDecisionOutcome.MODIFIED,
        }:
            raise UnsupportedRiskDecisionError("unsupported risk decision outcome")
        order = decision.approved_order
        audit = decision.sizing_audit
        if not isinstance(order, ApprovedOrderParameters) or not isinstance(audit, RiskSizingAudit):
            raise ExecutionInvariantError("executable decision is incomplete")
        rules = self._instrument_rules
        if decision.stream_key != rules.stream_key:
            raise ExecutionInvariantError("decision stream does not match instrument rules")
        if audit.instrument_rules_version != rules.rules_version:
            raise ExecutionInvariantError("instrument rules version does not match decision")
        if (
            decision.approved_target_exposure != order.approved_target_exposure
            or audit.approved_target_exposure != order.approved_target_exposure
            or audit.rounded_order_quantity != order.quantity
            or audit.approved_order_notional != order.gross_notional
        ):
            raise ExecutionInvariantError("approved order disagrees with risk sizing audit")
        if rules.round_quantity_down(order.quantity) != order.quantity:
            raise ExecutionInvariantError("approved quantity is not aligned")
        if rules.round_price_down(order.reference_price) != order.reference_price:
            raise ExecutionInvariantError("approved reference price is not aligned")
        if order.side is RiskOrderSide.BUY and order.dust_liquidation:
            raise ExecutionInvariantError("BUY cannot be a dust liquidation")
        if order.side is RiskOrderSide.SELL and order.required_quote != Decimal(0):
            raise ExecutionInvariantError("SELL cannot contain BUY quote requirements")
        if self._config.fee_rate > order.max_fee_rate:
            raise ExecutionConfigurationError("fee rate exceeds the approved risk envelope")

        try:
            gross = rules.round_quote(precise_product(order.quantity, order.reference_price))
            if gross != order.gross_notional:
                raise ExecutionInvariantError("approved gross notional is inconsistent")
            fee = rules.round_quote(precise_product(gross, self._config.fee_rate))
            fill_side = FillSide.BUY if order.side is RiskOrderSide.BUY else FillSide.SELL
            if fill_side is FillSide.BUY:
                quote_change = -precise_add(gross, fee)
                base_change = order.quantity
                if -quote_change > order.required_quote:
                    raise ExecutionInvariantError("BUY debit exceeds approved quote envelope")
            else:
                if fee > gross:
                    raise ExecutionInvariantError("SELL fee exceeds gross notional")
                quote_change = precise_subtract(gross, fee)
                base_change = -order.quantity
        except DecimalException as error:
            raise ExecutionArithmeticError("paper execution arithmetic failed") from error

        fill_id = create_fill_id(self._fill_id_factory)
        if fill_id == execution_id:
            raise ExecutionInvariantError("fill ID must differ from execution ID")
        fill = Fill(
            fill_id=fill_id,
            order_id=execution_id,
            risk_decision_id=decision.decision_id,
            intent_id=decision.intent_id,
            stream_key=rules.stream_key,
            side=fill_side,
            quantity=order.quantity,
            price=order.reference_price,
            gross_notional=gross,
            fee=fee,
            fee_asset=rules.quote_asset,
            net_quote_change=quote_change,
            net_base_change=base_change,
            filled_at_ms=completed_at_ms,
            correlation_id=decision.correlation_id,
            metadata={
                "provider_name": self.name,
                "provider_version": self.version,
                "fee_rate": self._config.fee_rate,
                "latency_ms": self._config.latency_ms,
                "instrument_rules_version": rules.rules_version,
                "risk_outcome": decision.outcome.value,
                "dust_liquidation": order.dust_liquidation,
            },
        )
        return self._result(
            execution_id,
            decision,
            PaperExecutionStatus.FILLED,
            fill,
            completed_at_ms,
            "filled",
        )

    def _result(
        self,
        execution_id: str,
        decision: RiskDecision,
        status: PaperExecutionStatus,
        fill: Fill | None,
        completed_at_ms: int,
        reason_code: str,
    ) -> PaperExecutionResult:
        return PaperExecutionResult(
            execution_id=execution_id,
            risk_decision_id=decision.decision_id,
            intent_id=decision.intent_id,
            status=status,
            fill=fill,
            started_at_ms=decision.decided_at_ms,
            completed_at_ms=completed_at_ms,
            latency_ms=self._config.latency_ms,
            provider_name=self.name,
            provider_version=self.version,
            reason_code=reason_code,
            risk_rejection_reasons=tuple(str(reason) for reason in decision.rejection_reasons),
            correlation_id=decision.correlation_id,
            metadata={
                "provider_name": self.name,
                "provider_version": self.version,
                "risk_outcome": decision.outcome.value,
            },
        )
