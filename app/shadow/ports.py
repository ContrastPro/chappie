from typing import Protocol, runtime_checkable

from app.market.models import Candle, MarketStreamKey


@runtime_checkable
class MarketCacheReader(Protocol):
    def get_snapshot(
        self, key: MarketStreamKey, limit: int | None = None
    ) -> tuple[Candle, ...]: ...


@runtime_checkable
class SnapshotIdFactory(Protocol):
    def new_snapshot_id(self) -> str: ...


@runtime_checkable
class ShadowRunIdFactory(Protocol):
    def new_shadow_run_id(self) -> str: ...
