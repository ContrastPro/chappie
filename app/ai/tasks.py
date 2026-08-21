"""Versioned Chappie AI task registry."""

from dataclasses import dataclass
from enum import StrEnum


class AITask(StrEnum):
    NEWS_INTERPRETATION = "NEWS_INTERPRETATION"
    MARKET_INTERPRETATION = "MARKET_INTERPRETATION"
    HYPOTHESIS_GENERATION = "HYPOTHESIS_GENERATION"
    HYPOTHESIS_CRITIQUE = "HYPOTHESIS_CRITIQUE"
    RESEARCH_REASONING_V11 = "RESEARCH_REASONING_V11"
    RESEARCH_REASONING_V111 = "RESEARCH_REASONING_V111"
    RESEARCH_EVIDENCE_CRITIQUE_V1 = "RESEARCH_EVIDENCE_CRITIQUE_V1"
    RESEARCH_EVIDENCE_CRITIQUE_V11 = "RESEARCH_EVIDENCE_CRITIQUE_V11"
    PROJECT_QA_V1 = "PROJECT_QA_V1"


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
        TaskContract(AITask.RESEARCH_REASONING_V11, "research_reasoning", "1.1.0"),
        TaskContract(AITask.RESEARCH_REASONING_V111, "research_reasoning", "1.1.1"),
        TaskContract(
            AITask.RESEARCH_EVIDENCE_CRITIQUE_V1,
            "research_evidence_critique",
            "1.0.0",
        ),
        TaskContract(AITask.PROJECT_QA_V1, "project_qa", "1.0.2"),
        TaskContract(
            AITask.RESEARCH_EVIDENCE_CRITIQUE_V11,
            "research_evidence_critique",
            "1.1.0",
        ),
    )
}


def task_contract(task: AITask) -> TaskContract:
    return TASKS[task]
