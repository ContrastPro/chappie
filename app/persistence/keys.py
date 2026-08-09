from dataclasses import dataclass
from decimal import Decimal

from app.market.models import CandleSnapshot
from app.persistence.canonical import canonical_sha256
from app.persistence.exceptions import PersistenceSerializationError

STRATEGY_INPUT_VERSION = "strategy-input/v1"
CONFIG_VERSION = "config/v1"
DECISION_KEY_VERSION = "stable-decision/v1"
DECISION_SLOT_VERSION = "decision-slot/v1"
COMMAND_KEY_VERSION = "paper-command/v1"


@dataclass(frozen=True, slots=True)
class EmaStrategyFingerprintInput:
    fast_window: int
    slow_window: int
    strategy_name: str
    strategy_version: str


@dataclass(frozen=True, slots=True)
class PlannerFingerprintInput:
    long_target_exposure: Decimal
    planner_name: str
    planner_version: str


@dataclass(frozen=True, slots=True)
class RiskPolicyFingerprintInput:
    max_order_notional: Decimal
    max_position_notional: Decimal
    max_target_exposure: Decimal
    min_cash_reserve: Decimal
    max_fee_rate: Decimal
    max_slippage_bps: Decimal
    reject_stale_market: bool
    policy_name: str
    policy_version: str


@dataclass(frozen=True, slots=True)
class InstrumentRulesFingerprintInput:
    symbol: str
    interval: str
    base_asset: str
    quote_asset: str
    price_tick: Decimal
    quantity_step: Decimal
    min_quantity: Decimal
    min_notional: Decimal
    base_asset_precision: int
    quote_asset_precision: int
    rules_version: str


@dataclass(frozen=True, slots=True)
class PaperExecutionFingerprintInput:
    fee_rate: Decimal
    latency_ms: int
    provider_name: str
    provider_version: str


@dataclass(frozen=True, slots=True)
class SnapshotPolicyFingerprintInput:
    stale_after_ms: int
    data_version: str


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PersistenceSerializationError(f"{name} must be a non-empty string")
    return value


def fingerprint_configuration(kind: str, payload: dict[str, object]) -> str:
    return canonical_sha256(
        {"kind": _text(kind, "kind"), "configuration": payload},
        algorithm_version=CONFIG_VERSION,
    )


def _typed_fingerprint(value: object, expected_type: type, domain: str) -> str:
    if not isinstance(value, expected_type):
        raise PersistenceSerializationError(f"value must be {expected_type.__name__}")
    payload = {name: getattr(value, name) for name in value.__dataclass_fields__}
    return canonical_sha256(payload, algorithm_version=domain)


def fingerprint_ema_strategy(value: EmaStrategyFingerprintInput) -> str:
    return _typed_fingerprint(value, EmaStrategyFingerprintInput, "ema-strategy-config/v1")


def fingerprint_planner(value: PlannerFingerprintInput) -> str:
    return _typed_fingerprint(value, PlannerFingerprintInput, "planner-config/v1")


def fingerprint_risk_policy(value: RiskPolicyFingerprintInput) -> str:
    return _typed_fingerprint(value, RiskPolicyFingerprintInput, "risk-policy-config/v1")


def fingerprint_instrument_rules(value: InstrumentRulesFingerprintInput) -> str:
    return _typed_fingerprint(value, InstrumentRulesFingerprintInput, "instrument-rules/v1")


def fingerprint_paper_execution(value: PaperExecutionFingerprintInput) -> str:
    return _typed_fingerprint(value, PaperExecutionFingerprintInput, "paper-execution-config/v1")


def fingerprint_snapshot_policy(value: SnapshotPolicyFingerprintInput) -> str:
    return _typed_fingerprint(value, SnapshotPolicyFingerprintInput, "snapshot-policy/v1")


def _candle_payload(candle: CandleSnapshot) -> dict[str, object]:
    if not isinstance(candle, CandleSnapshot):
        raise PersistenceSerializationError("strategy inputs must be CandleSnapshot values")
    return {
        "open_time_ms": candle.open_time_ms,
        "close_time_ms": candle.close_time_ms,
        "open": candle.open,
        "high": candle.high,
        "low": candle.low,
        "close": candle.close,
        "volume": candle.volume,
    }


def fingerprint_candle(candle: CandleSnapshot) -> str:
    return canonical_sha256(_candle_payload(candle), algorithm_version="candle/v1")


def fingerprint_strategy_input(candles: tuple[CandleSnapshot, ...]) -> str:
    if not isinstance(candles, tuple) or not candles:
        raise PersistenceSerializationError("strategy input must be a non-empty tuple")
    return canonical_sha256(
        {"length": len(candles), "candles": [_candle_payload(candle) for candle in candles]},
        algorithm_version=STRATEGY_INPUT_VERSION,
    )


@dataclass(frozen=True, slots=True)
class StableDecisionKeyInput:
    exchange: str
    symbol: str
    interval: str
    latest_closed_candle: CandleSnapshot
    strategy_input_fingerprint: str
    strategy_name: str
    strategy_version: str
    strategy_config_fingerprint: str
    planner_name: str
    planner_version: str
    planner_config_fingerprint: str
    snapshot_policy_fingerprint: str
    decision_schema_version: str = "1"

    def payload(self, *, include_market_data: bool) -> dict[str, object]:
        candle = self.latest_closed_candle
        result: dict[str, object] = {
            "exchange": _text(self.exchange, "exchange"),
            "symbol": _text(self.symbol, "symbol"),
            "interval": _text(self.interval, "interval"),
            "candle_open_time_ms": candle.open_time_ms,
            "candle_close_time_ms": candle.close_time_ms,
            "strategy_name": _text(self.strategy_name, "strategy_name"),
            "strategy_version": _text(self.strategy_version, "strategy_version"),
            "strategy_config_fingerprint": _text(
                self.strategy_config_fingerprint, "strategy_config_fingerprint"
            ),
            "planner_name": _text(self.planner_name, "planner_name"),
            "planner_version": _text(self.planner_version, "planner_version"),
            "planner_config_fingerprint": _text(
                self.planner_config_fingerprint, "planner_config_fingerprint"
            ),
            "snapshot_policy_fingerprint": _text(
                self.snapshot_policy_fingerprint, "snapshot_policy_fingerprint"
            ),
            "decision_schema_version": _text(
                self.decision_schema_version, "decision_schema_version"
            ),
        }
        if include_market_data:
            result.update(
                latest_candle_fingerprint=fingerprint_candle(candle),
                strategy_input_fingerprint=_text(
                    self.strategy_input_fingerprint, "strategy_input_fingerprint"
                ),
            )
        return result


def stable_decision_key(value: StableDecisionKeyInput) -> str:
    if not isinstance(value, StableDecisionKeyInput):
        raise PersistenceSerializationError("value must be StableDecisionKeyInput")
    return canonical_sha256(
        value.payload(include_market_data=True), algorithm_version=DECISION_KEY_VERSION
    )


def decision_slot_key(value: StableDecisionKeyInput) -> str:
    if not isinstance(value, StableDecisionKeyInput):
        raise PersistenceSerializationError("value must be StableDecisionKeyInput")
    return canonical_sha256(
        value.payload(include_market_data=False), algorithm_version=DECISION_SLOT_VERSION
    )


@dataclass(frozen=True, slots=True)
class DurableCommandKeyInput:
    stable_decision_key: str
    intent_action: str
    requested_target_exposure: Decimal
    approved_side: str
    approved_quantity: Decimal
    approved_target_exposure: Decimal
    reference_price: Decimal
    worst_case_price: Decimal
    risk_outcome: str
    modification_reasons: tuple[str, ...]
    risk_policy_fingerprint: str
    instrument_rules_fingerprint: str
    execution_config_fingerprint: str
    command_schema_version: str = "1"


def durable_command_key(value: DurableCommandKeyInput) -> str:
    if not isinstance(value, DurableCommandKeyInput):
        raise PersistenceSerializationError("value must be DurableCommandKeyInput")
    payload = {name: getattr(value, name) for name in value.__dataclass_fields__}
    return canonical_sha256(payload, algorithm_version=COMMAND_KEY_VERSION)
