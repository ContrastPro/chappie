from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from app.repository_inventory import (
    POLICY_DIGEST,
    REPOSITORY_INVENTORY_IDENTITY,
    KnowledgeSourceMetadataV1,
    ReferenceCoverageV1,
    RepositoryFileKindV1,
    RepositoryInventoryRequestV1,
    RepositoryInventoryStateV1,
    RepositoryRelationKindV1,
    build_repository_inventory,
    builder,
    forbidden_tracked_path_category,
    git_source,
    repository_inventory_policy_payload,
)
from app.repository_inventory.models import semantic_digest

ALL_RELATIONS = tuple(RepositoryRelationKindV1)


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(("git", *args), cwd=root, text=True).strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    subprocess.run(("git", "init", "-q"), cwd=tmp_path, check=True)
    subprocess.run(
        ("git", "config", "user.email", "inventory@example.invalid"), cwd=tmp_path, check=True
    )
    subprocess.run(("git", "config", "user.name", "Inventory Test"), cwd=tmp_path, check=True)
    paths = {
        "app/example/__init__.py": "from pathlib import Path\nclass Example:\n    pass\ndef run():\n    return Path('.')\n",
        "tests/test_example.py": "from app.example import Example\n\ndef test_example():\n    assert Example\n",
        "tests/test_architecture_boundaries.py": 'TARGET = "app/example/__init__.py"\n',
        "docs/architecture/EXAMPLE.md": "[source](../../app/example/__init__.py)\n",
        "README.md": "Repository fixture.\n",
        "pyproject.toml": "[project]\nname='example'\nversion='1'\n[project.scripts]\nexample='app.example:run'\n",
        "tools/vscode-chappie/package.json": '{"scripts":{"compile":"tsc"},"exports":{".":"./out.js"},"contributes":{"commands":[{"command":"example.open"}]}}',
        "tools/vscode-chappie/src/index.ts": "import { join } from 'path'; export { join };\n",
        "tools/vscode-chappie/dist/SHA256SUMS": f"{'0' * 64}  example.vsix\n",
        ".gitignore": "ignored.txt\n",
    }
    for name, value in paths.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value, encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("secret", encoding="utf-8")
    subprocess.run(("git", "add", "."), cwd=tmp_path, check=True)
    subprocess.run(("git", "commit", "-qm", "fixture"), cwd=tmp_path, check=True)
    (tmp_path / "untracked.txt").write_text("local", encoding="utf-8")
    return tmp_path


def _request(root: Path) -> RepositoryInventoryRequestV1:
    return RepositoryInventoryRequestV1(
        source_commit=_git(root, "rev-parse", "HEAD"),
        requested_relation_kinds=ALL_RELATIONS,
        requesting_context_digest="1" * 64,
        knowledge_identity="chappie_knowledge/1.0.0",
        knowledge_manifest_digest="2" * 64,
    )


def test_complete_inventory_is_deterministic_and_tracked_only(repository: Path) -> None:
    request = _request(repository)
    first = build_repository_inventory(request, repository)
    second = build_repository_inventory(request, repository)
    assert first == second
    assert first.identity == REPOSITORY_INVENTORY_IDENTITY
    assert first.state is RepositoryInventoryStateV1.COMPLETE
    assert first.inventory_digest == second.inventory_digest
    paths = {item.path for item in first.files}
    assert "ignored.txt" not in paths and "untracked.txt" not in paths
    assert first.counts["tracked_files"] == len(paths)
    assert "D:\\" not in str(first.semantic_payload())
    assert "timestamp" not in first.semantic_payload()


def test_source_binding_and_cleanliness_fail_closed(repository: Path) -> None:
    request = _request(repository)
    assert (
        build_repository_inventory(replace(request, source_commit="f" * 40), repository).state
        is RepositoryInventoryStateV1.SOURCE_COMMIT_MISMATCH
    )
    (repository / "app/example/__init__.py").write_text("dirty", encoding="utf-8")
    result = build_repository_inventory(request, repository)
    assert result.state is RepositoryInventoryStateV1.REPOSITORY_NOT_CLEAN
    assert result.inventory_digest is None


def test_taxonomy_detectors_symbols_and_direct_relations(repository: Path) -> None:
    result = build_repository_inventory(_request(repository), repository)
    kinds = {item.path: item.file_kind for item in result.files}
    assert kinds["app/example/__init__.py"] is RepositoryFileKindV1.SOURCE
    assert kinds["tests/test_example.py"] is RepositoryFileKindV1.TEST
    assert kinds["docs/architecture/EXAMPLE.md"] is RepositoryFileKindV1.ARCHITECTURE
    assert kinds["tools/vscode-chappie/src/index.ts"] is RepositoryFileKindV1.EXTENSION
    assert kinds["tools/vscode-chappie/dist/SHA256SUMS"] is RepositoryFileKindV1.DISTRIBUTION
    assert {item.name for item in result.declared_symbols} >= {"Example", "run", "test_example"}
    relation_kinds = {item.relation_kind for item in result.relations}
    assert {
        RepositoryRelationKindV1.IMPORTS,
        RepositoryRelationKindV1.DECLARES,
        RepositoryRelationKindV1.BOOTSTRAPS,
        RepositoryRelationKindV1.EXPORTS,
        RepositoryRelationKindV1.DOCUMENTS,
        RepositoryRelationKindV1.CHECKSUMS,
    } <= relation_kinds
    assert all(item.source_content_digest for item in result.relations)


def test_ownership_has_no_fallback_or_category(repository: Path) -> None:
    result = build_repository_inventory(_request(repository), repository)
    owned = next(item for item in result.files if item.path == "app/example/__init__.py")
    root = next(item for item in result.files if item.path == ".gitignore")
    assert owned.canonical_owner == "app.example"
    assert root.canonical_owner is None
    assert all(item.canonical_owner != "MODULE_RESPONSIBILITY" for item in result.files)


def test_knowledge_relation_uses_metadata_only(repository: Path) -> None:
    source = KnowledgeSourceMetadataV1("record-1", "3" * 64, "app/example/__init__.py")
    result = build_repository_inventory(
        _request(repository), repository, knowledge_sources=(source,)
    )
    relation = next(
        item
        for item in result.relations
        if item.relation_kind is RepositoryRelationKindV1.KNOWLEDGE_SOURCE
    )
    assert relation.target_ref == f"record-1:{'3' * 64}"
    assert "statement" not in str(result.semantic_payload()).lower()
    missing = replace(source, source_path="missing.py")
    assert (
        build_repository_inventory(
            _request(repository), repository, knowledge_sources=(missing,)
        ).state
        is RepositoryInventoryStateV1.INVALID_REPOSITORY_METADATA
    )


def test_secret_like_tracked_path_fails_without_reading(repository: Path) -> None:
    target = repository / ".env"
    target.write_text("DO_NOT_READ", encoding="utf-8")
    subprocess.run(("git", "add", "-f", ".env"), cwd=repository, check=True)
    subprocess.run(("git", "commit", "-qm", "tracked secret"), cwd=repository, check=True)
    result = build_repository_inventory(_request(repository), repository)
    assert result.reason_code == "FORBIDDEN_TRACKED_PATH"
    assert "DO_NOT_READ" not in str(result.semantic_payload())


def test_no_known_reference_is_coverage_not_obsolete(repository: Path) -> None:
    result = build_repository_inventory(_request(repository), repository)
    assert any(
        item.reference_coverage is ReferenceCoverageV1.NO_KNOWN_REFERENCE for item in result.files
    )
    payload = str(result.semantic_payload()).upper()
    assert "UNUSED" not in payload and "OBSOLETE" not in payload and "SAFE_TO_DELETE" not in payload


def test_semantic_payload_excludes_source_bodies(repository: Path) -> None:
    payload = str(build_repository_inventory(_request(repository), repository).semantic_payload())
    assert "return Path('.')" not in payload
    assert "Repository fixture." not in payload


def test_architecture_literal_is_a_direct_reference(repository: Path) -> None:
    result = build_repository_inventory(_request(repository), repository)
    assert any(
        item.relation_kind is RepositoryRelationKindV1.REFERENCES
        and item.source_path == "tests/test_architecture_boundaries.py"
        and item.target_ref == "app/example/__init__.py"
        for item in result.relations
    )


def test_per_file_evidence_budget_fails_closed(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(builder, "MAX_REFERENCES_PER_FILE", 0)
    result = build_repository_inventory(_request(repository), repository)
    assert result.state is RepositoryInventoryStateV1.RELATION_BUDGET_EXCEEDED
    assert result.reason_code == "PER_FILE_EVIDENCE_BUDGET_EXCEEDED"
    assert result.inventory_digest is None


def test_safe_path_validation_rejects_traversal() -> None:
    source = KnowledgeSourceMetadataV1("record-1", "3" * 64, "app/example/__init__.py")
    with pytest.raises(ValueError):
        replace(source, source_path="../outside")


@pytest.mark.parametrize(
    "path",
    (
        ".env.example",
        ".env.sample",
        ".env.template",
        "app/runtime/__init__.py",
        "app/runtime/service.py",
        "app/research/runtime_adapter.py",
        "app/research/data_loader.py",
        "app/research/log_parser.py",
    ),
)
def test_safe_templates_and_source_names_are_allowed(path: str) -> None:
    assert forbidden_tracked_path_category(path) is None


@pytest.mark.parametrize(
    ("path", "category"),
    (
        (".env", "SECRET_ENV_FILE"),
        (".env.local", "SECRET_ENV_FILE"),
        (".env.production", "SECRET_ENV_FILE"),
        (".env.development", "SECRET_ENV_FILE"),
        (".env.production.local", "SECRET_ENV_FILE"),
        ("credentials.json", "CREDENTIAL_FILE"),
        ("config/private_key.pem", "CREDENTIAL_FILE"),
        ("state.db", "LOCAL_STATE_FILE"),
        ("state.sqlite", "LOCAL_STATE_FILE"),
        ("state.sqlite3", "LOCAL_STATE_FILE"),
        ("service.log", "LOCAL_STATE_FILE"),
        ("cache/state.json", "LOCAL_STATE_ROOT"),
        ("runtime/status.json", "LOCAL_STATE_ROOT"),
    ),
)
def test_secret_and_local_state_paths_are_rejected(path: str, category: str) -> None:
    assert forbidden_tracked_path_category(path) == category


def test_forbidden_path_is_classified_before_content_read(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = repository / ".env.local"
    target.write_text("NEVER_READ", encoding="utf-8")
    subprocess.run(("git", "add", "-f", ".env.local"), cwd=repository, check=True)
    subprocess.run(("git", "commit", "-qm", "tracked local env"), cwd=repository, check=True)

    def forbidden_read(_self: Path) -> bytes:
        raise AssertionError("tracked secret content must not be read")

    monkeypatch.setattr(Path, "read_bytes", forbidden_read)
    result = build_repository_inventory(_request(repository), repository)
    assert result.reason_code == "FORBIDDEN_TRACKED_PATH"


def test_policy_digest_is_deterministic_and_semantics_bound() -> None:
    payload = repository_inventory_policy_payload()
    assert POLICY_DIGEST == semantic_digest(payload)
    assert POLICY_DIGEST == semantic_digest(repository_inventory_policy_payload())
    changed = {**payload, "env_rule": "different"}
    assert semantic_digest(changed) != POLICY_DIGEST


def test_exact_proven_sqlite_fixture_is_allowed_and_classified_as_test(
    repository: Path,
) -> None:
    fixture_path = "tests/fixtures/m4_10_recovery_proof_v6.sqlite3"
    fixture = repository / fixture_path
    fixture.parent.mkdir(parents=True, exist_ok=True)
    fixture.write_bytes(b"deterministic fixture")
    consumer = repository / "tests/test_m410_recovery_proof.py"
    consumer.write_text('FIXTURE = "m4_10_recovery_proof_v6.sqlite3"\n', encoding="utf-8")
    subprocess.run(("git", "add", "."), cwd=repository, check=True)
    subprocess.run(("git", "commit", "-qm", "fixture evidence"), cwd=repository, check=True)

    result = build_repository_inventory(_request(repository), repository)

    assert result.state is RepositoryInventoryStateV1.COMPLETE
    record = next(item for item in result.files if item.path == fixture_path)
    assert record.file_kind is RepositoryFileKindV1.TEST
    assert any(
        item.relation_kind is RepositoryRelationKindV1.USES_FIXTURE
        and item.source_path == "tests/test_m410_recovery_proof.py"
        and item.target_ref == fixture_path
        for item in result.relations
    )


def test_sqlite_fixture_requires_exact_path_and_direct_consumer_evidence(
    repository: Path,
) -> None:
    assert (
        forbidden_tracked_path_category(
            "tests/fixtures/m4_10_recovery_proof_v6.sqlite3",
            proven_test_fixture=True,
        )
        is None
    )
    assert (
        forbidden_tracked_path_category(
            "other/fixtures/m4_10_recovery_proof_v6.sqlite3",
            proven_test_fixture=True,
        )
        == "LOCAL_STATE_FILE"
    )

    fixture = repository / "tests/fixtures/m4_10_recovery_proof_v6.sqlite3"
    fixture.parent.mkdir(parents=True, exist_ok=True)
    fixture.write_bytes(b"unreferenced")
    subprocess.run(("git", "add", "."), cwd=repository, check=True)
    subprocess.run(("git", "commit", "-qm", "unreferenced fixture"), cwd=repository, check=True)
    result = build_repository_inventory(_request(repository), repository)
    assert result.reason_code == "FORBIDDEN_TRACKED_PATH"


def test_parse_failure_and_unsupported_coverage(repository: Path) -> None:
    (repository / "app/example/__init__.py").write_text("def broken(:", encoding="utf-8")
    (repository / "blob.bin").write_bytes(b"\xff\x00")
    subprocess.run(("git", "add", "."), cwd=repository, check=True)
    subprocess.run(("git", "commit", "-qm", "coverage"), cwd=repository, check=True)
    result = build_repository_inventory(_request(repository), repository)
    coverage = {item.path: item.reference_coverage for item in result.files}
    assert coverage["app/example/__init__.py"] is ReferenceCoverageV1.PARSE_FAILED
    assert coverage["blob.bin"] is ReferenceCoverageV1.UNSUPPORTED_FILE_KIND


def test_fixed_git_argv_and_no_filesystem_fallback(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []
    real = subprocess.run

    def recording(argv, **kwargs):
        calls.append(tuple(argv))
        return real(argv, **kwargs)

    monkeypatch.setattr(git_source.subprocess, "run", recording)
    git_source.read_tracked_snapshot(repository)
    assert calls == [
        ("git", "rev-parse", "HEAD"),
        ("git", "status", "--porcelain=v1", "--untracked-files=no"),
        ("git", "ls-files", "-z", "--cached"),
    ]
    assert all("--others" not in call for call in calls)


@pytest.mark.parametrize(
    ("attribute", "state"),
    [
        ("MAX_TRACKED_FILES", RepositoryInventoryStateV1.FILE_BUDGET_EXCEEDED),
        ("MAX_TOTAL_FILE_BYTES", RepositoryInventoryStateV1.BYTE_BUDGET_EXCEEDED),
        ("MAX_RELATIONS", RepositoryInventoryStateV1.RELATION_BUDGET_EXCEEDED),
        ("MAX_SEMANTIC_ARTIFACT_BYTES", RepositoryInventoryStateV1.SERIALIZATION_BUDGET_EXCEEDED),
    ],
)
def test_hard_budgets_fail_closed(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    attribute: str,
    state: RepositoryInventoryStateV1,
) -> None:
    monkeypatch.setattr(builder, attribute, 0)
    result = build_repository_inventory(_request(repository), repository)
    assert result.state is state
    assert result.inventory_digest is None


def test_request_rejects_unbounded_or_invalid_inputs(repository: Path) -> None:
    request = _request(repository)
    with pytest.raises(ValueError):
        replace(request, requesting_context_digest="bad")
    with pytest.raises(ValueError):
        replace(request, required_capability="FILESYSTEM")
