from app.ai.tasks import AITask
from app.knowledge import KnowledgeStatusV1, RepositorySnapshotV1, initial_records, manifest
from app.knowledge_retrieval import (
    RETRIEVAL_CONTRACT_DIGEST,
    RETRIEVAL_IDENTITY,
    KnowledgeRetrievalRequestV1,
    build_knowledge_request_for_task,
    retrieve,
)


def test_deterministic_retrieval_preserves_negative_result_and_decision():
    snapshot = RepositorySnapshotV1("a" * 40, True)
    records = initial_records(snapshot=snapshot)
    result, context = retrieve(
        records,
        manifest(records, snapshot),
        KnowledgeRetrievalRequestV1(include_historical=True, topic_tokens=("multi",)),
    )
    assert RETRIEVAL_IDENTITY == "chappie_knowledge_retrieval/1.0.0"
    assert len(RETRIEVAL_CONTRACT_DIGEST) == 64
    assert result.selected_record_ids
    assert context.context_digest
    assert any(
        "MULTI_ASSET_ADDS_NO_CLEAR_INFORMATION" in item.statement for item in context.records
    )


def test_default_excludes_planned_and_task_profiles_are_deterministic():
    snapshot = RepositorySnapshotV1("a" * 40, True)
    records = initial_records(snapshot=snapshot)
    result, _ = retrieve(records, manifest(records, snapshot), KnowledgeRetrievalRequestV1())
    selected = {
        item.knowledge_id for item in records if item.knowledge_id in result.selected_record_ids
    }
    assert all(
        item.status != KnowledgeStatusV1.PLANNED
        for item in records
        if item.knowledge_id in selected
    )
    request = build_knowledge_request_for_task(AITask.RESEARCH_EVIDENCE_CRITIQUE_V11)
    assert request.knowledge_types
