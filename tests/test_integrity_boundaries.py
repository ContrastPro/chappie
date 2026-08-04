import ast
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from app.market.events import (
    CandleClosed,
    CandleClosedReason,
    MarketEvent,
    MarketEventDispatcher,
    MarketEventSource,
    MarketGapDetected,
    MarketGapPhase,
    MarketGapReason,
    MarketStreamFailed,
    MarketStreamFailureReason,
)
from app.market.exceptions import (
    InvalidEventSubscriptionError,
    InvalidMarketEventError,
    PollingEngineConfigurationError,
)
from app.market.models import Candle, CandleSnapshot, MarketStreamKey
from app.market.polling import PollingPolicy, StopAwareSleeper, SystemClock
from app.trading.exceptions import (
    InvalidMarketSnapshotError,
    InvalidOrderIntentError,
    InvalidPipelineConfigurationError,
    PipelineInvariantError,
)
from app.trading.intent import IntentPlanOutcome, IntentPlanResult
from app.trading.models import OrderIntent, Signal, SignalDirection
from app.trading.pipeline import DecisionPipelineResult, PipelineOutcome, TradingDecisionPipeline
from app.trading.snapshot import MarketSnapshotFactory

KEY = MarketStreamKey("BTCUSDT", "1m")
OPEN_MS = 1_700_000_000_000


def candle(index: int = 0, *, duration_ms: int = 59_999) -> Candle:
    open_ms = OPEN_MS + index * 60_000
    return Candle(
        open_time_ms=open_ms,
        close_time_ms=open_ms + duration_ms,
        open=Decimal(100),
        high=Decimal(110),
        low=Decimal(90),
        close=Decimal(105),
        volume=Decimal(10),
    )


def test_portfolio_architecture_boundaries() -> None:
    forbidden = {
        "httpx",
        "random",
        "sqlite3",
        "app.shadow",
        "app.agent",
        "app.application",
        "app.main",
        "app.risk",
        "app.database",
        "app.market.binance_provider",
    }
    portfolio_files = sorted(Path("app/portfolio").glob("*.py"))
    assert portfolio_files
    for path in portfolio_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        imports.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        assert not any(
            imported == blocked or imported.startswith(f"{blocked}.")
            for imported in imports
            for blocked in forbidden
        ), f"{path} crosses a portfolio architecture boundary"
        calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert not ({"time", "time_ns", "now", "utcnow"} & calls)

    execution_tree = ast.parse(
        Path("app/execution/models.py").read_text(encoding="utf-8"),
        filename="app/execution/models.py",
    )
    execution_imports = {
        node.module
        for node in ast.walk(execution_tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert not any(
        name == "app.portfolio" or name.startswith("app.portfolio.") for name in execution_imports
    )


def event_values() -> dict[str, object]:
    return {
        "event_id": "event-1",
        "occurred_at_ms": OPEN_MS,
        "stream_key": KEY,
        "correlation_cycle_id": "cycle-1",
        "source": MarketEventSource.REST_POLLING,
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"cadence_seconds": 0},
        {"max_provider_attempts": 0},
        {"max_provider_attempts": 3, "provider_retry_delays": (1,)},
        {"provider_retry_delays": (-1, 2)},
        {"cycle_backoff_delays": ()},
        {"cycle_backoff_delays": (0,)},
        {"max_retry_after_seconds": 0},
        {"max_bootstrap_limit": 0},
        {"regular_limit": 1},
        {"max_failed_cycles": 0},
        {"clock_skew_warning_seconds": -1},
    ],
)
def test_polling_policy_rejects_every_invalid_boundary(overrides: dict[str, object]) -> None:
    with pytest.raises(PollingEngineConfigurationError):
        PollingPolicy(**overrides)  # type: ignore[arg-type]


def test_production_clock_and_stop_aware_sleeper_boundaries() -> None:
    clock = SystemClock()
    assert clock.now_ms() > 0
    assert clock.monotonic() > 0
    with pytest.raises(PollingEngineConfigurationError):
        StopAwareSleeper(0)
    assert StopAwareSleeper().wait(10, lambda: True) is False


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"event_id": ""}, "event_id"),
        ({"occurred_at_ms": True}, "occurred_at_ms"),
        ({"correlation_cycle_id": ""}, "correlation_cycle_id"),
        ({"stream_key": "BTCUSDT"}, "stream_key"),
        ({"source": "polling"}, "source"),
    ],
)
def test_market_event_envelope_rejects_invalid_values(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(InvalidMarketEventError, match=message):
        MarketEvent(**{**event_values(), **overrides})  # type: ignore[arg-type]


def test_diagnostic_event_and_dispatcher_validation_boundaries() -> None:
    with pytest.raises(InvalidMarketEventError, match="expected_open_time_ms"):
        MarketGapDetected(
            **event_values(),
            expected_open_time_ms=True,
            received_open_time_ms=None,
            last_confirmed_open_time_ms=None,
            phase=MarketGapPhase.REGULAR,
            reason=MarketGapReason.INTERVAL_GAP,
        )
    valid_gap = MarketGapDetected(
        **event_values(),
        expected_open_time_ms=None,
        received_open_time_ms=None,
        last_confirmed_open_time_ms=None,
        phase=MarketGapPhase.REGULAR,
        reason=MarketGapReason.INTERVAL_GAP,
    )
    with pytest.raises(InvalidMarketEventError, match="phase"):
        replace(valid_gap, phase="regular")  # type: ignore[arg-type]
    with pytest.raises(InvalidMarketEventError, match="reason"):
        replace(valid_gap, reason="gap")  # type: ignore[arg-type]

    failure = MarketStreamFailed(
        **event_values(),
        error_category="provider",
        consecutive_failures=1,
        terminal=True,
        reason=MarketStreamFailureReason.PROVIDER,
    )
    for field, value in (
        ("error_category", ""),
        ("consecutive_failures", 0),
        ("terminal", 1),
        ("reason", "provider"),
    ):
        with pytest.raises(InvalidMarketEventError):
            replace(failure, **{field: value})

    dispatcher = MarketEventDispatcher()
    with pytest.raises(InvalidEventSubscriptionError):
        dispatcher.subscribe(str, lambda event: None)  # type: ignore[arg-type]
    with pytest.raises(InvalidEventSubscriptionError):
        dispatcher.subscribe(MarketEvent, None)  # type: ignore[arg-type]
    with pytest.raises(InvalidMarketEventError):
        dispatcher.publish("event")  # type: ignore[arg-type]


def test_candle_event_rejects_invalid_snapshot_and_reason() -> None:
    snapshot = CandleSnapshot.from_candle(candle())
    valid = CandleClosed(
        **event_values(), candle=snapshot, reason=CandleClosedReason.TIME_CONFIRMED
    )
    with pytest.raises(InvalidMarketEventError, match="candle"):
        replace(valid, candle="bad")  # type: ignore[arg-type]
    with pytest.raises(InvalidMarketEventError, match="reason"):
        replace(valid, reason="closed")  # type: ignore[arg-type]


def test_snapshot_factory_rejects_configuration_and_malformed_inputs() -> None:
    for stale_after in (0, True, "1"):
        with pytest.raises(InvalidMarketSnapshotError):
            MarketSnapshotFactory(stale_after)  # type: ignore[arg-type]
    with pytest.raises(InvalidMarketSnapshotError, match="data_version"):
        MarketSnapshotFactory(1, " ")

    factory = MarketSnapshotFactory(1)
    for as_of in (-1, True):
        with pytest.raises(InvalidMarketSnapshotError, match="as_of_ms"):
            factory.create(KEY, (), as_of, "snapshot")  # type: ignore[arg-type]
    with pytest.raises(InvalidMarketSnapshotError, match="sequence"):
        factory.create(KEY, None, OPEN_MS, "snapshot")  # type: ignore[arg-type]
    with pytest.raises(InvalidMarketSnapshotError, match="only Candle"):
        factory.create(KEY, [object()], OPEN_MS, "snapshot")  # type: ignore[list-item]


def test_snapshot_factory_copies_snapshots_and_rejects_closed_after_open() -> None:
    original = CandleSnapshot.from_candle(candle())
    result = MarketSnapshotFactory(100_000).create(KEY, [original], OPEN_MS, "snapshot")
    assert result.latest_open_candle == original
    assert result.latest_open_candle is not original

    long_first = candle(0, duration_ms=180_000)
    short_second = candle(1, duration_ms=1)
    with pytest.raises(InvalidMarketSnapshotError, match="closed candle"):
        MarketSnapshotFactory(100_000).create(
            KEY,
            [long_first, short_second],
            short_second.close_time_ms + 1,
            "snapshot",
        )


def test_architecture_import_boundaries() -> None:
    constraints = {
        "app/market/cache.py": ("app.trading", "app.market.binance_provider", "httpx"),
        "app/market/events.py": ("app.trading", "app.execution", "httpx"),
        "app/trading/models.py": ("app.market.binance_provider", "httpx", "app.market.polling"),
        "app/strategies/ema_crossover.py": (
            "app.market.cache",
            "app.market.polling",
            "app.database",
            "httpx",
            "time",
        ),
        "app/trading/intent.py": ("app.risk", "app.execution", "httpx", "time"),
        "app/trading/pipeline.py": ("app.risk", "app.execution", "httpx", "time"),
        "app/shadow/engine.py": (
            "app.market.binance_provider",
            "httpx",
            "app.execution",
            "app.risk",
            "app.database",
            "app.main",
            "app.agent.trader",
        ),
        "app/shadow/observer.py": ("pathlib", "app.database"),
    }
    for filename, forbidden in constraints.items():
        tree = ast.parse(Path(filename).read_text(encoding="utf-8"))
        imported = {
            node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        }
        imported.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        assert not any(
            dependency == prefix or dependency.startswith(f"{prefix}.")
            for dependency in imported
            for prefix in forbidden
        )


def test_pipeline_constructor_and_result_reject_invalid_boundaries() -> None:
    with pytest.raises(InvalidPipelineConfigurationError, match="strategy"):
        TradingDecisionPipeline(object(), object(), object())  # type: ignore[arg-type]

    source = MarketSnapshotFactory(1).create(KEY, (), OPEN_MS, "snapshot")
    signal = Signal(
        signal_id="signal",
        snapshot_id=source.snapshot_id,
        stream_key=KEY,
        created_at_ms=OPEN_MS,
        effective_at_ms=OPEN_MS,
        direction=SignalDirection.HOLD,
        reason_code="empty_history",
        explanation="No candles",
        strategy_name="strategy",
        strategy_version="1",
        strategy_parameters={},
    )
    plan = IntentPlanResult(
        outcome=IntentPlanOutcome.NO_INTENT_FOR_HOLD,
        signal_id=signal.signal_id,
        intent=None,
        reason_code="hold_signal_no_intent",
        planner_name="planner",
        planner_version="1",
    )
    valid = DecisionPipelineResult(
        pipeline_run_id="run",
        snapshot_id=source.snapshot_id,
        stream_key=KEY,
        evaluated_at_ms=OPEN_MS,
        strategy_name=signal.strategy_name,
        strategy_version=signal.strategy_version,
        signal=signal,
        intent_plan=plan,
        order_intent=None,
        outcome=PipelineOutcome.HOLD,
        reason_code=signal.reason_code,
        correlation_id=None,
        completed_stages=("snapshot_validated", "strategy_evaluated", "intent_planned"),
    )
    for field, value in (
        ("pipeline_run_id", ""),
        ("evaluated_at_ms", True),
        ("stream_key", "BTCUSDT"),
        ("signal", object()),
        ("intent_plan", object()),
        ("outcome", "hold"),
        ("completed_stages", ()),
    ):
        with pytest.raises(PipelineInvariantError):
            replace(valid, **{field: value})


def test_order_intent_rejects_invalid_stream_and_action_types() -> None:
    values = {
        "intent_id": "intent",
        "source_signal_id": "signal",
        "stream_key": KEY,
        "created_at_ms": OPEN_MS,
        "action": "enter_long",
        "target_exposure": Decimal(1),
        "reason": "test",
        "strategy_name": "strategy",
        "strategy_version": "1",
    }
    with pytest.raises(InvalidOrderIntentError, match="action"):
        OrderIntent(**values)  # type: ignore[arg-type]
    with pytest.raises(InvalidOrderIntentError, match="stream_key"):
        OrderIntent(**{**values, "stream_key": "BTCUSDT"})  # type: ignore[arg-type]
