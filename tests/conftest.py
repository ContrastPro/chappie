"""Test-suite classification shared by local and CI validation."""

from __future__ import annotations

from pathlib import Path

import pytest
from _pytest.nodes import Item

_HISTORICAL_MODULES = frozenset(
    {
        "test_m410_recovery_proof.py",
        "test_phase3_smoke_authority.py",
        "test_stage4_managed_sell_authority.py",
        "test_stage4_sell_disposition.py",
        "test_stage4_sell_recovery.py",
        "test_stage4_sell_successor.py",
        "test_testnet_phase1.py",
        "test_testnet_phase2.py",
        "test_testnet_phase3.py",
        "test_testnet_phase4.py",
        "test_testnet_stage4_sell.py",
    }
)


def pytest_collection_modifyitems(items: list[Item]) -> None:
    """Keep historical evidence explicit without deleting its regression value."""
    for item in items:
        if Path(str(item.fspath)).name in _HISTORICAL_MODULES:
            item.add_marker(pytest.mark.historical)
