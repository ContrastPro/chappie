"""Deterministic evidence construction for bounded development analysis."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from app.ai.model_policy import resolve_development_model_role
from app.development_analysis.models import (
    DevelopmentAnalysisRequestV1,
    DevelopmentAnalysisStateV1,
    DevelopmentAnalysisV1,
    DevelopmentEvidencePackV1,
    assess_development_analysis_readiness,
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
                _development_retrieval_request(records, request.request, workspace_path),
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
            analysis = validate_development_analysis(analysis)
            return replace(analysis, readiness=assess_development_analysis_readiness(analysis))
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
    contracts = tuple(
        item.title
        for item in context.records
        if item.knowledge_type.value == "CONTRACT" or item.title.endswith("V1")
    )
    files = tuple(
        sorted(
            {
                source.path
                for item in context.records
                for source in item.sources
                if source.path.startswith(
                    ("app/development_analysis/", "tests/test_development_analysis")
                )
            }
        )
    )
    task_name = request.request.strip()
    return DevelopmentAnalysisV1(
        DevelopmentAnalysisStateV1.OK,
        request.request,
        evidence,
        facts=tuple(item.statement for item in context.records),
        inferences=(answer,),
        unknowns=unknowns,
        architecture_owners=owners,
        affected_contracts=contracts,
        likely_files=((workspace_path,) if workspace_path else files),
        risks=(f"{task_name}: preserve the read-only advisory and human-approval boundary.",),
        required_tests=(f"Test bounded evidence and readiness for {task_name}.",),
        non_goals=("No file edits, shell, Git, Knowledge mutation, or runtime authority.",),
        acceptance_criteria=(f"{task_name}: remain bound to the returned evidence digest.",),
        confidence=getattr(output, "confidence", None),
        validation_status="VALID",
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


def _failure(request, state, reason, evidence=None) -> DevelopmentAnalysisV1:
    return DevelopmentAnalysisV1(state, request.request, evidence=evidence, reason_code=reason)
