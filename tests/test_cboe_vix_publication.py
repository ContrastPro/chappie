from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.research.capital_flow.cboe_vix_authority import (
    CBOE_PROVIDER_IDENTITY,
    CBOE_VIX_AUTHORITY_IDENTITY,
    CBOE_VIX_END,
    CBOE_VIX_START,
    CboeVixBoundedResultV1,
    CboeVixQualityEvidenceV1,
    CboeVixSourceRowV1,
)
from app.research.capital_flow.cboe_vix_publication import (
    CBOE_VIX_BOUNDED_EXTRACTION_IDENTITY,
    CBOE_VIX_BOUNDED_SOURCE_NAME,
    CBOE_VIX_CANONICAL_NAME,
    CBOE_VIX_MANIFEST_NAME,
    publish_canonical_cboe_vix,
    read_bounded_cboe_vix_source,
    read_canonical_cboe_vix,
    read_cboe_vix_manifest,
    serialize_bounded_cboe_vix_source,
    serialize_canonical_cboe_vix,
    verify_published_cboe_vix_authority,
)


def _result() -> CboeVixBoundedResultV1:
    rows = (
        CboeVixSourceRowV1(
            date(2023, 9, 1),
            Decimal("13.2500"),
            datetime(2023, 9, 1, 20, tzinfo=UTC),
            datetime(2023, 9, 1, 21, tzinfo=UTC),
        ),
        CboeVixSourceRowV1(
            date(2026, 5, 29),
            Decimal("18.75"),
            datetime(2026, 5, 29, 20, tzinfo=UTC),
            datetime(2026, 5, 29, 21, tzinfo=UTC),
        ),
    )
    quality = CboeVixQualityEvidenceV1(
        2, 2, (), (), 0, 0, 0, rows[0].reference_date, rows[-1].reference_date, date(2026, 6, 1), 1
    )
    return CboeVixBoundedResultV1(
        CBOE_VIX_AUTHORITY_IDENTITY,
        CBOE_PROVIDER_IDENTITY,
        CBOE_VIX_START,
        CBOE_VIX_END,
        rows,
        quality,
    )


def test_deterministic_serialization_identity_bounds_decimal_and_utc() -> None:
    result = _result()
    assert serialize_bounded_cboe_vix_source(result) == serialize_bounded_cboe_vix_source(result)
    canonical = serialize_canonical_cboe_vix(result)
    assert canonical == serialize_canonical_cboe_vix(result)
    assert b'"close":"13.2500"' in canonical
    assert b'"usable_at_utc":"2023-09-01T21:00:00Z"' in canonical
    header = json.loads(serialize_bounded_cboe_vix_source(result).splitlines()[0])
    assert header["authority_identity"] == CBOE_VIX_AUTHORITY_IDENTITY
    assert header["extraction_identity"] == CBOE_VIX_BOUNDED_EXTRACTION_IDENTITY
    assert (header["authorized_start"], header["authorized_end"]) == (
        "2023-09-01",
        "2026-05-31",
    )


@pytest.mark.parametrize(
    "quality_update",
    [
        {"missing_expected_sessions": (date(2024, 1, 2),)},
        {"unexpected_sessions": (date(2024, 1, 6),)},
        {"conflicting_duplicate_count": 1},
        {"malformed_count": 1},
        {"cutoff_crossing_date": date(2026, 9, 1)},
    ],
)
def test_quality_gates_reject_without_manifest(tmp_path: Path, quality_update: dict) -> None:
    result = _result()
    with pytest.raises(ValueError):
        publish_canonical_cboe_vix(
            replace(result, quality=replace(result.quality, **quality_update)), tmp_path
        )
    assert not (tmp_path / CBOE_VIX_MANIFEST_NAME).exists()


def test_success_manifest_last_reload_and_digests(tmp_path: Path) -> None:
    phases: list[str] = []
    published = publish_canonical_cboe_vix(_result(), tmp_path, phase_hook=phases.append)
    assert phases[-3:] == [
        "bounded_source_published",
        "canonical_published",
        "manifest_published",
    ]
    assert read_canonical_cboe_vix(published.canonical_path) == _result().rows
    assert read_bounded_cboe_vix_source(published.bounded_source_path)[0]["provider"] == "CBOE"
    manifest = read_cboe_vix_manifest(published.manifest_path)
    assert manifest["semantic_authority_digest"] == published.semantic_authority_digest
    assert verify_published_cboe_vix_authority(tmp_path) == published
    assert published.bounded_source_digest != published.canonical_digest


@pytest.mark.parametrize(
    "failure_phase",
    [
        "bounded_source_temporary",
        "canonical_temporary",
        "canonical_validated",
        "bounded_source_published",
        "canonical_published",
    ],
)
def test_failure_before_manifest_cleans_all_artifacts(tmp_path: Path, failure_phase: str) -> None:
    def fail(phase: str) -> None:
        if phase == failure_phase:
            raise RuntimeError("injected publication failure")

    with pytest.raises(RuntimeError):
        publish_canonical_cboe_vix(_result(), tmp_path, phase_hook=fail)
    assert list(tmp_path.iterdir()) == []


def test_reload_mismatch_prevents_manifest_and_cleans_temporary_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.research.capital_flow.cboe_vix_publication.read_canonical_cboe_vix",
        lambda _path: (),
    )
    with pytest.raises(ValueError, match="reload"):
        publish_canonical_cboe_vix(_result(), tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_cleanup_failure_does_not_mask_primary_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(_phase: str) -> None:
        raise RuntimeError("primary publication failure")

    def unlink_fails(_path: Path, *, missing_ok: bool = False) -> None:
        del missing_ok
        raise PermissionError("synthetic Windows handle contention")

    monkeypatch.setattr(Path, "unlink", unlink_fails)
    with pytest.raises(RuntimeError, match="primary publication failure"):
        publish_canonical_cboe_vix(_result(), tmp_path, phase_hook=fail)


@pytest.mark.parametrize(
    "existing", [CBOE_VIX_BOUNDED_SOURCE_NAME, CBOE_VIX_CANONICAL_NAME, CBOE_VIX_MANIFEST_NAME]
)
def test_existing_authority_artifact_is_never_overwritten(tmp_path: Path, existing: str) -> None:
    path = tmp_path / existing
    path.write_text("preserve", encoding="utf-8")
    with pytest.raises(FileExistsError):
        publish_canonical_cboe_vix(_result(), tmp_path)
    assert path.read_text(encoding="utf-8") == "preserve"


def test_digests_exclude_absolute_path_and_repeat_identically(tmp_path: Path) -> None:
    first = publish_canonical_cboe_vix(_result(), tmp_path / "one")
    second = publish_canonical_cboe_vix(_result(), tmp_path / "two")
    assert first.bounded_source_digest == second.bounded_source_digest
    assert first.canonical_digest == second.canonical_digest
    assert first.semantic_authority_digest == second.semantic_authority_digest
    assert first.manifest_semantic_digest == second.manifest_semantic_digest
    assert first.manifest_path.read_bytes() == second.manifest_path.read_bytes()
