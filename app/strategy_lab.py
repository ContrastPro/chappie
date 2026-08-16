"""Bounded, offline-only Paper economics for apples-to-apples strategy research."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.market.models import Candle, MarketStreamKey
from app.strategies.factory import StrategySettings, build_strategy
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
    exposure_time_percent: Decimal
    regime_counts: dict[str, int]


def simple_regime(candles: tuple[Candle, ...], index: int) -> str:
    """Report-only deterministic regime label; it never selects a strategy."""
    if index < 14:
        return "UNKNOWN"
    window = candles[max(0, index - 13) : index + 1]
    high, low, close = max(c.high for c in window), min(c.low for c in window), window[-1].close
    range_bps = (high - low) / close * Decimal(10_000)
    trend_bps = abs(window[-1].close - window[0].close) / close * Decimal(10_000)
    if range_bps >= Decimal(250):
        return "HIGH_VOLATILITY"
    return "TREND" if trend_bps >= Decimal(60) else "RANGE"


def compare_strategy(
    candles: tuple[Candle, ...],
    strategy_config: StrategySettings,
    config: StrategyComparisonConfig | None = None,
) -> StrategyComparisonResult:
    config = config or StrategyComparisonConfig()
    if len(candles) < 2:
        raise ValueError("at least two candles are required")
    stream_key = MarketStreamKey("BTCUSDT", "1m")
    strategy: Strategy = build_strategy(strategy_config, _SequentialSignalIds())
    factory = MarketSnapshotFactory(config.stale_after_ms)
    cash, quantity, fees, turnover = config.starting_equity, Decimal(0), Decimal(0), Decimal(0)
    entry_cost = Decimal(0)
    entry_time: int | None = None
    completed: list[tuple[Decimal, Decimal, int]] = []
    peak, max_drawdown, exposed_ms = config.starting_equity, Decimal(0), 0
    regimes: dict[str, int] = {}
    previous_time = candles[0].close_time_ms
    for index, candle in enumerate(candles):
        closed = tuple(candles[: index + 1])
        snapshot = factory.create(
            stream_key,
            closed,
            candle.close_time_ms + 1,
            f"replay-{index}",
            correlation_cycle_id=f"replay-{index}",
        )
        signal = strategy.generate_signal(snapshot)
        price = candle.close
        if quantity:
            exposed_ms += candle.close_time_ms - previous_time
        previous_time = candle.close_time_ms
        regime = simple_regime(candles, index)
        regimes[regime] = regimes.get(regime, 0) + 1
        if signal.direction is SignalDirection.BUY and quantity == 0:
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
        elif signal.direction is SignalDirection.SELL and quantity > 0:
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
                )
            )
            quantity, entry_cost, entry_time = Decimal(0), Decimal(0), None
        equity = cash + quantity * price
        peak = max(peak, equity)
        if peak:
            max_drawdown = max(max_drawdown, (peak - equity) / peak * Decimal(100))
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
        sum(duration for _, _, duration in completed) // len(completed) if completed else None,
        Decimal(exposed_ms) / Decimal(total_time) * Decimal(100) if total_time else Decimal(0),
        regimes,
    )


def compare_all(
    candles: tuple[Candle, ...], config: StrategyComparisonConfig | None = None
) -> tuple[StrategyComparisonResult, ...]:
    config = config or StrategyComparisonConfig()
    return tuple(
        compare_strategy(candles, StrategySettings(name=name), config)
        for name in ("ema_crossover", "trend_filtered", "breakout", "mean_reversion")
    )
