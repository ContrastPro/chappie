from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
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
)
from app.market.models import Candle, CandleSnapshot, MarketStreamKey
from app.market.validation import validate_candle_sequence

DEFAULT_CAPACITY_PER_STREAM = 1000
ZERO = Decimal(0)


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
    previous_closed_confirmed: bool = False
    current_closed_confirmed: bool = False
    data_changed: bool = False
    closure_confirmed: bool = False


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    candle: Candle
    closure_confirmed: bool
    last_observed_at_ms: int


class InMemoryMarketCache:
    """Bounded in-memory candle cache for one synchronous writer.

    This class is intentionally not thread-safe. It performs no I/O and uses
    only caller-provided timestamps, making its behavior deterministic.
    """

    def __init__(self, capacity: int = DEFAULT_CAPACITY_PER_STREAM) -> None:
        if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity <= 0:
            raise MarketCacheConfigurationError("Market cache capacity must be positive")

        self._capacity = capacity
        self._streams: dict[MarketStreamKey, list[_CacheEntry]] = {}

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
        result, _, _, _, _, _ = self._upsert_into(
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
            stream_key: [self._copy_entry(item) for item in stream]
            for stream_key, stream in self._streams.items()
        }
        results: list[CandleUpsertResult] = []
        changes: list[MarketCacheChange] = []
        evicted_count = 0

        try:
            for candle in batch:
                result, evicted, previous, current, previous_closed, current_closed = (
                    self._upsert_into(
                        staged_streams,
                        key,
                        candle,
                        observed_at_ms,
                    )
                )
                results.append(result)
                evicted_count += len(evicted)
                changes.append(
                    MarketCacheChange(
                        result=result,
                        previous=(
                            None
                            if previous is None
                            else CandleSnapshot.from_candle(previous.candle)
                        ),
                        current=CandleSnapshot.from_candle(current.candle),
                        evicted=tuple(CandleSnapshot.from_candle(item.candle) for item in evicted),
                        observed_at_ms=observed_at_ms,
                        previous_closed_confirmed=previous_closed,
                        current_closed_confirmed=current_closed,
                        data_changed=(previous is None or previous.candle != current.candle),
                        closure_confirmed=(not previous_closed and current_closed),
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
        return tuple(self._copy_candle(entry.candle) for entry in selected)

    def get_latest_candle(self, key: MarketStreamKey) -> Candle | None:
        stream = self._streams.get(key)
        if not stream:
            return None
        return self._copy_candle(stream[-1].candle)

    def get_latest_closed_candle(
        self,
        key: MarketStreamKey,
        as_of_ms: int,
    ) -> Candle | None:
        self._validate_timestamp(as_of_ms, "as_of_ms")
        stream = self._streams.get(key, [])

        for entry in reversed(stream):
            if as_of_ms > entry.candle.close_time_ms:
                return self._copy_candle(entry.candle)

        return None

    def _upsert_into(
        self,
        streams: dict[MarketStreamKey, list[_CacheEntry]],
        key: MarketStreamKey,
        candle: Candle,
        observed_at_ms: int,
    ) -> tuple[
        CandleUpsertResult,
        tuple[_CacheEntry, ...],
        _CacheEntry | None,
        _CacheEntry,
        bool,
        bool,
    ]:
        stream = streams.setdefault(key, [])

        if not stream:
            current = self._new_entry(candle, observed_at_ms)
            stream.append(current)
            return (
                CandleUpsertResult.INSERTED,
                (),
                None,
                current,
                False,
                current.closure_confirmed,
            )

        latest = stream[-1]
        latest_candle = latest.candle

        if observed_at_ms < latest.last_observed_at_ms:
            raise self._invalid_open_update(
                "An open-candle observation must not regress time",
                key,
                latest,
                candle,
                observed_at_ms,
            )

        if candle.open_time_ms == latest_candle.open_time_ms:
            should_confirm = observed_at_ms > latest_candle.close_time_ms
            if candle == latest_candle:
                if should_confirm and not latest.closure_confirmed:
                    current = _CacheEntry(
                        candle=self._copy_candle(latest_candle),
                        closure_confirmed=True,
                        last_observed_at_ms=observed_at_ms,
                    )
                    stream[-1] = current
                    return CandleUpsertResult.UNCHANGED, (), latest, current, False, True
                return (
                    CandleUpsertResult.UNCHANGED,
                    (),
                    latest,
                    latest,
                    latest.closure_confirmed,
                    latest.closure_confirmed,
                )
            if latest.closure_confirmed:
                raise MarketCacheClosedCandleUpdateError(
                    "A confirmed closed candle cannot be updated",
                    stream_key=key,
                    stored_candle=CandleSnapshot.from_candle(latest_candle),
                    received_candle=CandleSnapshot.from_candle(candle),
                    observed_at_ms=observed_at_ms,
                    differing_fields=self._differing_candle_fields(latest_candle, candle),
                )
            self._validate_open_candle_update(
                latest_candle,
                candle,
                key,
                observed_at_ms,
            )
            current = _CacheEntry(
                candle=self._copy_candle(candle),
                closure_confirmed=should_confirm,
                last_observed_at_ms=observed_at_ms,
            )
            stream[-1] = current
            return (
                CandleUpsertResult.UPDATED,
                (),
                latest,
                current,
                False,
                current.closure_confirmed,
            )

        if candle.open_time_ms < latest_candle.open_time_ms:
            if any(item.candle.open_time_ms == candle.open_time_ms for item in stream[:-1]):
                raise MarketCacheDuplicateConflictError(
                    "A conflicting duplicate candle cannot replace cached history"
                )
            raise MarketCacheOutOfOrderError(
                "A candle cannot be inserted before the latest cached candle"
            )

        try:
            validate_candle_sequence(
                [latest_candle, candle],
                key.interval,
                allow_incomplete_last=True,
                current_time_ms=observed_at_ms,
                stream_key=key,
            )
        except CandleSequenceValidationError as error:
            raise MarketCacheGapError(
                "A candle must follow the expected stream interval without overlap"
            ) from error

        previous = latest
        current = self._new_entry(candle, observed_at_ms)
        stream.append(current)
        evicted: tuple[_CacheEntry, ...] = ()
        if len(stream) > self._capacity:
            evicted = (stream.pop(0),)

        return (
            CandleUpsertResult.INSERTED,
            evicted,
            previous,
            stream[-1],
            previous.closure_confirmed,
            current.closure_confirmed,
        )

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
            stream_key=key,
        )

    @classmethod
    def _validate_open_candle_update(
        cls,
        previous: Candle,
        updated: Candle,
        key: MarketStreamKey,
        observed_at_ms: int,
    ) -> None:
        if updated.open_time_ms != previous.open_time_ms:
            raise cls._invalid_open_update(
                "An open-candle update must preserve open_time_ms",
                key,
                previous,
                updated,
                observed_at_ms,
            )
        if updated.close_time_ms != previous.close_time_ms:
            raise cls._invalid_open_update(
                "An open-candle update must preserve close_time_ms",
                key,
                previous,
                updated,
                observed_at_ms,
            )

        # Binance can expose a new interval before its first trade.  Such a
        # zero-volume placeholder has provisional OHLC values; its first real
        # trade establishes the official OHLC, which may not be monotonic
        # relative to that placeholder.  All normalized candle invariants were
        # validated before reaching this point.
        if previous.volume == ZERO and updated.volume > ZERO:
            return

        if updated.open != previous.open:
            raise cls._invalid_open_update(
                "An open-candle update must preserve open",
                key,
                previous,
                updated,
                observed_at_ms,
            )
        if updated.high < previous.high:
            raise cls._invalid_open_update(
                "An open-candle update must not decrease high",
                key,
                previous,
                updated,
                observed_at_ms,
            )
        if updated.low > previous.low:
            raise cls._invalid_open_update(
                "An open-candle update must not increase low",
                key,
                previous,
                updated,
                observed_at_ms,
            )
        if updated.volume < previous.volume:
            raise cls._invalid_open_update(
                "An open-candle update must not decrease volume",
                key,
                previous,
                updated,
                observed_at_ms,
            )

    @classmethod
    def _invalid_open_update(
        cls,
        message: str,
        key: MarketStreamKey,
        previous: _CacheEntry | Candle,
        updated: Candle,
        observed_at_ms: int,
    ) -> MarketCacheInvalidOpenCandleUpdateError:
        entry = previous if isinstance(previous, _CacheEntry) else None
        stored = entry.candle if entry is not None else previous
        return MarketCacheInvalidOpenCandleUpdateError(
            message,
            stream_key=key,
            stored_candle=CandleSnapshot.from_candle(stored),
            received_candle=CandleSnapshot.from_candle(updated),
            observed_at_ms=observed_at_ms,
            differing_fields=cls._differing_candle_fields(stored, updated),
            previously_confirmed_closed=(entry.closure_confirmed if entry is not None else False),
        )

    @staticmethod
    def _validate_timestamp(value: int, field_name: str) -> None:
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError(f"{field_name} must be an integer")

    @staticmethod
    def _copy_candle(candle: Candle) -> Candle:
        return candle.model_copy(deep=True)

    @classmethod
    def _new_entry(cls, candle: Candle, observed_at_ms: int) -> _CacheEntry:
        return _CacheEntry(
            candle=cls._copy_candle(candle),
            closure_confirmed=observed_at_ms > candle.close_time_ms,
            last_observed_at_ms=observed_at_ms,
        )

    @classmethod
    def _copy_entry(cls, entry: _CacheEntry) -> _CacheEntry:
        return _CacheEntry(
            candle=cls._copy_candle(entry.candle),
            closure_confirmed=entry.closure_confirmed,
            last_observed_at_ms=entry.last_observed_at_ms,
        )

    @staticmethod
    def _differing_candle_fields(previous: Candle, current: Candle) -> tuple[str, ...]:
        fields = (
            "open_time_ms",
            "close_time_ms",
            "open",
            "high",
            "low",
            "close",
            "volume",
        )
        return tuple(name for name in fields if getattr(previous, name) != getattr(current, name))
