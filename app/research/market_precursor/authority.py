"""Fail-closed adaptation of released precursor authorities into Discovery observations."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from .analysis import (
    FrozenHorizonOutcomeV1,
    MultiHorizonFrozenConditionObservationV1,
)
from .builder import iter_materialized_rows, validate_materialized_dataset
from .context import AUTHORITY_IDENTITY
from .context_v2 import (
    CONTEXT_FEATURE_CONTRACT_IDENTITY_V2,
    CONTEXT_FEATURE_DATASET_IDENTITY_V2,
    MarketPrecursorContextRowV2,
    iter_materialized_context_rows_v2,
    validate_materialized_context_dataset_v2,
)
from .models import MARKET_PRECURSOR_DATASET_IDENTITY, MarketPrecursorRowV1

REQUIRED_QUANTILE_FEATURES_V1 = frozenset(
    {
        "breakout_proximity",
        "cross_market.eth_minus_btc_return_15m",
        "cross_market.sol_minus_btc_return_15m",
        "eth.acceleration_5m_vs_15m",
        "eth.atr_14_bps",
        "eth.volume_ratio_60m",
        "return_15m",
        "sol.acceleration_5m_vs_15m",
        "sol.atr_14_bps",
        "sol.volume_ratio_60m",
        "volatility_compression_ratio",
    }
)
REQUIRED_EQUALITY_FEATURE_V1 = "cross_market.all_three_direction_agreement"
REQUIRED_DISCOVERY_FEATURES_V1 = REQUIRED_QUANTILE_FEATURES_V1 | {REQUIRED_EQUALITY_FEATURE_V1}
_HORIZONS = (15, 60, 240)


@dataclass(frozen=True, slots=True)
class AuthoritativeObservationAdapterEvidenceV1:
    base_identity: str
    base_digest: str
    context_feature_identity: str
    context_dataset_identity: str
    context_digest: str
    context_authority_identity: str
    context_authority_digest: str
    start_utc: str
    end_utc: str
    first_joined_timestamp_ms: int
    last_joined_timestamp_ms: int
    joined_count: int
    base_leading_rows_skipped: int
    context_leading_rows_skipped: int
    base_trailing_rows_skipped: int
    context_trailing_rows_skipped: int
    internal_mismatch_count: int
    required_feature_count: int


@dataclass(frozen=True, slots=True)
class AuthoritativeObservationSplitV1:
    observations: tuple[MultiHorizonFrozenConditionObservationV1, ...]
    evidence: AuthoritativeObservationAdapterEvidenceV1


def _utc_bound(value: str, name: str) -> tuple[str, int]:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be an ISO UTC string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO UTC string") from error
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError(f"{name} must be UTC")
    normalized = parsed.astimezone(UTC)
    return normalized.isoformat().replace("+00:00", "Z"), int(normalized.timestamp() * 1_000)


def _manifest(rows_path: Path) -> dict[str, object]:
    try:
        value = json.loads(
            rows_path.with_suffix(rows_path.suffix + ".manifest.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("authority manifest cannot be read") from error
    if not isinstance(value, dict):
        raise TypeError("authority manifest is invalid")
    return value


def _range_rows(rows: Iterable[object], start_ms: int, end_ms: int) -> Iterator[object]:
    previous = None
    for row in rows:
        timestamp = row.close_time_ms
        if not isinstance(timestamp, int) or isinstance(timestamp, bool):
            raise TypeError("authority row timestamp is invalid")
        if previous is not None and timestamp <= previous:
            raise ValueError("authority row timestamps must be strictly increasing")
        previous = timestamp
        if timestamp < start_ms:
            continue
        if timestamp >= end_ms:
            return
        yield row


def _next_or_none(iterator):
    try:
        return next(iterator)
    except StopIteration:
        return None


def _finite_decimal(value: object, name: str) -> Decimal:
    if type(value) is not Decimal or not value.is_finite():
        raise ValueError(f"required Decimal value is invalid: {name}")
    return value


def _observation(base: MarketPrecursorRowV1, context: MarketPrecursorContextRowV2):
    base_keys = set(base.features)
    context_keys = set(context.numeric_features) | set(context.boolean_features)
    if base_keys & context_keys:
        raise ValueError("base and context feature namespaces collide")
    combined = {**base.features, **context.numeric_features, **context.boolean_features}
    missing = REQUIRED_DISCOVERY_FEATURES_V1 - set(combined)
    if missing:
        raise ValueError(f"required Discovery features are missing: {sorted(missing)}")
    features = {
        name: _finite_decimal(combined[name], name)
        for name in sorted(REQUIRED_QUANTILE_FEATURES_V1)
    }
    equality = combined[REQUIRED_EQUALITY_FEATURE_V1]
    if type(equality) is not bool:
        raise ValueError("required equality feature must be an exact Boolean")
    features[REQUIRED_EQUALITY_FEATURE_V1] = equality
    outcomes = tuple(
        FrozenHorizonOutcomeV1(
            horizon,
            _finite_decimal(
                base.labels.get(f"future_return_{horizon}m"), f"future_return_{horizon}m"
            ),
            _finite_decimal(
                base.labels.get(f"upside_excursion_{horizon}m"),
                f"upside_excursion_{horizon}m",
            ),
            _finite_decimal(
                base.labels.get(f"downside_excursion_{horizon}m"),
                f"downside_excursion_{horizon}m",
            ),
        )
        for horizon in _HORIZONS
    )
    return MultiHorizonFrozenConditionObservationV1(base.close_time_ms, features, outcomes)


def _remaining_count(current, iterator) -> int:
    return (0 if current is None else 1) + sum(1 for _ in iterator)


def _join_validated_rows(base_rows, context_rows, *, start_ms: int, end_ms: int):
    base_iterator = iter(_range_rows(base_rows, start_ms, end_ms))
    context_iterator = iter(_range_rows(context_rows, start_ms, end_ms))
    base = _next_or_none(base_iterator)
    context = _next_or_none(context_iterator)
    base_leading = context_leading = 0
    while base is not None and context is not None and base.close_time_ms != context.close_time_ms:
        if base.close_time_ms < context.close_time_ms:
            base_leading += 1
            base = _next_or_none(base_iterator)
        else:
            context_leading += 1
            context = _next_or_none(context_iterator)
    if base is None or context is None:
        raise ValueError("requested authority range has no exact timestamp intersection")

    observations = []
    while base is not None and context is not None:
        if base.close_time_ms != context.close_time_ms:
            raise ValueError("internal authority timestamp mismatch")
        observations.append(_observation(base, context))
        base = _next_or_none(base_iterator)
        context = _next_or_none(context_iterator)
    base_trailing = _remaining_count(base, base_iterator)
    context_trailing = _remaining_count(context, context_iterator)
    return (
        tuple(observations),
        base_leading,
        context_leading,
        base_trailing,
        context_trailing,
    )


def load_authoritative_observation_split(
    base_rows_path: Path,
    context_v2_rows_path: Path,
    *,
    start_utc: str,
    end_utc: str,
    expected_base_digest: str,
    expected_context_digest: str,
    expected_context_authority_digest: str,
) -> AuthoritativeObservationSplitV1:
    """Validate released artifacts and build one exact, bounded observation split."""
    base_path, context_path = Path(base_rows_path), Path(context_v2_rows_path)
    normalized_start, start_ms = _utc_bound(start_utc, "start_utc")
    normalized_end, end_ms = _utc_bound(end_utc, "end_utc")
    if start_ms >= end_ms:
        raise ValueError("requested authority range must satisfy start < end")

    validate_materialized_dataset(base_path)
    context_manifest = validate_materialized_context_dataset_v2(
        context_path, expected_authority_digest=expected_context_authority_digest
    )
    base_manifest = _manifest(base_path)
    raw_context_manifest = _manifest(context_path)
    if (
        base_manifest.get("identity") != MARKET_PRECURSOR_DATASET_IDENTITY
        or base_manifest.get("dataset_digest") != expected_base_digest
    ):
        raise ValueError("base precursor authority binding is invalid")
    if (
        raw_context_manifest.get("identity") != CONTEXT_FEATURE_DATASET_IDENTITY_V2
        or raw_context_manifest.get("feature_contract_identity")
        != CONTEXT_FEATURE_CONTRACT_IDENTITY_V2
        or context_manifest.dataset_digest != expected_context_digest
        or raw_context_manifest.get("authority_manifest_identity") != AUTHORITY_IDENTITY
        or context_manifest.authority_manifest_digest != expected_context_authority_digest
    ):
        raise ValueError("typed context authority binding is invalid")

    observations, base_leading, context_leading, base_trailing, context_trailing = (
        _join_validated_rows(
            iter_materialized_rows(base_path),
            iter_materialized_context_rows_v2(context_path),
            start_ms=start_ms,
            end_ms=end_ms,
        )
    )
    evidence = AuthoritativeObservationAdapterEvidenceV1(
        MARKET_PRECURSOR_DATASET_IDENTITY,
        expected_base_digest,
        CONTEXT_FEATURE_CONTRACT_IDENTITY_V2,
        CONTEXT_FEATURE_DATASET_IDENTITY_V2,
        expected_context_digest,
        AUTHORITY_IDENTITY,
        expected_context_authority_digest,
        normalized_start,
        normalized_end,
        observations[0].timestamp_ms,
        observations[-1].timestamp_ms,
        len(observations),
        base_leading,
        context_leading,
        base_trailing,
        context_trailing,
        0,
        len(REQUIRED_DISCOVERY_FEATURES_V1),
    )
    return AuthoritativeObservationSplitV1(observations, evidence)
