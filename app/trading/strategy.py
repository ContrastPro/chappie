from typing import Protocol, runtime_checkable
from uuid import uuid4

from app.trading.models import ImmutableMapping, MarketSnapshot, Signal


class SignalIdFactory(Protocol):
    def next_id(self) -> str:
        """Return the next signal identifier."""


class UUIDSignalIdFactory:
    def next_id(self) -> str:
        return str(uuid4())


@runtime_checkable
class Strategy(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str: ...

    @property
    def required_history(self) -> int: ...

    @property
    def configuration(self) -> ImmutableMapping: ...

    def generate_signal(self, snapshot: MarketSnapshot) -> Signal:
        """Generate one deterministic signal without I/O."""
