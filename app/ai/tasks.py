"""Versioned Chappie AI task registry."""

from dataclasses import dataclass
from enum import StrEnum


class AITask(StrEnum):
    NEWS_INTERPRETATION = "NEWS_INTERPRETATION"
    MARKET_INTERPRETATION = "MARKET_INTERPRETATION"
    HYPOTHESIS_GENERATION = "HYPOTHESIS_GENERATION"
    HYPOTHESIS_CRITIQUE = "HYPOTHESIS_CRITIQUE"


class ModelProfile(StrEnum):
    LOCAL_DEFAULT = "LOCAL_DEFAULT"
    LOCAL_FAST = "LOCAL_FAST"
    LOCAL_REASONING = "LOCAL_REASONING"
    CLOUD_REVIEWER = "CLOUD_REVIEWER"


@dataclass(frozen=True, slots=True)
class TaskContract:
    task: AITask
    prompt_name: str
    prompt_version: str = "1.0.0"

    @property
    def prompt_identity(self) -> str:
        return f"{self.prompt_name}/{self.prompt_version}"


TASKS = {
    task.task: task
    for task in (
        TaskContract(AITask.NEWS_INTERPRETATION, "news_interpretation"),
        TaskContract(AITask.MARKET_INTERPRETATION, "market_interpretation"),
        TaskContract(AITask.HYPOTHESIS_GENERATION, "hypothesis_generation"),
        TaskContract(AITask.HYPOTHESIS_CRITIQUE, "hypothesis_critique"),
    )
}


def task_contract(task: AITask) -> TaskContract:
    return TASKS[task]
