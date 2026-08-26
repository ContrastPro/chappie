"""Synthetic contracts for durable frozen OOS execution artifacts."""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from app.market.models import Candle
from app.research.strategy.history import (
    HistoricalDataset,
    HistoricalDatasetMetadata,
    candles_sha256,
)
from app.research.strategy.lab import StrategyComparisonConfig
from app.research.strategy.locked_evaluation import PreparedLockedReplay
from app.research.strategy.locked_oos_runner import (
    AUTHORIZATION_IDENTITY,
    PROGRESS_IDENTITY,
    RESULT_IDENTITY,
    _digest,
    _validate_attempt_id,
    main,
    run_attempt,
)
from app.strategies.factory import StrategySettings


def _prepared() -> PreparedLockedReplay:
    candles = tuple(
        Candle(
            open_time_ms=index * 60_000,
            close_time_ms=index * 60_000 + 59_999,
            open=Decimal(100 + index),
            high=Decimal(101 + index),
            low=Decimal(99 + index),
            close=Decimal(100 + index),
            volume=Decimal(1),
        )
        for index in range(40)
    )
    dataset = HistoricalDataset(
        HistoricalDatasetMetadata(
            symbol="BTCUSDT",
            interval="1m",
            requested_start_ms=0,
            requested_end_ms=2_400_000,
            actual_first_open_ms=0,
            actual_last_open_ms=candles[-1].open_time_ms,
            candle_count=len(candles),
            downloaded_at_utc="2026-01-01T00:00:00+00:00",
            source="synthetic",
            candles_sha256=candles_sha256(candles),
        ),
        candles,
    )
    return PreparedLockedReplay(
        dataset=dataset,
        strategy=StrategySettings(name="ema_crossover", ema_fast_window=2, ema_slow_window=3),
        source_commit="a" * 40,
        manifest_digest="manifest",
        evaluation_start_index=4,
        oos_start_utc="1970-01-01T00:04:00Z",
        oos_end_utc="1970-01-01T00:40:00Z",
        config=StrategyComparisonConfig(),
        benchmark="same-symbol buy-and-hold quote PnL over each evaluation interval",
        metrics=("net_pnl",),
        preparation_digest="preparation",
    )


def _authorization(
    prepared: PreparedLockedReplay, head: str, *, attempt_id: int
) -> dict[str, object]:
    payload: dict[str, object] = {
        "identity": AUTHORIZATION_IDENTITY,
        "attempt_id": attempt_id,
        "attempt_history": [
            {
                "attempt_id": index,
                "status": (
                    "INTERRUPTED_BY_HOST_SHUTDOWN"
                    if index == 1
                    else "INTERRUPTED_BY_TERMINAL_CLOSE"
                ),
                "result_observed": False,
            }
            for index in range(1, attempt_id)
        ],
        "execution_source_commit": head,
        "original_manifest_digest": prepared.manifest_digest,
        "dataset_digest": prepared.dataset.metadata.candles_sha256,
        "strategy_identity": "ema_crossover/1.0.0",
        "strategy_config_fingerprint": "fingerprint",
        "symbol": "BTCUSDT",
        "interval": "1m",
        "oos_start_utc": prepared.oos_start_utc,
        "oos_candle_count": 36,
        "oos_executed": False,
        "oos_result_observed": False,
    }
    payload["authorization_digest"] = _digest(
        payload, version="frozen-strategy-execution-authorization/v1"
    )
    return payload


def test_synthetic_attempt_writes_atomic_progress_and_immutable_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared()
    head = "b" * 40
    authorization = tmp_path / "authorization.json"
    authorization.write_text(
        json.dumps(_authorization(prepared, head, attempt_id=2)), encoding="utf-8"
    )
    monkeypatch.setattr(
        "app.research.strategy.locked_oos_runner.prepare_locked_replay",
        lambda *_args, **_kwargs: prepared,
    )
    progress, result = tmp_path / "progress.json", tmp_path / "result.json"
    assert (
        run_attempt(
            manifest_path=tmp_path / "manifest.json",
            dataset_path=tmp_path / "dataset.json",
            authorization_path=authorization,
            progress_path=progress,
            result_path=result,
            execution_head=head,
            attempt_id=2,
        )
        == result
    )
    progress_payload = json.loads(progress.read_text(encoding="utf-8"))
    result_payload = json.loads(result.read_text(encoding="utf-8"))
    assert progress_payload["identity"] == PROGRESS_IDENTITY
    assert progress_payload["processed_oos_candles"] == 36
    assert progress_payload["resume_supported"] is False
    assert result_payload["identity"] == RESULT_IDENTITY
    assert result_payload["attempt_id"] == 2
    assert result_payload["oos_executed"] is True
    with pytest.raises(FileExistsError):
        run_attempt(
            manifest_path=tmp_path / "manifest.json",
            dataset_path=tmp_path / "dataset.json",
            authorization_path=authorization,
            progress_path=progress,
            result_path=result,
            execution_head=head,
            attempt_id=2,
        )


def test_synthetic_attempt_rejects_tampered_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared()
    authorization = _authorization(prepared, "b" * 40, attempt_id=2)
    authorization["dataset_digest"] = "wrong"
    path = tmp_path / "authorization.json"
    path.write_text(json.dumps(authorization), encoding="utf-8")
    monkeypatch.setattr(
        "app.research.strategy.locked_oos_runner.prepare_locked_replay",
        lambda *_args, **_kwargs: prepared,
    )
    with pytest.raises(ValueError, match="digest"):
        run_attempt(
            manifest_path=tmp_path / "manifest.json",
            dataset_path=tmp_path / "dataset.json",
            authorization_path=path,
            progress_path=tmp_path / "progress.json",
            result_path=tmp_path / "result.json",
            execution_head="b" * 40,
            attempt_id=2,
        )


def test_attempt_three_preserves_history_and_labels_all_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared()
    head = "c" * 40
    authorization = tmp_path / "authorization.json"
    authorization.write_text(
        json.dumps(_authorization(prepared, head, attempt_id=3)), encoding="utf-8"
    )
    monkeypatch.setattr(
        "app.research.strategy.locked_oos_runner.prepare_locked_replay",
        lambda *_args, **_kwargs: prepared,
    )
    progress, result = tmp_path / "progress.json", tmp_path / "result.json"
    run_attempt(
        manifest_path=tmp_path / "manifest.json",
        dataset_path=tmp_path / "dataset.json",
        authorization_path=authorization,
        progress_path=progress,
        result_path=result,
        execution_head=head,
        attempt_id=3,
    )
    assert json.loads(progress.read_text(encoding="utf-8"))["attempt_id"] == 3
    result_payload = json.loads(result.read_text(encoding="utf-8"))
    assert result_payload["attempt_id"] == 3
    assert (
        result_payload["attempt_history"]
        == _authorization(prepared, head, attempt_id=3)["attempt_history"]
    )


@pytest.mark.parametrize("value", (None, 0, -1, True, "3"))
def test_attempt_id_rejects_missing_malformed_or_non_positive(value: object) -> None:
    with pytest.raises(ValueError, match="attempt_id"):
        _validate_attempt_id(value)


def test_attempt_id_mismatch_writes_matching_failure_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared()
    head = "d" * 40
    authorization = tmp_path / "authorization.json"
    authorization.write_text(
        json.dumps(_authorization(prepared, head, attempt_id=2)), encoding="utf-8"
    )
    failure = tmp_path / "failure.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "runner",
            "--manifest",
            str(tmp_path / "manifest.json"),
            "--dataset",
            str(tmp_path / "dataset.json"),
            "--authorization",
            str(authorization),
            "--progress",
            str(tmp_path / "progress.json"),
            "--result",
            str(tmp_path / "result.json"),
            "--failure",
            str(failure),
            "--execution-head",
            head,
            "--attempt-id",
            "3",
        ],
    )
    assert main() == 1
    assert json.loads(failure.read_text(encoding="utf-8"))["attempt_id"] == 3
