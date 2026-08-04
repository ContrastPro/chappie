from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from app.market.models import MarketStreamKey
from app.trading.exceptions import (
    IntentFactoryError,
    IntentPlannerInvariantError,
    InvalidIntentPlannerConfigurationError,
)
from app.trading.intent import (
    IntentIdFactory,
    IntentPlanner,
    IntentPlanOutcome,
    IntentPlanResult,
    LongOnlyIntentPlanner,
    LongOnlyIntentPlannerConfig,
    UUIDIntentIdFactory,
)
from app.trading.models import (
    OrderIntent,
    OrderIntentAction,
    Signal,
    SignalDirection,
)

KEY = MarketStreamKey("BTCUSDT", "1m")
NOW_MS = 1_700_000_000_000


class SequentialIntentIdFactory:
    def __init__(self, values: list[str | BaseException] | None = None) -> None:
        self.values = list(values or ["intent-1"])
        self.calls = 0

    def new_intent_id(self) -> str:
        self.calls += 1
        value = self.values.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


def signal(direction: SignalDirection, **overrides: object) -> Signal:
    reasons = {
        SignalDirection.BUY: "bullish_crossover",
        SignalDirection.SELL: "bearish_crossover",
        SignalDirection.HOLD: "no_crossover",
    }
    values = {
        "signal_id": "signal-1",
        "snapshot_id": "snapshot-1",
        "stream_key": KEY,
        "created_at_ms": NOW_MS,
        "effective_at_ms": NOW_MS,
        "direction": direction,
        "reason_code": reasons[direction],
        "explanation": "deterministic test signal",
        "strategy_name": "ema_crossover",
        "strategy_version": "1.0.0",
        "strategy_parameters": {"fast_window": 2, "slow_window": 3},
        "correlation_id": "cycle-1",
        **overrides,
    }
    return Signal(**values)  # type: ignore[arg-type]


def planner(
    *,
    exposure: Decimal = Decimal(1),
    factory: SequentialIntentIdFactory | None = None,
) -> tuple[LongOnlyIntentPlanner, SequentialIntentIdFactory]:
    ids = factory or SequentialIntentIdFactory()
    return (
        LongOnlyIntentPlanner(
            LongOnlyIntentPlannerConfig(long_target_exposure=exposure),
            ids,
        ),
        ids,
    )


def test_default_and_custom_configuration_are_immutable() -> None:
    default = LongOnlyIntentPlannerConfig()
    custom = LongOnlyIntentPlannerConfig(long_target_exposure=Decimal("0.25"))

    assert default.long_target_exposure == Decimal(1)
    assert default.planner_name == "long_only_target_exposure"
    assert default.planner_version == "1.0.0"
    assert custom.long_target_exposure == Decimal("0.25")
    with pytest.raises(FrozenInstanceError):
        default.long_target_exposure = Decimal("0.5")  # type: ignore[misc]


@pytest.mark.parametrize(
    "exposure",
    [
        Decimal(0),
        Decimal("-0.1"),
        Decimal("1.1"),
        Decimal("NaN"),
        Decimal("Infinity"),
        0.5,
        True,
    ],
)
def test_configuration_rejects_invalid_exposure(exposure: object) -> None:
    with pytest.raises(InvalidIntentPlannerConfigurationError):
        LongOnlyIntentPlannerConfig(long_target_exposure=exposure)  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["planner_name", "planner_version"])
def test_configuration_rejects_empty_identity(field: str) -> None:
    with pytest.raises(InvalidIntentPlannerConfigurationError):
        LongOnlyIntentPlannerConfig(**{field: ""})


def test_buy_maps_to_enter_long_with_configured_exposure() -> None:
    subject, ids = planner(exposure=Decimal("0.25"))
    source = signal(SignalDirection.BUY)

    result = subject.plan(source)

    assert result.outcome is IntentPlanOutcome.CREATED
    assert result.intent is not None
    assert result.intent.intent_id == "intent-1"
    assert result.intent.action is OrderIntentAction.ENTER_LONG
    assert result.intent.target_exposure == Decimal("0.25")
    assert result.intent.source_signal_id == source.signal_id
    assert result.intent.stream_key == source.stream_key
    assert result.intent.created_at_ms == source.effective_at_ms
    assert result.intent.strategy_name == source.strategy_name
    assert result.intent.strategy_version == source.strategy_version
    assert result.intent.correlation_id == source.correlation_id
    assert ids.calls == 1


def test_sell_maps_to_exit_long_with_zero_exposure() -> None:
    subject, ids = planner()
    source = signal(SignalDirection.SELL)

    result = subject.plan(source)

    assert result.outcome is IntentPlanOutcome.CREATED
    assert result.intent is not None
    assert result.intent.action is OrderIntentAction.EXIT_LONG
    assert result.intent.target_exposure == Decimal(0)
    assert result.intent.created_at_ms == source.effective_at_ms
    assert ids.calls == 1


def test_hold_returns_no_intent_without_allocating_id() -> None:
    subject, ids = planner(factory=SequentialIntentIdFactory([]))
    source = signal(SignalDirection.HOLD)

    result = subject.plan(source)

    assert result.outcome is IntentPlanOutcome.NO_INTENT_FOR_HOLD
    assert result.intent is None
    assert result.reason_code == "hold_signal_no_intent"
    assert result.correlation_id == source.correlation_id
    assert ids.calls == 0


def test_planner_does_not_mutate_signal_and_ids_are_deterministic() -> None:
    ids = SequentialIntentIdFactory(["intent-1", "intent-2"])
    subject, _ = planner(factory=ids)
    source = signal(SignalDirection.BUY)

    first = subject.plan(source)
    second = subject.plan(source)

    assert source.signal_id == "signal-1"
    assert (first.intent.intent_id, second.intent.intent_id) == (  # type: ignore[union-attr]
        "intent-1",
        "intent-2",
    )
    assert first.intent.target_exposure == second.intent.target_exposure  # type: ignore[union-attr]


def test_factory_failure_is_typed_and_planner_remains_reusable() -> None:
    ids = SequentialIntentIdFactory([RuntimeError("allocation failed"), "intent-2"])
    subject, _ = planner(factory=ids)
    source = signal(SignalDirection.BUY)

    with pytest.raises(IntentFactoryError) as caught:
        subject.plan(source)
    result = subject.plan(source)

    assert isinstance(caught.value.__cause__, RuntimeError)
    assert result.intent is not None
    assert result.intent.intent_id == "intent-2"


def test_empty_factory_id_is_rejected() -> None:
    subject, _ = planner(factory=SequentialIntentIdFactory([""]))
    with pytest.raises(IntentFactoryError, match="empty"):
        subject.plan(signal(SignalDirection.BUY))


def test_planner_runtime_protocol_and_configuration_immutability() -> None:
    subject, _ = planner()

    assert isinstance(subject, IntentPlanner)
    assert subject.name == "long_only_target_exposure"
    assert subject.version == "1.0.0"
    with pytest.raises(TypeError):
        subject.configuration["long_target_exposure"] = Decimal("0.5")  # type: ignore[index]


def test_intent_plan_result_supports_declined_and_freezes_metadata() -> None:
    metadata = {"reasons": ["policy"]}
    result = IntentPlanResult(
        outcome=IntentPlanOutcome.DECLINED,
        signal_id="signal-1",
        intent=None,
        reason_code="planner_policy_declined",
        planner_name="planner",
        planner_version="1",
        correlation_id="cycle-1",
        metadata=metadata,
    )
    metadata["reasons"].append("mutated")

    assert result.metadata["reasons"] == ("policy",)
    with pytest.raises(TypeError):
        result.metadata["new"] = 1  # type: ignore[index]


def test_intent_plan_result_enforces_outcome_intent_relationship() -> None:
    created_intent = OrderIntent(
        intent_id="intent-1",
        source_signal_id="signal-1",
        stream_key=KEY,
        created_at_ms=NOW_MS,
        action=OrderIntentAction.ENTER_LONG,
        target_exposure=Decimal(1),
        reason="test",
        strategy_name="ema_crossover",
        strategy_version="1.0.0",
    )
    common = {
        "signal_id": "signal-1",
        "reason_code": "reason",
        "planner_name": "planner",
        "planner_version": "1",
    }
    with pytest.raises(IntentPlannerInvariantError):
        IntentPlanResult(
            outcome=IntentPlanOutcome.CREATED,
            intent=None,
            **common,
        )
    with pytest.raises(IntentPlannerInvariantError):
        IntentPlanResult(
            outcome=IntentPlanOutcome.DECLINED,
            intent=created_intent,
            **common,
        )
    with pytest.raises(IntentPlannerInvariantError, match="source_signal_id"):
        IntentPlanResult(
            outcome=IntentPlanOutcome.CREATED,
            intent=created_intent,
            **{**common, "signal_id": "wrong"},
        )
    with pytest.raises(IntentPlannerInvariantError, match="correlation_id"):
        IntentPlanResult(
            outcome=IntentPlanOutcome.CREATED,
            intent=created_intent,
            correlation_id="cycle-1",
            **common,
        )
    with pytest.raises(IntentPlannerInvariantError):
        IntentPlanResult(
            outcome=IntentPlanOutcome.NO_INTENT_FOR_HOLD,
            intent=created_intent,
            **common,
        )


@pytest.mark.parametrize("value", ["", "   ", 42])
def test_intent_factory_rejects_invalid_output(value: object) -> None:
    ids = SequentialIntentIdFactory([value])  # type: ignore[list-item]
    subject, _ = planner(factory=ids)

    with pytest.raises(IntentFactoryError):
        subject.plan(signal(SignalDirection.BUY))


def test_production_intent_factory_satisfies_runtime_protocol() -> None:
    assert isinstance(UUIDIntentIdFactory(), IntentIdFactory)


def test_result_rejects_empty_identity_and_deep_mutability() -> None:
    common = {
        "outcome": IntentPlanOutcome.DECLINED,
        "signal_id": "signal-1",
        "intent": None,
        "reason_code": "declined",
        "planner_name": "planner",
        "planner_version": "1",
    }
    for field in ("reason_code", "planner_name", "planner_version"):
        with pytest.raises(IntentPlannerInvariantError):
            IntentPlanResult(**{**common, field: " "})
    with pytest.raises(IntentPlannerInvariantError):
        IntentPlanResult(**common, metadata={"nested": {"mutable": True}})


def test_planner_has_no_portfolio_time_or_io_dependencies() -> None:
    subject, _ = planner()

    assert set(vars(subject)) == {
        "_config",
        "_intent_id_factory",
        "_configuration",
    }


def test_corrupted_signal_direction_is_rejected_not_declined() -> None:
    subject, _ = planner()
    source = signal(SignalDirection.HOLD)
    object.__setattr__(source, "direction", "unknown")

    with pytest.raises(IntentPlannerInvariantError, match="Unsupported"):
        subject.plan(source)
