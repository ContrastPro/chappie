"""Deterministic evidence construction for bounded development analysis."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from time import monotonic
from uuid import uuid4

from app.ai.model_policy import resolve_development_model_role
from app.development_analysis.context import build_development_context_map
from app.development_analysis.models import (
    DevelopmentAnalysisRequestV1,
    DevelopmentAnalysisStateV1,
    DevelopmentAnalysisV1,
    DevelopmentCitationFailureV1,
    DevelopmentEvidencePackV1,
    DevelopmentLifecycleEventV1,
    DevelopmentLifecycleStageV1,
    DevelopmentLifecycleStatusV1,
    DevelopmentLifecycleV1,
    development_readiness,
    evidence_digest,
    validate_development_analysis,
)
from app.knowledge import KNOWLEDGE_IDENTITY, committed_snapshot, read_store
from app.knowledge.retrieval import (
    KnowledgeRetrievalRequestV1,
    retrieve,
    review_record_ids,
    tokenize_topic,
)


class DevelopmentAnalysisServiceV1:
    """Read-only composition root; its runner receives only verified bounded evidence."""

    def __init__(
        self,
        *,
        paths: object,
        runner: Callable[[DevelopmentAnalysisRequestV1, object, DevelopmentEvidencePackV1], object],
        role_available: Callable[[object], bool],
        clock: Callable[[], float] = monotonic,
        request_id_factory: Callable[[], str] = lambda: uuid4().hex,
    ) -> None:
        self._paths = paths
        self._runner = runner
        self._role_available = role_available
        self._clock = clock
        self._request_id_factory = request_id_factory

    def analyze(self, request: DevelopmentAnalysisRequestV1) -> DevelopmentAnalysisV1:
        from app.ai.errors import AIProviderError, AIProviderTimeoutError, AIValidationError
        from app.development_analysis.synthesis import DevelopmentCitationValidationError

        lifecycle = _LifecycleRecorder(request, self._clock, self._request_id_factory)
        lifecycle.begin(DevelopmentLifecycleStageV1.ROLE_RESOLUTION)
        try:
            role, model = resolve_development_model_role(request.requested_role)
        except ValueError:
            return _failure(
                request,
                DevelopmentAnalysisStateV1.VALIDATION_ERROR,
                "MODEL_ROLE_INVALID",
                lifecycle=lifecycle.failed("MODEL_ROLE_INVALID"),
            )
        lifecycle.complete(effective_model=model.concrete_model)
        if not self._role_available(request.requested_role):
            return _failure(
                request,
                DevelopmentAnalysisStateV1.MODEL_UNAVAILABLE,
                "MODEL_ROLE_UNAVAILABLE",
                lifecycle=lifecycle.failed("MODEL_ROLE_UNAVAILABLE"),
            )
        lifecycle.begin(DevelopmentLifecycleStageV1.EVIDENCE_BUILD)
        try:
            snapshot = committed_snapshot(self._paths.root)
        except (OSError, ValueError):
            return _failure(
                request,
                DevelopmentAnalysisStateV1.REPOSITORY_UNAVAILABLE,
                "REPOSITORY_UNAVAILABLE",
                lifecycle=lifecycle.failed("REPOSITORY_UNAVAILABLE"),
            )
        if not snapshot.clean:
            return _failure(
                request,
                DevelopmentAnalysisStateV1.REPOSITORY_UNAVAILABLE,
                "REPOSITORY_DIRTY",
                lifecycle=lifecycle.failed("REPOSITORY_DIRTY"),
            )
        try:
            records, manifest = read_store(self._paths.knowledge_file)
            if manifest.snapshot != snapshot:
                return _failure(
                    request,
                    DevelopmentAnalysisStateV1.KNOWLEDGE_UNAVAILABLE,
                    "KNOWLEDGE_STALE",
                    lifecycle=lifecycle.failed("KNOWLEDGE_STALE"),
                )
            workspace_path = getattr(request.workspace_context, "active_file_relative_path", None)
            result, context = retrieve(
                records,
                manifest,
                _development_retrieval_request(records, request.request, workspace_path),
            )
        except (OSError, TypeError, ValueError):
            return _failure(
                request,
                DevelopmentAnalysisStateV1.KNOWLEDGE_UNAVAILABLE,
                "KNOWLEDGE_UNAVAILABLE",
                lifecycle=lifecycle.failed("KNOWLEDGE_UNAVAILABLE"),
            )
        if not context.records:
            return _failure(
                request,
                DevelopmentAnalysisStateV1.KNOWLEDGE_UNAVAILABLE,
                "NO_RELEVANT_KNOWLEDGE",
                lifecycle=lifecycle.failed("NO_RELEVANT_KNOWLEDGE"),
            )
        payload = {
            "source_commit": snapshot.commit,
            "repository_clean": snapshot.clean,
            "knowledge_identity": KNOWLEDGE_IDENTITY,
            "knowledge_manifest_digest": manifest.manifest_digest,
            "knowledge_context_digest": context.context_digest,
            "workspace_context_digest": (
                request.workspace_context.context_digest if request.workspace_context else None
            ),
            "requested_role": request.requested_role.value,
            "requested_model": model.concrete_model,
            "effective_model": model.concrete_model,
            "profile_identity": role.profile_identity,
            "capability_identity": role.capability_identity,
            "record_ids": result.selected_record_ids,
            "record_digests": result.selected_record_digests,
            "allowed_fact_ids": _allowed_fact_ids(context),
        }
        evidence = DevelopmentEvidencePackV1(
            **{**payload, "requested_role": request.requested_role},
            evidence_digest=evidence_digest(payload),
        )
        lifecycle.complete()
        lifecycle.begin(DevelopmentLifecycleStageV1.SYNTHESIS)
        try:
            output = self._runner(request, context, evidence)
            lifecycle.complete()
            lifecycle.begin(DevelopmentLifecycleStageV1.STRUCTURAL_VALIDATION)
            analysis = _analysis_from_output(request, context, evidence, output)
            analysis = validate_development_analysis(analysis)
            lifecycle.complete()
            lifecycle.begin(DevelopmentLifecycleStageV1.READINESS_EVALUATION)
            readiness, reason_code = development_readiness(analysis)
            lifecycle.complete()
            lifecycle.begin(DevelopmentLifecycleStageV1.COMPLETE)
            return replace(
                analysis,
                readiness=readiness,
                reason_code=reason_code,
                lifecycle=lifecycle.complete(),
            )
        except (TimeoutError, AIProviderTimeoutError):
            return _failure(
                request,
                DevelopmentAnalysisStateV1.TIMEOUT,
                "AI_PROVIDER_TIMEOUT",
                evidence,
                lifecycle.failed("AI_PROVIDER_TIMEOUT"),
            )
        except DevelopmentCitationValidationError as error:
            return _failure(
                request,
                DevelopmentAnalysisStateV1.ANALYSIS_INVALID,
                error.code,
                evidence,
                lifecycle.failed(error.code),
                citation_failure=error.failure,
            )
        except AIValidationError as error:
            return _failure(
                request,
                DevelopmentAnalysisStateV1.ANALYSIS_INVALID,
                error.code if error.code != "VALIDATION_ERROR" else "DEVELOPMENT_SCHEMA_INVALID",
                evidence,
                lifecycle.failed(
                    error.code if error.code != "VALIDATION_ERROR" else "DEVELOPMENT_SCHEMA_INVALID"
                ),
            )
        except ValueError as error:
            return _failure(
                request,
                DevelopmentAnalysisStateV1.ANALYSIS_INVALID,
                _validation_reason(error),
                evidence,
                lifecycle.failed(_validation_reason(error)),
            )
        except (AIProviderError, OSError, RuntimeError, TypeError):
            return _failure(
                request,
                DevelopmentAnalysisStateV1.MODEL_UNAVAILABLE,
                "AI_UNAVAILABLE",
                evidence,
                lifecycle.failed("AI_UNAVAILABLE"),
            )


def _analysis_from_output(request, context, evidence, output) -> DevelopmentAnalysisV1:
    from app.ai.errors import AIValidationError
    from app.development_analysis.synthesis import DevelopmentSynthesisOutputV1

    if not isinstance(output, DevelopmentSynthesisOutputV1):
        raise AIValidationError(
            "development synthesis output is invalid", code="DEVELOPMENT_SCHEMA_INVALID"
        )
    context_map = build_development_context_map(context, evidence)
    requested_handles = output.current_evidence_handles
    if not requested_handles:
        legacy_fact_ids = getattr(output, "_legacy_fact_ids", ())
        requested_handles = tuple(
            item.handle for item in context_map.evidence_handles if item.fact_id in legacy_fact_ids
        )
    selected_handles = tuple(
        item for item in context_map.evidence_handles if item.handle in requested_handles
    )
    return DevelopmentAnalysisV1(
        DevelopmentAnalysisStateV1.OK,
        request.request,
        evidence,
        facts=tuple(item.statement for item in selected_handles),
        inferences=output.inferences,
        unknowns=output.unknowns,
        architecture_owners=(
            getattr(output, "_legacy_architecture_owners", ())
            or tuple(sorted({item.category for item in selected_handles}))
        ),
        affected_contracts=context_map.existing_contracts,
        likely_files=context_map.existing_files,
        risks=output.risks,
        required_tests=output.required_tests,
        non_goals=output.non_goals,
        acceptance_criteria=output.acceptance_criteria,
        confidence=output.confidence,
        validation_status="VALID",
        context_map=context_map,
        current_evidence_handles=requested_handles,
        proposed_scope=output.proposed_scope,
    )


def _development_retrieval_request(records, request: str, workspace_path: str | None):
    """Build an additive, deterministic development-only retrieval request.

    Exact identifiers become `record_ids`, which the generic retriever already ranks
    ahead of broad architecture tokens.  Project QA never calls this helper.
    """
    identifiers = _development_identifiers(request, workspace_path)
    exact_ids = []
    for record in records:
        searchable = " ".join(
            (
                record.title,
                record.statement,
                *record.tags,
                *(source.path for source in record.sources),
            )
        ).casefold()
        if any(identifier in searchable for identifier in identifiers):
            exact_ids.append(record.knowledge_id)
    if identifiers:
        exact_ids.extend(
            record.knowledge_id
            for record in records
            if record.title
            in {
                "Package ownership: Development Intelligence",
                "POST /v1/development/analyze",
                "POST /v1/development/task",
                "Development Intelligence advisory boundary",
                "Development Intelligence analysis tests",
            }
        )
    exact_ids.extend(review_record_ids(records, workspace_path))
    # Broad architecture is a fallback only; explicit development identifiers
    # must consume the bounded evidence budget before generic context is added.
    if not identifiers:
        exact_ids.extend(
            record.knowledge_id for record in records if record.title == "Chappie Architecture"
        )
    return KnowledgeRetrievalRequestV1(
        topic_tokens=tokenize_topic(request),
        record_ids=tuple(dict.fromkeys(exact_ids))[:8],
        maximum_records=8,
    )


def _development_identifiers(request: str, workspace_path: str | None) -> tuple[str, ...]:
    import re

    values = set(re.findall(r"app(?:\.[a-z_]+)+|/[a-z0-9_/-]+|[A-Z][A-Za-z0-9]+V1", request))
    if workspace_path:
        values.add(workspace_path.replace("/", ".").rsplit(".", 1)[0])
    return tuple(sorted(value.casefold() for value in values))


def _failure(
    request,
    state,
    reason,
    evidence=None,
    lifecycle=None,
    citation_failure: DevelopmentCitationFailureV1 | None = None,
) -> DevelopmentAnalysisV1:
    return DevelopmentAnalysisV1(
        state,
        request.request,
        evidence=evidence,
        validation_status="INVALID"
        if state == DevelopmentAnalysisStateV1.ANALYSIS_INVALID
        else "UNAVAILABLE",
        reason_code=reason,
        lifecycle=lifecycle,
        citation_failure=citation_failure,
    )


def _allowed_fact_ids(context: object) -> tuple[str, ...]:
    from app.development_analysis.synthesis import development_allowed_fact_ids

    return development_allowed_fact_ids(context)


def _validation_reason(error: ValueError) -> str:
    message = str(error)
    if message == "development analysis claims forbidden authority":
        return "FORBIDDEN_AUTHORITY_TEXT"
    return "DEVELOPMENT_SCHEMA_INVALID"


class _LifecycleRecorder:
    """Bounded operational telemetry; it intentionally contains no request content."""

    def __init__(
        self,
        request: DevelopmentAnalysisRequestV1,
        clock: Callable[[], float],
        request_id_factory: Callable[[], str],
    ) -> None:
        self._request_id = request_id_factory()
        self._requested_role = request.requested_role
        self._clock = clock
        self._started = clock()
        self._stage_started = self._started
        self._stage = DevelopmentLifecycleStageV1.REQUEST_ACCEPTED
        self._effective_model: str | None = None
        self._events = [
            DevelopmentLifecycleEventV1(
                DevelopmentLifecycleStageV1.REQUEST_ACCEPTED,
                DevelopmentLifecycleStatusV1.COMPLETED,
                0,
            )
        ]

    def begin(self, stage: DevelopmentLifecycleStageV1) -> None:
        self._stage = stage
        self._stage_started = self._clock()
        self._events.append(
            DevelopmentLifecycleEventV1(stage, DevelopmentLifecycleStatusV1.STARTED, 0)
        )

    def complete(self, *, effective_model: str | None = None) -> DevelopmentLifecycleV1:
        if effective_model is not None:
            self._effective_model = effective_model
        self._events.append(
            DevelopmentLifecycleEventV1(
                self._stage,
                DevelopmentLifecycleStatusV1.COMPLETED,
                self._elapsed_ms(),
            )
        )
        return self._snapshot()

    def failed(self, reason_code: str) -> DevelopmentLifecycleV1:
        self._events.append(
            DevelopmentLifecycleEventV1(
                self._stage,
                DevelopmentLifecycleStatusV1.FAILED,
                self._elapsed_ms(),
                reason_code,
            )
        )
        return self._snapshot()

    def _elapsed_ms(self) -> int:
        return min(120_000, max(0, round((self._clock() - self._stage_started) * 1000)))

    def _snapshot(self) -> DevelopmentLifecycleV1:
        return DevelopmentLifecycleV1(
            self._request_id,
            "DEVELOPMENT_ANALYSIS",
            self._requested_role,
            self._effective_model,
            tuple(self._events),
        )
