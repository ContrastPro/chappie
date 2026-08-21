"""One-shot, observation-only M4.10 Phase 1 Testnet preflight."""

from __future__ import annotations

import os
import sys
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from time import time_ns
from types import MappingProxyType
from typing import Protocol
from uuid import uuid4

import httpx

from app.exchange_execution.models import require_account_fingerprint
from app.exchange_execution.ports import (
    AccountBalance,
    ExchangeInstrumentRules,
)
from app.foundation.logging import suppress_private_transport_logging
from app.infrastructure.binance import (
    AccountTradeObservation,
    BinanceRateLimitError,
    BinanceSpotTestnetAdapter,
    BinanceSpotTestnetConfig,
    OpenOrderObservation,
)
from app.market.models import MarketStreamKey

PHASE1_AUTHORIZATION_ENV = "M4_10_PHASE1_AUTHORIZED"
PHASE1_AUTHORIZATION_VALUE = "YES"
SPOT_TESTNET_HOST = "https://testnet.binance.vision"
EVIDENCE_PATH = Path("logs/m4_10_phase1_retry1.log")
MAX_REQUESTS = 5
ALLOWED_ENDPOINTS = frozenset(
    {
        ("GET", "/api/v3/exchangeInfo"),
        ("GET", "/api/v3/account"),
        ("GET", "/api/v3/openOrders"),
        ("GET", "/api/v3/myTrades"),
        ("GET", "/api/v3/order"),
    }
)


class Phase1Error(RuntimeError):
    """Base class for safe Phase 1 failures."""


class Phase1AuthorizationError(Phase1Error):
    """The exact Phase 1 authorization token was absent or invalid."""


class Phase1TransportViolation(Phase1Error):
    """A request violated the method, endpoint, host, or budget boundary."""


class Phase1Outcome(StrEnum):
    PASS = "PASS"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class Phase1RulesSummary:
    quantity_step: Decimal
    min_quantity: Decimal
    min_notional: Decimal
    rules_version: str


@dataclass(frozen=True, slots=True)
class Phase1Result:
    run_id: str
    observed_at_ms: int
    host: str
    account_fingerprint: str
    outcome: Phase1Outcome
    request_count: int
    endpoint_counts: Mapping[str, int]
    btc_free: Decimal | None = None
    btc_locked: Decimal | None = None
    usdt_free: Decimal | None = None
    usdt_locked: Decimal | None = None
    open_order_count: int = 0
    managed_open_order_count: int = 0
    unmanaged_open_order_count: int = 0
    recent_trade_count: int = 0
    managed_trade_count: int = 0
    unsupported_commission_count: int = 0
    rules: Phase1RulesSummary | None = None
    retry_after_seconds: int | None = None
    stop_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "endpoint_counts", MappingProxyType(dict(self.endpoint_counts)))

    def render(self) -> str:
        """Render only normalized evidence; never transport or credential material."""

        lines = (
            f"outcome={self.outcome.value}",
            f"run_id={self.run_id}",
            f"observed_at_ms={self.observed_at_ms}",
            f"host={self.host}",
            f"account_fingerprint={self.account_fingerprint}",
            f"request_count={self.request_count}",
            "endpoint_counts=" + ",".join(f"{k}:{v}" for k, v in self.endpoint_counts.items()),
            f"btc_free={self.btc_free}",
            f"btc_locked={self.btc_locked}",
            f"usdt_free={self.usdt_free}",
            f"usdt_locked={self.usdt_locked}",
            f"open_order_count={self.open_order_count}",
            f"managed_open_order_count={self.managed_open_order_count}",
            f"unmanaged_open_order_count={self.unmanaged_open_order_count}",
            f"recent_trade_count={self.recent_trade_count}",
            f"managed_trade_count={self.managed_trade_count}",
            f"unsupported_commission_count={self.unsupported_commission_count}",
            f"rules_quantity_step={self.rules.quantity_step if self.rules else None}",
            f"rules_min_quantity={self.rules.min_quantity if self.rules else None}",
            f"rules_min_notional={self.rules.min_notional if self.rules else None}",
            f"rules_version={self.rules.rules_version if self.rules else None}",
            f"retry_after_seconds={self.retry_after_seconds}",
            "stop_reasons=" + ",".join(self.stop_reasons),
        )
        return "\n".join(lines) + "\n"


class Phase1Reader(Protocol):
    def read_instrument_rules(self, stream_key: MarketStreamKey) -> ExchangeInstrumentRules: ...
    def read_balances(self, assets: tuple[str, ...]) -> tuple[AccountBalance, ...]: ...
    def read_open_order_observations(
        self, *, stream_key: MarketStreamKey
    ) -> tuple[OpenOrderObservation, ...]: ...
    def read_account_trade_observations(
        self, *, stream_key: MarketStreamKey, start_at_ms: int
    ) -> tuple[AccountTradeObservation, ...]: ...


class Phase1ReadFacade:
    """Capability-narrowing facade: its public API contains only four approved reads."""

    __slots__ = ("__reader",)

    def __init__(self, reader: Phase1Reader) -> None:
        self.__reader = reader

    def read_instrument_rules(self, stream_key: MarketStreamKey) -> ExchangeInstrumentRules:
        return self.__reader.read_instrument_rules(stream_key)

    def read_balances(self, assets: tuple[str, ...]) -> tuple[AccountBalance, ...]:
        return self.__reader.read_balances(assets)

    def read_open_order_observations(
        self, *, stream_key: MarketStreamKey
    ) -> tuple[OpenOrderObservation, ...]:
        return self.__reader.read_open_order_observations(stream_key=stream_key)

    def read_account_trade_observations(
        self, *, stream_key: MarketStreamKey, start_at_ms: int
    ) -> tuple[AccountTradeObservation, ...]:
        return self.__reader.read_account_trade_observations(
            stream_key=stream_key, start_at_ms=start_at_ms
        )


class Phase1GuardTransport(httpx.BaseTransport):
    """Enforce the fixed host, GET-only whitelist, and hard request budget."""

    def __init__(self, transport: httpx.BaseTransport, *, max_requests: int = MAX_REQUESTS) -> None:
        self._transport = transport
        self._max_requests = max_requests
        self._counts: Counter[str] = Counter()
        self._closed = False

    @property
    def request_count(self) -> int:
        return sum(self._counts.values())

    @property
    def endpoint_counts(self) -> Mapping[str, int]:
        return MappingProxyType(dict(sorted(self._counts.items())))

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        method_path = (request.method, request.url.path)
        if request.url.scheme != "https" or request.url.netloc != b"testnet.binance.vision":
            raise Phase1TransportViolation("Phase 1 host is not approved")
        if method_path not in ALLOWED_ENDPOINTS:
            raise Phase1TransportViolation("Phase 1 method or endpoint is not approved")
        if self.request_count >= self._max_requests:
            raise Phase1TransportViolation("Phase 1 request budget exhausted")
        key = f"{request.method} {request.url.path}"
        self._counts[key] += 1
        return self._transport.handle_request(request)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._transport.close()


def run_phase1(
    reader: Phase1ReadFacade,
    guard: Phase1GuardTransport,
    *,
    account_fingerprint: str,
    run_id: str,
    now_ms: int,
    history_start_ms: int = 0,
) -> Phase1Result:
    """Run four deterministic reads; fail fast after an unsafe observation."""

    require_account_fingerprint(account_fingerprint)
    stream = MarketStreamKey("BTCUSDT", "1m")
    suppress_private_transport_logging()
    common = {
        "run_id": run_id,
        "observed_at_ms": now_ms,
        "host": SPOT_TESTNET_HOST,
        "account_fingerprint": account_fingerprint,
    }
    try:
        rules = reader.read_instrument_rules(stream)
        balances = {value.asset: value for value in reader.read_balances(("BTC", "USDT"))}
        open_orders = reader.read_open_order_observations(stream_key=stream)
        if open_orders:
            btc, usdt = balances["BTC"], balances["USDT"]
            managed_open = sum(value.client_order_id.startswith("ctt1t-") for value in open_orders)
            return Phase1Result(
                **common,
                outcome=Phase1Outcome.BLOCKED,
                request_count=guard.request_count,
                endpoint_counts=guard.endpoint_counts,
                btc_free=btc.free,
                btc_locked=btc.locked,
                usdt_free=usdt.free,
                usdt_locked=usdt.locked,
                open_order_count=len(open_orders),
                managed_open_order_count=managed_open,
                unmanaged_open_order_count=len(open_orders) - managed_open,
                rules=_rules_summary(rules),
                stop_reasons=("unexpected_open_order",),
            )
        history = reader.read_account_trade_observations(
            stream_key=stream, start_at_ms=history_start_ms
        )
        managed = tuple(
            t
            for t in history
            if t.client_order_id is not None and t.client_order_id.startswith("ctt1t-")
        )
        unsupported = tuple(t for t in history if t.commission_asset not in {"BTC", "USDT"})
        reasons = tuple(
            reason
            for condition, reason in (
                (bool(managed), "unexpected_managed_trade"),
                (bool(history), "unexpected_trade_history"),
                (bool(unsupported), "unsupported_commission_asset"),
            )
            if condition
        )
        btc, usdt = balances["BTC"], balances["USDT"]
        return Phase1Result(
            **common,
            outcome=Phase1Outcome.BLOCKED if reasons else Phase1Outcome.PASS,
            request_count=guard.request_count,
            endpoint_counts=guard.endpoint_counts,
            btc_free=btc.free,
            btc_locked=btc.locked,
            usdt_free=usdt.free,
            usdt_locked=usdt.locked,
            recent_trade_count=len(history),
            managed_trade_count=len(managed),
            unsupported_commission_count=len(unsupported),
            rules=_rules_summary(rules),
            stop_reasons=reasons,
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except BinanceRateLimitError as exc:
        return Phase1Result(
            **common,
            outcome=Phase1Outcome.FAILED,
            request_count=guard.request_count,
            endpoint_counts=guard.endpoint_counts,
            retry_after_seconds=exc.retry_after_seconds,
            stop_reasons=(type(exc).__name__,),
        )
    except Exception as exc:  # noqa: BLE001 - boundary converts failures to sanitized evidence
        return Phase1Result(
            **common,
            outcome=Phase1Outcome.FAILED,
            request_count=guard.request_count,
            endpoint_counts=guard.endpoint_counts,
            stop_reasons=(type(exc).__name__,),
        )


def _rules_summary(rules: ExchangeInstrumentRules) -> Phase1RulesSummary:
    return Phase1RulesSummary(
        rules.quantity_step, rules.min_quantity, rules.min_notional, rules.rules_version
    )


class _NoKnownOrderResolver:
    def by_client_order_id(self, client_order_id: str) -> None:
        return None

    def by_exchange_order_id(self, exchange_order_id: str) -> None:
        return None


def main() -> int:
    """Execute only after exact authorization and refuse ambiguous evidence collisions."""

    suppress_private_transport_logging()
    if os.environ.get(PHASE1_AUTHORIZATION_ENV) != PHASE1_AUTHORIZATION_VALUE:
        print("Phase 1 authorization token is absent or invalid", file=sys.stderr)
        return 2
    if EVIDENCE_PATH.exists():
        print("Phase 1 evidence path already exists", file=sys.stderr)
        return 2
    api_key = os.environ.get("BINANCE_TESTNET_API_KEY", "")
    api_secret = os.environ.get("BINANCE_TESTNET_SECRET_KEY", "")
    fingerprint = os.environ.get("BINANCE_TESTNET_ACCOUNT_FINGERPRINT", "")
    try:
        require_account_fingerprint(fingerprint)
        config = BinanceSpotTestnetConfig(api_key, api_secret, fingerprint)
    except (TypeError, ValueError):
        print("Phase 1 configuration is invalid", file=sys.stderr)
        return 2
    guard = Phase1GuardTransport(httpx.HTTPTransport())
    client = httpx.Client(transport=guard)
    adapter = BinanceSpotTestnetAdapter(
        config,
        client=client,
        timestamp_provider=lambda: time_ns() // 1_000_000,
        identity_resolver=_NoKnownOrderResolver(),
    )
    try:
        result = run_phase1(
            Phase1ReadFacade(adapter),
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
            if result.outcome is Phase1Outcome.PASS
            else 2
            if result.outcome is Phase1Outcome.BLOCKED
            else 1
        )
    finally:
        adapter.close()
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
