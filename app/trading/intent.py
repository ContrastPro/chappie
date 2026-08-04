from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Protocol, runtime_checkable
from uuid import uuid4

from app.trading.exceptions import (
    IntentFactoryError,
    IntentPlannerInvariantError,
    InvalidIntentPlannerConfigurationError,
)
from app.trading.models import (
    ImmutableMapping,
    OrderIntent,
    OrderIntentAction,
    Signal,
    SignalDirection,
    freeze_mapping,
)


class IntentPlanOutcome(Enum):
    CREATED = "created"
    NO_INTENT_FOR_HOLD = "no_intent_for_hold"
    DECLINED = "declined"


@runtime_checkable
class IntentIdFactory(Protocol):
    def new_intent_id(self) -> str:
        """Return the next intent identifier."""


class UUIDIntentIdFactory:
    def new_intent_id(self) -> str:
        return str(uuid4())


@dataclass(frozen=True, slots=True)
class LongOnlyIntentPlannerConfig:
    long_target_exposure: Decimal = Decimal(1)
    planner_name: str = "long_only_target_exposure"
    planner_version: str = "1.0.0"

    def __post_init__(self) -> None:
        exposure = self.long_target_exposure
        if not isinstance(exposure, Decimal) or not exposure.is_finite():
            raise InvalidIntentPlannerConfigurationError(
                "long_target_exposure must be a finite Decimal"
            )
        if exposure <= 0 or exposure > 1:
            raise InvalidIntentPlannerConfigurationError(
                "long_target_exposure must be greater than 0 and at most 1"
            )
        if not isinstance(self.planner_name, str) or not self.planner_name.strip():
            raise InvalidIntentPlannerConfigurationError(
                "planner_name must be a non-empty string"
            )
        if not isinstance(self.planner_version, str) or not self.planner_version.strip():
            raise InvalidIntentPlannerConfigurationError(
                "planner_version must be a non-empty string"
            )


@dataclass(frozen=True, slots=True)
class IntentPlanResult:
    outcome: IntentPlanOutcome
    signal_id: str
    intent: OrderIntent | None
    reason_code: str
    planner_name: str
    planner_version: str
    correlation_id: str | None = None
    metadata: ImmutableMapping = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, IntentPlanOutcome):
            raise IntentPlannerInvariantError("outcome must be an IntentPlanOutcome")
        for field_name in (
            "signal_id",
            "reason_code",
            "planner_name",
            "planner_version",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise IntentPlannerInvariantError(
                    f"{field_name} must be a non-empty string"
                )
        if self.correlation_id is not None and (
            not isinstance(self.correlation_id, str) or not self.correlation_id.strip()
        ):
            raise IntentPlannerInvariantError(
                "correlation_id must be a non-empty string when provided"
            )
        if self.outcome is IntentPlanOutcome.CREATED:
            if not isinstance(self.intent, OrderIntent):
                raise IntentPlannerInvariantError("CREATED requires an OrderIntent")
            if self.signal_id != self.intent.source_signal_id:
                raise IntentPlannerInvariantError(
                    "signal_id must match intent.source_signal_id"
                )
            if self.correlation_id != self.intent.correlation_id:
                raise IntentPlannerInvariantError(
                    "correlation_id must match intent.correlation_id"
                )
        elif self.intent is not None:
            raise IntentPlannerInvariantError(
                "Only CREATED may contain an OrderIntent"
            )
        try:
            object.__setattr__(self, "metadata", freeze_mapping(self.metadata))
        except (TypeError, ValueError) as error:
            raise IntentPlannerInvariantError(
                "metadata must contain immutable values"
            ) from error


@runtime_checkable
class IntentPlanner(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str: ...

    @property
    def configuration(self) -> ImmutableMapping: ...

    def plan(self, signal: Signal) -> IntentPlanResult:
        """Create or deliberately omit an intent without I/O."""


class LongOnlyIntentPlanner:
    def __init__(
        self,
        config: LongOnlyIntentPlannerConfig,
        intent_id_factory: IntentIdFactory,
    ) -> None:
        if not isinstance(config, LongOnlyIntentPlannerConfig):
            raise InvalidIntentPlannerConfigurationError(
                "config must be a LongOnlyIntentPlannerConfig"
            )
        if not hasattr(intent_id_factory, "new_intent_id"):
            raise InvalidIntentPlannerConfigurationError(
                "intent_id_factory must provide new_intent_id"
            )
        self._config = config
        self._intent_id_factory = intent_id_factory
        self._configuration = freeze_mapping(
            {"long_target_exposure": config.long_target_exposure}
        )

    @property
    def name(self) -> str:
        return self._config.planner_name

    @property
    def version(self) -> str:
        return self._config.planner_version

    @property
    def configuration(self) -> ImmutableMapping:
        return self._configuration

    def plan(self, signal: Signal) -> IntentPlanResult:
        if not isinstance(signal, Signal):
            raise IntentPlannerInvariantError("signal must be a valid Signal")
        for field_name in ("signal_id", "strategy_name", "strategy_version"):
            value = getattr(signal, field_name)
            if not isinstance(value, str) or not value.strip():
                raise IntentPlannerInvariantError(
                    f"Signal {field_name} must be a non-empty string"
                )
        if (
            not isinstance(signal.effective_at_ms, int)
            or isinstance(signal.effective_at_ms, bool)
            or signal.effective_at_ms < 0
        ):
            raise IntentPlannerInvariantError(
                "Signal effective_at_ms must be non-negative"
            )
        if not isinstance(signal.direction, SignalDirection):
            raise IntentPlannerInvariantError("Unsupported Signal direction")
        if signal.direction is SignalDirection.HOLD:
            return IntentPlanResult(
                outcome=IntentPlanOutcome.NO_INTENT_FOR_HOLD,
                signal_id=signal.signal_id,
                intent=None,
                reason_code="hold_signal_no_intent",
                planner_name=self.name,
                planner_version=self.version,
                correlation_id=signal.correlation_id,
                metadata={"signal_reason": signal.reason_code},
            )
        if signal.direction is SignalDirection.BUY:
            action = OrderIntentAction.ENTER_LONG
            exposure = self._config.long_target_exposure
            reason_code = "bullish_signal_long_target"
            reason = (
                "Bullish signal mapped to long target exposure "
                f"{self._config.long_target_exposure}"
            )
        elif signal.direction is SignalDirection.SELL:
            action = OrderIntentAction.EXIT_LONG
            exposure = Decimal(0)
            reason_code = "bearish_signal_exit_long"
            reason = "Bearish signal mapped to zero long target exposure"
        else:
            raise IntentPlannerInvariantError("Unsupported Signal direction")

        intent_id = self._new_intent_id()
        intent = OrderIntent(
            intent_id=intent_id,
            source_signal_id=signal.signal_id,
            stream_key=signal.stream_key,
            created_at_ms=signal.effective_at_ms,
            action=action,
            target_exposure=exposure,
            reason=reason,
            strategy_name=signal.strategy_name,
            strategy_version=signal.strategy_version,
            correlation_id=signal.correlation_id,
        )
        return IntentPlanResult(
            outcome=IntentPlanOutcome.CREATED,
            signal_id=signal.signal_id,
            intent=intent,
            reason_code=reason_code,
            planner_name=self.name,
            planner_version=self.version,
            correlation_id=signal.correlation_id,
            metadata={"target_exposure": exposure},
        )

    def _new_intent_id(self) -> str:
        try:
            intent_id = self._intent_id_factory.new_intent_id()
        except Exception as error:
            raise IntentFactoryError("Intent ID factory failed") from error
        if not isinstance(intent_id, str) or not intent_id.strip():
            raise IntentFactoryError("Intent ID factory returned an empty ID")
        return intent_id
