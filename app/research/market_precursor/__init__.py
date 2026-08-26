"""Causal, read-only market precursor research datasets."""

from .builder import MarketPrecursorBuilderV1, iter_materialized_rows, validate_materialized_dataset
from .context import (
    MarketPrecursorContextBuilderV1,
    iter_materialized_context_rows,
    validate_context_authority,
    validate_materialized_context_dataset,
)
from .models import (
    CONTEXT_FEATURE_CONTRACT_IDENTITY,
    CONTEXT_FEATURE_DATASET_IDENTITY,
    FEATURE_CONTRACT_IDENTITY,
    LABEL_CONTRACT_IDENTITY,
    MARKET_PRECURSOR_DATASET_IDENTITY,
    MarketPrecursorDatasetV1,
    MarketPrecursorFeatureConfigV1,
)

__all__ = [
    "CONTEXT_FEATURE_CONTRACT_IDENTITY",
    "CONTEXT_FEATURE_DATASET_IDENTITY",
    "FEATURE_CONTRACT_IDENTITY",
    "LABEL_CONTRACT_IDENTITY",
    "MARKET_PRECURSOR_DATASET_IDENTITY",
    "MarketPrecursorBuilderV1",
    "MarketPrecursorContextBuilderV1",
    "MarketPrecursorDatasetV1",
    "MarketPrecursorFeatureConfigV1",
    "iter_materialized_context_rows",
    "iter_materialized_rows",
    "validate_context_authority",
    "validate_materialized_context_dataset",
    "validate_materialized_dataset",
]
