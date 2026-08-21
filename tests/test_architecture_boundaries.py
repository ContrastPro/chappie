"""Static guardrails for the approved package-boundary migration plan.

These tests deliberately inspect source and tracked paths instead of exercising
runtime behavior. They are expected to stay cheap and to become stricter as the
flat :mod:`app` namespace shrinks.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import subprocess
import tomllib
from pathlib import Path

from app.ai.market_semantics import MARKET_SEMANTICS_IDENTITY
from app.ai.project_qa import PROJECT_QA_IDENTITY
from app.ai.research_reasoning import RESEARCH_REASONING_IDENTITY, RESEARCH_REASONING_V111_IDENTITY
from app.control_plane import CONTROL_PLANE_CONTRACT_DIGEST, CONTROL_PLANE_IDENTITY
from app.control_plane_transport import (
    CONTROL_PLANE_TRANSPORT_DIGEST,
    CONTROL_PLANE_TRANSPORT_IDENTITY,
)
from app.knowledge import KNOWLEDGE_CONTRACT_DIGEST, KNOWLEDGE_IDENTITY, SOURCE_ALLOWLIST
from app.knowledge_retrieval import RETRIEVAL_CONTRACT_DIGEST, RETRIEVAL_IDENTITY
from app.macos_ollama import MACOS_OLLAMA_POLICY_DIGEST, MACOS_OLLAMA_POLICY_IDENTITY
from app.project_qa_setup_smoke import PROJECT_QA_SETUP_SMOKE_IDENTITY
from app.setup_engine import SETUP_ENGINE_DIGEST, SETUP_ENGINE_IDENTITY
from app.windows_ollama import WINDOWS_OLLAMA_POLICY_DIGEST, WINDOWS_OLLAMA_POLICY_IDENTITY

ROOT = Path(__file__).parents[1]
APP = ROOT / "app"

# This list is intentionally temporary and may only shrink as modules move into
# the approved package tree. cli.py and main.py are stable adapters, not debt.
ROOT_MODULE_ALLOWLIST = frozenset(
    {
        "ai_setup_smoke.py",
        "application.py",
        "bootstrap.py",
        "control_plane.py",
        "control_plane_transport.py",
        "knowledge.py",
        "knowledge_retrieval.py",
        "macos_ollama.py",
        "ollama_readiness.py",
        "ollama_start.py",
        "project_qa_setup_smoke.py",
        "research_methodology.py",
        "server_status.py",
        "setup_engine.py",
        "vscode_integration.py",
        "windows_ollama.py",
        "workspace_context.py",
    }
)
STABLE_ROOT_ADAPTERS = frozenset({"cli.py", "main.py"})

OFFLINE_RESEARCH_PREFIXES = (
    "app.research_",
    "app.research.intelligence",
    "app.research.strategy",
)
RUNTIME_TRADING_PREFIXES = (
    "app.application",
    "app.main",
    "app.server_status",
    "app.agent",
    "app.coordination",
    "app.exchange_runtime",
    "app.execution",
    "app.paper",
    "app.portfolio",
    "app.risk",
    "app.shadow",
    "app.trading",
)
AI_FORBIDDEN_AUTHORITY_PREFIXES = (
    "app.application",
    "app.coordination",
    "app.exchange_execution",
    "app.exchange_runtime",
    "app.execution",
    "app.paper",
    "app.portfolio",
    "app.risk",
    "app.shadow",
    "app.trading",
)

# Future moves are intentionally classified before wrappers are created.
COMPATIBILITY_MAP = {
    "app.cli": "FROZEN_PATH",
    "app.main": "FROZEN_PATH",
    "app.persistence.migrations": "FROZEN_PATH",
    "app.knowledge": "TEMPORARY_COMPATIBILITY_WRAPPER",
    "app.control_plane": "TEMPORARY_COMPATIBILITY_WRAPPER",
    "app.tools.*": "TEMPORARY_COMPATIBILITY_WRAPPER",
}
COMPLETED_DIRECT_MIGRATIONS = {
    "app.research.data.alignment": "COMPLETED_DIRECT_MIGRATION",
    "app.research.data.anomalies": "COMPLETED_DIRECT_MIGRATION",
    "app.research.data.cache_identity": "COMPLETED_DIRECT_MIGRATION",
    "app.research.data.event_bridge": "COMPLETED_DIRECT_MIGRATION",
    "app.research.data.legacy_json": "COMPLETED_DIRECT_MIGRATION",
    "app.research.data.outcomes": "COMPLETED_DIRECT_MIGRATION",
    "app.research.paths": "COMPLETED_DIRECT_MIGRATION",
    "app.research.data.store": "COMPLETED_DIRECT_MIGRATION",
    "app.research.intelligence.behavioral": "COMPLETED_DIRECT_MIGRATION",
    "app.research.intelligence.capital_rotation": "COMPLETED_DIRECT_MIGRATION",
    "app.research.intelligence.flow": "COMPLETED_DIRECT_MIGRATION",
    "app.research.intelligence.leverage_stress": "COMPLETED_DIRECT_MIGRATION",
    "app.research.intelligence.macro_events": "COMPLETED_DIRECT_MIGRATION",
    "app.research.intelligence.market": "COMPLETED_DIRECT_MIGRATION",
    "app.research.intelligence.multi_asset_flow": "COMPLETED_DIRECT_MIGRATION",
    "app.research.intelligence.news_events": "COMPLETED_DIRECT_MIGRATION",
    "app.research.intelligence.universe": "COMPLETED_DIRECT_MIGRATION",
    "app.research.strategy.history": "COMPLETED_DIRECT_MIGRATION",
    "app.research.strategy.lab": "COMPLETED_DIRECT_MIGRATION",
    "app.research.strategy.long_history": "COMPLETED_DIRECT_MIGRATION",
    "app.research.strategy.walk_forward": "COMPLETED_DIRECT_MIGRATION",
}

# Dynamic resolution sites are a bounded compatibility surface during moves.
DYNAMIC_IMPORT_OWNERS = {
    "app.ai.__init__": frozenset(
        {
            "app.ai.adapters",
            "app.ai.research_query_compiler",
            "app.ai.research_reasoning",
            "app.ai.runtime",
        }
    ),
    "app.knowledge_retrieval": frozenset({"app.ai.schemas"}),
    "app.exchange_execution.__init__": frozenset(
        {
            "app.exchange_execution.phase3",
            "app.exchange_execution.stage4_sell",
            "app.exchange_execution.stage4_sell_disposition",
            "app.exchange_execution.stage4_sell_recovery",
        }
    ),
    "app.persistence.sqlite": frozenset({"app.persistence.migrations"}),
}

FROZEN_CONTRACT_SNAPSHOT = {
    "control_plane": (
        "chappie_control_plane/1.2.0",
        "de09b6c1eb917d95cdc53be3581a9500110fc856f0586e87bea88b95bccf3040",
    ),
    "control_plane_transport": (
        "chappie_control_plane_transport/1.2.0",
        "e2328fc9618a4ad95a728b128cdfb0d003e60fcd2b5473c91dbffff053993a96",
    ),
    "knowledge": (
        "chappie_knowledge/1.0.0",
        "697e6160c48d5683e1ba4078dc8a671f73c6df863e068e3a8d1ec729d7500bbb",
    ),
    "knowledge_retrieval": (
        "chappie_knowledge_retrieval/1.0.0",
        "0b9fc2235532822ab0a838da931147c08b77b5b03a5dc2ab637dc16deaca1786",
    ),
    "setup_engine": (
        "chappie_setup_engine/1.0.0",
        "d9bcee88b380bf666b8d81ce349078d8d642849ab1a583bf78801a8d24baeb75",
    ),
    "macos_ollama": (
        "ollama_macos_install_policy/1.0.0",
        "848e8dd219ac9e18a1a731b6c507aec410738bb4c002cc04cc80767f0ad628ce",
    ),
    "windows_ollama": (
        "ollama_windows_install_policy/1.0.0",
        "86891b65df1f2ae42f2cac430f00791c73a46aa6b885f44ea6100c695dcd9109",
    ),
    "project_qa_smoke": ("project_qa_setup_smoke/1.0.0", None),
    "project_qa": ("project_qa/1.0.0", None),
    "research_reasoning": ("research_reasoning/1.1.0", None),
    "research_reasoning_v111": ("research_reasoning/1.1.1", None),
    "market_semantics": ("market_semantics/1.0.0", None),
}

ALLOWED_TRACKED_DISTRIBUTION_ARTIFACTS = frozenset(
    {
        "tools/vscode-chappie/dist/chappie-vscode-0.1.1.vsix",
        "tools/vscode-chappie/dist/SHA256SUMS",
    }
)
ALLOWED_TRACKED_SQLITE_FIXTURES = frozenset({"tests/fixtures/m4_10_recovery_proof_v6.sqlite3"})


def _module_name(path: Path) -> str:
    relative = path.relative_to(ROOT).with_suffix("")
    return ".".join(relative.parts)


def _production_sources() -> tuple[Path, ...]:
    return tuple(path for path in APP.rglob("*.py") if "tools" not in path.parts)


def _imports(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    return tuple(imports)


def _matches(module: str, prefixes: tuple[str, ...]) -> bool:
    return any(module.startswith(prefix) for prefix in prefixes)


def _tracked_files() -> frozenset[str]:
    result = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, check=True, capture_output=True, text=True
    )
    return frozenset(line for line in result.stdout.splitlines() if line)


def test_root_module_allowlist_can_only_shrink() -> None:
    direct_modules = {path.name for path in APP.glob("*.py")}
    approved = ROOT_MODULE_ALLOWLIST | STABLE_ROOT_ADAPTERS | {"__init__.py"}
    assert direct_modules <= approved, sorted(direct_modules - approved)


def test_completed_direct_migrations_have_only_canonical_paths() -> None:
    assert not (APP / "research_alignment.py").exists()
    assert not (APP / "research_anomalies.py").exists()
    assert not (APP / "research_cache_identity.py").exists()
    assert not (APP / "research_data.py").exists()
    assert not (APP / "research_event_bridge.py").exists()
    assert not (APP / "research_json.py").exists()
    assert not (APP / "research_outcomes.py").exists()
    assert not (APP / "research_paths.py").exists()
    assert not (APP / "behavioral_market.py").exists()
    assert not (APP / "capital_rotation.py").exists()
    assert not (APP / "market_intelligence_universe.py").exists()
    assert not (APP / "multi_asset_flow_intelligence.py").exists()
    assert not (APP / "news_event_intelligence.py").exists()
    assert not (APP / "leverage_stress.py").exists()
    assert not (APP / "market_intelligence.py").exists()
    assert not (APP / "market_intelligence_flow.py").exists()
    assert not (APP / "news_event_macro.py").exists()
    assert not (APP / "strategy_history.py").exists()
    assert not (APP / "strategy_lab.py").exists()
    assert not (APP / "strategy_long_history.py").exists()
    assert not (APP / "strategy_walk_forward.py").exists()
    for module in COMPLETED_DIRECT_MIGRATIONS:
        assert importlib.util.find_spec(module) is not None


def test_production_code_does_not_import_tools() -> None:
    violations = {
        _module_name(path): imported
        for path in _production_sources()
        for imported in _imports(path)
        if imported == "app.tools" or imported.startswith("app.tools.")
    }
    assert violations == {}


def test_runtime_trading_code_does_not_import_offline_research() -> None:
    violations = {
        _module_name(path): imported
        for path in _production_sources()
        if _matches(_module_name(path), RUNTIME_TRADING_PREFIXES)
        for imported in _imports(path)
        if _matches(imported, OFFLINE_RESEARCH_PREFIXES)
    }
    assert violations == {}


def test_ai_has_no_execution_or_trading_authority_imports() -> None:
    violations = {
        _module_name(path): imported
        for path in (APP / "ai").rglob("*.py")
        for imported in _imports(path)
        if _matches(imported, AI_FORBIDDEN_AUTHORITY_PREFIXES)
    }
    assert violations == {}


def test_major_frozen_contracts_match_migration_snapshot() -> None:
    actual = {
        "control_plane": (CONTROL_PLANE_IDENTITY, CONTROL_PLANE_CONTRACT_DIGEST),
        "control_plane_transport": (
            CONTROL_PLANE_TRANSPORT_IDENTITY,
            CONTROL_PLANE_TRANSPORT_DIGEST,
        ),
        "knowledge": (KNOWLEDGE_IDENTITY, KNOWLEDGE_CONTRACT_DIGEST),
        "knowledge_retrieval": (RETRIEVAL_IDENTITY, RETRIEVAL_CONTRACT_DIGEST),
        "setup_engine": (SETUP_ENGINE_IDENTITY, SETUP_ENGINE_DIGEST),
        "macos_ollama": (MACOS_OLLAMA_POLICY_IDENTITY, MACOS_OLLAMA_POLICY_DIGEST),
        "windows_ollama": (WINDOWS_OLLAMA_POLICY_IDENTITY, WINDOWS_OLLAMA_POLICY_DIGEST),
        "project_qa_smoke": (PROJECT_QA_SETUP_SMOKE_IDENTITY, None),
        "project_qa": (PROJECT_QA_IDENTITY, None),
        "research_reasoning": (RESEARCH_REASONING_IDENTITY, None),
        "research_reasoning_v111": (RESEARCH_REASONING_V111_IDENTITY, None),
        "market_semantics": (MARKET_SEMANTICS_IDENTITY, None),
    }
    assert actual == FROZEN_CONTRACT_SNAPSHOT


def test_stable_path_contracts_and_console_entrypoint() -> None:
    assert (APP / "cli.py").is_file()
    assert (APP / "main.py").is_file()
    assert (APP / "persistence/migrations/__init__.py").is_file()
    assert (ROOT / "setup.sh").is_file()
    assert (ROOT / "setup.ps1").is_file()
    assert importlib.util.find_spec("app.main") is not None
    assert importlib.util.find_spec("app.persistence.migrations") is not None

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["scripts"]["chappie"] == "app.cli:main"


def test_vscode_distribution_and_workspace_recognition_contracts() -> None:
    dist = ROOT / "tools/vscode-chappie/dist"
    manifest = dist / "SHA256SUMS"
    assert dist.is_dir()
    assert manifest.is_file()
    checksums = [line.split(maxsplit=1) for line in manifest.read_text().splitlines() if line]
    assert len(checksums) == 1
    expected_digest, relative_artifact = checksums[0]
    artifact = ROOT / "tools/vscode-chappie" / relative_artifact
    assert artifact.is_file()
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == expected_digest
    assert artifact.relative_to(ROOT).as_posix() in ALLOWED_TRACKED_DISTRIBUTION_ARTIFACTS

    package = (ROOT / "tools/vscode-chappie/package.json").read_text(encoding="utf-8")
    assert "workspaceContains:**/app/cli.py" in package


def test_knowledge_allowlist_paths_exist_and_identify_move_sensitive_sources() -> None:
    missing = sorted(path for path in SOURCE_ALLOWLIST if not (ROOT / path).is_file())
    assert missing == []
    migration_sensitive = {
        "app/research_methodology.py",
        "app/research/intelligence/universe.py",
        "app/research/intelligence/multi_asset_flow.py",
    }
    assert migration_sensitive <= SOURCE_ALLOWLIST


def test_dynamic_import_inventory_targets_remain_resolvable() -> None:
    for owner, targets in DYNAMIC_IMPORT_OWNERS.items():
        assert importlib.util.find_spec(owner) is not None
        for target in targets:
            assert importlib.util.find_spec(target) is not None, (owner, target)


def test_dynamic_import_sites_are_bounded_by_the_inventory() -> None:
    observed: set[str] = set()
    for path in _production_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            is_import = isinstance(function, ast.Name) and function.id in {
                "__import__",
                "import_module",
            }
            is_resource_lookup = isinstance(function, ast.Attribute) and function.attr in {
                "files",
                "import_module",
            }
            if is_import or is_resource_lookup:
                observed.add(_module_name(path))
    assert observed == set(DYNAMIC_IMPORT_OWNERS)


def test_compatibility_map_classifies_required_future_surfaces() -> None:
    assert {"FROZEN_PATH", "TEMPORARY_COMPATIBILITY_WRAPPER"} == set(COMPATIBILITY_MAP.values())
    assert COMPATIBILITY_MAP["app.cli"] == "FROZEN_PATH"
    assert COMPATIBILITY_MAP["app.main"] == "FROZEN_PATH"
    assert COMPATIBILITY_MAP["app.persistence.migrations"] == "FROZEN_PATH"
    assert set(COMPLETED_DIRECT_MIGRATIONS.values()) == {"COMPLETED_DIRECT_MIGRATION"}


def test_tracked_generated_artifacts_are_limited_to_explicit_contracts() -> None:
    tracked = _tracked_files()
    forbidden_prefixes = ("data/", "logs/", "node_modules/", "tools/vscode-chappie/out/")
    assert not [path for path in tracked if path.startswith(forbidden_prefixes)]

    vsix_files = {path for path in tracked if path.endswith(".vsix")}
    assert vsix_files == {
        next(
            iter(ALLOWED_TRACKED_DISTRIBUTION_ARTIFACTS - {"tools/vscode-chappie/dist/SHA256SUMS"})
        )
    }

    sqlite_files = {path for path in tracked if path.endswith(".sqlite3")}
    assert sqlite_files == ALLOWED_TRACKED_SQLITE_FIXTURES


def test_boundary_document_records_stable_paths_and_shrinking_policy() -> None:
    document = (ROOT / "docs/architecture/PACKAGE_BOUNDARIES.md").read_text(encoding="utf-8")
    for required in (
        "app.cli",
        "app.main",
        "app.persistence.migrations",
        "tools/vscode-chappie/dist/",
        "may only shrink",
    ):
        assert required in document
