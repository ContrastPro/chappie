"""One-shot M4.10 Phase 2 signed order-test preflight."""

from __future__ import annotations

import os
import sys
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from time import time_ns
from types import MappingProxyType
from typing import Protocol
from uuid import uuid4

import httpx

from app.exchange_execution.factories import DeterministicClientOrderIdFactory
from app.exchange_execution.models import (
    ExchangeOrderSide,
    ExecutionEnvironment,
    require_account_fingerprint,
)
from app.foundation.logging import suppress_private_transport_logging
from app.infrastructure.binance import (
    BinanceRateLimitError,
    BinanceSpotTestnetAdapter,
    BinanceSpotTestnetConfig,
    OrderTestPreflightResult,
)
from app.market.models import MarketStreamKey

PHASE2_AUTHORIZATION_ENV = "M4_10_PHASE2_AUTHORIZED"
PHASE2_AUTHORIZATION_VALUE = "YES"
SPOT_TESTNET_HOST = "https://testnet.binance.vision"
EVIDENCE_PATH = Path("logs/m4_10_phase2.log")
MAX_REQUESTS = 1
ALLOWED_ENDPOINT = ("POST", "/api/v3/order/test")
SYMBOL = "BTCUSDT"
SIDE = ExchangeOrderSide.BUY
QUANTITY = Decimal("0.00016000")
REFERENCE_PRICE = Decimal("63849.22000000")
QUANTITY_STEP = Decimal("0.00001000")
MIN_QUANTITY = Decimal("0.00001000")
MIN_NOTIONAL = Decimal("5.00000000")
MIN_NOTIONAL_MARGIN = Decimal(2)


class Phase2TransportViolation(RuntimeError):
    """The request violated the exact Phase 2 transport boundary."""


class Phase2ValidationError(ValueError):
    """Reviewed Phase 2 inputs are invalid or stale by construction."""


class Phase2Outcome(StrEnum):
    PASS = "PASS"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class Phase2Result:
    run_id: str
    observed_at_ms: int
    host: str
    account_fingerprint: str
    outcome: Phase2Outcome
    request_count: int
    endpoint_counts: Mapping[str, int]
    symbol: str
    side: str
    quantity: Decimal
    client_order_id: str
    validated: bool
    retry_after_seconds: int | None = None
    stop_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "endpoint_counts", MappingProxyType(dict(self.endpoint_counts)))

    def render(self) -> str:
        lines = (
            f"outcome={self.outcome.value}",
            f"run_id={self.run_id}",
            f"observed_at_ms={self.observed_at_ms}",
            f"host={self.host}",
            f"account_fingerprint={self.account_fingerprint}",
            f"request_count={self.request_count}",
            "endpoint_counts=" + ",".join(f"{k}:{v}" for k, v in self.endpoint_counts.items()),
            f"symbol={self.symbol}",
            f"side={self.side}",
            f"quantity={self.quantity}",
            f"client_order_id={self.client_order_id}",
            f"validated={self.validated}",
            f"retry_after_seconds={self.retry_after_seconds}",
            "stop_reasons=" + ",".join(self.stop_reasons),
            "financial_semantics=none",
        )
        return "\n".join(lines) + "\n"


class Phase2OrderTester(Protocol):
    def test_market_order(
        self,
        *,
        stream_key: MarketStreamKey,
        side: ExchangeOrderSide,
        base_quantity: Decimal,
        client_order_id: str,
    ) -> OrderTestPreflightResult: ...


class Phase2OrderTestFacade:
    """Capability-narrowing facade exposing only the non-financial order-test method."""

    __slots__ = ("__tester",)

    def __init__(self, tester: Phase2OrderTester) -> None:
        self.__tester = tester

    def test_market_order(
        self,
        *,
        stream_key: MarketStreamKey,
        side: ExchangeOrderSide,
        base_quantity: Decimal,
        client_order_id: str,
    ) -> OrderTestPreflightResult:
        return self.__tester.test_market_order(
            stream_key=stream_key,
            side=side,
            base_quantity=base_quantity,
            client_order_id=client_order_id,
        )


class Phase2GuardTransport(httpx.BaseTransport):
    """Permit exactly one fixed-host POST to the Binance order-test endpoint."""

    def __init__(self, transport: httpx.BaseTransport) -> None:
        self._transport = transport
        self._counts: Counter[str] = Counter()
        self._closed = False

    @property
    def request_count(self) -> int:
        return sum(self._counts.values())

    @property
    def endpoint_counts(self) -> Mapping[str, int]:
        return MappingProxyType(dict(sorted(self._counts.items())))

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.scheme != "https" or request.url.netloc != b"testnet.binance.vision":
            raise Phase2TransportViolation("Phase 2 host is not approved")
        if (request.method, request.url.path) != ALLOWED_ENDPOINT:
            raise Phase2TransportViolation("Phase 2 method or endpoint is not approved")
        if self.request_count >= MAX_REQUESTS:
            raise Phase2TransportViolation("Phase 2 request budget exhausted")
        self._counts["POST /api/v3/order/test"] += 1
        return self._transport.handle_request(request)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._transport.close()


def validate_reviewed_inputs(
    *,
    quantity: Decimal,
    reference_price: Decimal,
    quantity_step: Decimal = QUANTITY_STEP,
    min_quantity: Decimal = MIN_QUANTITY,
    min_notional: Decimal = MIN_NOTIONAL,
) -> None:
    values = (quantity, reference_price, quantity_step, min_quantity, min_notional)
    if any(
        not isinstance(value, Decimal) or not value.is_finite() or value <= 0 for value in values
    ):
        raise Phase2ValidationError("Phase 2 financial inputs must be positive finite Decimals")
    if quantity < min_quantity:
        raise Phase2ValidationError("Phase 2 quantity is below minQty")
    if quantity % quantity_step != 0:
        raise Phase2ValidationError("Phase 2 quantity is not aligned to stepSize")
    if quantity * reference_price < min_notional * MIN_NOTIONAL_MARGIN:
        raise Phase2ValidationError("Phase 2 reviewed notional lacks the required safety margin")


def phase2_client_order_id(account_fingerprint: str) -> str:
    """Create a deterministic request-contract ID without durable submission identity."""

    require_account_fingerprint(account_fingerprint)
    reviewed = (
        f"m4.10-phase2-order-test|{account_fingerprint}|{SYMBOL}|{SIDE.value}|"
        f"{QUANTITY}|{REFERENCE_PRICE}"
    )
    command_key = sha256(reviewed.encode()).hexdigest()
    return DeterministicClientOrderIdFactory().create_client_order_id(
        environment=ExecutionEnvironment.TESTNET,
        venue="binance-spot",
        account_fingerprint=account_fingerprint,
        durable_command_key=command_key,
    )


def run_phase2(
    facade: Phase2OrderTestFacade,
    guard: Phase2GuardTransport,
    *,
    account_fingerprint: str,
    run_id: str,
    now_ms: int,
    quantity: Decimal = QUANTITY,
    reference_price: Decimal = REFERENCE_PRICE,
) -> Phase2Result:
    """Validate inputs then perform exactly one non-financial order-test request."""

    suppress_private_transport_logging()
    client_order_id = phase2_client_order_id(account_fingerprint)
    common = {
        "run_id": run_id,
        "observed_at_ms": now_ms,
        "host": SPOT_TESTNET_HOST,
        "account_fingerprint": account_fingerprint,
        "symbol": SYMBOL,
        "side": SIDE.value,
        "quantity": quantity,
        "client_order_id": client_order_id,
    }
    try:
        validate_reviewed_inputs(quantity=quantity, reference_price=reference_price)
        result = facade.test_market_order(
            stream_key=MarketStreamKey(SYMBOL, "1m"),
            side=SIDE,
            base_quantity=quantity,
            client_order_id=client_order_id,
        )
        return Phase2Result(
            **common,
            outcome=Phase2Outcome.PASS,
            request_count=guard.request_count,
            endpoint_counts=guard.endpoint_counts,
            validated=result.validated,
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except BinanceRateLimitError as exc:
        return Phase2Result(
            **common,
            outcome=Phase2Outcome.FAILED,
            request_count=guard.request_count,
            endpoint_counts=guard.endpoint_counts,
            validated=False,
            retry_after_seconds=exc.retry_after_seconds,
            stop_reasons=(type(exc).__name__,),
        )
    except Exception as exc:  # noqa: BLE001 - boundary emits only sanitized failure taxonomy
        return Phase2Result(
            **common,
            outcome=Phase2Outcome.BLOCKED
            if isinstance(exc, (Phase2ValidationError, Phase2TransportViolation))
            else Phase2Outcome.FAILED,
            request_count=guard.request_count,
            endpoint_counts=guard.endpoint_counts,
            validated=False,
            stop_reasons=(type(exc).__name__,),
        )


class _NoIdentities:
    def by_client_order_id(self, client_order_id: str) -> None:
        return None

    def by_exchange_order_id(self, exchange_order_id: str) -> None:
        return None


def main() -> int:
    """Execute only after exact Phase 2 authorization and evidence collision checks."""

    suppress_private_transport_logging()
    if os.environ.get(PHASE2_AUTHORIZATION_ENV) != PHASE2_AUTHORIZATION_VALUE:
        print("Phase 2 authorization token is absent or invalid", file=sys.stderr)
        return 2
    if EVIDENCE_PATH.exists():
        print("Phase 2 evidence path already exists", file=sys.stderr)
        return 2
    api_key = os.environ.get("BINANCE_TESTNET_API_KEY", "")
    api_secret = os.environ.get("BINANCE_TESTNET_SECRET_KEY", "")
    fingerprint = os.environ.get("BINANCE_TESTNET_ACCOUNT_FINGERPRINT", "")
    try:
        require_account_fingerprint(fingerprint)
        validate_reviewed_inputs(quantity=QUANTITY, reference_price=REFERENCE_PRICE)
        config = BinanceSpotTestnetConfig(api_key, api_secret, fingerprint)
    except (TypeError, ValueError):
        print("Phase 2 configuration is invalid", file=sys.stderr)
        return 2
    guard = Phase2GuardTransport(httpx.HTTPTransport())
    client = httpx.Client(transport=guard)
    adapter = BinanceSpotTestnetAdapter(
        config,
        client=client,
        timestamp_provider=lambda: time_ns() // 1_000_000,
        identity_resolver=_NoIdentities(),
    )
    try:
        result = run_phase2(
            Phase2OrderTestFacade(adapter),
            guard,
            account_fingerprint=fingerprint,
            run_id=str(uuid4()),
            now_ms=time_ns() // 1_000_000,
        )
        EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
        EVIDENCE_PATH.write_text(result.render(), encoding="utf-8")
        print(result.render(), end="")
        return (
            0
            if result.outcome is Phase2Outcome.PASS
            else 2
            if result.outcome is Phase2Outcome.BLOCKED
            else 1
        )
    finally:
        adapter.close()
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
