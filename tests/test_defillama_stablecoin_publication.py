from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.research.capital_flow.defillama_stablecoin_authority import (
    DEFILLAMA_FILTER_CONFIRMED,
    DEFILLAMA_STABLECOIN_ASSETS,
    DefiLlamaFilterEvidenceV1,
    DefiLlamaStablecoinBoundedResultV1,
    DefiLlamaStablecoinQualityV1,
    DefiLlamaStablecoinRowV1,
)
from app.research.capital_flow.defillama_stablecoin_publication import (
    DEFILLAMA_USABLE_AT_POLICY,
    publish_defillama_stablecoin_authority,
    usable_at_utc,
    verify_defillama_stablecoin_authority,
)


def result(asset_id: int, symbol: str) -> DefiLlamaStablecoinBoundedResultV1:
    row = DefiLlamaStablecoinRowV1(datetime(2023, 9, 1, tzinfo=UTC), Decimal("10.50"))
    evidence = DefiLlamaFilterEvidenceV1(
        asset_id,
        symbol,
        "https://stablecoins.llama.fi/stablecoincharts/all",
        ("stablecoin", str(asset_id)),
        (str(asset_id) * 64)[:64],
        DEFILLAMA_FILTER_CONFIRMED,
    )
    quality = DefiLlamaStablecoinQualityV1(
        1,
        1,
        0,
        0,
        0,
        "VALID_SOURCE_ABSENCE",
        row.reference_timestamp_utc,
        row.reference_timestamp_utc,
        datetime(2026, 6, 1, tzinfo=UTC),
        1,
    )
    return DefiLlamaStablecoinBoundedResultV1(
        "chappie_defillama_stablecoin_historical_authority/1.0.0",
        "DEFILLAMA",
        "DEFILLAMA_STABLECOIN_FLAT_FILTER_OBSERVED/1.0.0",
        "OBSERVED_PROVIDER_FILTER_BEHAVIOR",
        asset_id,
        symbol,
        date(2023, 9, 1),
        date(2026, 5, 31),
        (row,),
        quality,
        evidence,
    )


def results():
    return tuple(result(*item) for item in DEFILLAMA_STABLECOIN_ASSETS)


def test_three_series_publication_temporal_policy_and_reload(tmp_path: Path) -> None:
    published = publish_defillama_stablecoin_authority(results(), tmp_path)
    assert usable_at_utc(datetime(2023, 9, 1, tzinfo=UTC)) == datetime(2023, 9, 2, tzinfo=UTC)
    assert DEFILLAMA_USABLE_AT_POLICY == "REFERENCE_UTC_DAY_PLUS_FIXED_24_HOURS"
    assert verify_defillama_stablecoin_authority(tmp_path) == published
    assert len(published.bounded_digests) == len(published.canonical_digests) == 3


def test_filter_quality_and_existing_authority_fail_closed(tmp_path: Path) -> None:
    bad = replace(
        results()[1], filter_evidence=replace(results()[1].filter_evidence, verification_state="NO")
    )
    with pytest.raises(ValueError):
        publish_defillama_stablecoin_authority((results()[0], bad, results()[2]), tmp_path)
    assert not (tmp_path / "manifest.json").exists()
    publish_defillama_stablecoin_authority(results(), tmp_path)
    with pytest.raises(FileExistsError):
        publish_defillama_stablecoin_authority(results(), tmp_path)


def test_failure_manifest_last_and_determinism(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError):
        publish_defillama_stablecoin_authority(
            results(),
            tmp_path / "bad",
            phase_hook=lambda phase: (
                (_ for _ in ()).throw(RuntimeError()) if phase == "canonical_validated" else None
            ),
        )
    assert not (tmp_path / "bad" / "manifest.json").exists()
    assert not tuple((tmp_path / "bad").glob("*.ndjson"))
    first = publish_defillama_stablecoin_authority(results(), tmp_path / "one")
    second = publish_defillama_stablecoin_authority(results(), tmp_path / "two")
    assert (
        first.bounded_digests == second.bounded_digests
        and first.semantic_authority_digest == second.semantic_authority_digest
    )


def test_reload_rejects_tampered_canonical_temporal_semantics(tmp_path: Path) -> None:
    publish_defillama_stablecoin_authority(results(), tmp_path)
    path = tmp_path / "canonical_usdt.ndjson"
    path.write_text(
        path.read_text(encoding="utf-8").replace("2023-09-02T00:00:00Z", "2023-09-01T00:00:00Z"),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="digest mismatch"):
        verify_defillama_stablecoin_authority(tmp_path)
