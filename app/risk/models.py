from dataclasses import dataclass
from decimal import Decimal, localcontext
from enum import StrEnum
from typing import Protocol, runtime_checkable
from uuid import uuid4

from app.risk.exceptions import (
    RiskConfigurationError,
    RiskDecisionFactoryError,
    RiskModelValidationError,
)

ZERO = Decimal(0)
ONE = Decimal(1)


def precise_product(left: Decimal, right: Decimal) -> Decimal:
    digits = max(
        len(left.as_tuple().digits) + abs(left.as_tuple().exponent),
        len(right.as_tuple().digits) + abs(right.as_tuple().exponent),
    )
    with localcontext() as context:
        context.prec = max(50, digits * 2 + 16)
        return left * right


def require_text(value: object, name: str, error_type: type[Exception]) -> str:
    if not isinstance(value, str) or not value.strip():
        raise error_type(f"{name} must be a non-empty string")
    return value


def require_decimal(
    value: object,
    name: str,
    error_type: type[Exception],
    *,
    non_negative: bool = False,
    positive: bool = False,
) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise error_type(f"{name} must be a finite Decimal")
    if positive and value <= ZERO:
        raise error_type(f"{name} must be positive")
    if non_negative and value < ZERO:
        raise error_type(f"{name} must not be negative")
    return value


class RiskOrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class RiskRejectionReason(StrEnum):
    STALE_MARKET = "stale_market"
    NO_CLOSED_CANDLE = "no_closed_candle"
    STREAM_MISMATCH = "stream_mismatch"
    PORTFOLIO_STREAM_MISMATCH = "portfolio_stream_mismatch"
    PORTFOLIO_TIME_MISMATCH = "portfolio_time_mismatch"
    PORTFOLIO_VALUATION_MISMATCH = "portfolio_valuation_mismatch"
    INTENT_TIME_MISMATCH = "intent_time_mismatch"
    INVALID_ACTION = "invalid_action"
    INVALID_TARGET_EXPOSURE = "invalid_target_exposure"
    INVALID_REFERENCE_PRICE = "invalid_reference_price"
    NON_POSITIVE_EQUITY = "non_positive_equity"
    ALREADY_AT_TARGET = "already_at_target"
    ACTION_DELTA_MISMATCH = "action_delta_mismatch"
    ROUNDED_QUANTITY_ZERO = "rounded_quantity_zero"
    BELOW_MIN_QUANTITY = "below_min_quantity"
    BELOW_MIN_NOTIONAL = "below_min_notional"
    INSUFFICIENT_QUOTE = "insufficient_quote"
    INSUFFICIENT_BASE = "insufficient_base"
    NO_POSITION = "no_position"
    POSITION_LIMIT_UNSATISFIABLE = "position_limit_unsatisfiable"
    CASH_RESERVE_LIMIT = "cash_reserve_limit"


class RiskModificationReason(StrEnum):
    TARGET_EXPOSURE_CAPPED = "target_exposure_capped"
    ORDER_NOTIONAL_CAPPED = "order_notional_capped"
    POSITION_NOTIONAL_CAPPED = "position_notional_capped"
    QUANTITY_ROUNDED = "quantity_rounded"
    QUOTE_BUDGET_CAPPED = "quote_budget_capped"
    FULL_EXIT = "full_exit"
    DUST_LIQUIDATION = "dust_liquidation"


@dataclass(frozen=True, slots=True)
class PortfolioRiskPolicy:
    max_order_notional: Decimal
    max_position_notional: Decimal
    policy_name: str = "long_only_spot"
    policy_version: str = "1.0.0"
    max_target_exposure: Decimal = ONE
    min_cash_reserve: Decimal = ZERO
    max_fee_rate: Decimal = Decimal("0.001")
    max_slippage_bps: Decimal = Decimal(5)
    reject_stale_market: bool = True

    def __post_init__(self) -> None:
        require_text(self.policy_name, "policy_name", RiskConfigurationError)
        require_text(self.policy_version, "policy_version", RiskConfigurationError)
        exposure = require_decimal(
            self.max_target_exposure,
            "max_target_exposure",
            RiskConfigurationError,
            positive=True,
        )
        if exposure > ONE:
            raise RiskConfigurationError("max_target_exposure must not exceed 1")
        order = require_decimal(
            self.max_order_notional,
            "max_order_notional",
            RiskConfigurationError,
            positive=True,
        )
        position = require_decimal(
            self.max_position_notional,
            "max_position_notional",
            RiskConfigurationError,
            positive=True,
        )
        if position < order:
            raise RiskConfigurationError(
                "max_position_notional must not be below max_order_notional"
            )
        require_decimal(
            self.min_cash_reserve,
            "min_cash_reserve",
            RiskConfigurationError,
            non_negative=True,
        )
        fee = require_decimal(
            self.max_fee_rate,
            "max_fee_rate",
            RiskConfigurationError,
            non_negative=True,
        )
        if fee >= ONE:
            raise RiskConfigurationError("max_fee_rate must be below 1")
        require_decimal(
            self.max_slippage_bps,
            "max_slippage_bps",
            RiskConfigurationError,
            non_negative=True,
        )
        if not isinstance(self.reject_stale_market, bool):
            raise RiskConfigurationError("reject_stale_market must be a bool")


@dataclass(frozen=True, slots=True)
class RiskSizingAudit:
    requested_target_exposure: Decimal
    capped_target_exposure: Decimal
    portfolio_equity: Decimal
    reference_price: Decimal
    requested_target_notional: Decimal
    capped_target_notional: Decimal
    current_position_quantity: Decimal
    current_position_notional: Decimal
    raw_target_quantity: Decimal
    rounded_target_quantity: Decimal
    raw_delta_quantity: Decimal
    rounded_order_quantity: Decimal
    approved_order_notional: Decimal
    approved_target_exposure: Decimal
    worst_case_price: Decimal
    worst_case_fee: Decimal
    required_quote: Decimal
    dust_liquidation: bool
    instrument_rules_version: str

    def __post_init__(self) -> None:
        for name in (
            "requested_target_exposure",
            "capped_target_exposure",
            "portfolio_equity",
            "reference_price",
            "requested_target_notional",
            "capped_target_notional",
            "current_position_quantity",
            "current_position_notional",
            "raw_target_quantity",
            "rounded_target_quantity",
            "rounded_order_quantity",
            "approved_order_notional",
            "approved_target_exposure",
            "worst_case_price",
            "worst_case_fee",
            "required_quote",
        ):
            require_decimal(getattr(self, name), name, RiskModelValidationError, non_negative=True)
        require_decimal(self.raw_delta_quantity, "raw_delta_quantity", RiskModelValidationError)
        if self.capped_target_exposure > self.requested_target_exposure:
            raise RiskModelValidationError("capped exposure must not exceed requested exposure")
        if self.capped_target_notional > self.requested_target_notional:
            raise RiskModelValidationError("capped notional must not exceed requested notional")
        expected_current_notional = precise_product(
            self.current_position_quantity, self.reference_price
        )
        if self.current_position_notional != expected_current_notional:
            raise RiskModelValidationError("current position notional is inconsistent")
        if self.rounded_order_quantity == ZERO and self.approved_order_notional != ZERO:
            raise RiskModelValidationError("zero order quantity requires zero order notional")
        if self.rounded_order_quantity > ZERO:
            if self.reference_price <= ZERO:
                raise RiskModelValidationError("positive order requires reference price")
            if self.portfolio_equity <= ZERO:
                raise RiskModelValidationError("approved order requires positive equity")
            if self.worst_case_price < self.reference_price:
                raise RiskModelValidationError("worst-case price must not be below reference price")
            if self.required_quote < self.worst_case_fee:
                raise RiskModelValidationError("required quote must include worst-case fee")
        if not isinstance(self.dust_liquidation, bool):
            raise RiskModelValidationError("dust_liquidation must be a bool")
        require_text(
            self.instrument_rules_version,
            "instrument_rules_version",
            RiskModelValidationError,
        )


@dataclass(frozen=True, slots=True)
class ApprovedOrderParameters:
    side: RiskOrderSide
    quantity: Decimal
    reference_price: Decimal
    gross_notional: Decimal
    approved_target_exposure: Decimal
    worst_case_price: Decimal
    max_fee_rate: Decimal
    max_slippage_bps: Decimal
    required_quote: Decimal
    dust_liquidation: bool

    def __post_init__(self) -> None:
        if not isinstance(self.side, RiskOrderSide):
            raise RiskModelValidationError("side must be a RiskOrderSide")
        for name in ("quantity", "reference_price", "gross_notional", "worst_case_price"):
            require_decimal(getattr(self, name), name, RiskModelValidationError, positive=True)
        for name in (
            "approved_target_exposure",
            "max_fee_rate",
            "max_slippage_bps",
            "required_quote",
        ):
            require_decimal(getattr(self, name), name, RiskModelValidationError, non_negative=True)
        if self.approved_target_exposure > ONE:
            raise RiskModelValidationError("approved_target_exposure must not exceed 1")
        if self.max_fee_rate >= ONE:
            raise RiskModelValidationError("max_fee_rate must be below 1")
        if not isinstance(self.dust_liquidation, bool):
            raise RiskModelValidationError("dust_liquidation must be a bool")
        if self.side is RiskOrderSide.BUY:
            if self.dust_liquidation:
                raise RiskModelValidationError("BUY cannot be a dust liquidation")
            if self.worst_case_price < self.reference_price:
                raise RiskModelValidationError("BUY worst-case price must not be below reference")
            if self.required_quote < self.gross_notional:
                raise RiskModelValidationError("BUY required quote must cover gross notional")
        else:
            if self.approved_target_exposure != ZERO:
                raise RiskModelValidationError("SELL requires zero approved target exposure")
            if self.required_quote != ZERO:
                raise RiskModelValidationError("SELL must not contain BUY quote budget")
            if self.worst_case_price != self.reference_price:
                raise RiskModelValidationError("SELL worst-case price must equal reference price")


@runtime_checkable
class RiskDecisionIdFactory(Protocol):
    def new_risk_decision_id(self) -> str: ...


class UUIDRiskDecisionIdFactory:
    def new_risk_decision_id(self) -> str:
        return str(uuid4())


def create_decision_id(factory: RiskDecisionIdFactory) -> str:
    if not isinstance(factory, RiskDecisionIdFactory):
        raise RiskDecisionFactoryError("decision_id_factory does not implement the protocol")
    try:
        value = factory.new_risk_decision_id()
    except Exception as error:
        raise RiskDecisionFactoryError("risk decision ID factory failed") from error
    if not isinstance(value, str) or not value.strip():
        raise RiskDecisionFactoryError("risk decision ID factory returned an invalid ID")
    return value
