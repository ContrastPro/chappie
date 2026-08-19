"""Read-only, grounded Project QA smoke for product setup."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from app.ai.errors import AIProviderError, AIProviderTimeoutError, AIValidationError
from app.ai.runtime import ChappieAI
from app.ai_setup_smoke import build_configured_chappie_ai, valid_setup_ai_config
from app.bootstrap import ChappieLocalConfigV1
from app.knowledge import KnowledgeAuthorityV1, KnowledgeContextV1, KnowledgeTypeV1, read_store
from app.knowledge_retrieval import KnowledgeRetrievalRequestV1, retrieve

PROJECT_QA_SETUP_SMOKE_IDENTITY = "project_qa_setup_smoke/1.0.0"
PROJECT_QA_SETUP_QUESTION = "What owns research evidence authority?"


class ProjectQASetupSmokeStateV1(StrEnum):
    VALID = "VALID"
    KNOWLEDGE_ERROR = "KNOWLEDGE_ERROR"
    RETRIEVAL_ERROR = "RETRIEVAL_ERROR"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    TIMEOUT = "TIMEOUT"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    GROUNDING_ERROR = "GROUNDING_ERROR"
    CONFIG_ERROR = "CONFIG_ERROR"


@dataclass(frozen=True, slots=True)
class ProjectQASetupSmokeResultV1:
    state: ProjectQASetupSmokeStateV1
    context_digest: str | None = None
    selected_record_count: int = 0
    selected_knowledge_ids: tuple[str, ...] = ()
    provider: str | None = None
    model: str | None = None


AIBuilder = Callable[[ChappieLocalConfigV1], ChappieAI]


class ProjectQASetupSmokeV1:
    def __init__(self, *, ai_builder: AIBuilder | None = None) -> None:
        self._ai_builder = ai_builder or build_configured_chappie_ai

    def run(
        self, config: ChappieLocalConfigV1, knowledge_store: Path
    ) -> ProjectQASetupSmokeResultV1:
        if not valid_setup_ai_config(config):
            return ProjectQASetupSmokeResultV1(ProjectQASetupSmokeStateV1.CONFIG_ERROR)
        try:
            records, manifest = read_store(knowledge_store)
        except ValueError:
            return ProjectQASetupSmokeResultV1(ProjectQASetupSmokeStateV1.KNOWLEDGE_ERROR)
        try:
            result, context = retrieve(
                records,
                manifest,
                KnowledgeRetrievalRequestV1(
                    knowledge_types=(KnowledgeTypeV1.AUTHORITY_BOUNDARY,),
                    authorities=(KnowledgeAuthorityV1.VERSIONED_CONTRACT,),
                    tags=("ai", "authority"),
                    maximum_records=1,
                ),
            )
        except ValueError:
            return ProjectQASetupSmokeResultV1(ProjectQASetupSmokeStateV1.RETRIEVAL_ERROR)
        metadata: dict[str, Any] = {
            "context_digest": context.context_digest,
            "selected_record_count": len(context.records),
            "selected_knowledge_ids": result.selected_record_ids,
        }
        if len(context.records) != 1 or not context.records[0].statement.startswith(
            "RDE evidence is immutable authority"
        ):
            return ProjectQASetupSmokeResultV1(
                ProjectQASetupSmokeStateV1.GROUNDING_ERROR, **metadata
            )
        ai: ChappieAI | None = None
        try:
            ai = self._ai_builder(config)
            answer = ai.answer_project_qa(
                question=PROJECT_QA_SETUP_QUESTION, knowledge_context=context
            )
            if not _grounded_authority_answer(
                answer.answer, answer.supporting_knowledge_fact_ids, context
            ):
                return ProjectQASetupSmokeResultV1(
                    ProjectQASetupSmokeStateV1.GROUNDING_ERROR, **metadata
                )
            return ProjectQASetupSmokeResultV1(
                ProjectQASetupSmokeStateV1.VALID,
                provider=config.provider,
                model=config.model,
                **metadata,
            )
        except AIProviderTimeoutError:
            return ProjectQASetupSmokeResultV1(
                ProjectQASetupSmokeStateV1.TIMEOUT,
                provider=config.provider,
                model=config.model,
                **metadata,
            )
        except AIProviderError:
            return ProjectQASetupSmokeResultV1(
                ProjectQASetupSmokeStateV1.PROVIDER_ERROR,
                provider=config.provider,
                model=config.model,
                **metadata,
            )
        except AIValidationError:
            return ProjectQASetupSmokeResultV1(
                ProjectQASetupSmokeStateV1.VALIDATION_ERROR,
                provider=config.provider,
                model=config.model,
                **metadata,
            )
        finally:
            if ai is not None:
                ai.close()


def _grounded_authority_answer(
    answer: str, fact_ids: tuple[str, ...], context: KnowledgeContextV1
) -> bool:
    records = context.records
    required = f"knowledge.{records[0].knowledge_id}.statement"
    text = answer.casefold()
    forbidden = ("ai owns", "model owns", "provider owns", "trading agent owns")
    return (
        required in fact_ids
        and "rde" in text
        and "authority" in text
        and not any(phrase in text for phrase in forbidden)
    )
