import base64
import copy
import hashlib
import json
import pickle
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal, localcontext

import pytest

from app.exchange_execution import (
    DeterministicClientOrderIdFactory,
    ExchangeAccountReader,
    ExchangeCommand,
    ExchangeOrder,
    ExchangeOrderEvent,
    ExchangeOrderEventSource,
    ExchangeOrderGateway,
    ExchangeOrderSide,
    ExchangeOrderStatus,
    ExchangeTrade,
    ExchangeTradeIdentity,
    ExecutionEnvironment,
    ExecutionSession,
    ExecutionSessionState,
    KillSwitch,
    KillSwitchState,
    MarketOrderSubmission,
    PendingExposure,
    SettlementClassification,
    SubmissionCapability,
    SubmissionObservation,
    TransportFailureCategory,
    TransportOutcomeKind,
    aggregate_pending_exposure,
    aggregate_trades,
    assert_submission_gates,
    classify_settlement,
    create_client_order_id,
    status_after_transport,
    transition_order,
)
from app.exchange_execution.exceptions import (
    ExchangeCapabilityError,
    ExchangeExecutionInvariantError,
    ExchangeOrderIdentityError,
    ExchangeOrderStateError,
    ExchangeSubmissionSafetyError,
    ExchangeTradeConflictError,
)
from app.exchange_execution.lifecycle import ALLOWED_TRANSITIONS
from app.exchange_execution.ports import (
    AccountBalance,
    ExchangeInstrumentRules,
    OrderQueryResult,
    QueryResultKind,
)
from app.market.models import MarketStreamKey
from app.persistence.canonical import canonical_json
from app.persistence.exceptions import PersistenceSerializationError

KEY = MarketStreamKey("BTCUSDT", "1m")
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
FACTORY = DeterministicClientOrderIdFactory()


def client_id(command: str = HASH_A, account: str = "account-lineage-1") -> str:
    return FACTORY.create_client_order_id(
        environment=ExecutionEnvironment.TESTNET,
        venue="binance-spot",
        account_fingerprint=account,
        durable_command_key=command,
    )


def order(
    status: ExchangeOrderStatus = ExchangeOrderStatus.PREPARED, **overrides: object
) -> ExchangeOrder:
    values: dict[str, object] = {
        "durable_command_key": HASH_A,
        "client_order_id": client_id(),
        "account_fingerprint": "account-lineage-1",
        "stream_key": KEY,
        "side": ExchangeOrderSide.BUY,
        "original_base_quantity": Decimal(1),
        "status": status,
        "created_at_ms": 100,
    }
    if status is not ExchangeOrderStatus.PREPARED:
        values["submitted_at_ms"] = 101
    if status in {
        ExchangeOrderStatus.ACKNOWLEDGED,
        ExchangeOrderStatus.PARTIALLY_FILLED,
        ExchangeOrderStatus.FILLED,
    }:
        values["exchange_order_id"] = "9001"
        values["acknowledged_at_ms"] = 102
    if status is ExchangeOrderStatus.PARTIALLY_FILLED:
        values["cumulative_executed_base_quantity"] = Decimal("0.4")
        values["cumulative_quote_quantity"] = Decimal(40)
    if status is ExchangeOrderStatus.FILLED:
        values["cumulative_executed_base_quantity"] = Decimal(1)
        values["cumulative_quote_quantity"] = Decimal(100)
    values.update(overrides)
    return ExchangeOrder(**values)  # type: ignore[arg-type]


def trade(
    trade_id: str,
    quantity: str,
    quote: str,
    *,
    commission_asset: str = "USDT",
    commission: str = "0.01",
    **overrides: object,
) -> ExchangeTrade:
    values: dict[str, object] = {
        "identity": ExchangeTradeIdentity(
            venue="binance-spot",
            environment=ExecutionEnvironment.TESTNET,
            account_fingerprint="account-lineage-1",
            symbol="BTCUSDT",
            exchange_order_id="9001",
            exchange_trade_id=trade_id,
        ),
        "client_order_id": client_id(),
        "stream_key": KEY,
        "side": ExchangeOrderSide.BUY,
        "quantity": Decimal(quantity),
        "price": Decimal(100),
        "quote_quantity": Decimal(quote),
        "commission": Decimal(commission),
        "commission_asset": commission_asset,
        "traded_at_ms": 200,
    }
    values.update(overrides)
    return ExchangeTrade(**values)  # type: ignore[arg-type]


def test_client_order_id_matches_independent_fixture_and_is_stable() -> None:
    payload = {
        "account_fingerprint": "account-lineage-1",
        "durable_command_key": HASH_A,
        "environment": "testnet",
        "venue": "binance-spot",
    }
    envelope = {"algorithm_version": "exchange-client-order/v1", "payload": payload}
    raw = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
    expected = (
        "ctt1t-" + base64.b32encode(hashlib.sha256(raw).digest()).decode().rstrip("=").lower()[:26]
    )
    actual = client_id()
    assert actual == "ctt1t-zyk4rpzcorlikbh2w4dsppjlrz"
    assert actual == expected
    assert len(actual) == 32
    assert actual == client_id()
    assert set(actual) <= set("abcdefghijklmnopqrstuvwxyz0123456789-")
    assert client_id(HASH_B) != actual
    assert client_id(account="account-lineage-2") != actual
    assert client_id(account="Account-Lineage-1") != actual


@pytest.mark.parametrize("account", [" leading", "trailing ", "аккаунт", "bad/account"])
def test_client_order_id_rejects_noncanonical_account_fingerprint(account: str) -> None:
    with pytest.raises(ExchangeOrderIdentityError):
        client_id(account=account)


@pytest.mark.parametrize("account", ["", " "])
def test_client_order_id_rejects_empty_account(account: str) -> None:
    with pytest.raises(ExchangeExecutionInvariantError):
        client_id(account=account)


@pytest.mark.parametrize("command", ["a", "A" * 64, "g" * 64])
def test_client_order_id_rejects_invalid_command(command: str) -> None:
    with pytest.raises(ExchangeOrderIdentityError):
        client_id(command)


def test_client_order_id_protocol_wrapper() -> None:
    assert (
        create_client_order_id(
            FACTORY,
            environment=ExecutionEnvironment.TESTNET,
            venue="binance-spot",
            account_fingerprint="account-lineage-1",
            durable_command_key=HASH_A,
        )
        == client_id()
    )
    with pytest.raises(ExchangeOrderIdentityError):
        create_client_order_id(  # type: ignore[arg-type]
            object(),
            environment=ExecutionEnvironment.TESTNET,
            venue="binance-spot",
            account_fingerprint="account",
            durable_command_key=HASH_A,
        )


def test_order_status_invariants() -> None:
    order(ExchangeOrderStatus.PREPARED)
    order(ExchangeOrderStatus.SUBMITTING)
    order(ExchangeOrderStatus.ACKNOWLEDGED)
    order(ExchangeOrderStatus.PARTIALLY_FILLED)
    order(ExchangeOrderStatus.FILLED)
    with pytest.raises(ExchangeOrderStateError):
        order(ExchangeOrderStatus.PREPARED, exchange_order_id="unexpected")
    with pytest.raises(ExchangeOrderStateError):
        order(ExchangeOrderStatus.ACKNOWLEDGED, exchange_order_id=None)
    with pytest.raises(ExchangeOrderStateError):
        order(ExchangeOrderStatus.PARTIALLY_FILLED, cumulative_executed_base_quantity=Decimal(0))
    with pytest.raises(ExchangeOrderStateError):
        order(ExchangeOrderStatus.FILLED, cumulative_executed_base_quantity=Decimal("0.9"))
    with pytest.raises(ExchangeOrderStateError):
        order(ExchangeOrderStatus.REJECTED, cumulative_executed_base_quantity=Decimal("0.1"))


def test_all_approved_lifecycle_paths_and_regressions() -> None:
    prepared = order()
    submitting = transition_order(
        prepared, status=ExchangeOrderStatus.SUBMITTING, submitted_at_ms=101
    )
    acknowledged = transition_order(
        submitting,
        status=ExchangeOrderStatus.ACKNOWLEDGED,
        exchange_order_id="9001",
        acknowledged_at_ms=102,
    )
    partial = transition_order(
        acknowledged,
        status=ExchangeOrderStatus.PARTIALLY_FILLED,
        cumulative_executed_base_quantity=Decimal("0.4"),
        cumulative_quote_quantity=Decimal(40),
    )
    later = transition_order(
        partial,
        status=ExchangeOrderStatus.PARTIALLY_FILLED,
        cumulative_executed_base_quantity=Decimal("0.7"),
        cumulative_quote_quantity=Decimal(70),
    )
    filled = transition_order(
        later,
        status=ExchangeOrderStatus.FILLED,
        cumulative_executed_base_quantity=Decimal(1),
        cumulative_quote_quantity=Decimal(100),
    )
    assert filled.status.is_exchange_terminal
    with pytest.raises(ExchangeOrderStateError):
        transition_order(filled, status=ExchangeOrderStatus.ACKNOWLEDGED)
    with pytest.raises(ExchangeOrderStateError):
        transition_order(
            later,
            status=ExchangeOrderStatus.PARTIALLY_FILLED,
            cumulative_executed_base_quantity=Decimal("0.6"),
        )
    with pytest.raises(ExchangeOrderIdentityError):
        transition_order(
            later,
            status=ExchangeOrderStatus.FILLED,
            exchange_order_id="different",
            cumulative_executed_base_quantity=Decimal(1),
        )


def test_immediate_fill_and_reconciliation_paths() -> None:
    submitting = order(ExchangeOrderStatus.SUBMITTING)
    filled = transition_order(
        submitting,
        status=ExchangeOrderStatus.FILLED,
        exchange_order_id="9001",
        cumulative_executed_base_quantity=Decimal(1),
        cumulative_quote_quantity=Decimal(100),
    )
    assert filled.status is ExchangeOrderStatus.FILLED
    required = transition_order(submitting, status=ExchangeOrderStatus.RECONCILIATION_REQUIRED)
    found = transition_order(
        required,
        status=ExchangeOrderStatus.ACKNOWLEDGED,
        exchange_order_id="9001",
        acknowledged_at_ms=102,
    )
    assert found.status is ExchangeOrderStatus.ACKNOWLEDGED


def test_transport_outcome_does_not_fabricate_exchange_truth() -> None:
    assert status_after_transport(TransportOutcomeKind.DEFINITELY_NOT_TRANSMITTED) is None
    assert (
        status_after_transport(TransportOutcomeKind.MAY_HAVE_BEEN_TRANSMITTED)
        is ExchangeOrderStatus.RECONCILIATION_REQUIRED
    )
    assert (
        status_after_transport(TransportOutcomeKind.DETERMINISTIC_REJECTION)
        is ExchangeOrderStatus.REJECTED
    )
    assert (
        status_after_transport(TransportOutcomeKind.RESPONSE_RECEIVED)
        is ExchangeOrderStatus.ACKNOWLEDGED
    )
    assert TransportOutcomeKind.MAY_HAVE_BEEN_TRANSMITTED.requires_reconciliation


@pytest.mark.parametrize("category", list(TransportFailureCategory))
def test_every_ambiguous_transport_category_requires_reconciliation(
    category: TransportFailureCategory,
) -> None:
    observation = SubmissionObservation(
        transport_outcome=TransportOutcomeKind.MAY_HAVE_BEEN_TRANSMITTED,
        client_order_id=client_id(),
        failure_category=category,
    )
    assert observation.failure_category is category
    assert (
        status_after_transport(observation.transport_outcome)
        is ExchangeOrderStatus.RECONCILIATION_REQUIRED
    )


def test_acknowledgement_with_cumulative_facts_never_creates_a_trade() -> None:
    acknowledged = order(
        ExchangeOrderStatus.ACKNOWLEDGED,
        cumulative_executed_base_quantity=Decimal("0.25"),
        cumulative_quote_quantity=Decimal(25),
    )
    observation = SubmissionObservation(
        TransportOutcomeKind.RESPONSE_RECEIVED,
        acknowledged.client_order_id,
        acknowledged.exchange_order_id,
        ExchangeOrderStatus.ACKNOWLEDGED,
        200,
    )
    assert acknowledged.cumulative_executed_base_quantity == Decimal("0.25")
    assert not hasattr(observation, "trade")
    assert not hasattr(observation, "fill")
    assert (
        classify_settlement(
            acknowledged,
            aggregate_trades(
                replace(
                    acknowledged,
                    status=ExchangeOrderStatus.PARTIALLY_FILLED,
                ),
                (trade("authoritative", "0.25", "25"),),
            ),
            base_asset="BTC",
            quote_asset="USDT",
        )
        is SettlementClassification.INCOMPLETE
    )


def test_trade_aggregation_and_immutable_commissions() -> None:
    partial = order(ExchangeOrderStatus.PARTIALLY_FILLED)
    aggregate = aggregate_trades(
        partial, (trade("1", "0.1", "10"), trade("2", "0.3", "30", commission_asset="BNB"))
    )
    assert aggregate.executed_base_quantity == Decimal("0.4")
    assert aggregate.quote_quantity == Decimal(40)
    assert aggregate.commissions_by_asset == {"USDT": Decimal("0.01"), "BNB": Decimal("0.01")}
    with pytest.raises(TypeError):
        aggregate.commissions_by_asset["BTC"] = Decimal(1)  # type: ignore[index]


def test_exact_three_fill_aggregation_is_order_independent() -> None:
    filled = order(ExchangeOrderStatus.FILLED)
    trades = (
        trade("A", "0.25", "25", commission="0.01"),
        trade("B", "0.35", "35", commission="0.02"),
        trade("C", "0.40", "40", commission_asset="BNB", commission="0.03"),
    )
    forward = aggregate_trades(filled, trades)
    reverse = aggregate_trades(filled, tuple(reversed(trades)))
    assert forward == reverse
    assert forward.executed_base_quantity == Decimal("1.00")
    assert forward.quote_quantity == Decimal(100)
    assert forward.commissions_by_asset == {
        "USDT": Decimal("0.03"),
        "BNB": Decimal("0.03"),
    }


def test_trade_aggregation_rejects_duplicates_conflicts_and_wrong_scope() -> None:
    partial = order(ExchangeOrderStatus.PARTIALLY_FILLED)
    first = trade("1", "0.4", "40")
    duplicate = aggregate_trades(partial, (first, first))
    assert duplicate.trade_count == 1
    assert duplicate.executed_base_quantity == first.quantity
    conflicting = replace(first, commission=Decimal("0.02"))
    with pytest.raises(ExchangeTradeConflictError, match="conflicting"):
        aggregate_trades(partial, (first, conflicting))
    for conflicting in (
        replace(first, quantity=Decimal("0.3")),
        replace(first, price=Decimal(101)),
        replace(first, commission_asset="BNB"),
        replace(first, traded_at_ms=201),
    ):
        with pytest.raises(ExchangeTradeConflictError, match="conflicting"):
            aggregate_trades(partial, (first, conflicting))
    with pytest.raises(ExchangeTradeConflictError, match="client order"):
        aggregate_trades(partial, (replace(first, client_order_id="another"),))
    with pytest.raises(ExchangeExecutionInvariantError, match="stream"):
        replace(first, stream_key=MarketStreamKey("ETHUSDT", "1m"))


@pytest.mark.parametrize(
    ("fee_asset", "expected"),
    [
        ("USDT", SettlementClassification.SETTLEABLE),
        ("BTC", SettlementClassification.UNSUPPORTED_FEE_ASSET),
        ("BNB", SettlementClassification.UNSUPPORTED_FEE_ASSET),
    ],
)
def test_fee_settlement_classification(fee_asset: str, expected: SettlementClassification) -> None:
    partial = order(ExchangeOrderStatus.PARTIALLY_FILLED)
    aggregate = aggregate_trades(partial, (trade("1", "0.4", "40", commission_asset=fee_asset),))
    assert classify_settlement(partial, aggregate, base_asset="BTC", quote_asset="USDT") is expected
    assert (
        classify_settlement(
            order(ExchangeOrderStatus.ACKNOWLEDGED), aggregate, base_asset="BTC", quote_asset="USDT"
        )
        is SettlementClassification.CONFLICT
    )


def test_zero_and_mixed_unsupported_commission_assets_remain_exchange_truth() -> None:
    partial = order(ExchangeOrderStatus.PARTIALLY_FILLED)
    zero_bnb = trade("zero", "0.4", "40", commission_asset="BNB", commission="-0")
    assert zero_bnb.commission == Decimal(0) and not zero_bnb.commission.is_signed()
    zero_aggregate = aggregate_trades(partial, (zero_bnb,))
    assert zero_aggregate.commissions_by_asset == {"BNB": Decimal(0)}
    assert (
        classify_settlement(partial, zero_aggregate, base_asset="BTC", quote_asset="USDT")
        is SettlementClassification.UNSUPPORTED_FEE_ASSET
    )
    mixed = aggregate_trades(
        partial,
        (
            trade("quote", "0.2", "20"),
            trade("third", "0.2", "20", commission_asset="BNB"),
        ),
    )
    assert set(mixed.commissions_by_asset) == {"USDT", "BNB"}
    assert (
        classify_settlement(partial, mixed, base_asset="BTC", quote_asset="USDT")
        is SettlementClassification.UNSUPPORTED_FEE_ASSET
    )


def test_pending_exposure_and_session_validation() -> None:
    buy = PendingExposure(
        reserved_quote_amount=Decimal(110),
        pending_notional=Decimal(100),
        order_count=1,
        inflight_submission_count=1,
    )
    sell = PendingExposure(
        reserved_base_quantity=Decimal(1), pending_notional=Decimal(100), order_count=1
    )
    assert buy.reserved_quote_amount == Decimal(110)
    assert sell.reserved_base_quantity == Decimal(1)
    total = aggregate_pending_exposure((buy,))
    assert total == buy
    with pytest.raises(ExchangeSubmissionSafetyError, match="one order"):
        aggregate_pending_exposure((buy, sell))
    with pytest.raises(ExchangeExecutionInvariantError):
        PendingExposure(reserved_quote_amount=Decimal(-1))
    with pytest.raises(ExchangeExecutionInvariantError):
        PendingExposure(order_count=0, inflight_submission_count=1)
    session = ExecutionSession("session", "account", 100, ExecutionSessionState.ACTIVE)
    assert session.cumulative_submitted_notional == 0


def test_partial_fill_pending_exposure_is_explicit_and_exact() -> None:
    exposure = PendingExposure(
        reserved_quote_amount=Decimal(60),
        pending_notional=Decimal(60),
        original_base_quantity=Decimal(1),
        executed_base_quantity=Decimal("0.4"),
        remaining_base_quantity=Decimal("0.6"),
        order_count=1,
    )
    assert exposure.original_base_quantity == (
        exposure.executed_base_quantity + exposure.remaining_base_quantity
    )
    with pytest.raises(ExchangeExecutionInvariantError, match="executed plus remaining"):
        replace(exposure, remaining_base_quantity=Decimal("0.7"))


def test_kill_switch_and_process_local_capability_gates() -> None:
    active = KillSwitch(KillSwitchState.ACTIVE, 0, "safe_default", 100)
    cleared = KillSwitch(KillSwitchState.CLEARED, 1, "operator_authorized", 101)
    capability = SubmissionCapability._issue(reconciliation_generation=2, control_generation=1)
    gates = {
        "environment": ExecutionEnvironment.TESTNET,
        "kill_switch": cleared,
        "reconciliation_safe": True,
        "capability": capability,
        "expected_reconciliation_generation": 2,
        "expected_control_generation": 1,
        "risk_approved": True,
        "pending_control_safe": True,
    }
    with pytest.raises(ExchangeSubmissionSafetyError, match="kill switch"):
        assert_submission_gates(**{**gates, "kill_switch": active})
    with pytest.raises(ExchangeSubmissionSafetyError, match="reconciliation"):
        assert_submission_gates(**{**gates, "reconciliation_safe": False})
    with pytest.raises(ExchangeSubmissionSafetyError, match="capability"):
        assert_submission_gates(**{**gates, "capability": None})
    assert_submission_gates(**gates)
    with pytest.raises(TypeError):
        pickle.dumps(capability)
    with pytest.raises(TypeError):
        copy.copy(capability)
    with pytest.raises(TypeError):
        copy.deepcopy(capability)
    with pytest.raises(PersistenceSerializationError):
        canonical_json(capability)
    with pytest.raises(ExchangeCapabilityError):
        SubmissionCapability(reconciliation_generation=2, control_generation=1)
    for changes, message in (
        ({"environment": "live"}, "TESTNET"),
        ({"expected_reconciliation_generation": 3}, "stale"),
        ({"expected_control_generation": 2}, "stale"),
        ({"risk_approved": False}, "Risk"),
        ({"pending_control_safe": False}, "pending-order"),
        ({"risk_approved": 1}, "booleans"),
        ({"expected_control_generation": True}, "non-negative integers"),
    ):
        with pytest.raises(ExchangeSubmissionSafetyError, match=message):
            assert_submission_gates(**{**gates, **changes})


class FakeGateway:
    def submit_market_order(self, request: MarketOrderSubmission) -> SubmissionObservation:
        return SubmissionObservation(
            TransportOutcomeKind.RESPONSE_RECEIVED,
            request.client_order_id,
            "9001",
            ExchangeOrderStatus.ACKNOWLEDGED,
            200,
        )


class FakeReader:
    def query_order(self, *, stream_key: MarketStreamKey, client_order_id: str) -> OrderQueryResult:
        return OrderQueryResult(QueryResultKind.NOT_FOUND)

    def list_order_trades(
        self, *, stream_key: MarketStreamKey, exchange_order_id: str
    ) -> tuple[ExchangeTrade, ...]:
        return ()

    def list_open_orders(self, *, stream_key: MarketStreamKey) -> tuple[ExchangeOrder, ...]:
        return ()

    def read_balances(self, assets: tuple[str, ...]) -> tuple[AccountBalance, ...]:
        return tuple(AccountBalance(asset, Decimal(0), Decimal(0)) for asset in assets)

    def read_instrument_rules(self, stream_key: MarketStreamKey) -> ExchangeInstrumentRules:
        return ExchangeInstrumentRules(
            stream_key, Decimal("0.00001"), Decimal("0.00001"), Decimal(5), "v1"
        )


def test_ports_are_runtime_checkable_and_gateway_does_not_create_identity() -> None:
    gateway = FakeGateway()
    reader = FakeReader()
    assert isinstance(gateway, ExchangeOrderGateway)
    assert isinstance(reader, ExchangeAccountReader)
    request = MarketOrderSubmission(
        HASH_A, client_id(), "account-lineage-1", KEY, ExchangeOrderSide.BUY, Decimal("0.1")
    )
    assert gateway.submit_market_order(request).client_order_id == request.client_order_id
    assert (
        reader.query_order(stream_key=KEY, client_order_id=request.client_order_id).kind
        is QueryResultKind.NOT_FOUND
    )


def valid_command() -> ExchangeCommand:
    return ExchangeCommand(
        durable_command_key=HASH_A,
        stable_decision_key=HASH_B,
        decision_slot_key=HASH_C,
        stream_key=KEY,
        side=ExchangeOrderSide.BUY,
        base_quantity=Decimal("0.1"),
        reference_price=Decimal(100),
        approved_target_exposure=Decimal("0.5"),
        approved_notional=Decimal(10),
        risk_authorization_fingerprint=HASH_A,
        instrument_rules_fingerprint=HASH_B,
        execution_configuration_fingerprint=HASH_C,
        authorized_at_ms=100,
        correlation_id="audit-only",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("durable_command_key", "bad"),
        ("stream_key", MarketStreamKey("ETHUSDT", "1m")),
        ("side", "BUY"),
        ("base_quantity", Decimal(0)),
        ("reference_price", Decimal("NaN")),
        ("approved_target_exposure", Decimal("1.1")),
        ("approved_notional", Decimal(-1)),
        ("risk_authorization_fingerprint", "bad"),
        ("authorized_at_ms", True),
        ("correlation_id", " "),
        ("model_version", ""),
    ],
)
def test_exchange_command_rejects_invalid_boundaries(field: str, value: object) -> None:
    with pytest.raises((ExchangeExecutionInvariantError, ExchangeOrderIdentityError, ValueError)):
        replace(valid_command(), **{field: value})


def test_exchange_order_event_is_typed_and_clock_free() -> None:
    event = ExchangeOrderEvent(
        event_id="event-1",
        durable_command_key=HASH_A,
        client_order_id=client_id(),
        previous_status=ExchangeOrderStatus.PREPARED,
        new_status=ExchangeOrderStatus.SUBMITTING,
        source=ExchangeOrderEventSource.TRANSPORT,
        occurred_at_ms=100,
        reason_code="attempt_marked",
    )
    assert event.exchange_timestamp_ms is None
    for field, value in (
        ("event_id", ""),
        ("previous_status", "PREPARED"),
        ("new_status", "SUBMITTING"),
        ("source", "TRANSPORT"),
        ("occurred_at_ms", -1),
        ("reason_code", ""),
    ):
        with pytest.raises((ExchangeExecutionInvariantError, ExchangeOrderStateError)):
            replace(event, **{field: value})


def test_additional_transition_and_transport_boundaries() -> None:
    acknowledged = order(ExchangeOrderStatus.ACKNOWLEDGED)
    for terminal in (ExchangeOrderStatus.CANCELED, ExchangeOrderStatus.EXPIRED):
        assert transition_order(acknowledged, status=terminal).status is terminal
    required = transition_order(order(), status=ExchangeOrderStatus.RECONCILIATION_REQUIRED)
    assert transition_order(
        required, status=ExchangeOrderStatus.REJECTED
    ).status.is_exchange_terminal
    with pytest.raises(ExchangeOrderStateError):
        transition_order(order(), status="SUBMITTING")  # type: ignore[arg-type]
    with pytest.raises(ExchangeOrderStateError):
        status_after_transport("timeout")  # type: ignore[arg-type]


def test_lifecycle_transition_matrix_is_exhaustive() -> None:
    expected = {
        ExchangeOrderStatus.PREPARED: {
            ExchangeOrderStatus.SUBMITTING,
            ExchangeOrderStatus.RECONCILIATION_REQUIRED,
        },
        ExchangeOrderStatus.SUBMITTING: {
            ExchangeOrderStatus.ACKNOWLEDGED,
            ExchangeOrderStatus.PARTIALLY_FILLED,
            ExchangeOrderStatus.FILLED,
            ExchangeOrderStatus.REJECTED,
            ExchangeOrderStatus.EXPIRED,
            ExchangeOrderStatus.RECONCILIATION_REQUIRED,
        },
        ExchangeOrderStatus.ACKNOWLEDGED: {
            ExchangeOrderStatus.PARTIALLY_FILLED,
            ExchangeOrderStatus.FILLED,
            ExchangeOrderStatus.CANCELED,
            ExchangeOrderStatus.EXPIRED,
            ExchangeOrderStatus.RECONCILIATION_REQUIRED,
        },
        ExchangeOrderStatus.PARTIALLY_FILLED: {
            ExchangeOrderStatus.PARTIALLY_FILLED,
            ExchangeOrderStatus.FILLED,
            ExchangeOrderStatus.CANCELED,
            ExchangeOrderStatus.EXPIRED,
            ExchangeOrderStatus.RECONCILIATION_REQUIRED,
        },
        ExchangeOrderStatus.RECONCILIATION_REQUIRED: {
            ExchangeOrderStatus.ACKNOWLEDGED,
            ExchangeOrderStatus.PARTIALLY_FILLED,
            ExchangeOrderStatus.FILLED,
            ExchangeOrderStatus.CANCELED,
            ExchangeOrderStatus.REJECTED,
            ExchangeOrderStatus.EXPIRED,
        },
        ExchangeOrderStatus.FILLED: set(),
        ExchangeOrderStatus.CANCELED: set(),
        ExchangeOrderStatus.REJECTED: set(),
        ExchangeOrderStatus.EXPIRED: set(),
    }
    assert {status: set(targets) for status, targets in ALLOWED_TRANSITIONS.items()} == expected
    for source in ExchangeOrderStatus:
        current = order(source)
        for target in ExchangeOrderStatus:
            if target not in expected[source]:
                with pytest.raises(ExchangeOrderStateError):
                    transition_order(current, status=target)


@pytest.mark.parametrize(
    "target",
    [
        ExchangeOrderStatus.ACKNOWLEDGED,
        ExchangeOrderStatus.PARTIALLY_FILLED,
        ExchangeOrderStatus.FILLED,
        ExchangeOrderStatus.REJECTED,
        ExchangeOrderStatus.EXPIRED,
        ExchangeOrderStatus.CANCELED,
    ],
)
def test_reconciliation_required_can_resolve_to_authoritative_truth(
    target: ExchangeOrderStatus,
) -> None:
    current = order(ExchangeOrderStatus.RECONCILIATION_REQUIRED)
    changes: dict[str, object] = {"status": target}
    if target in {
        ExchangeOrderStatus.ACKNOWLEDGED,
        ExchangeOrderStatus.PARTIALLY_FILLED,
        ExchangeOrderStatus.FILLED,
    }:
        changes["exchange_order_id"] = "9001"
    if target is ExchangeOrderStatus.PARTIALLY_FILLED:
        changes.update(
            cumulative_executed_base_quantity=Decimal("0.4"),
            cumulative_quote_quantity=Decimal(40),
        )
    if target is ExchangeOrderStatus.FILLED:
        changes.update(
            cumulative_executed_base_quantity=Decimal(1),
            cumulative_quote_quantity=Decimal(100),
        )
    assert transition_order(current, **changes).status is target


def test_additional_trade_and_pending_boundaries() -> None:
    partial = order(ExchangeOrderStatus.PARTIALLY_FILLED)
    with pytest.raises(ExchangeTradeConflictError, match="at least one"):
        aggregate_trades(partial, ())
    with pytest.raises(ExchangeTradeConflictError, match="ExchangeTrade"):
        aggregate_trades(partial, (object(),))  # type: ignore[arg-type]
    with pytest.raises(ExchangeTradeConflictError, match="exceeds"):
        aggregate_trades(partial, (trade("large", "1.1", "110"),))
    with pytest.raises(ExchangeOrderStateError):
        aggregate_pending_exposure((object(),))  # type: ignore[arg-type]


def test_session_control_capability_and_port_dto_boundaries() -> None:
    session = ExecutionSession("session", "account", 100, ExecutionSessionState.ACTIVE)
    for field, value in (
        ("session_id", ""),
        ("authorized_at_ms", -1),
        ("state", "ACTIVE"),
        ("cumulative_executed_turnover", Decimal(-1)),
        ("submission_count", -1),
    ):
        with pytest.raises(ExchangeExecutionInvariantError):
            replace(session, **{field: value})
    active = KillSwitch(KillSwitchState.ACTIVE, 0, "safe_default", 100)
    for field, value in (("state", "ACTIVE"), ("generation", -1), ("reason_code", "")):
        with pytest.raises(ExchangeExecutionInvariantError):
            replace(active, **{field: value})
    for kwargs in (
        {"reconciliation_generation": -1, "control_generation": 0},
        {"reconciliation_generation": 0, "control_generation": True},
    ):
        with pytest.raises(ExchangeCapabilityError):
            SubmissionCapability._issue(**kwargs)  # type: ignore[arg-type]

    request = MarketOrderSubmission(
        HASH_A, client_id(), "account-lineage-1", KEY, ExchangeOrderSide.BUY, Decimal("0.1")
    )
    for field, value in (
        ("stream_key", MarketStreamKey("ETHUSDT", "1m")),
        ("side", "BUY"),
        ("base_quantity", Decimal(0)),
        ("base_quantity", 0.1),
        ("client_order_id", "missing"),
        ("account_fingerprint", " bad"),
        ("venue", "live"),
        ("order_type", "LIMIT"),
        ("response_type", "FULL"),
    ):
        with pytest.raises((ValueError, TypeError, ExchangeExecutionInvariantError)):
            replace(request, **{field: value})
    with pytest.raises(ValueError, match="FOUND"):
        OrderQueryResult(QueryResultKind.FOUND)
    with pytest.raises(ValueError, match="only FOUND"):
        OrderQueryResult(QueryResultKind.NOT_FOUND, order())
    with pytest.raises(ExchangeExecutionInvariantError):
        AccountBalance("BTC", Decimal(-1), Decimal(0))
    assert not hasattr(request, "quote_order_quantity")
    assert OrderQueryResult(QueryResultKind.AMBIGUOUS).order is None
    with pytest.raises(ValueError, match="failure category"):
        SubmissionObservation(
            TransportOutcomeKind.MAY_HAVE_BEEN_TRANSMITTED,
            client_id(),
        )
    valid_observation = SubmissionObservation(
        TransportOutcomeKind.RESPONSE_RECEIVED,
        client_id(),
        "9001",
        ExchangeOrderStatus.ACKNOWLEDGED,
        100,
    )
    for field, value in (
        ("transport_outcome", "timeout"),
        ("exchange_status", "ACKNOWLEDGED"),
        ("transact_time_ms", -1),
        ("failure_category", TransportFailureCategory.TIMEOUT),
    ):
        with pytest.raises(ExchangeExecutionInvariantError):
            replace(valid_observation, **{field: value})
    with pytest.raises(ExchangeExecutionInvariantError):
        AccountBalance("BTC", Decimal(0), Decimal(-1))
    rules = ExchangeInstrumentRules(KEY, Decimal("0.00001"), Decimal("0.00001"), Decimal(5), "v1")
    for field, value in (
        ("stream_key", MarketStreamKey("ETHUSDT", "1m")),
        ("quantity_step", Decimal(0)),
        ("rules_version", ""),
    ):
        with pytest.raises(ValueError):
            replace(rules, **{field: value})


def test_factory_wrong_venue_and_failure_wrapping() -> None:
    with pytest.raises(ExchangeOrderIdentityError, match="venue"):
        FACTORY.create_client_order_id(
            environment=ExecutionEnvironment.TESTNET,
            venue="other",
            account_fingerprint="account",
            durable_command_key=HASH_A,
        )

    class BrokenFactory:
        def create_client_order_id(self, **kwargs: object) -> str:
            raise RuntimeError("broken")

    with pytest.raises(ExchangeOrderIdentityError, match="factory failed"):
        create_client_order_id(
            BrokenFactory(),
            environment=ExecutionEnvironment.TESTNET,
            venue="binance-spot",
            account_fingerprint="account",
            durable_command_key=HASH_A,
        )
    with pytest.raises(ExchangeOrderIdentityError, match="TESTNET"):
        FACTORY.create_client_order_id(  # type: ignore[arg-type]
            environment="live",
            venue="binance-spot",
            account_fingerprint="account",
            durable_command_key=HASH_A,
        )
    with pytest.raises(ExchangeOrderIdentityError):
        create_client_order_id(
            FACTORY,
            environment=ExecutionEnvironment.TESTNET,
            venue="other",
            account_fingerprint="account",
            durable_command_key=HASH_A,
        )


def test_transition_identity_quantity_and_trade_scope_conflicts() -> None:
    submitting = order(ExchangeOrderStatus.SUBMITTING)
    with pytest.raises(ExchangeOrderIdentityError, match="durable"):
        transition_order(
            submitting,
            status=ExchangeOrderStatus.RECONCILIATION_REQUIRED,
            durable_command_key=HASH_B,
        )
    with pytest.raises(ExchangeOrderIdentityError, match="client"):
        transition_order(
            submitting,
            status=ExchangeOrderStatus.RECONCILIATION_REQUIRED,
            client_order_id=client_id(HASH_B),
        )
    for changes, message in (
        ({"account_fingerprint": "account-lineage-2"}, "account lineage"),
        ({"side": ExchangeOrderSide.SELL}, "stream/side"),
        ({"original_base_quantity": Decimal(2)}, "original quantity"),
    ):
        with pytest.raises(ExchangeOrderIdentityError, match=message):
            transition_order(
                submitting,
                status=ExchangeOrderStatus.RECONCILIATION_REQUIRED,
                **changes,
            )
    partial = order(ExchangeOrderStatus.PARTIALLY_FILLED)
    with pytest.raises(ExchangeOrderStateError, match="quote"):
        transition_order(
            partial,
            status=ExchangeOrderStatus.PARTIALLY_FILLED,
            cumulative_executed_base_quantity=Decimal("0.5"),
            cumulative_quote_quantity=Decimal(30),
        )
    with pytest.raises(ExchangeTradeConflictError, match="stream or side"):
        aggregate_trades(
            partial, (replace(trade("side", "0.4", "40"), side=ExchangeOrderSide.SELL),)
        )
    with pytest.raises(ExchangeTradeConflictError, match="exchange order"):
        wrong_identity = replace(trade("order", "0.4", "40").identity, exchange_order_id="other")
        aggregate_trades(partial, (replace(trade("order", "0.4", "40"), identity=wrong_identity),))
    aggregate = aggregate_trades(partial, (trade("incomplete", "0.4", "40"),))
    canceled = order(
        ExchangeOrderStatus.CANCELED,
        cumulative_executed_base_quantity=Decimal("0.4"),
    )
    assert (
        classify_settlement(canceled, aggregate, base_asset="BTC", quote_asset="USDT")
        is SettlementClassification.INCOMPLETE
    )


def test_remaining_model_validation_boundaries() -> None:
    for field, value in (
        ("client_order_id", "bad"),
        ("stream_key", "BTCUSDT"),
        ("side", "BUY"),
        ("cumulative_executed_base_quantity", Decimal(2)),
        ("status", "PREPARED"),
        ("venue", "other"),
    ):
        with pytest.raises((ExchangeExecutionInvariantError, ValueError)):
            replace(order(), **{field: value})
    with pytest.raises(ExchangeOrderStateError, match="SUBMITTING"):
        order(
            ExchangeOrderStatus.SUBMITTING,
            cumulative_executed_base_quantity=Decimal("0.1"),
        )
    identity = trade("model", "0.4", "40").identity
    for field, value in (("venue", "other"), ("symbol", "ETHUSDT")):
        with pytest.raises(ValueError):
            replace(identity, **{field: value})
    valid_trade = trade("model", "0.4", "40")
    for field, value in (("identity", object()), ("side", "BUY"), ("is_maker", 1)):
        with pytest.raises(ExchangeExecutionInvariantError):
            replace(valid_trade, **{field: value})
    with pytest.raises(ExchangeExecutionInvariantError, match="trade_count"):
        replace(
            aggregate_trades(order(ExchangeOrderStatus.PARTIALLY_FILLED), (valid_trade,)),
            trade_count=0,
        )
    with pytest.raises(ExchangeExecutionInvariantError, match="order_count"):
        PendingExposure(order_count=2)
    with pytest.raises(ValueError, match="TESTNET"):
        replace(
            ExecutionSession("session", "account", 100, ExecutionSessionState.ACTIVE),
            environment="live",
        )


def test_trade_identity_scope_and_model_immutability() -> None:
    identity = trade("same", "0.4", "40").identity
    assert replace(identity, account_fingerprint="account-lineage-2") != identity
    assert replace(identity, exchange_order_id="9002") != identity
    assert replace(identity, exchange_trade_id="other") != identity
    models = (
        valid_command(),
        order(),
        trade("immutable", "0.4", "40"),
        PendingExposure(),
        ExecutionSession("session", "account", 100, ExecutionSessionState.ACTIVE),
        KillSwitch(KillSwitchState.ACTIVE, 0, "default", 100),
        OrderQueryResult(QueryResultKind.NOT_FOUND),
    )
    for model in models:
        with pytest.raises((FrozenInstanceError, AttributeError)):
            model.model_version = "changed"  # type: ignore[attr-defined]


def test_decimal_context_independence_and_negative_zero_normalization() -> None:
    filled = order(ExchangeOrderStatus.FILLED)
    values = (trade("a", "0.25", "25"), trade("b", "0.75", "75"))
    with localcontext() as context:
        context.prec = 2
        low = aggregate_trades(filled, values)
        exposure = aggregate_pending_exposure(
            (
                PendingExposure(
                    reserved_quote_amount=Decimal("-0"),
                    original_base_quantity=Decimal(1),
                    executed_base_quantity=Decimal("0.4"),
                    remaining_base_quantity=Decimal("0.6"),
                    order_count=1,
                ),
            )
        )
    with localcontext() as context:
        context.prec = 50
        high = aggregate_trades(filled, values)
    assert low == high
    assert exposure.reserved_quote_amount == Decimal(0)
    assert not exposure.reserved_quote_amount.is_signed()
