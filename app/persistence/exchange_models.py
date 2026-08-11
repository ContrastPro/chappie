from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from app.exchange_execution.models import (
    ExchangeOrder,
    ExecutionEnvironment,
    ExecutionSession,
    KillSwitch,
    require_account_fingerprint,
    require_hash,
    require_text,
    require_timestamp,
)
from app.market.models import MarketStreamKey
from app.persistence.canonical import canonical_sha256
from app.persistence.exceptions import PersistenceIntegrityError


class LocalExchangeRecoveryStatus(StrEnum):
    CLEAN = "CLEAN"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


class RestartDisposition(StrEnum):
    FIRST_SUBMISSION_ELIGIBLE = "FIRST_SUBMISSION_ELIGIBLE"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    TERMINAL = "TERMINAL"


class TradeSettlementStatus(StrEnum):
    PENDING = "PENDING"
    SETTLED = "SETTLED"
    BLOCKED_UNSUPPORTED_FEE = "BLOCKED_UNSUPPORTED_FEE"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


class TradeCompletenessStatus(StrEnum):
    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"
    CONFLICT = "CONFLICT"


class ExchangeReconciliationStatus(StrEnum):
    REQUIRED = "REQUIRED"
    SAFE = "SAFE"
    FAILED = "FAILED"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True, slots=True)
class ExchangeReconciliationState:
    execution_root_id: str
    status: ExchangeReconciliationStatus
    generation: int
    latest_reconciliation_id: str | None
    updated_at_ms: int
    state_digest: str
    model_version: str = "exchange-reconciliation-state/v1"

    def __post_init__(self) -> None:
        require_text(self.execution_root_id, "execution_root_id")
        if not isinstance(self.status, ExchangeReconciliationStatus):
            raise PersistenceIntegrityError("invalid exchange reconciliation status")
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 0
        ):
            raise PersistenceIntegrityError("reconciliation generation must be non-negative")
        if self.latest_reconciliation_id is not None:
            require_text(self.latest_reconciliation_id, "latest_reconciliation_id")
        require_timestamp(self.updated_at_ms, "updated_at_ms")
        require_hash(self.state_digest, "state_digest")
        if self.model_version != "exchange-reconciliation-state/v1":
            raise PersistenceIntegrityError("unsupported reconciliation state model version")


@dataclass(frozen=True, slots=True)
class PersistentExchangeReconciliationRun:
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
    reasons: tuple[str, ...]
    started_at_ms: int
    completed_at_ms: int
    model_version: str = "exchange-reconciliation-run/v1"

    def __post_init__(self) -> None:
        for name in ("reconciliation_id", "execution_root_id", "session_id"):
            require_text(getattr(self, name), name)
        require_account_fingerprint(self.account_fingerprint)
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
                raise PersistenceIntegrityError(f"{name} must be non-negative")
        if not isinstance(self.outcome, ExchangeReconciliationStatus):
            raise PersistenceIntegrityError("invalid reconciliation outcome")
        expected = self.generation_before + (self.outcome is ExchangeReconciliationStatus.SAFE)
        if self.generation_after != expected:
            raise PersistenceIntegrityError("reconciliation generation transition is invalid")
        if self.completed_at_ms < self.started_at_ms:
            raise PersistenceIntegrityError("reconciliation completion precedes start")
        if not isinstance(self.reasons, tuple) or any(
            not isinstance(value, str) or not value for value in self.reasons
        ):
            raise PersistenceIntegrityError("reconciliation reasons must be typed strings")
        if tuple(sorted(set(self.reasons))) != self.reasons:
            raise PersistenceIntegrityError("reconciliation reasons must be sorted and unique")
        if self.model_version != "exchange-reconciliation-run/v1":
            raise PersistenceIntegrityError("unsupported reconciliation run model version")


@dataclass(frozen=True, slots=True)
class ExchangeExecutionRoot:
    execution_root_id: str
    account_fingerprint: str
    stream_key: MarketStreamKey
    configuration_fingerprint: str
    created_at_ms: int
    venue: str = "binance-spot"
    environment: ExecutionEnvironment = ExecutionEnvironment.TESTNET
    model_version: str = "exchange-execution-root/v1"

    def __post_init__(self) -> None:
        require_text(self.execution_root_id, "execution_root_id")
        require_account_fingerprint(self.account_fingerprint)
        if not isinstance(self.stream_key, MarketStreamKey) or self.stream_key.symbol != "BTCUSDT":
            raise PersistenceIntegrityError("execution root stream must identify BTCUSDT")
        require_hash(self.configuration_fingerprint, "configuration_fingerprint")
        require_timestamp(self.created_at_ms, "created_at_ms")
        if self.venue != "binance-spot" or self.environment is not ExecutionEnvironment.TESTNET:
            raise PersistenceIntegrityError("execution root must be Binance Spot Testnet")
        require_text(self.model_version, "model_version")

    def payload(self) -> dict[str, object]:
        return {
            "execution_root_id": self.execution_root_id,
            "venue": self.venue,
            "environment": self.environment,
            "account_fingerprint": self.account_fingerprint,
            "symbol": self.stream_key.symbol,
            "interval": self.stream_key.interval,
            "configuration_fingerprint": self.configuration_fingerprint,
            "created_at_ms": self.created_at_ms,
            "model_version": self.model_version,
        }

    @property
    def digest(self) -> str:
        return canonical_sha256(self.payload(), algorithm_version=self.model_version)


@dataclass(frozen=True, slots=True)
class DurableExchangeOrder:
    order: ExchangeOrder
    stable_decision_key: str
    execution_root_id: str
    session_id: str
    approved_notional: Decimal
    command_payload: str
    state_digest: str
    version: int

    def __post_init__(self) -> None:
        if not isinstance(self.order, ExchangeOrder):
            raise PersistenceIntegrityError("order must be ExchangeOrder")
        require_hash(self.stable_decision_key, "stable_decision_key")
        require_text(self.execution_root_id, "execution_root_id")
        require_text(self.session_id, "session_id")
        if (
            not isinstance(self.approved_notional, Decimal)
            or not self.approved_notional.is_finite()
            or self.approved_notional <= 0
        ):
            raise PersistenceIntegrityError("approved_notional must be a positive Decimal")
        require_text(self.command_payload, "command_payload")
        require_hash(self.state_digest, "state_digest")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise PersistenceIntegrityError("version must be positive")


@dataclass(frozen=True, slots=True)
class ExchangeControlState:
    execution_root_id: str
    kill_switch: KillSwitch
    reconciliation_generation: int
    audit_identifier: str

    def __post_init__(self) -> None:
        require_text(self.execution_root_id, "execution_root_id")
        if not isinstance(self.kill_switch, KillSwitch):
            raise PersistenceIntegrityError("kill_switch must be KillSwitch")
        if (
            isinstance(self.reconciliation_generation, bool)
            or not isinstance(self.reconciliation_generation, int)
            or self.reconciliation_generation < 0
        ):
            raise PersistenceIntegrityError("reconciliation_generation must be non-negative")
        require_text(self.audit_identifier, "audit_identifier")


@dataclass(frozen=True, slots=True)
class LocalExchangeRecoveryResult:
    status: LocalExchangeRecoveryStatus
    prepared: tuple[DurableExchangeOrder, ...]
    reconciliation_required: tuple[DurableExchangeOrder, ...]
    terminal: tuple[DurableExchangeOrder, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.status, LocalExchangeRecoveryStatus):
            raise PersistenceIntegrityError("invalid local exchange recovery status")


@dataclass(frozen=True, slots=True)
class ExecutionBootstrap:
    root: ExchangeExecutionRoot
    session: ExecutionSession
    control: ExchangeControlState
