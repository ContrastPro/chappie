import json
from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from app.market.models import Candle
from app.strategies.factory import StrategySettings
from app.strategy_history import (
    HistoricalDataset,
    HistoricalDatasetMetadata,
    candles_sha256,
    write_dataset_atomic,
)
from app.strategy_lab import StrategyComparisonConfig
from app.strategy_walk_forward import (
    MAX_CANDIDATES_PER_STRATEGY,
    Candidate,
    default_candidates,
    load_split_manifest,
    metrics_for,
    research_candidates,
    run_walk_forward,
)


def _dataset(seed: int) -> HistoricalDataset:
    candles = tuple(
        Candle(
            open_time_ms=index * 60_000,
            close_time_ms=index * 60_000 + 59_999,
            open=Decimal(100 + seed + (index % 7)),
            high=Decimal(102 + seed + (index % 7)),
            low=Decimal(98 + seed + (index % 7)),
            close=Decimal(100 + seed + (index % 7)),
            volume=Decimal(1),
        )
        for index in range(80)
    )
    return HistoricalDataset(
        HistoricalDatasetMetadata(
            symbol="BTCUSDT",
            interval="1m",
            requested_start_ms=0,
            requested_end_ms=4_800_000,
            actual_first_open_ms=0,
            actual_last_open_ms=4_740_000,
            candle_count=80,
            downloaded_at_utc="x",
            source="test",
            candles_sha256=candles_sha256(candles),
        ),
        candles,
    )


def test_manifest_splits_are_disjoint_and_test_is_not_selection_input(tmp_path) -> None:
    paths = []
    for name, seed in (("train", 1), ("validation", 2), ("test", 3)):
        path = tmp_path / f"{name}.json"
        write_dataset_atomic(path, _dataset(seed))
        paths.append(path.name)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {name: [dataset] for name, dataset in zip(("train", "validation", "test"), paths)}
        ),
        encoding="utf-8",
    )
    splits = load_split_manifest(manifest)
    report = run_walk_forward(splits, candidates=default_candidates())
    assert all(item.candidate.identifier not in {"test"} for item in report.train)
    assert all(item in report.train_selected for item in report.frozen)
    altered_test_splits = {**splits, "test": (_dataset(999),)}
    altered_test_report = run_walk_forward(altered_test_splits, candidates=default_candidates())
    assert altered_test_report.train_selected == report.train_selected
    assert altered_test_report.frozen == report.frozen


def test_manifest_rejects_a_dataset_reused_across_splits(tmp_path) -> None:
    path = tmp_path / "same.json"
    write_dataset_atomic(path, _dataset(1))
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"train": [path.name], "validation": [path.name], "test": [path.name]})
    )
    with pytest.raises(ValueError, match="exactly one"):
        load_split_manifest(manifest)


def test_candidate_spaces_are_small_and_deterministic() -> None:
    for name in ("trend_filtered", "breakout", "mean_reversion"):
        candidates = research_candidates(name)
        assert 0 < len(candidates) <= MAX_CANDIDATES_PER_STRATEGY
        assert candidates == research_candidates(name)


def test_run_rejects_an_oversized_candidate_set() -> None:
    datasets = {
        name: (_dataset(index),) for index, name in enumerate(("train", "validation", "test"))
    }
    candidates = tuple(
        Candidate(f"candidate-{index}", StrategySettings(name="breakout"))
        for index in range(MAX_CANDIDATES_PER_STRATEGY + 1)
    )
    with pytest.raises(ValueError, match="candidate limit"):
        run_walk_forward(datasets, candidates=candidates)


def test_inactive_and_overactive_classification_and_equal_fee_assumption() -> None:
    quiet = _dataset(1)
    config = StrategyComparisonConfig()
    inactive = metrics_for(
        Candidate(
            "inactive",
            StrategySettings(
                name="trend_filtered",
                trend_minimum_spread_bps=Decimal(10_000),
                trend_minimum_atr_bps=Decimal(10_000),
            ),
        ),
        (quiet,),
        config,
    )
    assert inactive.activity == "INACTIVE"
    overactive = metrics_for(
        Candidate("active", StrategySettings(name="mean_reversion")), (quiet,), config
    )
    assert overactive.activity == "OVERACTIVE"
    assert config.fee_rate == Decimal("0.001")


def test_candidate_parameters_are_immutable_after_they_are_frozen() -> None:
    candidate = research_candidates("breakout")[0]
    with pytest.raises(FrozenInstanceError):
        candidate.identifier = "mutated"  # type: ignore[misc]
