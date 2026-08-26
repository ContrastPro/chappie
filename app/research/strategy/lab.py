"""Bounded, offline-only Paper economics for apples-to-apples strategy research."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from app.market.models import Candle, MarketStreamKey
from app.research.strategy.history import candles_sha256
from app.strategies.ema_crossover import EmaCrossoverStrategy
from app.strategies.factory import StrategySettings, build_strategy
from app.strategies.indicators import simple_regime as _simple_regime
from app.strategies.trend_filtered import TrendFilteredStrategy
from app.trading.models import SignalDirection
from app.trading.snapshot import MarketSnapshotFactory
from app.trading.strategy import SignalIdFactory, Strategy


class _SequentialSignalIds(SignalIdFactory):
    def __init__(self) -> None:
        self._value = 0

    def next_id(self) -> str:
        self._value += 1
        return f"replay-signal-{self._value}"


@dataclass(frozen=True, slots=True)
class StrategyComparisonConfig:
    starting_equity: Decimal = Decimal(10000)
    fee_rate: Decimal = Decimal("0.001")
    target_exposure: Decimal = Decimal(1)
    stale_after_ms: int = 120_000

    def __post_init__(self) -> None:
        if any(
            not value.is_finite()
            for value in (self.starting_equity, self.fee_rate, self.target_exposure)
        ):
            raise ValueError("comparison financial configuration must be finite")
        if (
            self.starting_equity <= 0
            or not Decimal(0) <= self.fee_rate < 1
            or not Decimal(0) < self.target_exposure <= 1
        ):
            raise ValueError("comparison financial configuration is invalid")


@dataclass(frozen=True, slots=True)
class ReplayProgress:
    """Read-only, post-accounting progress for an offline strategy replay."""

    evaluation_processed: int
    evaluation_total: int
    last_close_time_ms: int
    decisions: int
    fills: int
    closed_trades: int
    current_equity: Decimal


@dataclass(frozen=True, slots=True)
class StrategyComparisonResult:
    strategy_name: str
    strategy_version: str
    starting_equity: Decimal
    ending_equity: Decimal
    net_pnl: Decimal
    return_percent: Decimal
    gross_realised_pnl: Decimal
    fees: Decimal
    fee_drag_percent: Decimal
    fills: int
    completed_round_trips: int
    wins: int
    losses: int
    win_rate_percent: Decimal | None
    profit_factor: Decimal | None
    average_round_trip_pnl: Decimal | None
    best_trade: Decimal | None
    worst_trade: Decimal | None
    max_drawdown_percent: Decimal
    turnover: Decimal
    turnover_equity_percent: Decimal
    average_holding_ms: int | None
    average_captured_move_percent: Decimal | None
    exposure_time_percent: Decimal
    regime_counts: dict[str, int]
    strategy_observations: dict[str, int]


def simple_regime(candles: tuple[Candle, ...], index: int) -> str:
    """Report-only deterministic regime label; it never selects a strategy."""
    window = candles[max(0, index - 13) : index + 1]
    return _simple_regime(
        tuple(c.high for c in window), tuple(c.low for c in window), tuple(c.close for c in window)
    )


def compare_strategy(
    candles: tuple[Candle, ...],
    strategy_config: StrategySettings,
    config: StrategyComparisonConfig | None = None,
    *,
    evaluation_start_index: int = 0,
    progress_observer: Callable[[ReplayProgress], None] | None = None,
    use_stateful_ema: bool = True,
    use_stateful_trend_filtered: bool = True,
) -> StrategyComparisonResult:
    config = config or StrategyComparisonConfig()
    if len(candles) < 2:
        raise ValueError("at least two candles are required")
    if not isinstance(evaluation_start_index, int) or isinstance(evaluation_start_index, bool):
        raise TypeError("evaluation_start_index must be an integer")
    if not 0 <= evaluation_start_index < len(candles):
        raise ValueError("evaluation_start_index is outside candle data")
    stream_key = MarketStreamKey("BTCUSDT", "1m")
    strategy: Strategy = build_strategy(strategy_config, _SequentialSignalIds())
    if evaluation_start_index and evaluation_start_index < strategy.required_history:
        raise ValueError("insufficient preceding warmup history")
    factory = MarketSnapshotFactory(config.stale_after_ms)
    stateful_ema = (
        use_stateful_ema
        and isinstance(strategy, EmaCrossoverStrategy)
        and evaluation_start_index > 0
    )
    stateful_trend_filtered = (
        use_stateful_trend_filtered
        and isinstance(strategy, TrendFilteredStrategy)
        and evaluation_start_index > 0
    )
    stateful_strategy = stateful_ema or stateful_trend_filtered
    dataset_digest = candles_sha256(candles) if stateful_strategy else ""
    ema_state = (
        strategy.create_state(
            candles[:evaluation_start_index],
            symbol=stream_key.symbol,
            interval=stream_key.interval,
            dataset_digest=dataset_digest,
            boundary_index=evaluation_start_index,
        )
        if stateful_ema
        else None
    )
    trend_state = (
        strategy.create_state(
            candles[:evaluation_start_index],
            symbol=stream_key.symbol,
            interval=stream_key.interval,
            dataset_digest=dataset_digest,
            boundary_index=evaluation_start_index,
        )
        if stateful_trend_filtered
        else None
    )
    cash, quantity, fees, turnover = config.starting_equity, Decimal(0), Decimal(0), Decimal(0)
    entry_cost = Decimal(0)
    entry_time: int | None = None
    entry_price: Decimal | None = None
    completed: list[tuple[Decimal, Decimal, int, Decimal]] = []
    peak, max_drawdown, exposed_ms = config.starting_equity, Decimal(0), 0
    regimes: dict[str, int] = {}
    observations: dict[str, int] = {}
    previous_time = candles[evaluation_start_index].close_time_ms
    replay_candles = (
        enumerate(candles[evaluation_start_index:], start=evaluation_start_index)
        if stateful_strategy
        else enumerate(candles)
    )
    for index, candle in replay_candles:
        if stateful_ema and index >= evaluation_start_index:
            direction, _reason, ema_state = strategy.continue_state(
                ema_state,
                candle,
                symbol=stream_key.symbol,
                interval=stream_key.interval,
                dataset_digest=dataset_digest,
                boundary_index=evaluation_start_index,
            )
        elif stateful_trend_filtered and index >= evaluation_start_index:
            continuation = strategy.continue_state(
                trend_state,
                candle,
                symbol=stream_key.symbol,
                interval=stream_key.interval,
                dataset_digest=dataset_digest,
                boundary_index=evaluation_start_index,
            )
            direction = continuation.direction
            trend_state = continuation.state
        else:
            closed = tuple(candles[: index + 1])
            snapshot = factory.create(
                stream_key,
                closed,
                candle.close_time_ms + 1,
                f"replay-{index}",
                correlation_cycle_id=f"replay-{index}",
            )
            signal = strategy.generate_signal(snapshot)
            direction = signal.direction
        # Feed deterministic strategy state with prior candles, but do not
        # account for pre-boundary decisions, fills, exposure, or regimes.
        if index < evaluation_start_index:
            continue
        if (
            not stateful_strategy
            and strategy.name == "regime_mean_reversion"
            and "regime" in signal.metadata
        ):
            regime_name = str(signal.metadata["regime"])
            observation = (
                "candidate_entries_allowed_in_range"
                if signal.direction is SignalDirection.BUY
                else f"candidate_entries_rejected_in_{regime_name.lower()}"
            )
            observations[observation] = observations.get(observation, 0) + 1
        if (
            not stateful_strategy
            and strategy.name == "multi_timeframe_trend"
            and "higher_confirmed" in signal.metadata
        ):
            observation = (
                "confirmation/"
                f"higher={signal.metadata['higher_confirmed']}/"
                f"medium={signal.metadata['medium_confirmed']}/"
                f"lower={signal.metadata['lower_entry']}"
            )
            observations[observation] = observations.get(observation, 0) + 1
        if not stateful_strategy and strategy.name in {
            "volatility_continuation",
            "trend_retracement_continuation",
            "compression_expansion_breakout",
        }:
            for key, value in signal.metadata.items():
                if key.startswith("diag_") and value == "true":
                    observation = f"{strategy.name}/{key.removeprefix('diag_')}"
                    observations[observation] = observations.get(observation, 0) + 1
        price = candle.close
        if quantity:
            exposed_ms += candle.close_time_ms - previous_time
        previous_time = candle.close_time_ms
        regime = simple_regime(candles, index)
        regimes[regime] = regimes.get(regime, 0) + 1
        if direction is SignalDirection.BUY and quantity == 0:
            # Match PaperExecutionProvider's quote-asset fee convention: the
            # complete debit is gross notional plus its fee, never a reduction
            # to base quantity or an implicit source of leverage.
            budget = cash * config.target_exposure
            gross = budget / (Decimal(1) + config.fee_rate)
            fee = gross * config.fee_rate
            if gross > 0:
                quantity = gross / price
                cash -= gross + fee
                fees += fee
                turnover += gross
                entry_cost = gross + fee
                entry_time = candle.close_time_ms
                entry_price = price
        elif direction is SignalDirection.SELL and quantity > 0:
            gross = quantity * price
            fee = gross * config.fee_rate
            proceeds = gross - fee
            cash += proceeds
            fees += fee
            turnover += gross
            completed.append(
                (
                    proceeds - entry_cost,
                    gross - (entry_cost / (Decimal(1) + config.fee_rate)),
                    candle.close_time_ms - (entry_time or candle.close_time_ms),
                    (price - (entry_price or price)) / (entry_price or price) * Decimal(100),
                )
            )
            quantity, entry_cost, entry_time, entry_price = Decimal(0), Decimal(0), None, None
        equity = cash + quantity * price
        peak = max(peak, equity)
        if peak:
            max_drawdown = max(max_drawdown, (peak - equity) / peak * Decimal(100))
        if progress_observer is not None:
            progress_observer(
                ReplayProgress(
                    evaluation_processed=index - evaluation_start_index + 1,
                    evaluation_total=len(candles) - evaluation_start_index,
                    last_close_time_ms=candle.close_time_ms,
                    decisions=sum(regimes.values()),
                    fills=(2 * len(completed)) + (1 if quantity else 0),
                    closed_trades=len(completed),
                    current_equity=equity,
                )
            )
    ending = cash + quantity * candles[-1].close
    outcomes = [item[0] for item in completed]
    gross_realised = sum((item[1] for item in completed), Decimal(0))
    gross_wins = sum((value for value in outcomes if value > 0), Decimal(0))
    gross_losses = -sum((value for value in outcomes if value < 0), Decimal(0))
    total_time = candles[-1].close_time_ms - candles[0].close_time_ms
    return StrategyComparisonResult(
        strategy.name,
        strategy.version,
        config.starting_equity,
        ending,
        ending - config.starting_equity,
        (ending - config.starting_equity) / config.starting_equity * Decimal(100),
        gross_realised,
        fees,
        fees / config.starting_equity * Decimal(100),
        (2 * len(completed)) + (1 if quantity else 0),
        len(completed),
        sum(value > 0 for value in outcomes),
        sum(value < 0 for value in outcomes),
        Decimal(sum(value > 0 for value in outcomes)) / len(outcomes) * Decimal(100)
        if outcomes
        else None,
        gross_wins / gross_losses if gross_losses else None,
        sum(outcomes, Decimal(0)) / len(outcomes) if outcomes else None,
        max(outcomes) if outcomes else None,
        min(outcomes) if outcomes else None,
        max_drawdown,
        turnover,
        turnover / config.starting_equity * Decimal(100),
        sum(item[2] for item in completed) // len(completed) if completed else None,
        sum(item[3] for item in completed) / len(completed) if completed else None,
        Decimal(exposed_ms) / Decimal(total_time) * Decimal(100) if total_time else Decimal(0),
        regimes,
        observations,
    )


def compare_all(
    candles: tuple[Candle, ...],
    config: StrategyComparisonConfig | None = None,
    *,
    include_research: bool = False,
) -> tuple[StrategyComparisonResult, ...]:
    config = config or StrategyComparisonConfig()
    names = ("ema_crossover", "trend_filtered", "breakout", "mean_reversion")
    if include_research:
        names += (
            "cost_aware_momentum",
            "regime_mean_reversion",
            "multi_timeframe_trend",
            "volatility_continuation",
            "trend_retracement_continuation",
            "compression_expansion_breakout",
        )
    return tuple(compare_strategy(candles, StrategySettings(name=name), config) for name in names)
