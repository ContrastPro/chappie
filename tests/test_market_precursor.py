import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.market.models import Candle
from app.research.market_precursor import (
    CONTEXT_FEATURE_CONTRACT_IDENTITY,
    MARKET_PRECURSOR_DATASET_IDENTITY,
    MarketPrecursorBuilderV1,
    MarketPrecursorContextBuilderV1,
    iter_materialized_rows,
    validate_context_authority,
    validate_materialized_context_dataset,
    validate_materialized_dataset,
)
from app.research.market_precursor.context import _features
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


def _context_dataset(symbol, start, count):
    candles = tuple(
        Candle(
            open_time_ms=(start + index) * 60000,
            close_time_ms=(start + index) * 60000 + 59999,
            open=Decimal(100 + index),
            high=Decimal(102 + index),
            low=Decimal(99 + index),
            close=Decimal(100 + index),
            volume=Decimal(10 + index % 11),
        )
        for index in range(count)
    )
    return HistoricalDataset(
        HistoricalDatasetMetadata(
            symbol,
            "1m",
            start * 60000,
            (start + count) * 60000,
            candles[0].open_time_ms,
            candles[-1].open_time_ms,
            count,
            "fixture",
            "https://data-api.binance.vision",
            candles_sha256(candles),
        ),
        candles,
    )


def _write_context_authority(tmp_path, btc_a, btc_b, eth, sol):
    def provenance(dataset, segments):
        semantic = {
            "identity": "chappie_segmented_historical_acquisition/1.0.0",
            "symbol": dataset.metadata.symbol,
            "interval": "1m",
            "requested_start_ms": dataset.metadata.requested_start_ms,
            "requested_end_ms": dataset.metadata.requested_end_ms,
            "segment_count": 2,
            "segments": segments,
            "merged_candles_sha256": dataset.metadata.candles_sha256,
        }
        semantic["provenance_digest"] = hashlib.sha256(
            json.dumps(semantic, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        path = tmp_path / f"{dataset.metadata.symbol}.provenance.json"
        path.write_text(json.dumps(semantic, sort_keys=True), encoding="utf-8")
        return path, semantic["provenance_digest"]

    segments = [
        {"start_ms": 0, "end_ms": 40 * 60000, "candle_count": 40},
        {"start_ms": 40 * 60000, "end_ms": 80 * 60000, "candle_count": 40},
    ]
    eth_path, eth_digest = provenance(eth, segments)
    sol_path, sol_digest = provenance(sol, segments)
    semantic = {
        "identity": "chappie_multi_market_context_dataset/1.0.0",
        "target": {
            "symbol": "BTCUSDT",
            "interval": "1m",
            "requested_start_ms": btc_a.metadata.requested_start_ms,
            "requested_end_ms": btc_b.metadata.requested_end_ms,
            "datasets": [
                {
                    "candles_sha256": btc_a.metadata.candles_sha256,
                    "candle_count": len(btc_a.candles),
                },
                {
                    "candles_sha256": btc_b.metadata.candles_sha256,
                    "candle_count": len(btc_b.candles),
                },
            ],
        },
        "context_datasets": [
            {
                "symbol": "ETHUSDT",
                "interval": "1m",
                "candles_sha256": eth.metadata.candles_sha256,
                "candle_count": len(eth.candles),
                "segmented_provenance_path": str(eth_path),
                "segmented_provenance_digest": eth_digest,
            },
            {
                "symbol": "SOLUSDT",
                "interval": "1m",
                "candles_sha256": sol.metadata.candles_sha256,
                "candle_count": len(sol.candles),
                "segmented_provenance_path": str(sol_path),
                "segmented_provenance_digest": sol_digest,
            },
        ],
        "alignment": {
            "btc_eth_common_timestamps": 80,
            "btc_eth_missing_timestamps": 0,
            "btc_eth_extra_timestamps": 0,
            "btc_sol_common_timestamps": 80,
            "btc_sol_missing_timestamps": 0,
            "btc_sol_extra_timestamps": 0,
            "eth_segment_a_aligned_timestamps": 40,
            "eth_segment_b_aligned_timestamps": 40,
            "sol_segment_a_aligned_timestamps": 40,
            "sol_segment_b_aligned_timestamps": 40,
        },
        "source_validation": {
            "provider": "https://data-api.binance.vision",
            "interval_ms": 60000,
            "ordered": True,
            "zero_gaps": True,
            "zero_duplicates": True,
            "reload_digest_verified": True,
        },
        "context_ready": True,
    }
    semantic["manifest_digest"] = hashlib.sha256(
        json.dumps(semantic, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    path = tmp_path / "authority.json"
    path.write_text(json.dumps(semantic, sort_keys=True), encoding="utf-8")
    return path, semantic["manifest_digest"]


def _context_sources(tmp_path):
    btc_a = _context_dataset("BTCUSDT", 0, 40)
    btc_b = _context_dataset("BTCUSDT", 40, 40)
    eth = _context_dataset("ETHUSDT", 0, 80)
    sol = _context_dataset("SOLUSDT", 0, 80)
    return (btc_a, btc_b, eth, sol, *_write_context_authority(tmp_path, btc_a, btc_b, eth, sol))


def test_context_sidecar_is_causal_and_streaming(monkeypatch, tmp_path):
    monkeypatch.setattr("app.research.market_precursor.context.EXPECTED_COMMON_TIMESTAMPS", 80)
    btc_a, btc_b, eth, sol, authority_path, authority_digest = _context_sources(tmp_path)
    builder = MarketPrecursorContextBuilderV1(authority_digest)
    rows_path = tmp_path / "context.ndjson"
    manifest = builder.materialize(authority_path, btc_a, btc_b, eth, sol, rows_path)
    assert manifest.row_count == 20
    assert manifest.drop_counts == {"INSUFFICIENT_CONTEXT_FEATURE_HISTORY": 60}
    validate_materialized_context_dataset(rows_path, expected_authority_digest=authority_digest)
    row = json.loads(rows_path.read_text(encoding="utf-8").splitlines()[0])
    assert row["feature_contract_identity"] == CONTEXT_FEATURE_CONTRACT_IDENTITY
    assert len(row["features"]) == 19


def test_context_authority_and_timestamp_binding_fail_closed(monkeypatch, tmp_path):
    monkeypatch.setattr("app.research.market_precursor.context.EXPECTED_COMMON_TIMESTAMPS", 80)
    btc_a, btc_b, eth, sol, authority_path, authority_digest = _context_sources(tmp_path)
    validate_context_authority(authority_path, authority_digest, btc_a, btc_b, eth, sol)
    with pytest.raises(ValueError):
        validate_context_authority(authority_path, "0" * 64, btc_a, btc_b, eth, sol)
    wrong_sol = _context_dataset("SOLUSDT", 1, 80)
    with pytest.raises(ValueError):
        validate_context_authority(authority_path, authority_digest, btc_a, btc_b, eth, wrong_sol)
    authority = json.loads(authority_path.read_text(encoding="utf-8"))
    provenance = Path(authority["context_datasets"][0]["segmented_provenance_path"])
    payload = json.loads(provenance.read_text(encoding="utf-8"))
    payload["provenance_digest"] = "0" * 64
    provenance.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    with pytest.raises(ValueError):
        validate_context_authority(authority_path, authority_digest, btc_a, btc_b, eth, sol)


def test_context_features_do_not_read_t_plus_one_but_read_past_context():
    btc = _context_dataset("BTCUSDT", 0, 62).candles
    eth = _context_dataset("ETHUSDT", 0, 62).candles
    sol = _context_dataset("SOLUSDT", 0, 62).candles
    baseline = _features(btc, eth, sol, 60)
    changed_future_eth = (*eth[:61], eth[61].model_copy(update={"close": Decimal(9999)}))
    assert _features(btc, changed_future_eth, sol, 60) == baseline
    changed_future_sol = (*sol[:61], sol[61].model_copy(update={"close": Decimal(9999)}))
    changed_future_btc = (*btc[:61], btc[61].model_copy(update={"close": Decimal(9999)}))
    assert _features(btc, eth, changed_future_sol, 60) == baseline
    assert _features(changed_future_btc, eth, sol, 60) == baseline
    changed_past_eth = (*eth[:59], eth[59].model_copy(update={"close": Decimal(1)}), *eth[60:])
    assert _features(btc, changed_past_eth, sol, 60) != baseline
    zero_volume_eth = tuple(item.model_copy(update={"volume": Decimal(0)}) for item in eth)
    with pytest.raises(ValueError, match="volume baseline"):
        _features(btc, zero_volume_eth, sol, 60)


def test_context_streaming_validator_rejects_corrupted_row(monkeypatch, tmp_path):
    monkeypatch.setattr("app.research.market_precursor.context.EXPECTED_COMMON_TIMESTAMPS", 80)
    btc_a, btc_b, eth, sol, authority_path, authority_digest = _context_sources(tmp_path)
    rows_path = tmp_path / "context.ndjson"
    MarketPrecursorContextBuilderV1(authority_digest).materialize(
        authority_path, btc_a, btc_b, eth, sol, rows_path
    )
    lines = rows_path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[0])
    row["features"]["eth.return_5m"] = "0"
    lines[0] = json.dumps(row, sort_keys=True, separators=(",", ":"))
    rows_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        validate_materialized_context_dataset(rows_path, expected_authority_digest=authority_digest)
