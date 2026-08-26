"""Causal, read-only market precursor research datasets."""

from .builder import MarketPrecursorBuilderV1, iter_materialized_rows, validate_materialized_dataset
from .models import (
    FEATURE_CONTRACT_IDENTITY,
    LABEL_CONTRACT_IDENTITY,
    MARKET_PRECURSOR_DATASET_IDENTITY,
    MarketPrecursorDatasetV1,
    MarketPrecursorFeatureConfigV1,
)

__all__ = [
    "FEATURE_CONTRACT_IDENTITY",
    "LABEL_CONTRACT_IDENTITY",
    "MARKET_PRECURSOR_DATASET_IDENTITY",
    "MarketPrecursorBuilderV1",
    "MarketPrecursorDatasetV1",
    "MarketPrecursorFeatureConfigV1",
    "iter_materialized_rows",
    "validate_materialized_dataset",
]
