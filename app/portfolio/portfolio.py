from collections import OrderedDict
from dataclasses import replace
from decimal import Decimal

from app.execution.models import Fill, FillSide
from app.portfolio.exceptions import (
    DuplicateFillConflictError,
    FillArithmeticMismatchError,
    FillQuantityExceedsPositionError,
    FillRuleViolationError,
    InsufficientBaseBalanceError,
    InsufficientQuoteBalanceError,
    InvalidPortfolioMarkPriceError,
    InvalidPortfolioTimestampError,
    PortfolioAssetMismatchError,
    PortfolioInvariantError,
    PortfolioStreamMismatchError,
    PositionRequiredError,
)
from app.portfolio.models import (
    ZERO,
    PortfolioConfig,
    PortfolioSnapshot,
    Position,
    precise_add,
    precise_divide,
    precise_product,
    precise_subtract,
)


class Portfolio:
    """Single-stream, single-writer in-memory portfolio aggregate.

    This aggregate is intentionally not thread-safe and performs no I/O.
    """

    def __init__(self, config: PortfolioConfig) -> None:
        if not isinstance(config, PortfolioConfig):
            raise PortfolioInvariantError("config must be a PortfolioConfig")
        self._config = config
        self._applied_fills: OrderedDict[str, Fill] = OrderedDict()
        equity = config.initial_quote_balance
        self._snapshot = PortfolioSnapshot(
            portfolio_id=config.portfolio_id,
            version=0,
            stream_key=config.stream_key,
            as_of_ms=0,
            base_asset=config.base_asset,
            quote_asset=config.quote_asset,
            base_balance=ZERO,
            quote_balance=config.initial_quote_balance,
            position=None,
            realised_pnl=ZERO,
            unrealised_pnl=ZERO,
            total_fees=ZERO,
            initial_equity=equity,
            current_equity=equity,
            peak_equity=equity,
            drawdown=ZERO,
            max_drawdown=ZERO,
            turnover=ZERO,
            trade_count=0,
            applied_fill_count=0,
        )

    @property
    def snapshot(self) -> PortfolioSnapshot:
        return replace(self._snapshot)

    @staticmethod
    def _fill_context(fill: Fill, reason: str, **values: object) -> str:
        details = "; ".join(f"{name}={value}" for name, value in values.items())
        suffix = f"; {details}" if details else ""
        return (
            f"{reason}; fill_id={fill.fill_id}; "
            f"stream={fill.stream_key.symbol}:{fill.stream_key.interval}{suffix}"
        )

    def apply_fill(self, fill: Fill, mark_price: Decimal) -> PortfolioSnapshot:
        if not isinstance(fill, Fill):
            raise PortfolioInvariantError("fill must be a Fill")
        existing = self._applied_fills.get(fill.fill_id)
        if existing is not None:
            if existing == fill:
                return self.snapshot
            raise DuplicateFillConflictError(
                self._fill_context(fill, "duplicate fill has conflicting data")
            )

        mark = self._validate_mark(mark_price)
        current = self._snapshot
        if fill.filled_at_ms < current.as_of_ms:
            raise InvalidPortfolioTimestampError(
                self._fill_context(
                    fill,
                    "fill timestamp regresses portfolio time",
                    expected_min=current.as_of_ms,
                    received=fill.filled_at_ms,
                )
            )
        self._validate_fill(fill)

        if fill.side is FillSide.BUY:
            staged = self._apply_buy(current, fill, mark)
        else:
            staged = self._apply_sell(current, fill, mark)

        staged_registry = self._applied_fills.copy()
        staged_registry[fill.fill_id] = fill
        if len(staged_registry) > self._config.applied_fill_id_capacity:
            staged_registry.popitem(last=False)
        self._snapshot = staged
        self._applied_fills = staged_registry
        return self.snapshot

    def mark_to_market(
        self,
        mark_price: Decimal,
        as_of_ms: int,
        correlation_id: str | None = None,
    ) -> PortfolioSnapshot:
        mark = self._validate_mark(mark_price)
        if isinstance(as_of_ms, bool) or not isinstance(as_of_ms, int) or as_of_ms < 0:
            raise InvalidPortfolioTimestampError("as_of_ms must be a non-negative integer")
        current = self._snapshot
        if as_of_ms < current.as_of_ms:
            raise InvalidPortfolioTimestampError("mark timestamp regresses portfolio time")
        if correlation_id is not None and (
            not isinstance(correlation_id, str) or not correlation_id.strip()
        ):
            raise PortfolioInvariantError("correlation_id must be a non-empty string")
        if current.position is None and current.base_balance != ZERO:
            raise PortfolioInvariantError("base balance requires a position")
        if current.position is not None and current.base_balance != current.position.quantity:
            raise PortfolioInvariantError("position quantity must equal base balance")
        if (
            current.position is not None
            and current.position.last_mark_price == mark
            and current.as_of_ms == as_of_ms
            and current.correlation_id == correlation_id
        ) or (
            current.position is None
            and current.as_of_ms == as_of_ms
            and current.correlation_id == correlation_id
        ):
            return self.snapshot
        staged = self._mark_snapshot(
            current,
            mark,
            as_of_ms,
            correlation_id,
            version=current.version + 1,
        )
        self._snapshot = staged
        return self.snapshot

    def _validate_mark(self, value: object) -> Decimal:
        if not isinstance(value, Decimal) or not value.is_finite() or value <= ZERO:
            raise InvalidPortfolioMarkPriceError("mark_price must be a positive finite Decimal")
        return value

    def _validate_fill(self, fill: Fill) -> None:
        rules = self._config.instrument_rules
        if fill.stream_key != self._config.stream_key:
            raise PortfolioStreamMismatchError(
                self._fill_context(
                    fill,
                    "fill stream does not match portfolio",
                    expected=self._config.stream_key,
                    received=fill.stream_key,
                )
            )
        if fill.fee_asset != self._config.quote_asset:
            raise PortfolioAssetMismatchError(
                self._fill_context(
                    fill,
                    "fill fee asset must equal quote asset",
                    expected=self._config.quote_asset,
                    received=fill.fee_asset,
                )
            )
        if rules.round_quantity_down(fill.quantity) != fill.quantity:
            raise FillRuleViolationError(
                self._fill_context(
                    fill,
                    "fill quantity is not aligned to quantity_step",
                    expected_step=rules.quantity_step,
                    received=fill.quantity,
                )
            )
        if rules.round_price_down(fill.price) != fill.price:
            raise FillRuleViolationError(
                self._fill_context(
                    fill,
                    "fill price is not aligned to price_tick",
                    expected_tick=rules.price_tick,
                    received=fill.price,
                )
            )
        if fill.quantity < rules.min_quantity:
            raise FillRuleViolationError(
                self._fill_context(
                    fill,
                    "fill quantity is below min_quantity",
                    expected_min=rules.min_quantity,
                    received=fill.quantity,
                )
            )
        expected_gross = rules.round_quote(precise_product(fill.quantity, fill.price))
        if expected_gross < rules.min_notional:
            raise FillRuleViolationError(
                self._fill_context(
                    fill,
                    "fill notional is below min_notional",
                    expected_min=rules.min_notional,
                    received=expected_gross,
                )
            )
        if fill.gross_notional != expected_gross:
            raise FillArithmeticMismatchError(
                self._fill_context(
                    fill,
                    "gross_notional does not match quantity * price",
                    expected=expected_gross,
                    received=fill.gross_notional,
                )
            )
        if rules.round_quote(fill.gross_notional) != fill.gross_notional:
            raise FillArithmeticMismatchError("gross_notional violates quote precision")
        if rules.round_quote(fill.fee) != fill.fee:
            raise FillArithmeticMismatchError("fee violates quote precision")
        if fill.side is FillSide.BUY:
            if fill.net_base_change != fill.quantity:
                raise FillArithmeticMismatchError("BUY base change must equal quantity")
            if fill.net_quote_change != -precise_add(fill.gross_notional, fill.fee):
                raise FillArithmeticMismatchError("BUY quote change is inconsistent")
        else:
            if fill.net_base_change != -fill.quantity:
                raise FillArithmeticMismatchError("SELL base change must equal negative quantity")
            if fill.net_quote_change != precise_subtract(fill.gross_notional, fill.fee):
                raise FillArithmeticMismatchError("SELL quote change is inconsistent")

    def _apply_buy(
        self, current: PortfolioSnapshot, fill: Fill, mark: Decimal
    ) -> PortfolioSnapshot:
        quote = precise_add(current.quote_balance, fill.net_quote_change)
        if quote < ZERO:
            raise InsufficientQuoteBalanceError(
                self._fill_context(
                    fill,
                    "BUY exceeds available quote balance",
                    available=current.quote_balance,
                    required=-fill.net_quote_change,
                )
            )
        base = precise_add(current.base_balance, fill.net_base_change)
        cost = precise_add(fill.gross_notional, fill.fee)
        old = current.position
        quantity = fill.quantity if old is None else precise_add(old.quantity, fill.quantity)
        cost_basis = cost if old is None else precise_add(old.cost_basis, cost)
        average = precise_divide(cost_basis, quantity)
        unrealised = precise_subtract(precise_product(quantity, mark), cost_basis)
        position = Position(
            stream_key=current.stream_key,
            quantity=quantity,
            average_entry_price=average,
            cost_basis=cost_basis,
            realised_pnl=ZERO if old is None else old.realised_pnl,
            unrealised_pnl=unrealised,
            last_mark_price=mark,
            opened_at_ms=fill.filled_at_ms if old is None else old.opened_at_ms,
            updated_at_ms=fill.filled_at_ms,
        )
        equity = precise_add(quote, precise_product(base, mark))
        peak = max(current.peak_equity, equity)
        drawdown = precise_subtract(peak, equity)
        return replace(
            current,
            version=current.version + 1,
            as_of_ms=fill.filled_at_ms,
            base_balance=base,
            quote_balance=quote,
            position=position,
            unrealised_pnl=unrealised,
            total_fees=precise_add(current.total_fees, fill.fee),
            current_equity=equity,
            peak_equity=peak,
            drawdown=drawdown,
            max_drawdown=max(current.max_drawdown, drawdown),
            turnover=precise_add(current.turnover, fill.gross_notional),
            trade_count=current.trade_count + 1,
            applied_fill_count=current.applied_fill_count + 1,
            correlation_id=fill.correlation_id,
        )

    def _apply_sell(
        self, current: PortfolioSnapshot, fill: Fill, mark: Decimal
    ) -> PortfolioSnapshot:
        old = current.position
        if old is None:
            raise PositionRequiredError(self._fill_context(fill, "SELL requires an open position"))
        if fill.quantity > old.quantity:
            raise FillQuantityExceedsPositionError(
                self._fill_context(
                    fill,
                    "SELL quantity exceeds position",
                    available=old.quantity,
                    received=fill.quantity,
                )
            )
        if fill.quantity > current.base_balance:
            raise InsufficientBaseBalanceError("SELL exceeds available base balance")
        base = precise_add(current.base_balance, fill.net_base_change)
        quote = precise_add(current.quote_balance, fill.net_quote_change)
        if base < ZERO:
            raise InsufficientBaseBalanceError("SELL would create negative base balance")
        full_exit = fill.quantity == old.quantity
        removed_cost = (
            old.cost_basis if full_exit else precise_product(old.average_entry_price, fill.quantity)
        )
        realised_increment = precise_subtract(
            precise_subtract(fill.gross_notional, fill.fee), removed_cost
        )
        realised = precise_add(current.realised_pnl, realised_increment)
        remaining = precise_subtract(old.quantity, fill.quantity)
        if full_exit:
            position = None
            unrealised = ZERO
        else:
            cost_basis = precise_subtract(old.cost_basis, removed_cost)
            if cost_basis < ZERO:
                raise PortfolioInvariantError("remaining cost basis must not be negative")
            unrealised = precise_subtract(precise_product(remaining, mark), cost_basis)
            position = Position(
                stream_key=current.stream_key,
                quantity=remaining,
                average_entry_price=old.average_entry_price,
                cost_basis=cost_basis,
                realised_pnl=precise_add(old.realised_pnl, realised_increment),
                unrealised_pnl=unrealised,
                last_mark_price=mark,
                opened_at_ms=old.opened_at_ms,
                updated_at_ms=fill.filled_at_ms,
            )
        equity = precise_add(quote, precise_product(base, mark))
        peak = max(current.peak_equity, equity)
        drawdown = precise_subtract(peak, equity)
        return replace(
            current,
            version=current.version + 1,
            as_of_ms=fill.filled_at_ms,
            base_balance=base,
            quote_balance=quote,
            position=position,
            realised_pnl=realised,
            unrealised_pnl=unrealised,
            total_fees=precise_add(current.total_fees, fill.fee),
            current_equity=equity,
            peak_equity=peak,
            drawdown=drawdown,
            max_drawdown=max(current.max_drawdown, drawdown),
            turnover=precise_add(current.turnover, fill.gross_notional),
            trade_count=current.trade_count + 1,
            applied_fill_count=current.applied_fill_count + 1,
            correlation_id=fill.correlation_id,
        )

    @staticmethod
    def _mark_snapshot(
        current: PortfolioSnapshot,
        mark: Decimal,
        as_of_ms: int,
        correlation_id: str | None,
        *,
        version: int | None = None,
    ) -> PortfolioSnapshot:
        position = current.position
        unrealised = ZERO
        if position is not None:
            unrealised = precise_subtract(
                precise_product(position.quantity, mark), position.cost_basis
            )
            position = replace(
                position,
                unrealised_pnl=unrealised,
                last_mark_price=mark,
                updated_at_ms=as_of_ms,
            )
        equity = precise_add(current.quote_balance, precise_product(current.base_balance, mark))
        peak = max(current.peak_equity, equity)
        drawdown = precise_subtract(peak, equity)
        return replace(
            current,
            version=current.version if version is None else version,
            as_of_ms=as_of_ms,
            position=position,
            unrealised_pnl=unrealised,
            current_equity=equity,
            peak_equity=peak,
            drawdown=drawdown,
            max_drawdown=max(current.max_drawdown, drawdown),
            correlation_id=correlation_id,
        )
