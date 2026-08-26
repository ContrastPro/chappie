import json
from decimal import Decimal

import pytest

from app.market.models import Candle
from app.research.market_precursor import (
    MARKET_PRECURSOR_DATASET_IDENTITY,
    MarketPrecursorBuilderV1,
    iter_materialized_rows,
    validate_materialized_dataset,
)
from app.research.strategy.history import (
    HistoricalDataset,
    HistoricalDatasetMetadata,
    candles_sha256,
)


def _dataset(count=400):
    candles = tuple(
        Candle(
            open_time_ms=i * 60000,
            close_time_ms=i * 60000 + 59999,
            open=Decimal(100 + i),
            high=Decimal(101 + i),
            low=Decimal(99 + i),
            close=Decimal(100 + i),
            volume=Decimal(10 + i % 7),
        )
        for i in range(count)
    )
    return HistoricalDataset(
        HistoricalDatasetMetadata(
            "BTCUSDT",
            "1m",
            0,
            count * 60000,
            0,
            candles[-1].open_time_ms,
            count,
            "fixture",
            "test",
            candles_sha256(candles),
        ),
        candles,
    )


def test_deterministic_causal_rows_and_labels():
    data = _dataset()
    builder = MarketPrecursorBuilderV1()
    first = builder.build(data)
    second = builder.build(data)
    assert first.dataset_digest == second.dataset_digest and first.rows == second.rows
    assert first.payload()["identity"] == MARKET_PRECURSOR_DATASET_IDENTITY
    assert len(first.rows) == 400 - first.feature_config.warmup - 240 + 1
    row = first.rows[0]
    source = data.candles[first.feature_config.warmup - 1]
    assert row.close_time_ms == source.close_time_ms
    assert (
        row.labels["future_return_5m"]
        == data.candles[first.feature_config.warmup + 4].close / source.close - 1
    )
    assert (
        row.labels["future_return_15m"]
        == data.candles[first.feature_config.warmup + 14].close / source.close - 1
    )
    assert (
        row.labels["future_return_60m"]
        == data.candles[first.feature_config.warmup + 59].close / source.close - 1
    )
    assert (
        row.labels["future_return_240m"]
        == data.candles[first.feature_config.warmup + 239].close / source.close - 1
    )
    assert (
        row.labels["upside_excursion_5m"]
        == max(
            c.high
            for c in data.candles[first.feature_config.warmup : first.feature_config.warmup + 5]
        )
        / source.close
        - 1
    )
    assert not first.context_sources


def test_streaming_materialization_is_deterministic_and_atomic(tmp_path):
    builder = MarketPrecursorBuilderV1()
    rows_path = tmp_path / "rows.ndjson"
    manifest = builder.materialize(_dataset(), rows_path)
    assert manifest.row_count == 41
    assert rows_path.exists()
    assert rows_path.with_suffix(".ndjson.manifest.json").exists()
    assert sum(1 for _ in rows_path.open(encoding="utf-8")) == manifest.row_count
    assert [row.row_digest for row in iter_materialized_rows(rows_path)] == [
        row.row_digest for row in builder.build(_dataset()).rows
    ]
    assert not rows_path.with_name(".rows.ndjson.partial").exists()
    validate_materialized_dataset(
        rows_path, expected_source_digest=_dataset().metadata.candles_sha256
    )


def test_tail_warmup_and_source_rejections():
    built = MarketPrecursorBuilderV1().build(_dataset())
    assert built.drop_counts == {
        "INSUFFICIENT_FEATURE_HISTORY": 119,
        "INSUFFICIENT_FUTURE_LABEL_HISTORY": 240,
    }
    data = _dataset()
    bad = HistoricalDataset(data.metadata, data.candles[:2] + (data.candles[1],) + data.candles[3:])
    with pytest.raises(ValueError):
        MarketPrecursorBuilderV1().build(bad)
    wrong_digest = HistoricalDataset(
        HistoricalDatasetMetadata(
            "BTCUSDT", "1m", 0, 1, 0, 0, len(data.candles), "fixture", "test", "wrong"
        ),
        data.candles,
    )
    with pytest.raises(ValueError):
        MarketPrecursorBuilderV1().build(wrong_digest)
    eth = HistoricalDataset(
        HistoricalDatasetMetadata("ETHUSDT", "1m", 0, 1, 0, 0, 1, "fixture", "test", "x"),
        data.candles,
    )
    with pytest.raises(ValueError):
        MarketPrecursorBuilderV1().build(eth)


def _materialized(tmp_path):
    rows = tmp_path / "rows.ndjson"
    source = _dataset()
    MarketPrecursorBuilderV1().materialize(source, rows)
    return rows, rows.with_suffix(".ndjson.manifest.json"), source.metadata.candles_sha256


@pytest.mark.parametrize(
    "mutation",
    [
        "malformed",
        "semantic",
        "digest",
        "missing",
        "remove",
        "partial",
        "empty",
        "extra",
        "reorder",
    ],
)
def test_streaming_validator_rejects_row_and_sequence_corruption(tmp_path, mutation):
    rows, _manifest, _digest = _materialized(tmp_path)
    lines = rows.read_text(encoding="utf-8").splitlines()
    if mutation == "malformed":
        lines[0] = "{"
    elif mutation == "semantic":
        value = json.loads(lines[0])
        value["features"]["fast_ema"] = "0"
        lines[0] = json.dumps(value, sort_keys=True, separators=(",", ":"))
    elif mutation == "digest":
        value = json.loads(lines[0])
        value["row_digest"] = "0" * 64
        lines[0] = json.dumps(value, sort_keys=True, separators=(",", ":"))
    elif mutation == "missing":
        value = json.loads(lines[0])
        del value["target_symbol"]
        lines[0] = json.dumps(value, sort_keys=True, separators=(",", ":"))
    elif mutation == "remove":
        lines.pop()
    elif mutation == "partial":
        lines[-1] = lines[-1][:-8]
    elif mutation == "empty":
        lines = []
    elif mutation == "extra":
        lines.append(lines[0])
    else:
        lines[0], lines[1] = lines[1], lines[0]
    rows.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    with pytest.raises(ValueError):
        validate_materialized_dataset(rows)


@pytest.mark.parametrize(
    "field,value",
    [
        ("row_count", 0),
        ("row_sequence_digest", "0" * 64),
        ("source_dataset_digest", "x"),
        ("target_symbol", "ETHUSDT"),
        ("target_interval", "5m"),
        ("feature_contract_identity", "x"),
        ("feature_contract_digest", "x"),
        ("label_contract_identity", "x"),
        ("label_contract_digest", "x"),
        ("warmup", 0),
        ("horizons", []),
        ("context_sources", ["ETHUSDT"]),
        ("dataset_digest", "0" * 64),
    ],
)
def test_streaming_validator_rejects_manifest_tampering(tmp_path, field, value):
    rows, manifest, _digest = _materialized(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload[field] = value
    manifest.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    with pytest.raises(ValueError):
        validate_materialized_dataset(rows)


def test_streaming_validator_rejects_drop_count_and_expected_source_tampering(tmp_path):
    rows, manifest, source_digest = _materialized(tmp_path)
    validate_materialized_dataset(rows, expected_source_digest=source_digest)
    with pytest.raises(ValueError):
        validate_materialized_dataset(rows, expected_source_digest="wrong")
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["drop_counts"]["INSUFFICIENT_FUTURE_LABEL_HISTORY"] = 0
    manifest.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    with pytest.raises(ValueError):
        validate_materialized_dataset(rows, expected_source_digest=source_digest)


def test_streaming_validator_rejects_same_count_valid_row_substitution(tmp_path):
    rows, _manifest, _digest = _materialized(tmp_path)
    lines = rows.read_text(encoding="utf-8").splitlines()
    assert len(lines) > 2
    lines[1] = lines[0]
    rows.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        validate_materialized_dataset(rows)
