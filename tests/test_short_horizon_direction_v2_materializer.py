from datetime import UTC, datetime

import numpy as np
import pandas as pd

from app.research.capital_flow.short_horizon_direction_v2_materializer import (
    FINAL_FEATURE_ORDER,
    FOLD_CACHE_MAX_ENTRIES,
    STATE_AVAILABLE,
    STATE_CODES,
    BoundedFoldCache,
    _resample,
    build_new_features,
    confirmed_swings,
    dataset_semantic_digest,
    manifest_semantic_digest,
    partition_quality_evidence,
    partition_semantic_digest,
    preflight_benchmark,
    verify_persisted_partition,
)


def _minute_frame(rows: int = 12_000) -> pd.DataFrame:
    index = pd.date_range(datetime(2024, 1, 1, tzinfo=UTC), periods=rows, freq="min")
    close = pd.Series(np.linspace(100.0, 200.0, rows), index=index)
    result = pd.DataFrame(
        {
            "open": close - 0.1,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": 10.0,
        },
        index=index,
    )
    result.index.name = "timestamp_utc"
    return result


def test_completed_candle_aggregation_and_feature_order_are_deterministic() -> None:
    minute = _minute_frame()
    candle = _resample(minute, "1h")
    assert candle.loc[datetime(2024, 1, 1, 1, tzinfo=UTC), "close"] == minute.iloc[59].close
    features = build_new_features(minute)
    assert tuple(features) == FINAL_FEATURE_ORDER[15:]
    assert len(features.columns) == 45
    assert features.index.max() < datetime(2024, 1, 10, tzinfo=UTC)


def test_unavailable_is_nan_not_a_fake_zero_and_cache_is_bounded() -> None:
    features = build_new_features(_minute_frame())
    assert features.iloc[0].isna().any()
    assert STATE_CODES[STATE_AVAILABLE] == 0
    cache = BoundedFoldCache(max_entries=1, max_bytes=10_000)
    cache.put("one", pd.DataFrame({"x": [1.0]}))
    cache.put("two", pd.DataFrame({"x": [2.0]}))
    assert len(cache._items) <= 1 <= FOLD_CACHE_MAX_ENTRIES


def test_benchmark_is_bounded_and_cannot_fit_models() -> None:
    payload = preflight_benchmark(500)
    assert payload["models_fitted"] is False and payload["metrics_produced"] is False


def test_confirmed_swings_are_delayed_and_equal_highs_choose_the_earliest() -> None:
    index = pd.date_range(datetime(2024, 1, 1, tzinfo=UTC), periods=8, freq="4h")
    high = pd.Series([1, 2, 5, 5, 3, 2, 1, 1], index=index)
    low = pd.Series([0, 0, 0, 1, 1, 1, 1, 1], index=index)
    pivot_high, _ = confirmed_swings(high, low)
    assert pd.isna(pivot_high.iloc[3])
    assert pivot_high.iloc[4] == 5


def test_four_timeframe_volatility_consensus_is_bounded() -> None:
    available = build_new_features(_minute_frame())["mtf.volatility_alignment"].dropna()
    assert available.between(0, 1).all()


def _logical_table() -> object:
    import pyarrow as pa

    timestamps = pa.array(
        [datetime(2024, 1, 1, 0, 1, tzinfo=UTC), datetime(2024, 1, 1, tzinfo=UTC)]
    )
    data: dict[str, object] = {"timestamp_utc": timestamps}
    for feature in FINAL_FEATURE_ORDER:
        data[feature] = pa.array([1.25, None], type=pa.float64())
        data[f"{feature}__state"] = pa.array(
            [STATE_CODES[STATE_AVAILABLE], STATE_CODES["INSUFFICIENT_HISTORY"]], type=pa.uint8()
        )
    return pa.table(data)


def test_partition_semantic_digest_is_order_and_path_independent_and_detects_tampering(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    import pyarrow.parquet as pq

    table = _logical_table()
    digest = partition_semantic_digest(table)
    assert digest == partition_semantic_digest(table.select(table.column_names))
    first, second = tmp_path / "a.parquet", tmp_path / "nested" / "b.parquet"
    second.parent.mkdir()
    pq.write_table(table, first, compression="zstd")
    pq.write_table(table, second, compression="snappy")
    assert (
        verify_persisted_partition(first)["semantic_sha256"]
        == verify_persisted_partition(second)["semantic_sha256"]
        == digest
    )
    assert (
        verify_persisted_partition(first)["physical_sha256"]
        != verify_persisted_partition(second)["physical_sha256"]
    )
    changed = table.set_column(1, FINAL_FEATURE_ORDER[0], __import__("pyarrow").array([9.0, None]))
    assert partition_semantic_digest(changed) != digest
    changed_state = table.set_column(
        2,
        f"{FINAL_FEATURE_ORDER[0]}__state",
        __import__("pyarrow").array([1, 1], type=__import__("pyarrow").uint8()),
    )
    assert partition_semantic_digest(changed_state) != digest
    changed_time = table.set_column(
        0,
        "timestamp_utc",
        __import__("pyarrow").array(
            [datetime(2024, 1, 1, 0, 2, tzinfo=UTC), datetime(2024, 1, 1, tzinfo=UTC)]
        ),
    )
    assert partition_semantic_digest(changed_time) != digest


def test_quality_evidence_fails_closed_for_state_and_timeline_violations() -> None:
    table = _logical_table()
    bad = table.set_column(1, FINAL_FEATURE_ORDER[0], __import__("pyarrow").array([None, None]))
    assert partition_quality_evidence(bad)["violations"]["available_null"] == 1
    bad = table.set_column(
        2,
        f"{FINAL_FEATURE_ORDER[0]}__state",
        __import__("pyarrow").array([99, 1], type=__import__("pyarrow").uint8()),
    )
    assert partition_quality_evidence(bad)["violations"]["unknown_state"] == 1
    bad = table.set_column(
        0, "timestamp_utc", __import__("pyarrow").array([datetime(2024, 1, 1, tzinfo=UTC)] * 2)
    )
    evidence = partition_quality_evidence(bad)
    assert evidence["violations"]["duplicate_timestamp"] == 1
    assert evidence["violations"]["ordering_timeline"] == 1


def test_dataset_and_manifest_digests_are_canonical() -> None:
    partition = {"partition": "year=2024/month=01", "semantic_sha256": "a", "row_count": 2}
    quality = {"row_count": 2}
    assert dataset_semantic_digest(
        source_identity="source", source_digest="digest", partitions=[partition], quality=quality
    ) == dataset_semantic_digest(
        source_identity="source",
        source_digest="digest",
        partitions=[dict(partition)],
        quality=dict(quality),
    )
    manifest = {"identity": "x", "nested": {"b": 2, "a": 1}, "root": "E:/one"}
    assert manifest_semantic_digest(manifest) == manifest_semantic_digest(
        {"nested": {"a": 1, "b": 2}, "identity": "x", "root": "D:/other"}
    )
    assert manifest_semantic_digest(manifest) != manifest_semantic_digest(
        {"identity": "tampered", "nested": {"a": 1, "b": 2}}
    )
