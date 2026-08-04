from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Protocol, runtime_checkable
from uuid import uuid4

from app.market.models import MarketStreamKey
from app.trading.exceptions import (
    IntentPlanningError,
    InvalidPipelineConfigurationError,
    PipelineInvariantError,
    PipelineRunIdFactoryError,
    PlannerExecutionError,
    StrategyExecutionError,
    TradingDomainError,
)
from app.trading.intent import (
    IntentPlanner,
    IntentPlanOutcome,
    IntentPlanResult,
)
from app.trading.models import (
    ImmutableMapping,
    MarketSnapshot,
    OrderIntent,
    OrderIntentAction,
    Signal,
    SignalDirection,
    freeze_mapping,
)
from app.trading.strategy import Strategy


class PipelineOutcome(Enum):
    HOLD = "hold"
    INTENT_CREATED = "intent_created"
    PLANNER_DECLINED = "planner_declined"


@runtime_checkable
class PipelineRunIdFactory(Protocol):
    def new_pipeline_run_id(self) -> str:
        """Return the next pipeline-run identifier."""


class UUIDPipelineRunIdFactory:
    def new_pipeline_run_id(self) -> str:
        return str(uuid4())


@dataclass(frozen=True, slots=True)
class DecisionPipelineResult:
    pipeline_run_id: str
    snapshot_id: str
    stream_key: MarketStreamKey
    evaluated_at_ms: int
    strategy_name: str
    strategy_version: str
    signal: Signal
    intent_plan: IntentPlanResult
    order_intent: OrderIntent | None
    outcome: PipelineOutcome
    reason_code: str
    correlation_id: str | None
    completed_stages: tuple[str, ...]
    metadata: ImmutableMapping = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        for field_name in (
            "pipeline_run_id",
            "snapshot_id",
            "strategy_name",
            "strategy_version",
            "reason_code",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise PipelineInvariantError(f"{field_name} must be a non-empty string")
        if (
            not isinstance(self.evaluated_at_ms, int)
            or isinstance(self.evaluated_at_ms, bool)
            or self.evaluated_at_ms < 0
        ):
            raise PipelineInvariantError("evaluated_at_ms must be non-negative")
        if not isinstance(self.signal, Signal):
            raise PipelineInvariantError("signal must be a Signal")
        if not isinstance(self.stream_key, MarketStreamKey):
            raise PipelineInvariantError("stream_key must be a MarketStreamKey")
        if not isinstance(self.intent_plan, IntentPlanResult):
            raise PipelineInvariantError("intent_plan must be an IntentPlanResult")
        if not isinstance(self.outcome, PipelineOutcome):
            raise PipelineInvariantError("outcome must be a PipelineOutcome")
        if self.signal.snapshot_id != self.snapshot_id:
            raise PipelineInvariantError("Signal snapshot_id does not match result")
        if self.signal.stream_key != self.stream_key:
            raise PipelineInvariantError("Signal stream_key does not match result")
        if self.intent_plan.signal_id != self.signal.signal_id:
            raise PipelineInvariantError("Intent plan signal_id does not match Signal")
        if (
            self.strategy_name != self.signal.strategy_name
            or self.strategy_version != self.signal.strategy_version
        ):
            raise PipelineInvariantError("Result strategy identity does not match Signal")
        if (
            self.correlation_id != self.signal.correlation_id
            or self.intent_plan.correlation_id != self.correlation_id
        ):
            raise PipelineInvariantError("Result correlation is inconsistent")
        if self.order_intent is not self.intent_plan.intent:
            raise PipelineInvariantError(
                "order_intent must be the same object as intent_plan.intent"
            )
        if not isinstance(self.completed_stages, tuple) or not self.completed_stages:
            raise PipelineInvariantError("completed_stages must be a non-empty tuple")
        expected_stages = (
            "snapshot_validated",
            "strategy_evaluated",
            "intent_planned",
        )
        if self.outcome is PipelineOutcome.INTENT_CREATED:
            expected_stages += ("intent_created",)
        if self.completed_stages != expected_stages:
            raise PipelineInvariantError("completed_stages are inconsistent")
        self._validate_outcome()
        try:
            object.__setattr__(self, "metadata", freeze_mapping(self.metadata))
        except (TypeError, ValueError) as error:
            raise PipelineInvariantError("metadata must contain immutable values") from error

    def _validate_outcome(self) -> None:
        if self.outcome is PipelineOutcome.HOLD:
            valid = (
                self.signal.direction is SignalDirection.HOLD
                and self.order_intent is None
                and self.intent_plan.outcome is IntentPlanOutcome.NO_INTENT_FOR_HOLD
            )
        elif self.outcome is PipelineOutcome.INTENT_CREATED:
            valid = (
                self.signal.direction in {SignalDirection.BUY, SignalDirection.SELL}
                and self.order_intent is not None
                and self.intent_plan.outcome is IntentPlanOutcome.CREATED
            )
        else:
            valid = (
                self.signal.direction in {SignalDirection.BUY, SignalDirection.SELL}
                and self.order_intent is None
                and self.intent_plan.outcome is IntentPlanOutcome.DECLINED
            )
        if not valid:
            raise PipelineInvariantError("Pipeline outcome is inconsistent")


class TradingDecisionPipeline:
    def __init__(
        self,
        strategy: Strategy,
        intent_planner: IntentPlanner,
        pipeline_run_id_factory: PipelineRunIdFactory,
    ) -> None:
        if not isinstance(strategy, Strategy):
            raise InvalidPipelineConfigurationError("strategy must satisfy Strategy Protocol")
        if not isinstance(intent_planner, IntentPlanner):
            raise InvalidPipelineConfigurationError(
                "intent_planner must satisfy IntentPlanner Protocol"
            )
        if not hasattr(pipeline_run_id_factory, "new_pipeline_run_id"):
            raise InvalidPipelineConfigurationError(
                "pipeline_run_id_factory must provide new_pipeline_run_id"
            )
        self._strategy = strategy
        self._intent_planner = intent_planner
        self._pipeline_run_id_factory = pipeline_run_id_factory

    def evaluate(self, snapshot: MarketSnapshot) -> DecisionPipelineResult:
        if not isinstance(snapshot, MarketSnapshot):
            raise PipelineInvariantError("snapshot must be a MarketSnapshot")
        pipeline_run_id = self._new_pipeline_run_id()
        try:
            signal = self._strategy.generate_signal(snapshot)
        except TradingDomainError:
            raise
        except Exception as error:
            raise StrategyExecutionError("Strategy evaluation failed") from error
        self._validate_signal(snapshot, signal)
        try:
            intent_plan = self._intent_planner.plan(signal)
        except IntentPlanningError:
            raise
        except TradingDomainError:
            raise
        except Exception as error:
            raise PlannerExecutionError("Intent planner execution failed") from error
        self._validate_plan(signal, intent_plan)

        if intent_plan.outcome is IntentPlanOutcome.NO_INTENT_FOR_HOLD:
            outcome = PipelineOutcome.HOLD
        elif intent_plan.outcome is IntentPlanOutcome.CREATED:
            outcome = PipelineOutcome.INTENT_CREATED
        else:
            outcome = PipelineOutcome.PLANNER_DECLINED
        completed_stages = (
            "snapshot_validated",
            "strategy_evaluated",
            "intent_planned",
        )
        if intent_plan.intent is not None:
            completed_stages += ("intent_created",)
        return DecisionPipelineResult(
            pipeline_run_id=pipeline_run_id,
            snapshot_id=snapshot.snapshot_id,
            stream_key=snapshot.stream_key,
            evaluated_at_ms=snapshot.as_of_ms,
            strategy_name=self._strategy.name,
            strategy_version=self._strategy.version,
            signal=signal,
            intent_plan=intent_plan,
            order_intent=intent_plan.intent,
            outcome=outcome,
            reason_code=(
                signal.reason_code if outcome is PipelineOutcome.HOLD else intent_plan.reason_code
            ),
            correlation_id=signal.correlation_id,
            completed_stages=completed_stages,
            metadata={
                "planner_name": self._intent_planner.name,
                "planner_version": self._intent_planner.version,
            },
        )

    def _new_pipeline_run_id(self) -> str:
        try:
            run_id = self._pipeline_run_id_factory.new_pipeline_run_id()
        except Exception as error:
            raise PipelineRunIdFactoryError("Pipeline run ID factory failed") from error
        if not isinstance(run_id, str) or not run_id.strip():
            raise PipelineRunIdFactoryError("Pipeline run ID factory returned an empty ID")
        return run_id

    def _validate_signal(self, snapshot: MarketSnapshot, signal: object) -> None:
        if not isinstance(signal, Signal):
            raise PipelineInvariantError("Strategy must return a Signal")
        if signal.snapshot_id != snapshot.snapshot_id:
            raise PipelineInvariantError("Signal snapshot_id does not match snapshot")
        if signal.stream_key != snapshot.stream_key:
            raise PipelineInvariantError("Signal stream_key does not match snapshot")
        if signal.strategy_name != self._strategy.name:
            raise PipelineInvariantError("Signal strategy_name does not match Strategy")
        if signal.strategy_version != self._strategy.version:
            raise PipelineInvariantError("Signal strategy_version does not match Strategy")
        if signal.created_at_ms != snapshot.as_of_ms or signal.effective_at_ms != snapshot.as_of_ms:
            raise PipelineInvariantError("Signal timestamps do not match snapshot")
        if signal.correlation_id != snapshot.correlation_cycle_id:
            raise PipelineInvariantError("Signal correlation does not match snapshot")

    def _validate_plan(self, signal: Signal, plan: object) -> None:
        if not isinstance(plan, IntentPlanResult):
            raise PipelineInvariantError("Intent Planner must return an IntentPlanResult")
        if plan.signal_id != signal.signal_id:
            raise PipelineInvariantError("Intent plan signal_id does not match Signal")
        if plan.planner_name != self._intent_planner.name:
            raise PipelineInvariantError("Intent plan planner_name does not match Planner")
        if plan.planner_version != self._intent_planner.version:
            raise PipelineInvariantError("Intent plan planner_version does not match Planner")
        if plan.correlation_id != signal.correlation_id:
            raise PipelineInvariantError("Intent plan correlation does not match Signal")
        if signal.direction is SignalDirection.HOLD:
            if plan.outcome is not IntentPlanOutcome.NO_INTENT_FOR_HOLD or plan.intent is not None:
                raise PipelineInvariantError("HOLD must not create an intent")
            return
        if plan.outcome is IntentPlanOutcome.DECLINED:
            if plan.intent is not None:
                raise PipelineInvariantError("DECLINED must not contain an intent")
            return
        if plan.outcome is not IntentPlanOutcome.CREATED or plan.intent is None:
            raise PipelineInvariantError("BUY/SELL CREATED plan requires an intent")
        self._validate_intent(signal, plan.intent)

    def _validate_intent(self, signal: Signal, intent: OrderIntent) -> None:
        if intent.source_signal_id != signal.signal_id:
            raise PipelineInvariantError("Intent source_signal_id does not match Signal")
        if intent.stream_key != signal.stream_key:
            raise PipelineInvariantError("Intent stream_key does not match Signal")
        if intent.created_at_ms != signal.effective_at_ms:
            raise PipelineInvariantError("Intent timestamp does not match Signal")
        if (
            intent.strategy_name != signal.strategy_name
            or intent.strategy_version != signal.strategy_version
        ):
            raise PipelineInvariantError("Intent strategy identity does not match Signal")
        if intent.correlation_id != signal.correlation_id:
            raise PipelineInvariantError("Intent correlation does not match Signal")
        if signal.direction is SignalDirection.BUY:
            if intent.action is not OrderIntentAction.ENTER_LONG:
                raise PipelineInvariantError("BUY must create ENTER_LONG")
            expected = self._intent_planner.configuration.get("long_target_exposure")
            if intent.target_exposure != expected:
                raise PipelineInvariantError(
                    "BUY target exposure does not match Planner configuration"
                )
        elif signal.direction is SignalDirection.SELL:
            if intent.action is not OrderIntentAction.EXIT_LONG or intent.target_exposure != 0:
                raise PipelineInvariantError("SELL must create EXIT_LONG with zero exposure")
