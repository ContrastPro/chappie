"""Regression tests for the active MVP versus historical experiment boundary."""

from __future__ import annotations

import subprocess
import sys

from app.persistence.models import ACTIVE_MVP_EXCHANGE_SCHEMA_VERSION


def test_active_mvp_exchange_schema_is_the_minimum_continuous_contract() -> None:
    assert ACTIVE_MVP_EXCHANGE_SCHEMA_VERSION == 4


def test_normal_testnet_composition_does_not_import_historical_stage4_modules() -> None:
    code = """
import sys
import app.exchange_runtime.composition
historical = [
    name for name in sys.modules
    if name.startswith('app.exchange_execution.phase3')
    or name.startswith('app.exchange_execution.stage4_sell')
]
raise SystemExit(1 if historical else 0)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
