from app.ai.tasks import AITask
from app.knowledge import KnowledgeStatusV1, RepositorySnapshotV1, initial_records, manifest
from app.knowledge.retrieval import (
    RETRIEVAL_CONTRACT_DIGEST,
    RETRIEVAL_IDENTITY,
    KnowledgeRetrievalRequestV1,
    architecture_query_tokens,
    build_knowledge_request_for_task,
    retrieve,
    tokenize_topic,
)


def test_deterministic_retrieval_preserves_negative_result_and_decision():
    snapshot = RepositorySnapshotV1("a" * 40, True)
    records = initial_records(snapshot=snapshot)
    result, context = retrieve(
        records,
        manifest(records, snapshot),
        KnowledgeRetrievalRequestV1(include_historical=True, topic_tokens=("multi",)),
    )
    assert RETRIEVAL_IDENTITY == "chappie_knowledge_retrieval/1.3.0"
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


def test_canonical_architecture_entities_outrank_generic_project_term_collisions():
    snapshot = RepositorySnapshotV1("a" * 40, True)
    records = initial_records(snapshot=snapshot)
    value = manifest(records, snapshot)
    expected_titles = {
        "What is Chappie Control Plane?": "Chappie Control Plane",
        "What is Project QA?": "Project QA",
        "What is the Knowledge Store?": "Knowledge Store",
        "What owns verified research evidence authority?": "RDE evidence authority",
        "What is the Trading Agent?": "Trading Agent",
        "What is the Setup Engine?": "Setup Engine",
        "Understand architecture": "Chappie Architecture",
    }
    for question, expected_title in expected_titles.items():
        result, context = retrieve(
            records,
            value,
            KnowledgeRetrievalRequestV1(
                topic_tokens=tokenize_topic(question), include_planned=True
            ),
        )
        assert context.records[0].title == expected_title
        assert result.selected_record_ids[0] == context.records[0].knowledge_id


def test_weak_multi_token_collision_is_excluded_instead_of_becoming_context():
    snapshot = RepositorySnapshotV1("a" * 40, True)
    records = initial_records(snapshot=snapshot)
    result, context = retrieve(
        records,
        manifest(records, snapshot),
        KnowledgeRetrievalRequestV1(topic_tokens=tokenize_topic("What is Chappie banana?")),
    )
    assert not context.records
    assert dict(result.excluded_reason_counts)["WEAK_TOPIC_MATCH"] >= 1


def test_architecture_self_knowledge_answers_direct_ownership_and_boundary_questions():
    snapshot = RepositorySnapshotV1("a" * 40, True)
    records = initial_records(snapshot=snapshot)
    value = manifest(records, snapshot)
    expected_titles = {
        "What package owns risk decisions?": "Risk decisions authority",
        "Who owns execution authority?": "Execution authority",
        "Can AI execute trades directly?": "AI authority boundary",
        "What is the role of persistence?": "Persistence state ownership",
        "Where should external provider adapters live?": "Platform I/O boundary",
        "What is the VS Code Workbench allowed to own?": "VS Code interface boundary",
        "What is the dependency direction for a new feature?": "Dependency direction",
        "How should a new feature be split across architecture layers?": "Feature implementation placement",
    }
    for question, expected_title in expected_titles.items():
        _, context = retrieve(
            records,
            value,
            KnowledgeRetrievalRequestV1(topic_tokens=tokenize_topic(question)),
        )
        assert expected_title in {record.title for record in context.records}


def test_architecture_query_composes_general_dimensions_and_safe_package_path_hints():
    snapshot = RepositorySnapshotV1("a" * 40, True)
    records = initial_records(snapshot=snapshot)
    _, context = retrieve(
        records,
        manifest(records, snapshot),
        KnowledgeRetrievalRequestV1(
            topic_tokens=architecture_query_tokens(
                "Review the attached file as a Chappie developer and assess dependencies.",
                workspace_path="app/knowledge/store.py",
            )
        ),
    )
    assert "Package ownership: Knowledge" in {record.title for record in context.records}
    assert architecture_query_tokens(
        "review package architecture", workspace_path="app/knowledge/store.py"
    ) == architecture_query_tokens(
        "review package architecture", workspace_path="app/knowledge/store.py"
    )


def test_related_records_do_not_expand_implicit_architecture_queries():
    snapshot = RepositorySnapshotV1("a" * 40, True)
    records = initial_records(snapshot=snapshot)
    _, context = retrieve(
        records,
        manifest(records, snapshot),
        KnowledgeRetrievalRequestV1(
            topic_tokens=architecture_query_tokens("external provider integration in app.risk")
        ),
    )
    assert "Platform I/O boundary" in {record.title for record in context.records}
    assert "Setup Engine" not in {record.title for record in context.records}


def test_architecture_query_composes_design_and_research_order_authority_dimensions():
    snapshot = RepositorySnapshotV1("a" * 40, True)
    records = initial_records(snapshot=snapshot)
    value = manifest(records, snapshot)
    for question, expected in (
        (
            "Is Economic Calendar currently implemented? Separate current fact, architectural inference, proposed design, and unknown decision.",
            "Clean Architecture layers",
        ),
        (
            "Let research submit exchange orders for experimental validation.",
            "Execution authority",
        ),
    ):
        _, context = retrieve(
            records,
            value,
            KnowledgeRetrievalRequestV1(topic_tokens=architecture_query_tokens(question)),
        )
        assert expected in {record.title for record in context.records}


def test_validated_review_paths_select_owning_package_records_first():
    from app.knowledge.retrieval import review_record_ids

    snapshot = RepositorySnapshotV1("a" * 40, True)
    records = initial_records(snapshot=snapshot)
    value = manifest(records, snapshot)
    expected = {
        "app/knowledge/store.py": "Package ownership: Knowledge",
        "app/control_plane/service.py": "Package ownership: Control Plane",
        "app/platform/setup/engine.py": "Package ownership: Platform setup",
        "app/ai/runtime.py": "Package ownership: AI",
        "app/persistence/sqlite.py": "Package ownership: Persistence",
    }
    for path, title in expected.items():
        ids = review_record_ids(records, path)
        _, context = retrieve(
            records,
            value,
            KnowledgeRetrievalRequestV1(
                topic_tokens=architecture_query_tokens(
                    "Review ownership and fit.", workspace_path=path
                ),
                record_ids=ids,
                maximum_records=4,
            ),
        )
        assert context.records[0].title == title
        assert len(context.records) <= 4
        assert title in {record.title for record in context.records}
        if path == "app/ai/runtime.py":
            assert "Package ownership: AI research" not in {
                record.title for record in context.records
            }
            assert "AI authority boundary" in {record.title for record in context.records}
