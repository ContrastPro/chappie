from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum

from app.trading.models import MarketSnapshot
from app.trading.pipeline import DecisionPipelineResult, PipelineOutcome


class TestnetDecisionOutcome(StrEnum):
    HOLD = "HOLD"
    DECLINED = "DECLINED"
    EXECUTED = "EXECUTED"
    DUPLICATE = "DUPLICATE"


@dataclass(frozen=True, slots=True)
class TestnetDecisionResult:
    outcome: TestnetDecisionOutcome
    pipeline_run_id: str
    intent_id: str | None


@dataclass(frozen=True, slots=True)
class TestnetConsumerStatus:
    handled: int = 0
    holds: int = 0
    declined: int = 0
    executed: int = 0
    duplicates: int = 0
    failures: int = 0
    last_pipeline_run_id: str | None = None


class TestnetExecutionConsumer:
    """Consumes the coordinator-owned decision objects without reevaluation."""

    def __init__(
        self,
        execute_intent: Callable[[DecisionPipelineResult, MarketSnapshot], object],
        *,
        dedup_capacity: int = 10_000,
    ) -> None:
        if not callable(execute_intent):
            raise TypeError("execute_intent must be callable")
        if (
            isinstance(dedup_capacity, bool)
            or not isinstance(dedup_capacity, int)
            or dedup_capacity <= 0
        ):
            raise TypeError("dedup_capacity must be positive")
        self._execute = execute_intent
        self._capacity = dedup_capacity
        self._completed: OrderedDict[str, TestnetDecisionResult] = OrderedDict()
        self._status = TestnetConsumerStatus()

    @property
    def status(self) -> TestnetConsumerStatus:
        return replace(self._status)

    def consume(
        self, decision: DecisionPipelineResult, snapshot: MarketSnapshot
    ) -> TestnetDecisionResult:
        if (
            decision.snapshot_id != snapshot.snapshot_id
            or decision.stream_key != snapshot.stream_key
        ):
            raise ValueError("Testnet decision/snapshot identity mismatch")
        existing = self._completed.get(decision.pipeline_run_id)
        if existing is not None:
            self._status = replace(self._status, duplicates=self._status.duplicates + 1)
            return replace(existing, outcome=TestnetDecisionOutcome.DUPLICATE)
        self._status = replace(
            self._status,
            handled=self._status.handled + 1,
            last_pipeline_run_id=decision.pipeline_run_id,
        )
        if decision.outcome is PipelineOutcome.HOLD:
            outcome = TestnetDecisionOutcome.HOLD
            self._status = replace(self._status, holds=self._status.holds + 1)
        elif decision.outcome is PipelineOutcome.PLANNER_DECLINED:
            outcome = TestnetDecisionOutcome.DECLINED
            self._status = replace(self._status, declined=self._status.declined + 1)
        else:
            try:
                self._execute(decision, snapshot)
            except Exception:
                self._status = replace(self._status, failures=self._status.failures + 1)
                raise
            outcome = TestnetDecisionOutcome.EXECUTED
            self._status = replace(self._status, executed=self._status.executed + 1)
        result = TestnetDecisionResult(
            outcome,
            decision.pipeline_run_id,
            None if decision.order_intent is None else decision.order_intent.intent_id,
        )
        self._completed[decision.pipeline_run_id] = result
        if len(self._completed) > self._capacity:
            self._completed.popitem(last=False)
        return result
