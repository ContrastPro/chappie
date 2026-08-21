"""Read-only, grounded Project QA smoke for product setup."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from app.ai.errors import AIProviderError, AIProviderTimeoutError, AIValidationError
from app.ai.runtime import ChappieAI
from app.knowledge.retrieval import KnowledgeRetrievalRequestV1, retrieve
from app.knowledge.store import (
    KnowledgeAuthorityV1,
    KnowledgeContextV1,
    KnowledgeTypeV1,
    read_store,
)
from app.platform.bootstrap import ChappieLocalConfigV1
from app.platform.setup.ai_smoke import build_configured_chappie_ai, valid_setup_ai_config

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


class ProjectQAGroundingReasonV1(StrEnum):
    CANONICAL_AUTHORITY_RECORD_MISMATCH = "CANONICAL_AUTHORITY_RECORD_MISMATCH"
    MISSING_REQUIRED_FACT_CITATION = "MISSING_REQUIRED_FACT_CITATION"
    RDE_AUTHORITY_NOT_STATED = "RDE_AUTHORITY_NOT_STATED"
    WRONG_AUTHORITY_CLAIM = "WRONG_AUTHORITY_CLAIM"


@dataclass(frozen=True, slots=True)
class ProjectQASetupSmokeResultV1:
    state: ProjectQASetupSmokeStateV1
    context_digest: str | None = None
    selected_record_count: int = 0
    selected_knowledge_ids: tuple[str, ...] = ()
    provider: str | None = None
    model: str | None = None
    reason_code: str | None = None


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
                ProjectQASetupSmokeStateV1.GROUNDING_ERROR,
                reason_code=ProjectQAGroundingReasonV1.CANONICAL_AUTHORITY_RECORD_MISMATCH,
                **metadata,
            )
        ai: ChappieAI | None = None
        try:
            ai = self._ai_builder(config)
            answer = ai.answer_project_qa(
                question=PROJECT_QA_SETUP_QUESTION, knowledge_context=context
            )
            grounding_reason = _grounded_authority_reason(
                answer.answer, answer.supporting_knowledge_fact_ids, context
            )
            if grounding_reason is not None:
                return ProjectQASetupSmokeResultV1(
                    ProjectQASetupSmokeStateV1.GROUNDING_ERROR,
                    reason_code=grounding_reason,
                    **metadata,
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


def _grounded_authority_reason(
    answer: str, fact_ids: tuple[str, ...], context: KnowledgeContextV1
) -> ProjectQAGroundingReasonV1 | None:
    records = context.records
    required = f"knowledge.{records[0].knowledge_id}.statement"
    text = answer.casefold()
    forbidden = ("ai owns", "model owns", "provider owns", "trading agent owns")
    if required not in fact_ids:
        return ProjectQAGroundingReasonV1.MISSING_REQUIRED_FACT_CITATION
    if any(phrase in text for phrase in forbidden):
        return ProjectQAGroundingReasonV1.WRONG_AUTHORITY_CLAIM
    if "rde" not in text or "authority" not in text:
        return ProjectQAGroundingReasonV1.RDE_AUTHORITY_NOT_STATED
    return None
