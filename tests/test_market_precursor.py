import hashlib
import json
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

import pytest

from app.market.models import Candle
from app.research.market_precursor import (
    BOOLEAN_CONTEXT_FEATURES_V2,
    CONTEXT_FEATURE_CONTRACT_IDENTITY,
    CONTEXT_FEATURE_CONTRACT_IDENTITY_V2,
    MARKET_PRECURSOR_DATASET_IDENTITY,
    NUMERIC_CONTEXT_FEATURES_V2,
    REQUIRED_DISCOVERY_FEATURES_V1,
    REQUIRED_QUANTILE_FEATURES_V1,
    EqualityFeatureConditionV1,
    FrozenHorizonOutcomeV1,
    MarketPrecursorBuilderV1,
    MarketPrecursorContextBuilderV1,
    MarketPrecursorContextBuilderV2,
    MultiHorizonFrozenConditionObservationV1,
    iter_materialized_context_rows_v2,
    iter_materialized_rows,
    load_authoritative_observation_split,
    validate_context_authority,
    validate_materialized_context_dataset,
    validate_materialized_context_dataset_v2,
    validate_materialized_dataset,
)
from app.research.market_precursor.authority import _join_validated_rows
from app.research.market_precursor.context import _features
from app.research.market_precursor.context_v2 import MarketPrecursorContextRowV2
from app.research.market_precursor.models import MarketPrecursorRowV1
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

    split = len(btc_a.candles)
    total = split + len(btc_b.candles)
    segments = [
        {"start_ms": 0, "end_ms": split * 60000, "candle_count": split},
        {
            "start_ms": split * 60000,
            "end_ms": total * 60000,
            "candle_count": len(btc_b.candles),
        },
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
            "btc_eth_common_timestamps": total,
            "btc_eth_missing_timestamps": 0,
            "btc_eth_extra_timestamps": 0,
            "btc_sol_common_timestamps": total,
            "btc_sol_missing_timestamps": 0,
            "btc_sol_extra_timestamps": 0,
            "eth_segment_a_aligned_timestamps": split,
            "eth_segment_b_aligned_timestamps": len(btc_b.candles),
            "sol_segment_a_aligned_timestamps": split,
            "sol_segment_b_aligned_timestamps": len(btc_b.candles),
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


def _context_sources(tmp_path, count=80):
    split = count // 2
    btc_a = _context_dataset("BTCUSDT", 0, split)
    btc_b = _context_dataset("BTCUSDT", split, count - split)
    eth = _context_dataset("ETHUSDT", 0, count)
    sol = _context_dataset("SOLUSDT", 0, count)
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


def _materialized_context_v2(monkeypatch, tmp_path):
    monkeypatch.setattr("app.research.market_precursor.context.EXPECTED_COMMON_TIMESTAMPS", 80)
    btc_a, btc_b, eth, sol, authority_path, authority_digest = _context_sources(tmp_path)
    rows_path = tmp_path / "context.v2.ndjson"
    manifest = MarketPrecursorContextBuilderV2(authority_digest).materialize(
        authority_path, btc_a, btc_b, eth, sol, rows_path
    )
    return rows_path, manifest, authority_digest, (btc_a, btc_b, eth, sol, authority_path)


def test_typed_context_v2_round_trip_and_v1_numeric_equivalence(monkeypatch, tmp_path):
    rows_path, manifest, authority_digest, sources = _materialized_context_v2(monkeypatch, tmp_path)
    validated = validate_materialized_context_dataset_v2(
        rows_path, expected_authority_digest=authority_digest
    )
    rows = tuple(iter_materialized_context_rows_v2(rows_path))
    assert validated == manifest
    assert manifest.row_count == 20
    assert manifest.drop_counts == {"INSUFFICIENT_CONTEXT_FEATURE_HISTORY": 60}
    assert all(set(row.numeric_features) == NUMERIC_CONTEXT_FEATURES_V2 for row in rows)
    assert all(set(row.boolean_features) == BOOLEAN_CONTEXT_FEATURES_V2 for row in rows)
    assert all(type(value) is Decimal for row in rows for value in row.numeric_features.values())
    assert all(type(value) is bool for row in rows for value in row.boolean_features.values())

    payload = json.loads(rows_path.read_text(encoding="utf-8").splitlines()[0])
    assert all(type(value) is bool for value in payload["boolean_features"].values())
    assert all(type(value) is str for value in payload["numeric_features"].values())
    assert payload["feature_contract_identity"] == CONTEXT_FEATURE_CONTRACT_IDENTITY_V2

    btc_a, btc_b, eth, sol, authority_path = sources
    v1_rows = tuple(
        MarketPrecursorContextBuilderV1(authority_digest).iter_rows(
            authority_path, btc_a, btc_b, eth, sol
        )
    )
    assert [row.numeric_features for row in rows] == [
        {key: value for key, value in row.features.items() if key in NUMERIC_CONTEXT_FEATURES_V2}
        for row in v1_rows
    ]


def test_typed_context_v2_boolean_semantics_match_frozen_equality(monkeypatch, tmp_path):
    rows_path, _manifest, _authority_digest, _sources = _materialized_context_v2(
        monkeypatch, tmp_path
    )
    rows = tuple(iter_materialized_context_rows_v2(rows_path))
    condition = EqualityFeatureConditionV1("cross_market.all_three_direction_agreement", True)
    for row in rows:
        value = row.boolean_features["cross_market.all_three_direction_agreement"]
        assert condition.matches("cross_market.all_three_direction_agreement", value) is value


@pytest.mark.parametrize(
    "field,value",
    [
        ("boolean_features", {"cross_market.all_three_direction_agreement": 1}),
        ("numeric_features", {"eth.return_5m": True}),
        ("boolean_features", {"cross_market.all_three_direction_agreement": "true"}),
        ("numeric_features", {"eth.return_5m": "NaN"}),
    ],
)
def test_typed_context_v2_rejects_malformed_feature_types(monkeypatch, tmp_path, field, value):
    rows_path, _manifest, authority_digest, _sources = _materialized_context_v2(
        monkeypatch, tmp_path
    )
    lines = rows_path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[0])
    row[field].update(value)
    lines[0] = json.dumps(row, sort_keys=True, separators=(",", ":"))
    rows_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="typed context dataset validation failed"):
        validate_materialized_context_dataset_v2(
            rows_path, expected_authority_digest=authority_digest
        )


def test_typed_context_v2_is_deterministic_ordered_and_create_only(monkeypatch, tmp_path):
    rows_path, first, authority_digest, sources = _materialized_context_v2(monkeypatch, tmp_path)
    btc_a, btc_b, eth, sol, authority_path = sources
    second_path = tmp_path / "context-copy.v2.ndjson"
    second = MarketPrecursorContextBuilderV2(authority_digest).materialize(
        authority_path, btc_a, btc_b, eth, sol, second_path
    )
    assert first.dataset_digest == second.dataset_digest
    first_rows = tuple(iter_materialized_context_rows_v2(rows_path))
    second_rows = tuple(iter_materialized_context_rows_v2(second_path))
    assert first_rows == second_rows
    assert all(
        previous.close_time_ms < current.close_time_ms for previous, current in pairwise(first_rows)
    )
    with pytest.raises(FileExistsError, match="not clean"):
        MarketPrecursorContextBuilderV2(authority_digest).materialize(
            authority_path, btc_a, btc_b, eth, sol, rows_path
        )


def test_typed_context_v2_validator_rejects_order_and_manifest_tampering(monkeypatch, tmp_path):
    rows_path, _manifest, authority_digest, _sources = _materialized_context_v2(
        monkeypatch, tmp_path
    )
    lines = rows_path.read_text(encoding="utf-8").splitlines()
    lines[0], lines[1] = lines[1], lines[0]
    rows_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        validate_materialized_context_dataset_v2(
            rows_path, expected_authority_digest=authority_digest
        )

    manifest_case = tmp_path / "manifest-case"
    manifest_case.mkdir()
    rows_path, _manifest, authority_digest, _sources = _materialized_context_v2(
        monkeypatch, manifest_case
    )
    manifest_path = rows_path.with_suffix(rows_path.suffix + ".manifest.json")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["eth_source_digest"] = "0" * 64
    manifest_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    with pytest.raises(ValueError):
        validate_materialized_context_dataset_v2(
            rows_path, expected_authority_digest=authority_digest
        )


def _authority_base_row(timestamp, *, features=None, labels=None):
    base_features = {
        "breakout_proximity": Decimal("0.123456789"),
        "return_15m": Decimal("-0.000000001"),
        "volatility_compression_ratio": Decimal("0.987654321"),
    }
    if features:
        base_features.update(features)
    base_labels = {
        f"{name}_{horizon}m": Decimal(f"{sign}{horizon:03d}000001")
        for horizon in (15, 60, 240)
        for name, sign in (
            ("future_return", "0."),
            ("upside_excursion", "1."),
            ("downside_excursion", "-0."),
        )
    }
    if labels:
        base_labels.update(labels)
    return MarketPrecursorRowV1(
        timestamp,
        "BTCUSDT",
        "1m",
        "features/1",
        "source",
        base_features,
        base_labels,
        "row",
    )


def _authority_context_row(timestamp, *, agreement=True, numeric=None, boolean=None):
    context_numeric = {
        name: Decimal(f"{index}.0000000001")
        for index, name in enumerate(
            sorted(REQUIRED_QUANTILE_FEATURES_V1 - _authority_base_row(0).features.keys()),
            start=1,
        )
    }
    if numeric:
        context_numeric.update(numeric)
    context_boolean = {"cross_market.all_three_direction_agreement": agreement}
    if boolean:
        context_boolean.update(boolean)
    return MarketPrecursorContextRowV2(
        timestamp,
        "BTCUSDT",
        "1m",
        "authority/1",
        "authority-digest",
        ("btc-a", "btc-b"),
        "eth",
        "sol",
        "context/2",
        context_numeric,
        context_boolean,
        "row",
    )


def _joined(base_timestamps, context_timestamps, *, start_ms=0, end_ms=10_000):
    return _join_validated_rows(
        tuple(_authority_base_row(value) for value in base_timestamps),
        tuple(_authority_context_row(value) for value in context_timestamps),
        start_ms=start_ms,
        end_ms=end_ms,
    )


def test_authority_adapter_exact_range_leading_and_trailing_semantics():
    observations, base_leading, context_leading, base_trailing, context_trailing = _joined(
        (1_000, 2_000, 3_000, 4_000), (2_000, 3_000, 4_000, 5_000)
    )
    assert [item.timestamp_ms for item in observations] == [2_000, 3_000, 4_000]
    assert (base_leading, context_leading, base_trailing, context_trailing) == (1, 0, 0, 1)

    observations, base_leading, context_leading, base_trailing, context_trailing = _joined(
        (2_000, 3_000, 4_000, 5_000),
        (1_000, 2_000, 3_000, 4_000),
        start_ms=2_000,
        end_ms=5_000,
    )
    assert [item.timestamp_ms for item in observations] == [2_000, 3_000, 4_000]
    assert (base_leading, context_leading, base_trailing, context_trailing) == (0, 0, 0, 0)

    observations, base_leading, context_leading, base_trailing, context_trailing = _joined(
        (2_000, 3_000, 4_000, 5_000), (1_000, 2_000, 3_000, 4_000)
    )
    assert [item.timestamp_ms for item in observations] == [2_000, 3_000, 4_000]
    assert (base_leading, context_leading, base_trailing, context_trailing) == (0, 1, 1, 0)


@pytest.mark.parametrize(
    "base_timestamps,context_timestamps",
    [
        ((1_000, 2_000, 4_000), (1_000, 2_000, 3_000, 4_000)),
        ((1_000, 2_000, 3_000, 4_000), (1_000, 2_000, 4_000)),
        ((1_000, 1_000), (1_000, 2_000)),
        ((1_000, 2_000), (1_000, 1_000)),
        ((2_000, 1_000), (1_000, 2_000)),
    ],
)
def test_authority_adapter_rejects_internal_holes_duplicates_and_decreasing_order(
    base_timestamps, context_timestamps
):
    with pytest.raises(ValueError):
        _joined(base_timestamps, context_timestamps)


@pytest.mark.parametrize("agreement", [True, False])
def test_authority_adapter_preserves_exact_features_boolean_and_decimals(agreement):
    base = _authority_base_row(1_000)
    context = _authority_context_row(1_000, agreement=agreement)
    observations, *_counts = _join_validated_rows((base,), (context,), start_ms=0, end_ms=2_000)
    observation = observations[0]
    assert isinstance(observation, MultiHorizonFrozenConditionObservationV1)
    assert set(observation.features) == REQUIRED_DISCOVERY_FEATURES_V1
    assert type(observation.features["cross_market.all_three_direction_agreement"]) is bool
    assert observation.features["cross_market.all_three_direction_agreement"] is agreement
    assert observation.features["breakout_proximity"] is base.features["breakout_proximity"]
    assert all(
        type(observation.features[name]) is Decimal for name in REQUIRED_QUANTILE_FEATURES_V1
    )
    with pytest.raises(TypeError):
        observation.features["breakout_proximity"] = Decimal(0)


def test_authority_adapter_maps_each_stored_horizon_without_reuse():
    base = _authority_base_row(1_000)
    observations, *_counts = _join_validated_rows(
        (base,), (_authority_context_row(1_000),), start_ms=0, end_ms=2_000
    )
    outcomes = observations[0].outcomes
    assert tuple(item.horizon_minutes for item in outcomes) == (15, 60, 240)
    for outcome in outcomes:
        horizon = outcome.horizon_minutes
        assert outcome == FrozenHorizonOutcomeV1(
            horizon,
            base.labels[f"future_return_{horizon}m"],
            base.labels[f"upside_excursion_{horizon}m"],
            base.labels[f"downside_excursion_{horizon}m"],
        )


@pytest.mark.parametrize(
    "base,context,match",
    [
        (
            _authority_base_row(1_000),
            _authority_context_row(1_000, agreement=Decimal(1)),
            "exact Boolean",
        ),
        (
            _authority_base_row(1_000, features={"breakout_proximity": None}),
            _authority_context_row(1_000),
            "Decimal value",
        ),
        (
            _authority_base_row(1_000, features={"return_15m": Decimal("NaN")}),
            _authority_context_row(1_000),
            "Decimal value",
        ),
        (
            _authority_base_row(1_000),
            _authority_context_row(1_000, numeric={"breakout_proximity": Decimal(1)}),
            "collide",
        ),
    ],
)
def test_authority_adapter_rejects_invalid_features(base, context, match):
    with pytest.raises(ValueError, match=match):
        _join_validated_rows((base,), (context,), start_ms=0, end_ms=2_000)


def test_authority_adapter_rejects_missing_feature_and_label():
    context = _authority_context_row(1_000)
    context.numeric_features.pop("eth.atr_14_bps")
    with pytest.raises(ValueError, match="features are missing"):
        _join_validated_rows((_authority_base_row(1_000),), (context,), start_ms=0, end_ms=2_000)

    base = _authority_base_row(1_000)
    del base.labels["future_return_60m"]
    with pytest.raises(ValueError, match="future_return_60m"):
        _join_validated_rows((base,), (_authority_context_row(1_000),), start_ms=0, end_ms=2_000)


def _materialized_adapter_authorities(monkeypatch, tmp_path):
    monkeypatch.setattr("app.research.market_precursor.context.EXPECTED_COMMON_TIMESTAMPS", 500)
    base_path = tmp_path / "base.ndjson"
    base_manifest = MarketPrecursorBuilderV1().materialize(_dataset(500), base_path)
    btc_a, btc_b, eth, sol, authority_path, authority_digest = _context_sources(tmp_path, 500)
    context_path = tmp_path / "context.v2.ndjson"
    context_manifest = MarketPrecursorContextBuilderV2(authority_digest).materialize(
        authority_path, btc_a, btc_b, eth, sol, context_path
    )
    return base_path, base_manifest, context_path, context_manifest, authority_digest


def test_authority_adapter_validates_authorities_and_returns_deterministic_evidence(
    monkeypatch, tmp_path
):
    base_path, base_manifest, context_path, context_manifest, authority_digest = (
        _materialized_adapter_authorities(monkeypatch, tmp_path)
    )
    arguments = {
        "start_utc": "1970-01-01T00:00:00Z",
        "end_utc": "1970-01-02T00:00:00Z",
        "expected_base_digest": base_manifest.dataset_digest,
        "expected_context_digest": context_manifest.dataset_digest,
        "expected_context_authority_digest": authority_digest,
    }
    first = load_authoritative_observation_split(base_path, context_path, **arguments)
    second = load_authoritative_observation_split(base_path, context_path, **arguments)
    assert first == second
    assert len(first.observations) == 141
    assert first.evidence.joined_count == 141
    assert first.evidence.context_leading_rows_skipped == 59
    assert first.evidence.context_trailing_rows_skipped == 240
    assert first.evidence.internal_mismatch_count == 0
    assert first.evidence.required_feature_count == 12


@pytest.mark.parametrize(
    "name",
    ["expected_base_digest", "expected_context_digest", "expected_context_authority_digest"],
)
def test_authority_adapter_rejects_wrong_expected_authority(monkeypatch, tmp_path, name):
    base_path, base_manifest, context_path, context_manifest, authority_digest = (
        _materialized_adapter_authorities(monkeypatch, tmp_path)
    )
    arguments = {
        "start_utc": "1970-01-01T00:00:00Z",
        "end_utc": "1970-01-02T00:00:00Z",
        "expected_base_digest": base_manifest.dataset_digest,
        "expected_context_digest": context_manifest.dataset_digest,
        "expected_context_authority_digest": authority_digest,
    }
    arguments[name] = "0" * 64
    with pytest.raises(ValueError):
        load_authoritative_observation_split(base_path, context_path, **arguments)


def test_authority_adapter_rejects_invalid_temporal_bounds(monkeypatch, tmp_path):
    base_path, base_manifest, context_path, context_manifest, authority_digest = (
        _materialized_adapter_authorities(monkeypatch, tmp_path)
    )
    with pytest.raises(ValueError, match="start < end"):
        load_authoritative_observation_split(
            base_path,
            context_path,
            start_utc="1970-01-02T00:00:00Z",
            end_utc="1970-01-02T00:00:00Z",
            expected_base_digest=base_manifest.dataset_digest,
            expected_context_digest=context_manifest.dataset_digest,
            expected_context_authority_digest=authority_digest,
        )
