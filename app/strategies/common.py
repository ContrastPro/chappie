"""Shared pure signal construction for active strategy implementations."""

from dataclasses import dataclass
from decimal import Decimal

from app.trading.models import (
    ImmutableMapping,
    MarketSnapshot,
    Signal,
    SignalDirection,
    freeze_mapping,
)
from app.trading.strategy import SignalIdFactory


@dataclass(frozen=True, slots=True)
class StrategyIdentity:
    name: str
    version: str = "1.0.0"


class PureStrategy:
    """Reusable no-I/O signal envelope; subclasses calculate only indicators."""

    _identity: StrategyIdentity
    _configuration: ImmutableMapping
    _signal_id_factory: SignalIdFactory

    @property
    def name(self) -> str:
        return self._identity.name

    @property
    def version(self) -> str:
        return self._identity.version

    @property
    def configuration(self) -> ImmutableMapping:
        return self._configuration

    def _signal(
        self,
        snapshot: MarketSnapshot,
        direction: SignalDirection,
        reason: str,
        values: dict[str, int | Decimal | str],
    ) -> Signal:
        details = " ".join(f"{key}={value}" for key, value in values.items())
        return Signal(
            signal_id=self._signal_id_factory.next_id(),
            snapshot_id=snapshot.snapshot_id,
            stream_key=snapshot.stream_key,
            created_at_ms=snapshot.as_of_ms,
            effective_at_ms=snapshot.as_of_ms,
            direction=direction,
            reason_code=reason,
            explanation=f"reason={reason} strategy={self.name}/{self.version}"
            + (f" {details}" if details else ""),
            strategy_name=self.name,
            strategy_version=self.version,
            strategy_parameters=self.configuration,
            correlation_id=snapshot.correlation_cycle_id,
            metadata=values,
        )


def parameters(**values: object) -> ImmutableMapping:
    return freeze_mapping(values)
