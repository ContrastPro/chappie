from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from app.market.models import Candle, CandleSnapshot, MarketStreamKey
from app.trading.exceptions import (
    InconsistentTradingTimestampError,
    InvalidMarketSnapshotError,
    InvalidOrderIntentError,
    InvalidRiskDecisionError,
    InvalidSignalError,
)
from app.trading.models import (
    MarketSnapshot,
    OrderIntent,
    OrderIntentAction,
    RiskDecision,
    RiskDecisionOutcome,
    Signal,
    SignalDirection,
)
from app.trading.snapshot import MarketSnapshotFactory

KEY = MarketStreamKey("BTCUSDT", "1m")
OPEN_MS = 1_700_000_000_000
INTERVAL_MS = 60_000


def candle(index: int, close: str = "100") -> Candle:
    open_ms = OPEN_MS + index * INTERVAL_MS
    price = Decimal(close)
    return Candle(
        open_time_ms=open_ms,
        close_time_ms=open_ms + INTERVAL_MS - 1,
        open=price,
        high=price + 1,
        low=price - 1,
        close=price,
        volume=Decimal(10),
    )


def snapshot_factory(stale_after_ms: int = 120_000) -> MarketSnapshotFactory:
    return MarketSnapshotFactory(stale_after_ms)


def test_empty_snapshot_is_stale_without_latest_candles() -> None:
    snapshot = snapshot_factory().create(KEY, (), OPEN_MS, "snapshot-1")

    assert snapshot.closed_candles == ()
    assert snapshot.latest_closed_candle is None
    assert snapshot.latest_open_candle is None
    assert snapshot.staleness_ms is None
    assert snapshot.is_stale is True


def test_factory_separates_closed_history_and_final_open_candle() -> None:
    closed = candle(0)
    current = candle(1)
    as_of_ms = current.close_time_ms

    snapshot = snapshot_factory().create(
        KEY,
        (closed, current),
        as_of_ms,
        "snapshot-1",
        source_event_id="event-1",
        correlation_cycle_id="cycle-1",
    )

    assert snapshot.closed_candles == (CandleSnapshot.from_candle(closed),)
    assert snapshot.latest_closed_candle == CandleSnapshot.from_candle(closed)
    assert snapshot.latest_open_candle == CandleSnapshot.from_candle(current)
    assert snapshot.latest_open_candle not in snapshot.closed_candles
    assert snapshot.source_event_id == "event-1"
    assert snapshot.correlation_cycle_id == "cycle-1"


def test_factory_defensively_copies_mutable_candles() -> None:
    source = candle(0)
    snapshot = snapshot_factory().create(
        KEY,
        [source],
        source.close_time_ms + 1,
        "snapshot-1",
    )

    source.close = Decimal(999)

    assert snapshot.closed_candles[0].close == Decimal(100)
    with pytest.raises(FrozenInstanceError):
        snapshot.closed_candles[0].close = Decimal(5)  # type: ignore[misc]


def test_factory_rejects_invalid_order_and_multiple_open_candles() -> None:
    first = candle(0)
    second = candle(1)
    with pytest.raises(InvalidMarketSnapshotError, match="strictly ordered"):
        snapshot_factory().create(
            KEY,
            (second, first),
            second.close_time_ms + 1,
            "snapshot-1",
        )
    with pytest.raises(InvalidMarketSnapshotError, match="Only the final"):
        snapshot_factory().create(
            KEY,
            (first, second),
            first.open_time_ms,
            "snapshot-1",
        )


def test_snapshot_rejects_duplicate_and_overlapping_ranges() -> None:
    first = CandleSnapshot.from_candle(candle(0))
    duplicate = CandleSnapshot.from_candle(candle(0))
    overlapping = CandleSnapshot(
        open_time_ms=first.close_time_ms,
        close_time_ms=first.close_time_ms + INTERVAL_MS,
        open=Decimal(100),
        high=Decimal(101),
        low=Decimal(99),
        close=Decimal(100),
        volume=Decimal(10),
    )
    as_of_ms = overlapping.close_time_ms + 1

    with pytest.raises(InvalidMarketSnapshotError, match="strictly ordered"):
        snapshot_factory().create(KEY, (first, duplicate), as_of_ms, "snapshot-1")
    with pytest.raises(InvalidMarketSnapshotError, match="overlap"):
        snapshot_factory().create(KEY, (first, overlapping), as_of_ms, "snapshot-1")


def test_snapshot_close_boundary_matches_market_semantics() -> None:
    source = candle(0)

    at_boundary = snapshot_factory().create(
        KEY,
        (source,),
        source.close_time_ms,
        "open-snapshot",
    )
    after_boundary = snapshot_factory().create(
        KEY,
        (source,),
        source.close_time_ms + 1,
        "closed-snapshot",
    )

    assert at_boundary.latest_open_candle == CandleSnapshot.from_candle(source)
    assert at_boundary.closed_candles == ()
    assert after_boundary.latest_open_candle is None
    assert after_boundary.closed_candles == (CandleSnapshot.from_candle(source),)

def test_snapshot_rejects_inconsistent_latest_closed_candle() -> None:
    closed = CandleSnapshot.from_candle(candle(0))
    with pytest.raises(InvalidMarketSnapshotError, match="must match"):
        MarketSnapshot(
            snapshot_id="snapshot-1",
            stream_key=KEY,
            as_of_ms=closed.close_time_ms + 1,
            closed_candles=(closed,),
            latest_closed_candle=None,
            latest_open_candle=None,
            source_event_id=None,
            correlation_cycle_id=None,
            data_version="market-snapshot/1",
            staleness_ms=1,
            is_stale=False,
        )


@pytest.mark.parametrize(
    ("age_ms", "expected_stale"),
    [(120_000, False), (120_001, True)],
)
def test_factory_calculates_staleness_from_explicit_as_of_time(
    age_ms: int,
    expected_stale: bool,
) -> None:
    source = candle(0)
    snapshot = snapshot_factory().create(
        KEY,
        (source,),
        source.close_time_ms + age_ms,
        "snapshot-1",
    )

    assert snapshot.staleness_ms == age_ms
    assert snapshot.is_stale is expected_stale


def test_snapshot_factory_has_no_history_between_calls() -> None:
    factory = snapshot_factory()
    source = candle(0)
    arguments = (KEY, (source,), source.close_time_ms + 1, "snapshot-1")

    assert factory.create(*arguments) == factory.create(*arguments)


def make_signal(**overrides: object) -> Signal:
    values = {
        "signal_id": "signal-1",
        "snapshot_id": "snapshot-1",
        "stream_key": KEY,
        "created_at_ms": OPEN_MS,
        "effective_at_ms": OPEN_MS,
        "direction": SignalDirection.HOLD,
        "reason_code": "no_crossover",
        "explanation": "No crossover",
        "strategy_name": "ema_crossover",
        "strategy_version": "1.0.0",
        "strategy_parameters": {"fast_window": 2},
        "metadata": {"values": [Decimal(1), Decimal(2)]},
        **overrides,
    }
    return Signal(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("direction", "reason"),
    [
        (SignalDirection.BUY, "bullish_crossover"),
        (SignalDirection.SELL, "bearish_crossover"),
        (SignalDirection.HOLD, "no_crossover"),
    ],
)
def test_signal_supports_buy_sell_and_hold(
    direction: SignalDirection,
    reason: str,
) -> None:
    assert make_signal(direction=direction, reason_code=reason).direction is direction


def test_signal_freezes_parameters_and_metadata() -> None:
    parameters = {"fast_window": 2}
    metadata = {"values": [Decimal(1), Decimal(2)]}
    signal = make_signal(strategy_parameters=parameters, metadata=metadata)
    parameters["fast_window"] = 99
    metadata["values"].append(Decimal(3))

    assert signal.strategy_parameters["fast_window"] == 2
    assert signal.metadata["values"] == (Decimal(1), Decimal(2))
    with pytest.raises(TypeError):
        signal.metadata["new"] = 1  # type: ignore[index]


@pytest.mark.parametrize("field", ["signal_id", "snapshot_id", "reason_code", "strategy_name", "strategy_version"])
def test_signal_rejects_empty_identity_fields(field: str) -> None:
    with pytest.raises(InvalidSignalError):
        make_signal(**{field: ""})


def test_signal_rejects_inconsistent_timestamps() -> None:
    with pytest.raises(InconsistentTradingTimestampError):
        make_signal(created_at_ms=1, effective_at_ms=2)
    with pytest.raises(InconsistentTradingTimestampError):
        make_signal(created_at_ms=2, effective_at_ms=1)


@pytest.mark.parametrize(
    ("direction", "reason"),
    [
        (SignalDirection.BUY, "no_crossover"),
        (SignalDirection.SELL, "stale_snapshot"),
        (SignalDirection.HOLD, "bullish_crossover"),
        (SignalDirection.HOLD, "bearish_crossover"),
    ],
)
def test_signal_rejects_direction_reason_mismatch(
    direction: SignalDirection,
    reason: str,
) -> None:
    with pytest.raises(InvalidSignalError):
        make_signal(direction=direction, reason_code=reason)


def make_intent(**overrides: object) -> OrderIntent:
    values = {
        "intent_id": "intent-1",
        "source_signal_id": "signal-1",
        "stream_key": KEY,
        "created_at_ms": OPEN_MS,
        "action": OrderIntentAction.ENTER_LONG,
        "target_exposure": Decimal("0.5"),
        "reason": "bullish crossover",
        "strategy_name": "ema_crossover",
        "strategy_version": "1.0.0",
        **overrides,
    }
    return OrderIntent(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("exposure", [Decimal("0.1"), Decimal(1)])
def test_enter_long_accepts_positive_target_exposure(exposure: Decimal) -> None:
    assert make_intent(target_exposure=exposure).target_exposure == exposure


def test_exit_long_requires_zero_target_exposure() -> None:
    intent = make_intent(
        action=OrderIntentAction.EXIT_LONG,
        target_exposure=Decimal(0),
    )
    assert intent.target_exposure == 0

    with pytest.raises(InvalidOrderIntentError):
        make_intent(action=OrderIntentAction.EXIT_LONG, target_exposure=Decimal("0.1"))


def test_negative_zero_is_treated_as_zero_exposure() -> None:
    intent = make_intent(
        action=OrderIntentAction.EXIT_LONG,
        target_exposure=Decimal("-0"),
    )
    assert intent.target_exposure == Decimal(0)


@pytest.mark.parametrize(
    "exposure",
    [Decimal("-0.1"), Decimal("1.1"), Decimal("NaN"), Decimal("Infinity")],
)
def test_order_intent_rejects_invalid_exposure(exposure: Decimal) -> None:
    with pytest.raises(InvalidOrderIntentError):
        make_intent(target_exposure=exposure)


def test_enter_long_rejects_zero_and_is_immutable() -> None:
    with pytest.raises(InvalidOrderIntentError):
        make_intent(target_exposure=Decimal(0))
    intent = make_intent()
    with pytest.raises(FrozenInstanceError):
        intent.target_exposure = Decimal(1)  # type: ignore[misc]


def make_risk_decision(**overrides: object) -> RiskDecision:
    values = {
        "decision_id": "decision-1",
        "intent_id": "intent-1",
        "decided_at_ms": OPEN_MS,
        "outcome": RiskDecisionOutcome.APPROVED,
        "requested_target_exposure": Decimal("0.5"),
        "approved_target_exposure": Decimal("0.5"),
        "rejection_reasons": (),
        "applied_limits": {"max_exposure": Decimal(1)},
        "risk_policy_version": "risk/1",
        **overrides,
    }
    return RiskDecision(**values)  # type: ignore[arg-type]


def test_risk_decision_approved_rejected_and_modified() -> None:
    assert make_risk_decision().outcome is RiskDecisionOutcome.APPROVED
    rejected = make_risk_decision(
        outcome=RiskDecisionOutcome.REJECTED,
        approved_target_exposure=None,
        rejection_reasons=("daily_limit",),
    )
    modified = make_risk_decision(
        outcome=RiskDecisionOutcome.MODIFIED,
        approved_target_exposure=Decimal("0.25"),
        applied_limits={"position_limit": Decimal("0.25")},
    )
    assert rejected.approved_target_exposure is None
    assert modified.approved_target_exposure == Decimal("0.25")


def test_risk_decision_rejects_inconsistent_outcomes() -> None:
    with pytest.raises(InvalidRiskDecisionError):
        make_risk_decision(
            outcome=RiskDecisionOutcome.REJECTED,
            approved_target_exposure=None,
        )
    with pytest.raises(InvalidRiskDecisionError):
        make_risk_decision(
            outcome=RiskDecisionOutcome.MODIFIED,
            approved_target_exposure=Decimal("0.5"),
        )
    with pytest.raises(InvalidRiskDecisionError):
        make_risk_decision(approved_target_exposure=Decimal("1.1"))
    with pytest.raises(InvalidRiskDecisionError):
        make_risk_decision(
            outcome=RiskDecisionOutcome.MODIFIED,
            approved_target_exposure=Decimal("0.25"),
            rejection_reasons=("not-a-rejection",),
        )


def test_risk_decision_copies_limits_and_is_immutable() -> None:
    limits = {"rules": ["position", "daily"]}
    decision = make_risk_decision(applied_limits=limits)
    limits["rules"].append("other")

    assert decision.applied_limits["rules"] == ("position", "daily")
    with pytest.raises(FrozenInstanceError):
        decision.outcome = RiskDecisionOutcome.REJECTED  # type: ignore[misc]
    with pytest.raises(TypeError):
        decision.applied_limits["other"] = Decimal(1)  # type: ignore[index]


def test_nested_mappings_are_rejected_instead_of_shallowly_wrapped() -> None:
    with pytest.raises(InvalidSignalError, match="immutable"):
        make_signal(metadata={"nested": {"mutable": "mapping"}})
