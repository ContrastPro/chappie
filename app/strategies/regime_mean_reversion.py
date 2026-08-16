"""Range-gated mean reversion research strategy with a cost hurdle."""

from dataclasses import dataclass
from decimal import Decimal

from app.strategies.common import PureStrategy, StrategyIdentity, parameters
from app.strategies.indicators import rolling_mean, simple_regime
from app.trading.exceptions import InvalidStrategyConfigurationError
from app.trading.models import MarketSnapshot, Signal, SignalDirection
from app.trading.strategy import SignalIdFactory


@dataclass(frozen=True, slots=True)
class RegimeMeanReversionConfig:
    lookback: int = 20
    entry_deviation_bps: Decimal = Decimal(60)
    exit_deviation_bps: Decimal = Decimal(0)
    round_trip_cost_bps: Decimal = Decimal(20)
    safety_margin_bps: Decimal = Decimal(15)
    strategy_name: str = "regime_mean_reversion"
    strategy_version: str = "1.0.0"

    def __post_init__(self) -> None:
        if self.lookback < 14:
            raise InvalidStrategyConfigurationError(
                "regime mean-reversion lookback must be at least 14"
            )
        if any(
            not isinstance(value, Decimal) or not value.is_finite() or value < 0
            for value in (
                self.entry_deviation_bps,
                self.exit_deviation_bps,
                self.round_trip_cost_bps,
                self.safety_margin_bps,
            )
        ):
            raise InvalidStrategyConfigurationError("regime mean-reversion thresholds are invalid")


class RegimeMeanReversionStrategy(PureStrategy):
    def __init__(
        self, config: RegimeMeanReversionConfig, signal_id_factory: SignalIdFactory
    ) -> None:
        self._config, self._signal_id_factory = config, signal_id_factory
        self._identity = StrategyIdentity(config.strategy_name, config.strategy_version)
        self._configuration = parameters(
            lookback=config.lookback,
            entry_deviation_bps=config.entry_deviation_bps,
            exit_deviation_bps=config.exit_deviation_bps,
            round_trip_cost_bps=config.round_trip_cost_bps,
            safety_margin_bps=config.safety_margin_bps,
        )

    @property
    def required_history(self) -> int:
        return self._config.lookback + 1

    def generate_signal(self, snapshot: MarketSnapshot) -> Signal:
        if snapshot.latest_closed_candle is None or snapshot.is_stale:
            return self._signal(snapshot, SignalDirection.HOLD, "stale_or_empty", {})
        if len(snapshot.closed_candles) < self.required_history:
            return self._signal(snapshot, SignalDirection.HOLD, "insufficient_history", {})
        candles = snapshot.closed_candles
        closes = tuple(c.close for c in candles)
        mean, previous_mean = (
            rolling_mean(closes, self._config.lookback),
            rolling_mean(closes[:-1], self._config.lookback),
        )
        deviation_bps = (closes[-1] - mean) / mean * Decimal(10_000)
        previous_deviation_bps = (closes[-2] - previous_mean) / previous_mean * Decimal(10_000)
        regime = simple_regime(
            tuple(c.high for c in candles), tuple(c.low for c in candles), closes
        )
        hurdle_bps = self._config.round_trip_cost_bps + self._config.safety_margin_bps
        values = {
            "regime": regime,
            "deviation_bps": deviation_bps,
            "previous_deviation_bps": previous_deviation_bps,
            "cost_hurdle_bps": hurdle_bps,
        }
        if (
            regime == "RANGE"
            and previous_deviation_bps > -self._config.entry_deviation_bps
            and deviation_bps <= -self._config.entry_deviation_bps
            and abs(deviation_bps) > hurdle_bps
        ):
            return self._signal(snapshot, SignalDirection.BUY, "mean_reversion_entry", values)
        if previous_deviation_bps < self._config.exit_deviation_bps <= deviation_bps:
            return self._signal(snapshot, SignalDirection.SELL, "mean_reversion_exit", values)
        return self._signal(snapshot, SignalDirection.HOLD, "regime_or_cost_hurdle_wait", values)
