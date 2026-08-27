from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.architecture.generate import (
    GENERATED_NAMES,
    REGISTRY_SCHEMA,
    RegistryError,
    build_payloads,
    extract_imports,
    load_registry,
    run,
    scan_repository,
)


def _registry(*, duplicate: bool = False, status: str = "IMPLEMENTED") -> dict[str, object]:
    components: list[dict[str, object]] = [
        {
            "id": "a",
            "name": "A",
            "status": status,
            "owner": "Test",
            "paths": ["app/a"],
            "tags": ["Implemented"],
            "role": "Source component.",
        },
        {
            "id": "b",
            "name": "B",
            "status": "IMPLEMENTED",
            "owner": "Test",
            "paths": ["app/b"],
            "tags": ["Implemented"],
            "role": "Target component.",
        },
        {
            "id": "future",
            "name": "Future",
            "status": "PLANNED",
            "owner": "Test",
            "paths": [],
            "allow_missing_paths": True,
            "tags": ["Planned"],
            "role": "Planned component.",
        },
    ]
    if duplicate:
        components.append({**components[0]})
    return {
        "schema": REGISTRY_SCHEMA,
        "components": components,
        "dependency_rules": [{"id": "a_no_b", "sources": ["a"], "forbidden_targets": ["b"]}],
        "architecture_test_authority": "tests/test_architecture_boundaries.py",
    }


def _repository(tmp_path: Path, registry: dict[str, object] | None = None) -> Path:
    root = tmp_path / "repository"
    (root / "docs/architecture/generated").mkdir(parents=True)
    (root / "app/a").mkdir(parents=True)
    (root / "app/b").mkdir(parents=True)
    (root / "app/__init__.py").write_text("", encoding="utf-8")
    (root / "app/a/__init__.py").write_text("", encoding="utf-8")
    (root / "app/b/__init__.py").write_text("", encoding="utf-8")
    (root / "docs/architecture/architecture.yaml").write_text(
        json.dumps(registry or _registry()), encoding="utf-8"
    )
    return root


def _generate(root: Path) -> dict[str, str]:
    registry = load_registry(root / "docs/architecture/architecture.yaml")
    payloads = build_payloads(root, registry)
    for name, content in payloads.items():
        (root / "docs/architecture/generated" / name).write_text(content, encoding="utf-8")
    return payloads


def test_registry_parses() -> None:
    registry = load_registry(Path("docs/architecture/architecture.yaml"))
    assert registry["schema"] == REGISTRY_SCHEMA
    assert registry["components"]


def test_duplicate_component_id_is_rejected(tmp_path: Path) -> None:
    root = _repository(tmp_path, _registry(duplicate=True))
    with pytest.raises(RegistryError, match="duplicate component id"):
        load_registry(root / "docs/architecture/architecture.yaml")


def test_invalid_status_is_rejected(tmp_path: Path) -> None:
    root = _repository(tmp_path, _registry(status="CURRENT"))
    with pytest.raises(RegistryError, match="invalid status"):
        load_registry(root / "docs/architecture/architecture.yaml")


def test_missing_declared_path_is_detected(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / "app/a").rename(root / "app/renamed")
    scan = scan_repository(root, load_registry(root / "docs/architecture/architecture.yaml"))
    assert {"component": "a", "path": "app/a"} in scan["missing_paths"]


def test_ast_import_extraction(tmp_path: Path) -> None:
    source = tmp_path / "sample.py"
    source.write_text(
        "import app.a\nfrom app.b.value import Item\nfrom . import local\n", encoding="utf-8"
    )
    assert extract_imports(source) == ("app.a", "app.b.value")


def test_dependency_aggregation_is_deterministic(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / "app/a/one.py").write_text("from app.b import value\n", encoding="utf-8")
    (root / "app/a/two.py").write_text("import app.b\n", encoding="utf-8")
    registry = load_registry(root / "docs/architecture/architecture.yaml")
    first = scan_repository(root, registry)["dependencies"]
    second = scan_repository(root, registry)["dependencies"]
    assert first == second == [("a", "b")]


def test_unmapped_package_is_detected(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / "app/new_area").mkdir()
    (root / "app/new_area/__init__.py").write_text("", encoding="utf-8")
    scan = scan_repository(root, load_registry(root / "docs/architecture/architecture.yaml"))
    assert scan["unmapped"] == ["app/new_area/__init__.py"]


def test_generated_output_is_deterministic(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    registry = load_registry(root / "docs/architecture/architecture.yaml")
    assert build_payloads(root, registry) == build_payloads(root, registry)


def test_check_clean(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    _generate(root)
    assert run(["--check"], root=root) == 0


def test_check_detects_stale_output(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    _generate(root)
    (root / "docs/architecture/generated/inventory.json").write_text("{}\n", encoding="utf-8")
    assert run(["--check"], root=root) == 1


def test_status_is_read_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _repository(tmp_path)
    expected = _generate(root)
    before = {
        name: (root / "docs/architecture/generated" / name).read_bytes() for name in GENERATED_NAMES
    }
    assert run(["--status"], root=root) == 0
    after = {
        name: (root / "docs/architecture/generated" / name).read_bytes() for name in GENERATED_NAMES
    }
    assert before == after
    assert "Architecture Map V1" in capsys.readouterr().out
    assert expected


def test_planned_component_needs_no_path(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    registry = load_registry(root / "docs/architecture/architecture.yaml")
    assert next(item for item in registry["components"] if item["id"] == "future")[
        "allow_missing_paths"
    ]


def test_forbidden_edge_is_detected(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / "app/a/source.py").write_text("import app.b\n", encoding="utf-8")
    scan = scan_repository(root, load_registry(root / "docs/architecture/architecture.yaml"))
    assert scan["unexpected"] == [{"rule": "a_no_b", "source": "a", "target": "b"}]


def test_generated_files_contain_no_absolute_machine_paths(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    payloads = _generate(root)
    assert all(str(root) not in content for content in payloads.values())
    assert all("D:\\" not in content for content in payloads.values())


def test_repeated_generation_is_byte_identical(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    assert run([], root=root) == 0
    first = {
        name: (root / "docs/architecture/generated" / name).read_bytes() for name in GENERATED_NAMES
    }
    assert run([], root=root) == 0
    second = {
        name: (root / "docs/architecture/generated" / name).read_bytes() for name in GENERATED_NAMES
    }
    assert first == second
