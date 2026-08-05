from decimal import Decimal, localcontext

from app.portfolio.models import InstrumentRules, PortfolioSnapshot, decimal_precision
from app.risk.exceptions import RiskConfigurationError, RiskInvariantError
from app.risk.models import (
    ONE,
    ZERO,
    ApprovedOrderParameters,
    PortfolioRiskPolicy,
    RiskDecisionIdFactory,
    RiskModificationReason,
    RiskOrderSide,
    RiskRejectionReason,
    RiskSizingAudit,
    create_decision_id,
)
from app.trading.models import (
    MarketSnapshot,
    OrderIntent,
    OrderIntentAction,
    RiskDecision,
    RiskDecisionOutcome,
)

TEN_THOUSAND = Decimal(10_000)


def _calculate(operation: str, *values: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = decimal_precision(*values, guard_digits=50)
        if operation == "add":
            return values[0] + values[1]
        if operation == "subtract":
            return values[0] - values[1]
        if operation == "multiply":
            return values[0] * values[1]
        if operation == "divide":
            return values[0] / values[1]
    raise RiskInvariantError(f"unsupported arithmetic operation: {operation}")


def _ordered_unique(
    reasons: list[RiskModificationReason],
) -> tuple[RiskModificationReason, ...]:
    return tuple(dict.fromkeys(reasons))


class RiskManager:
    """Pure deterministic long-only Spot risk evaluator."""

    def __init__(
        self,
        policy: PortfolioRiskPolicy,
        instrument_rules: InstrumentRules,
        decision_id_factory: RiskDecisionIdFactory,
    ) -> None:
        if not isinstance(policy, PortfolioRiskPolicy):
            raise RiskConfigurationError("policy must be a PortfolioRiskPolicy")
        if not isinstance(instrument_rules, InstrumentRules):
            raise RiskConfigurationError("instrument_rules must be InstrumentRules")
        if not isinstance(decision_id_factory, RiskDecisionIdFactory):
            raise RiskConfigurationError("decision_id_factory must implement RiskDecisionIdFactory")
        self._policy = policy
        self._instrument_rules = instrument_rules
        self._decision_id_factory = decision_id_factory

    @property
    def name(self) -> str:
        return self._policy.policy_name

    @property
    def version(self) -> str:
        return self._policy.policy_version

    @property
    def policy(self) -> PortfolioRiskPolicy:
        return self._policy

    @property
    def instrument_rules(self) -> InstrumentRules:
        return self._instrument_rules

    def evaluate(
        self,
        intent: OrderIntent,
        portfolio: PortfolioSnapshot,
        market_snapshot: MarketSnapshot,
    ) -> RiskDecision:
        if not isinstance(intent, OrderIntent):
            raise RiskInvariantError("intent must be an OrderIntent")
        if not isinstance(portfolio, PortfolioSnapshot):
            raise RiskInvariantError("portfolio must be a PortfolioSnapshot")
        if not isinstance(market_snapshot, MarketSnapshot):
            raise RiskInvariantError("market_snapshot must be a MarketSnapshot")

        decision_id = create_decision_id(self._decision_id_factory)
        empty_audit = self._empty_audit(intent.target_exposure, portfolio)

        if self._policy.reject_stale_market and market_snapshot.is_stale:
            return self._reject(
                decision_id,
                intent,
                portfolio,
                market_snapshot,
                empty_audit,
                RiskRejectionReason.STALE_MARKET,
            )
        latest = market_snapshot.latest_closed_candle
        if latest is None:
            return self._reject(
                decision_id,
                intent,
                portfolio,
                market_snapshot,
                empty_audit,
                RiskRejectionReason.NO_CLOSED_CANDLE,
            )
        reference_price = latest.close
        audit = self._empty_audit(intent.target_exposure, portfolio, reference_price)

        if intent.stream_key != market_snapshot.stream_key:
            return self._reject(
                decision_id,
                intent,
                portfolio,
                market_snapshot,
                audit,
                RiskRejectionReason.STREAM_MISMATCH,
            )
        if portfolio.stream_key != intent.stream_key:
            return self._reject(
                decision_id,
                intent,
                portfolio,
                market_snapshot,
                audit,
                RiskRejectionReason.PORTFOLIO_STREAM_MISMATCH,
            )
        rules = self._instrument_rules
        if rules.stream_key != intent.stream_key:
            return self._reject(
                decision_id,
                intent,
                portfolio,
                market_snapshot,
                audit,
                RiskRejectionReason.STREAM_MISMATCH,
            )
        if portfolio.base_asset != rules.base_asset or portfolio.quote_asset != rules.quote_asset:
            raise RiskInvariantError("portfolio assets do not match instrument rules")
        if intent.created_at_ms != market_snapshot.as_of_ms:
            return self._reject(
                decision_id,
                intent,
                portfolio,
                market_snapshot,
                audit,
                RiskRejectionReason.INTENT_TIME_MISMATCH,
            )
        if portfolio.as_of_ms != market_snapshot.as_of_ms:
            return self._reject(
                decision_id,
                intent,
                portfolio,
                market_snapshot,
                audit,
                RiskRejectionReason.PORTFOLIO_TIME_MISMATCH,
            )
        if (
            not isinstance(reference_price, Decimal)
            or not reference_price.is_finite()
            or reference_price <= ZERO
        ):
            return self._reject(
                decision_id,
                intent,
                portfolio,
                market_snapshot,
                audit,
                RiskRejectionReason.INVALID_REFERENCE_PRICE,
            )
        if rules.round_price_down(reference_price) != reference_price:
            return self._reject(
                decision_id,
                intent,
                portfolio,
                market_snapshot,
                audit,
                RiskRejectionReason.INVALID_REFERENCE_PRICE,
            )
        if portfolio.position is None:
            if portfolio.base_balance != ZERO:
                raise RiskInvariantError("base balance requires a position")
            current_quantity = ZERO
        else:
            if portfolio.position.quantity != portfolio.base_balance:
                raise RiskInvariantError("position quantity must equal base balance")
            current_quantity = portfolio.position.quantity
        expected_equity = _calculate(
            "add",
            portfolio.quote_balance,
            _calculate("multiply", portfolio.base_balance, reference_price),
        )
        if expected_equity != portfolio.current_equity or (
            portfolio.position is not None and portfolio.position.last_mark_price != reference_price
        ):
            return self._reject(
                decision_id,
                intent,
                portfolio,
                market_snapshot,
                audit,
                RiskRejectionReason.PORTFOLIO_VALUATION_MISMATCH,
            )

        if intent.action is OrderIntentAction.EXIT_LONG:
            return self._evaluate_exit(
                decision_id,
                intent,
                portfolio,
                market_snapshot,
                reference_price,
                current_quantity,
            )
        if intent.action is not OrderIntentAction.ENTER_LONG:
            return self._reject(
                decision_id,
                intent,
                portfolio,
                market_snapshot,
                audit,
                RiskRejectionReason.INVALID_ACTION,
            )
        return self._evaluate_enter(
            decision_id,
            intent,
            portfolio,
            market_snapshot,
            reference_price,
            current_quantity,
        )

    def _evaluate_enter(
        self,
        decision_id: str,
        intent: OrderIntent,
        portfolio: PortfolioSnapshot,
        market_snapshot: MarketSnapshot,
        reference_price: Decimal,
        current_quantity: Decimal,
    ) -> RiskDecision:
        if portfolio.current_equity <= ZERO:
            return self._reject_with_values(
                decision_id,
                intent,
                portfolio,
                market_snapshot,
                reference_price,
                current_quantity,
                RiskRejectionReason.NON_POSITIVE_EQUITY,
            )
        modifications: list[RiskModificationReason] = []
        requested_exposure = intent.target_exposure
        capped_exposure = min(requested_exposure, self._policy.max_target_exposure)
        if capped_exposure != requested_exposure:
            modifications.append(RiskModificationReason.TARGET_EXPOSURE_CAPPED)
        requested_target_notional = _calculate(
            "multiply", portfolio.current_equity, requested_exposure
        )
        capped_target_notional = _calculate("multiply", portfolio.current_equity, capped_exposure)
        allowed_target_notional = min(capped_target_notional, self._policy.max_position_notional)
        if allowed_target_notional != capped_target_notional:
            modifications.append(RiskModificationReason.POSITION_NOTIONAL_CAPPED)
        raw_target_quantity = _calculate("divide", allowed_target_notional, reference_price)
        rounded_target_quantity = self._instrument_rules.round_quantity_down(raw_target_quantity)
        if rounded_target_quantity != raw_target_quantity:
            modifications.append(RiskModificationReason.QUANTITY_ROUNDED)
        raw_delta = _calculate("subtract", rounded_target_quantity, current_quantity)
        if rounded_target_quantity == ZERO and raw_target_quantity > ZERO:
            return self._reject_from_sizing(
                decision_id,
                intent,
                portfolio,
                market_snapshot,
                reference_price,
                requested_target_notional,
                capped_target_notional,
                current_quantity,
                raw_target_quantity,
                rounded_target_quantity,
                raw_delta,
                RiskRejectionReason.ROUNDED_QUANTITY_ZERO,
            )
        if raw_delta == ZERO:
            return self._reject_from_sizing(
                decision_id,
                intent,
                portfolio,
                market_snapshot,
                reference_price,
                requested_target_notional,
                capped_target_notional,
                current_quantity,
                raw_target_quantity,
                rounded_target_quantity,
                raw_delta,
                RiskRejectionReason.ALREADY_AT_TARGET,
            )
        if raw_delta < ZERO:
            rejection = (
                RiskRejectionReason.POSITION_LIMIT_UNSATISFIABLE
                if RiskModificationReason.POSITION_NOTIONAL_CAPPED in modifications
                else RiskRejectionReason.ACTION_DELTA_MISMATCH
            )
            return self._reject_from_sizing(
                decision_id,
                intent,
                portfolio,
                market_snapshot,
                reference_price,
                requested_target_notional,
                capped_target_notional,
                current_quantity,
                raw_target_quantity,
                rounded_target_quantity,
                raw_delta,
                rejection,
            )
        quantity = self._instrument_rules.round_quantity_down(raw_delta)
        max_order_quantity = self._instrument_rules.round_quantity_down(
            _calculate("divide", self._policy.max_order_notional, reference_price)
        )
        if quantity > max_order_quantity:
            quantity = max_order_quantity
            modifications.append(RiskModificationReason.ORDER_NOTIONAL_CAPPED)

        available_quote = _calculate(
            "subtract", portfolio.quote_balance, self._policy.min_cash_reserve
        )
        if available_quote <= ZERO:
            reason = (
                RiskRejectionReason.CASH_RESERVE_LIMIT
                if self._policy.min_cash_reserve > ZERO
                else RiskRejectionReason.INSUFFICIENT_QUOTE
            )
            return self._reject_from_sizing(
                decision_id,
                intent,
                portfolio,
                market_snapshot,
                reference_price,
                requested_target_notional,
                capped_target_notional,
                current_quantity,
                raw_target_quantity,
                rounded_target_quantity,
                raw_delta,
                reason,
            )
        worst_price = self._buy_worst_price(reference_price)
        affordable = self._affordable_quantity(available_quote, worst_price)
        if quantity > affordable:
            quantity = affordable
            modifications.append(RiskModificationReason.QUOTE_BUDGET_CAPPED)

        gross = self._instrument_rules.round_quote(
            _calculate("multiply", quantity, reference_price)
        )
        worst_gross = _calculate("multiply", quantity, worst_price)
        worst_fee = _calculate("multiply", worst_gross, self._policy.max_fee_rate)
        required_quote = _calculate("add", worst_gross, worst_fee)
        reason = self._minimum_rejection(quantity, gross)
        if reason is not None:
            return self._reject_from_sizing(
                decision_id,
                intent,
                portfolio,
                market_snapshot,
                reference_price,
                requested_target_notional,
                capped_target_notional,
                current_quantity,
                raw_target_quantity,
                rounded_target_quantity,
                raw_delta,
                reason,
                rounded_order_quantity=quantity,
                approved_order_notional=gross,
                worst_case_price=worst_price,
                worst_case_fee=worst_fee,
                required_quote=required_quote,
            )
        if required_quote > available_quote:
            return self._reject_from_sizing(
                decision_id,
                intent,
                portfolio,
                market_snapshot,
                reference_price,
                requested_target_notional,
                capped_target_notional,
                current_quantity,
                raw_target_quantity,
                rounded_target_quantity,
                raw_delta,
                RiskRejectionReason.INSUFFICIENT_QUOTE,
                rounded_order_quantity=quantity,
                approved_order_notional=gross,
                worst_case_price=worst_price,
                worst_case_fee=worst_fee,
                required_quote=required_quote,
            )

        approved_position_quantity = _calculate("add", current_quantity, quantity)
        approved_target_notional = _calculate(
            "multiply", approved_position_quantity, reference_price
        )
        approved_exposure = _calculate("divide", approved_target_notional, portfolio.current_equity)
        audit = self._audit(
            intent.target_exposure,
            capped_exposure,
            portfolio.current_equity,
            reference_price,
            requested_target_notional,
            capped_target_notional,
            current_quantity,
            raw_target_quantity,
            rounded_target_quantity,
            raw_delta,
            quantity,
            gross,
            approved_exposure,
            worst_price,
            worst_fee,
            required_quote,
            False,
        )
        approved = ApprovedOrderParameters(
            side=RiskOrderSide.BUY,
            quantity=quantity,
            reference_price=reference_price,
            gross_notional=gross,
            approved_target_exposure=approved_exposure,
            worst_case_price=worst_price,
            max_fee_rate=self._policy.max_fee_rate,
            max_slippage_bps=self._policy.max_slippage_bps,
            required_quote=required_quote,
            dust_liquidation=False,
        )
        unique_modifications = _ordered_unique(modifications)
        outcome = (
            RiskDecisionOutcome.MODIFIED if unique_modifications else RiskDecisionOutcome.APPROVED
        )
        return self._decision(
            decision_id,
            intent,
            portfolio,
            market_snapshot,
            outcome,
            approved_exposure,
            approved,
            audit,
            (),
            unique_modifications,
        )

    def _evaluate_exit(
        self,
        decision_id: str,
        intent: OrderIntent,
        portfolio: PortfolioSnapshot,
        market_snapshot: MarketSnapshot,
        reference_price: Decimal,
        current_quantity: Decimal,
    ) -> RiskDecision:
        if portfolio.position is None or current_quantity == ZERO:
            return self._reject_with_values(
                decision_id,
                intent,
                portfolio,
                market_snapshot,
                reference_price,
                current_quantity,
                RiskRejectionReason.NO_POSITION,
            )
        if current_quantity > portfolio.base_balance:
            raise RiskInvariantError("position exceeds base balance")
        if self._instrument_rules.round_quantity_down(current_quantity) != current_quantity:
            raise RiskInvariantError("position quantity is not aligned to quantity_step")
        gross = self._instrument_rules.round_quote(
            _calculate("multiply", current_quantity, reference_price)
        )
        dust = (
            current_quantity < self._instrument_rules.min_quantity
            or gross < self._instrument_rules.min_notional
        )
        modifications = (RiskModificationReason.DUST_LIQUIDATION,) if dust else ()
        audit = self._audit(
            intent.target_exposure,
            ZERO,
            portfolio.current_equity,
            reference_price,
            ZERO,
            ZERO,
            current_quantity,
            ZERO,
            ZERO,
            -current_quantity,
            current_quantity,
            gross,
            ZERO,
            reference_price,
            ZERO,
            ZERO,
            dust,
        )
        approved = ApprovedOrderParameters(
            side=RiskOrderSide.SELL,
            quantity=current_quantity,
            reference_price=reference_price,
            gross_notional=gross,
            approved_target_exposure=ZERO,
            worst_case_price=reference_price,
            max_fee_rate=self._policy.max_fee_rate,
            max_slippage_bps=self._policy.max_slippage_bps,
            required_quote=ZERO,
            dust_liquidation=dust,
        )
        outcome = RiskDecisionOutcome.MODIFIED if dust else RiskDecisionOutcome.APPROVED
        return self._decision(
            decision_id,
            intent,
            portfolio,
            market_snapshot,
            outcome,
            ZERO,
            approved,
            audit,
            (),
            modifications,
        )

    def _buy_worst_price(self, reference_price: Decimal) -> Decimal:
        slippage = _calculate("divide", self._policy.max_slippage_bps, TEN_THOUSAND)
        return _calculate("multiply", reference_price, _calculate("add", ONE, slippage))

    def _affordable_quantity(self, available_quote: Decimal, worst_price: Decimal) -> Decimal:
        fee_multiplier = _calculate("add", ONE, self._policy.max_fee_rate)
        denominator = _calculate("multiply", worst_price, fee_multiplier)
        raw = _calculate("divide", available_quote, denominator)
        return self._instrument_rules.round_quantity_down(raw)

    def _minimum_rejection(self, quantity: Decimal, gross: Decimal) -> RiskRejectionReason | None:
        if quantity == ZERO:
            return RiskRejectionReason.ROUNDED_QUANTITY_ZERO
        if quantity < self._instrument_rules.min_quantity:
            return RiskRejectionReason.BELOW_MIN_QUANTITY
        if gross < self._instrument_rules.min_notional:
            return RiskRejectionReason.BELOW_MIN_NOTIONAL
        return None

    def _empty_audit(
        self,
        requested_exposure: Decimal,
        portfolio: PortfolioSnapshot,
        reference_price: Decimal = ZERO,
    ) -> RiskSizingAudit:
        safe_reference_price = (
            reference_price
            if isinstance(reference_price, Decimal)
            and reference_price.is_finite()
            and reference_price > ZERO
            else ZERO
        )
        return self._audit(
            requested_exposure,
            min(requested_exposure, self._policy.max_target_exposure),
            portfolio.current_equity,
            safe_reference_price,
            ZERO,
            ZERO,
            portfolio.base_balance,
            ZERO,
            ZERO,
            ZERO,
            ZERO,
            ZERO,
            ZERO,
            ZERO,
            ZERO,
            ZERO,
            False,
        )

    def _reject_with_values(
        self,
        decision_id: str,
        intent: OrderIntent,
        portfolio: PortfolioSnapshot,
        market_snapshot: MarketSnapshot,
        reference_price: Decimal,
        current_quantity: Decimal,
        reason: RiskRejectionReason,
    ) -> RiskDecision:
        audit = self._audit(
            intent.target_exposure,
            min(intent.target_exposure, self._policy.max_target_exposure),
            portfolio.current_equity,
            reference_price,
            ZERO,
            ZERO,
            current_quantity,
            ZERO,
            ZERO,
            ZERO,
            ZERO,
            ZERO,
            ZERO,
            ZERO,
            ZERO,
            ZERO,
            False,
        )
        return self._reject(decision_id, intent, portfolio, market_snapshot, audit, reason)

    def _reject_from_sizing(
        self,
        decision_id: str,
        intent: OrderIntent,
        portfolio: PortfolioSnapshot,
        market_snapshot: MarketSnapshot,
        reference_price: Decimal,
        requested_target_notional: Decimal,
        capped_target_notional: Decimal,
        current_quantity: Decimal,
        raw_target_quantity: Decimal,
        rounded_target_quantity: Decimal,
        raw_delta: Decimal,
        reason: RiskRejectionReason,
        *,
        rounded_order_quantity: Decimal = ZERO,
        approved_order_notional: Decimal = ZERO,
        worst_case_price: Decimal = ZERO,
        worst_case_fee: Decimal = ZERO,
        required_quote: Decimal = ZERO,
    ) -> RiskDecision:
        audit = self._audit(
            intent.target_exposure,
            min(intent.target_exposure, self._policy.max_target_exposure),
            portfolio.current_equity,
            reference_price,
            requested_target_notional,
            capped_target_notional,
            current_quantity,
            raw_target_quantity,
            rounded_target_quantity,
            raw_delta,
            rounded_order_quantity,
            approved_order_notional,
            ZERO,
            worst_case_price,
            worst_case_fee,
            required_quote,
            False,
        )
        return self._reject(decision_id, intent, portfolio, market_snapshot, audit, reason)

    def _reject(
        self,
        decision_id: str,
        intent: OrderIntent,
        portfolio: PortfolioSnapshot,
        market_snapshot: MarketSnapshot,
        audit: RiskSizingAudit,
        reason: RiskRejectionReason,
    ) -> RiskDecision:
        return self._decision(
            decision_id,
            intent,
            portfolio,
            market_snapshot,
            RiskDecisionOutcome.REJECTED,
            None,
            None,
            audit,
            (reason,),
            (),
        )

    def _decision(
        self,
        decision_id: str,
        intent: OrderIntent,
        portfolio: PortfolioSnapshot,
        market_snapshot: MarketSnapshot,
        outcome: RiskDecisionOutcome,
        approved_exposure: Decimal | None,
        approved_order: ApprovedOrderParameters | None,
        audit: RiskSizingAudit,
        rejection_reasons: tuple[RiskRejectionReason, ...],
        modification_reasons: tuple[RiskModificationReason, ...],
    ) -> RiskDecision:
        return RiskDecision(
            decision_id=decision_id,
            intent_id=intent.intent_id,
            decided_at_ms=market_snapshot.as_of_ms,
            outcome=outcome,
            requested_target_exposure=intent.target_exposure,
            approved_target_exposure=approved_exposure,
            rejection_reasons=rejection_reasons,
            applied_limits=self._applied_limits(),
            risk_policy_version=self._policy.policy_version,
            correlation_id=intent.correlation_id,
            market_snapshot_id=market_snapshot.snapshot_id,
            portfolio_id=portfolio.portfolio_id,
            portfolio_version=portfolio.version,
            approved_order=approved_order,
            sizing_audit=audit,
            modification_reasons=modification_reasons,
            risk_policy_name=self._policy.policy_name,
        )

    def _applied_limits(self) -> dict[str, str | Decimal]:
        rules = self._instrument_rules
        return {
            "max_target_exposure": self._policy.max_target_exposure,
            "max_order_notional": self._policy.max_order_notional,
            "max_position_notional": self._policy.max_position_notional,
            "min_cash_reserve": self._policy.min_cash_reserve,
            "max_fee_rate": self._policy.max_fee_rate,
            "max_slippage_bps": self._policy.max_slippage_bps,
            "min_quantity": rules.min_quantity,
            "min_notional": rules.min_notional,
            "quantity_step": rules.quantity_step,
            "price_tick": rules.price_tick,
            "rules_version": rules.rules_version,
        }

    def _audit(
        self,
        requested_target_exposure: Decimal,
        capped_target_exposure: Decimal,
        portfolio_equity: Decimal,
        reference_price: Decimal,
        requested_target_notional: Decimal,
        capped_target_notional: Decimal,
        current_position_quantity: Decimal,
        raw_target_quantity: Decimal,
        rounded_target_quantity: Decimal,
        raw_delta_quantity: Decimal,
        rounded_order_quantity: Decimal,
        approved_order_notional: Decimal,
        approved_target_exposure: Decimal,
        worst_case_price: Decimal,
        worst_case_fee: Decimal,
        required_quote: Decimal,
        dust_liquidation: bool,
    ) -> RiskSizingAudit:
        return RiskSizingAudit(
            requested_target_exposure=requested_target_exposure,
            capped_target_exposure=capped_target_exposure,
            portfolio_equity=portfolio_equity,
            reference_price=reference_price,
            requested_target_notional=requested_target_notional,
            capped_target_notional=capped_target_notional,
            current_position_quantity=current_position_quantity,
            current_position_notional=_calculate(
                "multiply", current_position_quantity, reference_price
            ),
            raw_target_quantity=raw_target_quantity,
            rounded_target_quantity=rounded_target_quantity,
            raw_delta_quantity=raw_delta_quantity,
            rounded_order_quantity=rounded_order_quantity,
            approved_order_notional=approved_order_notional,
            approved_target_exposure=approved_target_exposure,
            worst_case_price=worst_case_price,
            worst_case_fee=worst_case_fee,
            required_quote=required_quote,
            dust_liquidation=dust_liquidation,
            instrument_rules_version=self._instrument_rules.rules_version,
        )
