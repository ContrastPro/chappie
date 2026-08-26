"""Public, bounded and reproducible market-history research inputs.

This module is deliberately independent of the runtime market provider: it is
used only by explicit research tools and never constructs an execution client.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx

from app.market.intervals import fixed_binance_spot_interval_ms
from app.market.models import Candle

PUBLIC_MARKET_DATA_URL = "https://data-api.binance.vision"
MAX_PAGE_SIZE = 1_000
MAX_REQUESTS = 100
MAX_CANDLES_PER_ACQUISITION = MAX_PAGE_SIZE * MAX_REQUESTS


@dataclass(frozen=True, slots=True)
class HistoricalRangeSegment:
    """One exact, downloader-safe portion of a requested historical range."""

    symbol: str
    interval: str
    start_ms: int
    end_ms: int
    candle_count: int


@dataclass(frozen=True, slots=True)
class SegmentedHistoricalAcquisitionV1:
    """Bounded acquisition provenance; the merged dataset remains canonical."""

    symbol: str
    interval: str
    requested_start_ms: int
    requested_end_ms: int
    segments: tuple[HistoricalRangeSegment, ...]
    segment_digests: tuple[str, ...]
    dataset: HistoricalDataset

    def provenance_payload(self) -> dict[str, object]:
        semantic = {
            "identity": "chappie_segmented_historical_acquisition/1.0.0",
            "acquisition_method": "SEGMENTED",
            "symbol": self.symbol,
            "interval": self.interval,
            "requested_start_ms": self.requested_start_ms,
            "requested_end_ms": self.requested_end_ms,
            "segment_count": len(self.segments),
            "segments": [
                {
                    "start_ms": segment.start_ms,
                    "end_ms": segment.end_ms,
                    "candle_count": segment.candle_count,
                    "candles_sha256": segment_digest,
                }
                for segment, segment_digest in zip(self.segments, self.segment_digests, strict=True)
            ],
            "merged_candles_sha256": self.dataset.metadata.candles_sha256,
        }
        return {
            **semantic,
            "provenance_digest": hashlib.sha256(
                json.dumps(semantic, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        }


@dataclass(frozen=True, slots=True)
class HistoricalDatasetMetadata:
    symbol: str
    interval: str
    requested_start_ms: int
    requested_end_ms: int
    actual_first_open_ms: int
    actual_last_open_ms: int
    candle_count: int
    downloaded_at_utc: str
    source: str
    candles_sha256: str


@dataclass(frozen=True, slots=True)
class HistoricalDataset:
    metadata: HistoricalDatasetMetadata
    candles: tuple[Candle, ...]


def utc_date_to_ms(value: str) -> int:
    """Parse an ISO date as a UTC midnight; an end date is exclusive."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("date must be ISO YYYY-MM-DD") from error
    if parsed.tzinfo is not None or parsed.time() != datetime.min.time():
        raise ValueError("date must be a timezone-free ISO calendar date")
    return int(parsed.replace(tzinfo=UTC).timestamp() * 1_000)


def canonical_candles_payload(candles: Sequence[Candle]) -> str:
    return json.dumps(
        [
            {
                "close": str(candle.close),
                "close_time_ms": candle.close_time_ms,
                "high": str(candle.high),
                "low": str(candle.low),
                "open": str(candle.open),
                "open_time_ms": candle.open_time_ms,
                "volume": str(candle.volume),
            }
            for candle in candles
        ],
        sort_keys=True,
        separators=(",", ":"),
    )


def candles_sha256(candles: Sequence[Candle]) -> str:
    return hashlib.sha256(canonical_candles_payload(candles).encode()).hexdigest()


def validate_candles(candles: Sequence[Candle], interval: str) -> tuple[Candle, ...]:
    interval_ms = fixed_binance_spot_interval_ms(interval)
    if not candles:
        raise ValueError("dataset contains no candles")
    result = tuple(candles)
    previous: Candle | None = None
    for candle in result:
        if any(
            not value.is_finite()
            for value in (candle.open, candle.high, candle.low, candle.close, candle.volume)
        ):
            raise ValueError("candle contains non-finite value")
        if (
            candle.open <= 0
            or candle.high <= 0
            or candle.low <= 0
            or candle.close <= 0
            or candle.volume < 0
        ):
            raise ValueError("candle contains invalid price or volume")
        if candle.low > min(candle.open, candle.close) or candle.high < max(
            candle.open, candle.close
        ):
            raise ValueError("candle OHLC bounds are invalid")
        if candle.close_time_ms != candle.open_time_ms + interval_ms - 1:
            raise ValueError(
                "candle close time is incompatible with interval "
                f"at open={candle.open_time_ms}: expected="
                f"{candle.open_time_ms + interval_ms - 1} actual={candle.close_time_ms}"
            )
        if previous is not None:
            if candle.open_time_ms == previous.open_time_ms:
                raise ValueError("duplicate candle open time")
            if candle.open_time_ms < previous.open_time_ms:
                raise ValueError("candles are out of order")
            if candle.open_time_ms != previous.open_time_ms + interval_ms:
                raise ValueError("gap detected between candles")
        previous = candle
    return result


def plan_historical_segments(
    *,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    max_candles: int = MAX_CANDLES_PER_ACQUISITION,
) -> tuple[HistoricalRangeSegment, ...]:
    """Plan exact contiguous ranges without changing downloader request limits."""
    normalized_symbol = symbol.strip().upper()
    if not normalized_symbol.isascii() or not normalized_symbol.isalnum():
        raise ValueError("symbol must be normalized ASCII letters and digits")
    interval_ms = fixed_binance_spot_interval_ms(interval)
    if start_ms < 0 or end_ms <= start_ms or (end_ms - start_ms) % interval_ms:
        raise ValueError("requested range must contain an exact number of interval candles")
    if (
        not isinstance(max_candles, int)
        or isinstance(max_candles, bool)
        or not 0 < max_candles <= MAX_CANDLES_PER_ACQUISITION
    ):
        raise ValueError("max candles must be a positive integer")
    result: list[HistoricalRangeSegment] = []
    cursor = start_ms
    while cursor < end_ms:
        count = min(max_candles, (end_ms - cursor) // interval_ms)
        next_cursor = cursor + count * interval_ms
        result.append(
            HistoricalRangeSegment(normalized_symbol, interval, cursor, next_cursor, count)
        )
        cursor = next_cursor
    return tuple(result)


def _validate_segment_dataset(segment: HistoricalRangeSegment, dataset: HistoricalDataset) -> None:
    metadata = dataset.metadata
    interval_ms = fixed_binance_spot_interval_ms(segment.interval)
    candles = validate_candles(dataset.candles, segment.interval)
    if (
        metadata.symbol != segment.symbol
        or metadata.interval != segment.interval
        or metadata.requested_start_ms != segment.start_ms
        or metadata.requested_end_ms != segment.end_ms
        or metadata.actual_first_open_ms != segment.start_ms
        or metadata.actual_last_open_ms + interval_ms != segment.end_ms
        or metadata.candle_count != segment.candle_count
        or len(candles) != segment.candle_count
        or candles_sha256(candles) != metadata.candles_sha256
    ):
        raise ValueError("downloaded segment does not match its planned range")


def merge_historical_datasets(segments: Sequence[HistoricalDataset]) -> HistoricalDataset:
    """Merge only exact compatible contiguous canonical datasets."""
    if not segments:
        raise ValueError("at least one historical dataset is required")
    result = tuple(segments)
    first = result[0].metadata
    interval_ms = fixed_binance_spot_interval_ms(first.interval)
    all_candles: list[Candle] = []
    previous_end: int | None = None
    for dataset in result:
        metadata = dataset.metadata
        candles = validate_candles(dataset.candles, metadata.interval)
        if (
            metadata.symbol != first.symbol
            or metadata.interval != first.interval
            or metadata.source != first.source
            or metadata.candle_count != len(candles)
            or candles_sha256(candles) != metadata.candles_sha256
        ):
            raise ValueError("historical dataset merge compatibility mismatch")
        if metadata.actual_first_open_ms != metadata.requested_start_ms:
            raise ValueError("historical dataset segment does not start at its requested boundary")
        if metadata.actual_last_open_ms + interval_ms != metadata.requested_end_ms:
            raise ValueError("historical dataset segment does not end at its requested boundary")
        if previous_end is not None and metadata.requested_start_ms != previous_end:
            raise ValueError("historical dataset segments are not contiguous")
        all_candles.extend(candles)
        previous_end = metadata.requested_end_ms
    merged = validate_candles(all_candles, first.interval)
    return HistoricalDataset(
        HistoricalDatasetMetadata(
            symbol=first.symbol,
            interval=first.interval,
            requested_start_ms=first.requested_start_ms,
            requested_end_ms=result[-1].metadata.requested_end_ms,
            actual_first_open_ms=merged[0].open_time_ms,
            actual_last_open_ms=merged[-1].open_time_ms,
            candle_count=len(merged),
            downloaded_at_utc=result[-1].metadata.downloaded_at_utc,
            source=first.source,
            candles_sha256=candles_sha256(merged),
        ),
        merged,
    )


def download_segmented(
    downloader: PublicKlineDownloader,
    *,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    max_candles: int = MAX_CANDLES_PER_ACQUISITION,
) -> SegmentedHistoricalAcquisitionV1:
    """Sequentially download bounded segments and publish no result on a failed segment."""
    if max_candles > MAX_CANDLES_PER_ACQUISITION:
        raise ValueError("segmented acquisition cannot exceed the downloader candle capacity")
    plan = plan_historical_segments(
        symbol=symbol,
        interval=interval,
        start_ms=start_ms,
        end_ms=end_ms,
        max_candles=max_candles,
    )
    downloaded: list[HistoricalDataset] = []
    for segment in plan:
        dataset = downloader.download(
            symbol=segment.symbol,
            interval=segment.interval,
            start_ms=segment.start_ms,
            end_ms=segment.end_ms,
        )
        _validate_segment_dataset(segment, dataset)
        downloaded.append(dataset)
    merged = merge_historical_datasets(downloaded)
    return SegmentedHistoricalAcquisitionV1(
        plan[0].symbol,
        interval,
        start_ms,
        end_ms,
        plan,
        tuple(item.metadata.candles_sha256 for item in downloaded),
        merged,
    )


def _parse_kline(raw: Any, index: int) -> Candle:
    if not isinstance(raw, list) or len(raw) < 7:
        raise ValueError(f"malformed Binance kline at index {index}")
    if not all(
        isinstance(raw[position], int) and not isinstance(raw[position], bool)
        for position in (0, 6)
    ):
        raise ValueError(f"malformed Binance kline timestamp at index {index}")
    try:
        values = [Decimal(raw[position]) for position in (1, 2, 3, 4, 5)]
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"malformed Binance kline decimal at index {index}") from error
    return Candle(
        open_time_ms=raw[0],
        close_time_ms=raw[6],
        open=values[0],
        high=values[1],
        low=values[2],
        close=values[3],
        volume=values[4],
    )


class PublicKlineDownloader:
    """Public GET-only kline downloader with a strict pagination bound."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def download(
        self, *, symbol: str, interval: str, start_ms: int, end_ms: int
    ) -> HistoricalDataset:
        normalized_symbol = symbol.strip().upper()
        if not normalized_symbol.isascii() or not normalized_symbol.isalnum():
            raise ValueError("symbol must be normalized ASCII letters and digits")
        interval_ms = fixed_binance_spot_interval_ms(interval)
        if start_ms < 0 or end_ms <= start_ms:
            raise ValueError("requested range is invalid")
        expected = (end_ms - start_ms + interval_ms - 1) // interval_ms
        if expected > MAX_REQUESTS * MAX_PAGE_SIZE:
            raise ValueError("requested range exceeds bounded downloader limit")
        all_candles: list[Candle] = []
        cursor = start_ms
        for _ in range(MAX_REQUESTS):
            if cursor >= end_ms:
                break
            response = self._client.get(
                "/api/v3/klines",
                params={
                    "symbol": normalized_symbol,
                    "interval": interval,
                    "startTime": str(cursor),
                    "endTime": str(end_ms - 1),
                    "limit": str(MAX_PAGE_SIZE),
                },
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise TypeError("Binance kline response must be a list")
            page = [_parse_kline(item, index) for index, item in enumerate(payload)]
            if not page:
                break
            all_candles.extend(page)
            next_cursor = page[-1].open_time_ms + interval_ms
            if next_cursor <= cursor:
                raise ValueError("pagination did not advance")
            cursor = next_cursor
            if len(page) < MAX_PAGE_SIZE:
                break
        else:
            raise ValueError("downloader request bound exhausted")
        candles = validate_candles(all_candles, interval)
        if candles[0].open_time_ms < start_ms or candles[-1].open_time_ms >= end_ms:
            raise ValueError("downloaded candles fall outside requested range")
        metadata = HistoricalDatasetMetadata(
            symbol=normalized_symbol,
            interval=interval,
            requested_start_ms=start_ms,
            requested_end_ms=end_ms,
            actual_first_open_ms=candles[0].open_time_ms,
            actual_last_open_ms=candles[-1].open_time_ms,
            candle_count=len(candles),
            downloaded_at_utc=datetime.now(UTC).isoformat(),
            source=PUBLIC_MARKET_DATA_URL,
            candles_sha256=candles_sha256(candles),
        )
        return HistoricalDataset(metadata, candles)


def dataset_to_json(dataset: HistoricalDataset) -> str:
    return (
        json.dumps(
            {
                "format": "chappie-historical-candles/v1",
                "metadata": asdict(dataset.metadata),
                "candles": json.loads(canonical_candles_payload(dataset.candles)),
            },
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )


def write_dataset_atomic(path: Path, dataset: HistoricalDataset) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dataset_to_json(dataset)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # A downloaded dataset is an immutable research input.  link() is an
        # atomic create-if-absent publication on the same filesystem.
        os.link(temporary, path)
        os.unlink(temporary)
    except FileExistsError:
        os.unlink(temporary)
        raise FileExistsError(f"research dataset already exists: {path}") from None
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def write_segmented_provenance_atomic(
    path: Path, acquisition: SegmentedHistoricalAcquisitionV1
) -> None:
    """Publish bounded acquisition provenance before its canonical final dataset."""
    payload = json.dumps(acquisition.provenance_payload(), sort_keys=True, indent=2) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
        os.unlink(temporary)
    except FileExistsError:
        os.unlink(temporary)
        raise FileExistsError(f"segmented acquisition provenance already exists: {path}") from None
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def load_dataset(path: Path) -> HistoricalDataset:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("format") != "chappie-historical-candles/v1":
        raise ValueError("unsupported historical dataset format")
    raw_metadata, raw_candles = payload.get("metadata"), payload.get("candles")
    if not isinstance(raw_metadata, dict) or not isinstance(raw_candles, list):
        raise TypeError("dataset metadata or candles are missing")
    try:
        metadata = HistoricalDatasetMetadata(**raw_metadata)
        candles = tuple(
            Candle(
                open_time_ms=item["open_time_ms"],
                close_time_ms=item["close_time_ms"],
                open=Decimal(item["open"]),
                high=Decimal(item["high"]),
                low=Decimal(item["low"]),
                close=Decimal(item["close"]),
                volume=Decimal(item["volume"]),
            )
            for item in raw_candles
        )
    except (KeyError, TypeError, InvalidOperation) as error:
        raise ValueError("dataset candle payload is malformed") from error
    validate_candles(candles, metadata.interval)
    if candles_sha256(candles) != metadata.candles_sha256:
        raise ValueError("dataset candle digest does not match metadata")
    if (
        metadata.candle_count != len(candles)
        or metadata.actual_first_open_ms != candles[0].open_time_ms
        or metadata.actual_last_open_ms != candles[-1].open_time_ms
    ):
        raise ValueError("dataset metadata does not match candles")
    return HistoricalDataset(metadata, candles)
