"""Offline-only, causal joins of BTC candles with public market context.

This module deliberately models optional sources as optional.  A missing or
stale derivative observation does not alter a valid BTC candle and is never
silently forward-filled.  It simply makes derivative-dependent studies
unavailable for that timestamp.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal

from app.market.models import Candle

FUNDING_MAX_AGE_MS = 8 * 60 * 60 * 1_000
OI_MAX_AGE_MS = 5 * 60 * 1_000


@dataclass(frozen=True, slots=True)
class ContextObservation:
    """One public observation, available only at ``published_at_ms``."""

    timestamp_ms: int
    published_at_ms: int
    value: Decimal
    source: str


@dataclass(frozen=True, slots=True)
class ContextDataset:
    kind: str
    symbol: str
    frequency: str
    source: str
    observations: tuple[ContextObservation, ...]


def context_digest(dataset: ContextDataset) -> str:
    payload = {
        "kind": dataset.kind,
        "symbol": dataset.symbol,
        "frequency": dataset.frequency,
        "source": dataset.source,
        "observations": [
            {
                "timestamp_ms": item.timestamp_ms,
                "published_at_ms": item.published_at_ms,
                "value": str(item.value),
                "source": item.source,
            }
            for item in dataset.observations
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def validate_context(dataset: ContextDataset) -> ContextDataset:
    if not dataset.kind or not dataset.symbol or not dataset.frequency or not dataset.source:
        raise ValueError("context dataset metadata is incomplete")
    previous: ContextObservation | None = None
    for observation in dataset.observations:
        if (
            not observation.value.is_finite()
            or observation.published_at_ms < observation.timestamp_ms
        ):
            raise ValueError("context observation is invalid")
        if previous is not None and observation.timestamp_ms <= previous.timestamp_ms:
            raise ValueError("context observations must be strictly ordered")
        previous = observation
    return dataset


def context_to_payload(dataset: ContextDataset) -> dict[str, object]:
    validate_context(dataset)
    return {
        "format": "chappie-market-context/v1",
        "metadata": {
            "kind": dataset.kind,
            "symbol": dataset.symbol,
            "frequency": dataset.frequency,
            "source": dataset.source,
            "observations_sha256": context_digest(dataset),
        },
        "observations": [
            asdict(item) | {"value": str(item.value)} for item in dataset.observations
        ],
    }


def load_context_payload(payload: object) -> ContextDataset:
    if not isinstance(payload, dict) or payload.get("format") != "chappie-market-context/v1":
        raise ValueError("unsupported market context format")
    metadata, observations = payload.get("metadata"), payload.get("observations")
    if not isinstance(metadata, dict) or not isinstance(observations, list):
        raise TypeError("market context payload is incomplete")
    try:
        dataset = ContextDataset(
            kind=str(metadata["kind"]),
            symbol=str(metadata["symbol"]),
            frequency=str(metadata["frequency"]),
            source=str(metadata["source"]),
            observations=tuple(
                ContextObservation(
                    timestamp_ms=int(item["timestamp_ms"]),
                    published_at_ms=int(item["published_at_ms"]),
                    value=Decimal(str(item["value"])),
                    source=str(item["source"]),
                )
                for item in observations
            ),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("market context observations are malformed") from error
    validate_context(dataset)
    if metadata.get("observations_sha256") != context_digest(dataset):
        raise ValueError("market context digest does not match observations")
    return dataset


def asof_observation(
    observations: tuple[ContextObservation, ...], *, available_at_ms: int, max_age_ms: int
) -> ContextObservation | None:
    """Return a bounded as-of value; absence/staleness is explicit, never filled."""
    candidate: ContextObservation | None = None
    for item in observations:
        if item.published_at_ms > available_at_ms:
            break
        candidate = item
    if candidate is None or available_at_ms - candidate.published_at_ms > max_age_ms:
        return None
    return candidate


def completed_candle_asof(candles: tuple[Candle, ...], available_at_ms: int) -> Candle | None:
    """Only a candle whose close was known by T may join a BTC feature row."""
    candidate: Candle | None = None
    for candle in candles:
        if candle.close_time_ms > available_at_ms:
            break
        candidate = candle
    return candidate


def _return_bps(current: Decimal, previous: Decimal) -> Decimal:
    return (current - previous) / previous * Decimal(10_000)


def price_oi_state(
    price_now: Decimal, price_before: Decimal, oi_now: Decimal, oi_before: Decimal
) -> str:
    return (
        ("price_up" if price_now >= price_before else "price_down")
        + "_"
        + ("oi_up" if oi_now >= oi_before else "oi_down")
    )


def conditional_probability(
    labels_bps: Iterable[Decimal], *, threshold_bps: Decimal
) -> dict[str, str | int | None]:
    values = tuple(labels_bps)
    if not values:
        return {"count": 0, "probability": None}
    count = sum(value >= threshold_bps for value in values)
    return {"count": len(values), "probability": str(Decimal(count) / Decimal(len(values)))}


def conditional_study(
    rows: Iterable[tuple[int, str, Decimal]], *, threshold_bps: Decimal
) -> dict[str, object]:
    """Compare broad descriptive states with the exact same-period baseline."""
    materialized = tuple(rows)
    baseline = conditional_probability(
        (item[2] for item in materialized), threshold_bps=threshold_bps
    )
    groups: dict[str, list[Decimal]] = defaultdict(list)
    years: dict[str, dict[str, list[Decimal]]] = defaultdict(lambda: defaultdict(list))
    for timestamp, state, label in materialized:
        groups[state].append(label)
        year = str(datetime.fromtimestamp(timestamp / 1000, tz=UTC).year)
        years[year][state].append(label)
    baseline_probability = baseline["probability"]
    states = {}
    for state, labels in sorted(groups.items()):
        result = conditional_probability(labels, threshold_bps=threshold_bps)
        probability = result["probability"]
        states[state] = {
            **result,
            "baseline_probability": baseline_probability,
            "absolute_difference": (
                str(Decimal(str(probability)) - Decimal(str(baseline_probability)))
                if probability is not None and baseline_probability is not None
                else None
            ),
            "relative_lift": (
                str(Decimal(str(probability)) / Decimal(str(baseline_probability)) - 1)
                if probability is not None and baseline_probability not in (None, "0")
                else None
            ),
        }
    return {
        "threshold_bps": str(threshold_bps),
        "baseline": baseline,
        "states": states,
        "temporal_splits": {
            year: {
                state: conditional_probability(labels, threshold_bps=threshold_bps)
                for state, labels in sorted(group.items())
            }
            for year, group in sorted(years.items())
        },
    }
