from __future__ import annotations

import subprocess
from dataclasses import replace

import pytest

from app.knowledge import (
    KNOWLEDGE_CONTRACT_DIGEST,
    KNOWLEDGE_IDENTITY,
    KnowledgeAuthorityV1,
    KnowledgeCandidateV1,
    KnowledgeStatusV1,
    KnowledgeTypeV1,
    RepositorySnapshotV1,
    committed_snapshot,
    context,
    initial_records,
    manifest,
    query,
    reject_ai_write,
    verify_knowledge_store,
    write_store,
)

SNAPSHOT = RepositorySnapshotV1("a" * 40, True)


def test_contract_and_initial_records_are_deterministic() -> None:
    first = initial_records(snapshot=SNAPSHOT)
    second = initial_records(snapshot=SNAPSHOT)
    assert KNOWLEDGE_IDENTITY == "chappie_knowledge/1.0.0"
    assert len(KNOWLEDGE_CONTRACT_DIGEST) == 64
    assert first == second
    assert len(first) == 62
    architecture_records = [
        record for record in first if record.sources[0].path.startswith("docs/architecture/")
    ]
    assert len(architecture_records) == 48
    assert all(
        record.authority == KnowledgeAuthorityV1.DOCUMENTATION for record in architecture_records
    )
    assert all(record.status == KnowledgeStatusV1.ACTIVE for record in architecture_records)
    assert all(record.related_record_ids for record in architecture_records)


def test_records_cover_authority_result_decision_and_exact_filters() -> None:
    records = initial_records(snapshot=SNAPSHOT)
    result = query(records, knowledge_type=KnowledgeTypeV1.RESEARCH_RESULT)
    decision = query(records, tag="derivatives", authority=KnowledgeAuthorityV1.PROJECT_DECISION)
    assert result[0].statement.startswith("MULTI_ASSET_ADDS_NO_CLEAR_INFORMATION")
    assert decision[0].statement.startswith("DO_NOT_EXPAND_DERIVATIVES_YET")
    assert result[0].knowledge_id != decision[0].knowledge_id
    assert (
        query(records, status=KnowledgeStatusV1.PLANNED)[0].knowledge_type
        == KnowledgeTypeV1.BACKLOG
    )
    expected_path = "app/research/intelligence/multi_asset_flow.py"
    assert result[0].sources[0].path == expected_path
    assert decision[0].sources[0].path == expected_path
    assert result[0].authority == KnowledgeAuthorityV1.VERIFIED_RESEARCH_EVIDENCE
    assert result[0].status == KnowledgeStatusV1.HISTORICAL
    assert decision[0].authority == KnowledgeAuthorityV1.PROJECT_DECISION
    assert decision[0].status == KnowledgeStatusV1.ACTIVE


def test_manifest_context_verification_and_conflicts() -> None:
    records = initial_records(snapshot=SNAPSHOT)
    value = manifest(records, SNAPSHOT)
    assert verify_knowledge_store(records, value) == ()
    result = context(records[:2])
    assert result.knowledge_digest
    duplicate = replace(records[0], knowledge_id=records[0].knowledge_id)
    with pytest.raises(ValueError, match="semantic identities"):
        verify_knowledge_store((records[0], duplicate), value)


def test_dirty_snapshot_and_secret_paths_fail_closed() -> None:
    with pytest.raises(ValueError, match="REFUSE_CANONICAL"):
        initial_records(snapshot=RepositorySnapshotV1("b" * 40, False))
    with pytest.raises(ValueError, match="allowlisted"):
        from app.knowledge import KnowledgeSourceReferenceV1

        KnowledgeSourceReferenceV1(
            KnowledgeAuthorityV1.SOURCE_CODE, ".env", "a" * 64, "a" * 40, "x"
        )


def test_committed_snapshot_ignores_generated_gitignored_state(tmp_path) -> None:
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("generated/\n", encoding="utf-8")
    (tmp_path / "tracked.txt").write_text("source\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "fixture",
        ],
        cwd=tmp_path,
        check=True,
    )
    (tmp_path / "generated").mkdir()
    (tmp_path / "generated" / "local.sqlite3").write_text("ignored", encoding="utf-8")
    assert committed_snapshot(tmp_path).clean is True


def test_ai_has_no_canonical_write_authority() -> None:
    with pytest.raises(PermissionError, match="cannot write"):
        reject_ai_write(KnowledgeCandidateV1("A model proposal"))


def test_source_change_changes_only_affected_record_and_store_is_separate(tmp_path) -> None:
    # A tiny committed-like repository source snapshot, not a runtime or research DB.
    from app.knowledge import SOURCE_ALLOWLIST

    for path in SOURCE_ALLOWLIST:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(path, encoding="utf-8")
    before = initial_records(tmp_path, SNAPSHOT)
    (tmp_path / "docs/research/CHAPPIE_AI.md").write_text("changed", encoding="utf-8")
    after = initial_records(tmp_path, SNAPSHOT)
    changed = [left.title for left, right in zip(before, after, strict=True) if left != right]
    assert changed == [
        "Chappie AI provider abstraction",
        "Generic OI predicate limitation",
        "AI confidence limitation",
        "Semantic retrieval backlog",
    ]
    # An unrelated allowlisted source is not referenced by the initial record set.
    assert before[2] == after[2]
    path = tmp_path / "isolated" / "knowledge.json"
    write_store(path, after, manifest(after, SNAPSHOT))
    assert path.is_file() and path != tmp_path / "data/chappie-paper.sqlite3"
