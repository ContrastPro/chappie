from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum

from app.market.exceptions import (
    CandleSequenceValidationError,
    MarketCacheBatchError,
    MarketCacheClosedCandleUpdateError,
    MarketCacheConfigurationError,
    MarketCacheDuplicateConflictError,
    MarketCacheGapError,
    MarketCacheInvalidOpenCandleUpdateError,
    MarketCacheOutOfOrderError,
    MarketCacheReadError,
    MarketStreamKeyError,
)
from app.market.models import Candle, CandleSnapshot
from app.market.validation import validate_candle_sequence

DEFAULT_CAPACITY_PER_STREAM = 1000


@dataclass(frozen=True, slots=True)
class MarketStreamKey:
    symbol: str
    interval: str

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol:
            raise MarketStreamKeyError("Market stream symbol must be a non-empty string")
        if self.symbol != self.symbol.strip().upper():
            raise MarketStreamKeyError("Market stream symbol must be normalized")
        if not self.symbol.isascii() or not self.symbol.isalnum():
            raise MarketStreamKeyError(
                "Market stream symbol must contain only ASCII letters and digits"
            )
        if not isinstance(self.interval, str) or not self.interval:
            raise MarketStreamKeyError("Market stream interval must be a non-empty string")
        if self.interval != self.interval.strip():
            raise MarketStreamKeyError("Market stream interval must be normalized")


class CandleUpsertResult(Enum):
    INSERTED = "inserted"
    UPDATED = "updated"
    UNCHANGED = "unchanged"


@dataclass(frozen=True, slots=True)
class BatchUpsertResult:
    results: tuple[CandleUpsertResult, ...]
    evicted_count: int
    changes: tuple["MarketCacheChange", ...] = field(default=(), compare=False)


@dataclass(frozen=True, slots=True)
class MarketCacheChange:
    result: CandleUpsertResult
    previous: CandleSnapshot | None
    current: CandleSnapshot
    evicted: tuple[CandleSnapshot, ...]
    observed_at_ms: int


class InMemoryMarketCache:
    """Bounded in-memory candle cache for one synchronous writer.

    This class is intentionally not thread-safe. It performs no I/O and uses
    only caller-provided timestamps, making its behavior deterministic.
    """

    def __init__(self, capacity: int = DEFAULT_CAPACITY_PER_STREAM) -> None:
        if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity <= 0:
            raise MarketCacheConfigurationError("Market cache capacity must be positive")

        self._capacity = capacity
        self._streams: dict[MarketStreamKey, list[Candle]] = {}

    @property
    def capacity(self) -> int:
        return self._capacity

    def upsert_candle(
        self,
        key: MarketStreamKey,
        candle: Candle,
        observed_at_ms: int,
    ) -> CandleUpsertResult:
        self._validate_timestamp(observed_at_ms, "observed_at_ms")
        self._validate_input_candles([candle], key, observed_at_ms)
        result, _, _, _ = self._upsert_into(
            self._streams,
            key,
            candle,
            observed_at_ms,
        )
        return result

    def upsert_candles(
        self,
        key: MarketStreamKey,
        candles: Sequence[Candle],
        observed_at_ms: int,
    ) -> BatchUpsertResult:
        self._validate_timestamp(observed_at_ms, "observed_at_ms")

        try:
            batch = tuple(candles)
        except TypeError as error:
            raise MarketCacheBatchError("Candle batch must be a sequence") from error

        if not batch:
            return BatchUpsertResult(results=(), evicted_count=0)

        if any(not isinstance(candle, Candle) for candle in batch):
            raise MarketCacheBatchError("Candle batch must contain only Candle values")

        try:
            self._validate_input_candles(batch, key, observed_at_ms)
        except (TypeError, ValueError) as error:
            raise MarketCacheBatchError("Candle batch validation failed") from error

        staged_streams = {
            stream_key: [self._copy_candle(item) for item in stream]
            for stream_key, stream in self._streams.items()
        }
        results: list[CandleUpsertResult] = []
        changes: list[MarketCacheChange] = []
        evicted_count = 0

        try:
            for candle in batch:
                result, evicted, previous, current = self._upsert_into(
                    staged_streams,
                    key,
                    candle,
                    observed_at_ms,
                )
                results.append(result)
                evicted_count += len(evicted)
                changes.append(
                    MarketCacheChange(
                        result=result,
                        previous=(
                            None
                            if previous is None
                            else CandleSnapshot.from_candle(previous)
                        ),
                        current=CandleSnapshot.from_candle(current),
                        evicted=tuple(CandleSnapshot.from_candle(item) for item in evicted),
                        observed_at_ms=observed_at_ms,
                    )
                )
        except (TypeError, ValueError) as error:
            raise MarketCacheBatchError("Candle batch upsert failed") from error

        self._streams = staged_streams
        return BatchUpsertResult(
            results=tuple(results),
            evicted_count=evicted_count,
            changes=tuple(changes),
        )

    def get_snapshot(
        self,
        key: MarketStreamKey,
        limit: int | None = None,
    ) -> tuple[Candle, ...]:
        if limit is not None and (
            not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0
        ):
            raise MarketCacheReadError("Snapshot limit must be positive")

        stream = self._streams.get(key, [])
        selected = stream if limit is None else stream[-limit:]
        return tuple(self._copy_candle(candle) for candle in selected)

    def get_latest_candle(self, key: MarketStreamKey) -> Candle | None:
        stream = self._streams.get(key)
        if not stream:
            return None
        return self._copy_candle(stream[-1])

    def get_latest_closed_candle(
        self,
        key: MarketStreamKey,
        as_of_ms: int,
    ) -> Candle | None:
        self._validate_timestamp(as_of_ms, "as_of_ms")
        stream = self._streams.get(key, [])

        for candle in reversed(stream):
            if as_of_ms > candle.close_time_ms:
                return self._copy_candle(candle)

        return None

    def _upsert_into(
        self,
        streams: dict[MarketStreamKey, list[Candle]],
        key: MarketStreamKey,
        candle: Candle,
        observed_at_ms: int,
    ) -> tuple[CandleUpsertResult, tuple[Candle, ...], Candle | None, Candle]:
        stream = streams.setdefault(key, [])

        if not stream:
            current = self._copy_candle(candle)
            stream.append(current)
            return CandleUpsertResult.INSERTED, (), None, current

        latest = stream[-1]

        if candle.open_time_ms == latest.open_time_ms:
            if candle == latest:
                return CandleUpsertResult.UNCHANGED, (), latest, latest
            if observed_at_ms > latest.close_time_ms:
                raise MarketCacheClosedCandleUpdateError(
                    "A closed candle cannot be updated"
                )
            self._validate_open_candle_update(latest, candle)
            current = self._copy_candle(candle)
            stream[-1] = current
            return CandleUpsertResult.UPDATED, (), latest, current

        if candle.open_time_ms < latest.open_time_ms:
            if any(item.open_time_ms == candle.open_time_ms for item in stream[:-1]):
                raise MarketCacheDuplicateConflictError(
                    "A conflicting duplicate candle cannot replace cached history"
                )
            raise MarketCacheOutOfOrderError(
                "A candle cannot be inserted before the latest cached candle"
            )

        try:
            validate_candle_sequence(
                [latest, candle],
                key.interval,
                allow_incomplete_last=True,
                current_time_ms=observed_at_ms,
            )
        except CandleSequenceValidationError as error:
            raise MarketCacheGapError(
                "A candle must follow the expected stream interval without overlap"
            ) from error

        previous = latest
        stream.append(self._copy_candle(candle))
        evicted: tuple[Candle, ...] = ()
        if len(stream) > self._capacity:
            evicted = (stream.pop(0),)

        return CandleUpsertResult.INSERTED, evicted, previous, stream[-1]

    @staticmethod
    def _validate_input_candles(
        candles: Sequence[Candle],
        key: MarketStreamKey,
        observed_at_ms: int,
    ) -> None:
        validate_candle_sequence(
            list(candles),
            key.interval,
            allow_incomplete_last=True,
            current_time_ms=observed_at_ms,
        )

    @staticmethod
    def _validate_open_candle_update(previous: Candle, updated: Candle) -> None:
        if updated.open_time_ms != previous.open_time_ms:
            raise MarketCacheInvalidOpenCandleUpdateError(
                "An open-candle update must preserve open_time_ms"
            )
        if updated.close_time_ms != previous.close_time_ms:
            raise MarketCacheInvalidOpenCandleUpdateError(
                "An open-candle update must preserve close_time_ms"
            )
        if updated.open != previous.open:
            raise MarketCacheInvalidOpenCandleUpdateError(
                "An open-candle update must preserve open"
            )
        if updated.high < previous.high:
            raise MarketCacheInvalidOpenCandleUpdateError(
                "An open-candle update must not decrease high"
            )
        if updated.low > previous.low:
            raise MarketCacheInvalidOpenCandleUpdateError(
                "An open-candle update must not increase low"
            )
        if updated.volume < previous.volume:
            raise MarketCacheInvalidOpenCandleUpdateError(
                "An open-candle update must not decrease volume"
            )

    @staticmethod
    def _validate_timestamp(value: int, field_name: str) -> None:
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError(f"{field_name} must be an integer")

    @staticmethod
    def _copy_candle(candle: Candle) -> Candle:
        return candle.model_copy(deep=True)
