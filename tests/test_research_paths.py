from pathlib import Path, PureWindowsPath

import pytest

from app.research import paths


def test_complete_path_contract_uses_repository_relative_research_root(monkeypatch, tmp_path):
    assert paths.research_root() == Path("data/research")

    monkeypatch.setattr(paths, "_ROOT", tmp_path / "research")
    root = tmp_path / "research"
    assert paths.source_root() == root / "source"
    assert paths.news_macro_source_dir() == root / "source/news/macro"
    assert paths.news_macro_download_cache_dir() == root / "cache/downloads/news/macro"
    assert paths.spot_source_dir("BTCUSDT") == root / "source/spot/BTCUSDT"
    assert paths.futures_source_dir("ETHUSDT") == root / "source/futures/ETHUSDT"
    assert paths.basis_source_dir("SOLUSDT") == root / "source/basis/SOLUSDT"
    assert paths.basis_archive_root() == root / "source/basis/archives"
    assert paths.compact_db_path() == root / "compact/research.sqlite3"
    assert paths.cache_root() == root / "cache"
    assert paths.download_cache_dir() == root / "cache/downloads"
    assert paths.conversion_cache_dir() == root / "cache/conversion"
    assert paths.temporary_cache_dir() == root / "cache/temporary"
    assert paths.rde_execution_cache_dir() == root / "cache/ai/rde_queries"
    assert paths.study_dir("market_intelligence") == root / "studies/market_intelligence"
    assert paths.results_dir() == root / "results"
    assert paths.rde_execution_results_dir() == root / "results/ai/rde_queries"
    assert paths.legacy_dir() == root / "legacy"


def test_path_contract_preserves_windows_pathlib_semantics(monkeypatch):
    root = PureWindowsPath("C:/chappie/data/research")
    monkeypatch.setattr(paths, "_ROOT", root)

    assert paths.compact_db_path() == root / "compact" / "research.sqlite3"
    assert paths.spot_source_dir("BTCUSDT") == root / "source" / "spot" / "BTCUSDT"
    assert paths.study_dir("market_intelligence") == root / "studies" / "market_intelligence"
    assert paths.rde_execution_results_dir() == root / "results" / "ai" / "rde_queries"


def test_canonical_contract_and_path_safety(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "_ROOT", tmp_path / "research")
    assert paths.spot_source_dir("solusdt") == tmp_path / "research/source/spot/SOLUSDT"
    assert paths.basis_archive_root() == tmp_path / "research/source/basis/archives"
    assert paths.compact_db_path() == tmp_path / "research/compact/research.sqlite3"
    assert paths.results_dir() == tmp_path / "research/results"
    with pytest.raises(ValueError):
        paths.spot_source_dir("../BTCUSDT")
    with pytest.raises(ValueError):
        paths.study_dir("../escape")


def test_canonical_wins_legacy_read_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "_ROOT", tmp_path / "research")
    canonical, legacy = tmp_path / "canonical", tmp_path / "legacy"
    legacy.mkdir()
    assert paths.legacy_read(canonical, legacy) == legacy
    canonical.mkdir()
    assert paths.legacy_read(canonical, legacy) == canonical


def test_long_history_prefers_asset_scoped_canonical_root(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "_ROOT", tmp_path / "research")
    legacy = tmp_path / "research/long_history"
    legacy.mkdir(parents=True)
    assert paths.long_history_read_root() == legacy
    canonical = tmp_path / "research/source/spot/BTCUSDT/long_history"
    canonical.mkdir(parents=True)
    assert paths.long_history_read_root() == canonical


def test_research_contract_isolated_from_runtime_paper_database(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "_ROOT", tmp_path / "research")

    assert paths.compact_db_path() == tmp_path / "research/compact/research.sqlite3"
    assert paths.compact_db_path() != Path("data/chappie-paper.sqlite3")
    assert Path("data/chappie-paper.sqlite3") not in paths.compact_db_path().parents


def test_new_research_writes_target_canonical_contract(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "_ROOT", tmp_path / "research")
    target = paths.study_dir("market_intelligence") / "v3b" / "manifest.json"

    target.parent.mkdir(parents=True)
    target.write_text("{}", encoding="utf-8")

    assert target.exists()
    assert target.is_relative_to(tmp_path / "research" / "studies")


def test_contract_has_no_root_level_artifact_write_target(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "_ROOT", tmp_path / "research")

    write_targets = (
        paths.compact_db_path(),
        paths.download_cache_dir() / "download.json",
        paths.conversion_cache_dir() / "conversion.json",
        paths.temporary_cache_dir() / "temporary.json",
        paths.study_dir("market_intelligence") / "report.json",
        paths.results_dir() / "report.json",
    )

    assert all(target.parent != paths.research_root() for target in write_targets)
