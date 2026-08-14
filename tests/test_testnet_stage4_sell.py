from __future__ import annotations

import ast
import socket
import sqlite3
import threading
from contextlib import closing
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

import app.tools.testnet_stage4_sell as operator
from app.exchange_execution import ExchangeOrderStatus
from app.exchange_execution.models import TransportFailureCategory, TransportOutcomeKind
from app.exchange_execution.ports import (
    AccountBalance,
    ExchangeInstrumentRules,
    SubmissionObservation,
)
from app.exchange_execution.stage4_sell import Stage4ManagedSellReviewError
from app.infrastructure.binance import (
    AccountTradeObservation,
    BinanceOrderRejectedError,
    BinanceSpotTestnetAdapter,
    BinanceSpotTestnetConfig,
)
from app.market.models import MarketStreamKey
from app.persistence.exceptions import PersistenceIntegrityError, Stage4ManagedSellAuthorityError
from app.persistence.exchange import SQLiteExchangePersistence
from app.persistence.models import SQLitePersistenceConfig
from app.tools.testnet_stage4_sell import (
    AUTHORIZATION_ENV,
    Stage4SellBlocked,
    Stage4SellGuardTransport,
    Stage4SellOutcome,
    Stage4SellTransportViolation,
    _DurableIdentityResolver,
    _NoKnownIdentity,
    _ProductionReader,
    _publish_evidence,
    observe_snapshot,
    run_stage4_sell,
    validate_startup,
)
from tests.test_stage4_managed_sell_authority import predecessor


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*args, **kwargs):
        raise AssertionError("Stage 4 SELL test attempted real network/DNS")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)


class Reader:
    def __init__(self) -> None:
        self.rules = ExchangeInstrumentRules(
            MarketStreamKey("BTCUSDT", "1m"),
            Decimal("0.00001"),
            Decimal("0.00001"),
            Decimal(5),
            "a" * 64,
            Decimal(1000),
        )
        self.price = Decimal(65000)
        self.balances = (
            AccountBalance("BTC", Decimal("1.00016"), Decimal(0)),
            AccountBalance("USDT", Decimal("10014.890736"), Decimal(0)),
        )
        self.orders = ()
        self.trades = (AccountTradeObservation("123", "ctt1t-rrgeumu4r7y6kn63lowjef3jc4", "BTC"),)

    def read_instrument_rules(self, stream_key):
        return self.rules

    def read_reference_price(self, stream_key):
        return self.price

    def read_balances(self, assets):
        return self.balances

    def read_open_order_observations(self, *, stream_key):
        return self.orders

    def read_account_trade_observations(self, *, stream_key, start_at_ms):
        return self.trades


def environment() -> dict[str, str]:
    return {
        AUTHORIZATION_ENV: "YES",
        "BINANCE_TESTNET_ACCOUNT_FINGERPRINT": "phase3-account",
        "BINANCE_TESTNET_API_KEY": "key",
        "BINANCE_TESTNET_SECRET_KEY": "secret",
    }


def test_authorization_precedes_secret_and_database_access(tmp_path: Path) -> None:
    class Guarded(dict):
        def get(self, key, default=None):
            if key.startswith("BINANCE_"):
                raise AssertionError("secret/account access")
            return super().get(key, default)

    with pytest.raises(Stage4SellBlocked, match="authorization"):
        validate_startup(Guarded(), database=tmp_path / "missing", evidence=tmp_path / "log")


def test_startup_requires_existing_database_and_rejects_artifacts(tmp_path: Path) -> None:
    database = tmp_path / "smoke.sqlite3"
    with pytest.raises(Stage4SellBlocked, match="database"):
        validate_startup(environment(), database=database, evidence=tmp_path / "log")
    database.write_bytes(b"db")
    assert (
        validate_startup(environment(), database=database, evidence=tmp_path / "log")
        == "phase3-account"
    )
    for collision in (
        tmp_path / "smoke.sqlite3-wal",
        tmp_path / "smoke.sqlite3-shm",
        tmp_path / "log",
    ):
        collision.write_text("keep", encoding="utf-8")
        with pytest.raises(Stage4SellBlocked):
            validate_startup(environment(), database=database, evidence=tmp_path / "log")
        collision.unlink()


def test_transport_allowlist_budget_redirect_and_close() -> None:
    posts = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        posts += request.method == "POST"
        return httpx.Response(200, json={})

    guard = Stage4SellGuardTransport(httpx.MockTransport(respond))
    for path in ("exchangeInfo", "ticker/price", "account", "openOrders", "myTrades"):
        guard.handle_request(httpx.Request("GET", f"https://testnet.binance.vision/api/v3/{path}"))
    guard.handle_request(httpx.Request("POST", "https://testnet.binance.vision/api/v3/order"))
    assert guard.post_count == posts == 1
    with pytest.raises(Stage4SellTransportViolation):
        guard.handle_request(httpx.Request("POST", "https://testnet.binance.vision/api/v3/order"))
    for request in (
        httpx.Request("POST", "https://testnet.binance.vision/api/v3/order/test"),
        httpx.Request("DELETE", "https://testnet.binance.vision/api/v3/order"),
        httpx.Request("GET", "https://api.binance.com/api/v3/account"),
        httpx.Request("GET", "https://testnet.binance.vision:444/api/v3/account"),
    ):
        with pytest.raises(Stage4SellTransportViolation):
            Stage4SellGuardTransport(httpx.MockTransport(respond)).handle_request(request)
    redirect = Stage4SellGuardTransport(httpx.MockTransport(lambda request: httpx.Response(302)))
    with pytest.raises(Stage4SellTransportViolation, match="redirect"):
        redirect.handle_request(
            httpx.Request("GET", "https://testnet.binance.vision/api/v3/account")
        )
    guard.close()
    guard.close()
    exhausted = Stage4SellGuardTransport(httpx.MockTransport(respond), max_requests=0)
    with pytest.raises(Stage4SellTransportViolation, match="budget"):
        exhausted.handle_request(
            httpx.Request("GET", "https://testnet.binance.vision/api/v3/account")
        )


def test_snapshot_rejects_managed_open_order_and_detects_changes() -> None:
    reader = Reader()
    first = observe_snapshot(reader, now_ms=1000)
    assert first.digest
    reader.price += 1
    assert observe_snapshot(reader, now_ms=1001).digest != first.digest
    from app.infrastructure.binance import OpenOrderObservation

    reader.orders = (OpenOrderObservation("ctt1t-conflict"),)
    with pytest.raises(Stage4SellBlocked, match="open orders"):
        observe_snapshot(reader, now_ms=1002)
    reader.orders = ()
    reader.price = Decimal("NaN")
    with pytest.raises(Stage4SellBlocked, match="snapshot"):
        observe_snapshot(reader, now_ms=1003)


def test_invalid_account_and_missing_credentials_fail_closed(tmp_path: Path) -> None:
    database = tmp_path / "db.sqlite3"
    database.touch()
    values = environment()
    values["BINANCE_TESTNET_ACCOUNT_FINGERPRINT"] = ""
    with pytest.raises(Stage4SellBlocked, match="fingerprint"):
        validate_startup(values, database=database, evidence=tmp_path / "log")
    values = environment()
    values.pop("BINANCE_TESTNET_API_KEY")
    with pytest.raises(Stage4SellBlocked, match="credentials"):
        validate_startup(values, database=database, evidence=tmp_path / "log")


def test_conflicting_or_missing_managed_exchange_history_blocks_authority(tmp_path: Path) -> None:
    cases = (
        ("missing", ()),
        ("wrong", (AccountTradeObservation("999", "ctt1t-wrongwrongwrongwrongwrongwr", "BTC"),)),
    )
    for name, trades in cases:
        database = tmp_path / f"history-{name}.sqlite3"
        predecessor(database).close()
        reader = Reader()
        reader.trades = trades
        with pytest.raises(Stage4SellBlocked, match="history"):
            run_stage4_sell(
                reader,
                object(),
                Stage4SellGuardTransport(httpx.MockTransport(lambda request: httpx.Response(200))),
                account_fingerprint="phase3-account",
                database_path=database,
                run_id=name,
                now_ms=iter(range(4000, 4100)).__next__,
            )
        with closing(sqlite3.connect(database)) as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM exchange_managed_sell_authorities"
                ).fetchone()[0]
                == 0
            )


def test_rejection_facade_and_identity_resolver_are_narrow(tmp_path: Path) -> None:
    class Rejecting:
        def submit_market_order(self, request):
            raise BinanceOrderRejectedError(code=-1013, category="filter", safe_message="rejected")

    request = type("Request", (), {"client_order_id": "ctt1t-aaaaaaaaaaaaaaaaaaaaaaaaaa"})()
    observed = operator._GatewayFacade(Rejecting()).submit_market_order(request)
    assert observed.transport_outcome is TransportOutcomeKind.DETERMINISTIC_REJECTION
    resolver = _NoKnownIdentity()
    assert resolver.by_client_order_id("x") is None
    assert resolver.by_exchange_order_id("x") is None
    database = tmp_path / "identity.sqlite3"
    predecessor(database).close()
    durable = _DurableIdentityResolver(database)
    identity = durable.by_client_order_id("ctt1t-rrgeumu4r7y6kn63lowjef3jc4")
    assert identity is not None and identity.original_base_quantity == Decimal("0.00016")
    assert durable.by_exchange_order_id("missing") is None
    with pytest.raises(Stage4SellBlocked, match="lookup"):
        durable._find("unsafe_column", "x")


def test_production_reader_rejects_bad_ticker() -> None:
    class Client:
        def get(self, *args, **kwargs):
            return httpx.Response(
                200,
                json={"symbol": "WRONG", "price": "65000"},
                request=httpx.Request("GET", "https://testnet.binance.vision/api/v3/ticker/price"),
            )

    with pytest.raises(Stage4SellBlocked, match="ticker"):
        _ProductionReader(object(), Client()).read_reference_price(MarketStreamKey("BTCUSDT", "1m"))


def test_full_offline_graph_commits_submitting_before_exactly_one_gateway_call(
    tmp_path: Path,
) -> None:
    database = tmp_path / "sell.sqlite3"
    predecessor(database).close()
    guard = Stage4SellGuardTransport(httpx.MockTransport(lambda request: httpx.Response(200)))

    class Gateway:
        calls = 0

        def submit_market_order(self, request):
            self.calls += 1
            assert request.side.value == "SELL"
            assert request.base_quantity == Decimal("0.00016")
            with closing(sqlite3.connect(database)) as connection:
                assert connection.execute(
                    "SELECT status FROM exchange_orders WHERE side='SELL'"
                ).fetchone() == ("SUBMITTING",)
                assert (
                    connection.execute(
                        "SELECT count(*) FROM exchange_submission_attempts"
                    ).fetchone()[0]
                    == 2
                )
                assert (
                    connection.execute("SELECT submission_count FROM exchange_sessions").fetchone()[
                        0
                    ]
                    == 2
                )
                assert connection.execute(
                    "SELECT status FROM exchange_managed_sell_authorities"
                ).fetchone() == ("CONSUMED",)
            return SubmissionObservation(
                TransportOutcomeKind.RESPONSE_RECEIVED,
                request.client_order_id,
                "sell-order",
                ExchangeOrderStatus.ACKNOWLEDGED,
                4010,
            )

    gateway = Gateway()
    ticks = iter(range(4000, 4100))
    result = run_stage4_sell(
        Reader(),
        gateway,
        guard,
        account_fingerprint="phase3-account",
        database_path=database,
        run_id="stage4-offline",
        now_ms=lambda: next(ticks),
    )
    assert result.outcome is Stage4SellOutcome.PASS
    assert result.quantity == Decimal("0.00016")
    assert result.session_after == 2
    assert gateway.calls == 1
    rendered = result.render()
    assert "outcome=PASS" in rendered and "side=SELL" in rendered
    assert "secret" not in rendered and "signature" not in rendered
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT count(*) FROM exchange_trades").fetchone()[0] == 1
        payload = connection.execute(
            "SELECT state_payload FROM exchange_portfolio_current"
        ).fetchone()[0]
        assert '"base_balance":{"$decimal":"0.00016"}' in payload


@pytest.mark.parametrize(
    ("observation", "expected_status"),
    [
        (TransportOutcomeKind.DETERMINISTIC_REJECTION, "REJECTED"),
        (TransportOutcomeKind.MAY_HAVE_BEEN_TRANSMITTED, "RECONCILIATION_REQUIRED"),
    ],
)
def test_rejection_and_ambiguity_are_consumed_once_without_economics(
    tmp_path: Path, observation: TransportOutcomeKind, expected_status: str
) -> None:
    database = tmp_path / f"{expected_status}.sqlite3"
    predecessor(database).close()

    class Gateway:
        calls = 0

        def submit_market_order(self, request):
            self.calls += 1
            return SubmissionObservation(
                observation,
                request.client_order_id,
                failure_category=(
                    TransportFailureCategory.TIMEOUT
                    if observation is TransportOutcomeKind.MAY_HAVE_BEEN_TRANSMITTED
                    else None
                ),
            )

    gateway = Gateway()
    result = run_stage4_sell(
        Reader(),
        gateway,
        Stage4SellGuardTransport(httpx.MockTransport(lambda request: httpx.Response(200))),
        account_fingerprint="phase3-account",
        database_path=database,
        run_id=expected_status,
        now_ms=iter(range(4000, 4100)).__next__,
    )
    assert result.outcome is Stage4SellOutcome.BLOCKED and gateway.calls == 1
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(
            "SELECT status FROM exchange_orders WHERE side='SELL'"
        ).fetchone() == (expected_status,)
        assert (
            connection.execute("SELECT count(*) FROM exchange_submission_attempts").fetchone()[0]
            == 2
        )
        assert connection.execute("SELECT count(*) FROM exchange_trades").fetchone()[0] == 1
        state = connection.execute(
            "SELECT portfolio_version,state_payload FROM exchange_portfolio_current"
        ).fetchone()
        assert state[0] == 1 and '"base_balance":{"$decimal":"0.00016"}' in state[1]
    with pytest.raises(
        (Stage4SellBlocked, Stage4ManagedSellAuthorityError, PersistenceIntegrityError)
    ):
        run_stage4_sell(
            Reader(),
            gateway,
            Stage4SellGuardTransport(httpx.MockTransport(lambda request: httpx.Response(200))),
            account_fingerprint="phase3-account",
            database_path=database,
            run_id="rerun",
            now_ms=iter(range(5000, 5100)).__next__,
        )
    assert gateway.calls == 1


def test_mock_transport_exact_sell_contract_and_request_budget(tmp_path: Path) -> None:
    database = tmp_path / "adapter-sell.sqlite3"
    predecessor(database).close()

    def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v3/exchangeInfo":
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
                                    "maxQty": "1000",
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
        if path == "/api/v3/ticker/price":
            return httpx.Response(200, json={"symbol": "BTCUSDT", "price": "65000"})
        if path == "/api/v3/account":
            return httpx.Response(
                200,
                json={
                    "balances": [
                        {"asset": "BTC", "free": "1.00016", "locked": "0"},
                        {"asset": "USDT", "free": "10014.890736", "locked": "0"},
                    ]
                },
            )
        if path == "/api/v3/openOrders":
            return httpx.Response(200, json=[])
        if path == "/api/v3/myTrades":
            return httpx.Response(
                200,
                json=[
                    {
                        "symbol": "BTCUSDT",
                        "id": 1,
                        "orderId": 123,
                        "commission": "0",
                        "commissionAsset": "BTC",
                    }
                ],
            )
        assert path == "/api/v3/order" and request.method == "POST"
        body = request.content.decode()
        assert "symbol=BTCUSDT" in body and "side=SELL" in body and "type=MARKET" in body
        assert "quantity=0.00016" in body and "quoteOrderQty" not in body
        assert "newOrderRespType=ACK" in body and "newClientOrderId=ctt1t-" in body
        client_id = next(
            part.split("=", 1)[1]
            for part in body.split("&")
            if part.startswith("newClientOrderId=")
        )
        return httpx.Response(
            200,
            json={
                "symbol": "BTCUSDT",
                "orderId": 456,
                "clientOrderId": client_id,
                "transactTime": 4010,
            },
        )

    guard = Stage4SellGuardTransport(httpx.MockTransport(respond))
    client = httpx.Client(base_url="https://testnet.binance.vision", transport=guard)
    adapter = BinanceSpotTestnetAdapter(
        BinanceSpotTestnetConfig("key", "secret", "phase3-account"),
        client=client,
        timestamp_provider=lambda: 4000,
        identity_resolver=_DurableIdentityResolver(database),
    )
    result = run_stage4_sell(
        _ProductionReader(adapter, client),
        adapter,
        guard,
        account_fingerprint="phase3-account",
        database_path=database,
        run_id="adapter-offline",
        now_ms=iter(range(4000, 4100)).__next__,
    )
    assert result.outcome is Stage4SellOutcome.PASS
    assert guard.request_count == 11 and guard.post_count == 1
    assert all(
        count == (1 if endpoint.startswith("POST") else 2)
        for endpoint, count in guard.endpoint_counts.items()
    )
    adapter.close()
    client.close()


@pytest.mark.parametrize("mutation", ["price", "rules", "balances", "trades"])
def test_two_snapshot_change_consumes_authority_but_never_calls_gateway(
    tmp_path: Path, mutation: str
) -> None:
    database = tmp_path / f"changed-{mutation}.sqlite3"
    predecessor(database).close()

    class ChangingReader(Reader):
        calls = 0

        def read_instrument_rules(self, stream_key):
            self.calls += 1
            if mutation == "rules" and self.calls == 2:
                return ExchangeInstrumentRules(
                    stream_key,
                    Decimal("0.00001"),
                    Decimal("0.00001"),
                    Decimal(5),
                    "b" * 64,
                    Decimal(1000),
                )
            return super().read_instrument_rules(stream_key)

        def read_reference_price(self, stream_key):
            if mutation == "price" and self.calls == 2:
                return self.price + 1
            return super().read_reference_price(stream_key)

        def read_balances(self, assets):
            if mutation == "balances" and self.calls == 2:
                return (AccountBalance("BTC", Decimal("1.00017"), Decimal(0)), self.balances[1])
            return super().read_balances(assets)

        def read_account_trade_observations(self, *, stream_key, start_at_ms):
            if mutation == "trades" and self.calls == 2:
                return self.trades + (AccountTradeObservation("other", "external", "USDT"),)
            return super().read_account_trade_observations(
                stream_key=stream_key, start_at_ms=start_at_ms
            )

    class Gateway:
        calls = 0

        def submit_market_order(self, request):
            self.calls += 1
            raise AssertionError("must not submit")

    gateway = Gateway()
    with pytest.raises(Stage4SellBlocked, match="snapshot"):
        run_stage4_sell(
            ChangingReader(),
            gateway,
            Stage4SellGuardTransport(httpx.MockTransport(lambda request: httpx.Response(200))),
            account_fingerprint="phase3-account",
            database_path=database,
            run_id="changed",
            now_ms=iter(range(4000, 4100)).__next__,
        )
    assert gateway.calls == 0
    with closing(sqlite3.connect(database)) as connection:
        assert (
            connection.execute("SELECT count(*) FROM exchange_orders WHERE side='SELL'").fetchone()[
                0
            ]
            == 1
        )
        assert (
            connection.execute("SELECT count(*) FROM exchange_submission_attempts").fetchone()[0]
            == 1
        )
        recovery = connection.execute(
            "SELECT event_type,block_reason_code,snapshot_stage FROM "
            "exchange_managed_sell_recovery_events"
        ).fetchone()
        assert recovery == (
            "SELL_RECOVERY_REQUIRED",
            {
                "price": "SNAPSHOT_PRICE_CHANGED",
                "rules": "SNAPSHOT_RULE_CHANGED",
                "balances": "SNAPSHOT_BALANCE_CHANGED",
                "trades": "SNAPSHOT_HISTORY_CHANGED",
            }[mutation],
            "SNAPSHOT_B",
        )


def test_exact_quantity_not_tradable_blocks_without_authority(tmp_path: Path) -> None:
    database = tmp_path / "not-tradable.sqlite3"
    predecessor(database).close()
    reader = Reader()
    reader.rules = ExchangeInstrumentRules(
        MarketStreamKey("BTCUSDT", "1m"),
        Decimal("0.00001"),
        Decimal("0.00001"),
        Decimal(5),
        "c" * 64,
        Decimal("0.00015"),
    )
    with pytest.raises((Stage4SellBlocked, Stage4ManagedSellReviewError)):
        run_stage4_sell(
            reader,
            object(),
            Stage4SellGuardTransport(httpx.MockTransport(lambda request: httpx.Response(200))),
            account_fingerprint="phase3-account",
            database_path=database,
            run_id="coarse",
            now_ms=iter(range(4000, 4100)).__next__,
        )
    with closing(sqlite3.connect(database)) as connection:
        assert (
            connection.execute("SELECT count(*) FROM exchange_managed_sell_authorities").fetchone()[
                0
            ]
            == 0
        )


def test_durable_lineage_and_exchange_coverage_fail_before_authority(tmp_path: Path) -> None:
    database = tmp_path / "lineage.sqlite3"
    predecessor(database).close()
    guard = Stage4SellGuardTransport(httpx.MockTransport(lambda request: httpx.Response(200)))
    with pytest.raises(Stage4SellBlocked, match="predecessor"):
        run_stage4_sell(
            Reader(),
            object(),
            guard,
            account_fingerprint="wrong-account",
            database_path=database,
            run_id="wrong",
            now_ms=iter(range(4000, 4100)).__next__,
        )
    reader = Reader()
    reader.balances = (
        AccountBalance("BTC", Decimal("0.00015"), Decimal(0)),
        reader.balances[1],
    )
    with pytest.raises(Stage4SellBlocked, match="covered"):
        run_stage4_sell(
            reader,
            object(),
            guard,
            account_fingerprint="phase3-account",
            database_path=database,
            run_id="coverage",
            now_ms=iter(range(4000, 4100)).__next__,
        )


def test_evidence_is_exclusive_and_contains_no_private_material(tmp_path: Path) -> None:
    path = tmp_path / "evidence.log"
    _publish_evidence(path, "run_id=one\noutcome=PASS\n")
    with pytest.raises(FileExistsError):
        _publish_evidence(path, "secret=bad\noutcome=PASS\n")
    assert path.read_text(encoding="utf-8") == "run_id=one\noutcome=PASS\n"
    with pytest.raises(operator.Stage4SellError, match="last"):
        _publish_evidence(tmp_path / "bad.log", "outcome=PASS\ntrailing=unsafe\n")


def test_main_blocks_without_authorization(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(operator, "DATABASE_PATH", Path("missing-stage4-sell.sqlite3"))
    monkeypatch.setattr(operator, "EVIDENCE_PATH", Path("missing-stage4-sell.log"))
    monkeypatch.delenv(AUTHORIZATION_ENV, raising=False)
    assert operator.main() == 2


def test_evidence_collision_race_publishes_exactly_one_complete_pass(tmp_path: Path) -> None:
    path = tmp_path / "race.log"
    outcomes: list[str] = []

    def actor(value: str) -> None:
        try:
            _publish_evidence(path, f"run_id={value}\noutcome=PASS\n")
            outcomes.append("published")
        except FileExistsError:
            outcomes.append("collision")

    threads = [threading.Thread(target=actor, args=(str(index),)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["collision", "published"]
    payload = path.read_text(encoding="utf-8")
    assert payload.count("outcome=PASS") == 1 and payload.endswith("outcome=PASS\n")
    assert not tuple(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt, SystemExit, GeneratorExit])
def test_crash_after_submitting_commit_never_calls_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: type[BaseException]
) -> None:
    database = tmp_path / f"after-commit-{failure.__name__}.sqlite3"
    predecessor(database).close()
    real_store = SQLiteExchangePersistence

    def store(config):
        def inject(boundary: str) -> None:
            if boundary == "attempt_after_commit":
                raise failure("injected after durable SUBMITTING")

        return real_store(config, failure_injector=inject)

    monkeypatch.setattr(operator, "SQLiteExchangePersistence", store)

    class Gateway:
        calls = 0

        def submit_market_order(self, request):
            self.calls += 1
            raise AssertionError("transport must not be reached")

    gateway = Gateway()
    with pytest.raises(failure):
        run_stage4_sell(
            Reader(),
            gateway,
            Stage4SellGuardTransport(httpx.MockTransport(lambda request: httpx.Response(200))),
            account_fingerprint="phase3-account",
            database_path=database,
            run_id="crash",
            now_ms=iter(range(4000, 4100)).__next__,
        )
    assert gateway.calls == 0
    reopened = real_store(SQLitePersistenceConfig(database, expected_schema_version=8))
    sell = next(value for value in reopened.list_orders() if value.order.side.value == "SELL")
    assert sell.order.status is ExchangeOrderStatus.SUBMITTING
    assert reopened.load_session(sell.session_id).submission_count == 2
    assert reopened.recover_local().reconciliation_required == (sell,)
    reopened.close()


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt, SystemExit, GeneratorExit])
def test_during_gateway_baseexception_is_at_most_one_post_and_recovery_only(
    tmp_path: Path, failure: type[BaseException]
) -> None:
    database = tmp_path / f"gateway-{failure.__name__}.sqlite3"
    predecessor(database).close()

    class Gateway:
        calls = 0

        def submit_market_order(self, request):
            self.calls += 1
            raise failure("gateway boundary")

    gateway = Gateway()
    with pytest.raises(failure):
        run_stage4_sell(
            Reader(),
            gateway,
            Stage4SellGuardTransport(httpx.MockTransport(lambda request: httpx.Response(200))),
            account_fingerprint="phase3-account",
            database_path=database,
            run_id="gateway-crash",
            now_ms=iter(range(4000, 4100)).__next__,
        )
    assert gateway.calls == 1
    reopened = SQLiteExchangePersistence(
        SQLitePersistenceConfig(database, expected_schema_version=8)
    )
    sell = next(value for value in reopened.list_orders() if value.order.side.value == "SELL")
    assert sell.order.status is ExchangeOrderStatus.SUBMITTING
    assert reopened.recover_local().reconciliation_required == (sell,)
    reopened.close()


def test_main_composes_once_publishes_and_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []

    class Guard:
        def __init__(self, transport):
            events.append("guard")

    class Client:
        def __init__(self, **kwargs):
            events.append("client")

        def close(self):
            events.append("client-close")

    class Adapter:
        def __init__(self, config, **kwargs):
            events.append("adapter")

        def close(self):
            events.append("adapter-close")

    result = operator.Stage4SellResult(
        Stage4SellOutcome.PASS,
        "run",
        "phase3-account",
        "authority",
        "a" * 64,
        "ctt1t-aaaaaaaaaaaaaaaaaaaaaaaaaa",
        Decimal("0.00016"),
        Decimal(65000),
        Decimal("10.4"),
        "b" * 64,
        1,
        1,
        2,
        "ACKNOWLEDGED",
        False,
        11,
        1,
        {"POST /api/v3/order": 1},
    )
    monkeypatch.setattr(operator, "validate_startup", lambda *args, **kwargs: "phase3-account")
    monkeypatch.setattr(operator.httpx, "HTTPTransport", lambda **kwargs: object())
    monkeypatch.setattr(operator.httpx, "Client", Client)
    monkeypatch.setattr(operator, "Stage4SellGuardTransport", Guard)
    monkeypatch.setattr(operator, "BinanceSpotTestnetAdapter", Adapter)
    monkeypatch.setattr(operator, "run_stage4_sell", lambda *args, **kwargs: result)
    monkeypatch.setattr(
        operator, "_publish_evidence", lambda path, payload: events.append("publish")
    )
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "key")
    monkeypatch.setenv("BINANCE_TESTNET_SECRET_KEY", "secret")
    assert operator.main() == 0
    assert events == ["guard", "client", "adapter", "adapter-close", "client-close", "publish"]


@pytest.mark.parametrize("boundary", ["client", "adapter"])
def test_main_closes_construction_resources_on_failure(
    monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    events: list[str] = []

    class Guard:
        def __init__(self, transport):
            pass

        def close(self):
            events.append("guard-close")

    class Client:
        def __init__(self, **kwargs):
            if boundary == "client":
                raise RuntimeError("client construction")

        def close(self):
            events.append("client-close")

    class Adapter:
        def __init__(self, *args, **kwargs):
            if boundary == "adapter":
                raise RuntimeError("adapter construction")

    monkeypatch.setattr(operator, "validate_startup", lambda *args, **kwargs: "phase3-account")
    monkeypatch.setattr(operator.httpx, "HTTPTransport", lambda **kwargs: object())
    monkeypatch.setattr(operator.httpx, "Client", Client)
    monkeypatch.setattr(operator, "Stage4SellGuardTransport", Guard)
    monkeypatch.setattr(operator, "BinanceSpotTestnetAdapter", Adapter)
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "key")
    monkeypatch.setenv("BINANCE_TESTNET_SECRET_KEY", "secret")
    assert operator.main() == 1
    assert events == (["guard-close"] if boundary == "client" else ["client-close"])


def test_main_evidence_failure_occurs_after_close(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []

    class Closable:
        def __init__(self, *args, **kwargs):
            pass

        def close(self):
            events.append("close")

    result = operator.Stage4SellResult(
        Stage4SellOutcome.PASS,
        "run",
        "phase3-account",
        "authority",
        "a" * 64,
        "ctt1t-aaaaaaaaaaaaaaaaaaaaaaaaaa",
        Decimal("0.00016"),
        Decimal(65000),
        Decimal("10.4"),
        "b" * 64,
        1,
        1,
        2,
        "ACKNOWLEDGED",
        False,
        11,
        1,
        {},
    )
    monkeypatch.setattr(operator, "validate_startup", lambda *args, **kwargs: "phase3-account")
    monkeypatch.setattr(operator.httpx, "HTTPTransport", lambda **kwargs: object())
    monkeypatch.setattr(operator.httpx, "Client", Closable)
    monkeypatch.setattr(operator, "Stage4SellGuardTransport", Closable)
    monkeypatch.setattr(operator, "BinanceSpotTestnetAdapter", Closable)
    monkeypatch.setattr(operator, "run_stage4_sell", lambda *args, **kwargs: result)
    monkeypatch.setattr(
        operator, "_publish_evidence", lambda *args: (_ for _ in ()).throw(OSError("disk"))
    )
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "key")
    monkeypatch.setenv("BINANCE_TESTNET_SECRET_KEY", "secret")
    assert operator.main() == 1
    assert events == ["close", "close"]


@pytest.mark.parametrize(
    ("failure", "expected"), [(Stage4SellBlocked("blocked"), 2), (RuntimeError("failed"), 1)]
)
def test_main_sanitizes_runtime_failures(
    monkeypatch: pytest.MonkeyPatch, failure: Exception, expected: int
) -> None:
    class Closable:
        def __init__(self, *args, **kwargs):
            pass

        def close(self):
            pass

    monkeypatch.setattr(operator, "validate_startup", lambda *args, **kwargs: "phase3-account")
    monkeypatch.setattr(operator.httpx, "HTTPTransport", lambda **kwargs: object())
    monkeypatch.setattr(operator.httpx, "Client", Closable)
    monkeypatch.setattr(operator, "Stage4SellGuardTransport", Closable)
    monkeypatch.setattr(operator, "BinanceSpotTestnetAdapter", Closable)
    monkeypatch.setattr(
        operator, "run_stage4_sell", lambda *args, **kwargs: (_ for _ in ()).throw(failure)
    )
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "key")
    monkeypatch.setenv("BINANCE_TESTNET_SECRET_KEY", "secret")
    assert operator.main() == expected


def test_operator_architecture_has_no_continuous_or_live_imports() -> None:
    path = Path("app/tools/testnet_stage4_sell.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not imports & {
        "app.main",
        "app.exchange_runtime.consumer",
        "app.application",
        "app.strategies",
    }
