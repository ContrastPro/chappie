from collections.abc import Callable
from decimal import Decimal

import pytest

from app.market.cache import InMemoryMarketCache
from app.market.events import (
    CandleClosed,
    CandleEvicted,
    CandleInserted,
    CandleUpdated,
    MarketEvent,
    MarketEventDispatcher,
    MarketEventProjector,
)
from app.market.models import Candle, MarketStreamKey, ServerTime, TickerPrice
from app.market.polling import PollCycleOutcome, SynchronousRestPollingEngine
from app.shadow.engine import ShadowTradingDecisionEngine
from app.shadow.models import ShadowDecisionStatus, ShadowObservationStatus
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

    def new_snapshot_id(self) -> str:
        return self.next_id()

    def new_shadow_run_id(self) -> str:
        return self.next_id()


class RecordingObserver:
    def __init__(self) -> None:
        self.records = []

    def on_decision(self, record) -> None:
        self.records.append(record)


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


class FinalizingFlowProvider:
    def __init__(
        self,
        bootstrap: list[Candle],
        finalized: Candle,
        next_open: Candle,
    ) -> None:
        self.bootstrap = bootstrap
        self.regular = [finalized, next_open]
        self.times = [bootstrap[-1].close_time_ms, next_open.open_time_ms]
        self.candle_calls = 0
        self.close_calls = 0

    def get_server_time(self) -> ServerTime:
        return ServerTime(timestamp_ms=self.times[self.candle_calls])

    def get_ticker_price(self, symbol: str) -> TickerPrice:
        return TickerPrice(symbol=symbol, price=self.regular[-1].close)

    def get_candles(self, symbol: str, interval: str, limit: int) -> list[Candle]:
        del symbol, interval, limit
        source = self.bootstrap if self.candle_calls == 0 else self.regular
        self.candle_calls += 1
        return [item.model_copy(deep=True) for item in source]

    def close(self) -> None:
        self.close_calls += 1


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


@pytest.mark.parametrize(
    ("closes", "direction", "outcome", "action", "exposure"),
    [
        (
            [3, 2, 1, 4],
            SignalDirection.BUY,
            PipelineOutcome.INTENT_CREATED,
            OrderIntentAction.ENTER_LONG,
            Decimal("0.4"),
        ),
        (
            [2, 3, 4, 1],
            SignalDirection.SELL,
            PipelineOutcome.INTENT_CREATED,
            OrderIntentAction.EXIT_LONG,
            Decimal(0),
        ),
        (
            [3, 2, 1, 4, 5],
            SignalDirection.HOLD,
            PipelineOutcome.HOLD,
            None,
            None,
        ),
    ],
)
def test_polling_to_shadow_runtime_uses_only_new_candle_closed(
    closes: list[int],
    direction: SignalDirection,
    outcome: PipelineOutcome,
    action: OrderIntentAction | None,
    exposure: Decimal | None,
) -> None:
    candles = candles_with_closes(closes)
    provider = FlowProvider(candles)
    cache = InMemoryMarketCache()
    dispatcher = MarketEventDispatcher()
    ids = SequentialIdFactory("shadow")
    observer = RecordingObserver()
    shadow = ShadowTradingDecisionEngine(
        cache,
        MarketSnapshotFactory(120_000),
        TradingDecisionPipeline(
            EmaCrossoverStrategy(EmaCrossoverConfig(2, 3), ids),
            LongOnlyIntentPlanner(
                LongOnlyIntentPlannerConfig(Decimal("0.4")),
                ids,
            ),
            ids,
        ),
        ids,
        ids,
        observer,
        history_limit=1000,
        dedup_capacity=100,
        monotonic_clock=lambda: 0.0,
    )
    shadow.start(dispatcher)
    polling = SynchronousRestPollingEngine(
        provider,
        cache,
        KEY,
        clock=FixedClock(),
        sleeper=NoWaitSleeper(),
        dispatcher=dispatcher,
        projector=MarketEventProjector(SequentialIdFactory("event")),
        correlation_id_factory=SequentialIdFactory("cycle"),
    )

    assert polling.run_once().outcome is PollCycleOutcome.SUCCESS
    assert observer.records == []  # bootstrap history is not replayed
    assert polling.run_once().outcome is PollCycleOutcome.SUCCESS

    assert len(observer.records) == 1
    record = observer.records[0]
    assert record.decision_status is ShadowDecisionStatus.SUCCEEDED
    assert record.observation_status is ShadowObservationStatus.OBSERVED
    assert record.signal_direction is direction
    assert record.pipeline_outcome is outcome
    assert record.source_event_id.startswith("event-")
    assert record.snapshot_id
    assert record.shadow_run_id
    assert record.pipeline_run_id
    assert record.signal_id
    if action is None:
        assert record.intent_id is record.intent_action is record.target_exposure is None
    else:
        assert record.intent_id
        assert record.intent_action is action
        assert record.target_exposure == exposure


def test_bootstrap_open_tail_finalizes_atomically_and_reaches_shadow() -> None:
    bootstrap = candles_with_closes([100] * 1000)
    stored_open = bootstrap[-1]
    finalized = stored_open.model_copy(
        update={
            "high": Decimal(112),
            "low": Decimal(98),
            "close": Decimal(108),
            "volume": Decimal(12),
        }
    )
    next_open = Candle(
        open_time_ms=stored_open.open_time_ms + INTERVAL_MS,
        close_time_ms=stored_open.close_time_ms + INTERVAL_MS,
        open=Decimal(108),
        high=Decimal(109),
        low=Decimal(107),
        close=Decimal("108.5"),
        volume=Decimal(1),
    )
    provider = FinalizingFlowProvider(bootstrap, finalized, next_open)
    cache = InMemoryMarketCache(capacity=1000)
    dispatcher = MarketEventDispatcher()
    events: list[MarketEvent] = []
    dispatcher.subscribe(MarketEvent, events.append)
    ids = SequentialIdFactory("shadow")
    observer = RecordingObserver()
    shadow = ShadowTradingDecisionEngine(
        cache,
        MarketSnapshotFactory(120_000),
        TradingDecisionPipeline(
            EmaCrossoverStrategy(EmaCrossoverConfig(), ids),
            LongOnlyIntentPlanner(LongOnlyIntentPlannerConfig(), ids),
            ids,
        ),
        ids,
        ids,
        observer,
        history_limit=1000,
        dedup_capacity=100,
        monotonic_clock=lambda: 0.0,
    )
    shadow.start(dispatcher)
    polling = SynchronousRestPollingEngine(
        provider,
        cache,
        KEY,
        clock=FixedClock(),
        sleeper=NoWaitSleeper(),
        dispatcher=dispatcher,
        projector=MarketEventProjector(SequentialIdFactory("event")),
        correlation_id_factory=SequentialIdFactory("cycle"),
    )

    bootstrap_result = polling.run_once()
    regular_result = polling.run_once()

    assert bootstrap_result.outcome is regular_result.outcome is PollCycleOutcome.SUCCESS
    assert (regular_result.updated, regular_result.inserted, regular_result.evicted) == (1, 1, 1)
    assert [type(event) for event in events[-4:]] == [
        CandleUpdated,
        CandleClosed,
        CandleInserted,
        CandleEvicted,
    ]
    assert events[-2].candle.open_time_ms == next_open.open_time_ms  # type: ignore[union-attr]
    assert cache.get_snapshot(KEY)[-2:] == (finalized, next_open)
    assert shadow.status.handled_events == shadow.status.successful_decisions == 1
    assert observer.records[-1].source_event_id == events[-3].event_id
    assert provider.close_calls == 0
