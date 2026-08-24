"""Deterministic builder for bounded tracked-repository facts."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import tomllib
from pathlib import Path, PurePosixPath

from app.repository_inventory.git_source import GitSourceError, read_tracked_snapshot
from app.repository_inventory.models import (
    APPROVED_TRACKED_FIXTURE_EVIDENCE,
    FORBIDDEN_CREDENTIAL_PARTS,
    FORBIDDEN_LOCAL_STATE_ROOTS,
    FORBIDDEN_LOCAL_STATE_SUFFIXES,
    FORBIDDEN_PRIVATE_KEY_NAMES,
    FORBIDDEN_PRIVATE_KEY_SUFFIXES,
    MAX_DECLARED_SYMBOLS,
    MAX_DECLARED_SYMBOLS_PER_FILE,
    MAX_PARSEABLE_FILE_BYTES,
    MAX_REFERENCES_PER_FILE,
    MAX_RELATIONS,
    MAX_SEMANTIC_ARTIFACT_BYTES,
    MAX_SINGLE_FILE_BYTES,
    MAX_TOTAL_FILE_BYTES,
    MAX_TRACKED_FILES,
    POLICY_DIGEST,
    SAFE_ENV_TEMPLATE_NAMES,
    ArtifactClassificationV1,
    KnowledgeSourceMetadataV1,
    ReferenceCoverageV1,
    RepositoryDeclaredSymbolV1,
    RepositoryDirectRelationV1,
    RepositoryFileKindV1,
    RepositoryFileRecordV1,
    RepositoryInventoryCompletenessV1,
    RepositoryInventoryRequestV1,
    RepositoryInventoryStateV1,
    RepositoryInventoryV1,
    RepositoryRelationKindV1,
    RepositoryTargetKindV1,
    canonical_json,
    semantic_digest,
)

_SOURCE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx"}
_DOC_SUFFIXES = {".md", ".rst", ".txt"}
_CONFIG_NAMES = {
    "pyproject.toml",
    "package.json",
    "package-lock.json",
    "ruff.toml",
    "pytest.ini",
    ".gitattributes",
    ".gitignore",
}
_STATIC_IMPORT = re.compile(
    r"(?:from\s+|import\s*(?:\(|)\s*)[\"']([^\"']+)[\"']|require\(\s*[\"']([^\"']+)[\"']\s*\)"
)
_MD_LINK = re.compile(r"\[[^\]]*\]\(([^)#]+)(?:#[^)]+)?\)")
_LITERAL_PATH = re.compile(r"[\"']((?:app|tests|docs|tools)/[^\"']+)[\"']")


def forbidden_tracked_path_category(path: str, *, proven_test_fixture: bool = False) -> str | None:
    """Classify unsafe tracked paths using exact repository-relative semantics."""

    normalized = PurePosixPath(path)
    parts = tuple(part.casefold() for part in normalized.parts)
    name = normalized.name.casefold()
    suffix = normalized.suffix.casefold()
    if name in SAFE_ENV_TEMPLATE_NAMES:
        return None
    if name == ".env" or name.startswith(".env."):
        return "SECRET_ENV_FILE"
    if parts and parts[0] in FORBIDDEN_LOCAL_STATE_ROOTS:
        return "LOCAL_STATE_ROOT"
    if suffix in FORBIDDEN_LOCAL_STATE_SUFFIXES:
        if proven_test_fixture and len(parts) == 3 and parts[:2] == ("tests", "fixtures"):
            return None
        return "LOCAL_STATE_FILE"
    if any(part in FORBIDDEN_CREDENTIAL_PARTS for part in parts):
        return "CREDENTIAL_PATH"
    stem = normalized.stem.casefold()
    if (
        suffix in FORBIDDEN_PRIVATE_KEY_SUFFIXES
        or name in FORBIDDEN_PRIVATE_KEY_NAMES
        or stem in {"credentials", "credential", "secrets", "secret", "tokens", "token"}
        or stem.endswith(("_credentials", "_credential", "_private_key", "_api_key"))
    ):
        return "CREDENTIAL_FILE"
    return None


def _verified_test_fixture_evidence(root: Path, tracked: set[str]) -> dict[str, str]:
    verified: dict[str, str] = {}
    for fixture_path, consumer_path in APPROVED_TRACKED_FIXTURE_EVIDENCE:
        if fixture_path not in tracked or consumer_path not in tracked:
            continue
        consumer = (root / Path(*PurePosixPath(consumer_path).parts)).resolve(strict=True)
        if consumer != root and root not in consumer.parents:
            continue
        if consumer.stat().st_size > MAX_PARSEABLE_FILE_BYTES:
            continue
        try:
            text = consumer.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if PurePosixPath(fixture_path).name in text:
            verified[fixture_path] = consumer_path
    return verified


def _taxonomy(path: str) -> tuple[RepositoryFileKindV1, ArtifactClassificationV1]:
    p = PurePosixPath(path)
    suffix = p.suffix.lower()
    lower = path.lower()
    if "/dist/" in f"/{lower}" or suffix in {".vsix", ".whl"} or p.name == "SHA256SUMS":
        return RepositoryFileKindV1.DISTRIBUTION, ArtifactClassificationV1.DECLARED_DISTRIBUTION
    if any(part in {"generated", "build"} for part in p.parts):
        return RepositoryFileKindV1.GENERATED, ArtifactClassificationV1.DECLARED_GENERATED
    if lower.startswith("docs/architecture/") or p.name in {"ARCHITECTURE.md"}:
        return RepositoryFileKindV1.ARCHITECTURE, ArtifactClassificationV1.DECLARED_SOURCE
    if lower.startswith("tests/") or "fixtures" in p.parts:
        return RepositoryFileKindV1.TEST, ArtifactClassificationV1.DECLARED_SOURCE
    if lower.startswith("tools/acceptance/"):
        return RepositoryFileKindV1.ACCEPTANCE, ArtifactClassificationV1.DECLARED_SOURCE
    if lower.startswith("tools/vscode-chappie/"):
        return RepositoryFileKindV1.EXTENSION, ArtifactClassificationV1.DECLARED_SOURCE
    if lower.startswith("app/") and suffix in _SOURCE_SUFFIXES:
        return RepositoryFileKindV1.SOURCE, ArtifactClassificationV1.DECLARED_SOURCE
    if p.name in _CONFIG_NAMES or suffix in {".toml", ".json", ".yaml", ".yml"}:
        return RepositoryFileKindV1.CONFIG, ArtifactClassificationV1.DECLARED_SOURCE
    if lower.startswith("tools/") or p.name.lower().startswith(("setup", "bootstrap")):
        return RepositoryFileKindV1.TOOLING, ArtifactClassificationV1.DECLARED_SOURCE
    if suffix in _DOC_SUFFIXES:
        return RepositoryFileKindV1.DOC, ArtifactClassificationV1.DECLARED_SOURCE
    return RepositoryFileKindV1.OTHER, ArtifactClassificationV1.UNCLASSIFIED


def _owner(path: str) -> tuple[str | None, str | None]:
    parts = PurePosixPath(path).parts
    if len(parts) >= 2 and parts[0] == "app" and parts[1].isidentifier():
        return f"app.{parts[1]}", "PATH_PACKAGE"
    return None, None


def _relation(kind, path, target_kind, target, evidence, locator, digest):
    return RepositoryDirectRelationV1(
        kind, path, target_kind, target[:256], evidence, locator[:256], digest
    )


def _detect(path: str, data: bytes, digest: str):
    symbols: list[RepositoryDeclaredSymbolV1] = []
    relations: list[RepositoryDirectRelationV1] = []
    suffix = PurePosixPath(path).suffix.lower()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return symbols, relations, ReferenceCoverageV1.UNSUPPORTED_FILE_KIND
    try:
        if suffix == ".py":
            tree = ast.parse(text)
            for node in tree.body:
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    kind = "CLASS" if isinstance(node, ast.ClassDef) else "FUNCTION"
                    symbols.append(
                        RepositoryDeclaredSymbolV1(path, node.name, kind, f"line:{node.lineno}")
                    )
                    relations.append(
                        _relation(
                            RepositoryRelationKindV1.DECLARES,
                            path,
                            RepositoryTargetKindV1.SYMBOL,
                            node.name,
                            "PYTHON_AST",
                            f"line:{node.lineno}",
                            digest,
                        )
                    )
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = (
                        [a.name for a in node.names]
                        if isinstance(node, ast.Import)
                        else [node.module or ""]
                    )
                    for name in names:
                        if name:
                            relations.append(
                                _relation(
                                    RepositoryRelationKindV1.IMPORTS,
                                    path,
                                    RepositoryTargetKindV1.MODULE,
                                    name,
                                    "PYTHON_AST",
                                    f"line:{node.lineno}",
                                    digest,
                                )
                            )
                            if path.startswith("tests/"):
                                relations.append(
                                    _relation(
                                        RepositoryRelationKindV1.TESTS,
                                        path,
                                        RepositoryTargetKindV1.MODULE,
                                        name,
                                        "PYTHON_AST",
                                        f"line:{node.lineno}",
                                        digest,
                                    )
                                )
            if path.startswith("tests/"):
                for item in _LITERAL_PATH.findall(text):
                    relations.append(
                        _relation(
                            RepositoryRelationKindV1.TESTS,
                            path,
                            RepositoryTargetKindV1.FILE,
                            item,
                            "LITERAL_PATH",
                            "test source literal",
                            digest,
                        )
                    )
            if path == "tests/test_architecture_boundaries.py":
                for item in _LITERAL_PATH.findall(text):
                    relations.append(
                        _relation(
                            RepositoryRelationKindV1.REFERENCES,
                            path,
                            RepositoryTargetKindV1.FILE,
                            item,
                            "ARCHITECTURE_LITERAL",
                            "literal path",
                            digest,
                        )
                    )
        elif suffix in {".ts", ".tsx", ".js", ".jsx"}:
            for match in _STATIC_IMPORT.finditer(text):
                target = match.group(1) or match.group(2)
                relations.append(
                    _relation(
                        RepositoryRelationKindV1.IMPORTS,
                        path,
                        RepositoryTargetKindV1.MODULE,
                        target,
                        "STATIC_IMPORT",
                        f"offset:{match.start()}",
                        digest,
                    )
                )
        elif PurePosixPath(path).name == "pyproject.toml":
            value = tomllib.loads(text)
            for name, target in sorted(value.get("project", {}).get("scripts", {}).items()):
                relations.append(
                    _relation(
                        RepositoryRelationKindV1.BOOTSTRAPS,
                        path,
                        RepositoryTargetKindV1.SCRIPT,
                        f"{name}={target}",
                        "TOML_SCRIPT",
                        f"project.scripts.{name}",
                        digest,
                    )
                )
        elif PurePosixPath(path).name == "package.json":
            value = json.loads(text)
            for section, kind in (
                ("scripts", RepositoryRelationKindV1.BOOTSTRAPS),
                ("exports", RepositoryRelationKindV1.EXPORTS),
            ):
                values = value.get(section, {})
                if isinstance(values, dict):
                    for name in sorted(values):
                        relations.append(
                            _relation(
                                kind,
                                path,
                                RepositoryTargetKindV1.SCRIPT,
                                name,
                                "PACKAGE_JSON",
                                f"$.{section}.{name}",
                                digest,
                            )
                        )
            for item in value.get("contributes", {}).get("commands", []):
                if isinstance(item, dict) and isinstance(item.get("command"), str):
                    relations.append(
                        _relation(
                            RepositoryRelationKindV1.EXPORTS,
                            path,
                            RepositoryTargetKindV1.CONTRACT,
                            item["command"],
                            "PACKAGE_JSON",
                            "$.contributes.commands",
                            digest,
                        )
                    )
            for section in ("dependencies", "devDependencies"):
                values = value.get(section, {})
                if isinstance(values, dict):
                    for name in sorted(values):
                        relations.append(
                            _relation(
                                RepositoryRelationKindV1.PACKAGES,
                                path,
                                RepositoryTargetKindV1.MODULE,
                                name,
                                "PACKAGE_JSON",
                                f"$.{section}.{name}",
                                digest,
                            )
                        )
        elif suffix == ".md":
            for match in _MD_LINK.finditer(text):
                target = match.group(1).replace("\\", "/")
                if not target.startswith(("http:", "https:", "mailto:", "/")):
                    relations.append(
                        _relation(
                            RepositoryRelationKindV1.DOCUMENTS,
                            path,
                            RepositoryTargetKindV1.FILE,
                            target,
                            "MARKDOWN_LINK",
                            f"offset:{match.start()}",
                            digest,
                        )
                    )
        elif PurePosixPath(path).name == "SHA256SUMS":
            for index, line in enumerate(text.splitlines(), 1):
                parts = line.split()
                if len(parts) >= 2 and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
                    relations.append(
                        _relation(
                            RepositoryRelationKindV1.CHECKSUMS,
                            path,
                            RepositoryTargetKindV1.FILE,
                            parts[-1].lstrip("*"),
                            "SHA256SUMS",
                            f"line:{index}",
                            digest,
                        )
                    )
        else:
            return symbols, relations, ReferenceCoverageV1.NOT_APPLICABLE
    except (SyntaxError, ValueError, TypeError, json.JSONDecodeError, tomllib.TOMLDecodeError):
        return [], [], ReferenceCoverageV1.PARSE_FAILED
    if len(symbols) > MAX_DECLARED_SYMBOLS_PER_FILE or len(relations) > MAX_REFERENCES_PER_FILE:
        return [], [], ReferenceCoverageV1.BUDGET_EXCEEDED
    return (
        symbols,
        relations,
        ReferenceCoverageV1.COMPLETE if relations else ReferenceCoverageV1.NO_KNOWN_REFERENCE,
    )


def _failed(request, state, reason, *, clean=False, counts=None):
    return RepositoryInventoryV1(
        state,
        RepositoryInventoryCompletenessV1.INCOMPLETE,
        reason,
        request.source_commit,
        clean,
        request.scope,
        request.policy_identity,
        POLICY_DIGEST,
        request.knowledge_identity,
        request.knowledge_manifest_digest,
        (),
        (),
        (),
        {},
        counts or {},
        ("UNTRACKED", "IGNORED", "SECRET_CONTENT", "SOURCE_BODIES"),
        RepositoryInventoryCompletenessV1.INCOMPLETE,
        RepositoryInventoryCompletenessV1.INCOMPLETE,
        None,
    )


def build_repository_inventory(
    request: RepositoryInventoryRequestV1,
    repository_root: Path,
    *,
    knowledge_sources: tuple[KnowledgeSourceMetadataV1, ...] = (),
) -> RepositoryInventoryV1:
    try:
        root = repository_root.resolve(strict=True)
        snapshot = read_tracked_snapshot(repository_root)
    except (GitSourceError, OSError, ValueError, TimeoutError):
        return _failed(
            request,
            RepositoryInventoryStateV1.INVALID_REPOSITORY_METADATA,
            "INVALID_REPOSITORY_METADATA",
        )
    if snapshot.commit != request.source_commit:
        return _failed(
            request,
            RepositoryInventoryStateV1.SOURCE_COMMIT_MISMATCH,
            "SOURCE_COMMIT_MISMATCH",
            clean=snapshot.clean,
        )
    if request.require_clean_repository and not snapshot.clean:
        return _failed(
            request, RepositoryInventoryStateV1.REPOSITORY_NOT_CLEAN, "REPOSITORY_NOT_CLEAN"
        )
    if len(snapshot.paths) > MAX_TRACKED_FILES:
        return _failed(
            request,
            RepositoryInventoryStateV1.FILE_BUDGET_EXCEEDED,
            "FILE_BUDGET_EXCEEDED",
            clean=True,
            counts={"tracked_files": len(snapshot.paths)},
        )
    tracked = set(snapshot.paths)
    fixture_evidence = _verified_test_fixture_evidence(root, tracked)
    for path in snapshot.paths:
        if (
            forbidden_tracked_path_category(path, proven_test_fixture=path in fixture_evidence)
            is not None
        ):
            return _failed(
                request,
                RepositoryInventoryStateV1.INVALID_REPOSITORY_METADATA,
                "FORBIDDEN_TRACKED_PATH",
                clean=True,
            )
    if any(item.source_path not in tracked for item in knowledge_sources):
        return _failed(
            request,
            RepositoryInventoryStateV1.INVALID_REPOSITORY_METADATA,
            "KNOWLEDGE_SOURCE_NOT_TRACKED",
            clean=True,
        )
    files, symbols, relations = [], [], []
    total = 0
    for path in snapshot.paths:
        resolved = (root / Path(*PurePosixPath(path).parts)).resolve(strict=True)
        if resolved != root and root not in resolved.parents:
            return _failed(
                request,
                RepositoryInventoryStateV1.INVALID_REPOSITORY_METADATA,
                "UNSAFE_TRACKED_PATH",
                clean=True,
            )
        size = resolved.stat().st_size
        total += size
        if size > MAX_SINGLE_FILE_BYTES or total > MAX_TOTAL_FILE_BYTES:
            return _failed(
                request,
                RepositoryInventoryStateV1.BYTE_BUDGET_EXCEEDED,
                "BYTE_BUDGET_EXCEEDED",
                clean=True,
                counts={"bytes": total},
            )
        content = resolved.read_bytes()
        content_digest = hashlib.sha256(content).hexdigest()
        kind, artifact = _taxonomy(path)
        owner, owner_evidence = _owner(path)
        module = path[:-3].replace("/", ".") if path.endswith(".py") else None
        if len(content) <= MAX_PARSEABLE_FILE_BYTES and (
            artifact is not ArtifactClassificationV1.DECLARED_DISTRIBUTION
            or PurePosixPath(path).name == "SHA256SUMS"
        ):
            found_symbols, found_relations, coverage = _detect(path, content, content_digest)
        else:
            found_symbols, found_relations, coverage = (
                [],
                [],
                ReferenceCoverageV1.BUDGET_EXCEEDED
                if len(content) > MAX_PARSEABLE_FILE_BYTES
                and kind
                in {
                    RepositoryFileKindV1.SOURCE,
                    RepositoryFileKindV1.TEST,
                    RepositoryFileKindV1.CONFIG,
                }
                else ReferenceCoverageV1.NOT_APPLICABLE,
            )
        if coverage is ReferenceCoverageV1.BUDGET_EXCEEDED:
            return _failed(
                request,
                RepositoryInventoryStateV1.RELATION_BUDGET_EXCEEDED,
                "PER_FILE_EVIDENCE_BUDGET_EXCEEDED",
                clean=True,
                counts={"tracked_files": len(files) + 1},
            )
        if owner and RepositoryRelationKindV1.OWNED_BY in request.requested_relation_kinds:
            found_relations.append(
                _relation(
                    RepositoryRelationKindV1.OWNED_BY,
                    path,
                    RepositoryTargetKindV1.OWNER,
                    owner,
                    "PATH_PACKAGE",
                    "path",
                    content_digest,
                )
            )
        symbols.extend(found_symbols)
        relations.extend(
            item
            for item in found_relations
            if item.relation_kind in request.requested_relation_kinds
        )
        files.append(
            RepositoryFileRecordV1(
                path,
                kind,
                "TRACKED",
                content_digest,
                size,
                owner,
                owner_evidence,
                module,
                artifact,
                coverage,
            )
        )
    for source in knowledge_sources:
        digest = next(item.content_digest for item in files if item.path == source.source_path)
        if RepositoryRelationKindV1.KNOWLEDGE_SOURCE in request.requested_relation_kinds:
            relations.append(
                _relation(
                    RepositoryRelationKindV1.KNOWLEDGE_SOURCE,
                    source.source_path,
                    RepositoryTargetKindV1.KNOWLEDGE_RECORD,
                    f"{source.record_id}:{source.record_digest}",
                    "KNOWLEDGE_METADATA",
                    "source",
                    digest,
                )
            )
    if RepositoryRelationKindV1.USES_FIXTURE in request.requested_relation_kinds:
        file_digests = {item.path: item.content_digest for item in files}
        for fixture_path, consumer_path in sorted(fixture_evidence.items()):
            relations.append(
                _relation(
                    RepositoryRelationKindV1.USES_FIXTURE,
                    consumer_path,
                    RepositoryTargetKindV1.FILE,
                    fixture_path,
                    "TRACKED_TEST_FIXTURE",
                    f"literal:{PurePosixPath(fixture_path).name}",
                    file_digests[consumer_path],
                )
            )
    symbols = sorted(
        set(symbols), key=lambda item: (item.path, item.name, item.symbol_kind, item.locator)
    )
    relations = sorted(set(relations), key=lambda item: tuple(item.payload().values()))
    if len(symbols) > MAX_DECLARED_SYMBOLS or len(relations) > MAX_RELATIONS:
        state = RepositoryInventoryStateV1.RELATION_BUDGET_EXCEEDED
        return _failed(
            request,
            state,
            state.value,
            clean=True,
            counts={"declared_symbols": len(symbols), "relations": len(relations)},
        )
    coverage = {
        item.value: sum(file.reference_coverage is item for file in files)
        for item in ReferenceCoverageV1
    }
    counts = {
        "tracked_files": len(files),
        "declared_symbols": len(symbols),
        "relations": len(relations),
        "total_file_bytes": total,
    }
    args = (
        RepositoryInventoryStateV1.COMPLETE,
        RepositoryInventoryCompletenessV1.COMPLETE,
        None,
        snapshot.commit,
        snapshot.clean,
        request.scope,
        request.policy_identity,
        POLICY_DIGEST,
        request.knowledge_identity,
        request.knowledge_manifest_digest,
        tuple(files),
        tuple(symbols),
        tuple(relations),
        coverage,
        counts,
        ("UNTRACKED", "IGNORED", "SECRET_CONTENT", "SOURCE_BODIES"),
        RepositoryInventoryCompletenessV1.COMPLETE,
        RepositoryInventoryCompletenessV1.COMPLETE,
    )
    provisional = RepositoryInventoryV1(*args, "0" * 64)
    payload = provisional.semantic_payload(include_digest=False)
    encoded = canonical_json(payload).encode()
    if len(encoded) > MAX_SEMANTIC_ARTIFACT_BYTES:
        return _failed(
            request,
            RepositoryInventoryStateV1.SERIALIZATION_BUDGET_EXCEEDED,
            "SERIALIZATION_BUDGET_EXCEEDED",
            clean=True,
            counts=counts,
        )
    return RepositoryInventoryV1(*args, semantic_digest(payload))
