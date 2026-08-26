"""Durable, standalone execution for a separately authorized frozen OOS attempt."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any

from app.research.strategy.lab import ReplayProgress, StrategyComparisonResult, compare_strategy
from app.research.strategy.locked_evaluation import PreparedLockedReplay, prepare_locked_replay

AUTHORIZATION_IDENTITY = "chappie_frozen_strategy_execution_authorization/1.0.0"
PROGRESS_IDENTITY = "chappie_frozen_strategy_oos_progress/1.0.0"
RESULT_IDENTITY = "chappie_frozen_strategy_oos_result/1.0.0"
FAILURE_IDENTITY = "chappie_frozen_strategy_oos_failure/1.0.0"
CHECKPOINT_EVERY_CANDLES = 100


def _digest(value: object, *, version: str) -> str:
    payload = json.dumps(
        {"algorithm_version": version, "payload": value},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _atomic_replace(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _atomic_create(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
        os.unlink(temporary)
    except FileExistsError:
        os.unlink(temporary)
        raise FileExistsError(f"immutable result artifact already exists: {path}") from None
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _validate_attempt_id(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("attempt_id must be a positive integer")
    return value


def _validated_history(value: object, *, attempt_id: int) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list) or len(value) != attempt_id - 1:
        raise ValueError("attempt history is incomplete")
    history: list[dict[str, object]] = []
    for expected_id, item in enumerate(value, start=1):
        if (
            not isinstance(item, dict)
            or item.get("attempt_id") != expected_id
            or not isinstance(item.get("status"), str)
            or item.get("result_observed") is not False
        ):
            raise ValueError("attempt history is invalid")
        history.append(dict(item))
    return tuple(history)


def _load_authorization(
    path: Path, *, execution_head: str, attempt_id: int
) -> tuple[dict[str, object], str, tuple[dict[str, object], ...]]:
    attempt_id = _validate_attempt_id(attempt_id)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError("execution authorization is invalid")
    digest = raw.pop("authorization_digest", None)
    if not isinstance(digest, str) or digest != _digest(
        raw, version="frozen-strategy-execution-authorization/v1"
    ):
        raise ValueError("execution authorization digest mismatch")
    if (
        raw.get("identity") != AUTHORIZATION_IDENTITY
        or raw.get("attempt_id") != attempt_id
        or raw.get("execution_source_commit") != execution_head
        or raw.get("oos_executed") is not False
        or raw.get("oos_result_observed") is not False
    ):
        raise ValueError("execution authorization does not permit this attempt")
    return raw, digest, _validated_history(raw.get("attempt_history"), attempt_id=attempt_id)


def _validate_authority(authorization: dict[str, object], prepared: PreparedLockedReplay) -> None:
    strategy_identity = f"{prepared.strategy.name}/1.0.0"
    parameters: dict[str, object]
    if prepared.strategy.name == "ema_crossover":
        parameters = {
            "fast_window": prepared.strategy.ema_fast_window,
            "slow_window": prepared.strategy.ema_slow_window,
        }
    elif prepared.strategy.name == "trend_filtered":
        parameters = {
            "fast_window": prepared.strategy.ema_fast_window,
            "slow_window": prepared.strategy.ema_slow_window,
            "atr_window": prepared.strategy.trend_atr_window,
            "minimum_spread_bps": str(prepared.strategy.trend_minimum_spread_bps),
            "minimum_atr_bps": str(prepared.strategy.trend_minimum_atr_bps),
        }
    else:
        raise ValueError("execution strategy is not authorized")
    expected = {
        "original_manifest_digest": prepared.manifest_digest,
        "dataset_digest": prepared.dataset.metadata.candles_sha256,
        "strategy_identity": strategy_identity,
        "parameters": parameters,
        "symbol": prepared.dataset.metadata.symbol,
        "interval": prepared.dataset.metadata.interval,
        "oos_start_utc": prepared.oos_start_utc,
        "oos_candle_count": len(prepared.dataset.candles) - prepared.evaluation_start_index,
    }
    if any(authorization.get(key) != value for key, value in expected.items()):
        raise ValueError("execution authorization binding mismatch")
    if authorization.get("strategy_config_fingerprint") is None:
        raise ValueError("execution authorization fingerprint is absent")


def _progress_payload(
    *,
    attempt_id: int,
    attempt_history: tuple[dict[str, object], ...],
    authorization: dict[str, object],
    authorization_digest: str,
    prepared: PreparedLockedReplay,
    progress: ReplayProgress,
    sequence: int,
    elapsed_seconds: float,
) -> dict[str, object]:
    state = {
        "processed_oos_candles": progress.evaluation_processed,
        "last_close_time_ms": progress.last_close_time_ms,
        "decisions": progress.decisions,
        "fills": progress.fills,
        "closed_trades": progress.closed_trades,
        "current_equity": str(progress.current_equity),
    }
    return {
        "identity": PROGRESS_IDENTITY,
        "attempt_id": attempt_id,
        "attempt_history": list(attempt_history),
        "authorization_digest": authorization_digest,
        "original_manifest_digest": prepared.manifest_digest,
        "dataset_digest": prepared.dataset.metadata.candles_sha256,
        "strategy_identity": authorization["strategy_identity"],
        "strategy_config_fingerprint": authorization["strategy_config_fingerprint"],
        "oos_start_utc": prepared.oos_start_utc,
        "oos_end_utc": prepared.oos_end_utc,
        "evaluation_start_index": prepared.evaluation_start_index,
        "total_oos_candles": progress.evaluation_total,
        "processed_oos_candles": progress.evaluation_processed,
        "last_processed_candle_close_time_ms": progress.last_close_time_ms,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "status": "RUNNING",
        "checkpoint_sequence": sequence,
        "execution_state_digest": _digest(state, version="frozen-oos-progress-state/v1"),
        "resume_supported": False,
    }


def _result_metrics(
    result: StrategyComparisonResult, prepared: PreparedLockedReplay
) -> dict[str, object]:
    first = prepared.dataset.candles[prepared.evaluation_start_index].close
    last = prepared.dataset.candles[-1].close
    benchmark = prepared.config.starting_equity * ((last / first) - Decimal(1))
    return {
        "decisions": sum(result.regime_counts.values()),
        "fills": result.fills,
        "closed_trades": result.completed_round_trips,
        "gross_realised_pnl": str(result.gross_realised_pnl),
        "fees": str(result.fees),
        "net_pnl": str(result.net_pnl),
        "wins": result.wins,
        "losses": result.losses,
        "expectancy": None
        if result.average_round_trip_pnl is None
        else str(result.average_round_trip_pnl),
        "profit_factor": None if result.profit_factor is None else str(result.profit_factor),
        "max_drawdown_percent": str(result.max_drawdown_percent),
        "turnover": str(result.turnover),
        "exposure_time_percent": str(result.exposure_time_percent),
        "best_trade": None if result.best_trade is None else str(result.best_trade),
        "worst_trade": None if result.worst_trade is None else str(result.worst_trade),
        "benchmark_result": str(benchmark),
        "excess_result": str(result.net_pnl - benchmark),
    }


def run_attempt(
    *,
    manifest_path: Path,
    dataset_path: Path,
    authorization_path: Path,
    progress_path: Path,
    result_path: Path,
    execution_head: str,
    attempt_id: int,
) -> Path:
    """Execute exactly one authorized attempt and make files, not stdout, authoritative."""
    if result_path.exists():
        raise FileExistsError(f"immutable result artifact already exists: {result_path}")
    attempt_id = _validate_attempt_id(attempt_id)
    authorization, authorization_digest, attempt_history = _load_authorization(
        authorization_path, execution_head=execution_head, attempt_id=attempt_id
    )
    prepared = prepare_locked_replay(
        manifest_path,
        dataset_path,
        expected_manifest_digest=str(authorization["original_manifest_digest"]),
    )
    _validate_authority(authorization, prepared)
    started = time.monotonic()
    sequence = 0

    def observe(progress: ReplayProgress) -> None:
        nonlocal sequence
        if progress.evaluation_processed % CHECKPOINT_EVERY_CANDLES and (
            progress.evaluation_processed != progress.evaluation_total
        ):
            return
        sequence += 1
        elapsed = time.monotonic() - started
        _atomic_replace(
            progress_path,
            _progress_payload(
                attempt_id=attempt_id,
                attempt_history=attempt_history,
                authorization=authorization,
                authorization_digest=authorization_digest,
                prepared=prepared,
                progress=progress,
                sequence=sequence,
                elapsed_seconds=elapsed,
            ),
        )
        percent = progress.evaluation_processed / progress.evaluation_total * 100
        print(
            f"EMA Frozen OOS — Attempt {attempt_id}: {progress.evaluation_processed}/"
            f"{progress.evaluation_total} ({percent:.1f}%) status=RUNNING",
            flush=True,
        )

    result = compare_strategy(
        prepared.dataset.candles,
        prepared.strategy,
        prepared.config,
        evaluation_start_index=prepared.evaluation_start_index,
        progress_observer=observe,
    )
    elapsed = time.monotonic() - started
    payload: dict[str, Any] = {
        "identity": RESULT_IDENTITY,
        "attempt_id": attempt_id,
        "attempt_history": list(attempt_history),
        "authorization_digest": authorization_digest,
        "original_manifest_digest": prepared.manifest_digest,
        "execution_source_commit": execution_head,
        "dataset_digest": prepared.dataset.metadata.candles_sha256,
        "strategy_identity": authorization["strategy_identity"],
        "strategy_config_fingerprint": authorization["strategy_config_fingerprint"],
        "parameters": authorization["parameters"],
        "symbol": prepared.dataset.metadata.symbol,
        "interval": prepared.dataset.metadata.interval,
        "oos_start_utc": prepared.oos_start_utc,
        "oos_end_utc": prepared.oos_end_utc,
        "oos_candle_count": len(prepared.dataset.candles) - prepared.evaluation_start_index,
        "starting_equity": str(prepared.config.starting_equity),
        "fee_rate_per_fill": str(prepared.config.fee_rate),
        "benchmark": prepared.benchmark,
        "metrics_lock": list(prepared.metrics),
        "quality_status": "VALIDATED_ORDERED_GAP_FREE_LOCKED_DATASET",
        "resume_supported": False,
        "checkpoint_write_count": sequence,
        "elapsed_seconds": round(elapsed, 3),
        "candles_per_second": round(
            (len(prepared.dataset.candles) - prepared.evaluation_start_index) / elapsed, 6
        ),
        "completed_at_utc": datetime.now(UTC).isoformat(),
        "oos_executed": True,
        "oos_result_observed": True,
        "final_metrics": _result_metrics(result, prepared),
    }
    payload["result_digest"] = _digest(payload, version="frozen-oos-result/v1")
    _atomic_create(result_path, payload)
    print(
        f"EMA Frozen OOS — Attempt {attempt_id}: status=COMPLETE result={result_path}", flush=True
    )
    return result_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--progress", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--failure", type=Path, required=True)
    parser.add_argument("--execution-head", required=True)
    parser.add_argument("--attempt-id", type=int, required=True)
    arguments = parser.parse_args()
    try:
        run_attempt(
            manifest_path=arguments.manifest,
            dataset_path=arguments.dataset,
            authorization_path=arguments.authorization,
            progress_path=arguments.progress,
            result_path=arguments.result,
            execution_head=arguments.execution_head,
            attempt_id=arguments.attempt_id,
        )
    except Exception as error:  # noqa: BLE001 - persist every bounded runner failure.
        if not arguments.failure.exists():
            _atomic_create(
                arguments.failure,
                {
                    "identity": FAILURE_IDENTITY,
                    "attempt_id": arguments.attempt_id,
                    "status": "FAILED",
                    "failure_category": type(error).__name__,
                    "explanation": str(error),
                    "result_observed": False,
                },
            )
        print(f"EMA Frozen OOS — Attempt {arguments.attempt_id}: status=FAILED", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
