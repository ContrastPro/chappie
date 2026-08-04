from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType

from app.market.cache import MarketStreamKey
from app.trading.models import ImmutableMapping, freeze_mapping


class InvalidFillError(ValueError):
    """Raised when an execution fill is structurally invalid."""


class FillSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


def _require_text(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise InvalidFillError(f"{name} must be a non-empty string")


def _require_decimal(value: object, name: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise InvalidFillError(f"{name} must be a finite Decimal")
    if positive and value <= 0:
        raise InvalidFillError(f"{name} must be positive")
    return value


@dataclass(frozen=True, slots=True)
class Fill:
    fill_id: str
    order_id: str
    risk_decision_id: str
    intent_id: str
    stream_key: MarketStreamKey
    side: FillSide
    quantity: Decimal
    price: Decimal
    gross_notional: Decimal
    fee: Decimal
    fee_asset: str
    net_quote_change: Decimal
    net_base_change: Decimal
    filled_at_ms: int
    correlation_id: str | None = None
    metadata: ImmutableMapping = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        for name in ("fill_id", "order_id", "risk_decision_id", "intent_id", "fee_asset"):
            _require_text(getattr(self, name), name)
        if not isinstance(self.stream_key, MarketStreamKey):
            raise InvalidFillError("stream_key must be a MarketStreamKey")
        if not isinstance(self.side, FillSide):
            raise InvalidFillError("side must be a FillSide")
        _require_decimal(self.quantity, "quantity", positive=True)
        _require_decimal(self.price, "price", positive=True)
        _require_decimal(self.gross_notional, "gross_notional", positive=True)
        fee = _require_decimal(self.fee, "fee")
        quote_change = _require_decimal(self.net_quote_change, "net_quote_change")
        base_change = _require_decimal(self.net_base_change, "net_base_change")
        if fee < 0:
            raise InvalidFillError("fee must not be negative")
        if isinstance(self.filled_at_ms, bool) or not isinstance(self.filled_at_ms, int):
            raise InvalidFillError("filled_at_ms must be an integer")
        if self.filled_at_ms < 0:
            raise InvalidFillError("filled_at_ms must not be negative")
        if self.side is FillSide.BUY and not (quote_change < 0 < base_change):
            raise InvalidFillError("BUY requires negative quote and positive base changes")
        if self.side is FillSide.SELL and not (base_change < 0 < quote_change):
            raise InvalidFillError("SELL requires positive quote and negative base changes")
        if self.correlation_id is not None:
            _require_text(self.correlation_id, "correlation_id")
        try:
            object.__setattr__(self, "metadata", freeze_mapping(self.metadata))
        except (TypeError, ValueError) as error:
            raise InvalidFillError("metadata must contain immutable values") from error
