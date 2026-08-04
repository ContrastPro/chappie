from collections.abc import Sequence

from app.market.models import Candle, CandleSnapshot, MarketStreamKey
from app.trading.exceptions import InvalidMarketSnapshotError
from app.trading.models import MarketSnapshot


class MarketSnapshotFactory:
    """Build deterministic domain snapshots from already-read market data."""

    def __init__(
        self,
        stale_after_ms: int,
        data_version: str = "market-snapshot/1",
    ) -> None:
        if (
            not isinstance(stale_after_ms, int)
            or isinstance(stale_after_ms, bool)
            or stale_after_ms <= 0
        ):
            raise InvalidMarketSnapshotError("stale_after_ms must be positive")
        if not isinstance(data_version, str) or not data_version.strip():
            raise InvalidMarketSnapshotError("data_version must not be empty")
        self._stale_after_ms = stale_after_ms
        self._data_version = data_version

    def create(
        self,
        stream_key: MarketStreamKey,
        candles: Sequence[Candle | CandleSnapshot],
        as_of_ms: int,
        snapshot_id: str,
        source_event_id: str | None = None,
        correlation_cycle_id: str | None = None,
    ) -> MarketSnapshot:
        if not isinstance(as_of_ms, int) or isinstance(as_of_ms, bool) or as_of_ms < 0:
            raise InvalidMarketSnapshotError("as_of_ms must be non-negative")
        try:
            items = tuple(self._snapshot(candle) for candle in candles)
        except TypeError as error:
            raise InvalidMarketSnapshotError("candles must be a sequence") from error

        previous_open: int | None = None
        for candle in items:
            if previous_open is not None and candle.open_time_ms <= previous_open:
                raise InvalidMarketSnapshotError("candles must be strictly ordered")
            previous_open = candle.open_time_ms

        closed: list[CandleSnapshot] = []
        open_candles: list[CandleSnapshot] = []
        for candle in items:
            if as_of_ms > candle.close_time_ms:
                if open_candles:
                    raise InvalidMarketSnapshotError("A closed candle cannot follow an open candle")
                closed.append(candle)
            else:
                open_candles.append(candle)
        if len(open_candles) > 1:
            raise InvalidMarketSnapshotError("Only the final candle may be open")

        latest_closed = closed[-1] if closed else None
        latest_open = open_candles[0] if open_candles else None
        staleness_ms = (
            None if latest_closed is None else max(0, as_of_ms - latest_closed.close_time_ms)
        )
        return MarketSnapshot(
            snapshot_id=snapshot_id,
            stream_key=stream_key,
            as_of_ms=as_of_ms,
            closed_candles=tuple(closed),
            latest_closed_candle=latest_closed,
            latest_open_candle=latest_open,
            source_event_id=source_event_id,
            correlation_cycle_id=correlation_cycle_id,
            data_version=self._data_version,
            staleness_ms=staleness_ms,
            is_stale=(True if staleness_ms is None else staleness_ms > self._stale_after_ms),
        )

    @staticmethod
    def _snapshot(candle: Candle | CandleSnapshot) -> CandleSnapshot:
        if isinstance(candle, Candle):
            return CandleSnapshot.from_candle(candle)
        if isinstance(candle, CandleSnapshot):
            return CandleSnapshot(
                open_time_ms=candle.open_time_ms,
                close_time_ms=candle.close_time_ms,
                open=candle.open,
                high=candle.high,
                low=candle.low,
                close=candle.close,
                volume=candle.volume,
            )
        raise InvalidMarketSnapshotError(
            "candles must contain only Candle or CandleSnapshot values"
        )
