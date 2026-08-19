"""High-level Chappie AI facade and strict validation pipeline."""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from app.ai.errors import AIError, AIValidationError
from app.ai.market_semantics import semantics_context
from app.ai.models import ModelRequest, ModelResponse
from app.ai.project_qa import (
    PROJECT_QA_JSON_SCHEMA,
    ProjectQAAnswerV1,
    knowledge_facts,
    parse_project_qa,
)
from app.ai.prompts import prompt_digest, system_instruction
from app.ai.research_evidence_review import (
    RESEARCH_EVIDENCE_CRITIQUE_JSON_SCHEMA,
    RESEARCH_EVIDENCE_CRITIQUE_V11_JSON_SCHEMA,
    EvidenceCritiqueRunV1,
    EvidenceCritiqueRunV11,
    ResearchEvidenceCritiqueContextV1,
    critique_run_identity,
    critique_run_identity_v11,
    evidence_relation,
    parse_research_evidence_critique,
    parse_research_evidence_critique_v11,
)
from app.ai.research_reasoning import (
    RESEARCH_REASONING_JSON_SCHEMA,
    parse_research_reasoning_output,
    parse_research_reasoning_output_v111,
)
from app.ai.router import ModelRouter
from app.ai.schemas import (
    REASONING_OUTPUT_JSON_SCHEMA,
    FactV1,
    digest,
    parse_reasoning_output,
    timestamp_ms,
)
from app.ai.tasks import AITask, ModelProfile, task_contract
from app.ai.trace import AITraceEventType, trace_event


@dataclass(frozen=True, slots=True)
class AIConfig:
    profile: ModelProfile = ModelProfile.LOCAL_DEFAULT
    temperature: float = 0.0
    timeout_seconds: float = 30.0
    endpoint: str = "http://localhost:11434"
    model: str = "qwen3:8b"

    def __post_init__(self) -> None:
        if not math.isfinite(self.temperature) or not 0 <= self.temperature <= 2:
            raise AIValidationError("temperature must be finite and between zero and two")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise AIValidationError("timeout must be finite and positive")


class ChappieAI:
    def __init__(self, router: ModelRouter, config: AIConfig | None = None) -> None:
        self.router = router
        self.config = config or AIConfig()

    def generate(
        self,
        *,
        task: AITask,
        facts: tuple[FactV1, ...],
        information_cutoff: datetime,
        context: dict[str, object] | None = None,
        request_id: str | None = None,
        created_at: datetime | None = None,
    ) -> ModelResponse:
        cutoff_ms = timestamp_ms(information_cutoff)
        identifiers = tuple(fact.fact_id for fact in facts)
        if len(set(identifiers)) != len(identifiers):
            raise AIValidationError("fact IDs must be unique")
        future = [fact.fact_id for fact in facts if timestamp_ms(fact.timestamp) > cutoff_ms]
        if future:
            raise AIValidationError(f"facts exceed information cutoff: {future}")
        contract = task_contract(task)
        context_payload = dict(context or {})
        if task in {AITask.RESEARCH_REASONING_V11, AITask.RESEARCH_REASONING_V111}:
            context_payload["market_semantics"] = semantics_context()
        input_payload = {
            "task": task.value,
            "profile": self.config.profile.value,
            "prompt_version": contract.prompt_identity,
            "prompt_digest": prompt_digest(contract),
            "information_cutoff_ms": cutoff_ms,
            "facts": [fact.payload() for fact in facts],
            "context": context_payload,
            "temperature": self.config.temperature,
            "timeout_seconds": self.config.timeout_seconds,
        }
        request = ModelRequest(
            request_id or uuid.uuid4().hex,
            task,
            self.config.profile,
            system_instruction(contract),
            contract.prompt_identity,
            facts,
            context_payload,
            (
                RESEARCH_REASONING_JSON_SCHEMA
                if task in {AITask.RESEARCH_REASONING_V11, AITask.RESEARCH_REASONING_V111}
                else REASONING_OUTPUT_JSON_SCHEMA
            ),
            self.config.temperature,
            self.config.timeout_seconds,
            information_cutoff,
            digest(input_payload),
        )
        provider = self.router.provider_for(self.config.profile)
        try:
            raw = provider.generate(request)
        except AIError as error:
            # Benchmark infrastructure may record this causal request identity;
            # provider failures never become a validated model response.
            error.request_digest = request.input_digest  # type: ignore[attr-defined]
            raise
        try:
            validated = (
                (
                    parse_research_reasoning_output_v111(raw.structured_output, facts)
                    if task == AITask.RESEARCH_REASONING_V111
                    else parse_research_reasoning_output(raw.structured_output, facts)
                )
                if task in {AITask.RESEARCH_REASONING_V11, AITask.RESEARCH_REASONING_V111}
                else parse_reasoning_output(raw.structured_output, set(identifiers))
            )
        except AIValidationError as error:
            error.request_digest = request.input_digest  # type: ignore[attr-defined]
            error.rejected_output = _sanitized_rejected_output(raw.structured_output)  # type: ignore[attr-defined]
            error.received = _sanitized_diagnostic_value(error.received)
            raise
        return ModelResponse(
            request.request_id,
            provider.provider_identity,
            raw.model,
            raw.model_version,
            task,
            contract.prompt_identity,
            validated,
            raw.latency_ms,
            request.input_digest,
            digest(validated.payload()),
            created_at or datetime.now(tz=UTC),
            "VALID",
            raw.input_tokens,
            raw.output_tokens,
            tuple(sorted(raw.provider_metadata)),
        )

    def close(self) -> None:
        self.router.close()

    def answer_project_qa(
        self, *, question: str, knowledge_context, request_id: str | None = None
    ) -> ProjectQAAnswerV1:
        if not question.strip():
            raise AIValidationError("project QA question is required")
        task = AITask.PROJECT_QA_V1
        contract = task_contract(task)
        facts = knowledge_facts(knowledge_context, datetime.now(tz=UTC))
        payload = {
            "task": task.value,
            "question": question,
            "context_digest": knowledge_context.context_digest,
            "facts": [x.payload() for x in facts],
            "prompt": contract.prompt_identity,
        }
        request = ModelRequest(
            request_id or uuid.uuid4().hex,
            task,
            self.config.profile,
            system_instruction(contract),
            contract.prompt_identity,
            facts,
            {"question": question, "knowledge_context_digest": knowledge_context.context_digest},
            PROJECT_QA_JSON_SCHEMA,
            self.config.temperature,
            self.config.timeout_seconds,
            datetime.now(tz=UTC),
            digest(payload),
        )
        provider = self.router.provider_for(self.config.profile)
        try:
            raw = provider.generate(request)
            return parse_project_qa(raw.structured_output, knowledge_context)
        except AIValidationError as error:
            error.request_digest = request.input_digest  # type: ignore[attr-defined]
            error.rejected_output = _sanitized_rejected_output(raw.structured_output)  # type: ignore[attr-defined]
            raise

    def review_research_evidence(
        self,
        *,
        context: ResearchEvidenceCritiqueContextV1,
        request_id: str | None = None,
    ) -> EvidenceCritiqueRunV1:
        """Run one validated critique. It cannot trigger further RDE execution."""
        task = AITask.RESEARCH_EVIDENCE_CRITIQUE_V1
        contract = task_contract(task)
        facts = context.facts()
        cutoff_ms = timestamp_ms(context.information_cutoff)
        request_payload = {
            "task": task.value,
            "profile": self.config.profile.value,
            "prompt_version": contract.prompt_identity,
            "prompt_digest": prompt_digest(contract),
            "information_cutoff_ms": cutoff_ms,
            "facts": [fact.payload() for fact in facts],
            "context": context.payload(),
            "temperature": self.config.temperature,
            "timeout_seconds": self.config.timeout_seconds,
        }
        request = ModelRequest(
            request_id or uuid.uuid4().hex,
            task,
            self.config.profile,
            system_instruction(contract),
            contract.prompt_identity,
            facts,
            context.payload(),
            RESEARCH_EVIDENCE_CRITIQUE_JSON_SCHEMA,
            self.config.temperature,
            self.config.timeout_seconds,
            context.information_cutoff,
            digest(request_payload),
        )
        provider = self.router.provider_for(self.config.profile)
        try:
            raw = provider.generate(request)
            review = parse_research_evidence_critique(raw.structured_output, context)
        except AIValidationError as error:
            error.request_digest = request.input_digest  # type: ignore[attr-defined]
            error.rejected_output = _sanitized_rejected_output(raw.structured_output)  # type: ignore[attr-defined]
            raise
        except AIError as error:
            error.request_digest = request.input_digest  # type: ignore[attr-defined]
            raise
        prompt_hash = prompt_digest(contract)
        return EvidenceCritiqueRunV1(
            request.request_id,
            provider.provider_identity,
            raw.model,
            raw.model_version,
            task,
            contract.prompt_identity,
            prompt_hash,
            request.input_digest,
            critique_run_identity(
                context,
                provider=provider.provider_identity,
                model=raw.model,
                prompt_digest=prompt_hash,
            ),
            context.evidence_digest,
            context.original_hypothesis.hypothesis_id,
            "VALID",
            raw.latency_ms,
            review,
            tuple(sorted(raw.provider_metadata)),
            _critique_trace_events(
                request.request_id, provider.provider_identity, raw.model, context, review
            ),
        )

    def review_research_evidence_v11(
        self, *, context: ResearchEvidenceCritiqueContextV1, request_id: str | None = None
    ) -> EvidenceCritiqueRunV11:
        """v1.1: evidence relation is Chappie metadata, never a model verdict."""
        task = AITask.RESEARCH_EVIDENCE_CRITIQUE_V11
        contract = task_contract(task)
        facts = context.facts()
        request_payload = {
            "task": task.value,
            "profile": self.config.profile.value,
            "prompt_version": contract.prompt_identity,
            "prompt_digest": prompt_digest(contract),
            "information_cutoff_ms": timestamp_ms(context.information_cutoff),
            "facts": [fact.payload() for fact in facts],
            "context": {
                **context.payload(),
                "contract": "research_evidence_critique/1.1.0",
                "evidence_relation": evidence_relation(context.evidence.evidence_status).value,
            },
            "temperature": self.config.temperature,
            "timeout_seconds": self.config.timeout_seconds,
        }
        request = ModelRequest(
            request_id or uuid.uuid4().hex,
            task,
            self.config.profile,
            system_instruction(contract),
            contract.prompt_identity,
            facts,
            request_payload["context"],
            RESEARCH_EVIDENCE_CRITIQUE_V11_JSON_SCHEMA,
            self.config.temperature,
            self.config.timeout_seconds,
            context.information_cutoff,
            digest(request_payload),
        )
        provider = self.router.provider_for(self.config.profile)
        try:
            raw = provider.generate(request)
            review = parse_research_evidence_critique_v11(raw.structured_output, context)
        except AIValidationError as error:
            error.request_digest = request.input_digest  # type: ignore[attr-defined]
            error.rejected_output = _sanitized_rejected_output(raw.structured_output)  # type: ignore[attr-defined]
            raise
        except AIError as error:
            error.request_digest = request.input_digest  # type: ignore[attr-defined]
            raise
        prompt_hash = prompt_digest(contract)
        return EvidenceCritiqueRunV11(
            request.request_id,
            provider.provider_identity,
            raw.model,
            raw.model_version,
            task,
            contract.prompt_identity,
            prompt_hash,
            request.input_digest,
            critique_run_identity_v11(
                context,
                provider=provider.provider_identity,
                model=raw.model,
                prompt_digest=prompt_hash,
            ),
            context.evidence_digest,
            context.original_hypothesis.hypothesis_id,
            "VALID",
            raw.latency_ms,
            review,
            tuple(sorted(raw.provider_metadata)),
            _critique_trace_events_v11(
                request.request_id, provider.provider_identity, raw.model, context, review
            ),
        )


def _sanitized_rejected_output(value: object) -> object:
    """Keep only structured model JSON; never preserve hidden provider reasoning."""
    forbidden = {"thinking", "reasoning", "chain_of_thought", "raw_response"}
    if isinstance(value, dict):
        return {
            str(key): _sanitized_rejected_output(item)
            for key, item in value.items()
            if str(key).casefold() not in forbidden
        }
    if isinstance(value, list):
        return [_sanitized_rejected_output(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return type(value).__name__


def _critique_trace_events(request_id, provider, model, context, review):
    """Visible lifecycle facts only; provider-private reasoning is never traced."""
    events = [
        trace_event(
            AITraceEventType.RESEARCH_EVIDENCE_RECEIVED,
            request_id=request_id,
            task=AITask.RESEARCH_EVIDENCE_CRITIQUE_V1.value,
            provider=provider,
            model=model,
            metadata={"evidence_digest": context.evidence_digest},
        ),
        trace_event(
            AITraceEventType.HYPOTHESIS_REVIEW_STARTED,
            request_id=request_id,
            task=AITask.RESEARCH_EVIDENCE_CRITIQUE_V1.value,
            provider=provider,
            model=model,
            metadata={"original_hypothesis_id": context.original_hypothesis.hypothesis_id},
        ),
    ]
    if review.assessment.value == "CONTRADICTED_BY_EVIDENCE":
        events.append(trace_event(AITraceEventType.HYPOTHESIS_CONTRADICTED, request_id=request_id))
    if review.assessment.value == "SUPPORTED_BY_EVIDENCE":
        events.append(trace_event(AITraceEventType.HYPOTHESIS_SUPPORTED, request_id=request_id))
    if review.revised_hypothesis is not None:
        events.append(trace_event(AITraceEventType.HYPOTHESIS_REVISED, request_id=request_id))
    events.extend(
        trace_event(AITraceEventType.RESEARCH_REQUEST_CREATED, request_id=request_id)
        for _ in review.next_research_requests
    )
    return tuple(events)


def _critique_trace_events_v11(request_id, provider, model, context, review):
    events = [
        trace_event(
            AITraceEventType.RESEARCH_EVIDENCE_RECEIVED,
            request_id=request_id,
            task=AITask.RESEARCH_EVIDENCE_CRITIQUE_V11.value,
            provider=provider,
            model=model,
            metadata={
                "evidence_digest": context.evidence_digest,
                "rde_evidence_relation": review.evidence_relation.value,
            },
        ),
        trace_event(
            AITraceEventType.HYPOTHESIS_REVIEW_STARTED,
            request_id=request_id,
            task=AITask.RESEARCH_EVIDENCE_CRITIQUE_V11.value,
            provider=provider,
            model=model,
            metadata={
                "original_hypothesis_id": context.original_hypothesis.hypothesis_id,
                "confidence_before": review.confidence_before,
                "confidence_after": review.confidence_after,
            },
        ),
    ]
    if review.evidence_relation.value == "OPPOSED":
        events.append(trace_event(AITraceEventType.HYPOTHESIS_CONTRADICTED, request_id=request_id))
    if review.evidence_relation.value == "ALIGNED":
        events.append(trace_event(AITraceEventType.HYPOTHESIS_SUPPORTED, request_id=request_id))
    if review.revised_hypothesis is not None:
        events.append(trace_event(AITraceEventType.HYPOTHESIS_REVISED, request_id=request_id))
    events.extend(
        trace_event(AITraceEventType.RESEARCH_REQUEST_CREATED, request_id=request_id)
        for _ in review.next_research_requests
    )
    return tuple(events)


def _sanitized_diagnostic_value(value: object) -> object:
    forbidden = {"thinking", "reasoning", "chain_of_thought", "raw_response"}
    if isinstance(value, list):
        return [
            item for item in value if not isinstance(item, str) or item.casefold() not in forbidden
        ]
    return _sanitized_rejected_output(value)
