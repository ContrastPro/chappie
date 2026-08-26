"""Fail-closed preparation for one frozen, pre-OOS strategy evaluation.

This module validates authority and prepares replay inputs. It deliberately
does not call ``compare_strategy`` or inspect any OOS result.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

from app.research.strategy.history import HistoricalDataset, load_dataset
from app.research.strategy.lab import StrategyComparisonConfig
from app.strategies.factory import StrategySettings, build_strategy
from app.strategies.trend_filtered import TREND_FILTERED_STATE_IDENTITY, TrendFilteredConfig

IDENTITY = "chappie_frozen_strategy_evaluation/1.0.0"


def _canonical_sha256(value: object, *, algorithm_version: str) -> str:
    """Match the immutable manifest's simple JSON digest envelope locally."""
    envelope = {"algorithm_version": algorithm_version, "payload": value}
    payload = json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PreparedLockedReplay:
    """Validated inputs for the locked replay; no execution result is present."""

    dataset: HistoricalDataset
    strategy: StrategySettings
    source_commit: str
    manifest_digest: str
    evaluation_start_index: int
    oos_start_utc: str
    oos_end_utc: str
    config: StrategyComparisonConfig
    benchmark: str
    metrics: tuple[str, ...]
    preparation_digest: str


def _utc_ms(value: object) -> int:
    if not isinstance(value, str):
        raise TypeError("locked timestamp must be text")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("locked timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise ValueError("locked timestamp must be UTC")
    return int(parsed.astimezone(UTC).timestamp() * 1_000)


def _require_span(dataset: HistoricalDataset, span: object, *, offset: int) -> int:
    if not isinstance(span, dict) or not isinstance(span.get("count"), int):
        raise TypeError("locked split is invalid")
    count = span["count"]
    if count <= 0 or offset + count > len(dataset.candles):
        raise ValueError("locked split count is invalid")
    segment = dataset.candles[offset : offset + count]
    last_boundary = _utc_ms(span.get("last_close_utc"))
    if segment[0].open_time_ms != _utc_ms(span.get("first_open_utc")) or last_boundary not in {
        segment[-1].close_time_ms,
        segment[-1].close_time_ms + 1,
    }:
        raise ValueError("locked split boundary is absent")
    return offset + count


def prepare_locked_replay(
    manifest_path: Path,
    dataset_path: Path,
    *,
    expected_manifest_digest: str,
) -> PreparedLockedReplay:
    """Read and validate an externally authorized pre-OOS lock without replaying it."""
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("identity") != IDENTITY:
        raise ValueError("invalid frozen evaluation manifest identity")
    if raw.get("created_before_oos") is not True:
        raise ValueError("frozen evaluation was not created before OOS")
    manifest_digest = raw.pop("manifest_digest", None)
    calculated_digest = _canonical_sha256(raw, algorithm_version="frozen-strategy-evaluation/v1")
    if not isinstance(manifest_digest, str) or manifest_digest != calculated_digest:
        raise ValueError("frozen evaluation manifest digest mismatch")
    if manifest_digest != expected_manifest_digest:
        raise ValueError("frozen evaluation authority does not match expected lock")
    source_commit = raw.get("source_commit")
    if (
        not isinstance(source_commit, str)
        or len(source_commit) != 40
        or any(character not in "0123456789abcdef" for character in source_commit)
    ):
        raise ValueError("frozen evaluation source binding is invalid")

    dataset = load_dataset(dataset_path)
    locked_dataset = raw.get("dataset")
    strategy_raw = raw.get("strategy")
    splits = raw.get("splits")
    evaluation = raw.get("evaluation")
    if not all(
        isinstance(value, dict) for value in (locked_dataset, strategy_raw, splits, evaluation)
    ):
        raise ValueError("frozen evaluation manifest sections are invalid")
    complete_span = locked_dataset.get("complete_span")
    if (
        locked_dataset.get("schema") != "chappie-historical-candles/v1"
        or locked_dataset.get("candles_sha256") != dataset.metadata.candles_sha256
        or locked_dataset.get("symbol") != dataset.metadata.symbol
        or locked_dataset.get("interval") != dataset.metadata.interval
        or locked_dataset.get("candle_count") != len(dataset.candles)
    ):
        raise ValueError("locked dataset binding mismatch")
    _require_span(dataset, complete_span, offset=0)
    if complete_span.get("count") != len(dataset.candles):
        raise ValueError("locked complete dataset span is invalid")

    parameters = strategy_raw.get("parameters")
    if not isinstance(parameters, dict) or strategy_raw.get("version") != "1.0.0":
        raise ValueError("locked strategy identity or parameters mismatch")
    if strategy_raw.get("name") == "ema_crossover":
        if set(parameters) != {"fast_window", "slow_window"} or not all(
            isinstance(parameters[key], int) for key in parameters
        ):
            raise ValueError("locked strategy identity or parameters mismatch")
        expected_fingerprint = _canonical_sha256(
            {
                "kind": "strategy",
                "configuration": {
                    "name": "ema_crossover",
                    "version": "1.0.0",
                    "parameters": parameters,
                },
            },
            algorithm_version="config/v1",
        )
        settings = StrategySettings(
            name="ema_crossover",
            ema_fast_window=parameters["fast_window"],
            ema_slow_window=parameters["slow_window"],
        )
    elif strategy_raw.get("name") == "trend_filtered":
        expected_parameters = {
            "fast_window": 12,
            "slow_window": 26,
            "atr_window": 14,
            "minimum_spread_bps": "8",
            "minimum_atr_bps": "5",
        }
        if parameters != expected_parameters:
            raise ValueError("locked trend strategy parameters mismatch")
        if raw.get("experiment_identity") != "TREND_FILTERED_BASELINE_001":
            raise ValueError("locked trend experiment identity mismatch")
        stateful_replay = raw.get("stateful_replay")
        if (
            not isinstance(stateful_replay, dict)
            or stateful_replay.get("state_identity") != TREND_FILTERED_STATE_IDENTITY
        ):
            raise ValueError("locked trend state identity mismatch")
        settings = StrategySettings(
            name="trend_filtered",
            ema_fast_window=12,
            ema_slow_window=26,
            trend_atr_window=14,
            trend_minimum_spread_bps=Decimal(8),
            trend_minimum_atr_bps=Decimal(5),
        )
        expected_fingerprint = TrendFilteredConfig().strategy_name
        trend = build_strategy(settings, _NoIds())
        expected_fingerprint = trend._config_fingerprint()  # type: ignore[attr-defined]
    else:
        raise ValueError("locked strategy identity or parameters mismatch")
    if strategy_raw.get("config_fingerprint") != expected_fingerprint:
        raise ValueError("locked strategy fingerprint mismatch")
    strategy = build_strategy(settings, _NoIds())

    if set(splits) != {"train", "validation", "final_oos"}:
        raise ValueError("locked splits are invalid")
    after_train = _require_span(dataset, splits["train"], offset=0)
    after_validation = _require_span(dataset, splits["validation"], offset=after_train)
    start_index = after_validation
    after_oos = _require_span(dataset, splits["final_oos"], offset=after_validation)
    if after_oos != len(dataset.candles) or start_index < strategy.required_history:
        raise ValueError("insufficient locked OOS or warmup history")
    oos = splits["final_oos"]

    risk = evaluation.get("risk")
    decision_policy = evaluation.get("decision_policy")
    if strategy_raw.get("name") == "trend_filtered":
        decision_policy = raw.get("decision_policy")
        required_metrics = {
            "decisions",
            "fills",
            "closed_trades",
            "gross_pnl",
            "fees",
            "net_pnl",
            "wins",
            "losses",
            "expectancy",
            "profit_factor",
            "max_drawdown",
            "turnover",
            "exposure_time",
            "benchmark_result",
            "excess_result",
        }
        required_churn_metrics = {
            "fills_per_evaluation_candle",
            "turnover_per_starting_equity",
            "fees_per_starting_equity",
            "average_fee_per_closed_trade",
            "closed_trades",
            "gross_pnl_before_fees",
            "net_pnl_after_fees",
        }
        if (
            set(evaluation.get("metrics", ())) != required_metrics
            or set(evaluation.get("churn_metrics", ())) != required_churn_metrics
            or risk
            != {
                "long_only": True,
                "max_fee_rate": "0.001",
                "max_order_notional": "1000",
                "max_position_notional": "5000",
            }
        ):
            raise ValueError("locked trend evaluation authority is invalid")
    if (
        not isinstance(evaluation.get("metrics"), list)
        or not evaluation["metrics"]
        or not isinstance(decision_policy, dict)
        or not decision_policy
        or not isinstance(evaluation.get("benchmark"), str)
        or not evaluation["benchmark"]
        or not isinstance(risk, dict)
    ):
        raise ValueError("locked evaluation authority is invalid")
    try:
        config = StrategyComparisonConfig(
            starting_equity=Decimal(evaluation["starting_equity"]),
            fee_rate=Decimal(evaluation["fee_rate_per_fill"]),
            target_exposure=Decimal(evaluation["target_exposure"]),
        )
    except (KeyError, ValueError) as error:
        raise ValueError("locked financial assumptions are invalid") from error
    preparation = {
        "manifest_digest": manifest_digest,
        "dataset_digest": dataset.metadata.candles_sha256,
        "evaluation_start_index": start_index,
    }
    return PreparedLockedReplay(
        dataset=dataset,
        strategy=settings,
        source_commit=source_commit,
        manifest_digest=manifest_digest,
        evaluation_start_index=start_index,
        oos_start_utc=oos["first_open_utc"],
        oos_end_utc=oos["last_close_utc"],
        config=config,
        benchmark=evaluation["benchmark"],
        metrics=tuple(evaluation["metrics"]),
        preparation_digest=_canonical_sha256(preparation, algorithm_version="locked-replay/v1"),
    )


class _NoIds:
    def next_id(self) -> str:
        return "locked-preparation"
