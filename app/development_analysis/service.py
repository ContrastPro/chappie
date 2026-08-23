"""Deterministic evidence construction for bounded development analysis."""

from __future__ import annotations

from collections.abc import Callable

from app.ai.model_policy import resolve_development_model_role
from app.development_analysis.models import (
    DevelopmentAnalysisRequestV1,
    DevelopmentAnalysisStateV1,
    DevelopmentAnalysisV1,
    DevelopmentEvidencePackV1,
    evidence_digest,
    validate_development_analysis,
)
from app.knowledge import KNOWLEDGE_IDENTITY, committed_snapshot, read_store
from app.knowledge.retrieval import (
    KnowledgeRetrievalRequestV1,
    architecture_query_tokens,
    retrieve,
    review_record_ids,
)


class DevelopmentAnalysisServiceV1:
    """Read-only composition root; its runner receives only verified bounded evidence."""

    def __init__(
        self,
        *,
        paths: object,
        runner: Callable[[DevelopmentAnalysisRequestV1, object, DevelopmentEvidencePackV1], object],
        role_available: Callable[[object], bool],
    ) -> None:
        self._paths = paths
        self._runner = runner
        self._role_available = role_available

    def analyze(self, request: DevelopmentAnalysisRequestV1) -> DevelopmentAnalysisV1:
        from app.ai.errors import AIProviderError, AIProviderTimeoutError, AIValidationError

        try:
            role, model = resolve_development_model_role(request.requested_role)
        except ValueError:
            return _failure(
                request, DevelopmentAnalysisStateV1.VALIDATION_ERROR, "MODEL_ROLE_INVALID"
            )
        if not self._role_available(request.requested_role):
            return _failure(
                request,
                DevelopmentAnalysisStateV1.MODEL_UNAVAILABLE,
                "MODEL_ROLE_UNAVAILABLE",
            )
        try:
            snapshot = committed_snapshot(self._paths.root)
        except (OSError, ValueError):
            return _failure(
                request, DevelopmentAnalysisStateV1.REPOSITORY_UNAVAILABLE, "REPOSITORY_UNAVAILABLE"
            )
        if not snapshot.clean:
            return _failure(
                request, DevelopmentAnalysisStateV1.REPOSITORY_UNAVAILABLE, "REPOSITORY_DIRTY"
            )
        try:
            records, manifest = read_store(self._paths.knowledge_file)
            if manifest.snapshot != snapshot:
                return _failure(
                    request, DevelopmentAnalysisStateV1.KNOWLEDGE_UNAVAILABLE, "KNOWLEDGE_STALE"
                )
            workspace_path = getattr(request.workspace_context, "active_file_relative_path", None)
            result, context = retrieve(
                records,
                manifest,
                KnowledgeRetrievalRequestV1(
                    topic_tokens=architecture_query_tokens(
                        request.request, workspace_path=workspace_path
                    ),
                    record_ids=review_record_ids(records, workspace_path),
                    maximum_records=4 if workspace_path else 8,
                ),
            )
        except (OSError, TypeError, ValueError):
            return _failure(
                request, DevelopmentAnalysisStateV1.KNOWLEDGE_UNAVAILABLE, "KNOWLEDGE_UNAVAILABLE"
            )
        if not context.records:
            return _failure(
                request, DevelopmentAnalysisStateV1.KNOWLEDGE_UNAVAILABLE, "NO_RELEVANT_KNOWLEDGE"
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
        }
        evidence = DevelopmentEvidencePackV1(
            **{**payload, "requested_role": request.requested_role},
            evidence_digest=evidence_digest(payload),
        )
        try:
            output = self._runner(request, context, evidence)
            analysis = _analysis_from_output(request, context, evidence, output)
            return validate_development_analysis(analysis)
        except (TimeoutError, AIProviderTimeoutError):
            return _failure(
                request, DevelopmentAnalysisStateV1.TIMEOUT, "AI_PROVIDER_TIMEOUT", evidence
            )
        except (AIValidationError, ValueError):
            return _failure(
                request,
                DevelopmentAnalysisStateV1.ANALYSIS_INVALID,
                "DEVELOPMENT_ANALYSIS_INVALID",
                evidence,
            )
        except (AIProviderError, OSError, RuntimeError, TypeError):
            return _failure(
                request, DevelopmentAnalysisStateV1.MODEL_UNAVAILABLE, "AI_UNAVAILABLE", evidence
            )


def _analysis_from_output(request, context, evidence, output) -> DevelopmentAnalysisV1:
    answer = getattr(output, "answer", None)
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("analysis answer is invalid")
    unknowns = tuple(getattr(output, "unknowns", ()))
    if any(not isinstance(item, str) for item in unknowns):
        raise ValueError("analysis unknowns are invalid")
    workspace_path = getattr(request.workspace_context, "active_file_relative_path", None)
    owners = tuple(sorted({item.knowledge_type.value for item in context.records}))
    return DevelopmentAnalysisV1(
        DevelopmentAnalysisStateV1.OK,
        request.request,
        evidence,
        facts=tuple(item.statement for item in context.records),
        inferences=(answer,),
        unknowns=unknowns,
        architecture_owners=owners,
        affected_contracts=(),
        likely_files=(workspace_path,) if workspace_path else (),
        risks=("Human review is required before implementation.",),
        required_tests=("Focused deterministic analysis and boundary tests.",),
        non_goals=("No file edits, shell, Git, Knowledge mutation, or runtime authority.",),
        acceptance_criteria=("Analysis remains bound to the returned evidence digest.",),
        confidence=getattr(output, "confidence", None),
        validation_status="VALID",
    )


def _failure(request, state, reason, evidence=None) -> DevelopmentAnalysisV1:
    return DevelopmentAnalysisV1(state, request.request, evidence=evidence, reason_code=reason)
