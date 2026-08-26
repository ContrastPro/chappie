"""Immutable contracts for causal precursor observations."""

from dataclasses import dataclass
from decimal import Decimal

from app.ai.schemas import digest

MARKET_PRECURSOR_DATASET_IDENTITY = "chappie_market_precursor_dataset/1.0.0"
FEATURE_CONTRACT_IDENTITY = "chappie_market_precursor_features/1.0.0"
LABEL_CONTRACT_IDENTITY = "chappie_market_precursor_labels/1.0.0"


@dataclass(frozen=True, slots=True)
class MarketPrecursorFeatureConfigV1:
    fast_ema_window: int = 12
    slow_ema_window: int = 26
    ema_slope_window: int = 15
    atr_window: int = 14
    volatility_window: int = 60
    compression_window: int = 15
    volume_window: int = 60
    volume_acceleration_window: int = 5
    structure_window: int = 120
    return_windows: tuple[int, ...] = (5, 15, 30, 60)
    horizons: tuple[int, ...] = (5, 15, 60, 240)
    fee_per_fill: Decimal = Decimal("0.001")

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value <= 0
            for value in self.windows
        ):
            raise ValueError("precursor windows must be positive integers")
        if self.return_windows != (5, 15, 30, 60) or self.horizons != (5, 15, 60, 240):
            raise ValueError("V1 return windows and horizons are fixed")
        if not self.fee_per_fill.is_finite() or self.fee_per_fill <= 0:
            raise ValueError("fee per fill must be positive and finite")

    @property
    def windows(self) -> tuple[int, ...]:
        return (
            self.fast_ema_window,
            self.slow_ema_window,
            self.ema_slope_window,
            self.atr_window,
            self.volatility_window,
            self.compression_window,
            self.volume_window,
            self.volume_acceleration_window,
            self.structure_window,
            *self.return_windows,
            *self.horizons,
        )

    @property
    def warmup(self) -> int:
        return max(
            self.fast_ema_window,
            self.slow_ema_window,
            self.ema_slope_window + self.slow_ema_window,
            self.atr_window,
            self.volatility_window,
            self.volume_window,
            self.structure_window,
            *self.return_windows,
        )

    def payload(self) -> dict[str, object]:
        return {
            "identity": FEATURE_CONTRACT_IDENTITY,
            "fast_ema_window": self.fast_ema_window,
            "slow_ema_window": self.slow_ema_window,
            "ema_slope_window": self.ema_slope_window,
            "atr_window": self.atr_window,
            "volatility_window": self.volatility_window,
            "compression_window": self.compression_window,
            "volume_window": self.volume_window,
            "volume_acceleration_window": self.volume_acceleration_window,
            "structure_window": self.structure_window,
            "return_windows": self.return_windows,
            "horizons": self.horizons,
            "fee_per_fill": str(self.fee_per_fill),
        }

    @property
    def contract_digest(self) -> str:
        return digest(self.payload())


@dataclass(frozen=True, slots=True)
class MarketPrecursorRowV1:
    close_time_ms: int
    target_symbol: str
    target_interval: str
    feature_contract_identity: str
    source_dataset_digest: str
    features: dict[str, Decimal]
    labels: dict[str, Decimal | bool]
    row_digest: str

    def payload(self) -> dict[str, object]:
        return {
            "close_time_ms": self.close_time_ms,
            "target_symbol": self.target_symbol,
            "target_interval": self.target_interval,
            "feature_contract_identity": self.feature_contract_identity,
            "source_dataset_digest": self.source_dataset_digest,
            "features": {k: str(v) for k, v in sorted(self.features.items())},
            "labels": {
                k: str(v) if isinstance(v, Decimal) else v for k, v in sorted(self.labels.items())
            },
        }


@dataclass(frozen=True, slots=True)
class MarketPrecursorDatasetV1:
    target_symbol: str
    target_interval: str
    source_dataset_digest: str
    source_start_ms: int
    source_end_ms: int
    feature_config: MarketPrecursorFeatureConfigV1
    context_sources: tuple[str, ...]
    rows: tuple[MarketPrecursorRowV1, ...]
    drop_counts: dict[str, int]
    dataset_digest: str

    def payload(self) -> dict[str, object]:
        return {
            "identity": MARKET_PRECURSOR_DATASET_IDENTITY,
            "target_symbol": self.target_symbol,
            "target_interval": self.target_interval,
            "source_dataset_digest": self.source_dataset_digest,
            "source_start_ms": self.source_start_ms,
            "source_end_ms": self.source_end_ms,
            "feature_contract_identity": FEATURE_CONTRACT_IDENTITY,
            "feature_contract_digest": self.feature_config.contract_digest,
            "label_contract_identity": LABEL_CONTRACT_IDENTITY,
            "label_contract_digest": digest(
                {
                    "identity": LABEL_CONTRACT_IDENTITY,
                    "horizons": self.feature_config.horizons,
                    "fee_per_fill": str(self.feature_config.fee_per_fill),
                }
            ),
            "warmup": self.feature_config.warmup,
            "horizons": self.feature_config.horizons,
            "context_sources": self.context_sources,
            "row_count": len(self.rows),
            "drop_counts": dict(sorted(self.drop_counts.items())),
            "row_digests": [row.row_digest for row in self.rows],
        }


@dataclass(frozen=True, slots=True)
class MarketPrecursorManifestV1:
    """Authoritative metadata for an NDJSON precursor artifact."""

    source_dataset_digest: str
    target_symbol: str
    target_interval: str
    feature_config: MarketPrecursorFeatureConfigV1
    row_count: int
    drop_counts: dict[str, int]
    row_sequence_digest: str
    dataset_digest: str
    rows_path: str

    def payload(self) -> dict[str, object]:
        return {
            "identity": MARKET_PRECURSOR_DATASET_IDENTITY,
            "source_dataset_digest": self.source_dataset_digest,
            "target_symbol": self.target_symbol,
            "target_interval": self.target_interval,
            "feature_contract_identity": FEATURE_CONTRACT_IDENTITY,
            "feature_contract_digest": self.feature_config.contract_digest,
            "label_contract_identity": LABEL_CONTRACT_IDENTITY,
            "label_contract_digest": digest(
                {
                    "identity": LABEL_CONTRACT_IDENTITY,
                    "horizons": self.feature_config.horizons,
                    "fee_per_fill": str(self.feature_config.fee_per_fill),
                }
            ),
            "warmup": self.feature_config.warmup,
            "horizons": self.feature_config.horizons,
            "row_count": self.row_count,
            "drop_counts": dict(sorted(self.drop_counts.items())),
            "row_sequence_digest": self.row_sequence_digest,
            "dataset_digest": self.dataset_digest,
            "rows_path": self.rows_path,
            "context_sources": (),
        }
