from dataclasses import dataclass
from enum import StrEnum

from app.exchange_execution.models import ExecutionEnvironment, KillSwitchState
from app.exchange_execution.ports import SubmissionCapability


class TestnetReadinessState(StrEnum):
    DISABLED = "DISABLED"
    BLOCKED = "BLOCKED"
    READY = "READY"


class TestnetReadinessReason(StrEnum):
    MODE_DISABLED = "MODE_DISABLED"
    LOCAL_RECOVERY_UNSAFE = "LOCAL_RECOVERY_UNSAFE"
    RECONCILIATION_UNSAFE = "RECONCILIATION_UNSAFE"
    SETTLEMENT_PENDING = "SETTLEMENT_PENDING"
    SETTLEMENT_BLOCKED = "SETTLEMENT_BLOCKED"
    KILL_SWITCH_ACTIVE = "KILL_SWITCH_ACTIVE"
    UNRESOLVED_ORDER = "UNRESOLVED_ORDER"
    SESSION_INVALID = "SESSION_INVALID"


@dataclass(frozen=True, slots=True)
class TestnetExecutionReadiness:
    state: TestnetReadinessState
    reasons: tuple[TestnetReadinessReason, ...]
    reconciliation_generation: int
    control_generation: int
    session_id: str | None
    capability: SubmissionCapability | None = None
    environment: ExecutionEnvironment = ExecutionEnvironment.TESTNET

    def __post_init__(self) -> None:
        if not isinstance(self.state, TestnetReadinessState):
            raise TypeError("state must be TestnetReadinessState")
        if any(not isinstance(value, TestnetReadinessReason) for value in self.reasons):
            raise TypeError("invalid readiness reason")
        if tuple(sorted(set(self.reasons), key=lambda value: value.value)) != self.reasons:
            raise ValueError("readiness reasons must be sorted and unique")
        for value in (self.reconciliation_generation, self.control_generation):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("readiness generations must be non-negative")
        if self.environment is not ExecutionEnvironment.TESTNET:
            raise ValueError("runtime readiness supports TESTNET only")
        if self.state is TestnetReadinessState.READY:
            if self.reasons or not isinstance(self.capability, SubmissionCapability):
                raise ValueError("READY requires a capability and no reasons")
            if self.session_id is None or (
                self.capability.environment is not self.environment
                or self.capability.session_id != self.session_id
                or self.capability.reconciliation_generation != self.reconciliation_generation
                or self.capability.control_generation != self.control_generation
            ):
                raise ValueError("READY capability does not match readiness lineage")
        elif self.capability is not None:
            raise ValueError("only READY may contain a capability")


class ExchangeSettlementStatus(StrEnum):
    SYNCHRONIZED = "SYNCHRONIZED"
    PENDING = "PENDING"
    BLOCKED_UNSUPPORTED_FEE = "BLOCKED_UNSUPPORTED_FEE"


@dataclass(frozen=True, slots=True)
class ExchangeSettlementResult:
    status: ExchangeSettlementStatus
    settled_count: int
    blocked_count: int
    portfolio_version: int
    last_settlement_id: str | None = None
    resolved_count: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.status, ExchangeSettlementStatus):
            raise TypeError("invalid settlement status")
        for name in ("settled_count", "blocked_count", "portfolio_version", "resolved_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be non-negative")


@dataclass(frozen=True, slots=True)
class TestnetRuntimeStatus:
    execution_mode: str
    local_recovery: str
    reconciliation_status: str
    reconciliation_generation: int
    control_generation: int
    kill_switch: KillSwitchState
    settlement_status: ExchangeSettlementStatus
    readiness: TestnetReadinessState
    non_terminal_orders: int
    session_id: str
    submission_count: int
    submitted_notional: str
    executed_turnover: str
