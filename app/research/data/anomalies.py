"""Offline-only Binance historical-source anomaly classification and segmentation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation

from app.market.intervals import fixed_binance_spot_interval_ms
from app.market.models import Candle
from app.research.strategy.history import validate_candles

QUALITY_MAX_EXCLUDED_PERCENT = Decimal("0.10")
QUALITY_MAX_DISCONTINUITIES_PER_10K = Decimal(5)


@dataclass(frozen=True, slots=True)
class SourceAnomaly:
    source: str
    symbol: str
    interval: str
    timestamp_ms: int | None
    anomaly_type: str
    observed: dict[str, str | int | None]


@dataclass(frozen=True, slots=True)
class SegmentationResult:
    segments: tuple[tuple[Candle, ...], ...]
    anomalies: tuple[SourceAnomaly, ...]
    requested_rows: int


def _classify_validation_error(message: str) -> str:
    if "close time" in message:
        return "NONCANONICAL_CLOSE_TIME"
    if "duplicate" in message:
        return "DUPLICATE_CANDLE"
    if "out of order" in message:
        return "OUT_OF_ORDER"
    if "volume" in message:
        return "INVALID_VOLUME"
    return "MALFORMED_OHLC"


def split_historical_rows(
    rows: tuple[list[str], ...], *, source: str, symbol: str, interval: str
) -> SegmentationResult:
    """Exclude anomalous rows unchanged and split strict segments at every discontinuity."""
    interval_ms = fixed_binance_spot_interval_ms(interval)
    anomalies: list[SourceAnomaly] = []
    segments: list[tuple[Candle, ...]] = []
    current: list[Candle] = []
    previous_open: int | None = None
    for row in rows:
        timestamp: int | None = None
        try:
            if len(row) < 7:
                raise ValueError("malformed row")
            timestamp = int(row[0])
            close_timestamp = int(row[6])
            # Binance Vision switched historical CSV timestamp representation
            # from milliseconds to microseconds. This is transport decoding,
            # not a price/volume/candle repair: both raw timestamps must map
            # exactly to the canonical millisecond boundary convention.
            if timestamp >= 100_000_000_000_000:
                if timestamp % 1_000 or close_timestamp % 1_000 != 999:
                    raise ValueError("unsupported microsecond timestamp precision")
                timestamp //= 1_000
                close_timestamp //= 1_000
            candle = Candle(
                open_time_ms=timestamp,
                open=Decimal(row[1]),
                high=Decimal(row[2]),
                low=Decimal(row[3]),
                close=Decimal(row[4]),
                volume=Decimal(row[5]),
                close_time_ms=close_timestamp,
            )
            validate_candles((candle,), interval)
        except (InvalidOperation, ValueError, TypeError) as error:
            anomalies.append(
                SourceAnomaly(
                    source,
                    symbol,
                    interval,
                    timestamp,
                    _classify_validation_error(str(error)),
                    {"error": str(error), "row_length": len(row)},
                )
            )
            if current:
                segments.append(tuple(current))
                current = []
            previous_open = timestamp
            continue
        if previous_open is not None and candle.open_time_ms != previous_open + interval_ms:
            anomalies.append(
                SourceAnomaly(
                    source,
                    symbol,
                    interval,
                    candle.open_time_ms,
                    "MISSING_CANDLE_GAP",
                    {
                        "expected_open_ms": previous_open + interval_ms,
                        "observed_open_ms": candle.open_time_ms,
                    },
                )
            )
            if current:
                segments.append(tuple(current))
                current = []
        current.append(candle)
        previous_open = candle.open_time_ms
    if current:
        segments.append(tuple(current))
    # This assertion guarantees every emitted segment retains the live-grade contract.
    for segment in segments:
        validate_candles(segment, interval)
    return SegmentationResult(tuple(segments), tuple(anomalies), len(rows))


def anomaly_ledger_payload(result: SegmentationResult) -> dict[str, object]:
    accepted = sum(len(segment) for segment in result.segments)
    counts: dict[str, int] = {}
    for anomaly in result.anomalies:
        counts[anomaly.anomaly_type] = counts.get(anomaly.anomaly_type, 0) + 1
    excluded = result.requested_rows - accepted
    payload: dict[str, object] = {
        "format": "chappie-historical-anomaly-ledger/v1",
        "requested_candles": result.requested_rows,
        "accepted_candles": accepted,
        "excluded_candles": excluded,
        "discontinuities": len(result.anomalies),
        "excluded_percent": str(Decimal(excluded) / result.requested_rows * 100)
        if result.requested_rows
        else None,
        "anomaly_counts": dict(sorted(counts.items())),
        "anomalies": [asdict(item) for item in result.anomalies],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    payload["ledger_sha256"] = hashlib.sha256(encoded).hexdigest()
    return payload


def quality_status(result: SegmentationResult) -> str:
    accepted = sum(len(segment) for segment in result.segments)
    excluded = result.requested_rows - accepted
    if not result.requested_rows or not accepted:
        return "RESEARCH_DATA_QUALITY_INSUFFICIENT"
    if Decimal(excluded) / result.requested_rows * 100 > QUALITY_MAX_EXCLUDED_PERCENT:
        return "RESEARCH_DATA_QUALITY_INSUFFICIENT"
    if Decimal(len(result.anomalies)) / accepted * 10_000 > QUALITY_MAX_DISCONTINUITIES_PER_10K:
        return "RESEARCH_DATA_QUALITY_INSUFFICIENT"
    return "RESEARCH_DATA_QUALITY_ACCEPTED"
