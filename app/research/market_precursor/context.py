"""Fail-closed causal BTC/ETH/SOL Market Precursor context sidecars."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

from app.ai.schemas import digest
from app.market.models import Candle
from app.research.strategy.history import HistoricalDataset, candles_sha256, validate_candles
from app.strategies.indicators import average_true_range

from .models import (
    CONTEXT_FEATURE_CONTRACT_IDENTITY,
    CONTEXT_FEATURE_DATASET_IDENTITY,
    MarketPrecursorContextFeatureConfigV1,
    MarketPrecursorContextManifestV1,
    MarketPrecursorContextRowV1,
)

AUTHORITY_IDENTITY = "chappie_multi_market_context_dataset/1.0.0"
EXPECTED_COMMON_TIMESTAMPS = 172_800
_FEATURE_KEYS = frozenset(
    {
        "eth.return_5m",
        "eth.return_15m",
        "eth.return_60m",
        "eth.volume_ratio_60m",
        "eth.atr_14_bps",
        "eth.acceleration_5m_vs_15m",
        "sol.return_5m",
        "sol.return_15m",
        "sol.return_60m",
        "sol.volume_ratio_60m",
        "sol.atr_14_bps",
        "sol.acceleration_5m_vs_15m",
        "cross_market.eth_minus_btc_return_5m",
        "cross_market.eth_minus_btc_return_15m",
        "cross_market.sol_minus_btc_return_5m",
        "cross_market.sol_minus_btc_return_15m",
        "cross_market.btc_eth_direction_agreement",
        "cross_market.btc_sol_direction_agreement",
        "cross_market.all_three_direction_agreement",
    }
)


def _canonical_digest(payload: dict[str, object], digest_key: str) -> str:
    semantic = dict(payload)
    semantic.pop(digest_key, None)
    return hashlib.sha256(
        json.dumps(semantic, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _source(dataset: HistoricalDataset, symbol: str) -> tuple[Candle, ...]:
    metadata = dataset.metadata
    candles = validate_candles(dataset.candles, metadata.interval)
    if (
        metadata.symbol != symbol
        or metadata.interval != "1m"
        or len(candles) != metadata.candle_count
        or candles_sha256(candles) != metadata.candles_sha256
    ):
        raise ValueError("context source binding is invalid")
    return candles


def validate_context_authority(
    authority_path: Path,
    expected_authority_digest: str,
    btc_a: HistoricalDataset,
    btc_b: HistoricalDataset,
    eth: HistoricalDataset,
    sol: HistoricalDataset,
) -> dict[str, object]:
    """Load and strictly bind the externally authorized aligned dataset manifest."""
    try:
        raw = json.loads(Path(authority_path).read_text(encoding="utf-8"))
        observed = raw["manifest_digest"]
        if (
            not isinstance(expected_authority_digest, str)
            or len(expected_authority_digest) != 64
            or observed != expected_authority_digest
            or _canonical_digest(raw, "manifest_digest") != observed
            or raw["identity"] != AUTHORITY_IDENTITY
            or raw["context_ready"] is not True
        ):
            raise ValueError("context authority digest is invalid")
        btc_a_candles = _source(btc_a, "BTCUSDT")
        btc_b_candles = _source(btc_b, "BTCUSDT")
        eth_candles = _source(eth, "ETHUSDT")
        sol_candles = _source(sol, "SOLUSDT")
        target = raw["target"]
        contexts = {item["symbol"]: item for item in raw["context_datasets"]}
        if (
            set(contexts) != {"ETHUSDT", "SOLUSDT"}
            or target["symbol"] != "BTCUSDT"
            or target["interval"] != "1m"
        ):
            raise ValueError("context authority target is invalid")
        expected_btc = (btc_a.metadata.candles_sha256, btc_b.metadata.candles_sha256)
        actual_btc = tuple(item["candles_sha256"] for item in target["datasets"])
        if (
            actual_btc != expected_btc
            or tuple(item.get("candle_count") for item in target["datasets"])
            != (len(btc_a_candles), len(btc_b_candles))
            or target["requested_start_ms"] != btc_a.metadata.requested_start_ms
            or target["requested_end_ms"] != btc_b.metadata.requested_end_ms
            or btc_a.metadata.requested_end_ms != btc_b.metadata.requested_start_ms
        ):
            raise ValueError("context authority BTC binding is invalid")
        for symbol, dataset in (("ETHUSDT", eth), ("SOLUSDT", sol)):
            item = contexts[symbol]
            provenance = json.loads(
                Path(item["segmented_provenance_path"]).read_text(encoding="utf-8")
            )
            segments = provenance.get("segments")
            if (
                item["interval"] != "1m"
                or item["candles_sha256"] != dataset.metadata.candles_sha256
                or item.get("candle_count") != len(dataset.candles)
                or not isinstance(item.get("segmented_provenance_digest"), str)
                or len(item["segmented_provenance_digest"]) != 64
                or provenance.get("identity") != "chappie_segmented_historical_acquisition/1.0.0"
                or provenance.get("provenance_digest") != item["segmented_provenance_digest"]
                or _canonical_digest(provenance, "provenance_digest")
                != item["segmented_provenance_digest"]
                or provenance.get("symbol") != symbol
                or provenance.get("interval") != "1m"
                or provenance.get("merged_candles_sha256") != dataset.metadata.candles_sha256
                or provenance.get("requested_start_ms") != target["requested_start_ms"]
                or provenance.get("requested_end_ms") != target["requested_end_ms"]
                or provenance.get("segment_count") != 2
                or not isinstance(segments, list)
                or len(segments) != 2
                or segments[0].get("start_ms") != target["requested_start_ms"]
                or segments[0].get("end_ms") != segments[1].get("start_ms")
                or segments[1].get("end_ms") != target["requested_end_ms"]
                or sum(segment.get("candle_count", 0) for segment in segments)
                != len(dataset.candles)
                or dataset.metadata.requested_start_ms != target["requested_start_ms"]
                or dataset.metadata.requested_end_ms != target["requested_end_ms"]
            ):
                raise ValueError("context authority context binding is invalid")
        btc_times = {candle.open_time_ms for candle in (*btc_a_candles, *btc_b_candles)}
        eth_times = {candle.open_time_ms for candle in eth_candles}
        sol_times = {candle.open_time_ms for candle in sol_candles}
        alignment = raw["alignment"]
        source_validation = raw["source_validation"]
        if (
            len(btc_times) != EXPECTED_COMMON_TIMESTAMPS
            or len(eth_times) != EXPECTED_COMMON_TIMESTAMPS
            or len(sol_times) != EXPECTED_COMMON_TIMESTAMPS
            or btc_times != eth_times
            or btc_times != sol_times
            or alignment
            != {
                "btc_eth_common_timestamps": EXPECTED_COMMON_TIMESTAMPS,
                "btc_eth_missing_timestamps": 0,
                "btc_eth_extra_timestamps": 0,
                "btc_sol_common_timestamps": EXPECTED_COMMON_TIMESTAMPS,
                "btc_sol_missing_timestamps": 0,
                "btc_sol_extra_timestamps": 0,
                "eth_segment_a_aligned_timestamps": len(btc_a_candles),
                "eth_segment_b_aligned_timestamps": len(btc_b_candles),
                "sol_segment_a_aligned_timestamps": len(btc_a_candles),
                "sol_segment_b_aligned_timestamps": len(btc_b_candles),
            }
            or source_validation
            != {
                "provider": "https://data-api.binance.vision",
                "interval_ms": 60_000,
                "ordered": True,
                "zero_gaps": True,
                "zero_duplicates": True,
                "reload_digest_verified": True,
            }
        ):
            raise ValueError("context authority alignment is invalid")
        return raw
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("context authority validation failed") from error


class MarketPrecursorContextBuilderV1:
    def __init__(self, expected_authority_digest: str) -> None:
        self._expected_authority_digest = expected_authority_digest
        self._config = MarketPrecursorContextFeatureConfigV1()

    @property
    def config(self) -> MarketPrecursorContextFeatureConfigV1:
        return self._config

    def iter_rows(
        self,
        authority_path: Path,
        btc_a: HistoricalDataset,
        btc_b: HistoricalDataset,
        eth: HistoricalDataset,
        sol: HistoricalDataset,
    ) -> Iterator[MarketPrecursorContextRowV1]:
        validate_context_authority(
            authority_path, self._expected_authority_digest, btc_a, btc_b, eth, sol
        )
        btc = (*_source(btc_a, "BTCUSDT"), *_source(btc_b, "BTCUSDT"))
        eth_candles = _source(eth, "ETHUSDT")
        sol_candles = _source(sol, "SOLUSDT")
        for index in range(len(btc)):
            if (
                btc[index].open_time_ms != eth_candles[index].open_time_ms
                or btc[index].open_time_ms != sol_candles[index].open_time_ms
            ):
                raise ValueError("context timestamp join is invalid")
            if index < self._config.warmup:
                continue
            features = _features(btc, eth_candles, sol_candles, index)
            if set(features) != _FEATURE_KEYS:
                raise ValueError("context feature set is invalid")
            base = {
                "close_time_ms": btc[index].close_time_ms,
                "target_symbol": "BTCUSDT",
                "target_interval": "1m",
                "authority_manifest_identity": AUTHORITY_IDENTITY,
                "authority_manifest_digest": self._expected_authority_digest,
                "btc_source_digests": [
                    btc_a.metadata.candles_sha256,
                    btc_b.metadata.candles_sha256,
                ],
                "eth_source_digest": eth.metadata.candles_sha256,
                "sol_source_digest": sol.metadata.candles_sha256,
                "feature_contract_identity": CONTEXT_FEATURE_CONTRACT_IDENTITY,
                "features": {key: str(value) for key, value in sorted(features.items())},
            }
            yield MarketPrecursorContextRowV1(
                btc[index].close_time_ms,
                "BTCUSDT",
                "1m",
                AUTHORITY_IDENTITY,
                self._expected_authority_digest,
                (btc_a.metadata.candles_sha256, btc_b.metadata.candles_sha256),
                eth.metadata.candles_sha256,
                sol.metadata.candles_sha256,
                CONTEXT_FEATURE_CONTRACT_IDENTITY,
                features,
                digest(base),
            )

    def materialize(
        self,
        authority_path: Path,
        btc_a: HistoricalDataset,
        btc_b: HistoricalDataset,
        eth: HistoricalDataset,
        sol: HistoricalDataset,
        rows_path: Path,
    ) -> MarketPrecursorContextManifestV1:
        rows_path = Path(rows_path)
        manifest_path = rows_path.with_suffix(rows_path.suffix + ".manifest.json")
        temporary = rows_path.with_name(f".{rows_path.name}.partial")
        if rows_path.exists() or manifest_path.exists() or temporary.exists():
            raise FileExistsError("context output path is not clean")
        authority = validate_context_authority(
            authority_path, self._expected_authority_digest, btc_a, btc_b, eth, sol
        )
        sequence, count = sha256(), 0
        drops = {"INSUFFICIENT_CONTEXT_FEATURE_HISTORY": self._config.warmup}
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as output:
                for row in self.iter_rows(authority_path, btc_a, btc_b, eth, sol):
                    output.write(
                        json.dumps(
                            {**row.payload(), "row_digest": row.row_digest},
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
                    sequence.update(row.row_digest.encode("ascii"))
                    sequence.update(b"\n")
                    count += 1
                output.flush()
                os.fsync(output.fileno())
            dataset_digest = _dataset_digest(
                self._expected_authority_digest, self._config, sequence.hexdigest(), drops
            )
            manifest = MarketPrecursorContextManifestV1(
                self._expected_authority_digest,
                "BTCUSDT",
                "1m",
                authority["target"]["requested_start_ms"],
                authority["target"]["requested_end_ms"],
                self._config,
                count,
                drops,
                sequence.hexdigest(),
                dataset_digest,
                rows_path.name,
            )
            manifest_temporary = manifest_path.with_name(f".{manifest_path.name}.partial")
            manifest_temporary.write_text(
                json.dumps(manifest.payload(), sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, rows_path)
            os.replace(manifest_temporary, manifest_path)
            return manifest
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise


def _return(candles: tuple[Candle, ...], index: int, window: int) -> Decimal:
    return candles[index].close / candles[index - window].close - 1


def _atr_bps(candles: tuple[Candle, ...], index: int) -> Decimal:
    sample = candles[index - 14 : index + 1]
    atr = average_true_range(
        tuple(c.high for c in sample),
        tuple(c.low for c in sample),
        tuple(c.close for c in sample),
        14,
    )
    return atr / candles[index].close * Decimal(10_000)


def _sign(value: Decimal) -> int:
    return 1 if value > 0 else -1 if value < 0 else 0


def _agreement(*values: Decimal) -> Decimal:
    signs = {_sign(value) for value in values}
    return Decimal(1) if len(signs) == 1 and 0 not in signs else Decimal(0)


def _features(
    btc: tuple[Candle, ...], eth: tuple[Candle, ...], sol: tuple[Candle, ...], index: int
) -> dict[str, Decimal]:
    b5, b15 = _return(btc, index, 5), _return(btc, index, 15)
    e5, e15, e60 = (_return(eth, index, window) for window in (5, 15, 60))
    s5, s15, s60 = (_return(sol, index, window) for window in (5, 15, 60))
    eth_baseline = sum((c.volume for c in eth[index - 60 : index]), Decimal(0)) / Decimal(60)
    sol_baseline = sum((c.volume for c in sol[index - 60 : index]), Decimal(0)) / Decimal(60)
    if eth_baseline <= 0 or sol_baseline <= 0:
        raise ValueError("context volume baseline is invalid")
    return {
        "eth.return_5m": e5,
        "eth.return_15m": e15,
        "eth.return_60m": e60,
        "eth.volume_ratio_60m": eth[index].volume / eth_baseline,
        "eth.atr_14_bps": _atr_bps(eth, index),
        "eth.acceleration_5m_vs_15m": e5 - e15,
        "sol.return_5m": s5,
        "sol.return_15m": s15,
        "sol.return_60m": s60,
        "sol.volume_ratio_60m": sol[index].volume / sol_baseline,
        "sol.atr_14_bps": _atr_bps(sol, index),
        "sol.acceleration_5m_vs_15m": s5 - s15,
        "cross_market.eth_minus_btc_return_5m": e5 - b5,
        "cross_market.eth_minus_btc_return_15m": e15 - b15,
        "cross_market.sol_minus_btc_return_5m": s5 - b5,
        "cross_market.sol_minus_btc_return_15m": s15 - b15,
        "cross_market.btc_eth_direction_agreement": _agreement(b5, e5),
        "cross_market.btc_sol_direction_agreement": _agreement(b5, s5),
        "cross_market.all_three_direction_agreement": _agreement(b5, e5, s5),
    }


def iter_materialized_context_rows(rows_path: Path) -> Iterator[MarketPrecursorContextRowV1]:
    with Path(rows_path).open(encoding="utf-8") as source:
        for line in source:
            try:
                raw = json.loads(line)
                row_digest = raw.pop("row_digest")
                row = MarketPrecursorContextRowV1(
                    raw["close_time_ms"],
                    raw["target_symbol"],
                    raw["target_interval"],
                    raw["authority_manifest_identity"],
                    raw["authority_manifest_digest"],
                    tuple(raw["btc_source_digests"]),
                    raw["eth_source_digest"],
                    raw["sol_source_digest"],
                    raw["feature_contract_identity"],
                    {key: Decimal(value) for key, value in raw["features"].items()},
                    row_digest,
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise ValueError("materialized context row is invalid") from error
            if set(row.features) != _FEATURE_KEYS or digest(row.payload()) != row.row_digest:
                raise ValueError("materialized context row digest is invalid")
            yield row


def _dataset_digest(
    authority_digest: str,
    config: MarketPrecursorContextFeatureConfigV1,
    sequence: str,
    drops: dict[str, int],
) -> str:
    return digest(
        {
            "authority_manifest_digest": authority_digest,
            "feature_config": config.payload(),
            "row_sequence_digest": sequence,
            "drop_counts": drops,
        }
    )


def validate_materialized_context_dataset(
    rows_path: Path, *, expected_authority_digest: str
) -> None:
    rows_path = Path(rows_path)
    manifest_path = rows_path.with_suffix(rows_path.suffix + ".manifest.json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        config = MarketPrecursorContextFeatureConfigV1()
        required = {
            "identity",
            "authority_manifest_identity",
            "authority_manifest_digest",
            "target_symbol",
            "target_interval",
            "source_start_ms",
            "source_end_ms",
            "feature_contract_identity",
            "feature_contract_digest",
            "warmup",
            "row_count",
            "drop_counts",
            "row_sequence_digest",
            "dataset_digest",
            "rows_path",
        }
        if (
            set(manifest) != required
            or manifest["identity"] != CONTEXT_FEATURE_DATASET_IDENTITY
            or manifest["authority_manifest_identity"] != AUTHORITY_IDENTITY
            or manifest["authority_manifest_digest"] != expected_authority_digest
            or manifest["feature_contract_identity"] != CONTEXT_FEATURE_CONTRACT_IDENTITY
            or manifest["feature_contract_digest"] != config.contract_digest
            or manifest["warmup"] != config.warmup
            or manifest["rows_path"] != rows_path.name
        ):
            raise ValueError("context manifest binding is invalid")
        sequence, count, previous = sha256(), 0, None
        for row in iter_materialized_context_rows(rows_path):
            if (
                row.authority_manifest_digest != expected_authority_digest
                or row.authority_manifest_identity != AUTHORITY_IDENTITY
                or row.target_symbol != "BTCUSDT"
                or row.target_interval != "1m"
                or row.feature_contract_identity != CONTEXT_FEATURE_CONTRACT_IDENTITY
                or len(row.btc_source_digests) != 2
                or (previous is not None and row.close_time_ms <= previous)
            ):
                raise ValueError("context row manifest binding is invalid")
            sequence.update(row.row_digest.encode("ascii"))
            sequence.update(b"\n")
            count += 1
            previous = row.close_time_ms
        if (
            count != manifest["row_count"]
            or count == 0
            or sequence.hexdigest() != manifest["row_sequence_digest"]
            or _dataset_digest(
                expected_authority_digest, config, sequence.hexdigest(), manifest["drop_counts"]
            )
            != manifest["dataset_digest"]
        ):
            raise ValueError("materialized context dataset validation failed")
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("materialized context dataset validation failed") from error
