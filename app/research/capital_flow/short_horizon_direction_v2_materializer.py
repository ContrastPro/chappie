"""Research-only, resumable Short-Horizon Direction V2 feature store.

No targets are read or written here.  In particular, CSV consumption stops before
the sealed Validation boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

from .binance_breadth_authority_v2 import (
    BINANCE_BREADTH_AUTHORITY_V2_IDENTITY,
    BINANCE_BREADTH_V2_SCHEMA,
)
from .free_v2_formulas import FEATURE_UNAVAILABLE_STATES
from .free_v2_materializer import BINANCE_V2_AUTHORITY_DIGEST
from .free_v2_materializer import FEATURE_ORDER as CAPITAL_FLOW_FEATURE_ORDER
from .short_horizon_direction_v2_contract import (
    EXPERIMENT_IDENTITY,
    FEATURE_FAMILIES,
    NEW_FEATURES,
    RESEARCH_END_EXCLUSIVE,
    RESEARCH_START,
)
from .tsunami_research_targets import BINANCE_V2_ROOT, BTC_CANONICAL_RELATIVE_PATH

FEATURE_STORE_ROOT = Path(
    "E:/projects/chappie/data/research/analytical/market_brain/short_horizon_direction_v2"
)
MATRIX_ROOT = FEATURE_STORE_ROOT / "experiment_matrix"
CAPITAL_FLOW_ROOT = Path("E:/projects/chappie/data/research/analytical/capital_flow/free_v2")
IDENTITY = "CHAPPIE_SHORT_HORIZON_DIRECTION_V2_FEATURE_STORE/1.0.0"
ROW_GROUP_ROWS = 20_160
COMPRESSION = "zstd"
STATE_AVAILABLE = "AVAILABLE"
STATE_INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
# Capital-flow partitions are already authoritative in this exact code order.
# New technical features use AVAILABLE/INSUFFICIENT_HISTORY from the same mapping.
STATE_CODES = {
    name: index for index, name in enumerate((STATE_AVAILABLE, *FEATURE_UNAVAILABLE_STATES))
}
FINAL_FEATURE_ORDER = CAPITAL_FLOW_FEATURE_ORDER + tuple(item.name for item in NEW_FEATURES)
RUNTIME_MEMORY_BUDGET_BYTES = 10 * 1024**3
FOLD_CACHE_MAX_ENTRIES = 3


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    """Publish JSON only after its complete canonical representation is on disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_bytes(_canonical(payload))
    os.replace(temporary, path)


def _timestamp_text(value: object) -> str:
    import pandas as pd

    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC").isoformat().replace("+00:00", "Z")


def _feature_definition_digest() -> str:
    return _digest(
        {
            "capital_flow_order": list(CAPITAL_FLOW_FEATURE_ORDER),
            "new_features": [
                {
                    "name": item.name,
                    "family": item.family,
                    "formula": item.formula,
                    "completed_timeframes": list(item.completed_timeframes),
                }
                for item in NEW_FEATURES
            ],
        }
    )


def _causal_contract_identity() -> str:
    return _digest(
        {
            "research_start": RESEARCH_START.isoformat(),
            "research_end_exclusive": RESEARCH_END_EXCLUSIVE.isoformat(),
            "feature_definition_digest": _feature_definition_digest(),
            "completed_candles_only": True,
        }
    )


def _partition_frame(table):  # type: ignore[no-untyped-def]
    import pandas as pd

    frame = table.to_pandas().drop(columns=["year", "month"], errors="ignore")
    if "timestamp_utc" not in frame:
        raise ValueError("partition missing timestamp_utc")
    frame["timestamp_utc"] = pd.to_datetime(frame["timestamp_utc"], utc=True)
    return frame.sort_values("timestamp_utc", kind="mergesort").reset_index(drop=True)


def partition_semantic_digest(table, feature_order: tuple[str, ...] = FINAL_FEATURE_ORDER) -> str:  # type: ignore[no-untyped-def]
    """Digest logical persisted rows, never a Parquet physical representation."""
    frame = _partition_frame(table)
    columns = ["timestamp_utc"] + [
        column for feature in feature_order for column in (feature, f"{feature}__state")
    ]
    missing = set(columns).difference(frame.columns)
    if missing:
        raise ValueError(f"partition schema missing columns: {sorted(missing)}")
    rows = []
    for row in frame.loc[:, columns].itertuples(index=False, name=None):
        values = [_timestamp_text(row[0])]
        for value in row[1:]:
            if hasattr(value, "item"):
                value = value.item()
            if value is None or (isinstance(value, float) and __import__("math").isnan(value)):
                values.append(None)
            elif isinstance(value, float):
                values.append(value.hex())
            else:
                values.append(value)
        rows.append(values)
    return _digest({"columns": columns, "rows": rows})


def partition_quality_evidence(
    table, feature_order: tuple[str, ...] = FINAL_FEATURE_ORDER
) -> dict[str, object]:  # type: ignore[no-untyped-def]
    import numpy as np

    frame = _partition_frame(table)
    timestamps = frame["timestamp_utc"]
    schema = [
        [field.name, str(field.type)]
        for field in table.schema
        if field.name not in {"year", "month"}
    ]
    states = {name: 0 for name in STATE_CODES}
    features: dict[str, object] = {}
    violations = {
        "nonfinite_available": 0,
        "available_null": 0,
        "unavailable_non_null": 0,
        "unknown_state": 0,
        "duplicate_timestamp": int(timestamps.duplicated().sum()),
        "ordering_timeline": 0,
    }
    if len(timestamps) > 1:
        violations["ordering_timeline"] = int(
            (timestamps.diff().dropna() != __import__("pandas").Timedelta(minutes=1)).sum()
        )
    for feature in feature_order:
        values = __import__("pandas").to_numeric(frame[feature], errors="coerce")
        codes = __import__("pandas").to_numeric(frame[f"{feature}__state"], errors="coerce")
        available = codes == STATE_CODES[STATE_AVAILABLE]
        unknown = ~codes.isin(list(STATE_CODES.values()))
        unavailable = ~available & ~unknown
        counts = {name: int((codes == code).sum()) for name, code in STATE_CODES.items()}
        for name, count in counts.items():
            states[name] += count
        features[feature] = {
            "states": counts,
            "null": int(values.isna().sum()),
            "non_null": int(values.notna().sum()),
            "finite_available": int(np.isfinite(values[available]).sum()),
        }
        violations["nonfinite_available"] += int((available & ~np.isfinite(values)).sum())
        violations["available_null"] += int((available & values.isna()).sum())
        violations["unavailable_non_null"] += int((unavailable & values.notna()).sum())
        violations["unknown_state"] += int(unknown.sum())
    return {
        "row_count": len(frame),
        "first_timestamp": _timestamp_text(timestamps.iloc[0]) if len(frame) else None,
        "last_timestamp": _timestamp_text(timestamps.iloc[-1]) if len(frame) else None,
        "schema": schema,
        "feature_counts": features,
        "state_counts": states,
        "violations": violations,
    }


def verify_persisted_partition(
    path: Path, *, expected: dict[str, object] | None = None
) -> dict[str, object]:
    import pyarrow.parquet as pq

    parquet = pq.ParquetFile(path)
    table = parquet.read()
    quality = partition_quality_evidence(table)
    evidence = {
        **quality,
        "row_groups": parquet.num_row_groups,
        "physical_bytes": path.stat().st_size,
        "physical_sha256": _sha(path),
        "semantic_sha256": partition_semantic_digest(table),
    }
    if expected is not None and evidence != expected:
        raise ValueError("persisted partition evidence mismatch")
    if any(int(value) for value in quality["violations"].values()):
        raise ValueError("partition quality violation")
    return evidence


def dataset_semantic_digest(
    *,
    source_identity: str,
    source_digest: str,
    partitions: list[dict[str, object]],
    quality: dict[str, object],
) -> str:
    return _digest(
        {
            "dataset_identity": IDENTITY,
            "experiment_identity": EXPERIMENT_IDENTITY,
            "source_identity": source_identity,
            "source_digest": source_digest,
            "feature_definition_digest": _feature_definition_digest(),
            "feature_order": list(FINAL_FEATURE_ORDER),
            "state_mapping": STATE_CODES,
            "research_bounds": [RESEARCH_START.isoformat(), RESEARCH_END_EXCLUSIVE.isoformat()],
            "partitions": [
                {key: item[key] for key in ("partition", "semantic_sha256", "row_count")}
                for item in partitions
            ],
            "quality": quality,
            "causal_contract_identity": _causal_contract_identity(),
        }
    )


def manifest_semantic_digest(manifest: dict[str, object]) -> str:
    payload = dict(manifest)
    payload.pop("semantic_digest", None)
    # Paths are operational metadata and explicitly excluded from authority identity.
    payload.pop("root", None)
    payload.pop("created_at", None)
    return _digest(payload)


def verify_manifest(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("semantic_digest") != manifest_semantic_digest(payload):
        raise ValueError("manifest semantic digest mismatch")
    return payload


def verify_feature_store(root: Path = FEATURE_STORE_ROOT) -> dict[str, object]:
    """Independently validate an already-published store using persisted files only."""
    import pandas as pd

    root = Path(root)
    manifest = verify_manifest(root / "manifest.json")
    if (
        manifest.get("identity") != IDENTITY
        or manifest.get("experiment_identity") != EXPERIMENT_IDENTITY
    ):
        raise ValueError("feature-store identity mismatch")
    if (
        manifest.get("source_authority_identity") != BINANCE_BREADTH_AUTHORITY_V2_IDENTITY
        or manifest.get("source_authority_digest") != BINANCE_V2_AUTHORITY_DIGEST
    ):
        raise ValueError("feature-store source binding mismatch")
    if (
        manifest.get("feature_order") != list(FINAL_FEATURE_ORDER)
        or manifest.get("availability_states") != STATE_CODES
    ):
        raise ValueError("feature-store feature/state binding mismatch")
    if (
        manifest.get("feature_definition_digest") != _feature_definition_digest()
        or manifest.get("causal_contract_identity") != _causal_contract_identity()
    ):
        raise ValueError("feature-store causal contract binding mismatch")
    inventory = manifest.get("partitions")
    if not isinstance(inventory, list) or len(inventory) != 17:
        raise ValueError("feature-store partition inventory mismatch")
    persisted: list[dict[str, object]] = []
    for item in inventory:
        if not isinstance(item, dict) or not isinstance(item.get("partition"), str):
            raise TypeError("invalid partition inventory item")
        partition = str(item["partition"])
        data = root / "experiment_matrix" / partition / "data.parquet"
        checkpoint = root / "experiment_matrix" / partition / "checkpoint.json"
        evidence = verify_persisted_partition(data)
        if {"partition": partition, **evidence} != item:
            raise ValueError(f"partition inventory evidence mismatch: {partition}")
        check = json.loads(checkpoint.read_text(encoding="utf-8"))
        if check.get("status") != "VERIFIED" or check.get("evidence") != evidence:
            raise ValueError(f"partition checkpoint mismatch: {partition}")
        persisted.append({"partition": partition, **evidence})
    persisted.sort(key=lambda item: str(item["partition"]))
    total = sum(int(item["row_count"]) for item in persisted)
    if (
        total != 744_480
        or persisted[0]["first_timestamp"] != "2024-01-01T00:00:00Z"
        or persisted[-1]["last_timestamp"] != "2025-05-31T23:59:00Z"
    ):
        raise ValueError("persisted whole-dataset bounds mismatch")
    for previous, current in pairwise(persisted):
        if pd.Timestamp(current["first_timestamp"]) - pd.Timestamp(
            previous["last_timestamp"]
        ) != pd.Timedelta(minutes=1):
            raise ValueError("persisted partition continuity mismatch")
    violations = {
        key: sum(int(item["violations"][key]) for item in persisted)
        for key in persisted[0]["violations"]
    }  # type: ignore[index]
    if any(violations.values()):
        raise ValueError("persisted feature-store quality violations")
    features: dict[str, object] = {}
    for feature in FINAL_FEATURE_ORDER:
        states = {
            state: sum(int(item["feature_counts"][feature]["states"][state]) for item in persisted)
            for state in STATE_CODES
        }  # type: ignore[index]
        features[feature] = {
            "states": states,
            "null": sum(int(item["feature_counts"][feature]["null"]) for item in persisted),
            "non_null": sum(int(item["feature_counts"][feature]["non_null"]) for item in persisted),
            "finite_available": sum(
                int(item["feature_counts"][feature]["finite_available"]) for item in persisted
            ),
        }  # type: ignore[index]
        counts = features[feature]
        if (
            sum(counts["states"].values()) != total
            or counts["states"][STATE_AVAILABLE] != counts["finite_available"]
            or counts["non_null"] != counts["finite_available"]
        ):  # type: ignore[index]
            raise ValueError(f"persisted feature reconciliation mismatch: {feature}")
    quality = {
        "row_count": total,
        "first_timestamp": persisted[0]["first_timestamp"],
        "last_timestamp": persisted[-1]["last_timestamp"],
        "violations": violations,
        "feature_counts": features,
    }
    digest = dataset_semantic_digest(
        source_identity=BINANCE_BREADTH_AUTHORITY_V2_IDENTITY,
        source_digest=BINANCE_V2_AUTHORITY_DIGEST,
        partitions=persisted,
        quality=quality,
    )
    if manifest.get("quality") != quality or manifest.get("dataset_semantic_digest") != digest:
        raise ValueError("persisted dataset digest/quality mismatch")
    return {
        "classification": "CHAPPIE_SHORT_HORIZON_DIRECTION_V2_FEATURE_STORE_VALIDATED",
        "dataset_identity": IDENTITY,
        "experiment_identity": EXPERIMENT_IDENTITY,
        "source_identity": BINANCE_BREADTH_AUTHORITY_V2_IDENTITY,
        "rows": total,
        "partitions": len(persisted),
        "dataset_semantic_digest": digest,
        "manifest_semantic_digest": manifest["semantic_digest"],
        "quality_violations": violations,
        "models_fitted": False,
        "targets_inspected": False,
        "metrics_produced": False,
        "validation_accessed": False,
        "blind_oos_accessed": False,
    }


def _projection_columns(features: tuple[str, ...]) -> list[str]:
    return ["timestamp_utc"] + [
        column for feature in features for column in (feature, f"{feature}__state")
    ]


def benchmark_persisted_feature_store(
    root: Path = FEATURE_STORE_ROOT, repeats: int = 3
) -> dict[str, object]:
    """Read-only persisted-matrix benchmark; deliberately has no source/model/target path."""
    import gc
    import statistics

    import pyarrow as pa
    import pyarrow.dataset as ds

    if repeats < 1 or repeats > 5:
        raise ValueError("benchmark repeats must be bounded")
    verified = verify_feature_store(root)
    manifest = verify_manifest(Path(root) / "manifest.json")
    family_map = manifest["feature_family_mapping"]
    groups = {
        "CAPITAL_FLOW": tuple(
            feature for feature in FINAL_FEATURE_ORDER if family_map[feature] == "CAPITAL_FLOW"
        ),
        "TECHNICAL": tuple(
            feature for feature in FINAL_FEATURE_ORDER if family_map[feature] == "TECHNICAL"
        ),
        "VOLUME": tuple(
            feature for feature in FINAL_FEATURE_ORDER if family_map[feature] == "VOLUME"
        ),
        "MARKET_STRUCTURE": tuple(
            feature for feature in FINAL_FEATURE_ORDER if family_map[feature] == "MARKET_STRUCTURE"
        ),
        "FULL_V2": FINAL_FEATURE_ORDER,
    }
    parquet_paths = sorted((Path(root) / "experiment_matrix").glob("year=*/month=*/data.parquet"))
    dataset = ds.dataset(parquet_paths, format="parquet")
    try:
        import psutil

        process = psutil.Process()
        baseline = process.memory_info()
        memory = lambda: {
            "private_bytes": int(getattr(process.memory_full_info(), "uss", 0)),
            "working_set_bytes": int(process.memory_info().rss),
        }
    except ImportError:
        baseline = None
        memory = lambda: {"private_bytes": None, "working_set_bytes": None}
    projections: dict[str, object] = {}
    for name, features in groups.items():
        times = []
        for _ in range(repeats):
            started = time.perf_counter()
            table = dataset.to_table(columns=_projection_columns(features))
            times.append(time.perf_counter() - started)
            del table
            gc.collect()
        ordered = sorted(times)
        projections[name] = {
            "rows": verified["rows"],
            "projected_columns": len(_projection_columns(features)),
            "median_seconds": statistics.median(times),
            "p25_seconds": ordered[(len(ordered) - 1) // 4],
            "p75_seconds": ordered[(3 * (len(ordered) - 1)) // 4],
            "memory": memory(),
        }

    def fold(hours: int, features: tuple[str, ...]) -> dict[str, object]:
        # Frozen Research-only train/assessment windows; no outcomes are read.
        columns = _projection_columns(features)
        train_end = pa.scalar(datetime(2025, 1, 1, tzinfo=UTC))
        assessment_end = pa.scalar(datetime(2025, 2, 1, tzinfo=UTC))
        train_dataset = ds.dataset(parquet_paths[:12], format="parquet")
        assessment_dataset = ds.dataset(parquet_paths[12:13], format="parquet")
        times = []
        for _ in range(repeats):
            started = time.perf_counter()
            train = train_dataset.to_table(
                columns=columns, filter=ds.field("timestamp_utc") < train_end
            )
            assessment = assessment_dataset.to_table(
                columns=columns,
                filter=(ds.field("timestamp_utc") >= train_end)
                & (ds.field("timestamp_utc") < assessment_end),
            )
            times.append(time.perf_counter() - started)
            train_rows, assessment_rows = train.num_rows, assessment.num_rows
            del train, assessment
            gc.collect()
        return {
            "train_rows": train_rows,
            "assessment_rows": assessment_rows,
            "selected_columns": len(columns),
            "median_seconds": statistics.median(times),
            "memory": memory(),
            "timestamp_predicate_pushdown": True,
            "partition_pruning": True,
        }

    folds = {
        "1h": {name: fold(1, groups[name]) for name in ("CAPITAL_FLOW", "TECHNICAL", "FULL_V2")},
        "4h": {name: fold(4, groups[name]) for name in ("CAPITAL_FLOW", "TECHNICAL", "FULL_V2")},
    }
    cache = BoundedFoldCache(max_entries=3, max_bytes=2 * 1024**3)
    cache_times = []
    for key in ("A", "A", "B", "C", "D"):
        started = time.perf_counter()
        try:
            cache.get(key)
            hit = True
        except KeyError:
            hit = False
            cache.put(
                key,
                dataset.to_table(columns=_projection_columns(groups["FULL_V2"]))
                .slice(0, 10_000)
                .to_pandas(),
            )
        cache_times.append(
            {
                "key": key,
                "hit": hit,
                "seconds": time.perf_counter() - started,
                "cache_bytes": cache._bytes,
            }
        )
    return {
        "classification": verified["classification"],
        "projections": projections,
        "folds": folds,
        "cache": {
            "max_entries": 3,
            "cap_bytes": 2 * 1024**3,
            "max_observed_bytes": max(item["cache_bytes"] for item in cache_times),
            "events": cache_times,
            "eviction_pass": len(cache._items) == 3 and "A" not in cache._items,
            "cap_enforced": cache._bytes <= cache.max_bytes,
        },
        "process_memory": {
            "baseline": None
            if baseline is None
            else {
                "private_bytes": int(getattr(baseline, "uss", 0)),
                "working_set_bytes": int(baseline.rss),
            },
            "final": memory(),
        },
        "query_pruning": {
            "column_projection": True,
            "timestamp_predicate": True,
            "partition_pruning": True,
        },
        "read_only": True,
        "models_fitted": False,
        "metrics_produced": False,
        "targets_inspected": False,
        "validation_accessed": False,
        "blind_oos_accessed": False,
        "raw_authority_rescanned": False,
        "features_recomputed": False,
        "repeated_joins": False,
        "dataset_semantic_digest": manifest["dataset_semantic_digest"],
        "manifest_semantic_digest": manifest["semantic_digest"],
    }


def _source_manifest(root: Path) -> dict[str, object]:
    payload = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if payload.get("authority_identity") != BINANCE_BREADTH_AUTHORITY_V2_IDENTITY:
        raise ValueError("BTC authority identity mismatch")
    if (
        payload.get("semantic_authority_digest") != BINANCE_V2_AUTHORITY_DIGEST
        or payload.get("status") != "VALIDATED"
    ):
        raise ValueError("BTC authority digest/status mismatch")
    if payload.get("canonical_schema") != list(BINANCE_BREADTH_V2_SCHEMA):
        raise ValueError("BTC OHLCV schema mismatch")
    return payload


def _resample(frame, rule: str):  # type: ignore[no-untyped-def]
    return (
        frame.resample(rule, label="right", closed="left")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna()
    )


def _rsi(close, span: int = 14):  # type: ignore[no-untyped-def]
    import numpy as np

    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    rs = (
        gain.ewm(alpha=1 / span, adjust=False, min_periods=span).mean()
        / loss.ewm(alpha=1 / span, adjust=False, min_periods=span).mean()
    )
    return 100 - 100 / (1 + rs.replace([np.inf, -np.inf], np.nan))


def _timeframe_features(candles, name: str):  # type: ignore[no-untyped-def]
    import numpy as np

    close, high, low, opened, volume = (
        candles[item] for item in ("close", "high", "low", "open", "volume")
    )
    result = {f"rsi_{name}": _rsi(close)}
    if name == "1h":
        tr = (
            __import__("pandas")
            .concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1)
            .max(axis=1)
        )
        atr = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
        std20 = close.rolling(20, min_periods=20).std(ddof=1)
        ema12, ema26 = (
            close.ewm(span=12, adjust=False, min_periods=12).mean(),
            close.ewm(span=26, adjust=False, min_periods=26).mean(),
        )
        hist = ema12 - ema26 - (ema12 - ema26).ewm(span=9, adjust=False, min_periods=9).mean()
        rng = high - low
        result.update(
            {
                "atr": atr,
                "normalized_atr": atr / close,
                "return_1h": np.log(close / close.shift(1)),
                "return_4h": np.log(close / close.shift(4)),
                "trend_strength": np.log(close / close.shift(4)).abs()
                / np.log(close / close.shift()).abs().rolling(4).sum(),
                "macd_histogram": hist,
                "bollinger_location": (close - close.rolling(20, min_periods=20).mean())
                / (2 * std20),
                "body_to_range": (close - opened) / rng,
                "upper_wick_to_range": (
                    high.__sub__(__import__("pandas").concat([opened, close], axis=1).max(axis=1))
                )
                / rng,
                "lower_wick_to_range": (
                    __import__("pandas").concat([opened, close], axis=1).min(axis=1) - low
                )
                / rng,
                "close_location": (close - low) / rng,
                "range_to_atr": rng / atr,
                "consecutive_direction": np.sign(close.diff())
                .groupby((np.sign(close.diff()) != np.sign(close.diff()).shift()).cumsum())
                .cumcount()
                .add(1)
                .mul(np.sign(close.diff()))
                .clip(-5, 5),
                "log_volume": np.log(volume),
                "relative_volume": volume / volume.rolling(24, min_periods=24).mean(),
                "volume_zscore": (volume - volume.rolling(24, min_periods=24).mean())
                / volume.rolling(24, min_periods=24).std(ddof=1),
                "volume_acceleration": np.log(volume / volume.shift()),
                "vwap": (
                    ((high + low + close) / 3 * volume).rolling(20, min_periods=20).sum()
                    / volume.rolling(20, min_periods=20).sum()
                ),
            }
        )
        result["volume_spike"] = (result["volume_zscore"] >= 2).astype(float)
        result["vwap_distance"] = (close - result["vwap"]) / result["vwap"]
        result["vwap_slope"] = np.log(result["vwap"] / result["vwap"].shift())
        prior_high, prior_low = (
            high.shift().rolling(20, min_periods=20).max(),
            low.shift().rolling(20, min_periods=20).min(),
        )
        breakout = ((close > prior_high).astype(int) - (close < prior_low).astype(int)).astype(
            float
        )
        result.update(
            {
                "breakout_20h": breakout,
                "failed_breakout_20h": (
                    (breakout.shift() != 0) & (close <= prior_high) & (close >= prior_low)
                ).astype(float),
                "retest_20h": (
                    (breakout.shift() != 0)
                    & (
                        (close - prior_high.where(breakout.shift() > 0, prior_low)).abs()
                        <= 0.25 * atr
                    )
                ).astype(float),
                "compression_1h": atr / atr.rolling(20, min_periods=20).mean(),
                "support_resistance_distance_1h": np.minimum(
                    (close - prior_low).abs(), (close - prior_high).abs()
                )
                / atr,
                "inside_outside_bar_1h": ((high < high.shift()) & (low > low.shift()))
                .astype(float)
                .mul(-1)
                + ((high > high.shift()) & (low < low.shift())).astype(float),
                "engulfing_body_1h": (
                    ((close - opened) * (close.shift() - opened.shift()) < 0)
                    & (np.minimum(opened, close) <= np.minimum(opened.shift(), close.shift()))
                    & (np.maximum(opened, close) >= np.maximum(opened.shift(), close.shift()))
                ).astype(float)
                * np.sign(close - opened),
            }
        )
        result["expansion_1h"] = (result["compression_1h"] >= 1.5).astype(float)
        result["breakout_confirmation"] = breakout * result["relative_volume"]
    return result


def confirmed_swings(high, low, *, left: int = 2, right: int = 2):  # type: ignore[no-untyped-def]
    """Latest confirmed pivots, known only on completion of the right-th bar.

    Equal highs/lows choose the earliest candidate: strict on the left and weak on
    the right.  A pivot at i is therefore first observable at i + right.
    """
    import numpy as np
    import pandas as pd

    latest_high = pd.Series(np.nan, index=high.index)
    latest_low = latest_high.copy()
    h_value = l_value = np.nan
    for confirmed in range(left + right, len(high)):
        pivot = confirmed - right
        h = high.iloc[pivot]
        l = low.iloc[pivot]
        if (
            h > high.iloc[pivot - left : pivot].max()
            and h >= high.iloc[pivot + 1 : pivot + right + 1].max()
        ):
            h_value = h
        if (
            l < low.iloc[pivot - left : pivot].min()
            and l <= low.iloc[pivot + 1 : pivot + right + 1].min()
        ):
            l_value = l
        latest_high.iloc[confirmed], latest_low.iloc[confirmed] = h_value, l_value
    return latest_high.ffill(), latest_low.ffill()


def build_new_features(minute):  # type: ignore[no-untyped-def]
    """Build 45 causal V2 values indexed at minute forecast timestamps."""
    import numpy as np
    import pandas as pd

    frames = {
        name: _resample(minute, rule)
        for name, rule in (
            ("5m", "5min"),
            ("15m", "15min"),
            ("1h", "1h"),
            ("4h", "4h"),
            ("1D", "1D"),
        )
    }
    features = {}
    for name, frame in frames.items():
        features.update(_timeframe_features(frame, name))
    # Explicit released 15m/4h values and completed-candle asof join are formed below.
    features["return_15m"] = np.log(frames["15m"].close / frames["15m"].close.shift())
    features["volatility_ratio_5m_1h"] = frames["5m"].close.pct_change().rolling(
        12, min_periods=12
    ).std(ddof=1) / frames["1h"].close.pct_change().rolling(24, min_periods=24).std(ddof=1)
    features["price_volume_divergence_4h"] = np.sign(
        np.log(frames["4h"].close / frames["4h"].close.shift())
    ) * np.sign(np.log(frames["4h"].volume / frames["4h"].volume.shift()))
    high4, low4, close4 = (frames["4h"][v] for v in ("high", "low", "close"))
    swing_high4, swing_low4 = confirmed_swings(high4, low4)
    swing_high1, swing_low1 = confirmed_swings(frames["1h"].high, frames["1h"].low)
    prior_high1, prior_low1 = swing_high1.shift(), swing_low1.shift()
    breakout1 = (
        (frames["1h"].close > prior_high1).astype(int)
        - (frames["1h"].close < prior_low1).astype(int)
    ).astype(float)
    atr1 = features["atr"]
    features.update(
        {
            "swing_trend_4h": np.sign(
                (swing_high4 - swing_high4.shift()) + (swing_low4 - swing_low4.shift())
            ),
            "distance_swing_high_4h": (close4 - swing_high4) / close4,
            "distance_swing_low_4h": (close4 - swing_low4) / close4,
            "breakout_20h": breakout1,
            "failed_breakout_20h": (
                (breakout1.shift() != 0)
                & (frames["1h"].close <= prior_high1)
                & (frames["1h"].close >= prior_low1)
            ).astype(float),
            "retest_20h": (
                (breakout1.shift() != 0)
                & (
                    (
                        frames["1h"].close - prior_high1.where(breakout1.shift() > 0, prior_low1)
                    ).abs()
                    <= 0.25 * atr1
                )
            ).astype(float),
            "support_resistance_distance_1h": np.minimum(
                (frames["1h"].close - swing_low1).abs(), (frames["1h"].close - swing_high1).abs()
            )
            / atr1,
        }
    )

    def asof(series):
        left = (
            minute[["close"]]
            .reset_index()
            .rename(columns={minute.index.name or "index": "timestamp"})
        )
        right = (
            series.rename("value")
            .reset_index()
            .rename(columns={series.index.name or "index": "completed"})
        )
        return pd.merge_asof(
            left, right, left_on="timestamp", right_on="completed", direction="backward"
        ).set_index("timestamp")["value"]

    out = pd.DataFrame(index=minute.index)
    mapping = {
        "technical.rsi_5m": "rsi_5m",
        "technical.rsi_15m": "rsi_15m",
        "technical.rsi_1h": "rsi_1h",
        "technical.rsi_4h": "rsi_4h",
        "technical.rsi_1h_slope": "rsi_1h",
        "technical.rsi_1h_distance_50": "rsi_1h",
        "technical.rsi_1h_extreme": "rsi_1h",
        "technical.normalized_atr_1h": "normalized_atr",
        "technical.volatility_ratio_5m_1h": "volatility_ratio_5m_1h",
        "technical.return_15m": "return_15m",
        "technical.return_1h": "return_1h",
        "technical.return_4h": "return_4h",
        "technical.trend_strength_1h": "trend_strength",
        "technical.macd_histogram_1h": "macd_histogram",
        "technical.bollinger_location_1h": "bollinger_location",
        "technical.body_to_range_1h": "body_to_range",
        "technical.upper_wick_to_range_1h": "upper_wick_to_range",
        "technical.lower_wick_to_range_1h": "lower_wick_to_range",
        "technical.close_location_1h": "close_location",
        "technical.range_to_atr_1h": "range_to_atr",
        "technical.consecutive_direction_1h": "consecutive_direction",
        "volume.log_volume_1h": "log_volume",
        "volume.relative_volume_1h": "relative_volume",
        "volume.zscore_1h": "volume_zscore",
        "volume.acceleration_1h": "volume_acceleration",
        "volume.spike_1h": "volume_spike",
        "volume.price_volume_divergence_4h": "price_volume_divergence_4h",
        "volume.breakout_confirmation_1h": "breakout_confirmation",
        "volume.vwap_distance_1h": "vwap_distance",
        "volume.vwap_slope_1h": "vwap_slope",
        "structure.swing_trend_4h": "swing_trend_4h",
        "structure.distance_swing_high_4h": "distance_swing_high_4h",
        "structure.distance_swing_low_4h": "distance_swing_low_4h",
        "structure.breakout_20h": "breakout_20h",
        "structure.failed_breakout_20h": "failed_breakout_20h",
        "structure.retest_20h": "retest_20h",
        "structure.compression_1h": "compression_1h",
        "structure.expansion_1h": "expansion_1h",
        "structure.support_resistance_distance_1h": "support_resistance_distance_1h",
        "structure.inside_outside_bar_1h": "inside_outside_bar_1h",
        "structure.engulfing_body_1h": "engulfing_body_1h",
    }
    for final, internal in mapping.items():
        out[final] = asof(features[internal])
    out["mtf.trend_consensus"] = (
        np.sign(asof(features["return_1h"]))
        + np.sign(asof(features["return_15m"]))
        + np.sign(asof(frames["5m"].close.pct_change()))
        + np.sign(asof(frames["4h"].close.pct_change()))
    ) / 4
    out["mtf.momentum_consensus"] = (
        np.sign(asof(features["rsi_5m"]) - 50)
        + np.sign(asof(features["rsi_15m"]) - 50)
        + np.sign(asof(features["rsi_1h"]) - 50)
        + np.sign(asof(features["rsi_4h"]) - 50)
    ) / 4
    regimes = []
    for frame_name, candle in frames.items():
        if frame_name == "1d":
            continue
        tr = pd.concat(
            [
                candle.high - candle.low,
                (candle.high - candle.close.shift()).abs(),
                (candle.low - candle.close.shift()).abs(),
            ],
            axis=1,
        ).max(axis=1)
        normalized = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean() / candle.close
        regimes.append(
            (asof(normalized) > asof(normalized.rolling(20, min_periods=20).median())).astype(float)
        )
    out["mtf.volatility_alignment"] = sum(regimes) / 4
    out["mtf.structure_alignment"] = (
        np.sign(asof(features["breakout_20h"])) + np.sign(asof(features["swing_trend_4h"]))
    ) / 2
    return out.reindex(columns=tuple(item.name for item in NEW_FEATURES))


@dataclass(slots=True)
class BoundedFoldCache:
    """LRU for immutable column-pruned fold data; never stores fitted state."""

    max_entries: int = FOLD_CACHE_MAX_ENTRIES
    max_bytes: int = 2 * 1024**3
    _items: OrderedDict = None  # type: ignore[assignment]
    _bytes: int = 0

    def __post_init__(self):
        self._items = OrderedDict()

    def put(self, key, frame):  # type: ignore[no-untyped-def]
        size = int(frame.memory_usage(index=True, deep=True).sum())
        while self._items and (
            len(self._items) >= self.max_entries or self._bytes + size > self.max_bytes
        ):
            _, old = self._items.popitem(last=False)
            self._bytes -= int(old.memory_usage(index=True, deep=True).sum())
        if size <= self.max_bytes:
            self._items[key] = frame
            self._bytes += size

    def get(self, key):  # type: ignore[no-untyped-def]
        item = self._items.pop(key)
        self._items[key] = item
        return item


def preflight_benchmark(rows: int = 100_000) -> dict[str, object]:
    """Bounded, feature-only preflight; it explicitly cannot fit predictive models."""
    if rows < 1 or rows > 100_000:
        raise ValueError("benchmark rows must be bounded")
    return {
        "identity": IDENTITY,
        "rows": rows,
        "models_fitted": False,
        "metrics_produced": False,
        "recommended_workers": {
            "light": 3,
            "permutation": 2,
            "bootstrap": 2,
            "heavy_refit": 1,
            "interaction": 1,
        },
        "research_ram_budget_bytes": RUNTIME_MEMORY_BUDGET_BYTES,
    }


def materialize_research_only(
    root: Path = FEATURE_STORE_ROOT,
    source_root: Path = BINANCE_V2_ROOT,
) -> dict[str, object]:
    """Publish a create-only, feature-only V2 matrix from the Research prefix.

    This intentionally reads exactly the Research minute count, never the first
    Validation candle.  A verified monthly partition is reused without rereading.
    """
    import numpy as np
    import pandas as pd
    import pyarrow as pa
    import pyarrow.parquet as pq

    started = time.monotonic()
    _source_manifest(source_root)
    root = Path(root)
    manifest_path = root / "manifest.json"
    if manifest_path.exists():
        # A final manifest is the immutable publication marker.  Never overwrite it.
        verify_manifest(manifest_path)
        raise FileExistsError("authoritative V2 manifest already exists (create-only)")
    expected = int((RESEARCH_END_EXCLUSIVE - RESEARCH_START) / timedelta(minutes=1))
    path = source_root / BTC_CANONICAL_RELATIVE_PATH
    frame = pd.read_csv(path, nrows=expected)
    if tuple(frame.columns) != BINANCE_BREADTH_V2_SCHEMA or len(frame) != expected:
        raise ValueError("incomplete BTC Research prefix")
    ts = pd.to_datetime(frame.open_time_ms, unit="ms", utc=True)
    if (
        not ts.is_monotonic_increasing
        or ts.duplicated().any()
        or ts.iloc[0] != pd.Timestamp(RESEARCH_START)
        or ts.iloc[-1] >= pd.Timestamp(RESEARCH_END_EXCLUSIVE)
    ):
        raise ValueError("BTC timestamp continuity/boundary violation")
    if not (ts.diff().dropna() == pd.Timedelta(minutes=1)).all():
        raise ValueError("BTC minute gap")
    numeric = frame[["open", "high", "low", "close", "volume"]].apply(
        pd.to_numeric, errors="coerce"
    )
    if (
        not np.isfinite(numeric.to_numpy()).all()
        or (numeric[["open", "high", "low", "close"]] <= 0).any().any()
        or (numeric.volume < 0).any()
    ):
        raise ValueError("BTC OHLCV finite/positive validation failed")
    minute = numeric.set_index(ts)
    minute.index.name = "timestamp_utc"
    new = build_new_features(minute)
    if tuple(new.columns) != FINAL_FEATURE_ORDER[15:]:
        raise RuntimeError("frozen V2 feature order drift")
    matrix = root / "experiment_matrix"
    partitions = []
    for period, piece in new.groupby(new.index.to_period("M")):
        stamp = period.strftime("%Y-%m")
        out = matrix / f"year={period.year}" / f"month={period.month:02d}"
        data = out / "data.parquet"
        checkpoint = out / "checkpoint.json"
        partition_id = f"year={period.year}/month={period.month:02d}"
        if checkpoint.exists() or data.exists():
            if not (checkpoint.exists() and data.exists()):
                raise ValueError(f"partial non-authoritative V2 partition {stamp}")
            check = json.loads(checkpoint.read_text(encoding="utf-8"))
            if check.get("status") != "VERIFIED" or check.get("partition") != partition_id:
                raise ValueError(f"corrupt V2 checkpoint {stamp}")
            evidence = verify_persisted_partition(data)
            if check.get("evidence") != evidence or check.get("binding") != {
                "dataset_identity": IDENTITY,
                "experiment_identity": EXPERIMENT_IDENTITY,
                "source_identity": BINANCE_BREADTH_AUTHORITY_V2_IDENTITY,
                "source_digest": BINANCE_V2_AUTHORITY_DIGEST,
                "feature_definition_digest": _feature_definition_digest(),
                "partition": partition_id,
            }:
                raise ValueError(f"corrupt verified V2 evidence {stamp}")
            partitions.append({"partition": partition_id, **evidence})
            continue
        out.mkdir(parents=True, exist_ok=True)
        cf_path = (
            CAPITAL_FLOW_ROOT
            / f"partitions/year={period.year}"
            / f"month={period.month:02d}"
            / "data.parquet"
        )
        import pyarrow.parquet as pq2

        cf = (
            pq2.read_table(cf_path)
            .to_pandas()
            .drop(columns=["year", "month"], errors="ignore")
            .set_index("timestamp_utc")
            .reindex(piece.index)
        )
        final = pd.DataFrame(index=piece.index)
        final.index.name = "timestamp_utc"
        for column in CAPITAL_FLOW_FEATURE_ORDER:
            final[column] = cf[column]
            final[f"{column}__state"] = cf[f"{column}__state"]
        for column in new.columns:
            final[column] = piece[column]
        for column in FINAL_FEATURE_ORDER:
            if column in CAPITAL_FLOW_FEATURE_ORDER:
                continue
            state = np.where(
                np.isfinite(final[column].to_numpy(dtype=float)),
                STATE_CODES[STATE_AVAILABLE],
                STATE_CODES[STATE_INSUFFICIENT_HISTORY],
            )
            final[f"{column}__state"] = state.astype("uint8")
        table = pa.Table.from_pandas(final.reset_index(), preserve_index=False)
        temporary = data.with_suffix(".partial")
        pq.write_table(
            table,
            temporary,
            compression=COMPRESSION,
            compression_level=9,
            row_group_size=ROW_GROUP_ROWS,
        )
        # Verification is deliberately against the file which will be promoted.
        evidence = verify_persisted_partition(temporary)
        os.replace(temporary, data)
        final_evidence = verify_persisted_partition(data, expected=evidence)
        check = {
            "status": "VERIFIED",
            "partition": partition_id,
            "binding": {
                "dataset_identity": IDENTITY,
                "experiment_identity": EXPERIMENT_IDENTITY,
                "source_identity": BINANCE_BREADTH_AUTHORITY_V2_IDENTITY,
                "source_digest": BINANCE_V2_AUTHORITY_DIGEST,
                "feature_definition_digest": _feature_definition_digest(),
                "partition": partition_id,
            },
            "evidence": final_evidence,
        }
        _atomic_json(checkpoint, check)
        partitions.append({"partition": partition_id, **final_evidence})
    partitions.sort(key=lambda item: str(item["partition"]))
    total = sum(int(item["row_count"]) for item in partitions)
    if (
        total != expected
        or total != 744_480
        or partitions[0]["first_timestamp"] != _timestamp_text(RESEARCH_START)
        or partitions[-1]["last_timestamp"] != "2025-05-31T23:59:00Z"
    ):
        raise ValueError("whole-dataset row/timestamp reconciliation failed")
    import pandas as pd

    for previous, current in pairwise(partitions):
        if pd.Timestamp(current["first_timestamp"]) - pd.Timestamp(
            previous["last_timestamp"]
        ) != pd.Timedelta(minutes=1):
            raise ValueError("whole-dataset partition continuity failed")
    all_violations = {
        key: sum(int(item["violations"][key]) for item in partitions)
        for key in partitions[0]["violations"]
    }  # type: ignore[index]
    feature_counts: dict[str, object] = {}
    for feature in FINAL_FEATURE_ORDER:
        feature_counts[feature] = {
            key: sum(
                int(item["feature_counts"][feature][key]) if key != "states" else 0
                for item in partitions
            )
            for key in ("null", "non_null", "finite_available")
        }  # type: ignore[index]
        feature_counts[feature]["states"] = {
            state: sum(int(item["feature_counts"][feature]["states"][state]) for item in partitions)
            for state in STATE_CODES
        }  # type: ignore[index]
        counts = feature_counts[feature]
        if (
            sum(counts["states"].values()) != total
            or counts["states"][STATE_AVAILABLE] != counts["finite_available"]
            or counts["non_null"] != counts["finite_available"]
        ):  # type: ignore[index]
            raise ValueError("whole-dataset feature state reconciliation failed")
    if any(all_violations.values()):
        raise ValueError("whole-dataset violations")
    quality = {
        "row_count": total,
        "first_timestamp": partitions[0]["first_timestamp"],
        "last_timestamp": partitions[-1]["last_timestamp"],
        "violations": all_violations,
        "feature_counts": feature_counts,
    }
    family_mapping = {feature: "CAPITAL_FLOW" for feature in CAPITAL_FLOW_FEATURE_ORDER}
    family_mapping.update({item.name: item.family for item in NEW_FEATURES})
    manifest = {
        "identity": IDENTITY,
        "experiment_identity": EXPERIMENT_IDENTITY,
        "source_authority_identity": BINANCE_BREADTH_AUTHORITY_V2_IDENTITY,
        "source_authority_digest": BINANCE_V2_AUTHORITY_DIGEST,
        "feature_order": list(FINAL_FEATURE_ORDER),
        "feature_families": list(FEATURE_FAMILIES),
        "feature_family_mapping": family_mapping,
        "causal_contract_identity": _causal_contract_identity(),
        "feature_definition_digest": _feature_definition_digest(),
        "timeline": {
            "start": RESEARCH_START.isoformat(),
            "end_exclusive": RESEARCH_END_EXCLUSIVE.isoformat(),
            "rows": expected,
        },
        "format": "parquet",
        "compression": COMPRESSION,
        "row_group_rows": ROW_GROUP_ROWS,
        "partition_scheme": "year=YYYY/month=MM",
        "availability_states": STATE_CODES,
        "partitions": partitions,
        "quality": quality,
        "dataset_semantic_digest": dataset_semantic_digest(
            source_identity=BINANCE_BREADTH_AUTHORITY_V2_IDENTITY,
            source_digest=BINANCE_V2_AUTHORITY_DIGEST,
            partitions=partitions,
            quality=quality,
        ),
        "verifier_publication_version": "authority-core-v1",
        "models_fitted": False,
        "metrics_produced": False,
        "validation_accessed": False,
        "blind_oos_accessed": False,
    }
    manifest["semantic_digest"] = manifest_semantic_digest(manifest)
    temporary_manifest = manifest_path.with_suffix(".json.partial")
    _atomic_json(temporary_manifest, manifest)
    verify_manifest(temporary_manifest)
    os.replace(temporary_manifest, manifest_path)
    verify_manifest(manifest_path)
    return {
        "semantic_digest": manifest["semantic_digest"],
        "rows": expected,
        "partitions": len(partitions),
        "wall_seconds": time.monotonic() - started,
        "rows_per_second": expected / max(time.monotonic() - started, 1e-9),
        "models_fitted": False,
        "metrics_produced": False,
    }
