from datetime import UTC, datetime

import pytest

from app.research.capital_flow import binance_breadth_authority as authority
from app.research.capital_flow.binance_breadth_authority import (
    BINANCE_BREADTH_AUTHORITY_IDENTITY,
    BINANCE_BREADTH_SYMBOLS_V1,
    END_MS,
    INTERVAL_MS,
    START_MS,
    archive_url,
    months,
    normalize_native_timestamp,
)


def test_frozen_archive_authority_scope_and_months_are_exact() -> None:
    assert (
        BINANCE_BREADTH_AUTHORITY_IDENTITY == "chappie_binance_breadth_historical_authority/1.0.0"
    )
    assert len(BINANCE_BREADTH_SYMBOLS_V1) == 19
    assert months()[0] == "2024-01" and months()[-1] == "2026-05" and len(months()) == 29
    assert START_MS == int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1_000)
    assert END_MS == int(datetime(2026, 6, 1, tzinfo=UTC).timestamp() * 1_000)
    assert archive_url("BTCUSDT", "2024-01").endswith("BTCUSDT-1m-2024-01.zip")


def test_timestamp_normalization_rejects_wrong_units_and_canonicalizes_once() -> None:
    assert normalize_native_timestamp("1735689600000", "2024-12") == 1_735_689_600_000
    assert normalize_native_timestamp("1735689600000000", "2025-01") == 1_735_689_600_000
    with pytest.raises(ValueError, match="unit|magnitude"):
        normalize_native_timestamp("1735689600000000", "2024-12")
    with pytest.raises(ValueError, match="unit|magnitude"):
        normalize_native_timestamp("1735689600000", "2025-01")
    assert INTERVAL_MS == 60_000


def test_non_authoritative_cleanup_cannot_mask_primary_failure(
    tmp_path, monkeypatch, capsys
) -> None:
    path = tmp_path / "symbol.pending"
    path.write_text("partial", encoding="utf-8")

    def locked_unlink(self, *, missing_ok=False):
        raise PermissionError("locked")

    monkeypatch.setattr(type(path), "unlink", locked_unlink)
    authority._cleanup_non_authoritative(path)
    assert "cleanup deferred" in capsys.readouterr().out
