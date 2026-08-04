from collections.abc import Callable
from decimal import Decimal

import pytest

from app.market.cache import InMemoryMarketCache
from app.market.events import CandleClosed, MarketEventDispatcher, MarketEventProjector
from app.market.models import Candle, MarketStreamKey, ServerTime, TickerPrice
from app.market.polling import PollCycleOutcome, SynchronousRestPollingEngine
from app.strategies.ema_crossover import EmaCrossoverConfig, EmaCrossoverStrategy
from app.trading.intent import LongOnlyIntentPlanner, LongOnlyIntentPlannerConfig
from app.trading.models import OrderIntentAction, SignalDirection
from app.trading.pipeline import PipelineOutcome, TradingDecisionPipeline
from app.trading.snapshot import MarketSnapshotFactory

OPEN_TIME_MS = 1_700_000_000_000
INTERVAL_MS = 60_000
KEY = MarketStreamKey("BTCUSDT", "1m")


class SequentialIdFactory:
    def __init__(self, prefix: str) -> None:
        self._prefix = prefix
        self._value = 0

    def next_id(self) -> str:
        self._value += 1
        return f"{self._prefix}-{self._value}"

    def new_intent_id(self) -> str:
        return self.next_id()

    def new_pipeline_run_id(self) -> str:
        return self.next_id()


class FixedClock:
    def now_ms(self) -> int:
        return OPEN_TIME_MS

    def monotonic(self) -> float:
        return 0.0


class NoWaitSleeper:
    def wait(self, seconds: float, stop_requested: Callable[[], bool]) -> bool:
        del seconds
        return not stop_requested()


class FlowProvider:
    def __init__(self, candles: list[Candle]) -> None:
        self._candles = candles
        self._times = [candles[-1].close_time_ms, candles[-1].close_time_ms + 1]
        self._cycle = 0

    def get_server_time(self) -> ServerTime:
        value = self._times[self._cycle]
        self._cycle += 1
        return ServerTime(timestamp_ms=value)

    def get_ticker_price(self, symbol: str) -> TickerPrice:
        return TickerPrice(symbol=symbol, price=self._candles[-1].close)

    def get_candles(self, symbol: str, interval: str, limit: int) -> list[Candle]:
        del symbol, interval
        selected = self._candles if limit > 2 else self._candles[-2:]
        return [candle.model_copy(deep=True) for candle in selected]

    def close(self) -> None:
        raise AssertionError("Polling Engine must not close its provider")


def candles_with_closes(closes: list[int]) -> list[Candle]:
    result: list[Candle] = []
    for index, close_value in enumerate(closes):
        price = Decimal(close_value)
        open_time_ms = OPEN_TIME_MS + index * INTERVAL_MS
        result.append(
            Candle(
                open_time_ms=open_time_ms,
                close_time_ms=open_time_ms + INTERVAL_MS - 1,
                open=price,
                high=price + 1,
                low=max(Decimal("0.1"), price - 1),
                close=price,
                volume=Decimal(10),
            )
        )
    return result


@pytest.mark.parametrize(
    ("closes", "stale", "direction", "outcome", "action", "exposure"),
    [
        (
            [3, 2, 1, 4],
            False,
            SignalDirection.BUY,
            PipelineOutcome.INTENT_CREATED,
            OrderIntentAction.ENTER_LONG,
            Decimal("0.4"),
        ),
        (
            [2, 3, 4, 1],
            False,
            SignalDirection.SELL,
            PipelineOutcome.INTENT_CREATED,
            OrderIntentAction.EXIT_LONG,
            Decimal(0),
        ),
        (
            [3, 2, 1, 4, 5],
            False,
            SignalDirection.HOLD,
            PipelineOutcome.HOLD,
            None,
            None,
        ),
        (
            [3, 2, 1, 4],
            True,
            SignalDirection.HOLD,
            PipelineOutcome.HOLD,
            None,
            None,
        ),
    ],
)
def test_complete_offline_market_to_decision_flow(
    closes: list[int],
    stale: bool,
    direction: SignalDirection,
    outcome: PipelineOutcome,
    action: OrderIntentAction | None,
    exposure: Decimal | None,
) -> None:
    candles = candles_with_closes(closes)
    provider = FlowProvider(candles)
    cache = InMemoryMarketCache()
    dispatcher = MarketEventDispatcher()
    closed_events: list[CandleClosed] = []
    dispatcher.subscribe(CandleClosed, closed_events.append)
    engine = SynchronousRestPollingEngine(
        provider,
        cache,
        KEY,
        clock=FixedClock(),
        sleeper=NoWaitSleeper(),
        dispatcher=dispatcher,
        projector=MarketEventProjector(SequentialIdFactory("event")),
        correlation_id_factory=SequentialIdFactory("cycle"),
    )

    bootstrap = engine.run_once()
    regular = engine.run_once()

    assert bootstrap.outcome is regular.outcome is PollCycleOutcome.SUCCESS
    assert len(closed_events) == 1
    assert closed_events[0].candle.open_time_ms == candles[-1].open_time_ms
    cache_snapshot = cache.get_snapshot(KEY)
    original_close = cache_snapshot[-1].close
    cache_snapshot[-1].close = Decimal(999)
    assert cache.get_latest_candle(KEY).close == original_close  # type: ignore[union-attr]

    as_of_ms = candles[-1].close_time_ms + (100 if stale else 1)
    snapshot = MarketSnapshotFactory(1 if stale else 120_000).create(
        KEY,
        cache.get_snapshot(KEY),
        as_of_ms,
        "snapshot-1",
        source_event_id=closed_events[0].event_id,
        correlation_cycle_id=closed_events[0].correlation_cycle_id,
    )
    strategy_config = EmaCrossoverConfig(fast_window=2, slow_window=3)
    planner_config = LongOnlyIntentPlannerConfig(long_target_exposure=Decimal("0.4"))
    pipeline = TradingDecisionPipeline(
        EmaCrossoverStrategy(strategy_config, SequentialIdFactory("signal")),
        LongOnlyIntentPlanner(planner_config, SequentialIdFactory("intent")),
        SequentialIdFactory("run"),
    )

    result = pipeline.evaluate(snapshot)

    assert result.signal.direction is direction
    assert result.outcome is outcome
    assert strategy_config == EmaCrossoverConfig(fast_window=2, slow_window=3)
    assert planner_config.long_target_exposure == Decimal("0.4")
    if action is None:
        assert result.order_intent is None
    else:
        assert result.order_intent is not None
        assert result.order_intent.action is action
        assert result.order_intent.target_exposure == exposure
