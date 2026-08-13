from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any, Protocol, Self, runtime_checkable

import httpx

from app.exchange_execution.models import (
    ExchangeOrder,
    ExchangeOrderSide,
    ExchangeOrderStatus,
    ExchangeTrade,
    ExchangeTradeIdentity,
    ExecutionEnvironment,
    QueryResultKind,
    TransportFailureCategory,
    TransportOutcomeKind,
    require_account_fingerprint,
    require_client_order_id,
    require_hash,
)
from app.exchange_execution.ports import (
    AccountBalance,
    ExchangeInstrumentRules,
    ExchangeTradeHistory,
    MarketOrderSubmission,
    OrderQueryResult,
    SubmissionObservation,
)
from app.infrastructure.binance.exceptions import (
    BinanceAuthenticationError,
    BinanceConfigurationError,
    BinanceNormalizationError,
    BinanceOrderRejectedError,
    BinanceOrderTestPreflightError,
    BinanceRateLimitError,
    BinanceResponseError,
    BinanceTransportError,
    BinanceUnsupportedContractError,
)
from app.infrastructure.binance.signing import ParameterValue, encode_parameters, sign_hmac_sha256
from app.market.models import MarketStreamKey

SPOT_TESTNET_BASE_URL = "https://testnet.binance.vision"
AUTH_ERROR_CODES = {-1002, -1022, -2014, -2015}
TIMESTAMP_ERROR_CODE = -1021
TIMEOUT_ERROR_CODE = -1007
NOT_FOUND_ERROR_CODE = -2013
SAFE_REJECTION_CODES = {
    -1013,
    -1100,
    -1101,
    -1102,
    -1103,
    -1104,
    -1105,
    -1106,
    -1108,
    -1111,
    -1112,
    -1121,
    -2010,
}


@runtime_checkable
class TimestampProvider(Protocol):
    def __call__(self) -> int: ...


@dataclass(frozen=True, slots=True)
class OrderIdentity:
    durable_command_key: str
    client_order_id: str
    side: ExchangeOrderSide
    original_base_quantity: Decimal
    created_at_ms: int

    def __post_init__(self) -> None:
        require_hash(self.durable_command_key, "durable_command_key")
        require_client_order_id(self.client_order_id)
        if not isinstance(self.side, ExchangeOrderSide):
            raise BinanceConfigurationError("order identity side is invalid")
        if (
            not isinstance(self.original_base_quantity, Decimal)
            or not self.original_base_quantity.is_finite()
            or self.original_base_quantity <= 0
        ):
            raise BinanceConfigurationError("order identity quantity must be positive Decimal")
        if (
            isinstance(self.created_at_ms, bool)
            or not isinstance(self.created_at_ms, int)
            or self.created_at_ms < 0
        ):
            raise BinanceConfigurationError("order identity timestamp must be non-negative")


@dataclass(frozen=True, slots=True)
class OrderTestPreflightResult:
    """Successful validation only; deliberately carries no exchange or financial identity."""

    @property
    def validated(self) -> bool:
        return True


@runtime_checkable
class OrderIdentityResolver(Protocol):
    def by_client_order_id(self, client_order_id: str) -> OrderIdentity | None: ...

    def by_exchange_order_id(self, exchange_order_id: str) -> OrderIdentity | None: ...


@dataclass(frozen=True, slots=True, repr=False)
class BinanceSpotTestnetConfig:
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)
    account_fingerprint: str
    base_url: str = SPOT_TESTNET_BASE_URL
    recv_window_ms: int = 5000
    request_timeout_seconds: Decimal = Decimal(10)
    adapter_version: str = "binance-spot-testnet/v1"

    def __post_init__(self) -> None:
        if self.base_url != SPOT_TESTNET_BASE_URL:
            raise BinanceConfigurationError("only the approved Binance Spot Testnet URL is allowed")
        if not isinstance(self.api_key, str) or not self.api_key:
            raise BinanceConfigurationError("API key must be non-empty")
        if not isinstance(self.api_secret, str) or not self.api_secret:
            raise BinanceConfigurationError("API secret must be non-empty")
        require_account_fingerprint(self.account_fingerprint)
        if (
            isinstance(self.recv_window_ms, bool)
            or not isinstance(self.recv_window_ms, int)
            or not 1 <= self.recv_window_ms <= 60_000
        ):
            raise BinanceConfigurationError("recv_window_ms must be between 1 and 60000")
        if (
            not isinstance(self.request_timeout_seconds, Decimal)
            or not self.request_timeout_seconds.is_finite()
            or self.request_timeout_seconds <= 0
        ):
            raise BinanceConfigurationError("request timeout must be a positive Decimal")
        if not isinstance(self.adapter_version, str) or not self.adapter_version.strip():
            raise BinanceConfigurationError("adapter_version must be non-empty")

    def __repr__(self) -> str:
        return (
            "BinanceSpotTestnetConfig("
            f"account_fingerprint={self.account_fingerprint!r}, "
            f"base_url={self.base_url!r}, recv_window_ms={self.recv_window_ms!r}, "
            f"request_timeout_seconds={self.request_timeout_seconds!r}, "
            f"adapter_version={self.adapter_version!r}, api_key=<redacted>, api_secret=<redacted>)"
        )


class BinanceSpotTestnetAdapter:
    """Synchronous, no-retry private adapter. The injected client remains caller-owned."""

    def __init__(
        self,
        config: BinanceSpotTestnetConfig,
        *,
        client: httpx.Client,
        timestamp_provider: TimestampProvider,
        identity_resolver: OrderIdentityResolver,
    ) -> None:
        if not isinstance(config, BinanceSpotTestnetConfig):
            raise BinanceConfigurationError("config must be BinanceSpotTestnetConfig")
        if not isinstance(client, httpx.Client):
            raise BinanceConfigurationError("client must be httpx.Client")
        if not callable(timestamp_provider):
            raise BinanceConfigurationError("timestamp_provider must be callable")
        if not isinstance(identity_resolver, OrderIdentityResolver):
            raise BinanceConfigurationError("identity_resolver does not implement its protocol")
        self._config = config
        self._client = client
        self._timestamp_provider = timestamp_provider
        self._identity_resolver = identity_resolver
        self._closed = False

    def close(self) -> None:
        self._closed = True

    def __enter__(self) -> Self:
        self._ensure_open()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def submit_market_order(self, request: MarketOrderSubmission) -> SubmissionObservation:
        self._ensure_open()
        if not isinstance(request, MarketOrderSubmission):
            raise BinanceConfigurationError("request must be MarketOrderSubmission")
        if request.account_fingerprint != self._config.account_fingerprint:
            raise BinanceConfigurationError("submission account lineage mismatch")
        parameters: list[tuple[str, ParameterValue]] = [
            ("symbol", request.stream_key.symbol),
            ("side", request.side.value),
            ("type", "MARKET"),
            ("quantity", request.base_quantity),
            ("newClientOrderId", request.client_order_id),
            ("newOrderRespType", "ACK"),
            ("timestamp", self._timestamp()),
            ("recvWindow", self._config.recv_window_ms),
        ]
        try:
            response = self._send("POST", "/api/v3/order", parameters, signed=True)
        except httpx.TimeoutException:
            return self._ambiguous(request.client_order_id, TransportFailureCategory.TIMEOUT)
        except httpx.RequestError:
            return self._ambiguous(
                request.client_order_id, TransportFailureCategory.UNKNOWN_TRANSMISSION
            )
        if response.status_code >= 500:
            return self._ambiguous(request.client_order_id, TransportFailureCategory.HTTP_5XX)
        if response.status_code in {418, 429}:
            self._raise_rate_limit(response)
        if 300 <= response.status_code < 400:
            return self._ambiguous(
                request.client_order_id, TransportFailureCategory.UNKNOWN_TRANSMISSION
            )
        error = self._error_payload(response)
        if error is not None:
            code, message = error
            if code == TIMEOUT_ERROR_CODE:
                return self._ambiguous(request.client_order_id, TransportFailureCategory.TIMEOUT)
            self._raise_private_error(response, code, message, submit=True)
        if response.status_code >= 400:
            return self._ambiguous(
                request.client_order_id, TransportFailureCategory.UNKNOWN_TRANSMISSION
            )
        try:
            payload = self._json_object(response)
            if self._string(payload, "symbol") != request.stream_key.symbol:
                raise BinanceNormalizationError("ACK symbol mismatch")
            client_id = self._string(payload, "clientOrderId")
            if client_id != request.client_order_id:
                raise BinanceNormalizationError("ACK client order ID mismatch")
            order_id = str(self._integer(payload, "orderId"))
            transact_time = self._integer(payload, "transactTime")
        except BinanceResponseError:
            return self._ambiguous(request.client_order_id, TransportFailureCategory.RESPONSE_LOST)
        return SubmissionObservation(
            TransportOutcomeKind.RESPONSE_RECEIVED,
            request.client_order_id,
            order_id,
            ExchangeOrderStatus.ACKNOWLEDGED,
            transact_time,
        )

    def test_market_order(
        self,
        *,
        stream_key: MarketStreamKey,
        side: ExchangeOrderSide,
        base_quantity: Decimal,
        client_order_id: str,
    ) -> OrderTestPreflightResult:
        """Validate one signed MARKET request without entering the durable order lifecycle."""

        self._validate_stream(stream_key)
        if not isinstance(side, ExchangeOrderSide):
            raise BinanceConfigurationError("order-test side is invalid")
        if (
            not isinstance(base_quantity, Decimal)
            or not base_quantity.is_finite()
            or base_quantity <= 0
        ):
            raise BinanceConfigurationError("order-test quantity must be positive Decimal")
        require_client_order_id(client_order_id)
        parameters: list[tuple[str, ParameterValue]] = [
            ("symbol", stream_key.symbol),
            ("side", side.value),
            ("type", "MARKET"),
            ("quantity", base_quantity),
            ("newClientOrderId", client_order_id),
            ("newOrderRespType", "ACK"),
            ("timestamp", self._timestamp()),
            ("recvWindow", self._config.recv_window_ms),
        ]
        try:
            response = self._send("POST", "/api/v3/order/test", parameters, signed=True)
        except httpx.TimeoutException:
            raise BinanceOrderTestPreflightError(category="timeout") from None
        except httpx.RequestError:
            raise BinanceOrderTestPreflightError(category="transport") from None
        if response.status_code in {418, 429}:
            self._raise_rate_limit(response)
        if response.status_code >= 500:
            raise BinanceOrderTestPreflightError(
                category="http_5xx", status_code=response.status_code
            )
        if 300 <= response.status_code < 400:
            raise BinanceOrderTestPreflightError(
                category="redirect", status_code=response.status_code
            )
        error = self._error_payload(response)
        if error is not None:
            self._raise_private_error(response, *error, submit=False)
        if response.status_code >= 400:
            raise BinanceOrderTestPreflightError(
                category="http_error", status_code=response.status_code
            )
        payload = self._json_object(response)
        if payload:
            raise BinanceNormalizationError("order-test success response must be empty")
        return OrderTestPreflightResult()

    def query_order(self, *, stream_key: MarketStreamKey, client_order_id: str) -> OrderQueryResult:
        self._validate_stream(stream_key)
        require_client_order_id(client_order_id)
        identity = self._resolve_client_identity(client_order_id)
        response = self._signed_get(
            "/api/v3/order",
            [("symbol", stream_key.symbol), ("origClientOrderId", client_order_id)],
        )
        error = self._error_payload(response)
        if error is not None and error[0] == NOT_FOUND_ERROR_CODE:
            return OrderQueryResult(QueryResultKind.NOT_FOUND)
        self._raise_if_error(response, error)
        return OrderQueryResult(
            QueryResultKind.FOUND, self._normalize_order(response, identity, stream_key)
        )

    def list_order_trades(
        self, *, stream_key: MarketStreamKey, exchange_order_id: str
    ) -> tuple[ExchangeTrade, ...]:
        self._validate_stream(stream_key)
        if not isinstance(exchange_order_id, str) or not exchange_order_id:
            raise BinanceConfigurationError("exchange_order_id must be non-empty")
        identity = self._resolve_exchange_identity(exchange_order_id)
        response = self._signed_get(
            "/api/v3/myTrades",
            [("symbol", stream_key.symbol), ("orderId", exchange_order_id)],
        )
        self._raise_if_error(response, self._error_payload(response))
        payload = self._json_list(response)
        return tuple(
            self._normalize_trade(value, identity, stream_key, exchange_order_id)
            for value in payload
        )

    def list_open_orders(self, *, stream_key: MarketStreamKey) -> tuple[ExchangeOrder, ...]:
        self._validate_stream(stream_key)
        response = self._signed_get("/api/v3/openOrders", [("symbol", stream_key.symbol)])
        self._raise_if_error(response, self._error_payload(response))
        orders: list[ExchangeOrder] = []
        for value in self._json_list(response):
            if not isinstance(value, dict):
                raise BinanceNormalizationError("open order entry must be an object")
            client_id = self._string(value, "clientOrderId")
            identity = self._identity_resolver.by_client_order_id(client_id)
            if identity is None:
                raise BinanceUnsupportedContractError("open order identity is unavailable")
            orders.append(self._normalize_order_payload(value, identity, stream_key))
        return tuple(orders)

    def read_trade_history(
        self, *, stream_key: MarketStreamKey, start_at_ms: int
    ) -> ExchangeTradeHistory:
        self._validate_stream(stream_key)
        if isinstance(start_at_ms, bool) or not isinstance(start_at_ms, int) or start_at_ms < 0:
            raise BinanceConfigurationError("start_at_ms must be non-negative")
        coverage_end_ms = self._timestamp_provider()
        response = self._signed_get(
            "/api/v3/myTrades",
            [("symbol", stream_key.symbol), ("startTime", start_at_ms), ("limit", 1000)],
        )
        self._raise_if_error(response, self._error_payload(response))
        payload = self._json_list(response)
        trades: list[ExchangeTrade] = []
        for value in payload:
            if not isinstance(value, dict):
                raise BinanceNormalizationError("trade entry must be an object")
            exchange_order_id = str(self._integer(value, "orderId"))
            identity = self._resolve_exchange_identity(exchange_order_id)
            trades.append(self._normalize_trade(value, identity, stream_key, exchange_order_id))
        return ExchangeTradeHistory(
            tuple(trades), start_at_ms, coverage_end_ms, len(payload) < 1000
        )

    def read_balances(self, assets: tuple[str, ...]) -> tuple[AccountBalance, ...]:
        self._ensure_open()
        if not isinstance(assets, tuple) or any(not isinstance(v, str) or not v for v in assets):
            raise BinanceConfigurationError("assets must be a tuple of non-empty strings")
        if len(set(assets)) != len(assets):
            raise BinanceConfigurationError("assets must not duplicate")
        response = self._signed_get("/api/v3/account", [("omitZeroBalances", False)])
        self._raise_if_error(response, self._error_payload(response))
        payload = self._json_object(response)
        balances = payload.get("balances")
        if not isinstance(balances, list):
            raise BinanceNormalizationError("account balances must be a list")
        selected: dict[str, AccountBalance] = {}
        for value in balances:
            if not isinstance(value, dict):
                raise BinanceNormalizationError("balance entry must be an object")
            asset = self._string(value, "asset")
            if asset in assets:
                selected[asset] = AccountBalance(
                    asset,
                    self._decimal(value, "free", non_negative=True),
                    self._decimal(value, "locked", non_negative=True),
                )
        if set(selected) != set(assets):
            raise BinanceNormalizationError("requested account balance is missing")
        return tuple(selected[asset] for asset in assets)

    def read_instrument_rules(self, stream_key: MarketStreamKey) -> ExchangeInstrumentRules:
        self._validate_stream(stream_key)
        response = self._request(
            "GET", "/api/v3/exchangeInfo", [("symbol", stream_key.symbol)], signed=False
        )
        self._raise_if_error(response, self._error_payload(response))
        payload = self._json_object(response)
        symbols = payload.get("symbols")
        if not isinstance(symbols, list) or len(symbols) != 1 or not isinstance(symbols[0], dict):
            raise BinanceNormalizationError("exchangeInfo must contain exactly one symbol")
        symbol = symbols[0]
        if symbol.get("symbol") != stream_key.symbol:
            raise BinanceNormalizationError("exchangeInfo symbol mismatch")
        filters = symbol.get("filters")
        if not isinstance(filters, list):
            raise BinanceNormalizationError("exchangeInfo filters must be a list")
        by_type = {
            value.get("filterType"): value
            for value in filters
            if isinstance(value, dict) and isinstance(value.get("filterType"), str)
        }
        known_filters = {
            "PRICE_FILTER",
            "PERCENT_PRICE",
            "PERCENT_PRICE_BY_SIDE",
            "LOT_SIZE",
            "MIN_NOTIONAL",
            "NOTIONAL",
            "ICEBERG_PARTS",
            "MARKET_LOT_SIZE",
            "MAX_NUM_ORDERS",
            "MAX_NUM_ALGO_ORDERS",
            "MAX_NUM_ICEBERG_ORDERS",
            "MAX_POSITION",
            "TRAILING_DELTA",
        }
        unknown_filters = set(by_type) - known_filters
        if unknown_filters:
            raise BinanceUnsupportedContractError("unknown Binance symbol filter")
        lot = by_type.get("MARKET_LOT_SIZE")
        if isinstance(lot, dict) and (
            self._decimal(lot, "stepSize", non_negative=True).is_zero()
            or self._decimal(lot, "minQty", non_negative=True).is_zero()
        ):
            lot = by_type.get("LOT_SIZE")
        if not isinstance(lot, dict):
            raise BinanceUnsupportedContractError("market quantity filter is missing")
        step = self._decimal(lot, "stepSize", positive=True)
        minimum = self._decimal(lot, "minQty", positive=True)
        notional = by_type.get("NOTIONAL") or by_type.get("MIN_NOTIONAL")
        if not isinstance(notional, dict):
            raise BinanceUnsupportedContractError("market notional filter is missing")
        min_notional = self._decimal(notional, "minNotional", positive=True)
        if notional.get("applyMinToMarket", notional.get("applyToMarket", True)) is False:
            raise BinanceUnsupportedContractError("minimum notional does not apply to MARKET")
        if notional.get("applyMaxToMarket") is True:
            max_notional = self._decimal(notional, "maxNotional", non_negative=True)
            if not max_notional.is_zero():
                raise BinanceUnsupportedContractError(
                    "maximum MARKET notional is not representable by current port"
                )
        canonical = encode_parameters(
            [
                ("symbol", stream_key.symbol),
                ("stepSize", step),
                ("minQty", minimum),
                ("minNotional", min_notional),
            ]
        )
        return ExchangeInstrumentRules(
            stream_key, step, minimum, min_notional, sha256(canonical.encode()).hexdigest()
        )

    def _normalize_order(
        self,
        response: httpx.Response,
        identity: OrderIdentity,
        stream_key: MarketStreamKey,
    ) -> ExchangeOrder:
        return self._normalize_order_payload(self._json_object(response), identity, stream_key)

    def _normalize_order_payload(
        self,
        payload: dict[str, Any],
        identity: OrderIdentity,
        stream_key: MarketStreamKey,
    ) -> ExchangeOrder:
        if self._string(payload, "clientOrderId") != identity.client_order_id:
            raise BinanceNormalizationError("order client ID mismatch")
        if self._string(payload, "symbol") != "BTCUSDT":
            raise BinanceNormalizationError("order symbol mismatch")
        side = self._enum_side(payload.get("side"))
        if side is not identity.side:
            raise BinanceNormalizationError("order side mismatch")
        original = self._decimal(payload, "origQty", positive=True)
        if original != identity.original_base_quantity:
            raise BinanceNormalizationError("order original quantity mismatch")
        executed = self._decimal(payload, "executedQty", non_negative=True)
        quote = self._decimal(payload, "cummulativeQuoteQty", non_negative=True)
        status = self._status(payload.get("status"))
        return ExchangeOrder(
            identity.durable_command_key,
            identity.client_order_id,
            self._config.account_fingerprint,
            stream_key,
            side,
            original,
            status,
            identity.created_at_ms,
            str(self._integer(payload, "orderId")),
            executed,
            quote,
            latest_exchange_update_ms=self._optional_integer(payload, "updateTime"),
        )

    def _normalize_trade(
        self,
        value: object,
        identity: OrderIdentity,
        stream_key: MarketStreamKey,
        expected_exchange_order_id: str,
    ) -> ExchangeTrade:
        if not isinstance(value, dict):
            raise BinanceNormalizationError("trade entry must be an object")
        if self._string(value, "symbol") != stream_key.symbol:
            raise BinanceNormalizationError("trade symbol mismatch")
        exchange_order_id = str(self._integer(value, "orderId"))
        if exchange_order_id != expected_exchange_order_id:
            raise BinanceNormalizationError("trade order ID mismatch")
        side = ExchangeOrderSide.BUY if self._boolean(value, "isBuyer") else ExchangeOrderSide.SELL
        if side is not identity.side:
            raise BinanceNormalizationError("trade side mismatch")
        return ExchangeTrade(
            ExchangeTradeIdentity(
                "binance-spot",
                ExecutionEnvironment.TESTNET,
                self._config.account_fingerprint,
                stream_key.symbol,
                exchange_order_id,
                str(self._integer(value, "id")),
            ),
            identity.client_order_id,
            stream_key,
            side,
            self._decimal(value, "qty", positive=True),
            self._decimal(value, "price", positive=True),
            self._decimal(value, "quoteQty", positive=True),
            self._decimal(value, "commission", non_negative=True),
            self._string(value, "commissionAsset"),
            self._integer(value, "time"),
            self._boolean(value, "isMaker"),
        )

    def _signed_get(
        self, path: str, parameters: list[tuple[str, ParameterValue]]
    ) -> httpx.Response:
        return self._request(
            "GET",
            path,
            [
                *parameters,
                ("timestamp", self._timestamp()),
                ("recvWindow", self._config.recv_window_ms),
            ],
            signed=True,
        )

    def _request(
        self, method: str, path: str, parameters: list[tuple[str, ParameterValue]], *, signed: bool
    ) -> httpx.Response:
        try:
            return self._send(method, path, parameters, signed=signed)
        except httpx.RequestError:
            raise BinanceTransportError(
                f"Binance read request failed; method={method} path={path}"
            ) from None

    def _send(
        self, method: str, path: str, parameters: list[tuple[str, ParameterValue]], *, signed: bool
    ) -> httpx.Response:
        self._ensure_open()
        names = [name for name, _ in parameters]
        if "signature" in names:
            raise BinanceConfigurationError("signature is adapter-owned")
        if signed and (names.count("timestamp") != 1 or names.count("recvWindow") != 1):
            raise BinanceConfigurationError("signed timing parameters are adapter-owned")
        query = encode_parameters(parameters)
        if signed:
            query = f"{query}&signature={sign_hmac_sha256(query, self._config.api_secret)}"
        headers = {"X-MBX-APIKEY": self._config.api_key} if signed else {}
        timeout = float(self._config.request_timeout_seconds)
        if method == "POST":
            return self._client.post(
                f"{self._config.base_url}{path}",
                content=query,
                headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
                timeout=httpx.Timeout(timeout),
                follow_redirects=False,
            )
        return self._client.get(
            f"{self._config.base_url}{path}?{query}",
            headers=headers,
            timeout=httpx.Timeout(timeout),
            follow_redirects=False,
        )

    def _raise_if_error(self, response: httpx.Response, error: tuple[int, str] | None) -> None:
        if response.status_code in {418, 429}:
            self._raise_rate_limit(response)
        if error is not None:
            self._raise_private_error(response, *error, submit=False)
        if response.status_code >= 400:
            raise BinanceResponseError(f"Binance returned HTTP {response.status_code}")

    def _raise_private_error(
        self, response: httpx.Response, code: int, message: str, *, submit: bool
    ) -> None:
        if response.status_code in {418, 429}:
            raise BinanceRateLimitError(
                status_code=response.status_code,
                retry_after_seconds=self._retry_after(response.headers.get("Retry-After")),
            )
        if code in AUTH_ERROR_CODES:
            raise BinanceAuthenticationError(code=code, status_code=response.status_code)
        if code == TIMESTAMP_ERROR_CODE:
            raise BinanceAuthenticationError(code=code, status_code=response.status_code)
        if submit and code in SAFE_REJECTION_CODES:
            category = "exchange_rejection" if code == -2010 else "invalid_order"
            raise BinanceOrderRejectedError(code=code, category=category, safe_message=message)
        raise BinanceResponseError(
            f"Binance private response was not safely classified; code={code} status={response.status_code}"
        )

    @staticmethod
    def _raise_rate_limit(response: httpx.Response) -> None:
        raise BinanceRateLimitError(
            status_code=response.status_code,
            retry_after_seconds=BinanceSpotTestnetAdapter._retry_after(
                response.headers.get("Retry-After")
            ),
        )

    @staticmethod
    def _ambiguous(
        client_order_id: str, category: TransportFailureCategory
    ) -> SubmissionObservation:
        return SubmissionObservation(
            TransportOutcomeKind.MAY_HAVE_BEEN_TRANSMITTED,
            client_order_id,
            failure_category=category,
        )

    def _timestamp(self) -> int:
        try:
            value = self._timestamp_provider()
        except Exception as error:
            raise BinanceConfigurationError("timestamp provider failed") from error
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise BinanceConfigurationError("timestamp provider must return non-negative int")
        return value

    def _resolve_client_identity(self, client_order_id: str) -> OrderIdentity:
        try:
            identity = self._identity_resolver.by_client_order_id(client_order_id)
        except Exception as error:
            raise BinanceConfigurationError("order identity resolver failed") from error
        if identity is None:
            raise BinanceUnsupportedContractError("order identity is unavailable")
        return identity

    def _resolve_exchange_identity(self, exchange_order_id: str) -> OrderIdentity:
        try:
            identity = self._identity_resolver.by_exchange_order_id(exchange_order_id)
        except Exception as error:
            raise BinanceConfigurationError("trade identity resolver failed") from error
        if identity is None:
            raise BinanceUnsupportedContractError("trade order identity is unavailable")
        return identity

    def _ensure_open(self) -> None:
        if self._closed:
            raise BinanceConfigurationError("Binance Testnet adapter is closed")

    @staticmethod
    def _validate_stream(stream_key: MarketStreamKey) -> None:
        if not isinstance(stream_key, MarketStreamKey) or stream_key.symbol != "BTCUSDT":
            raise BinanceConfigurationError("adapter supports BTCUSDT only")

    @staticmethod
    def _json_object(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as error:
            raise BinanceNormalizationError("Binance returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise BinanceNormalizationError("Binance response must be an object")
        return payload

    @staticmethod
    def _json_list(response: httpx.Response) -> list[Any]:
        try:
            payload = response.json()
        except ValueError as error:
            raise BinanceNormalizationError("Binance returned invalid JSON") from error
        if not isinstance(payload, list):
            raise BinanceNormalizationError("Binance response must be a list")
        return payload

    @staticmethod
    def _error_payload(response: httpx.Response) -> tuple[int, str] | None:
        try:
            payload = response.json()
        except ValueError:
            return None
        if not isinstance(payload, dict) or "code" not in payload:
            return None
        code = payload.get("code")
        message = payload.get("msg")
        if isinstance(code, bool) or not isinstance(code, int) or not isinstance(message, str):
            raise BinanceNormalizationError("Binance error payload is malformed")
        return code, message

    @staticmethod
    def _string(payload: dict[str, Any], name: str) -> str:
        value = payload.get(name)
        if not isinstance(value, str) or not value:
            raise BinanceNormalizationError(f"Binance field {name} must be a string")
        return value

    @staticmethod
    def _integer(payload: dict[str, Any], name: str) -> int:
        value = payload.get(name)
        if isinstance(value, bool) or not isinstance(value, int):
            raise BinanceNormalizationError(f"Binance field {name} must be an integer")
        return value

    @classmethod
    def _optional_integer(cls, payload: dict[str, Any], name: str) -> int | None:
        return None if payload.get(name) is None else cls._integer(payload, name)

    @staticmethod
    def _boolean(payload: dict[str, Any], name: str) -> bool:
        value = payload.get(name)
        if not isinstance(value, bool):
            raise BinanceNormalizationError(f"Binance field {name} must be boolean")
        return value

    @staticmethod
    def _decimal(
        payload: dict[str, Any], name: str, *, positive: bool = False, non_negative: bool = False
    ) -> Decimal:
        value = payload.get(name)
        if not isinstance(value, str):
            raise BinanceNormalizationError(f"Binance field {name} must be a Decimal string")
        try:
            result = Decimal(value)
        except InvalidOperation as error:
            raise BinanceNormalizationError(f"Binance field {name} is malformed") from error
        if not result.is_finite() or (positive and result <= 0) or (non_negative and result < 0):
            raise BinanceNormalizationError(f"Binance field {name} is outside its domain")
        return Decimal(0) if result.is_zero() else result

    @staticmethod
    def _enum_side(value: object) -> ExchangeOrderSide:
        try:
            return ExchangeOrderSide(value)
        except (TypeError, ValueError) as error:
            raise BinanceUnsupportedContractError("unknown Binance order side") from error

    @staticmethod
    def _status(value: object) -> ExchangeOrderStatus:
        mapping = {
            "NEW": ExchangeOrderStatus.ACKNOWLEDGED,
            "PARTIALLY_FILLED": ExchangeOrderStatus.PARTIALLY_FILLED,
            "FILLED": ExchangeOrderStatus.FILLED,
            "CANCELED": ExchangeOrderStatus.CANCELED,
            "REJECTED": ExchangeOrderStatus.REJECTED,
            "EXPIRED": ExchangeOrderStatus.EXPIRED,
            "EXPIRED_IN_MATCH": ExchangeOrderStatus.EXPIRED,
        }
        if not isinstance(value, str) or value not in mapping:
            raise BinanceUnsupportedContractError("unknown Binance order status")
        return mapping[value]

    @staticmethod
    def _retry_after(value: str | None) -> int | None:
        if value is None:
            return None
        try:
            parsed = int(value)
        except ValueError:
            return None
        return parsed if parsed >= 0 else None
