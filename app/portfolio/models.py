from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal, localcontext
from types import MappingProxyType

from app.market.cache import MarketStreamKey
from app.portfolio.exceptions import (
    InvalidInstrumentRulesError,
    PortfolioConfigurationError,
    PortfolioInvariantError,
)
from app.trading.models import ImmutableMapping, freeze_mapping

ZERO = Decimal(0)


def decimal_precision(*values: Decimal, guard_digits: int = 32) -> int:
    """Return operation precision independent of the process Decimal context."""
    magnitude = max(
        (
            len(value.as_tuple().digits)
            + max(0, value.adjusted())
            + max(0, -value.as_tuple().exponent)
            for value in values
        ),
        default=1,
    )
    return max(50, magnitude + guard_digits)


def precise_divide(numerator: Decimal, denominator: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = decimal_precision(numerator, denominator, guard_digits=50)
        return numerator / denominator


def precise_product(left: Decimal, right: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = decimal_precision(left, right)
        return left * right


def precise_add(left: Decimal, right: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = decimal_precision(left, right)
        return left + right


def precise_subtract(left: Decimal, right: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = decimal_precision(left, right)
        return left - right


def _text(value: object, name: str, error_type: type[Exception]) -> str:
    if not isinstance(value, str) or not value.strip():
        raise error_type(f"{name} must be a non-empty string")
    return value


def _decimal(
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


def _timestamp(value: object, name: str, error_type: type[Exception]) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise error_type(f"{name} must be a non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class InstrumentRules:
    stream_key: MarketStreamKey
    base_asset: str
    quote_asset: str
    price_tick: Decimal
    quantity_step: Decimal
    min_quantity: Decimal
    min_notional: Decimal
    base_asset_precision: int
    quote_asset_precision: int
    rules_version: str

    def __post_init__(self) -> None:
        if not isinstance(self.stream_key, MarketStreamKey):
            raise InvalidInstrumentRulesError("stream_key must be a MarketStreamKey")
        _text(self.base_asset, "base_asset", InvalidInstrumentRulesError)
        _text(self.quote_asset, "quote_asset", InvalidInstrumentRulesError)
        if self.base_asset == self.quote_asset:
            raise InvalidInstrumentRulesError("base_asset and quote_asset must differ")
        for name in ("price_tick", "quantity_step", "min_quantity", "min_notional"):
            _decimal(getattr(self, name), name, InvalidInstrumentRulesError, positive=True)
        for name in ("base_asset_precision", "quote_asset_precision"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise InvalidInstrumentRulesError(f"{name} must be a non-negative integer")
        _text(self.rules_version, "rules_version", InvalidInstrumentRulesError)

    @staticmethod
    def _round_step(value: Decimal, step: Decimal, name: str) -> Decimal:
        _decimal(value, name, InvalidInstrumentRulesError, non_negative=True)
        with localcontext() as context:
            context.prec = decimal_precision(value, step)
            units = (value / step).to_integral_value(rounding=ROUND_DOWN)
            return units * step

    def round_price_down(self, price: Decimal) -> Decimal:
        return self._round_step(price, self.price_tick, "price")

    def round_quantity_down(self, quantity: Decimal) -> Decimal:
        return self._round_step(quantity, self.quantity_step, "quantity")

    def round_quote(self, value: Decimal) -> Decimal:
        _decimal(value, "quote value", InvalidInstrumentRulesError, non_negative=True)
        quantum = Decimal(1).scaleb(-self.quote_asset_precision)
        with localcontext() as context:
            context.prec = decimal_precision(value, quantum)
            return value.quantize(quantum, rounding=ROUND_DOWN)


@dataclass(frozen=True, slots=True)
class Position:
    stream_key: MarketStreamKey
    quantity: Decimal
    average_entry_price: Decimal
    cost_basis: Decimal
    realised_pnl: Decimal
    unrealised_pnl: Decimal
    last_mark_price: Decimal
    opened_at_ms: int
    updated_at_ms: int

    def __post_init__(self) -> None:
        if not isinstance(self.stream_key, MarketStreamKey):
            raise PortfolioInvariantError("stream_key must be a MarketStreamKey")
        for name in ("quantity", "average_entry_price", "cost_basis", "last_mark_price"):
            _decimal(getattr(self, name), name, PortfolioInvariantError, positive=True)
        _decimal(self.realised_pnl, "realised_pnl", PortfolioInvariantError)
        _decimal(self.unrealised_pnl, "unrealised_pnl", PortfolioInvariantError)
        _timestamp(self.opened_at_ms, "opened_at_ms", PortfolioInvariantError)
        _timestamp(self.updated_at_ms, "updated_at_ms", PortfolioInvariantError)
        if self.opened_at_ms > self.updated_at_ms:
            raise PortfolioInvariantError("opened_at_ms must not exceed updated_at_ms")


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    portfolio_id: str
    version: int
    stream_key: MarketStreamKey
    as_of_ms: int
    base_asset: str
    quote_asset: str
    base_balance: Decimal
    quote_balance: Decimal
    position: Position | None
    realised_pnl: Decimal
    unrealised_pnl: Decimal
    total_fees: Decimal
    initial_equity: Decimal
    current_equity: Decimal
    peak_equity: Decimal
    drawdown: Decimal
    max_drawdown: Decimal
    turnover: Decimal
    trade_count: int
    applied_fill_count: int
    correlation_id: str | None = None
    metadata: ImmutableMapping = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        _text(self.portfolio_id, "portfolio_id", PortfolioInvariantError)
        if not isinstance(self.stream_key, MarketStreamKey):
            raise PortfolioInvariantError("stream_key must be a MarketStreamKey")
        _text(self.base_asset, "base_asset", PortfolioInvariantError)
        _text(self.quote_asset, "quote_asset", PortfolioInvariantError)
        _timestamp(self.as_of_ms, "as_of_ms", PortfolioInvariantError)
        for name in ("version", "trade_count", "applied_fill_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise PortfolioInvariantError(f"{name} must be a non-negative integer")
        for name in (
            "base_balance",
            "quote_balance",
            "total_fees",
            "initial_equity",
            "current_equity",
            "peak_equity",
            "drawdown",
            "max_drawdown",
            "turnover",
        ):
            _decimal(getattr(self, name), name, PortfolioInvariantError, non_negative=True)
        _decimal(self.realised_pnl, "realised_pnl", PortfolioInvariantError)
        _decimal(self.unrealised_pnl, "unrealised_pnl", PortfolioInvariantError)
        if self.position is not None and not isinstance(self.position, Position):
            raise PortfolioInvariantError("position must be a Position or None")
        if self.position is None and self.base_balance != ZERO:
            raise PortfolioInvariantError("non-zero base balance requires a position")
        if self.position is not None:
            if self.position.stream_key != self.stream_key:
                raise PortfolioInvariantError("position stream must match portfolio stream")
            if self.position.quantity != self.base_balance:
                raise PortfolioInvariantError("position quantity must equal base balance")
            if self.position.unrealised_pnl != self.unrealised_pnl:
                raise PortfolioInvariantError("position and portfolio unrealised PnL must match")
            expected_equity = precise_add(
                self.quote_balance,
                precise_product(self.base_balance, self.position.last_mark_price),
            )
            if self.current_equity != expected_equity:
                raise PortfolioInvariantError("current_equity does not reconcile with balances")
            if self.position.updated_at_ms != self.as_of_ms:
                raise PortfolioInvariantError("position update time must equal snapshot time")
        elif self.current_equity != self.quote_balance:
            raise PortfolioInvariantError("cash-only equity must equal quote balance")
        if self.peak_equity < self.current_equity:
            raise PortfolioInvariantError("peak_equity must not be below current_equity")
        if self.drawdown != precise_subtract(self.peak_equity, self.current_equity):
            raise PortfolioInvariantError("drawdown must equal peak_equity - current_equity")
        if self.max_drawdown < self.drawdown:
            raise PortfolioInvariantError("max_drawdown must not be below drawdown")
        if self.trade_count != self.applied_fill_count:
            raise PortfolioInvariantError("trade and applied fill counters must match in M3.2")
        if self.correlation_id is not None:
            _text(self.correlation_id, "correlation_id", PortfolioInvariantError)
        try:
            object.__setattr__(self, "metadata", freeze_mapping(self.metadata))
        except (TypeError, ValueError) as error:
            raise PortfolioInvariantError("metadata must contain immutable values") from error


@dataclass(frozen=True, slots=True)
class PortfolioConfig:
    portfolio_id: str
    stream_key: MarketStreamKey
    base_asset: str
    quote_asset: str
    initial_base_balance: Decimal
    initial_quote_balance: Decimal
    instrument_rules: InstrumentRules
    applied_fill_id_capacity: int = 10_000

    def __post_init__(self) -> None:
        _text(self.portfolio_id, "portfolio_id", PortfolioConfigurationError)
        if not isinstance(self.stream_key, MarketStreamKey):
            raise PortfolioConfigurationError("stream_key must be a MarketStreamKey")
        _text(self.base_asset, "base_asset", PortfolioConfigurationError)
        _text(self.quote_asset, "quote_asset", PortfolioConfigurationError)
        if self.base_asset == self.quote_asset:
            raise PortfolioConfigurationError("base_asset and quote_asset must differ")
        base = _decimal(
            self.initial_base_balance,
            "initial_base_balance",
            PortfolioConfigurationError,
            non_negative=True,
        )
        _decimal(
            self.initial_quote_balance,
            "initial_quote_balance",
            PortfolioConfigurationError,
            non_negative=True,
        )
        if base != ZERO:
            raise PortfolioConfigurationError("initial_base_balance must be zero in M3.2")
        if not isinstance(self.instrument_rules, InstrumentRules):
            raise PortfolioConfigurationError("instrument_rules must be InstrumentRules")
        rules = self.instrument_rules
        if rules.stream_key != self.stream_key:
            raise PortfolioConfigurationError("instrument rules stream does not match config")
        if rules.base_asset != self.base_asset or rules.quote_asset != self.quote_asset:
            raise PortfolioConfigurationError("instrument rules assets do not match config")
        if (
            isinstance(self.applied_fill_id_capacity, bool)
            or not isinstance(self.applied_fill_id_capacity, int)
            or self.applied_fill_id_capacity <= 0
        ):
            raise PortfolioConfigurationError("applied_fill_id_capacity must be positive")
