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
from collections.abc import Iterable
from pathlib import Path

from app.ai.project_qa import PROJECT_QA_IDENTITY
from app.ai.research.market_semantics import MARKET_SEMANTICS_IDENTITY
from app.ai.research.reasoning import RESEARCH_REASONING_IDENTITY, RESEARCH_REASONING_V111_IDENTITY
from app.control_plane import CONTROL_PLANE_CONTRACT_DIGEST, CONTROL_PLANE_IDENTITY
from app.control_plane.transport import (
    CONTROL_PLANE_TRANSPORT_DIGEST,
    CONTROL_PLANE_TRANSPORT_IDENTITY,
)
from app.knowledge import KNOWLEDGE_CONTRACT_DIGEST, KNOWLEDGE_IDENTITY, SOURCE_ALLOWLIST
from app.knowledge.retrieval import RETRIEVAL_CONTRACT_DIGEST, RETRIEVAL_IDENTITY
from app.platform.ollama.macos import MACOS_OLLAMA_POLICY_DIGEST, MACOS_OLLAMA_POLICY_IDENTITY
from app.platform.ollama.windows import WINDOWS_OLLAMA_POLICY_DIGEST, WINDOWS_OLLAMA_POLICY_IDENTITY
from app.platform.setup.engine import SETUP_ENGINE_DIGEST, SETUP_ENGINE_IDENTITY
from app.platform.setup.project_qa_smoke import PROJECT_QA_SETUP_SMOKE_IDENTITY

ROOT = Path(__file__).parents[1]
APP = ROOT / "app"


def test_windows_timezone_database_is_a_declared_platform_dependency() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert "tzdata>=2025.2; sys_platform == 'win32'" in project["dependencies"]


# This list is intentionally small and may only shrink. cli.py and main.py are
# stable adapters, not debt.
ROOT_MODULE_ALLOWLIST = frozenset(
    {
        "research_methodology.py",
    }
)
STABLE_ROOT_ADAPTERS = frozenset({"cli.py", "main.py"})

OFFLINE_RESEARCH_PREFIXES = (
    "app.research_",
    "app.research.intelligence",
    "app.research.strategy",
)
RUNTIME_TRADING_PREFIXES = (
    "app.main",
    "app.runtime",
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
    "app.runtime",
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
AI_RESEARCH_FORBIDDEN_PREFIXES = (
    "app.platform",
    "app.control_plane",
    "app.runtime",
    "app.coordination",
    "app.exchange_execution",
    "app.exchange_runtime",
    "app.execution",
    "app.infrastructure",
    "app.market",
    "app.paper",
    "app.persistence",
    "app.portfolio",
    "app.risk",
    "app.shadow",
    "app.strategies",
    "app.tools",
    "app.trading",
)
AI_EVALUATION_FORBIDDEN_PREFIXES = (
    "app.ai.runtime",
    "app.platform",
    "app.control_plane",
    "app.runtime",
    "app.coordination",
    "app.exchange_execution",
    "app.exchange_runtime",
    "app.execution",
    "app.infrastructure",
    "app.market",
    "app.paper",
    "app.persistence",
    "app.portfolio",
    "app.risk",
    "app.shadow",
    "app.strategies",
    "app.tools",
    "app.trading",
)

# Future moves are intentionally classified before wrappers are created.
COMPATIBILITY_MAP = {
    "app.cli": "FROZEN_PATH",
    "app.main": "FROZEN_PATH",
    "app.persistence.migrations": "FROZEN_PATH",
    "app.knowledge": "STABLE_PACKAGE_API",
    "app.knowledge.store": "CANONICAL_IMPLEMENTATION",
    "app.knowledge.retrieval": "CANONICAL_IMPLEMENTATION",
    "app.control_plane": "STABLE_PACKAGE_API",
    "app.control_plane.service": "CANONICAL_IMPLEMENTATION",
    "app.control_plane.transport": "CANONICAL_IMPLEMENTATION",
    "app.control_plane.workspace_context": "CANONICAL_IMPLEMENTATION",
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
    "app.foundation.config": "COMPLETED_DIRECT_MIGRATION",
    "app.foundation.logging": "COMPLETED_DIRECT_MIGRATION",
    "app.control_plane.service": "COMPLETED_DIRECT_MIGRATION",
    "app.control_plane.transport": "COMPLETED_DIRECT_MIGRATION",
    "app.control_plane.workspace_context": "COMPLETED_DIRECT_MIGRATION",
    "app.knowledge.retrieval": "COMPLETED_DIRECT_MIGRATION",
    "app.knowledge.store": "COMPLETED_DIRECT_MIGRATION",
    "app.platform.ollama.macos": "COMPLETED_DIRECT_MIGRATION",
    "app.platform.ollama.readiness": "COMPLETED_DIRECT_MIGRATION",
    "app.platform.ollama.start": "COMPLETED_DIRECT_MIGRATION",
    "app.platform.ollama.windows": "COMPLETED_DIRECT_MIGRATION",
    "app.platform.bootstrap": "COMPLETED_DIRECT_MIGRATION",
    "app.platform.setup.ai_smoke": "COMPLETED_DIRECT_MIGRATION",
    "app.platform.setup.engine": "COMPLETED_DIRECT_MIGRATION",
    "app.platform.setup.project_qa_smoke": "COMPLETED_DIRECT_MIGRATION",
    "app.platform.vscode": "COMPLETED_DIRECT_MIGRATION",
    "app.runtime.application": "COMPLETED_DIRECT_MIGRATION",
    "app.runtime.status": "COMPLETED_DIRECT_MIGRATION",
    "app.runtime.trader": "COMPLETED_DIRECT_MIGRATION",
    "app.ai.research.canonical_storage": "COMPLETED_DIRECT_MIGRATION",
    "app.ai.research.capabilities": "COMPLETED_DIRECT_MIGRATION",
    "app.ai.research.evidence_review": "COMPLETED_DIRECT_MIGRATION",
    "app.ai.research.execution_cache": "COMPLETED_DIRECT_MIGRATION",
    "app.ai.research.execution_contracts": "COMPLETED_DIRECT_MIGRATION",
    "app.ai.research.market_semantics": "COMPLETED_DIRECT_MIGRATION",
    "app.ai.research.query_compiler": "COMPLETED_DIRECT_MIGRATION",
    "app.ai.research.query_executor": "COMPLETED_DIRECT_MIGRATION",
    "app.ai.research.reasoning": "COMPLETED_DIRECT_MIGRATION",
    "app.ai.evaluation.benchmark": "COMPLETED_DIRECT_MIGRATION",
    "app.ai.evaluation.closed_loop_calibration": "COMPLETED_DIRECT_MIGRATION",
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
            "app.ai.research.query_compiler",
            "app.ai.research.reasoning",
            "app.ai.runtime",
        }
    ),
    "app.exchange_execution.__init__": frozenset(
        {
            "app.exchange_execution.phase3",
            "app.exchange_execution.stage4_sell",
            "app.exchange_execution.stage4_sell_disposition",
            "app.exchange_execution.stage4_sell_recovery",
        }
    ),
    "app.knowledge.retrieval": frozenset({"app.ai.schemas"}),
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
        "tools/vscode-chappie/dist/chappie-vscode-0.1.13.vsix",
        "tools/vscode-chappie/dist/SHA256SUMS",
    }
)
ALLOWED_TRACKED_SQLITE_FIXTURES = frozenset({"tests/fixtures/m4_10_recovery_proof_v6.sqlite3"})

# Machine-readable ownership policy. ``allowed`` contains top-level internal
# dependency families; third-party and standard-library imports are outside
# this map. Nested owners override their parent policy. ``app.tools`` is the
# only intentionally open consumer because tools are leaf entry points.
PACKAGE_OWNERSHIP = {
    "app.ai": {
        "responsibility": "Provider-neutral structured reasoning and read-only assistance",
        "allowed": frozenset({"app.ai", "app.knowledge", "app.research"}),
    },
    "app.ai.research": {
        "responsibility": "Research reasoning, RDE query, and evidence-review workflows",
        "allowed": frozenset({"app.ai", "app.knowledge", "app.research"}),
    },
    "app.ai.evaluation": {
        "responsibility": "Offline AI benchmarks and calibration",
        "allowed": frozenset({"app.ai", "app.research"}),
    },
    "app.control_plane": {
        "responsibility": "Read-only local application projection and loopback transport",
        "allowed": frozenset({"app.ai", "app.control_plane", "app.knowledge", "app.platform"}),
    },
    "app.coordination": {
        "responsibility": "Cross-service trading workflow coordination",
        "allowed": frozenset(
            {
                "app.coordination",
                "app.exchange_runtime",
                "app.market",
                "app.paper",
                "app.shadow",
                "app.trading",
            }
        ),
    },
    "app.exchange_execution": {
        "responsibility": "Durable exchange execution lifecycle and reconciliation",
        "allowed": frozenset({"app.exchange_execution", "app.market", "app.persistence"}),
    },
    "app.exchange_runtime": {
        "responsibility": "Exchange-mode application services and composition",
        "allowed": frozenset(
            {
                "app.exchange_execution",
                "app.exchange_runtime",
                "app.execution",
                "app.foundation",
                "app.infrastructure",
                "app.market",
                "app.persistence",
                "app.portfolio",
                "app.risk",
                "app.trading",
            }
        ),
    },
    "app.execution": {
        "responsibility": "Execution contracts, providers, and paper execution boundary",
        "allowed": frozenset(
            {"app.execution", "app.market", "app.portfolio", "app.risk", "app.trading"}
        ),
    },
    "app.foundation": {
        "responsibility": "Neutral configuration and logging primitives",
        "allowed": frozenset({"app.foundation"}),
    },
    "app.infrastructure": {
        "responsibility": "External-system adapters implementing bounded ports",
        "allowed": frozenset({"app.exchange_execution", "app.infrastructure", "app.market"}),
    },
    "app.knowledge": {
        "responsibility": "Clean-snapshot provenance store and deterministic retrieval",
        "allowed": frozenset({"app.ai", "app.knowledge"}),
    },
    "app.market": {
        "responsibility": "Market data contracts, validation, events, and provider ports",
        "allowed": frozenset({"app.market"}),
    },
    "app.paper": {
        "responsibility": "Paper-trading application service",
        "allowed": frozenset(
            {
                "app.execution",
                "app.paper",
                "app.persistence",
                "app.portfolio",
                "app.risk",
                "app.trading",
            }
        ),
    },
    "app.persistence": {
        "responsibility": "Durable repositories, recovery, and migration resources",
        "allowed": frozenset(
            {
                "app.exchange_execution",
                "app.execution",
                "app.market",
                "app.persistence",
                "app.portfolio",
                "app.trading",
            }
        ),
    },
    "app.platform": {
        "responsibility": "Host setup, provider installation, and workstation integration",
        "allowed": frozenset({"app.ai", "app.knowledge", "app.platform"}),
    },
    "app.platform.ollama": {
        "responsibility": "Shared and OS-specific Ollama adapters",
        "allowed": frozenset({"app.ai", "app.platform"}),
    },
    "app.platform.setup": {
        "responsibility": "Setup orchestration and bounded verification smokes",
        "allowed": frozenset({"app.ai", "app.knowledge", "app.platform"}),
    },
    "app.portfolio": {
        "responsibility": "Portfolio state and accounting domain",
        "allowed": frozenset({"app.execution", "app.market", "app.portfolio", "app.trading"}),
    },
    "app.research": {
        "responsibility": "Offline reproducible research and canonical data paths",
        "allowed": frozenset(
            {
                "app.ai",
                "app.market",
                "app.research",
                "app.research_methodology",
                "app.strategies",
                "app.trading",
            }
        ),
    },
    "app.research.data": {
        "responsibility": "Research storage, alignment, outcomes, and provenance identities",
        "allowed": frozenset({"app.ai", "app.market", "app.research", "app.research_methodology"}),
    },
    "app.research.intelligence": {
        "responsibility": "Offline market, flow, event, and behavioral intelligence studies",
        "allowed": frozenset(
            {
                "app.ai",
                "app.market",
                "app.research",
                "app.research_methodology",
                "app.strategies",
                "app.trading",
            }
        ),
    },
    "app.research.strategy": {
        "responsibility": "Offline strategy history, lab, and walk-forward evaluation",
        "allowed": frozenset({"app.market", "app.research", "app.strategies", "app.trading"}),
    },
    "app.risk": {
        "responsibility": "Deterministic risk decisions and limits",
        "allowed": frozenset(
            {"app.exchange_execution", "app.market", "app.portfolio", "app.risk", "app.trading"}
        ),
    },
    "app.runtime": {
        "responsibility": "Application composition, process lifecycle, and status",
        "allowed": frozenset(
            {
                "app.coordination",
                "app.exchange_runtime",
                "app.execution",
                "app.foundation",
                "app.infrastructure",
                "app.market",
                "app.paper",
                "app.persistence",
                "app.portfolio",
                "app.risk",
                "app.runtime",
                "app.shadow",
                "app.strategies",
                "app.trading",
            }
        ),
    },
    "app.shadow": {
        "responsibility": "Read-only shadow observation and comparison services",
        "allowed": frozenset({"app.market", "app.shadow", "app.trading"}),
    },
    "app.strategies": {
        "responsibility": "Advisory strategy implementations",
        "allowed": frozenset({"app.market", "app.strategies", "app.trading"}),
    },
    "app.trading": {
        "responsibility": "Trading intent, snapshots, pipeline, and strategy ports",
        "allowed": frozenset({"app.market", "app.trading"}),
    },
    "app.tools": {
        "responsibility": "Leaf operator and offline command entry points",
        "allowed": frozenset({"app.*"}),
    },
    "app.cli": {
        "responsibility": "Stable command-line composition adapter",
        "allowed": frozenset({"app.cli", "app.control_plane", "app.platform"}),
    },
    "app.main": {
        "responsibility": "Stable runtime process adapter",
        "allowed": frozenset({"app.foundation", "app.main", "app.runtime"}),
    },
    "app.research_methodology": {
        "responsibility": "Stable frozen research methodology contract",
        "allowed": frozenset({"app.research", "app.research_methodology"}),
    },
}
INTERNAL_DEPENDENCY_FAMILIES = frozenset(
    ".".join(owner.split(".")[:2]) for owner in PACKAGE_OWNERSHIP
)
for _policy in PACKAGE_OWNERSHIP.values():
    allowed = _policy["allowed"]
    _policy["forbidden"] = (
        frozenset() if "app.*" in allowed else INTERNAL_DEPENDENCY_FAMILIES - allowed
    )


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
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return frozenset(
        line for line in result.stdout.splitlines() if line and (ROOT / line).is_file()
    )


def _owner_for_module(module: str) -> str | None:
    matches = [
        owner for owner in PACKAGE_OWNERSHIP if module == owner or module.startswith(f"{owner}.")
    ]
    return max(matches, key=len, default=None)


def _dependency_family(module: str) -> str:
    parts = module.split(".")
    return ".".join(parts[:2])


def _importable_module_name(path: Path) -> str:
    parts = list(path.relative_to(ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolved_internal_imports(path: Path, available: frozenset[str]) -> frozenset[str]:
    source_module = _importable_module_name(path)
    package = source_module if path.name == "__init__.py" else source_module.rpartition(".")[0]
    resolved: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        candidates: Iterable[str]
        if isinstance(node, ast.Import):
            candidates = (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                package_parts = package.split(".")
                keep = len(package_parts) - (node.level - 1)
                base_parts = package_parts[:keep]
                if node.module:
                    base_parts.extend(node.module.split("."))
                base = ".".join(base_parts)
            else:
                base = node.module or ""
            expanded = [f"{base}.{alias.name}" for alias in node.names if base]
            candidates = (*expanded, base)
        else:
            continue
        for candidate in candidates:
            if candidate in available:
                resolved.add(candidate)
    return frozenset(resolved)


def _strongly_connected_components(
    graph: dict[str, frozenset[str]],
) -> tuple[tuple[str, ...], ...]:
    next_index = 0
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[tuple[str, ...]] = []

    def visit(node: str) -> None:
        nonlocal next_index
        indices[node] = lowlinks[node] = next_index
        next_index += 1
        stack.append(node)
        on_stack.add(node)
        for target in graph[node]:
            if target not in indices:
                visit(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[target])
        if lowlinks[node] != indices[node]:
            return
        component: list[str] = []
        while True:
            target = stack.pop()
            on_stack.remove(target)
            component.append(target)
            if target == node:
                break
        if len(component) > 1:
            components.append(tuple(sorted(component)))

    for node in graph:
        if node not in indices:
            visit(node)
    return tuple(sorted(components))


def test_root_module_allowlist_can_only_shrink() -> None:
    direct_modules = {path.name for path in APP.glob("*.py")}
    approved = ROOT_MODULE_ALLOWLIST | STABLE_ROOT_ADAPTERS | {"__init__.py"}
    assert direct_modules <= approved, sorted(direct_modules - approved)


def test_package_ownership_map_covers_every_production_package() -> None:
    package_modules = {
        _importable_module_name(path)
        for path in APP.rglob("__init__.py")
        if "tools" not in path.parts and path != APP / "__init__.py"
    }
    assert all(_owner_for_module(module) is not None for module in package_modules)
    assert all(policy["responsibility"] for policy in PACKAGE_OWNERSHIP.values())
    assert all(policy["allowed"] for policy in PACKAGE_OWNERSHIP.values())
    assert all("forbidden" in policy for policy in PACKAGE_OWNERSHIP.values())


def test_internal_imports_follow_package_ownership_map() -> None:
    violations: dict[str, set[str]] = {}
    for path in _production_sources():
        module = _module_name(path)
        owner = _owner_for_module(module)
        if owner is None:
            continue
        allowed = PACKAGE_OWNERSHIP[owner]["allowed"]
        if "app.*" in allowed:
            continue
        for imported in _imports(path):
            if not imported.startswith("app."):
                continue
            family = _dependency_family(imported)
            if family not in allowed:
                violations.setdefault(module, set()).add(imported)
    assert violations == {}


def test_production_module_import_graph_has_no_cycles() -> None:
    sources = _production_sources()
    modules = frozenset(_importable_module_name(path) for path in sources)
    graph = {
        _importable_module_name(path): _resolved_internal_imports(path, modules) for path in sources
    }
    assert _strongly_connected_components(graph) == ()


def test_completed_direct_migrations_have_only_canonical_paths() -> None:
    assert not (APP / "agent" / "__init__.py").exists()
    assert not (APP / "core" / "__init__.py").exists()
    assert not (APP / "database" / "__init__.py").exists()
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
    assert not (APP / "knowledge_retrieval.py").exists()
    assert not (APP / "market_intelligence.py").exists()
    assert not (APP / "market_intelligence_flow.py").exists()
    assert not (APP / "news_event_macro.py").exists()
    assert not (APP / "strategy_history.py").exists()
    assert not (APP / "strategy_lab.py").exists()
    assert not (APP / "strategy_long_history.py").exists()
    assert not (APP / "strategy_walk_forward.py").exists()
    assert not (APP / "application.py").exists()
    assert not (APP / "ai_setup_smoke.py").exists()
    assert not (APP / "bootstrap.py").exists()
    assert not (APP / "control_plane_transport.py").exists()
    assert not (APP / "macos_ollama.py").exists()
    assert not (APP / "ollama_readiness.py").exists()
    assert not (APP / "ollama_start.py").exists()
    assert not (APP / "project_qa_setup_smoke.py").exists()
    assert not (APP / "server_status.py").exists()
    assert not (APP / "setup_engine.py").exists()
    assert not (APP / "vscode_integration.py").exists()
    assert not (APP / "windows_ollama.py").exists()
    assert not (APP / "workspace_context.py").exists()
    assert not (APP / "ai" / "market_semantics.py").exists()
    assert not (APP / "ai" / "research_evidence_review.py").exists()
    assert not (APP / "ai" / "research_query_compiler.py").exists()
    assert not (APP / "ai" / "research_reasoning.py").exists()
    assert not (APP / "ai" / "rde_canonical_storage.py").exists()
    assert not (APP / "ai" / "rde_capabilities.py").exists()
    assert not (APP / "ai" / "rde_execution_cache.py").exists()
    assert not (APP / "ai" / "rde_execution_contracts.py").exists()
    assert not (APP / "ai" / "rde_query_executor.py").exists()
    assert not (APP / "ai" / "benchmark.py").exists()
    assert not (APP / "ai" / "closed_loop_calibration.py").exists()
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


def test_domain_subsystems_do_not_import_runtime_composition() -> None:
    domain_roots = (
        "coordination",
        "exchange_execution",
        "exchange_runtime",
        "execution",
        "infrastructure",
        "market",
        "paper",
        "persistence",
        "portfolio",
        "risk",
        "shadow",
        "strategies",
        "trading",
    )
    violations = {
        _module_name(path): imported
        for root in domain_roots
        for path in (APP / root).rglob("*.py")
        for imported in _imports(path)
        if imported == "app.runtime" or imported.startswith("app.runtime.")
    }
    assert violations == {}


def test_foundation_does_not_import_upward_runtime_or_domain_packages() -> None:
    upward_prefixes = (
        "app.runtime",
        "app.coordination",
        "app.exchange_execution",
        "app.exchange_runtime",
        "app.execution",
        "app.infrastructure",
        "app.market",
        "app.paper",
        "app.persistence",
        "app.portfolio",
        "app.risk",
        "app.shadow",
        "app.strategies",
        "app.trading",
    )
    violations = {
        _module_name(path): imported
        for path in (APP / "foundation").glob("*.py")
        for imported in _imports(path)
        if _matches(imported, upward_prefixes)
    }
    assert violations == {}


def test_platform_ollama_does_not_import_runtime_or_authority_packages() -> None:
    forbidden_prefixes = (
        "app.runtime",
        "app.control_plane",
        "app.coordination",
        "app.exchange_execution",
        "app.exchange_runtime",
        "app.execution",
        "app.infrastructure",
        "app.market",
        "app.paper",
        "app.persistence",
        "app.portfolio",
        "app.research",
        "app.risk",
        "app.shadow",
        "app.strategies",
        "app.tools",
        "app.trading",
    )
    violations = {
        _module_name(path): imported
        for path in (APP / "platform" / "ollama").rglob("*.py")
        for imported in _imports(path)
        if _matches(imported, forbidden_prefixes)
    }
    assert violations == {}


def test_platform_setup_does_not_import_runtime_or_authority_packages() -> None:
    forbidden_prefixes = (
        "app.runtime",
        "app.control_plane",
        "app.coordination",
        "app.exchange_execution",
        "app.exchange_runtime",
        "app.execution",
        "app.infrastructure",
        "app.market",
        "app.paper",
        "app.persistence",
        "app.portfolio",
        "app.research",
        "app.risk",
        "app.shadow",
        "app.strategies",
        "app.tools",
        "app.trading",
    )
    violations = {
        _module_name(path): imported
        for path in (APP / "platform" / "setup").rglob("*.py")
        for imported in _imports(path)
        if _matches(imported, forbidden_prefixes)
    }
    assert violations == {}


def test_platform_bootstrap_and_vscode_remain_infrastructure_only() -> None:
    forbidden_prefixes = (
        "app.runtime",
        "app.control_plane",
        "app.coordination",
        "app.exchange_execution",
        "app.exchange_runtime",
        "app.execution",
        "app.infrastructure",
        "app.market",
        "app.paper",
        "app.persistence",
        "app.portfolio",
        "app.research",
        "app.risk",
        "app.shadow",
        "app.strategies",
        "app.tools",
        "app.trading",
    )
    violations = {
        _module_name(path): imported
        for path in (APP / "platform" / "bootstrap.py", APP / "platform" / "vscode.py")
        for imported in _imports(path)
        if _matches(imported, forbidden_prefixes)
    }
    assert violations == {}


def test_knowledge_does_not_import_platform_or_runtime_authority() -> None:
    forbidden_prefixes = (
        "app.platform",
        "app.runtime",
        "app.control_plane",
        "app.coordination",
        "app.exchange_execution",
        "app.exchange_runtime",
        "app.execution",
        "app.infrastructure",
        "app.market",
        "app.paper",
        "app.persistence",
        "app.portfolio",
        "app.research",
        "app.risk",
        "app.shadow",
        "app.strategies",
        "app.tools",
        "app.trading",
    )
    violations = {
        _module_name(path): imported
        for path in (APP / "knowledge").rglob("*.py")
        for imported in _imports(path)
        if _matches(imported, forbidden_prefixes)
    }
    assert violations == {}


def test_control_plane_does_not_import_runtime_or_execution_authority() -> None:
    forbidden_prefixes = (
        "app.runtime",
        "app.coordination",
        "app.exchange_execution",
        "app.exchange_runtime",
        "app.execution",
        "app.infrastructure",
        "app.market",
        "app.paper",
        "app.persistence",
        "app.portfolio",
        "app.research",
        "app.risk",
        "app.shadow",
        "app.strategies",
        "app.tools",
        "app.trading",
    )
    violations = {
        _module_name(path): imported
        for path in (APP / "control_plane").rglob("*.py")
        for imported in _imports(path)
        if _matches(imported, forbidden_prefixes)
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


def test_ai_research_does_not_import_platform_or_authority_packages() -> None:
    violations = {
        _module_name(path): imported
        for path in (APP / "ai" / "research").rglob("*.py")
        for imported in _imports(path)
        if _matches(imported, AI_RESEARCH_FORBIDDEN_PREFIXES)
    }
    assert violations == {}


def test_ai_evaluation_does_not_import_platform_or_authority_packages() -> None:
    violations = {
        _module_name(path): imported
        for path in (APP / "ai" / "evaluation").rglob("*.py")
        for imported in _imports(path)
        if _matches(imported, AI_EVALUATION_FORBIDDEN_PREFIXES)
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
    assert {
        "CANONICAL_IMPLEMENTATION",
        "FROZEN_PATH",
        "STABLE_PACKAGE_API",
    } == set(COMPATIBILITY_MAP.values())
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


def test_final_architecture_governance_documents_are_present_and_actionable() -> None:
    required_sections = {
        "CLEAN_ARCHITECTURE.md": (
            "Package ownership",
            "Composition roots and state ownership",
            "Provenance and authority",
            "VS Code boundary",
        ),
        "DEPENDENCY_RULES.md": (
            "Package matrix",
            "Permanent prohibitions",
            "Root and composition policy",
            "Versioned and provenance-sensitive changes",
        ),
        "FEATURE_IMPLEMENTATION_RULES.md": (
            "Placement decision tree",
            "Chappie OOP rules",
            "Testing requirements",
            "Automated coding-agent contract",
        ),
        "ARCHITECTURE_DECISIONS.md": (
            "Restricted application root",
            "Stable Knowledge and Control Plane APIs",
            "Lazy AI exports",
            "Compatibility wrappers are exceptional",
        ),
        "REFACTOR_FINAL_REPORT.md": (
            "Quantitative comparison",
            "Permanent guards",
            "Intentionally deferred",
            "Final validation",
        ),
    }
    architecture_docs = ROOT / "docs" / "architecture"
    for filename, headings in required_sections.items():
        document = (architecture_docs / filename).read_text(encoding="utf-8")
        for heading in headings:
            assert f"## {heading}" in document
