#!/usr/bin/env python3
"""Generate and validate supplemental Architecture Map V1 evidence.

The registry and Structurizr workspace own architectural meaning. This tool only
maps static Python files/imports to that declared model; it never imports Chappie.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

REGISTRY_SCHEMA = "chappie_architecture_registry/1.0.0"
INVENTORY_SCHEMA = "chappie_architecture_inventory/1.0.0"
DEPENDENCY_SCHEMA = "chappie_architecture_dependencies/1.0.0"
DRIFT_SCHEMA = "chappie_architecture_drift_report/1.0.0"
STATUS_ORDER = ("IMPLEMENTED", "PARTIAL", "EXPERIMENTAL", "HISTORICAL", "PLANNED")
VALID_STATUSES = frozenset(STATUS_ORDER)
GENERATED_NAMES = (
    "inventory.json",
    "package_dependencies.json",
    "package_dependencies.mmd",
    "drift_report.json",
)


class RegistryError(ValueError):
    """The architecture registry is malformed or internally inconsistent."""


def repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_registry(path: Path) -> dict[str, Any]:
    """Load JSON-compatible YAML with strict bounded validation."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RegistryError(f"invalid registry: {error}") from error
    if not isinstance(value, dict) or value.get("schema") != REGISTRY_SCHEMA:
        raise RegistryError(f"registry schema must be {REGISTRY_SCHEMA}")
    components = value.get("components")
    if not isinstance(components, list) or not components:
        raise RegistryError("registry components must be a non-empty list")
    identifiers: set[str] = set()
    for component in components:
        if not isinstance(component, dict):
            raise RegistryError("each component must be an object")
        identifier = component.get("id")
        if (
            not isinstance(identifier, str)
            or not identifier
            or not identifier.replace("_", "").isalnum()
        ):
            raise RegistryError("component id must be a bounded identifier")
        if identifier in identifiers:
            raise RegistryError(f"duplicate component id: {identifier}")
        identifiers.add(identifier)
        if component.get("status") not in VALID_STATUSES:
            raise RegistryError(f"invalid status for {identifier}: {component.get('status')}")
        for field in ("name", "owner", "role"):
            if not isinstance(component.get(field), str) or not component[field].strip():
                raise RegistryError(f"component {identifier} requires {field}")
        paths = component.get("paths")
        if not isinstance(paths, list) or any(not _safe_relative_path(item) for item in paths):
            raise RegistryError(f"component {identifier} has invalid paths")
        if not paths and not component.get("allow_missing_paths", False):
            raise RegistryError(f"component {identifier} requires a path")
    rules = value.get("dependency_rules", [])
    if not isinstance(rules, list):
        raise RegistryError("dependency_rules must be a list")
    rule_ids: set[str] = set()
    for rule in rules:
        if not isinstance(rule, dict) or not isinstance(rule.get("id"), str):
            raise RegistryError("dependency rule requires an id")
        if rule["id"] in rule_ids:
            raise RegistryError(f"duplicate dependency rule id: {rule['id']}")
        rule_ids.add(rule["id"])
        for field in ("sources", "forbidden_targets"):
            values = rule.get(field)
            if not isinstance(values, list) or any(item not in identifiers for item in values):
                raise RegistryError(f"dependency rule {rule['id']} has invalid {field}")
    return value


def _safe_relative_path(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    path = Path(value)
    return not path.is_absolute() and ".." not in path.parts


def extract_imports(path: Path) -> tuple[str, ...]:
    """Return statically declared absolute import names without importing code."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
    return tuple(sorted(names))


def _module_name(relative: str) -> str:
    path = Path(relative).with_suffix("")
    parts = list(path.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _path_matches(relative: str, declared: str) -> bool:
    return relative == declared or relative.startswith(declared.rstrip("/") + "/")


def component_for_path(relative: str, components: list[dict[str, Any]]) -> str | None:
    candidates = (
        (len(path), component["id"])
        for component in components
        for path in component["paths"]
        if _path_matches(relative, path)
    )
    return max(candidates, default=(0, None))[1]


def component_for_module(module: str, module_components: dict[str, str]) -> str | None:
    candidates = (
        (len(name), component)
        for name, component in module_components.items()
        if module == name or module.startswith(name + ".")
    )
    return max(candidates, default=(0, None))[1]


def scan_repository(root: Path, registry: dict[str, Any]) -> dict[str, Any]:
    components = registry["components"]
    files = sorted(path for path in (root / "app").rglob("*.py") if "__pycache__" not in path.parts)
    modules_by_component: dict[str, list[str]] = defaultdict(list)
    files_by_component: dict[str, list[str]] = defaultdict(list)
    module_components: dict[str, str] = {}
    imports_by_file: dict[str, tuple[str, ...]] = {}
    unmapped: list[str] = []
    for path in files:
        relative = path.relative_to(root).as_posix()
        if relative == "app/__init__.py":
            continue
        component = component_for_path(relative, components)
        if component is None:
            unmapped.append(relative)
            continue
        module = _module_name(relative)
        modules_by_component[component].append(module)
        files_by_component[component].append(relative)
        module_components[module] = component
        imports_by_file[relative] = extract_imports(path)

    dependencies: set[tuple[str, str]] = set()
    external: dict[str, set[str]] = defaultdict(set)
    stdlib = sys.stdlib_module_names
    for relative, imports in imports_by_file.items():
        source = component_for_path(relative, components)
        if source is None:
            continue
        for imported in imports:
            if imported == "app" or imported.startswith("app."):
                target = component_for_module(imported, module_components)
                if target is not None and target != source:
                    dependencies.add((source, target))
            else:
                root_name = imported.split(".", 1)[0]
                if root_name not in stdlib and root_name != "__future__":
                    external[source].add(root_name)

    missing_paths: list[dict[str, str]] = []
    for component in components:
        if component.get("allow_missing_paths", False):
            continue
        for declared in component["paths"]:
            if not (root / declared).exists():
                missing_paths.append({"component": component["id"], "path": declared})

    unexpected = forbidden_edges(registry, dependencies)
    return {
        "modules": {key: sorted(value) for key, value in modules_by_component.items()},
        "files": {key: sorted(value) for key, value in files_by_component.items()},
        "dependencies": sorted(dependencies),
        "external": {key: sorted(value) for key, value in sorted(external.items())},
        "unmapped": sorted(unmapped),
        "missing_paths": sorted(missing_paths, key=lambda item: (item["component"], item["path"])),
        "unexpected": unexpected,
    }


def forbidden_edges(
    registry: dict[str, Any], dependencies: set[tuple[str, str]] | list[tuple[str, str]]
) -> list[dict[str, str]]:
    edges = set(dependencies)
    issues: list[dict[str, str]] = []
    for rule in registry.get("dependency_rules", []):
        for source in rule["sources"]:
            for target in rule["forbidden_targets"]:
                if (source, target) in edges:
                    issues.append({"rule": rule["id"], "source": source, "target": target})
    return sorted(issues, key=lambda item: (item["rule"], item["source"], item["target"]))


def build_payloads(root: Path, registry: dict[str, Any]) -> dict[str, str]:
    scan = scan_repository(root, registry)
    components = []
    for component in registry["components"]:
        identifier = component["id"]
        components.append(
            {
                "id": identifier,
                "name": component["name"],
                "status": component["status"],
                "owner": component["owner"],
                "declared_paths": component["paths"],
                "discovered_modules": scan["modules"].get(identifier, []),
                "discovered_file_count": len(scan["files"].get(identifier, [])),
                "internal_dependency_targets": sorted(
                    target for source, target in scan["dependencies"] if source == identifier
                ),
                "external_import_roots": scan["external"].get(identifier, []),
            }
        )
    inventory = {
        "schema": INVENTORY_SCHEMA,
        "registry_schema": registry["schema"],
        "components": components,
        "unmapped_paths": scan["unmapped"],
    }
    edges = [{"source": source, "target": target} for source, target in scan["dependencies"]]
    dependencies = {
        "schema": DEPENDENCY_SCHEMA,
        "scope": "production app Python modules; tests and tooling excluded",
        "edges": edges,
    }
    issues = architecture_issues(scan)
    drift = {
        "schema": DRIFT_SCHEMA,
        "status": "CLEAN" if not issues else "DRIFT",
        "issues": issues,
    }
    return {
        "inventory.json": _json_text(inventory),
        "package_dependencies.json": _json_text(dependencies),
        "package_dependencies.mmd": dependency_mermaid(registry, scan["dependencies"]),
        "drift_report.json": _json_text(drift),
    }


def architecture_issues(scan: dict[str, Any]) -> list[dict[str, str]]:
    issues = [
        {"category": "UNMAPPED_PATH", "path": path, "message": "ARCHITECTURE_MODEL_UPDATE_REQUIRED"}
        for path in scan["unmapped"]
    ]
    issues.extend(
        {
            "category": "MISSING_DECLARED_PATH",
            "component": item["component"],
            "path": item["path"],
        }
        for item in scan["missing_paths"]
    )
    issues.extend(
        {
            "category": "UNEXPECTED_DEPENDENCY",
            "rule": item["rule"],
            "source": item["source"],
            "target": item["target"],
        }
        for item in scan["unexpected"]
    )
    return sorted(issues, key=lambda item: json.dumps(item, sort_keys=True))


def dependency_mermaid(registry: dict[str, Any], dependencies: list[tuple[str, str]]) -> str:
    names = {component["id"]: component["name"] for component in registry["components"]}
    nodes = sorted({item for edge in dependencies for item in edge})
    lines = [
        "%% Generated by tools/architecture/generate.py; do not edit.",
        "flowchart LR",
    ]
    lines.extend(f'    {node}["{names[node]}"]' for node in nodes)
    lines.extend(f"    {source} --> {target}" for source, target in dependencies)
    return "\n".join(lines) + "\n"


def _json_text(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def check_outputs(directory: Path, expected: dict[str, str]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for name in GENERATED_NAMES:
        path = directory / name
        try:
            actual = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            actual = ""
        if actual != expected[name]:
            issues.append({"category": "STALE_GENERATED_OUTPUT", "path": path.as_posix()})
    return issues


def status_summary(registry: dict[str, Any], payloads: dict[str, str], stale: bool) -> str:
    inventory = json.loads(payloads["inventory.json"])
    dependencies = json.loads(payloads["package_dependencies.json"])
    drift = json.loads(payloads["drift_report.json"])
    counts = Counter(component["status"] for component in registry["components"])
    mapped = sum(len(component["discovered_modules"]) for component in inventory["components"])
    detected = stale or drift["status"] != "CLEAN"
    lines = [
        "Architecture Map V1",
        f"Components: {len(registry['components'])}",
        *(f"{status.title()}: {counts[status]}" for status in STATUS_ORDER),
        f"Mapped Python modules: {mapped}",
        f"Dependency edges: {len(dependencies['edges'])}",
        f"Unmapped architecture paths: {len(inventory['unmapped_paths'])}",
        f"Drift: {'DETECTED' if detected else 'CLEAN'}",
    ]
    if inventory["unmapped_paths"]:
        lines.append("ARCHITECTURE_MODEL_UPDATE_REQUIRED")
    return "\n".join(lines)


def run(argv: list[str] | None = None, *, root: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help="check without writing")
    modes.add_argument("--status", action="store_true", help="show read-only summary")
    args = parser.parse_args(argv)
    root = root or repository_root()
    registry_path = root / "docs/architecture/architecture.yaml"
    generated = root / "docs/architecture/generated"
    try:
        registry = load_registry(registry_path)
        payloads = build_payloads(root, registry)
    except (RegistryError, OSError, SyntaxError) as error:
        print(f"INVALID_REGISTRY: {error}", file=sys.stderr)
        return 2
    stale_issues = check_outputs(generated, payloads)
    semantic_drift = json.loads(payloads["drift_report.json"])["status"] != "CLEAN"
    if args.status:
        print(status_summary(registry, payloads, bool(stale_issues)))
        return 1 if semantic_drift or stale_issues else 0
    if args.check:
        for issue in stale_issues:
            print(f"{issue['category']}: {issue['path']}", file=sys.stderr)
        if semantic_drift:
            print("ARCHITECTURE_MODEL_UPDATE_REQUIRED", file=sys.stderr)
        return 1 if semantic_drift or stale_issues else 0
    for name in GENERATED_NAMES:
        write_atomic(generated / name, payloads[name])
    if semantic_drift:
        print("ARCHITECTURE_MODEL_UPDATE_REQUIRED", file=sys.stderr)
        return 1
    print(f"Generated {len(GENERATED_NAMES)} Architecture Map V1 artifacts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
