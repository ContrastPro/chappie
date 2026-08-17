"""Offline-only causal flow and positioning research primitives.

Nothing in this module is imported by the trading runtime.  It decodes public
Binance Vision archives into descriptive research observations and deliberately
keeps source absence and source gaps explicit.
"""

from __future__ import annotations

import hashlib
import json
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from itertools import pairwise

from app.market_intelligence import FUNDING_MAX_AGE_MS, ContextDataset, asof_observation

FIVE_MINUTES_MS = 5 * 60 * 1_000
ONE_MINUTE_MS = 60 * 1_000
METRICS_COLUMNS = (
    "create_time",
    "symbol",
    "sum_open_interest",
    "sum_open_interest_value",
    "count_toptrader_long_short_ratio",
    "sum_toptrader_long_short_ratio",
    "count_long_short_ratio",
    "sum_taker_long_short_vol_ratio",
)


@dataclass(frozen=True, slots=True)
class FuturesMetricsObservation:
    timestamp_ms: int
    symbol: str
    open_interest: Decimal
    open_interest_value: Decimal
    top_trader_long_short_ratio: Decimal | None
    global_long_short_ratio: Decimal | None
    taker_long_short_volume_ratio: Decimal | None


@dataclass(frozen=True, slots=True)
class MetricsDataset:
    symbol: str
    source: str
    observations: tuple[FuturesMetricsObservation, ...]
    source_row_count: int
    was_source_ordered: bool
    gaps_after_ms: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class SpotMinuteObservation:
    open_time_ms: int
    close_time_ms: int
    close: Decimal
    quote_volume: Decimal
    trade_count: int
    taker_buy_quote_volume: Decimal


@dataclass(frozen=True, slots=True)
class SpotFlowObservation:
    timestamp_ms: int
    close: Decimal
    quote_volume: Decimal
    taker_buy_quote_volume: Decimal
    taker_sell_quote_volume: Decimal
    taker_buy_quote_ratio: Decimal | None
    signed_taker_quote_imbalance: Decimal | None
    trade_count: int
    average_quote_per_trade: Decimal | None
    relative_quote_volume: Decimal | None


@dataclass(frozen=True, slots=True)
class FlowObservation:
    timestamp_ms: int
    symbol: str
    close: Decimal
    spot: SpotFlowObservation
    metrics: FuturesMetricsObservation | None
    funding: Decimal | None
    oi_change_5m: Decimal | None
    oi_change_15m: Decimal | None
    oi_change_1h: Decimal | None
    oi_change_5m_pct: Decimal | None
    price_oi_state: str | None
    positioning_state: str | None
    futures_taker_state: str | None


def _timestamp_ms(value: str) -> int:
    try:
        return int(
            datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC).timestamp() * 1_000
        )
    except ValueError as error:
        raise ValueError(f"invalid metrics create_time: {value!r}") from error


def _decimal(value: str, field: str) -> Decimal:
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"invalid {field}") from error
    if not result.is_finite():
        raise ValueError(f"invalid {field}")
    return result


def _optional_decimal(value: str, field: str) -> Decimal | None:
    return None if value == "" else _decimal(value, field)


def canonicalize_metrics_rows(
    rows: Iterable[Sequence[str]], *, symbol: str, source: str
) -> MetricsDataset:
    """Validate, de-duplicate reject, then stably sort public metrics rows.

    Binance Vision metrics files are not guaranteed to be in chronological row
    order.  Sorting is canonicalization, not interpolation or source repair.
    """
    materialized = tuple(tuple(row) for row in rows)
    if not materialized or tuple(materialized[0]) != METRICS_COLUMNS:
        raise ValueError("unexpected futures metrics header")
    normalized_symbol = symbol.upper()
    parsed: list[FuturesMetricsObservation] = []
    seen: set[int] = set()
    source_times: list[int] = []
    for row in materialized[1:]:
        if len(row) != len(METRICS_COLUMNS):
            raise ValueError("malformed futures metrics row")
        timestamp = _timestamp_ms(row[0])
        if row[1] != normalized_symbol:
            raise ValueError("futures metrics symbol mismatch")
        if timestamp in seen:
            raise ValueError("duplicate futures metrics timestamp")
        seen.add(timestamp)
        source_times.append(timestamp)
        parsed.append(
            FuturesMetricsObservation(
                timestamp,
                row[1],
                _decimal(row[2], "sum_open_interest"),
                _decimal(row[3], "sum_open_interest_value"),
                _optional_decimal(row[5], "sum_toptrader_long_short_ratio"),
                _optional_decimal(row[6], "count_long_short_ratio"),
                _optional_decimal(row[7], "sum_taker_long_short_vol_ratio"),
            )
        )
    if not parsed:
        raise ValueError("futures metrics archive contains no observations")
    ordered = tuple(sorted(parsed, key=lambda item: item.timestamp_ms))
    gaps = tuple(
        later.timestamp_ms
        for previous, later in pairwise(ordered)
        if later.timestamp_ms - previous.timestamp_ms != FIVE_MINUTES_MS
    )
    return MetricsDataset(
        normalized_symbol,
        source,
        ordered,
        len(parsed),
        all(later > previous for previous, later in pairwise(source_times)),
        gaps,
    )


def metrics_digest(dataset: MetricsDataset) -> str:
    payload = {
        "symbol": dataset.symbol,
        "source": dataset.source,
        "observations": [
            {
                "timestamp_ms": item.timestamp_ms,
                "symbol": item.symbol,
                "open_interest": str(item.open_interest),
                "open_interest_value": str(item.open_interest_value),
                "top_trader_long_short_ratio": str(item.top_trader_long_short_ratio)
                if item.top_trader_long_short_ratio is not None
                else None,
                "global_long_short_ratio": str(item.global_long_short_ratio)
                if item.global_long_short_ratio is not None
                else None,
                "taker_long_short_volume_ratio": str(item.taker_long_short_volume_ratio)
                if item.taker_long_short_volume_ratio is not None
                else None,
            }
            for item in dataset.observations
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def metrics_to_payload(dataset: MetricsDataset) -> dict[str, object]:
    return {
        "format": "chappie-usdm-metrics/v1",
        "symbol": dataset.symbol,
        "source": dataset.source,
        "source_row_count": dataset.source_row_count,
        "was_source_ordered": dataset.was_source_ordered,
        "gaps_after_ms": list(dataset.gaps_after_ms),
        "canonical_sha256": metrics_digest(dataset),
        "observations": json.loads(
            json.dumps([asdict(item) for item in dataset.observations], default=str)
        ),
    }


def load_metrics_payload(payload: object) -> MetricsDataset:
    if not isinstance(payload, dict) or payload.get("format") != "chappie-usdm-metrics/v1":
        raise ValueError("unsupported futures metrics dataset")
    raw_observations = payload.get("observations")
    if not isinstance(raw_observations, list):
        raise TypeError("futures metrics observations are missing")
    try:
        observations = tuple(
            FuturesMetricsObservation(
                int(item["timestamp_ms"]),
                str(item["symbol"]),
                Decimal(str(item["open_interest"])),
                Decimal(str(item["open_interest_value"])),
                Decimal(str(item["top_trader_long_short_ratio"]))
                if item["top_trader_long_short_ratio"] is not None
                else None,
                Decimal(str(item["global_long_short_ratio"]))
                if item["global_long_short_ratio"] is not None
                else None,
                Decimal(str(item["taker_long_short_volume_ratio"]))
                if item["taker_long_short_volume_ratio"] is not None
                else None,
            )
            for item in raw_observations
        )
        dataset = MetricsDataset(
            str(payload["symbol"]),
            str(payload["source"]),
            observations,
            int(payload["source_row_count"]),
            bool(payload["was_source_ordered"]),
            tuple(int(item) for item in payload["gaps_after_ms"]),
        )
    except (KeyError, TypeError, ValueError, InvalidOperation) as error:
        raise ValueError("malformed futures metrics dataset") from error
    if tuple(sorted(item.timestamp_ms for item in dataset.observations)) != tuple(
        item.timestamp_ms for item in dataset.observations
    ) or len({item.timestamp_ms for item in dataset.observations}) != len(dataset.observations):
        raise ValueError("futures metrics observations are not canonical")
    if metrics_digest(dataset) != payload.get("canonical_sha256"):
        raise ValueError("futures metrics digest does not match observations")
    return dataset


def parse_spot_minute_rows(rows: Iterable[Sequence[str]]) -> tuple[SpotMinuteObservation, ...]:
    """Parse completed Binance spot 1m kline rows without inferring trade side."""
    result: list[SpotMinuteObservation] = []
    previous: int | None = None
    for row in rows:
        if len(row) < 11:
            raise ValueError("malformed spot 1m kline row")
        open_ms, close_ms = int(row[0]), int(row[6])
        # Vision switched its spot archive to microsecond timestamps in 2025.
        if open_ms >= 100_000_000_000_000:
            if open_ms % 1_000 or close_ms % 1_000 != 999:
                raise ValueError("unsupported spot microsecond timestamp precision")
            open_ms //= 1_000
            close_ms //= 1_000
        if close_ms != open_ms + ONE_MINUTE_MS - 1:
            raise ValueError("spot 1m close time is invalid")
        if previous is not None and open_ms != previous + ONE_MINUTE_MS:
            raise ValueError("spot 1m candles contain a gap")
        quote_volume = _decimal(row[7], "quote_volume")
        taker_buy = _decimal(row[10], "taker_buy_quote_volume")
        if quote_volume < 0 or taker_buy < 0 or taker_buy > quote_volume:
            raise ValueError("spot taker quote volume is invalid")
        count = int(row[8])
        if count < 0:
            raise ValueError("spot trade count is invalid")
        result.append(
            SpotMinuteObservation(
                open_ms, close_ms, _decimal(row[4], "close"), quote_volume, count, taker_buy
            )
        )
        previous = open_ms
    if not result:
        raise ValueError("spot 1m dataset contains no rows")
    return tuple(result)


def parse_spot_minute_rows_excluding_invalid(
    rows: Iterable[Sequence[str]],
) -> tuple[tuple[SpotMinuteObservation, ...], tuple[dict[str, object], ...]]:
    """Exclude, but never repair, malformed public source candles.

    The strict parser above remains the normal invariant boundary.  Long public
    archives can contain isolated malformed source rows; research ingestion
    records them as anomalies, keeps valid observations canonical, and leaves a
    gap rather than inventing a candle.
    """
    valid: list[SpotMinuteObservation] = []
    anomalies: list[dict[str, object]] = []
    for index, row in enumerate(rows):
        try:
            if len(row) < 11:
                raise ValueError("malformed spot 1m kline row")
            open_ms, close_ms = int(row[0]), int(row[6])
            if open_ms >= 100_000_000_000_000:
                if open_ms % 1_000 or close_ms % 1_000 != 999:
                    raise ValueError("unsupported spot microsecond timestamp precision")
                open_ms //= 1_000
                close_ms //= 1_000
            if close_ms != open_ms + ONE_MINUTE_MS - 1:
                raise ValueError("spot 1m close time is invalid")
            quote_volume = _decimal(row[7], "quote_volume")
            taker_buy = _decimal(row[10], "taker_buy_quote_volume")
            if quote_volume < 0 or taker_buy < 0 or taker_buy > quote_volume:
                raise ValueError("spot taker quote volume is invalid")
            trade_count = int(row[8])
            if trade_count < 0:
                raise ValueError("spot trade count is invalid")
            valid.append(
                SpotMinuteObservation(
                    open_ms,
                    close_ms,
                    _decimal(row[4], "close"),
                    quote_volume,
                    trade_count,
                    taker_buy,
                )
            )
        except (IndexError, InvalidOperation, ValueError) as error:
            anomalies.append(
                {
                    "type": "INVALID_SPOT_1M_SOURCE_ROW",
                    "row_index": index,
                    "open_time_raw": row[0] if row else None,
                    "reason": str(error),
                }
            )
    by_timestamp: dict[int, SpotMinuteObservation] = {}
    duplicate_timestamps: set[int] = set()
    for observation in valid:
        if observation.open_time_ms in by_timestamp:
            duplicate_timestamps.add(observation.open_time_ms)
        else:
            by_timestamp[observation.open_time_ms] = observation
    for timestamp in sorted(duplicate_timestamps):
        by_timestamp.pop(timestamp, None)
        anomalies.append({"type": "DUPLICATE_SPOT_1M_SOURCE_TIMESTAMP", "timestamp_ms": timestamp})
    observations = tuple(by_timestamp[timestamp] for timestamp in sorted(by_timestamp))
    if not observations:
        raise ValueError("spot 1m dataset has no valid observations")
    return observations, tuple(anomalies)


def aggregate_completed_spot_5m(
    candles: Sequence[SpotMinuteObservation], *, volume_lookback: int = 12
) -> tuple[SpotFlowObservation, ...]:
    """Aggregate only complete, contiguous 1m components into causal 5m flow."""
    if volume_lookback < 1:
        raise ValueError("volume lookback must be positive")
    grouped: list[tuple[SpotMinuteObservation, ...]] = []
    current: list[SpotMinuteObservation] = []
    for candle in candles:
        if candle.open_time_ms % FIVE_MINUTES_MS == 0 and current:
            current = []
        current.append(candle)
        if len(current) == 5:
            expected = current[0].open_time_ms
            if all(
                item.open_time_ms == expected + offset * ONE_MINUTE_MS
                for offset, item in enumerate(current)
            ):
                grouped.append(tuple(current))
            current = []
    raw: list[SpotFlowObservation] = []
    for group in grouped:
        quote = sum((item.quote_volume for item in group), Decimal(0))
        buy = sum((item.taker_buy_quote_volume for item in group), Decimal(0))
        trades = sum(item.trade_count for item in group)
        ratio = buy / quote if quote else None
        raw.append(
            SpotFlowObservation(
                group[-1].close_time_ms,
                group[-1].close,
                quote,
                buy,
                quote - buy,
                ratio,
                (ratio * 2 - 1) if ratio is not None else None,
                trades,
                quote / Decimal(trades) if trades else None,
                None,
            )
        )
    result: list[SpotFlowObservation] = []
    contiguous_prior: list[SpotFlowObservation] = []
    for item in raw:
        if result and item.timestamp_ms != result[-1].timestamp_ms + FIVE_MINUTES_MS:
            contiguous_prior = []
        baseline = contiguous_prior[-volume_lookback:]
        average = (
            sum((value.quote_volume for value in baseline), Decimal(0)) / Decimal(len(baseline))
            if baseline
            else None
        )
        result.append(
            replace(
                item,
                relative_quote_volume=item.quote_volume / average if average else None,
            )
        )
        contiguous_prior.append(item)
    return tuple(result)


def positioning_bucket(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if value <= Decimal("0.67"):
        return "EXTREME_SHORT"
    if value < Decimal("0.90"):
        return "SHORT_BIASED"
    if value < Decimal("1.10"):
        return "NEUTRAL"
    if value < Decimal("1.50"):
        return "LONG_BIASED"
    return "EXTREME_LONG"


def _taker_state(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if value > Decimal("1.10"):
        return "FUTURES_BUYING"
    if value < Decimal("0.90"):
        return "FUTURES_SELLING"
    return "FUTURES_NEUTRAL"


def _price_oi_state(
    close: Decimal, prior_close: Decimal, oi: Decimal, prior_oi: Decimal
) -> str | None:
    if close <= 0 or prior_close <= 0 or oi <= 0 or prior_oi <= 0:
        return None
    price_bps = (close / prior_close - 1) * Decimal(10_000)
    oi_pct = (oi / prior_oi - 1) * Decimal(100)
    if abs(price_bps) < Decimal(5) or abs(oi_pct) < Decimal("0.10"):
        return "FLAT_OR_MIXED"
    return f"PRICE_{'UP' if price_bps > 0 else 'DOWN'}_OI_{'UP' if oi_pct > 0 else 'DOWN'}"


def build_flow_observations(
    *,
    symbol: str,
    spot: Sequence[SpotFlowObservation],
    metrics: MetricsDataset,
    funding: ContextDataset | None,
) -> tuple[FlowObservation, ...]:
    """Create an as-of 5m timeline; unavailable metrics are never filled."""
    if symbol != metrics.symbol:
        raise ValueError("spot and metrics symbols differ")
    metric_times = tuple(item.timestamp_ms for item in metrics.observations)
    by_time = {item.timestamp_ms: item for item in metrics.observations}
    result: list[FlowObservation] = []
    for index, row in enumerate(spot):
        candidate_index = bisect_right(metric_times, row.timestamp_ms) - 1
        item = metrics.observations[candidate_index] if candidate_index >= 0 else None
        if item is not None and row.timestamp_ms - item.timestamp_ms > FIVE_MINUTES_MS:
            item = None
        funding_item = (
            asof_observation(
                funding.observations,
                available_at_ms=row.timestamp_ms,
                max_age_ms=FUNDING_MAX_AGE_MS,
            )
            if funding is not None
            else None
        )
        oi_5 = oi_15 = oi_1h = oi_pct = None
        state = None
        if item is not None and index:
            prior = by_time.get(item.timestamp_ms - FIVE_MINUTES_MS)
            prior_15 = by_time.get(item.timestamp_ms - 3 * FIVE_MINUTES_MS)
            prior_1h = by_time.get(item.timestamp_ms - 12 * FIVE_MINUTES_MS)
            if prior is not None:
                oi_5 = item.open_interest - prior.open_interest
                oi_pct = oi_5 / prior.open_interest if prior.open_interest else None
                state = _price_oi_state(
                    row.close, spot[index - 1].close, item.open_interest, prior.open_interest
                )
            if prior_15 is not None:
                oi_15 = item.open_interest - prior_15.open_interest
            if prior_1h is not None:
                oi_1h = item.open_interest - prior_1h.open_interest
        result.append(
            FlowObservation(
                row.timestamp_ms,
                symbol,
                row.close,
                row,
                item,
                funding_item.value if funding_item else None,
                oi_5,
                oi_15,
                oi_1h,
                oi_pct,
                state,
                positioning_bucket(item.global_long_short_ratio) if item else None,
                _taker_state(item.taker_long_short_volume_ratio) if item else None,
            )
        )
    return tuple(result)


def flow_state(row: FlowObservation) -> str | None:
    """Small interpretable conjunction set; labels describe, never trade."""
    if row.price_oi_state is None or row.spot.signed_taker_quote_imbalance is None:
        return None
    spot_buying = row.spot.signed_taker_quote_imbalance > Decimal("0.05")
    future_buying = row.futures_taker_state == "FUTURES_BUYING"
    future_selling = row.futures_taker_state == "FUTURES_SELLING"
    if row.price_oi_state == "PRICE_UP_OI_UP" and spot_buying and future_buying:
        return "PRICE_UP_OI_UP_FLOW_AGREE"
    if row.price_oi_state == "PRICE_UP_OI_DOWN":
        return "PRICE_UP_OI_DOWN"
    if row.price_oi_state == "PRICE_DOWN_OI_UP":
        return "PRICE_DOWN_OI_UP"
    if spot_buying and future_selling:
        return "SPOT_FUTURES_FLOW_DIVERGENCE"
    if spot_buying and future_buying:
        return "SPOT_FUTURES_FLOW_AGREE"
    return row.price_oi_state


def cross_asset_flow_state(btc: FlowObservation, eth: FlowObservation) -> str | None:
    if btc.timestamp_ms != eth.timestamp_ms:
        raise ValueError("cross-asset rows require exact timestamp alignment")
    if (
        btc.spot.signed_taker_quote_imbalance is None
        or eth.spot.signed_taker_quote_imbalance is None
    ):
        return None
    btc_buy = (
        btc.spot.signed_taker_quote_imbalance > Decimal("0.05")
        and btc.oi_change_5m
        and btc.oi_change_5m > 0
    )
    eth_buy = (
        eth.spot.signed_taker_quote_imbalance > Decimal("0.05")
        and eth.oi_change_5m
        and eth.oi_change_5m > 0
    )
    btc_sell = (
        btc.spot.signed_taker_quote_imbalance < Decimal("-0.05")
        and btc.oi_change_5m
        and btc.oi_change_5m < 0
    )
    eth_sell = (
        eth.spot.signed_taker_quote_imbalance < Decimal("-0.05")
        and eth.oi_change_5m
        and eth.oi_change_5m < 0
    )
    if btc_buy and eth_buy:
        return "BROAD_CRYPTO_INFLOW_PROXY"
    if btc_sell and eth_sell:
        return "BROAD_CRYPTO_OUTFLOW_PROXY"
    if eth_buy and not btc_buy:
        return "ETH_FLOW_LEADERSHIP"
    if btc_buy and not eth_buy:
        return "BTC_FLOW_LEADERSHIP"
    return "MIXED_FLOW"


def future_return_bps(
    rows: Sequence[FlowObservation], index: int, horizon_bars: int
) -> Decimal | None:
    future_index = index + horizon_bars
    if future_index >= len(rows):
        return None
    if rows[future_index].timestamp_ms != rows[index].timestamp_ms + horizon_bars * FIVE_MINUTES_MS:
        return None
    return (rows[future_index].close / rows[index].close - 1) * Decimal(10_000)


def conditional_lift(
    labels: Iterable[tuple[int, str, Decimal]], *, threshold_bps: Decimal
) -> dict[str, object]:
    materialized = tuple(labels)
    outcomes = tuple(item[2] for item in materialized)
    baseline = (
        Decimal(sum(value >= threshold_bps for value in outcomes)) / Decimal(len(outcomes))
        if outcomes
        else None
    )
    groups: dict[str, list[Decimal]] = defaultdict(list)
    years: dict[str, dict[str, list[Decimal]]] = defaultdict(lambda: defaultdict(list))
    for timestamp, state, value in materialized:
        groups[state].append(value)
        years[str(datetime.fromtimestamp(timestamp / 1_000, tz=UTC).year)][state].append(value)

    def _summary(values: Sequence[Decimal]) -> dict[str, object]:
        probability = (
            Decimal(sum(value >= threshold_bps for value in values)) / Decimal(len(values))
            if values
            else None
        )
        return {
            "sample_size": len(values),
            "conditional_probability": str(probability) if probability is not None else None,
            "baseline_probability": str(baseline) if baseline is not None else None,
            "absolute_lift": str(probability - baseline)
            if probability is not None and baseline is not None
            else None,
            "relative_lift": str(probability / baseline - 1)
            if probability is not None and baseline not in (None, Decimal(0))
            else None,
        }

    return {
        "threshold_bps": str(threshold_bps),
        "states": {state: _summary(values) for state, values in sorted(groups.items())},
        "years": {
            year: {state: _summary(values) for state, values in sorted(group.items())}
            for year, group in sorted(years.items())
        },
    }


def flow_digest(rows: Sequence[FlowObservation]) -> str:
    payload = [
        {
            "timestamp_ms": row.timestamp_ms,
            "symbol": row.symbol,
            "close": str(row.close),
            "spot": asdict(row.spot),
            "metrics": asdict(row.metrics) if row.metrics else None,
            "funding": str(row.funding) if row.funding is not None else None,
            "price_oi_state": row.price_oi_state,
        }
        for row in rows
    ]
    return hashlib.sha256(
        json.dumps(payload, default=str, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def flow_to_payload(rows: Sequence[FlowObservation]) -> dict[str, object]:
    """Serialize a derived research dataset with decimal values as text."""
    return {
        "format": "chappie-flow-positioning/v1",
        "digest": flow_digest(rows),
        "rows": json.loads(json.dumps([asdict(row) for row in rows], default=str)),
    }


def load_flow_payload(payload: object) -> tuple[FlowObservation, ...]:
    if not isinstance(payload, dict) or payload.get("format") != "chappie-flow-positioning/v1":
        raise ValueError("unsupported flow positioning dataset")
    raw_rows = payload.get("rows")
    if not isinstance(raw_rows, list):
        raise TypeError("flow positioning dataset has no rows")
    result: list[FlowObservation] = []
    for raw in raw_rows:
        if not isinstance(raw, dict) or not isinstance(raw.get("spot"), dict):
            raise TypeError("malformed flow positioning row")
        raw_spot = raw["spot"]
        raw_metrics = raw.get("metrics")
        try:
            spot = SpotFlowObservation(
                int(raw_spot["timestamp_ms"]),
                Decimal(str(raw_spot["close"])),
                Decimal(str(raw_spot["quote_volume"])),
                Decimal(str(raw_spot["taker_buy_quote_volume"])),
                Decimal(str(raw_spot["taker_sell_quote_volume"])),
                Decimal(str(raw_spot["taker_buy_quote_ratio"]))
                if raw_spot["taker_buy_quote_ratio"] is not None
                else None,
                Decimal(str(raw_spot["signed_taker_quote_imbalance"]))
                if raw_spot["signed_taker_quote_imbalance"] is not None
                else None,
                int(raw_spot["trade_count"]),
                Decimal(str(raw_spot["average_quote_per_trade"]))
                if raw_spot["average_quote_per_trade"] is not None
                else None,
                Decimal(str(raw_spot["relative_quote_volume"]))
                if raw_spot["relative_quote_volume"] is not None
                else None,
            )
            metrics = (
                FuturesMetricsObservation(
                    int(raw_metrics["timestamp_ms"]),
                    str(raw_metrics["symbol"]),
                    Decimal(str(raw_metrics["open_interest"])),
                    Decimal(str(raw_metrics["open_interest_value"])),
                    Decimal(str(raw_metrics["top_trader_long_short_ratio"]))
                    if raw_metrics["top_trader_long_short_ratio"] is not None
                    else None,
                    Decimal(str(raw_metrics["global_long_short_ratio"]))
                    if raw_metrics["global_long_short_ratio"] is not None
                    else None,
                    Decimal(str(raw_metrics["taker_long_short_volume_ratio"]))
                    if raw_metrics["taker_long_short_volume_ratio"] is not None
                    else None,
                )
                if isinstance(raw_metrics, dict)
                else None
            )
            result.append(
                FlowObservation(
                    int(raw["timestamp_ms"]),
                    str(raw["symbol"]),
                    Decimal(str(raw["close"])),
                    spot,
                    metrics,
                    Decimal(str(raw["funding"])) if raw.get("funding") is not None else None,
                    Decimal(str(raw["oi_change_5m"]))
                    if raw.get("oi_change_5m") is not None
                    else None,
                    Decimal(str(raw["oi_change_15m"]))
                    if raw.get("oi_change_15m") is not None
                    else None,
                    Decimal(str(raw["oi_change_1h"]))
                    if raw.get("oi_change_1h") is not None
                    else None,
                    Decimal(str(raw["oi_change_5m_pct"]))
                    if raw.get("oi_change_5m_pct") is not None
                    else None,
                    str(raw["price_oi_state"]) if raw.get("price_oi_state") is not None else None,
                    str(raw["positioning_state"])
                    if raw.get("positioning_state") is not None
                    else None,
                    str(raw["futures_taker_state"])
                    if raw.get("futures_taker_state") is not None
                    else None,
                )
            )
        except (KeyError, TypeError, ValueError, InvalidOperation) as error:
            raise ValueError("malformed flow positioning row") from error
    rows = tuple(result)
    if flow_digest(rows) != payload.get("digest"):
        raise ValueError("flow positioning digest does not match rows")
    return rows
