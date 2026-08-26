"""Single-pass causal feature and strictly future-label construction."""

import json
import os
from collections import deque
from decimal import Decimal, localcontext
from hashlib import sha256
from math import sqrt
from pathlib import Path

from app.ai.schemas import digest
from app.market.models import Candle
from app.research.strategy.history import HistoricalDataset, candles_sha256, validate_candles
from app.strategies.ema_crossover import EMA_DECIMAL_PRECISION
from app.strategies.indicators import average_true_range

from .models import (
    FEATURE_CONTRACT_IDENTITY,
    MarketPrecursorDatasetV1,
    MarketPrecursorFeatureConfigV1,
    MarketPrecursorManifestV1,
    MarketPrecursorRowV1,
)


class MarketPrecursorBuilderV1:
    def __init__(self, config: MarketPrecursorFeatureConfigV1 | None = None) -> None:
        self._config = config or MarketPrecursorFeatureConfigV1()

    def build(self, dataset: HistoricalDataset) -> MarketPrecursorDatasetV1:
        metadata, candles = (
            dataset.metadata,
            validate_candles(dataset.candles, dataset.metadata.interval),
        )
        if metadata.symbol != "BTCUSDT" or metadata.interval != "1m":
            raise ValueError("V1 precursor target must be BTCUSDT 1m")
        if (
            len(candles) != metadata.candle_count
            or candles_sha256(candles) != metadata.candles_sha256
        ):
            raise ValueError("source dataset count binding is invalid")
        rows: list[MarketPrecursorRowV1] = []
        drops = {"INSUFFICIENT_FEATURE_HISTORY": 0, "INSUFFICIENT_FUTURE_LABEL_HISTORY": 0}
        fast = slow = None
        closes: deque[Decimal] = deque(maxlen=self._config.structure_window + 1)
        volumes: deque[Decimal] = deque(maxlen=self._config.volume_window)
        ranges: deque[Decimal] = deque(maxlen=self._config.volatility_window)
        ohlc: deque[Candle] = deque(maxlen=self._config.atr_window + 1)
        structure_candles: deque[Candle] = deque(maxlen=self._config.structure_window)
        ema_fast: deque[Decimal] = deque(maxlen=self._config.ema_slope_window + 1)
        ema_slow: deque[Decimal] = deque(maxlen=self._config.ema_slope_window + 1)
        for index, candle in enumerate(candles):
            with localcontext() as context:
                context.prec = EMA_DECIMAL_PRECISION
                fast = (
                    candle.close
                    if fast is None
                    else candle.close * Decimal(2) / (self._config.fast_ema_window + 1)
                    + fast * (Decimal(1) - Decimal(2) / (self._config.fast_ema_window + 1))
                )
                slow = (
                    candle.close
                    if slow is None
                    else candle.close * Decimal(2) / (self._config.slow_ema_window + 1)
                    + slow * (Decimal(1) - Decimal(2) / (self._config.slow_ema_window + 1))
                )
            closes.append(candle.close)
            volumes.append(candle.volume)
            ranges.append((candle.high - candle.low) / candle.close)
            ohlc.append(candle)
            structure_candles.append(candle)
            ema_fast.append(fast)
            ema_slow.append(slow)
            if index < self._config.warmup - 1:
                drops["INSUFFICIENT_FEATURE_HISTORY"] += 1
                continue
            if index + max(self._config.horizons) >= len(candles):
                drops["INSUFFICIENT_FUTURE_LABEL_HISTORY"] += 1
                continue
            features = self._features(
                candle,
                closes,
                volumes,
                ranges,
                ohlc,
                structure_candles,
                ema_fast,
                ema_slow,
                fast,
                slow,
            )
            labels = self._labels(candles, index)
            base = {
                "close_time_ms": candle.close_time_ms,
                "target_symbol": metadata.symbol,
                "target_interval": metadata.interval,
                "feature_contract_identity": FEATURE_CONTRACT_IDENTITY,
                "source_dataset_digest": metadata.candles_sha256,
                "features": {k: str(v) for k, v in sorted(features.items())},
                "labels": {
                    k: str(v) if isinstance(v, Decimal) else v for k, v in sorted(labels.items())
                },
            }
            rows.append(
                MarketPrecursorRowV1(
                    candle.close_time_ms,
                    metadata.symbol,
                    metadata.interval,
                    FEATURE_CONTRACT_IDENTITY,
                    metadata.candles_sha256,
                    features,
                    labels,
                    digest(base),
                )
            )
        base = {
            "target_symbol": metadata.symbol,
            "target_interval": metadata.interval,
            "source_dataset_digest": metadata.candles_sha256,
            "source_start_ms": metadata.actual_first_open_ms,
            "source_end_ms": metadata.actual_last_open_ms,
            "feature_config": self._config.payload(),
            "context_sources": (),
            "row_digests": [row.row_digest for row in rows],
            "drop_counts": drops,
        }
        return MarketPrecursorDatasetV1(
            metadata.symbol,
            metadata.interval,
            metadata.candles_sha256,
            metadata.actual_first_open_ms,
            metadata.actual_last_open_ms,
            self._config,
            (),
            tuple(rows),
            drops,
            digest(base),
        )

    def materialize(self, dataset: HistoricalDataset, rows_path: Path) -> MarketPrecursorManifestV1:
        """Write canonical NDJSON rows without retaining output rows in memory."""
        rows_path = Path(rows_path)
        if rows_path.exists():
            raise FileExistsError(f"precursor rows already exist: {rows_path}")
        metadata, candles = (
            dataset.metadata,
            validate_candles(dataset.candles, dataset.metadata.interval),
        )
        if (
            metadata.symbol != "BTCUSDT"
            or metadata.interval != "1m"
            or candles_sha256(candles) != metadata.candles_sha256
        ):
            raise ValueError("precursor source binding is invalid")
        temporary = rows_path.with_name(f".{rows_path.name}.partial")
        manifest_path = rows_path.with_suffix(rows_path.suffix + ".manifest.json")
        if temporary.exists() or manifest_path.exists():
            raise FileExistsError("precursor output path is not clean")
        drops = {
            "INSUFFICIENT_FEATURE_HISTORY": self._config.warmup - 1,
            "INSUFFICIENT_FUTURE_LABEL_HISTORY": max(self._config.horizons),
        }
        row_sequence = sha256()
        count = 0
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as output:
                for row in self.iter_rows(dataset):
                    output.write(
                        json.dumps(
                            {**row.payload(), "row_digest": row.row_digest},
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
                    row_sequence.update(row.row_digest.encode("ascii"))
                    row_sequence.update(b"\n")
                    count += 1
                output.flush()
                os.fsync(output.fileno())
            semantic = _semantic_dataset_digest(
                metadata.candles_sha256, self._config, row_sequence.hexdigest(), drops
            )
            manifest = MarketPrecursorManifestV1(
                metadata.candles_sha256,
                metadata.symbol,
                metadata.interval,
                self._config,
                count,
                drops,
                row_sequence.hexdigest(),
                semantic,
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

    def iter_rows(self, dataset: HistoricalDataset):
        """Yield rows in source order; only rolling state and one row are retained."""
        metadata, candles = (
            dataset.metadata,
            validate_candles(dataset.candles, dataset.metadata.interval),
        )
        if (
            metadata.symbol != "BTCUSDT"
            or metadata.interval != "1m"
            or candles_sha256(candles) != metadata.candles_sha256
        ):
            raise ValueError("precursor source binding is invalid")
        fast = slow = None
        closes = deque(maxlen=self._config.structure_window + 1)
        volumes = deque(maxlen=self._config.volume_window)
        ranges = deque(maxlen=self._config.volatility_window)
        ohlc = deque(maxlen=self._config.atr_window + 1)
        structure = deque(maxlen=self._config.structure_window)
        fasts = deque(maxlen=self._config.ema_slope_window + 1)
        slows = deque(maxlen=self._config.ema_slope_window + 1)
        for index, candle in enumerate(candles):
            with localcontext() as context:
                context.prec = EMA_DECIMAL_PRECISION
                fast = (
                    candle.close
                    if fast is None
                    else candle.close * Decimal(2) / (self._config.fast_ema_window + 1)
                    + fast * (Decimal(1) - Decimal(2) / (self._config.fast_ema_window + 1))
                )
                slow = (
                    candle.close
                    if slow is None
                    else candle.close * Decimal(2) / (self._config.slow_ema_window + 1)
                    + slow * (Decimal(1) - Decimal(2) / (self._config.slow_ema_window + 1))
                )
            closes.append(candle.close)
            volumes.append(candle.volume)
            ranges.append((candle.high - candle.low) / candle.close)
            ohlc.append(candle)
            structure.append(candle)
            fasts.append(fast)
            slows.append(slow)
            if index < self._config.warmup - 1 or index + max(self._config.horizons) >= len(
                candles
            ):
                continue
            features = self._features(
                candle, closes, volumes, ranges, ohlc, structure, fasts, slows, fast, slow
            )
            labels = self._labels(candles, index)
            base = {
                "close_time_ms": candle.close_time_ms,
                "target_symbol": metadata.symbol,
                "target_interval": metadata.interval,
                "feature_contract_identity": FEATURE_CONTRACT_IDENTITY,
                "source_dataset_digest": metadata.candles_sha256,
                "features": {k: str(v) for k, v in sorted(features.items())},
                "labels": {
                    k: str(v) if isinstance(v, Decimal) else v for k, v in sorted(labels.items())
                },
            }
            yield MarketPrecursorRowV1(
                candle.close_time_ms,
                metadata.symbol,
                metadata.interval,
                FEATURE_CONTRACT_IDENTITY,
                metadata.candles_sha256,
                features,
                labels,
                digest(base),
            )

    def _features(
        self, candle, closes, volumes, ranges, ohlc, structure_candles, fasts, slows, fast, slow
    ):
        c = self._config
        close_list = tuple(closes)
        volume_list = tuple(volumes)
        range_list = tuple(ranges)
        mean_volume = sum(volume_list, Decimal(0)) / len(volume_list)
        variance = sum((v - mean_volume) ** 2 for v in volume_list) / len(volume_list)
        std = Decimal(str(sqrt(float(variance))))
        structural_high = max(item.high for item in structure_candles)
        structural_low = min(item.low for item in structure_candles)
        atr = average_true_range(
            tuple(x.high for x in ohlc),
            tuple(x.low for x in ohlc),
            tuple(x.close for x in ohlc),
            c.atr_window,
        )
        result = {f"return_{w}m": candle.close / close_list[-w - 1] - 1 for w in c.return_windows}
        average_range = sum(range_list, Decimal(0)) / len(range_list)
        short_range = sum(range_list[-c.compression_window :], Decimal(0)) / c.compression_window
        acceleration_mean = (
            sum(volume_list[-c.volume_acceleration_window - 1 : -1], Decimal(0))
            / c.volume_acceleration_window
        )
        range_width = structural_high - structural_low
        realized = Decimal(
            str(sqrt(float(sum(value * value for value in result.values()) / len(result))))
        )
        result.update(
            {
                "fast_ema": fast,
                "slow_ema": slow,
                "ema_separation_bps": abs(fast - slow) / candle.close * Decimal(10000),
                "fast_ema_slope": fast / fasts[0] - 1,
                "slow_ema_slope": slow / slows[0] - 1,
                "atr_bps": atr / candle.close * Decimal(10000),
                "realized_volatility": realized,
                "volatility_compression_ratio": short_range / average_range,
                "candle_range_anomaly": range_list[-1] / average_range,
                "volume_mean_ratio": candle.volume / mean_volume,
                "volume_zscore": (candle.volume - mean_volume) / std if std else Decimal(0),
                "volume_acceleration": candle.volume / acceleration_mean,
                "range_high": structural_high,
                "range_low": structural_low,
                "range_position": (candle.close - structural_low) / range_width
                if range_width
                else Decimal(0),
                "distance_recent_high": candle.close / structural_high - 1,
                "distance_recent_low": candle.close / structural_low - 1,
                "local_drawdown": candle.close / structural_high - 1,
                "breakout_proximity": (structural_high - candle.close) / range_width
                if range_width
                else Decimal(0),
            }
        )
        return result

    def _labels(self, candles, index):
        current = candles[index].close
        labels = {}
        for horizon in self._config.horizons:
            future = candles[index + 1 : index + horizon + 1]
            ret = future[-1].close / current - 1
            up = max(x.high for x in future) / current - 1
            down = min(x.low for x in future) / current - 1
            labels.update(
                {
                    f"future_return_{horizon}m": ret,
                    f"upside_excursion_{horizon}m": up,
                    f"downside_excursion_{horizon}m": down,
                    f"up_move_exceeds_cost_2x_{horizon}m": up >= self._config.fee_per_fill * 4,
                    f"down_move_exceeds_cost_2x_{horizon}m": -down >= self._config.fee_per_fill * 4,
                    f"up_move_exceeds_cost_3x_{horizon}m": up >= self._config.fee_per_fill * 6,
                    f"down_move_exceeds_cost_3x_{horizon}m": -down >= self._config.fee_per_fill * 6,
                }
            )
        return labels


def iter_materialized_rows(rows_path: Path):
    """Read and validate canonical NDJSON rows without accumulating them."""
    with Path(rows_path).open(encoding="utf-8") as source:
        for line in source:
            try:
                raw = json.loads(line)
                row_digest = raw.pop("row_digest")
                features = {key: Decimal(value) for key, value in raw["features"].items()}
                labels = {
                    key: value if isinstance(value, bool) else Decimal(value)
                    for key, value in raw["labels"].items()
                }
                row = MarketPrecursorRowV1(
                    raw["close_time_ms"],
                    raw["target_symbol"],
                    raw["target_interval"],
                    raw["feature_contract_identity"],
                    raw["source_dataset_digest"],
                    features,
                    labels,
                    row_digest,
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise ValueError("materialized precursor row is invalid") from error
            if digest(row.payload()) != row.row_digest:
                raise ValueError("materialized precursor row digest is invalid")
            yield row


def _semantic_dataset_digest(source_digest, config, row_sequence_digest, drops):
    return digest(
        {
            "source_dataset_digest": source_digest,
            "feature_config": config.payload(),
            "row_sequence_digest": row_sequence_digest,
            "drop_counts": drops,
        }
    )


def validate_materialized_dataset(
    rows_path: Path, *, expected_source_digest: str | None = None
) -> None:
    """Authoritatively stream-validate rows against their adjacent manifest."""
    rows_path = Path(rows_path)
    manifest_path = rows_path.with_suffix(rows_path.suffix + ".manifest.json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        required = {
            "identity",
            "source_dataset_digest",
            "target_symbol",
            "target_interval",
            "feature_contract_identity",
            "feature_contract_digest",
            "label_contract_identity",
            "label_contract_digest",
            "warmup",
            "horizons",
            "row_count",
            "drop_counts",
            "row_sequence_digest",
            "dataset_digest",
            "rows_path",
            "context_sources",
        }
        if (
            set(manifest) != required
            or manifest["identity"] != "chappie_market_precursor_dataset/1.0.0"
            or manifest["rows_path"] != rows_path.name
            or manifest["context_sources"] != []
        ):
            raise ValueError("manifest shape is invalid")
        config = MarketPrecursorFeatureConfigV1()
        if (
            manifest["feature_contract_identity"] != FEATURE_CONTRACT_IDENTITY
            or manifest["feature_contract_digest"] != config.contract_digest
            or manifest["label_contract_identity"] != "chappie_market_precursor_labels/1.0.0"
            or manifest["label_contract_digest"]
            != digest(
                {
                    "identity": "chappie_market_precursor_labels/1.0.0",
                    "horizons": config.horizons,
                    "fee_per_fill": str(config.fee_per_fill),
                }
            )
            or manifest["warmup"] != config.warmup
            or manifest["horizons"] != list(config.horizons)
        ):
            raise ValueError("manifest contract binding is invalid")
        if (
            expected_source_digest is not None
            and manifest["source_dataset_digest"] != expected_source_digest
        ):
            raise ValueError("manifest source binding is invalid")
        sequence = sha256()
        count = 0
        source = None
        for row in iter_materialized_rows(rows_path):
            if (
                row.target_symbol != manifest["target_symbol"]
                or row.target_interval != manifest["target_interval"]
                or row.source_dataset_digest != manifest["source_dataset_digest"]
            ):
                raise ValueError("row manifest binding is invalid")
            sequence.update(row.row_digest.encode("ascii"))
            sequence.update(b"\n")
            count += 1
            source = row.source_dataset_digest
        if (
            not source
            or count != manifest["row_count"]
            or sequence.hexdigest() != manifest["row_sequence_digest"]
        ):
            raise ValueError("materialized row sequence is invalid")
        if (
            _semantic_dataset_digest(source, config, sequence.hexdigest(), manifest["drop_counts"])
            != manifest["dataset_digest"]
        ):
            raise ValueError("materialized dataset digest is invalid")
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("materialized precursor dataset validation failed") from error
