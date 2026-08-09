from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType

from app.execution.models import PaperExecutionResult, PaperExecutionStatus
from app.paper.exceptions import PaperTradingConfigurationError, PaperTradingInvariantError
from app.portfolio.models import PortfolioSnapshot
from app.trading.models import (
    ImmutableMapping,
    OrderIntentAction,
    RiskDecision,
    RiskDecisionOutcome,
    SignalDirection,
    freeze_mapping,
)
from app.trading.pipeline import DecisionPipelineResult


class PaperTradingCycleOutcome(StrEnum):
    NO_INTENT = "no_intent"
    PLANNER_DECLINED = "planner_declined"
    RISK_REJECTED = "risk_rejected"
    NOT_EXECUTED = "not_executed"
    EXECUTED = "executed"
    DURABLE_REPLAY = "durable_replay"


class PaperTradingStage(StrEnum):
    DECISION_RECEIVED = "decision_received"
    DECISION_VALIDATED = "decision_validated"
    PORTFOLIO_VALUED = "portfolio_valued"
    NO_INTENT = "no_intent"
    PLANNER_DECLINED = "planner_declined"
    RISK_EVALUATED = "risk_evaluated"
    RISK_REJECTED = "risk_rejected"
    EXECUTION_COMPLETED = "execution_completed"
    NO_FILL = "no_fill"
    PORTFOLIO_UPDATED = "portfolio_updated"
    DURABLE_REPLAY = "durable_replay"


class PaperTradingObservationStatus(StrEnum):
    OBSERVED = "observed"
    OBSERVATION_FAILED = "observation_failed"
    NOT_ATTEMPTED = "not_attempted"


class PaperTradingEngineActivity(StrEnum):
    IDLE = "idle"
    PROCESSING = "processing"


class PaperTradingEngineHealth(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"


class PaperTradingFailureCategory(StrEnum):
    DECISION_VALIDATION = "decision_validation"
    PORTFOLIO_VALUATION = "portfolio_valuation"
    RISK_EVALUATION = "risk_evaluation"
    EXECUTION = "execution"
    PORTFOLIO_APPLY = "portfolio_apply"
    OBSERVER = "observer"
    INTERNAL_ERROR = "internal_error"


def _text(value: object, name: str, error_type: type[Exception]) -> None:
    if not isinstance(value, str) or not value.strip():
        raise error_type(f"{name} must be a non-empty string")


def _duration(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PaperTradingInvariantError(f"{name} must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class PaperTradingEngineConfig:
    engine_name: str = "paper_trading"
    engine_version: str = "1.0.0"
    dedup_capacity: int = 10_000
    observe_no_intent: bool = True
    observe_risk_rejected: bool = True

    def __post_init__(self) -> None:
        _text(self.engine_name, "engine_name", PaperTradingConfigurationError)
        _text(self.engine_version, "engine_version", PaperTradingConfigurationError)
        if (
            isinstance(self.dedup_capacity, bool)
            or not isinstance(self.dedup_capacity, int)
            or self.dedup_capacity <= 0
        ):
            raise PaperTradingConfigurationError("dedup_capacity must be positive")
        for name in ("observe_no_intent", "observe_risk_rejected"):
            if not isinstance(getattr(self, name), bool):
                raise PaperTradingConfigurationError(f"{name} must be a bool")


@dataclass(frozen=True, slots=True)
class PaperTradingCycleResult:
    cycle_id: str
    decision_pipeline_run_id: str
    market_snapshot_id: str
    signal_id: str
    intent_id: str | None
    risk_decision_id: str | None
    execution_id: str | None
    fill_id: str | None
    portfolio_id: str
    portfolio_version_at_start: int
    portfolio_version_before: int
    portfolio_version_after: int
    portfolio_before: PortfolioSnapshot
    portfolio_after: PortfolioSnapshot
    decision_result: DecisionPipelineResult
    risk_decision: RiskDecision | None
    execution_result: PaperExecutionResult | None
    outcome: PaperTradingCycleOutcome
    reason_code: str
    correlation_id: str | None
    completed_stages: tuple[PaperTradingStage, ...]
    risk_duration_ms: int
    execution_duration_ms: int
    portfolio_duration_ms: int
    observation_duration_ms: int
    total_duration_ms: int
    observation_status: PaperTradingObservationStatus
    failure_category: PaperTradingFailureCategory | None = None
    failure_message: str | None = None
    metadata: ImmutableMapping = field(default_factory=lambda: MappingProxyType({}))
    stable_decision_key: str | None = None
    decision_slot_key: str | None = None
    durable_command_key: str | None = None
    durable_journal_seq: int = 0
    durable_committed: bool = False
    recovered_existing: bool = False

    def __post_init__(self) -> None:
        for name in (
            "cycle_id",
            "decision_pipeline_run_id",
            "market_snapshot_id",
            "signal_id",
            "portfolio_id",
            "reason_code",
        ):
            _text(getattr(self, name), name, PaperTradingInvariantError)
        for name in ("intent_id", "risk_decision_id", "execution_id", "fill_id"):
            value = getattr(self, name)
            if value is not None:
                _text(value, name, PaperTradingInvariantError)
        for name in (
            "portfolio_version_at_start",
            "portfolio_version_before",
            "portfolio_version_after",
        ):
            _duration(getattr(self, name), name)
        for name in (
            "risk_duration_ms",
            "execution_duration_ms",
            "portfolio_duration_ms",
            "observation_duration_ms",
            "total_duration_ms",
        ):
            _duration(getattr(self, name), name)
        if not isinstance(self.portfolio_before, PortfolioSnapshot) or not isinstance(
            self.portfolio_after, PortfolioSnapshot
        ):
            raise PaperTradingInvariantError("portfolio snapshots are required")
        if self.portfolio_before.portfolio_id != self.portfolio_id or (
            self.portfolio_after.portfolio_id != self.portfolio_id
        ):
            raise PaperTradingInvariantError("portfolio IDs do not match")
        if self.portfolio_version_before != self.portfolio_before.version or (
            self.portfolio_version_after != self.portfolio_after.version
        ):
            raise PaperTradingInvariantError("portfolio versions do not match snapshots")
        if self.portfolio_version_before not in {
            self.portfolio_version_at_start,
            self.portfolio_version_at_start + 1,
        }:
            raise PaperTradingInvariantError("valuation version progression is invalid")
        if not isinstance(self.decision_result, DecisionPipelineResult):
            raise PaperTradingInvariantError("decision_result is invalid")
        if self.decision_pipeline_run_id != self.decision_result.pipeline_run_id:
            raise PaperTradingInvariantError("pipeline run ID mismatch")
        if self.market_snapshot_id != self.decision_result.snapshot_id:
            raise PaperTradingInvariantError("market snapshot ID mismatch")
        if self.signal_id != self.decision_result.signal.signal_id:
            raise PaperTradingInvariantError("signal ID mismatch")
        if not isinstance(self.outcome, PaperTradingCycleOutcome):
            raise PaperTradingInvariantError("outcome is invalid")
        if not isinstance(self.observation_status, PaperTradingObservationStatus):
            raise PaperTradingInvariantError("observation_status is invalid")
        if not isinstance(self.completed_stages, tuple) or not self.completed_stages:
            raise PaperTradingInvariantError("completed_stages must be a non-empty tuple")
        if any(not isinstance(stage, PaperTradingStage) for stage in self.completed_stages):
            raise PaperTradingInvariantError("completed_stages contains invalid stage")
        if len(set(self.completed_stages)) != len(self.completed_stages):
            raise PaperTradingInvariantError("completed_stages must not contain duplicates")
        for name in ("stable_decision_key", "decision_slot_key", "durable_command_key"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise PaperTradingInvariantError(f"{name} must be a SHA-256 digest")
        _duration(self.durable_journal_seq, "durable_journal_seq")
        if not isinstance(self.durable_committed, bool) or not isinstance(
            self.recovered_existing, bool
        ):
            raise PaperTradingInvariantError("durable flags must be bool")
        if self.recovered_existing and not self.durable_committed:
            raise PaperTradingInvariantError("recovered result must be durable")
        self._validate_outcome()
        expected_stages = {
            PaperTradingCycleOutcome.NO_INTENT: (
                PaperTradingStage.DECISION_RECEIVED,
                PaperTradingStage.DECISION_VALIDATED,
                PaperTradingStage.PORTFOLIO_VALUED,
                PaperTradingStage.NO_INTENT,
            ),
            PaperTradingCycleOutcome.PLANNER_DECLINED: (
                PaperTradingStage.DECISION_RECEIVED,
                PaperTradingStage.DECISION_VALIDATED,
                PaperTradingStage.PORTFOLIO_VALUED,
                PaperTradingStage.PLANNER_DECLINED,
            ),
            PaperTradingCycleOutcome.RISK_REJECTED: (
                PaperTradingStage.DECISION_RECEIVED,
                PaperTradingStage.DECISION_VALIDATED,
                PaperTradingStage.PORTFOLIO_VALUED,
                PaperTradingStage.RISK_EVALUATED,
                PaperTradingStage.RISK_REJECTED,
            ),
            PaperTradingCycleOutcome.NOT_EXECUTED: (
                PaperTradingStage.DECISION_RECEIVED,
                PaperTradingStage.DECISION_VALIDATED,
                PaperTradingStage.PORTFOLIO_VALUED,
                PaperTradingStage.RISK_EVALUATED,
                PaperTradingStage.EXECUTION_COMPLETED,
                PaperTradingStage.NO_FILL,
            ),
            PaperTradingCycleOutcome.EXECUTED: (
                PaperTradingStage.DECISION_RECEIVED,
                PaperTradingStage.DECISION_VALIDATED,
                PaperTradingStage.PORTFOLIO_VALUED,
                PaperTradingStage.RISK_EVALUATED,
                PaperTradingStage.EXECUTION_COMPLETED,
                PaperTradingStage.PORTFOLIO_UPDATED,
            ),
            PaperTradingCycleOutcome.DURABLE_REPLAY: (
                PaperTradingStage.DECISION_RECEIVED,
                PaperTradingStage.DECISION_VALIDATED,
                PaperTradingStage.DURABLE_REPLAY,
            ),
        }[self.outcome]
        if self.completed_stages != expected_stages:
            raise PaperTradingInvariantError("completed_stages do not match outcome")
        if self.failure_category is None and self.failure_message is not None:
            raise PaperTradingInvariantError("failure message requires category")
        if self.failure_category is not None:
            if self.failure_category is not PaperTradingFailureCategory.OBSERVER:
                raise PaperTradingInvariantError(
                    "completed result may only contain observer failure"
                )
            _text(self.failure_message, "failure_message", PaperTradingInvariantError)
        if (self.observation_status is PaperTradingObservationStatus.OBSERVATION_FAILED) != (
            self.failure_category is PaperTradingFailureCategory.OBSERVER
        ):
            raise PaperTradingInvariantError(
                "observer failure category and observation status must agree"
            )
        try:
            object.__setattr__(self, "metadata", freeze_mapping(self.metadata))
        except (TypeError, ValueError) as error:
            raise PaperTradingInvariantError("metadata must be deeply immutable") from error

    def _validate_outcome(self) -> None:
        if self.outcome is PaperTradingCycleOutcome.DURABLE_REPLAY:
            if (
                any(
                    value is not None
                    for value in (
                        self.risk_decision,
                        self.risk_decision_id,
                        self.execution_result,
                        self.execution_id,
                        self.fill_id,
                    )
                )
                or self.portfolio_after != self.portfolio_before
            ):
                raise PaperTradingInvariantError("durable replay must not contain financial work")
            return
        if self.outcome in {
            PaperTradingCycleOutcome.NO_INTENT,
            PaperTradingCycleOutcome.PLANNER_DECLINED,
        }:
            if any(
                value is not None
                for value in (
                    self.intent_id,
                    self.risk_decision,
                    self.risk_decision_id,
                    self.execution_result,
                    self.execution_id,
                    self.fill_id,
                )
            ):
                raise PaperTradingInvariantError("no-intent outcome contains trade data")
        elif self.outcome is PaperTradingCycleOutcome.RISK_REJECTED:
            if (
                self.intent_id is None
                or self.risk_decision is None
                or self.risk_decision_id != self.risk_decision.decision_id
                or self.risk_decision.outcome is not RiskDecisionOutcome.REJECTED
                or self.execution_result is not None
                or self.execution_id is not None
                or self.fill_id is not None
            ):
                raise PaperTradingInvariantError("invalid risk-rejected result")
        elif self.outcome is PaperTradingCycleOutcome.NOT_EXECUTED:
            if (
                self.intent_id is None
                or self.risk_decision is None
                or self.risk_decision_id != self.risk_decision.decision_id
                or self.execution_result is None
                or self.execution_id != self.execution_result.execution_id
                or self.execution_result.status is not PaperExecutionStatus.NOT_EXECUTED
                or self.execution_result.fill is not None
                or self.fill_id is not None
            ):
                raise PaperTradingInvariantError("invalid not-executed result")
        elif (
            self.intent_id is None
            or self.risk_decision is None
            or self.risk_decision_id != self.risk_decision.decision_id
            or self.execution_result is None
            or self.execution_id != self.execution_result.execution_id
            or self.execution_result.status is not PaperExecutionStatus.FILLED
            or self.execution_result.fill is None
            or self.fill_id != self.execution_result.fill.fill_id
            or self.portfolio_after.version != self.portfolio_before.version + 1
            or self.portfolio_after.applied_fill_count
            != self.portfolio_before.applied_fill_count + 1
            or PaperTradingStage.PORTFOLIO_UPDATED not in self.completed_stages
        ):
            raise PaperTradingInvariantError("invalid executed result")


@dataclass(frozen=True, slots=True)
class PaperTradingRecord:
    cycle_id: str
    decision_pipeline_run_id: str
    market_snapshot_id: str
    signal_direction: SignalDirection
    signal_reason_code: str
    intent_id: str | None
    intent_action: OrderIntentAction | None
    risk_decision_id: str | None
    risk_outcome: RiskDecisionOutcome | None
    risk_rejection_reasons: tuple[str, ...]
    risk_modification_reasons: tuple[str, ...]
    execution_id: str | None
    execution_status: str | None
    fill_id: str | None
    side: str | None
    quantity: Decimal | None
    fill_price: Decimal | None
    gross_notional: Decimal | None
    fee: Decimal | None
    quote_balance_after: Decimal
    base_balance_after: Decimal
    position_quantity_after: Decimal | None
    realised_pnl: Decimal
    unrealised_pnl: Decimal
    equity: Decimal
    drawdown: Decimal
    cycle_outcome: PaperTradingCycleOutcome
    reason_code: str
    observation_status: PaperTradingObservationStatus
    failure_category: PaperTradingFailureCategory | None
    correlation_id: str | None
    metadata: ImmutableMapping = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        for name in (
            "cycle_id",
            "decision_pipeline_run_id",
            "market_snapshot_id",
            "signal_reason_code",
            "reason_code",
        ):
            _text(getattr(self, name), name, PaperTradingInvariantError)
        if not isinstance(self.signal_direction, SignalDirection):
            raise PaperTradingInvariantError("signal_direction is invalid")
        if not isinstance(self.cycle_outcome, PaperTradingCycleOutcome):
            raise PaperTradingInvariantError("cycle_outcome is invalid")
        if not isinstance(self.observation_status, PaperTradingObservationStatus):
            raise PaperTradingInvariantError("observation_status is invalid")
        for name in (
            "quote_balance_after",
            "base_balance_after",
            "realised_pnl",
            "unrealised_pnl",
            "equity",
            "drawdown",
            "quantity",
            "fill_price",
            "gross_notional",
            "fee",
            "position_quantity_after",
        ):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, Decimal) or not value.is_finite()):
                raise PaperTradingInvariantError(f"{name} must be a finite Decimal")
        try:
            object.__setattr__(self, "metadata", freeze_mapping(self.metadata))
        except (TypeError, ValueError) as error:
            raise PaperTradingInvariantError("record metadata must be deeply immutable") from error


@dataclass(frozen=True, slots=True)
class PaperTradingEngineStatus:
    activity: PaperTradingEngineActivity = PaperTradingEngineActivity.IDLE
    health: PaperTradingEngineHealth = PaperTradingEngineHealth.READY
    handled_decisions: int = 0
    no_intent_cycles: int = 0
    planner_declined_cycles: int = 0
    risk_rejected_cycles: int = 0
    executed_cycles: int = 0
    not_executed_cycles: int = 0
    failed_cycles: int = 0
    observation_failures: int = 0
    ignored_duplicates: int = 0
    last_cycle_id: str | None = None
    last_intent_id: str | None = None
    last_risk_outcome: RiskDecisionOutcome | None = None
    last_execution_status: str | None = None
    last_fill_id: str | None = None
    last_portfolio_version: int | None = None
    last_error_category: PaperTradingFailureCategory | None = None
    last_error_message: str | None = None
