from typing import Protocol, runtime_checkable

from app.execution.models import PaperExecutionResult
from app.trading.models import RiskDecision


@runtime_checkable
class ExecutionProvider(Protocol):
    def execute(self, decision: RiskDecision) -> PaperExecutionResult: ...
