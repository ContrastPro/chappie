"""Regression coverage for the dependency-light :mod:`app.ai` package boundary."""

from __future__ import annotations

import subprocess
import sys


def _import(statement: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", statement], capture_output=True, text=True, check=False
    )


def test_schemas_import_does_not_eagerly_load_high_level_ai_modules() -> None:
    result = _import(
        "import sys; import app.ai.schemas; "
        "assert 'app.ai.runtime' not in sys.modules; "
        "assert 'app.ai.project_qa' not in sys.modules; "
        "assert 'app.ai.research.query_executor' not in sys.modules"
    )
    assert result.returncode == 0, result.stderr


def test_ai_package_import_does_not_eagerly_load_research_or_rde_modules() -> None:
    result = _import(
        "import sys; import app.ai; "
        "assert 'app.ai.research.reasoning' not in sys.modules; "
        "assert 'app.ai.research.query_compiler' not in sys.modules; "
        "assert 'app.ai.research.query_executor' not in sys.modules"
    )
    assert result.returncode == 0, result.stderr


def test_cache_identity_and_knowledge_import_orders_are_safe() -> None:
    for statement in (
        "import app.research.data.cache_identity",
        "import app.research.data.cache_identity; import app.ai",
        "import app.ai; import app.research.data.cache_identity",
        "import app.knowledge; import app.ai",
        "import app.ai; import app.knowledge",
        "import app.ai.project_qa",
    ):
        result = _import(statement)
        assert result.returncode == 0, result.stderr


def test_public_ai_reexports_remain_available_lazily() -> None:
    result = _import(
        "from app.ai import AIConfig, AITask, ChappieAI, FactV1, ModelProfile; "
        "assert AIConfig and AITask and ChappieAI and FactV1 and ModelProfile"
    )
    assert result.returncode == 0, result.stderr
