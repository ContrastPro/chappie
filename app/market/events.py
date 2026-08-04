from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol
from uuid import uuid4

from app.market.cache import CandleUpsertResult, MarketCacheChange, MarketStreamKey
from app.market.exceptions import (
    DuplicateCandleClosureError,
    InvalidEventSubscriptionError,
    InvalidMarketEventError,
    MarketEventProjectionError,
    RecursiveEventPublishError,
)
from app.market.models import CandleSnapshot


class EventIdFactory(Protocol):
    def next_id(self) -> str:
        """Return the next event identifier."""


class CorrelationIdFactory(Protocol):
    def next_id(self) -> str:
        """Return the next polling-cycle correlation identifier."""


class UUIDEventIdFactory:
    def next_id(self) -> str:
        return str(uuid4())


class UUIDCorrelationIdFactory:
    def next_id(self) -> str:
        return str(uuid4())


class MarketEventSource(Enum):
    REST_POLLING = "rest_polling"
    REPLAY = "replay"


class CandleInsertedReason(Enum):
    NEW_CANDLE = "new_candle"
    RECOVERY_BACKFILL = "recovery_backfill"


class CandleUpdatedReason(Enum):
    OPEN_CANDLE_REFRESH = "open_candle_refresh"


class CandleClosedReason(Enum):
    TIME_CONFIRMED = "time_confirmed"
    NEXT_CANDLE_OBSERVED = "next_candle_observed"
    RESUME_RECONCILIATION = "resume_reconciliation"


class CandleEvictedReason(Enum):
    CAPACITY_LIMIT = "capacity_limit"


class MarketGapReason(Enum):
    INTERVAL_GAP = "interval_gap"
    OVERLAP = "overlap"
    RECOVERY_DISCONNECTED = "recovery_disconnected"


class MarketGapPhase(Enum):
    BOOTSTRAP = "bootstrap"
    REGULAR = "regular"
    RECOVERY = "recovery"


class MarketStreamFailureReason(Enum):
    PROVIDER = "provider"
    EMPTY_RESPONSE = "empty_response"
    VALIDATION = "validation"
    CACHE_INTEGRITY = "cache_integrity"
    GAP_RECOVERY = "gap_recovery"
    DELIVERY = "delivery"


@dataclass(frozen=True, slots=True)
class MarketEvent:
    event_id: str
    occurred_at_ms: int
    stream_key: MarketStreamKey
    correlation_cycle_id: str
    source: MarketEventSource

    def __post_init__(self) -> None:
        if not self.event_id:
            raise InvalidMarketEventError("event_id must not be empty")
        if not isinstance(self.occurred_at_ms, int) or isinstance(self.occurred_at_ms, bool):
            raise InvalidMarketEventError("occurred_at_ms must be an integer")
        if not self.correlation_cycle_id:
            raise InvalidMarketEventError("correlation_cycle_id must not be empty")
        if not isinstance(self.stream_key, MarketStreamKey):
            raise InvalidMarketEventError("stream_key must be a MarketStreamKey")
        if not isinstance(self.source, MarketEventSource):
            raise InvalidMarketEventError("source must be a MarketEventSource")


@dataclass(frozen=True, slots=True)
class CandleInserted(MarketEvent):
    candle: CandleSnapshot
    reason: CandleInsertedReason
    bootstrap: bool = False

    def __post_init__(self) -> None:
        MarketEvent.__post_init__(self)
        _validate_candle_event(self.candle, self.reason, CandleInsertedReason)
        if not isinstance(self.bootstrap, bool):
            raise InvalidMarketEventError("bootstrap must be a boolean")


@dataclass(frozen=True, slots=True)
class CandleUpdated(MarketEvent):
    previous: CandleSnapshot
    current: CandleSnapshot
    reason: CandleUpdatedReason

    def __post_init__(self) -> None:
        MarketEvent.__post_init__(self)
        _validate_candle_event(self.previous, self.reason, CandleUpdatedReason)
        if not isinstance(self.current, CandleSnapshot):
            raise InvalidMarketEventError("current must be a CandleSnapshot")
        if self.previous.open_time_ms != self.current.open_time_ms:
            raise InvalidMarketEventError("CandleUpdated snapshots must identify the same candle")


@dataclass(frozen=True, slots=True)
class CandleClosed(MarketEvent):
    candle: CandleSnapshot
    reason: CandleClosedReason

    def __post_init__(self) -> None:
        MarketEvent.__post_init__(self)
        _validate_candle_event(self.candle, self.reason, CandleClosedReason)


@dataclass(frozen=True, slots=True)
class CandleEvicted(MarketEvent):
    candle: CandleSnapshot
    reason: CandleEvictedReason

    def __post_init__(self) -> None:
        MarketEvent.__post_init__(self)
        _validate_candle_event(self.candle, self.reason, CandleEvictedReason)


@dataclass(frozen=True, slots=True)
class MarketGapDetected(MarketEvent):
    expected_open_time_ms: int | None
    received_open_time_ms: int | None
    last_confirmed_open_time_ms: int | None
    phase: MarketGapPhase
    reason: MarketGapReason
    previous_index: int | None = None
    current_index: int | None = None
    previous_open_time_ms: int | None = None
    previous_close_time_ms: int | None = None
    actual_difference_ms: int | None = None

    def __post_init__(self) -> None:
        MarketEvent.__post_init__(self)
        for field_name in (
            "expected_open_time_ms",
            "received_open_time_ms",
            "last_confirmed_open_time_ms",
            "previous_index",
            "current_index",
            "previous_open_time_ms",
            "previous_close_time_ms",
            "actual_difference_ms",
        ):
            value = getattr(self, field_name)
            if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
                raise InvalidMarketEventError(f"{field_name} must be an integer or None")
        if not isinstance(self.phase, MarketGapPhase):
            raise InvalidMarketEventError("phase must be a MarketGapPhase")
        if not isinstance(self.reason, MarketGapReason):
            raise InvalidMarketEventError("reason must be a MarketGapReason")


@dataclass(frozen=True, slots=True)
class MarketStreamFailed(MarketEvent):
    error_category: str
    consecutive_failures: int
    terminal: bool
    reason: MarketStreamFailureReason

    def __post_init__(self) -> None:
        MarketEvent.__post_init__(self)
        if not self.error_category:
            raise InvalidMarketEventError("error_category must not be empty")
        if (
            not isinstance(self.consecutive_failures, int)
            or isinstance(self.consecutive_failures, bool)
            or self.consecutive_failures <= 0
        ):
            raise InvalidMarketEventError("consecutive_failures must be a positive integer")
        if not isinstance(self.terminal, bool):
            raise InvalidMarketEventError("terminal must be a boolean")
        if not isinstance(self.reason, MarketStreamFailureReason):
            raise InvalidMarketEventError("reason must be a MarketStreamFailureReason")


def _validate_candle_event(
    candle: CandleSnapshot,
    reason: Enum,
    reason_type: type[Enum],
) -> None:
    if not isinstance(candle, CandleSnapshot):
        raise InvalidMarketEventError("candle must be a CandleSnapshot")
    if not isinstance(reason, reason_type):
        raise InvalidMarketEventError(f"reason must be a {reason_type.__name__}")


MarketEventHandler = Callable[[MarketEvent], None]


@dataclass(frozen=True, slots=True)
class SubscriptionToken:
    dispatcher_id: int
    sequence: int


@dataclass(frozen=True, slots=True)
class EventDeliveryFailure:
    token: SubscriptionToken
    handler_name: str
    error: Exception


@dataclass(frozen=True, slots=True)
class EventDeliveryResult:
    delivered_count: int
    failures: tuple[EventDeliveryFailure, ...]

    @property
    def failure_count(self) -> int:
        return len(self.failures)

    @property
    def degraded(self) -> bool:
        return bool(self.failures)


@dataclass(frozen=True, slots=True)
class _Subscription:
    token: SubscriptionToken
    event_type: type[MarketEvent]
    handler: MarketEventHandler


class MarketEventDispatcher:
    """Synchronous, in-process dispatcher for one execution thread."""

    def __init__(self) -> None:
        self._subscriptions: list[_Subscription] = []
        self._next_sequence = 1
        self._publishing = False

    def subscribe(
        self,
        event_type: type[MarketEvent],
        handler: MarketEventHandler,
    ) -> SubscriptionToken:
        if not isinstance(event_type, type) or not issubclass(event_type, MarketEvent):
            raise InvalidEventSubscriptionError("event_type must be a MarketEvent class")
        if not callable(handler):
            raise InvalidEventSubscriptionError("handler must be callable")

        token = SubscriptionToken(id(self), self._next_sequence)
        self._next_sequence += 1
        self._subscriptions.append(_Subscription(token, event_type, handler))
        return token

    def unsubscribe(self, token: SubscriptionToken) -> bool:
        if not isinstance(token, SubscriptionToken) or token.dispatcher_id != id(self):
            return False

        for index, subscription in enumerate(self._subscriptions):
            if subscription.token == token:
                del self._subscriptions[index]
                return True
        return False

    def publish(self, event: MarketEvent) -> EventDeliveryResult:
        if not isinstance(event, MarketEvent):
            raise InvalidMarketEventError("publish requires a MarketEvent")
        if self._publishing:
            raise RecursiveEventPublishError("Recursive event publish is forbidden")

        subscriptions = tuple(
            subscription
            for subscription in self._subscriptions
            if isinstance(event, subscription.event_type)
        )
        delivered_count = 0
        failures: list[EventDeliveryFailure] = []
        self._publishing = True
        try:
            for subscription in subscriptions:
                try:
                    subscription.handler(event)
                    delivered_count += 1
                # Handler isolation requires collecting arbitrary application
                # exceptions without aborting delivery to later subscribers.
                except Exception as error:  # noqa: BLE001
                    failures.append(
                        EventDeliveryFailure(
                            token=subscription.token,
                            handler_name=self._handler_name(subscription.handler),
                            error=error,
                        )
                    )
        finally:
            self._publishing = False

        return EventDeliveryResult(
            delivered_count=delivered_count,
            failures=tuple(failures),
        )

    @staticmethod
    def _handler_name(handler: MarketEventHandler) -> str:
        return getattr(handler, "__qualname__", handler.__class__.__qualname__)


class MarketEventProjector:
    """Projects committed cache mutation details into ordered market events."""

    def __init__(self, event_id_factory: EventIdFactory) -> None:
        self._event_id_factory = event_id_factory
        self._closure_watermarks: dict[MarketStreamKey, int] = {}
        self._latest: dict[MarketStreamKey, CandleSnapshot] = {}

    def project(
        self,
        stream_key: MarketStreamKey,
        changes: tuple[MarketCacheChange, ...],
        *,
        occurred_at_ms: int,
        correlation_cycle_id: str,
        source: MarketEventSource,
        bootstrap: bool,
        recovery: bool = False,
        resumed: bool = False,
    ) -> tuple[MarketEvent, ...]:
        """Atomically project one committed cache result into ordered events."""

        original_watermarks = self._closure_watermarks
        original_latest = self._latest
        self._closure_watermarks = dict(original_watermarks)
        self._latest = dict(original_latest)
        try:
            return self._project_staged(
                stream_key,
                changes,
                occurred_at_ms=occurred_at_ms,
                correlation_cycle_id=correlation_cycle_id,
                source=source,
                bootstrap=bootstrap,
                recovery=recovery,
                resumed=resumed,
            )
        except Exception:
            self._closure_watermarks = original_watermarks
            self._latest = original_latest
            raise

    def _project_staged(
        self,
        stream_key: MarketStreamKey,
        changes: tuple[MarketCacheChange, ...],
        *,
        occurred_at_ms: int,
        correlation_cycle_id: str,
        source: MarketEventSource,
        bootstrap: bool,
        recovery: bool = False,
        resumed: bool = False,
    ) -> tuple[MarketEvent, ...]:
        self._validate_projection_input(changes, occurred_at_ms)
        if not changes:
            return ()

        events: list[MarketEvent] = []
        if bootstrap:
            self._initialize_bootstrap_watermark(stream_key, changes)

        for index, change in enumerate(changes):
            future_insert = any(
                candidate.result is CandleUpsertResult.INSERTED
                and candidate.current.open_time_ms > change.current.open_time_ms
                for candidate in changes[index + 1 :]
            )
            previous = change.previous or self._latest.get(stream_key)

            if (
                not bootstrap
                and previous is not None
                and change.result is CandleUpsertResult.INSERTED
            ):
                close_reason = (
                    CandleClosedReason.RESUME_RECONCILIATION
                    if resumed
                    else (
                        CandleClosedReason.NEXT_CANDLE_OBSERVED
                        if change.result is CandleUpsertResult.INSERTED
                        else CandleClosedReason.TIME_CONFIRMED
                    )
                )
                closed = self._project_closure(
                    stream_key,
                    previous,
                    change.observed_at_ms,
                    occurred_at_ms,
                    correlation_cycle_id,
                    source,
                    close_reason,
                )
                if closed is not None:
                    events.append(closed)

            if change.result is CandleUpsertResult.INSERTED:
                events.append(
                    CandleInserted(
                        **self._envelope(
                            stream_key,
                            occurred_at_ms,
                            correlation_cycle_id,
                            source,
                        ),
                        candle=change.current,
                        reason=(
                            CandleInsertedReason.RECOVERY_BACKFILL
                            if recovery
                            else CandleInsertedReason.NEW_CANDLE
                        ),
                        bootstrap=bootstrap,
                    )
                )
            elif change.result is CandleUpsertResult.UPDATED:
                if change.previous is None:
                    raise MarketEventProjectionError(
                        "Updated cache change requires previous candle"
                    )
                events.append(
                    CandleUpdated(
                        **self._envelope(
                            stream_key,
                            occurred_at_ms,
                            correlation_cycle_id,
                            source,
                        ),
                        previous=change.previous,
                        current=change.current,
                        reason=CandleUpdatedReason.OPEN_CANDLE_REFRESH,
                    )
                )

            if not bootstrap and change.closure_confirmed:
                closed = self._project_closure(
                    stream_key,
                    change.current,
                    change.observed_at_ms,
                    occurred_at_ms,
                    correlation_cycle_id,
                    source,
                    (
                        CandleClosedReason.RESUME_RECONCILIATION
                        if resumed
                        else (
                            CandleClosedReason.NEXT_CANDLE_OBSERVED
                            if future_insert
                            else CandleClosedReason.TIME_CONFIRMED
                        )
                    ),
                )
                if closed is not None:
                    events.append(closed)

            for evicted in change.evicted:
                events.append(
                    CandleEvicted(
                        **self._envelope(
                            stream_key,
                            occurred_at_ms,
                            correlation_cycle_id,
                            source,
                        ),
                        candle=evicted,
                        reason=CandleEvictedReason.CAPACITY_LIMIT,
                    )
                )
            self._latest[stream_key] = change.current

        if not bootstrap:
            latest = self._latest[stream_key]
            closed = self._project_closure(
                stream_key,
                latest,
                changes[-1].observed_at_ms,
                occurred_at_ms,
                correlation_cycle_id,
                source,
                (
                    CandleClosedReason.RESUME_RECONCILIATION
                    if resumed
                    else CandleClosedReason.TIME_CONFIRMED
                ),
            )
            if closed is not None:
                events.append(closed)

        return tuple(events)

    def _project_closure(
        self,
        stream_key: MarketStreamKey,
        candle: CandleSnapshot,
        observed_at_ms: int,
        occurred_at_ms: int,
        correlation_cycle_id: str,
        source: MarketEventSource,
        reason: CandleClosedReason,
    ) -> CandleClosed | None:
        watermark = self._closure_watermarks.get(stream_key)
        if watermark is not None and candle.open_time_ms <= watermark:
            return None
        if observed_at_ms <= candle.close_time_ms:
            return None
        if watermark is not None and candle.open_time_ms < watermark:
            raise DuplicateCandleClosureError("Candle closure is older than the stream watermark")

        self._closure_watermarks[stream_key] = candle.open_time_ms
        return CandleClosed(
            **self._envelope(
                stream_key,
                occurred_at_ms,
                correlation_cycle_id,
                source,
            ),
            candle=candle,
            reason=reason,
        )

    def _initialize_bootstrap_watermark(
        self,
        stream_key: MarketStreamKey,
        changes: tuple[MarketCacheChange, ...],
    ) -> None:
        closed_open_times = [
            change.current.open_time_ms
            for change in changes
            if change.observed_at_ms > change.current.close_time_ms
        ]
        if closed_open_times:
            current = self._closure_watermarks.get(stream_key)
            maximum = max(closed_open_times)
            self._closure_watermarks[stream_key] = (
                maximum if current is None else max(current, maximum)
            )

    def _envelope(
        self,
        stream_key: MarketStreamKey,
        occurred_at_ms: int,
        correlation_cycle_id: str,
        source: MarketEventSource,
    ) -> dict[str, object]:
        return {
            "event_id": self._event_id_factory.next_id(),
            "occurred_at_ms": occurred_at_ms,
            "stream_key": stream_key,
            "correlation_cycle_id": correlation_cycle_id,
            "source": source,
        }

    @staticmethod
    def _validate_projection_input(
        changes: tuple[MarketCacheChange, ...],
        occurred_at_ms: int,
    ) -> None:
        if not isinstance(occurred_at_ms, int) or isinstance(occurred_at_ms, bool):
            raise InvalidMarketEventError("occurred_at_ms must be an integer")
        if any(not isinstance(change, MarketCacheChange) for change in changes):
            raise MarketEventProjectionError(
                "Projector requires committed MarketCacheChange values"
            )
