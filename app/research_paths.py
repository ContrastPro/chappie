"""Canonical, research-only path contract with read-only legacy fallback."""

from __future__ import annotations

from pathlib import Path

_ROOT = Path("data/research")
_ASSET = frozenset(
    {"BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "AVAXUSDT", "XRPUSDT", "LINKUSDT", "DOGEUSDT"}
)


def research_root() -> Path:
    return _ROOT


def _asset(asset: str) -> str:
    normalized = asset.upper()
    if normalized not in _ASSET:
        raise ValueError("unsupported canonical research asset")
    return normalized


def _study(name: str) -> str:
    if not name or name != Path(name).name or name in {".", ".."}:
        raise ValueError("invalid research study name")
    return name


def source_root() -> Path:
    return research_root() / "source"


def spot_source_dir(asset: str) -> Path:
    return source_root() / "spot" / _asset(asset)


def futures_source_dir(asset: str) -> Path:
    return source_root() / "futures" / _asset(asset)


def basis_source_dir(asset: str) -> Path:
    return source_root() / "basis" / _asset(asset)


def basis_archive_root() -> Path:
    """Shared immutable mark/index archives, grouped by asset beneath this root."""
    return source_root() / "basis" / "archives"


def compact_db_path() -> Path:
    return research_root() / "compact" / "research.sqlite3"


def cache_root() -> Path:
    return research_root() / "cache"


def download_cache_dir() -> Path:
    return cache_root() / "downloads"


def conversion_cache_dir() -> Path:
    return cache_root() / "conversion"


def temporary_cache_dir() -> Path:
    return cache_root() / "temporary"


def study_dir(name: str) -> Path:
    return research_root() / "studies" / _study(name)


def results_dir() -> Path:
    return research_root() / "results"


def legacy_dir() -> Path:
    return research_root() / "legacy"


def legacy_read(canonical: Path, legacy: Path) -> Path:
    """Prefer canonical data; legacy data is read-compatible only during migration."""
    return canonical if canonical.exists() else legacy


def flow_read_root() -> Path:
    return legacy_read(
        study_dir("market_intelligence") / "v3b", research_root() / "market_intelligence_v3b"
    )


def leverage_read_root() -> Path:
    return legacy_read(
        study_dir("leverage_stress") / "v4c", research_root() / "leverage_stress_v4c"
    )


def long_history_read_root() -> Path:
    return legacy_read(
        spot_source_dir("BTCUSDT") / "long_history", research_root() / "long_history"
    )
