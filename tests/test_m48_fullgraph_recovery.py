import json
import socket
from decimal import Decimal
from urllib.parse import parse_qs

import httpx
import pytest

from app.application import build_application
from app.core.config import Settings
from app.exchange_execution import ExchangeOrderStatus, KillSwitchState
from app.market.events import CandleClosed, CandleClosedReason, MarketEventSource
from app.market.models import Candle, CandleSnapshot, MarketStreamKey, ServerTime, TickerPrice
from app.persistence import SQLiteExchangePersistence, SQLitePersistenceConfig

NOW = 1_700_000_600_000
KEY = MarketStreamKey("BTCUSDT", "1m")
API_KEY = "API_KEY_SHOULD_NEVER_LEAK"
SECRET = "SECRET_SHOULD_NEVER_LEAK"


@pytest.fixture(autouse=True)
def forbid_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args, **_kwargs):
        raise AssertionError("full-graph recovery test attempted external network or DNS")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)


class Clock:
    def now_ms(self) -> int:
        return NOW

    def monotonic(self) -> float:
        return 0.0


class Provider:
    def __init__(self) -> None:
        self.close_calls = 0

    def get_server_time(self) -> ServerTime:
        return ServerTime(timestamp_ms=NOW)

    def get_ticker_price(self, symbol: str) -> TickerPrice:
        return TickerPrice(symbol=symbol, price=Decimal(4))

    def get_candles(self, symbol: str, interval: str, limit: int) -> list[Candle]:
        del symbol, interval, limit
        return []

    def close(self) -> None:
        self.close_calls += 1


class Sleeper:
    def wait(self, seconds: float, stop_requested) -> bool:
        del seconds
        return not stop_requested()


class RemoteExchange:
    def __init__(self) -> None:
        self.submissions = 0
        self.client_order_id: str | None = None
        self.quantity: str | None = None
        self.status = "NEW"
        self.not_found = False
        self.queried_client_ids: list[str] = []
        self.trades: list[dict[str, object]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "testnet.binance.vision"
        if request.method == "POST":
            body = parse_qs(request.content.decode())
            self.submissions += 1
            self.client_order_id = body["newClientOrderId"][0]
            self.quantity = body["quantity"][0]
            return httpx.Response(
                200,
                json={
                    "symbol": "BTCUSDT",
                    "orderId": 123,
                    "clientOrderId": self.client_order_id,
                    "transactTime": NOW,
                },
            )
        if request.url.path == "/api/v3/order":
            client_id = request.url.params["origClientOrderId"]
            self.queried_client_ids.append(client_id)
            if self.not_found:
                return httpx.Response(400, json={"code": -2013, "msg": "Order does not exist"})
            return httpx.Response(200, json=self.order_payload())
        if request.url.path == "/api/v3/myTrades":
            return httpx.Response(200, json=self.trades)
        if request.url.path == "/api/v3/openOrders":
            open_order = self.client_order_id is not None and self.status in {
                "NEW",
                "PARTIALLY_FILLED",
            }
            return httpx.Response(200, json=[self.order_payload()] if open_order else [])
        if request.url.path == "/api/v3/account":
            return httpx.Response(
                200,
                json={
                    "balances": [
                        {"asset": "BTC", "free": "0", "locked": "0"},
                        {"asset": "USDT", "free": "100", "locked": "0"},
                    ]
                },
            )
        if request.url.path == "/api/v3/exchangeInfo":
            return httpx.Response(
                200,
                json={
                    "symbols": [
                        {
                            "symbol": "BTCUSDT",
                            "filters": [
                                {
                                    "filterType": "MARKET_LOT_SIZE",
                                    "minQty": "0.00001",
                                    "stepSize": "0.00001",
                                },
                                {
                                    "filterType": "MIN_NOTIONAL",
                                    "minNotional": "5",
                                    "applyToMarket": True,
                                },
                            ],
                        }
                    ]
                },
            )
        raise AssertionError(f"unexpected mocked endpoint {request.method} {request.url.path}")

    def order_payload(self) -> dict[str, object]:
        assert self.client_order_id is not None and self.quantity is not None
        executed = "0"
        quote = "0"
        if self.status == "PARTIALLY_FILLED":
            executed, quote = str(Decimal(self.quantity) / 2), str(Decimal(self.quantity) * 2)
        elif self.status == "FILLED":
            executed, quote = self.quantity, str(Decimal(self.quantity) * 4)
        return {
            "symbol": "BTCUSDT",
            "orderId": 123,
            "clientOrderId": self.client_order_id,
            "price": "0",
            "origQty": self.quantity,
            "executedQty": executed,
            "cummulativeQuoteQty": quote,
            "status": self.status,
            "type": "MARKET",
            "side": "BUY",
            "updateTime": NOW,
        }

    def set_executed(self, status: str) -> None:
        assert self.client_order_id is not None and self.quantity is not None
        self.status = status
        quantity = Decimal(self.quantity) if status == "FILLED" else Decimal(self.quantity) / 2
        quote = quantity * 4
        self.trades = [
            {
                "symbol": "BTCUSDT",
                "id": 9001,
                "orderId": 123,
                "price": "4",
                "qty": str(quantity),
                "quoteQty": str(quote),
                "commission": str(quote / Decimal(1000)),
                "commissionAsset": "USDT",
                "time": NOW,
                "isBuyer": True,
                "isMaker": False,
            }
        ]


def settings(path) -> Settings:
    return Settings(
        _env_file=None,
        trading_mode="testnet",
        testnet_execution_enabled=True,
        persistence_enabled=True,
        sqlite_path=path,
        binance_testnet_api_key=API_KEY,
        binance_testnet_secret_key=SECRET,
        binance_testnet_account_fingerprint="account-fullgraph",
        testnet_initial_quote_balance=Decimal(100),
        risk_max_order_notional=Decimal(1000),
        risk_max_position_notional=Decimal(1000),
        testnet_max_session_submitted_notional=Decimal(1000),
        testnet_max_session_executed_turnover=Decimal(2000),
        ema_fast_window=2,
        ema_slow_window=3,
        long_target_exposure=Decimal("0.4"),
        smoke_run_cycles=1,
    )


def initialize_and_clear(path, remote: RemoteExchange) -> None:
    client = httpx.Client(transport=httpx.MockTransport(remote.handler))
    app = build_application(
        settings(path),
        provider=Provider(),
        clock=Clock(),
        sleeper=Sleeper(),
        testnet_http_client=client,
    )
    assert app.testnet is not None and app.testnet.runtime_availability == "RECOVERY_ONLY"
    app.close()
    client.close()
    store = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=4))
    assert store.list_orders() == ()
    store.set_kill_switch(
        settings(path).testnet_execution_root_id,
        state=KillSwitchState.CLEARED,
        expected_generation=0,
        reason_code="full_graph_test_enable",
        audit_identifier="full-graph-test",
        occurred_at_ms=NOW,
    )
    store.close()


def build_process(path, remote: RemoteExchange, *, persistence=None, handler=None):
    client = httpx.Client(transport=httpx.MockTransport(handler or remote.handler))
    provider = Provider()
    app = build_application(
        settings(path),
        provider=provider,
        clock=Clock(),
        sleeper=Sleeper(),
        exchange_persistence=persistence,
        testnet_http_client=client,
    )
    return app, provider, client


def drive_buy(app) -> None:
    assert app.coordinator is not None
    candles = []
    first = NOW - 5 * 60_000
    for index, close in enumerate((3, 2, 1, 4)):
        value = Decimal(close)
        opened = first + index * 60_000
        candles.append(
            Candle(
                open_time_ms=opened,
                close_time_ms=opened + 59_999,
                open=value,
                high=value + 1,
                low=max(Decimal("0.1"), value - 1),
                close=value,
                volume=Decimal(10),
            )
        )
    app.cache.upsert_candles(KEY, candles, NOW)
    app.coordinator.process_event(
        CandleClosed(
            "fullgraph-buy-event",
            NOW,
            KEY,
            "fullgraph-cycle",
            MarketEventSource.REPLAY,
            CandleSnapshot.from_candle(candles[-1]),
            CandleClosedReason.TIME_CONFIRMED,
        )
    )


def durable_counts(path):
    store = SQLiteExchangePersistence(SQLitePersistenceConfig(path, expected_schema_version=4))
    orders = store.list_orders()
    assert len(orders) == 1
    order = orders[0]
    attempts = store._connection.execute(
        "SELECT count(*) FROM exchange_submission_attempts WHERE durable_command_key=?",
        (order.order.durable_command_key,),
    ).fetchone()[0]
    session = store.load_session(order.session_id)
    result = order, attempts, session
    store.close()
    return result


def test_lost_ack_full_graph_processes_a_b_c_never_resubmit(tmp_path) -> None:
    path = tmp_path / "lost-ack-fullgraph.sqlite3"
    remote = RemoteExchange()
    initialize_and_clear(path, remote)

    def crash_after_remote_ack(boundary: str) -> None:
        if boundary == "status_before_commit":
            raise SystemExit("lost acknowledgement persistence")

    crashing_store = SQLiteExchangePersistence(
        SQLitePersistenceConfig(path, expected_schema_version=4),
        failure_injector=crash_after_remote_ack,
    )
    process_a, provider_a, client_a = build_process(path, remote, persistence=crashing_store)
    assert process_a.testnet is not None and process_a.testnet.submission_ready, (
        process_a.testnet.readiness.reasons,
        [
            json.loads(row[0])["reasons"]
            for row in crashing_store._connection.execute(
                "SELECT result_payload FROM exchange_reconciliation_runs ORDER BY started_at_ms"
            ).fetchall()
        ],
    )
    with pytest.raises(SystemExit, match="lost acknowledgement"):
        drive_buy(process_a)
    order_a, attempts_a, session_a = durable_counts(path)
    assert order_a.order.status is ExchangeOrderStatus.SUBMITTING
    assert attempts_a == session_a.submission_count == remote.submissions == 1
    original_client_id = order_a.order.client_order_id
    process_a.close()
    crashing_store.close()
    client_a.close()
    assert provider_a.close_calls == 1

    for _ in ("B", "C"):
        process, provider, client = build_process(path, remote)
        assert process.testnet is not None
        assert process.testnet.runtime_availability == "RECOVERY_ONLY"
        assert process.testnet.readiness.capability is None
        assert process.testnet.application_service is None
        assert process.testnet.consumer is None
        assert process.coordinator is None
        status = process.status_report()
        assert status.testnet_non_terminal_order_count == 1
        assert not status.testnet_submission_capability_present
        process.close()
        client.close()
        assert provider.close_calls == 1

    order_c, attempts_c, session_c = durable_counts(path)
    assert order_c.order.status is ExchangeOrderStatus.ACKNOWLEDGED
    assert order_c.order.client_order_id == original_client_id
    assert attempts_c == session_c.submission_count == remote.submissions == 1
    assert remote.queried_client_ids == [original_client_id, original_client_id]


def test_ambiguous_five_fresh_not_found_processes_remain_recovery_only(tmp_path) -> None:
    path = tmp_path / "ambiguous-five-process.sqlite3"
    remote = RemoteExchange()
    initialize_and_clear(path, remote)
    remote.not_found = True

    original_handler = remote.handler

    def ambiguous_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            original_handler(request)
            return httpx.Response(503, json={"code": -1000, "msg": "unknown"})
        return original_handler(request)

    process_a, provider_a, client_a = build_process(path, remote, handler=ambiguous_handler)
    drive_buy(process_a)
    process_a.close()
    client_a.close()
    assert provider_a.close_calls == 1
    first, attempts, session = durable_counts(path)
    assert first.order.status is ExchangeOrderStatus.RECONCILIATION_REQUIRED
    original_client_id = first.order.client_order_id
    assert attempts == session.submission_count == remote.submissions == 1

    for _ in range(5):
        process, provider, client = build_process(path, remote)
        assert process.testnet is not None
        assert process.testnet.runtime_availability == "RECOVERY_ONLY"
        assert process.testnet.readiness.capability is None
        assert process.coordinator is None
        process.close()
        client.close()
        assert provider.close_calls == 1

    final, attempts, session = durable_counts(path)
    assert final.order.status is ExchangeOrderStatus.RECONCILIATION_REQUIRED
    assert final.order.client_order_id == original_client_id
    assert attempts == session.submission_count == remote.submissions == 1
    assert remote.queried_client_ids == [original_client_id] * 5
    rendered = repr((final, session))
    assert API_KEY not in rendered and SECRET not in rendered and "signature=" not in rendered


@pytest.mark.parametrize(
    ("remote_status", "expected_local_status", "expected_ready"),
    [
        ("FILLED", ExchangeOrderStatus.FILLED, True),
        ("PARTIALLY_FILLED", ExchangeOrderStatus.PARTIALLY_FILLED, False),
    ],
)
def test_lost_ack_full_graph_settles_authoritative_trade_once_across_restarts(
    tmp_path, remote_status: str, expected_local_status: ExchangeOrderStatus, expected_ready: bool
) -> None:
    path = tmp_path / f"lost-ack-{remote_status.lower()}.sqlite3"
    remote = RemoteExchange()
    initialize_and_clear(path, remote)

    def crash_after_remote_ack(boundary: str) -> None:
        if boundary == "status_before_commit":
            raise GeneratorExit("lost acknowledgement persistence")

    crashing_store = SQLiteExchangePersistence(
        SQLitePersistenceConfig(path, expected_schema_version=4),
        failure_injector=crash_after_remote_ack,
    )
    process_a, _, client_a = build_process(path, remote, persistence=crashing_store)
    with pytest.raises(GeneratorExit, match="lost acknowledgement"):
        drive_buy(process_a)
    original, _, _ = durable_counts(path)
    original_client_id = original.order.client_order_id
    process_a.close()
    crashing_store.close()
    client_a.close()

    remote.set_executed(remote_status)
    snapshots = []
    for process_name in ("B", "C"):
        process, provider, client = build_process(path, remote)
        assert process.testnet is not None
        assert process.testnet.submission_ready is expected_ready
        if not expected_ready:
            assert process.testnet.runtime_availability == "RECOVERY_ONLY"
            assert process.coordinator is None
        snapshots.append(process.testnet.portfolio.snapshot)
        process.close()
        client.close()
        assert provider.close_calls == 1, process_name

    final, attempts, session = durable_counts(path)
    settled_quantity = Decimal(remote.quantity or "0")
    if remote_status == "PARTIALLY_FILLED":
        settled_quantity /= 2
    settled_quote = settled_quantity * 4
    fee = settled_quote / Decimal(1000)
    assert final.order.status is expected_local_status
    assert final.order.client_order_id == original_client_id
    assert attempts == session.submission_count == remote.submissions == 1
    assert session.cumulative_executed_turnover == settled_quote
    assert snapshots[0] == snapshots[1]
    assert snapshots[-1].base_balance == settled_quantity
    assert snapshots[-1].quote_balance == Decimal(100) - settled_quote - fee
    expected_queries = 1 if remote_status == "FILLED" else 2
    assert remote.queried_client_ids == [original_client_id] * expected_queries


def test_ambiguous_result_lost_before_persistence_recovers_submitting_without_retry(
    tmp_path,
) -> None:
    path = tmp_path / "ambiguous-result-lost.sqlite3"
    remote = RemoteExchange()
    initialize_and_clear(path, remote)
    remote.not_found = True
    original_handler = remote.handler

    def ambiguous_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            original_handler(request)
            return httpx.Response(503, json={"code": -1000, "msg": "unknown"})
        return original_handler(request)

    def lose_ambiguity_record(boundary: str) -> None:
        if boundary == "status_before_commit":
            raise KeyboardInterrupt("ambiguity result persistence lost")

    crashing_store = SQLiteExchangePersistence(
        SQLitePersistenceConfig(path, expected_schema_version=4),
        failure_injector=lose_ambiguity_record,
    )
    process_a, _, client_a = build_process(
        path, remote, persistence=crashing_store, handler=ambiguous_handler
    )
    with pytest.raises(KeyboardInterrupt, match="ambiguity result persistence lost"):
        drive_buy(process_a)
    original, attempts, session = durable_counts(path)
    assert original.order.status is ExchangeOrderStatus.SUBMITTING
    assert attempts == session.submission_count == remote.submissions == 1
    client_id = original.order.client_order_id
    process_a.close()
    crashing_store.close()
    client_a.close()

    for _ in ("B", "C"):
        process, provider, client = build_process(path, remote)
        assert process.testnet is not None
        assert process.testnet.runtime_availability == "RECOVERY_ONLY"
        assert process.testnet.readiness.capability is None
        assert process.coordinator is None
        process.close()
        client.close()
        assert provider.close_calls == 1

    final, attempts, session = durable_counts(path)
    assert final.order.status is ExchangeOrderStatus.RECONCILIATION_REQUIRED
    assert final.order.client_order_id == client_id
    assert attempts == session.submission_count == remote.submissions == 1
    assert remote.queried_client_ids == [client_id, client_id]
