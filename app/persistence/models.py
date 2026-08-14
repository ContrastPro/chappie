import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType

from app.market.models import MarketStreamKey
from app.persistence.canonical import canonical_json, canonical_sha256
from app.persistence.exceptions import (
    PersistenceConfigurationError,
    PersistenceIntegrityError,
)

ZERO = Decimal(0)


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PersistenceIntegrityError(f"{name} must be a non-empty string")
    return value


def _integer(value: object, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise PersistenceIntegrityError(f"{name} must be an integer >= {minimum}")
    return value


def _decimal(value: object, name: str, *, non_negative: bool = False) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise PersistenceIntegrityError(f"{name} must be a finite Decimal")
    if non_negative and value < ZERO:
        raise PersistenceIntegrityError(f"{name} must not be negative")
    return value


def _digest(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise PersistenceIntegrityError(f"{name} must be a lowercase SHA-256 digest")
    return text


def _verify_payload_digest(payload: str, digest: str) -> None:
    expected = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    if digest != expected:
        raise PersistenceIntegrityError("payload_digest does not match payload")


@dataclass(frozen=True, slots=True)
class SQLitePersistenceConfig:
    database_path: str | Path
    busy_timeout_ms: int = 5_000
    checkpoint_interval_transitions: int = 100
    expected_schema_version: int = 1
    exclusive_writer: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.database_path, (str, Path)) or not str(self.database_path):
            raise PersistenceConfigurationError("database_path must be a path")
        if str(self.database_path) != ":memory:":
            path = Path(self.database_path)
            if path.exists() and path.is_dir():
                raise PersistenceConfigurationError("database_path must not be a directory")
        for name in ("busy_timeout_ms", "checkpoint_interval_transitions"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise PersistenceConfigurationError(f"{name} must be positive")
        if self.expected_schema_version not in {1, 2, 3, 4, 5, 6, 7, 8}:
            raise PersistenceConfigurationError("expected_schema_version must be between 1 and 8")
        if not isinstance(self.exclusive_writer, bool):
            raise PersistenceConfigurationError("exclusive_writer must be bool")


@dataclass(frozen=True, slots=True)
class PositionState:
    quantity: Decimal
    cost_basis: Decimal
    average_entry_price: Decimal
    realised_pnl: Decimal
    unrealised_pnl: Decimal
    last_mark_price: Decimal
    opened_at_ms: int
    updated_at_ms: int

    def __post_init__(self) -> None:
        for name in ("quantity", "cost_basis", "average_entry_price", "last_mark_price"):
            if _decimal(getattr(self, name), name) <= ZERO:
                raise PersistenceIntegrityError(f"{name} must be positive")
        _decimal(self.realised_pnl, "realised_pnl")
        _decimal(self.unrealised_pnl, "unrealised_pnl")
        _integer(self.opened_at_ms, "opened_at_ms")
        _integer(self.updated_at_ms, "updated_at_ms")
        if self.opened_at_ms > self.updated_at_ms:
            raise PersistenceIntegrityError("position timestamps are inconsistent")


@dataclass(frozen=True, slots=True)
class PortfolioState:
    portfolio_id: str
    stream_key: MarketStreamKey
    base_asset: str
    quote_asset: str
    quote_balance: Decimal
    base_balance: Decimal
    position: PositionState | None
    realised_pnl: Decimal
    unrealised_pnl: Decimal
    total_fees: Decimal
    initial_equity: Decimal
    turnover: Decimal
    trade_count: int
    applied_fill_count: int
    peak_equity: Decimal
    current_equity: Decimal
    drawdown: Decimal
    max_drawdown: Decimal
    mark_price: Decimal | None
    as_of_ms: int
    version: int
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        _text(self.portfolio_id, "portfolio_id")
        if not isinstance(self.stream_key, MarketStreamKey):
            raise PersistenceIntegrityError("stream_key must be MarketStreamKey")
        _text(self.base_asset, "base_asset")
        _text(self.quote_asset, "quote_asset")
        for name in (
            "quote_balance",
            "base_balance",
            "total_fees",
            "initial_equity",
            "turnover",
            "peak_equity",
            "current_equity",
            "drawdown",
            "max_drawdown",
        ):
            _decimal(getattr(self, name), name, non_negative=True)
        _decimal(self.realised_pnl, "realised_pnl")
        _decimal(self.unrealised_pnl, "unrealised_pnl")
        for name in ("trade_count", "applied_fill_count", "as_of_ms", "version"):
            _integer(getattr(self, name), name)
        if self.trade_count != self.applied_fill_count:
            raise PersistenceIntegrityError("trade_count must equal applied_fill_count")
        if self.position is None:
            if self.base_balance != ZERO:
                raise PersistenceIntegrityError("base balance requires a position")
            if self.current_equity != self.quote_balance:
                raise PersistenceIntegrityError("cash-only equity must equal quote balance")
        else:
            if not isinstance(self.position, PositionState):
                raise PersistenceIntegrityError("position must be PositionState")
            if self.position.quantity != self.base_balance:
                raise PersistenceIntegrityError("position quantity must equal base balance")
            if self.position.unrealised_pnl != self.unrealised_pnl:
                raise PersistenceIntegrityError("position unrealised PnL mismatch")
            if self.mark_price != self.position.last_mark_price:
                raise PersistenceIntegrityError("mark price must equal position mark")
            if self.position.updated_at_ms != self.as_of_ms:
                raise PersistenceIntegrityError("position timestamp mismatch")
            if self.current_equity != self.quote_balance + self.base_balance * self.mark_price:
                raise PersistenceIntegrityError("equity does not reconcile")
        if self.peak_equity < self.current_equity:
            raise PersistenceIntegrityError("peak equity below current equity")
        if self.drawdown != self.peak_equity - self.current_equity:
            raise PersistenceIntegrityError("drawdown does not reconcile")
        if self.max_drawdown < self.drawdown:
            raise PersistenceIntegrityError("max drawdown below drawdown")
        if self.correlation_id is not None:
            _text(self.correlation_id, "correlation_id")

    def payload(self) -> dict[str, object]:
        position = None
        if self.position is not None:
            position = {
                name: getattr(self.position, name) for name in self.position.__dataclass_fields__
            }
        return {
            "portfolio_id": self.portfolio_id,
            "symbol": self.stream_key.symbol,
            "interval": self.stream_key.interval,
            "base_asset": self.base_asset,
            "quote_asset": self.quote_asset,
            "quote_balance": self.quote_balance,
            "base_balance": self.base_balance,
            "position": position,
            "realised_pnl": self.realised_pnl,
            "unrealised_pnl": self.unrealised_pnl,
            "total_fees": self.total_fees,
            "initial_equity": self.initial_equity,
            "turnover": self.turnover,
            "trade_count": self.trade_count,
            "applied_fill_count": self.applied_fill_count,
            "peak_equity": self.peak_equity,
            "current_equity": self.current_equity,
            "drawdown": self.drawdown,
            "max_drawdown": self.max_drawdown,
            "mark_price": self.mark_price,
            "as_of_ms": self.as_of_ms,
            "version": self.version,
            "correlation_id": self.correlation_id,
        }

    @property
    def canonical_payload(self) -> str:
        return canonical_json(self.payload())

    @property
    def digest(self) -> str:
        return canonical_sha256(self.payload(), algorithm_version="portfolio-state/v1")


class CycleOutcome(StrEnum):
    HOLD = "HOLD"
    PLANNER_DECLINED = "PLANNER_DECLINED"
    RISK_REJECTED = "RISK_REJECTED"
    NOT_EXECUTED = "NOT_EXECUTED"
    EXECUTED = "EXECUTED"


@dataclass(frozen=True, slots=True)
class PersistentDecisionRecord:
    stable_decision_key: str
    decision_slot_key: str
    payload: str
    payload_digest: str
    created_at_ms: int

    def __post_init__(self) -> None:
        _digest(self.stable_decision_key, "stable_decision_key")
        _digest(self.decision_slot_key, "decision_slot_key")
        _text(self.payload, "payload")
        _digest(self.payload_digest, "payload_digest")
        _verify_payload_digest(self.payload, self.payload_digest)
        _integer(self.created_at_ms, "created_at_ms")


@dataclass(frozen=True, slots=True)
class PersistentCommandRecord:
    durable_command_key: str
    stable_decision_key: str
    payload: str
    payload_digest: str

    def __post_init__(self) -> None:
        _digest(self.durable_command_key, "durable_command_key")
        _digest(self.stable_decision_key, "stable_decision_key")
        _text(self.payload, "payload")
        _digest(self.payload_digest, "payload_digest")
        _verify_payload_digest(self.payload, self.payload_digest)


@dataclass(frozen=True, slots=True)
class PersistentFillRecord:
    fill_id: str
    durable_command_key: str
    order_id: str
    risk_decision_id: str
    intent_id: str
    stream_key: MarketStreamKey
    side: str
    quantity: Decimal
    price: Decimal
    gross_notional: Decimal
    fee: Decimal
    fee_asset: str
    net_quote_change: Decimal
    net_base_change: Decimal
    filled_at_ms: int
    dust_liquidation: bool = False
    correlation_id: str | None = None
    model_version: str = "fill/v1"

    def __post_init__(self) -> None:
        for name in (
            "fill_id",
            "order_id",
            "risk_decision_id",
            "intent_id",
            "fee_asset",
            "model_version",
        ):
            _text(getattr(self, name), name)
        _digest(self.durable_command_key, "durable_command_key")
        if not isinstance(self.stream_key, MarketStreamKey):
            raise PersistenceIntegrityError("stream_key must be MarketStreamKey")
        if self.side not in {"BUY", "SELL"}:
            raise PersistenceIntegrityError("side must be BUY or SELL")
        for name in ("quantity", "price", "gross_notional"):
            if _decimal(getattr(self, name), name) <= ZERO:
                raise PersistenceIntegrityError(f"{name} must be positive")
        _decimal(self.fee, "fee", non_negative=True)
        _decimal(self.net_quote_change, "net_quote_change")
        _decimal(self.net_base_change, "net_base_change")
        _integer(self.filled_at_ms, "filled_at_ms")
        if not isinstance(self.dust_liquidation, bool):
            raise PersistenceIntegrityError("dust_liquidation must be bool")
        if self.correlation_id is not None:
            _text(self.correlation_id, "correlation_id")


@dataclass(frozen=True, slots=True)
class PortfolioTransitionRecord:
    portfolio_id: str
    transition_type: str
    version_before: int
    version_after: int
    before_digest: str
    after_state: PortfolioState
    domain_timestamp_ms: int
    stable_decision_key: str
    durable_command_key: str | None = None
    fill_id: str | None = None
    model_version: str = "portfolio-transition/v1"

    def __post_init__(self) -> None:
        _text(self.transition_type, "transition_type")
        _digest(self.before_digest, "before_digest")
        _digest(self.stable_decision_key, "stable_decision_key")
        if self.durable_command_key is not None:
            _digest(self.durable_command_key, "durable_command_key")
        _integer(self.domain_timestamp_ms, "domain_timestamp_ms")
        if self.version_after != self.version_before + 1:
            raise PersistenceIntegrityError("transition version must increment by one")
        if self.after_state.version != self.version_after:
            raise PersistenceIntegrityError("after state version mismatch")
        if self.after_state.portfolio_id != self.portfolio_id:
            raise PersistenceIntegrityError("transition portfolio mismatch")


@dataclass(frozen=True, slots=True)
class PortfolioCheckpoint:
    portfolio_id: str
    portfolio_version: int
    state: PortfolioState
    state_digest: str
    last_journal_seq: int
    model_version: str = "portfolio-checkpoint/v1"

    def __post_init__(self) -> None:
        _text(self.portfolio_id, "portfolio_id")
        _integer(self.portfolio_version, "portfolio_version")
        _digest(self.state_digest, "state_digest")
        _integer(self.last_journal_seq, "last_journal_seq", minimum=1)
        if self.state.portfolio_id != self.portfolio_id:
            raise PersistenceIntegrityError("checkpoint portfolio mismatch")
        if self.state.version != self.portfolio_version or self.state.digest != self.state_digest:
            raise PersistenceIntegrityError("checkpoint state mismatch")


@dataclass(frozen=True, slots=True)
class InitialPortfolioRecord:
    state: PortfolioState
    config_fingerprint: str
    created_at_ms: int

    def __post_init__(self) -> None:
        if self.state.version != 0:
            raise PersistenceIntegrityError("initial Portfolio state must have version 0")
        _digest(self.config_fingerprint, "config_fingerprint")
        _integer(self.created_at_ms, "created_at_ms")


@dataclass(frozen=True, slots=True)
class JournalEntry:
    journal_seq: int
    entry_type: str
    portfolio_id: str
    stable_decision_key: str
    portfolio_version_before: int
    portfolio_version_after: int
    payload: str
    payload_digest: str
    domain_timestamp_ms: int
    model_version: str


class RecoveryStatus(StrEnum):
    CLEAN = "CLEAN"
    RECOVERED = "RECOVERED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    status: RecoveryStatus
    portfolio_state: PortfolioState | None
    checkpoint_version: int | None
    last_journal_seq: int
    replayed_transitions: int
    unresolved_conflicts: int = 0
    diagnostics: tuple[str, ...] = ()
    metadata: Mapping[str, str | int | bool | None] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
        if self.status is RecoveryStatus.RECOVERY_REQUIRED and self.portfolio_state is not None:
            raise PersistenceIntegrityError("recovery-required result cannot expose state")


@dataclass(frozen=True, slots=True)
class PersistenceStatus:
    opened: bool
    schema_version: int
    journal_mode: str
    synchronous_mode: int
    foreign_keys: bool
    last_journal_seq: int
    latest_portfolio_version: int | None
    latest_checkpoint_version: int | None
    database_path: str


@dataclass(frozen=True, slots=True)
class DurableDecisionState:
    stable_decision_key: str
    decision_slot_key: str
    terminal_outcome: CycleOutcome
    last_journal_seq: int

    def __post_init__(self) -> None:
        _digest(self.stable_decision_key, "stable_decision_key")
        _digest(self.decision_slot_key, "decision_slot_key")
        if not isinstance(self.terminal_outcome, CycleOutcome):
            raise PersistenceIntegrityError("terminal_outcome must be CycleOutcome")
        _integer(self.last_journal_seq, "last_journal_seq")
