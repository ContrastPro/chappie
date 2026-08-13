import hashlib
import json
from dataclasses import asdict, dataclass
from decimal import Decimal

from app.exchange_execution.exceptions import ExchangeCapabilityError, Phase3ReviewError
from app.exchange_execution.models import (
    ExchangeCommand,
    ExchangeOrderSide,
    ExecutionEnvironment,
    require_account_fingerprint,
    require_hash,
    require_text,
    require_timestamp,
)

MAX_PLAIN_DECIMAL_CHARACTERS = 4_096


def _serialize_decimal(value: Decimal) -> str:
    if not value.is_finite():
        raise Phase3ReviewError("Phase 3 Decimal values must be finite")
    if value.is_zero():
        return "0"
    sign, digits, exponent = value.as_tuple()
    coefficient = list(digits)
    while len(coefficient) > 1 and coefficient[-1] == 0:
        coefficient.pop()
        exponent += 1
    raw = "".join(str(digit) for digit in coefficient)
    adjusted = exponent + len(raw) - 1
    expanded_size = len(raw) + max(exponent, 0) + max(-adjusted, 0) + 2
    if expanded_size > MAX_PLAIN_DECIMAL_CHARACTERS:
        mantissa = raw[0] if len(raw) == 1 else f"{raw[0]}.{raw[1:]}"
        result = f"{mantissa}E{adjusted:+d}"
    elif exponent >= 0:
        result = raw + "0" * exponent
    else:
        split = len(raw) + exponent
        result = raw[:split] + "." + raw[split:] if split > 0 else "0." + "0" * -split + raw
        result = result.rstrip("0").rstrip(".")
    return f"-{result}" if sign else result


def _normalize(value: object) -> object:
    if isinstance(value, Decimal):
        return {"$decimal": _serialize_decimal(value)}
    if isinstance(value, dict):
        return {key: _normalize(item) for key, item in value.items()}
    return value


def _digest(value: object, algorithm_version: str) -> str:
    envelope = {"algorithm_version": algorithm_version, "payload": value}
    payload = json.dumps(_normalize(envelope), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


PHASE3_POLICY_VERSION = "m4.10-phase3-smoke/v1"


def derive_phase3_economic_slot(execution_root_id: str, account_fingerprint: str) -> str:
    require_text(execution_root_id, "execution_root_id")
    require_account_fingerprint(account_fingerprint)
    return _digest(
        {
            "account_fingerprint": account_fingerprint,
            "execution_root_id": execution_root_id,
            "slot": "first-managed-btcusdt-buy",
        },
        "phase3-economic-slot/v1",
    )


@dataclass(frozen=True, slots=True)
class Phase3RiskEnvelope:
    managed_quote_allocation: Decimal = Decimal(25)
    max_order_notional: Decimal = Decimal(12)
    max_submitted_notional: Decimal = Decimal(12)
    max_turnover: Decimal = Decimal(12)
    max_managed_position_notional: Decimal = Decimal(12)
    min_retained_reserve: Decimal = Decimal(13)
    max_submissions: int = 1
    max_non_terminal: int = 1
    max_in_flight: int = 1
    symbol: str = "BTCUSDT"
    side: str = "BUY"
    assumptions_fingerprint: str = "0" * 64

    def __post_init__(self) -> None:
        expected = (Decimal(25), Decimal(12), Decimal(12), Decimal(12), Decimal(12), Decimal(13))
        actual = tuple(
            getattr(self, name)
            for name in (
                "managed_quote_allocation",
                "max_order_notional",
                "max_submitted_notional",
                "max_turnover",
                "max_managed_position_notional",
                "min_retained_reserve",
            )
        )
        if actual != expected or any(
            not isinstance(value, Decimal) or not value.is_finite() for value in actual
        ):
            raise Phase3ReviewError("Phase 3 risk envelope must equal the reviewed 25/12/13 policy")
        if (self.max_submissions, self.max_non_terminal, self.max_in_flight) != (1, 1, 1):
            raise Phase3ReviewError("Phase 3 count limits must all equal one")
        if self.symbol != "BTCUSDT" or self.side != "BUY":
            raise Phase3ReviewError("Phase 3 supports BUY BTCUSDT only")
        require_hash(self.assumptions_fingerprint, "assumptions_fingerprint")

    @property
    def digest(self) -> str:
        return _digest(asdict(self), PHASE3_POLICY_VERSION)


@dataclass(frozen=True, slots=True)
class Phase3BalanceScope:
    account_fingerprint: str
    btc_free: Decimal
    btc_locked: Decimal
    usdt_free: Decimal
    usdt_locked: Decimal
    managed_quote_allocation: Decimal
    observed_at_ms: int
    interpretation: str = "btc-external-unmanaged;usdt-managed-allocation-only"

    def __post_init__(self) -> None:
        require_account_fingerprint(self.account_fingerprint)
        values = (self.btc_free, self.btc_locked, self.usdt_free, self.usdt_locked)
        if any(
            not isinstance(value, Decimal) or not value.is_finite() or value < 0 for value in values
        ):
            raise Phase3ReviewError("Phase 3 balance values must be non-negative Decimal values")
        if self.managed_quote_allocation != Decimal(25):
            raise Phase3ReviewError("Phase 3 managed quote allocation must equal 25 USDT")
        if self.usdt_free < self.managed_quote_allocation:
            raise Phase3ReviewError("exchange free USDT does not cover the managed allocation")
        require_timestamp(self.observed_at_ms, "observed_at_ms")
        if self.interpretation != "btc-external-unmanaged;usdt-managed-allocation-only":
            raise Phase3ReviewError("unsupported Phase 3 balance ownership interpretation")

    @property
    def digest(self) -> str:
        return _digest(asdict(self), "phase3-balance-scope/v1")


@dataclass(frozen=True, slots=True)
class ReviewedPhase3SmokeRequest:
    authority_id: str
    operator_run_id: str
    execution_root_id: str
    session_id: str
    account_fingerprint: str
    control_generation: int
    reconciliation_generation: int
    economic_slot_key: str
    client_order_id: str
    command: ExchangeCommand
    envelope: Phase3RiskEnvelope
    balance_scope: Phase3BalanceScope
    portfolio_id: str
    created_at_ms: int
    policy_version: str = PHASE3_POLICY_VERSION

    def __post_init__(self) -> None:
        for name in ("authority_id", "operator_run_id", "execution_root_id", "session_id"):
            require_text(getattr(self, name), name)
        require_account_fingerprint(self.account_fingerprint)
        require_hash(self.economic_slot_key, "economic_slot_key")
        if self.economic_slot_key != derive_phase3_economic_slot(
            self.execution_root_id, self.account_fingerprint
        ):
            raise Phase3ReviewError(
                "Phase 3 economic slot is not the deterministic first-smoke slot"
            )
        require_text(self.portfolio_id, "portfolio_id")
        require_timestamp(self.created_at_ms, "created_at_ms")
        if self.policy_version != PHASE3_POLICY_VERSION:
            raise Phase3ReviewError("unsupported Phase 3 policy version")
        if any(
            isinstance(v, bool) or not isinstance(v, int) or v < 0
            for v in (self.control_generation, self.reconciliation_generation)
        ):
            raise Phase3ReviewError("Phase 3 generations must be non-negative integers")
        if self.command.side is not ExchangeOrderSide.BUY:
            raise Phase3ReviewError("Phase 3 SELL authority is prohibited")
        if self.command.approved_notional > self.envelope.max_order_notional:
            raise Phase3ReviewError("Phase 3 approved notional exceeds the reviewed envelope")
        if (
            self.command.instrument_rules_fingerprint == ""
            or self.command.execution_configuration_fingerprint == ""
        ):
            raise Phase3ReviewError("Phase 3 rules/config fingerprints are required")
        if self.balance_scope.account_fingerprint != self.account_fingerprint:
            raise Phase3ReviewError("Phase 3 balance/account lineage mismatch")


class Phase3ProcessClaim:
    """Opaque single-use authority; it deliberately cannot survive a restart."""

    __slots__ = (
        "_consumed",
        "_nonce",
        "account_fingerprint",
        "authority_id",
        "balance_scope_digest",
        "config_fingerprint",
        "control_generation",
        "durable_command_key",
        "economic_slot_key",
        "envelope_digest",
        "environment",
        "execution_root_id",
        "portfolio_id",
        "reconciliation_generation",
        "rules_fingerprint",
        "session_id",
    )
    _ISSUER = object()

    def __init__(
        self, request: ReviewedPhase3SmokeRequest, *, _issuer: object | None = None
    ) -> None:
        if _issuer is not self._ISSUER:
            raise ExchangeCapabilityError("Phase3ProcessClaim can only be issued by persistence")
        self._nonce = object()
        self._consumed = False
        self.authority_id = request.authority_id
        self.durable_command_key = request.command.durable_command_key
        self.execution_root_id = request.execution_root_id
        self.session_id = request.session_id
        self.account_fingerprint = request.account_fingerprint
        self.portfolio_id = request.portfolio_id
        self.control_generation = request.control_generation
        self.reconciliation_generation = request.reconciliation_generation
        self.economic_slot_key = request.economic_slot_key
        self.envelope_digest = request.envelope.digest
        self.balance_scope_digest = request.balance_scope.digest
        self.rules_fingerprint = request.command.instrument_rules_fingerprint
        self.config_fingerprint = request.command.execution_configuration_fingerprint
        self.environment = ExecutionEnvironment.TESTNET

    @classmethod
    def _issue(cls, request: ReviewedPhase3SmokeRequest) -> "Phase3ProcessClaim":
        return cls(request, _issuer=cls._ISSUER)

    def _consume(self) -> None:
        if self._consumed:
            raise ExchangeCapabilityError("Phase3ProcessClaim has already been consumed")
        self._consumed = True

    def __copy__(self):
        raise TypeError("Phase3ProcessClaim cannot be copied")

    def __deepcopy__(self, memo):
        raise TypeError("Phase3ProcessClaim cannot be copied")

    def __reduce__(self):
        raise TypeError("Phase3ProcessClaim is process-local")


@dataclass(frozen=True, slots=True)
class Phase3ClaimReceipt:
    authority_id: str
    durable_command_key: str
    client_order_id: str
    claim: Phase3ProcessClaim | None
    recovered: bool = False
