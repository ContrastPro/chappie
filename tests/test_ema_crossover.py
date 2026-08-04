from dataclasses import FrozenInstanceError
from decimal import Decimal, getcontext, localcontext

import pytest

from app.market.models import Candle, MarketStreamKey
from app.strategies.ema_crossover import (
    EmaCrossoverConfig,
    EmaCrossoverStrategy,
    calculate_ema,
)
from app.trading.exceptions import (
    InvalidDecimalValueError,
    InvalidStrategyConfigurationError,
)
from app.trading.models import SignalDirection
from app.trading.snapshot import MarketSnapshotFactory
from app.trading.strategy import Strategy

KEY = MarketStreamKey("BTCUSDT", "1m")
OPEN_MS = 1_700_000_000_000


class SequentialSignalIdFactory:
    def __init__(self) -> None:
        self.value = 0

    def next_id(self) -> str:
        self.value += 1
        return f"signal-{self.value}"


def candles(closes: list[int], *, include_open: bool = False) -> tuple[Candle, ...]:
    result: list[Candle] = []
    for index, close_value in enumerate(closes):
        open_ms = OPEN_MS + index * 60_000
        close = Decimal(close_value)
        result.append(
            Candle(
                open_time_ms=open_ms,
                close_time_ms=open_ms + 59_999,
                open=close,
                high=close + 1,
                low=max(Decimal("0.1"), close - 1),
                close=close,
                volume=Decimal(10),
            )
        )
    if include_open:
        open_ms = OPEN_MS + len(closes) * 60_000
        result.append(
            Candle(
                open_time_ms=open_ms,
                close_time_ms=open_ms + 59_999,
                open=Decimal(999),
                high=Decimal(1000),
                low=Decimal(998),
                close=Decimal(999),
                volume=Decimal(10),
            )
        )
    return tuple(result)


def make_snapshot(
    closes: list[int],
    *,
    stale: bool = False,
    include_open: bool = False,
    snapshot_id: str = "snapshot-1",
):
    values = candles(closes, include_open=include_open)
    if values:
        if include_open:
            as_of_ms = values[-1].close_time_ms
        else:
            as_of_ms = values[-1].close_time_ms + 1
    else:
        as_of_ms = OPEN_MS
    stale_after = 1 if stale else 120_000
    if stale and values:
        as_of_ms = values[-1].close_time_ms + 2
    return MarketSnapshotFactory(stale_after).create(
        KEY,
        values,
        as_of_ms,
        snapshot_id,
        correlation_cycle_id="cycle-1",
    )


def strategy() -> EmaCrossoverStrategy:
    return EmaCrossoverStrategy(
        EmaCrossoverConfig(fast_window=2, slow_window=3),
        SequentialSignalIdFactory(),
    )


def test_config_defaults_and_required_history() -> None:
    default = EmaCrossoverConfig()
    custom = strategy()

    assert (default.fast_window, default.slow_window) == (12, 26)
    assert (default.strategy_name, default.strategy_version) == (
        "ema_crossover",
        "1.0.0",
    )
    assert custom.required_history == 4
    with pytest.raises(FrozenInstanceError):
        default.fast_window = 5  # type: ignore[misc]


@pytest.mark.parametrize(
    ("fast", "slow"),
    [(1, 3), (0, 3), (-1, 3), (3, 3), (4, 3), (True, 3)],
)
def test_config_rejects_invalid_windows(fast: int, slow: int) -> None:
    with pytest.raises(InvalidStrategyConfigurationError):
        EmaCrossoverConfig(fast_window=fast, slow_window=slow)


def test_calculate_ema_uses_exact_decimal_sma_seed_and_recursion() -> None:
    result = calculate_ema(
        (Decimal(1), Decimal(2), Decimal(3), Decimal(4)),
        3,
    )

    assert result == (Decimal(2), Decimal(3))
    assert all(isinstance(value, Decimal) for value in result)


def test_manual_ema_control_values_for_two_windows() -> None:
    values = tuple(Decimal(value) for value in (1, 2, 3, 4))

    assert calculate_ema(values, 2) == (
        Decimal("1.5"),
        Decimal("2.5"),
        Decimal("3.5"),
    )
    assert calculate_ema(values, 3) == (Decimal(2), Decimal(3))


def test_ema_uses_fixed_local_precision_without_mutating_global_context() -> None:
    values = tuple(Decimal(value) for value in (1, 2, 4, 8, 16))
    original_precision = getcontext().prec
    with localcontext() as low_precision:
        low_precision.prec = 6
        low = calculate_ema(values, 3)
    with localcontext() as high_precision:
        high_precision.prec = 50
        high = calculate_ema(values, 3)

    assert low == high
    assert getcontext().prec == original_precision


def test_calculate_ema_rejects_float_and_non_finite_values() -> None:
    with pytest.raises(InvalidDecimalValueError):
        calculate_ema((Decimal(1), 2.0, Decimal(3)), 2)  # type: ignore[arg-type]
    with pytest.raises(InvalidDecimalValueError):
        calculate_ema((Decimal(1), Decimal("NaN")), 2)


@pytest.mark.parametrize(
    ("closes", "direction", "reason"),
    [
        ([3, 2, 1, 4], SignalDirection.BUY, "bullish_crossover"),
        ([1, 2, 3, 0], SignalDirection.SELL, "bearish_crossover"),
        ([3, 2, 1, 3], SignalDirection.HOLD, "equal_emas"),
        ([3, 2, 1, 4, 5], SignalDirection.HOLD, "no_crossover"),
        ([1, 2, 3, 0, 0], SignalDirection.HOLD, "no_crossover"),
        ([1, 1, 1, 2], SignalDirection.BUY, "bullish_crossover"),
        ([1, 1, 1, 0], SignalDirection.SELL, "bearish_crossover"),
    ],
)
def test_exact_crossover_semantics(
    closes: list[int],
    direction: SignalDirection,
    reason: str,
) -> None:
    signal = strategy().generate_signal(make_snapshot(closes))

    assert signal.direction is direction
    assert signal.reason_code == reason
    assert "fast_window=2" in signal.explanation
    assert "slow_window=3" in signal.explanation
    assert "previous_fast=" in signal.explanation
    assert "current_slow=" in signal.explanation


def test_signal_uses_the_last_two_exact_ema_points() -> None:
    signal = strategy().generate_signal(make_snapshot([1, 1, 1, 2]))

    assert signal.metadata == {
        "previous_fast": Decimal(1),
        "previous_slow": Decimal(1),
        "current_fast": Decimal("1.666666666666666666666666666"),
        "current_slow": Decimal("1.5"),
    }


def test_empty_insufficient_and_stale_snapshots_return_explained_hold() -> None:
    baseline = strategy()

    empty = baseline.generate_signal(make_snapshot([]))
    insufficient = baseline.generate_signal(make_snapshot([1, 2, 3]))
    stale = baseline.generate_signal(make_snapshot([3, 2, 1, 4], stale=True))

    assert (empty.direction, empty.reason_code) == (
        SignalDirection.HOLD,
        "no_closed_candles",
    )
    assert insufficient.reason_code == "insufficient_history"
    assert stale.reason_code == "stale_snapshot"


def test_strategy_uses_exact_minimum_and_longer_history() -> None:
    minimum = strategy().generate_signal(make_snapshot([3, 2, 1, 4]))
    longer = strategy().generate_signal(make_snapshot([9, 8, 3, 2, 1, 4]))

    assert minimum.direction is SignalDirection.BUY
    assert longer.reason_code not in {"insufficient_history", "no_closed_candles"}
    assert "previous_fast=" in longer.explanation


def test_strategy_ignores_open_candle_and_does_not_mutate_snapshot() -> None:
    baseline = strategy()
    without_open = make_snapshot([3, 2, 1, 4])
    with_open = make_snapshot([3, 2, 1, 4], include_open=True)
    original_closed = with_open.closed_candles

    first = baseline.generate_signal(without_open)
    second = baseline.generate_signal(with_open)

    assert first.direction is second.direction is SignalDirection.BUY
    assert with_open.closed_candles == original_closed
    assert with_open.latest_open_candle is not None


def test_open_candle_ohlcv_and_presence_do_not_affect_signal_or_staleness() -> None:
    baseline_without_open = strategy()
    baseline_with_open = strategy()
    closed_only = make_snapshot([3, 2, 1, 4], snapshot_id="closed")
    with_open = make_snapshot(
        [3, 2, 1, 4],
        include_open=True,
        snapshot_id="with-open",
    )

    without_signal = baseline_without_open.generate_signal(closed_only)
    with_signal = baseline_with_open.generate_signal(with_open)

    assert without_signal.direction == with_signal.direction
    assert without_signal.reason_code == with_signal.reason_code
    assert without_signal.metadata == with_signal.metadata
    assert closed_only.staleness_ms == 1
    assert with_open.staleness_ms == 60_000
    assert with_open.staleness_ms == (
        with_open.as_of_ms - with_open.latest_closed_candle.close_time_ms
    )


def test_changing_only_open_candle_ohlcv_does_not_change_signal() -> None:
    first_values = list(candles([3, 2, 1, 4], include_open=True))
    second_values = list(candles([3, 2, 1, 4], include_open=True))
    second_values[-1].open = Decimal(5)
    second_values[-1].high = Decimal(8)
    second_values[-1].low = Decimal(2)
    second_values[-1].close = Decimal(7)
    second_values[-1].volume = Decimal(999)
    as_of_ms = first_values[-1].close_time_ms
    factory = MarketSnapshotFactory(120_000)
    first_snapshot = factory.create(KEY, first_values, as_of_ms, "snapshot")
    second_snapshot = factory.create(KEY, second_values, as_of_ms, "snapshot")

    first_signal = strategy().generate_signal(first_snapshot)
    second_signal = strategy().generate_signal(second_snapshot)

    assert first_signal == second_signal


def test_strategy_uses_deterministic_ids_and_exchange_time() -> None:
    factory = SequentialSignalIdFactory()
    baseline = EmaCrossoverStrategy(
        EmaCrossoverConfig(fast_window=2, slow_window=3),
        factory,
    )
    snapshot = make_snapshot([3, 2, 1, 4])

    first = baseline.generate_signal(snapshot)
    second = baseline.generate_signal(snapshot)

    assert (first.signal_id, second.signal_id) == ("signal-1", "signal-2")
    assert first.created_at_ms == first.effective_at_ms == snapshot.as_of_ms
    assert first.snapshot_id == snapshot.snapshot_id
    assert first.correlation_id == "cycle-1"
    assert first.direction == second.direction
    assert first.metadata == second.metadata


def test_same_inputs_and_equivalent_factories_produce_identical_signal() -> None:
    snapshot = make_snapshot([3, 2, 1, 4])
    left = strategy().generate_signal(snapshot)
    right = strategy().generate_signal(snapshot)

    assert left == right


def test_long_history_uses_warmup_before_minimum_suffix() -> None:
    full = strategy().generate_signal(make_snapshot([9, 8, 3, 2, 1, 4]))
    suffix = strategy().generate_signal(make_snapshot([3, 2, 1, 4]))

    assert full.metadata != suffix.metadata


class FailOnceSignalIdFactory:
    def __init__(self) -> None:
        self.failed = False

    def next_id(self) -> str:
        if not self.failed:
            self.failed = True
            raise RuntimeError("id factory failed")
        return "signal-after-failure"


def test_id_factory_failure_does_not_change_strategy_state() -> None:
    baseline = EmaCrossoverStrategy(
        EmaCrossoverConfig(fast_window=2, slow_window=3),
        FailOnceSignalIdFactory(),
    )
    snapshot = make_snapshot([3, 2, 1, 4])

    with pytest.raises(RuntimeError, match="id factory failed"):
        baseline.generate_signal(snapshot)
    signal = baseline.generate_signal(snapshot)

    assert signal.signal_id == "signal-after-failure"
    assert signal.direction is SignalDirection.BUY


def test_strategy_is_reusable_across_streams_without_hidden_state() -> None:
    baseline = strategy()
    first = make_snapshot([3, 2, 1, 4])
    other_key = MarketStreamKey("ETHUSDT", "1m")
    source = candles([1, 1, 1, 0])
    other = MarketSnapshotFactory(120_000).create(
        other_key,
        source,
        source[-1].close_time_ms + 1,
        "eth-snapshot",
    )

    first_signal = baseline.generate_signal(first)
    other_signal = baseline.generate_signal(other)
    repeated = baseline.generate_signal(first)

    assert first_signal.direction is repeated.direction is SignalDirection.BUY
    assert other_signal.direction is SignalDirection.SELL
    assert other_signal.stream_key == other_key


def test_ema_strategy_conforms_to_public_strategy_protocol() -> None:
    baseline = strategy()

    assert isinstance(baseline, Strategy)
    assert baseline.name == "ema_crossover"
    assert baseline.version == "1.0.0"
    assert baseline.required_history == 4
    with pytest.raises(TypeError):
        baseline.configuration["fast_window"] = 99  # type: ignore[index]
