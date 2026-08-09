from dataclasses import FrozenInstanceError, replace
from decimal import Decimal, localcontext

import pytest

from app.execution.models import Fill, FillSide, InvalidFillError
from app.market.models import MarketStreamKey
from app.portfolio.exceptions import (
    DuplicateFillConflictError,
    FillArithmeticMismatchError,
    FillQuantityExceedsPositionError,
    FillRuleViolationError,
    InsufficientQuoteBalanceError,
    InvalidInstrumentRulesError,
    InvalidPortfolioMarkPriceError,
    InvalidPortfolioTimestampError,
    PortfolioAssetMismatchError,
    PortfolioConfigurationError,
    PortfolioInvariantError,
    PortfolioRestoreError,
    PortfolioStreamMismatchError,
    PortfolioTransitionConflictError,
    PositionRequiredError,
)
from app.portfolio.models import (
    InstrumentRules,
    PortfolioConfig,
    Position,
    precise_product,
    precise_subtract,
)
from app.portfolio.portfolio import Portfolio

KEY = MarketStreamKey("BTCUSDT", "1m")
OTHER_KEY = MarketStreamKey("ETHUSDT", "1m")
D = Decimal


def rules(**overrides: object) -> InstrumentRules:
    values: dict[str, object] = {
        "stream_key": KEY,
        "base_asset": "BTC",
        "quote_asset": "USDT",
        "price_tick": D("0.01"),
        "quantity_step": D("0.00001"),
        "min_quantity": D("0.00001"),
        "min_notional": D("10"),
        "base_asset_precision": 5,
        "quote_asset_precision": 2,
        "rules_version": "btc-usdt/1",
    }
    return InstrumentRules(**{**values, **overrides})  # type: ignore[arg-type]


def config(**overrides: object) -> PortfolioConfig:
    values: dict[str, object] = {
        "portfolio_id": "paper-1",
        "stream_key": KEY,
        "base_asset": "BTC",
        "quote_asset": "USDT",
        "initial_base_balance": D("0"),
        "initial_quote_balance": D("10000"),
        "instrument_rules": rules(),
    }
    return PortfolioConfig(**{**values, **overrides})  # type: ignore[arg-type]


def fill(
    fill_id: str,
    side: FillSide,
    quantity: str,
    price: str,
    fee: str,
    *,
    timestamp: int = 1,
    stream_key: MarketStreamKey = KEY,
    fee_asset: str = "USDT",
    metadata: dict[str, object] | None = None,
) -> Fill:
    quantity_value = D(quantity)
    price_value = D(price)
    fee_value = D(fee)
    gross = rules().round_quote(quantity_value * price_value)
    quote_change = -(gross + fee_value) if side is FillSide.BUY else gross - fee_value
    base_change = quantity_value if side is FillSide.BUY else -quantity_value
    return Fill(
        fill_id=fill_id,
        order_id=f"order-{fill_id}",
        risk_decision_id=f"risk-{fill_id}",
        intent_id=f"intent-{fill_id}",
        stream_key=stream_key,
        side=side,
        quantity=quantity_value,
        price=price_value,
        gross_notional=gross,
        fee=fee_value,
        fee_asset=fee_asset,
        net_quote_change=quote_change,
        net_base_change=base_change,
        filled_at_ms=timestamp,
        correlation_id=f"cycle-{fill_id}",
        metadata={} if metadata is None else metadata,
    )


def assert_identity(snapshot, mark: Decimal) -> None:  # type: ignore[no-untyped-def]
    assert snapshot.current_equity == snapshot.quote_balance + snapshot.base_balance * mark
    if snapshot.position is None:
        assert snapshot.base_balance == 0
        assert snapshot.unrealised_pnl == 0
    else:
        assert snapshot.position.quantity == snapshot.base_balance
        assert snapshot.position.cost_basis == (
            snapshot.position.quantity * snapshot.position.average_entry_price
        )
        assert snapshot.position.unrealised_pnl == snapshot.unrealised_pnl
    assert snapshot.realised_pnl + snapshot.unrealised_pnl == (
        snapshot.realised_pnl + snapshot.unrealised_pnl
    )


def test_instrument_rules_defaults_rounding_and_immutability() -> None:
    value = rules()
    assert value.round_price_down(D("100.019")) == D("100.01")
    assert value.round_quantity_down(D("0.123456")) == D("0.12345")
    assert value.round_quote(D("10.129")) == D("10.12")
    assert value.round_price_down(D("100.01")) == D("100.01")
    with pytest.raises(FrozenInstanceError):
        value.price_tick = D("1")  # type: ignore[misc]


def test_preview_restore_and_committed_transition_are_explicit_and_atomic() -> None:
    account = Portfolio(config())
    before = account.snapshot
    valuation = account.preview_mark_to_market(D("100"), 1, "cycle-1")
    assert account.snapshot == before
    assert valuation.before == before
    assert valuation.after.version == 1
    assert account.apply_committed_transition(valuation) == valuation.after
    assert account.apply_committed_transition(valuation) == valuation.after

    restored = Portfolio.restore(config(), account.snapshot)
    assert restored.snapshot == account.snapshot
    conflicting = Portfolio(config()).preview_mark_to_market(D("101"), 2)
    with pytest.raises(PortfolioTransitionConflictError):
        restored.apply_committed_transition(conflicting)
    assert restored.snapshot == account.snapshot


def test_committed_transition_batch_is_atomic_on_late_conflict() -> None:
    account = Portfolio(config())
    before = account.snapshot
    valuation = account.preview_mark_to_market(D("100"), 1, "cycle-1")
    execution = fill("batch-fill", FillSide.BUY, "1", "100", "0.10")
    applied = account.preview_fill(execution, D("100"), base_state=valuation.after)
    other_valuation = Portfolio(config()).preview_mark_to_market(D("101"), 1, "other-cycle")
    conflicting = account.preview_fill(execution, D("100"), base_state=other_valuation.after)

    with pytest.raises(PortfolioTransitionConflictError):
        account.apply_committed_transitions((valuation, conflicting))

    assert account.snapshot == before
    assert account.apply_committed_transitions((valuation, applied)) == applied.after
    assert account.apply_committed_transitions((valuation, applied)) == applied.after


def test_restore_rejects_config_identity_and_initial_equity_mismatch() -> None:
    snapshot = Portfolio(config()).snapshot
    with pytest.raises(PortfolioRestoreError):
        Portfolio.restore(config(portfolio_id="other"), snapshot)
    with pytest.raises(PortfolioRestoreError):
        Portfolio.restore(config(initial_quote_balance=D("9999")), snapshot)


def test_restore_and_preview_reject_invalid_external_state() -> None:
    account = Portfolio(config())
    snapshot = account.snapshot
    with pytest.raises(PortfolioRestoreError):
        Portfolio.restore(object(), snapshot)  # type: ignore[arg-type]
    with pytest.raises(PortfolioRestoreError):
        Portfolio.restore(config(), object())  # type: ignore[arg-type]
    with pytest.raises(PortfolioRestoreError):
        Portfolio.restore(config(), snapshot, recent_fills=[object()])  # type: ignore[arg-type]
    sample = fill("restore", FillSide.BUY, "0.1", "100", "0.01")
    with pytest.raises(PortfolioRestoreError, match="duplicate"):
        Portfolio.restore(config(), snapshot, recent_fills=(sample, sample))
    wrong_stream = replace(sample, stream_key=OTHER_KEY)
    with pytest.raises(PortfolioRestoreError, match="stream"):
        Portfolio.restore(config(), snapshot, recent_fills=(wrong_stream,))

    with pytest.raises(InvalidPortfolioTimestampError):
        account.preview_mark_to_market(D("100"), True)  # type: ignore[arg-type]
    with pytest.raises(PortfolioInvariantError, match="correlation"):
        account.preview_mark_to_market(D("100"), 1, " ")
    with pytest.raises(PortfolioInvariantError, match="base_state"):
        account.preview_mark_to_market(D("100"), 1, base_state=object())  # type: ignore[arg-type]
    wrong_identity = replace(snapshot, portfolio_id="other")
    with pytest.raises(PortfolioInvariantError, match="does not match"):
        account.preview_mark_to_market(D("100"), 1, base_state=wrong_identity)
    with pytest.raises(PortfolioInvariantError, match="Fill"):
        account.preview_fill(object(), D("100"))  # type: ignore[arg-type]


def test_preview_fill_registry_and_transition_conflicts_are_guarded() -> None:
    small = Portfolio(config(applied_fill_id_capacity=1))
    first = fill("first", FillSide.BUY, "0.1", "100", "0.01", timestamp=1)
    preview = small.preview_fill(first, D("100"))
    assert small.snapshot.version == 0
    small.apply_committed_transition(preview)
    second = fill("second", FillSide.BUY, "0.1", "100", "0.01", timestamp=2)
    small.apply_committed_transition(small.preview_fill(second, D("100")))
    assert small.snapshot.applied_fill_count == 2
    with pytest.raises(PortfolioTransitionConflictError):
        small.apply_committed_transition(object())  # type: ignore[arg-type]

    current = small.snapshot
    stale = replace(first, filled_at_ms=0)
    with pytest.raises(InvalidPortfolioTimestampError):
        small.preview_fill(stale, D("100"), base_state=current)


@pytest.mark.parametrize(
    "overrides",
    [
        {"price_tick": D("0")},
        {"price_tick": D("-1")},
        {"quantity_step": D("0")},
        {"min_quantity": D("0")},
        {"min_notional": D("-1")},
        {"base_asset": ""},
        {"quote_asset": "BTC"},
        {"base_asset_precision": -1},
        {"quote_asset_precision": True},
        {"price_tick": D("NaN")},
        {"price_tick": 0.01},
    ],
)
def test_instrument_rules_reject_invalid_values(overrides: dict[str, object]) -> None:
    with pytest.raises(InvalidInstrumentRulesError):
        rules(**overrides)


def test_fill_is_immutable_and_deeply_detaches_metadata() -> None:
    metadata: dict[str, object] = {"path": ["paper", 1]}
    value = fill("a", FillSide.BUY, "1", "100.00", "0.10", metadata=metadata)
    metadata["path"] = ["mutated"]
    assert value.metadata["path"] == ("paper", 1)
    with pytest.raises(FrozenInstanceError):
        value.fee = D("1")  # type: ignore[misc]


@pytest.mark.parametrize(
    "change",
    [
        {"quantity": D("0")},
        {"price": D("NaN")},
        {"fee": D("-1")},
        {"filled_at_ms": -1},
        {"filled_at_ms": True},
        {"fee": 0.1},
        {"fill_id": ""},
    ],
)
def test_fill_rejects_invalid_structure(change: dict[str, object]) -> None:
    value = fill("a", FillSide.BUY, "1", "100.00", "0.10")
    with pytest.raises(InvalidFillError):
        replace(value, **change)


def test_initial_portfolio_snapshot() -> None:
    portfolio = Portfolio(config())
    snapshot = portfolio.snapshot
    assert snapshot.version == 0
    assert snapshot.quote_balance == D("10000")
    assert snapshot.base_balance == 0
    assert snapshot.position is None
    assert snapshot.current_equity == snapshot.initial_equity == snapshot.peak_equity == D("10000")
    assert snapshot.realised_pnl == snapshot.unrealised_pnl == snapshot.total_fees == 0
    assert snapshot.trade_count == snapshot.applied_fill_count == 0
    with pytest.raises(FrozenInstanceError):
        snapshot.version = 1  # type: ignore[misc]


def test_snapshot_metadata_is_detached_from_input_mapping() -> None:
    metadata: dict[str, object] = {"labels": ["paper"]}
    snapshot = replace(Portfolio(config()).snapshot, metadata=metadata)
    metadata["labels"] = ["mutated"]
    assert snapshot.metadata["labels"] == ("paper",)


@pytest.mark.parametrize(
    "overrides",
    [
        {"initial_quote_balance": D("-1")},
        {"initial_base_balance": D("0.1")},
        {"initial_quote_balance": 10000.0},
        {"applied_fill_id_capacity": 0},
        {"applied_fill_id_capacity": True},
        {"instrument_rules": rules(stream_key=OTHER_KEY)},
    ],
)
def test_portfolio_config_rejects_invalid_values(overrides: dict[str, object]) -> None:
    with pytest.raises(PortfolioConfigurationError):
        config(**overrides)


def test_first_and_second_buy_accounting_with_fee_in_cost_basis() -> None:
    portfolio = Portfolio(config())
    first = portfolio.apply_fill(fill("a", FillSide.BUY, "1", "100.00", "0.10"), D("110"))
    assert first.quote_balance == D("9899.90")
    assert first.base_balance == 1
    assert first.position is not None
    assert first.position.cost_basis == D("100.10")
    assert first.position.average_entry_price == D("100.10")
    assert first.unrealised_pnl == D("9.90")

    second = portfolio.apply_fill(
        fill("b", FillSide.BUY, "1", "120.00", "0.12", timestamp=2), D("120")
    )
    assert second.position is not None
    assert second.position.cost_basis == D("220.22")
    assert second.position.average_entry_price == D("110.11")
    assert second.quote_balance == D("9779.78")
    assert second.total_fees == D("0.22")
    assert second.turnover == D("220.00")
    assert second.trade_count == second.applied_fill_count == second.version == 2
    assert_identity(second, D("120"))


def test_same_price_buys_and_mark_below_average() -> None:
    portfolio = Portfolio(config())
    portfolio.apply_fill(fill("a", FillSide.BUY, "1", "100.00", "0.10"), D("100"))
    result = portfolio.apply_fill(
        fill("b", FillSide.BUY, "1", "100.00", "0.10", timestamp=2), D("90")
    )
    assert result.position is not None
    assert result.position.average_entry_price == D("100.10")
    assert result.unrealised_pnl == D("-20.20")


@pytest.mark.parametrize(
    ("mutator", "error"),
    [
        (lambda value: replace(value, stream_key=OTHER_KEY), PortfolioStreamMismatchError),
        (lambda value: replace(value, fee_asset="BTC"), PortfolioAssetMismatchError),
        (lambda value: replace(value, quantity=D("0.100001")), FillRuleViolationError),
        (lambda value: replace(value, price=D("100.001")), FillRuleViolationError),
        (
            lambda value: replace(value, quantity=D("0.00001"), gross_notional=D("0.01")),
            FillRuleViolationError,
        ),
        (lambda value: replace(value, gross_notional=D("101.00")), FillArithmeticMismatchError),
    ],
)
def test_buy_validation_rolls_back(mutator, error: type[Exception]) -> None:  # type: ignore[no-untyped-def]
    portfolio = Portfolio(config())
    before = portfolio.snapshot
    with pytest.raises(error):
        portfolio.apply_fill(mutator(fill("a", FillSide.BUY, "1", "100.00", "0.10")), D("100"))
    assert portfolio.snapshot == before


def test_insufficient_quote_and_timestamp_regression_roll_back() -> None:
    portfolio = Portfolio(config(initial_quote_balance=D("50")))
    before = portfolio.snapshot
    with pytest.raises(InsufficientQuoteBalanceError):
        portfolio.apply_fill(fill("a", FillSide.BUY, "1", "100.00", "0.10"), D("100"))
    assert portfolio.snapshot == before


def test_fill_below_configured_minimum_quantity_is_rejected() -> None:
    strict_rules = rules(min_quantity=D("0.1"))
    portfolio = Portfolio(config(instrument_rules=strict_rules))
    with pytest.raises(FillRuleViolationError, match="min_quantity"):
        portfolio.apply_fill(fill("a", FillSide.BUY, "0.01", "1000.00", "0.01"), D("1000"))

    portfolio = Portfolio(config())
    portfolio.mark_to_market(D("100"), 10)
    before = portfolio.snapshot
    with pytest.raises(InvalidPortfolioTimestampError):
        portfolio.apply_fill(fill("a", FillSide.BUY, "1", "100.00", "0.10"), D("100"))
    assert portfolio.snapshot == before


def test_partial_profitable_sell_keeps_average_and_includes_fee() -> None:
    portfolio = Portfolio(config())
    portfolio.apply_fill(fill("buy", FillSide.BUY, "2", "100.00", "0.20"), D("100"))
    result = portfolio.apply_fill(
        fill("sell", FillSide.SELL, "1", "120.00", "0.12", timestamp=2), D("120")
    )
    assert result.position is not None
    assert result.position.quantity == 1
    assert result.position.average_entry_price == D("100.10")
    assert result.position.cost_basis == D("100.10")
    assert result.realised_pnl == D("19.78")
    assert result.quote_balance == D("9919.68")
    assert result.base_balance == 1
    assert result.total_fees == D("0.32")
    assert_identity(result, D("120"))


def test_partial_losing_and_full_profitable_exit() -> None:
    portfolio = Portfolio(config())
    portfolio.apply_fill(fill("buy", FillSide.BUY, "2", "100.00", "0.20"), D("100"))
    loss = portfolio.apply_fill(
        fill("loss", FillSide.SELL, "1", "90.00", "0.09", timestamp=2), D("90")
    )
    assert loss.realised_pnl == D("-10.19")
    result = portfolio.apply_fill(
        fill("exit", FillSide.SELL, "1", "130.00", "0.13", timestamp=3), D("130")
    )
    assert result.position is None
    assert result.base_balance == 0
    assert result.unrealised_pnl == 0
    assert result.realised_pnl == D("19.58")
    assert result.current_equity == result.quote_balance == D("10019.58")


def test_full_losing_exit_removes_position() -> None:
    portfolio = Portfolio(config())
    portfolio.apply_fill(fill("buy", FillSide.BUY, "1", "100.00", "0.10"), D("100"))
    result = portfolio.apply_fill(
        fill("sell", FillSide.SELL, "1", "80.00", "0.08", timestamp=2), D("80")
    )
    assert result.position is None
    assert result.realised_pnl == D("-20.18")


def test_sell_requires_position_and_cannot_exceed_it() -> None:
    portfolio = Portfolio(config())
    with pytest.raises(PositionRequiredError):
        portfolio.apply_fill(fill("sell", FillSide.SELL, "1", "100.00", "0.10"), D("100"))
    portfolio.apply_fill(fill("buy", FillSide.BUY, "1", "100.00", "0.10"), D("100"))
    before = portfolio.snapshot
    with pytest.raises(FillQuantityExceedsPositionError):
        portfolio.apply_fill(
            fill("sell", FillSide.SELL, "2", "100.00", "0.20", timestamp=2), D("100")
        )
    assert portfolio.snapshot == before


def test_mark_to_market_peaks_drawdown_recovery_and_no_trade_changes() -> None:
    portfolio = Portfolio(config())
    no_position = portfolio.mark_to_market(D("50"), 1)
    assert no_position.current_equity == D("10000")
    assert no_position.trade_count == 0
    portfolio.apply_fill(fill("buy", FillSide.BUY, "1", "100.00", "0.10", timestamp=2), D("100"))
    high = portfolio.mark_to_market(D("150"), 3)
    assert high.unrealised_pnl == D("49.90")
    assert high.peak_equity == high.current_equity == D("10049.90")
    low = portfolio.mark_to_market(D("80"), 4)
    assert low.unrealised_pnl == D("-20.10")
    assert low.drawdown == D("70")
    recovered = portfolio.mark_to_market(D("120"), 5)
    assert recovered.drawdown == D("30")
    assert recovered.max_drawdown == D("70")
    assert recovered.trade_count == recovered.applied_fill_count == 1
    assert_identity(recovered, D("120"))


def test_mark_validation_timestamp_and_exact_repeat() -> None:
    portfolio = Portfolio(config())
    for invalid in (D("0"), D("-1"), D("NaN"), 100.0):
        with pytest.raises(InvalidPortfolioMarkPriceError):
            portfolio.mark_to_market(invalid, 1)  # type: ignore[arg-type]
    first = portfolio.mark_to_market(D("100"), 1, "cycle-1")
    duplicate = portfolio.mark_to_market(D("100"), 1, "cycle-1")
    assert duplicate == first
    with pytest.raises(InvalidPortfolioTimestampError):
        portfolio.mark_to_market(D("100"), 0)


def test_exact_duplicate_is_noop_and_conflict_rolls_back() -> None:
    portfolio = Portfolio(config())
    value = fill("a", FillSide.BUY, "1", "100.00", "0.10")
    first = portfolio.apply_fill(value, D("100"))
    assert portfolio.apply_fill(value, D("200")) == first
    conflict = replace(value, order_id="different")
    with pytest.raises(DuplicateFillConflictError):
        portfolio.apply_fill(conflict, D("100"))
    assert portfolio.snapshot == first


def test_fill_registry_fifo_eviction_and_mark_survival() -> None:
    portfolio = Portfolio(config(applied_fill_id_capacity=1))
    first = fill("a", FillSide.BUY, "1", "100.00", "0.10")
    portfolio.apply_fill(first, D("100"))
    portfolio.mark_to_market(D("101"), 1)
    duplicate = portfolio.apply_fill(first, D("101"))
    assert duplicate.applied_fill_count == 1
    portfolio.apply_fill(fill("b", FillSide.BUY, "1", "100.00", "0.10"), D("100"))
    replayed = portfolio.apply_fill(first, D("100"))
    assert replayed.applied_fill_count == 3
    assert replayed.base_balance == 3


def test_failed_fill_does_not_enter_registry() -> None:
    portfolio = Portfolio(config())
    value = fill("a", FillSide.BUY, "1", "100.00", "0.10")
    with pytest.raises(FillArithmeticMismatchError):
        portfolio.apply_fill(replace(value, gross_notional=D("101.00")), D("100"))
    assert portfolio.apply_fill(value, D("100")).applied_fill_count == 1


def test_manual_buy_buy_partial_sell_full_sell_accounting_scenario() -> None:
    portfolio = Portfolio(config())

    buy_one = portfolio.apply_fill(
        fill("buy-1", FillSide.BUY, "0.10000", "50000.00", "5.00"), D("50000.00")
    )
    assert buy_one.quote_balance == D("4995.00")
    assert buy_one.base_balance == D("0.10000")
    assert buy_one.position is not None
    assert buy_one.position.cost_basis == D("5005.00")
    assert buy_one.position.average_entry_price == D("50050.00")
    assert buy_one.realised_pnl == D("0")
    assert buy_one.total_fees == D("5.00")
    assert buy_one.turnover == D("5000.00")
    assert buy_one.trade_count == buy_one.version == 1

    buy_two = portfolio.apply_fill(
        fill("buy-2", FillSide.BUY, "0.05000", "40000.00", "2.00", timestamp=2),
        D("40000.00"),
    )
    expected_average = D("46713.33333333333333333333333333333333333333333333333333333333")
    assert buy_two.quote_balance == D("2993.00")
    assert buy_two.base_balance == D("0.15000")
    assert buy_two.position is not None
    assert buy_two.position.cost_basis == D("7007.00")
    assert buy_two.position.average_entry_price == expected_average
    assert precise_subtract(
        buy_two.position.cost_basis,
        precise_product(buy_two.position.quantity, expected_average),
    ) == D("5E-58")

    partial = portfolio.apply_fill(
        fill("sell-1", FillSide.SELL, "0.05000", "60000.00", "3.00", timestamp=3),
        D("60000.00"),
    )
    expected_removed_cost = D("2335.6666666666666666666666666666666666666666666666666666666665000")
    expected_realised = D("661.3333333333333333333333333333333333333333333333333333333335000")
    expected_remaining_cost = D(
        "4671.3333333333333333333333333333333333333333333333333333333335000"
    )
    assert partial.quote_balance == D("5990.00")
    assert partial.base_balance == D("0.10000")
    assert partial.position is not None
    assert partial.position.average_entry_price == expected_average
    assert partial.position.cost_basis == expected_remaining_cost
    with localcontext() as context:
        context.prec = 100
        assert D("3000.00") - D("3.00") - expected_removed_cost == expected_realised
    assert partial.realised_pnl == expected_realised
    assert partial.total_fees == D("10.00")
    assert partial.turnover == D("10000.00")
    assert partial.trade_count == 3
    assert partial.current_equity - partial.initial_equity == D("1990.00000")
    assert partial.realised_pnl + partial.unrealised_pnl == D("1990.00000")

    closed = portfolio.apply_fill(
        fill("sell-2", FillSide.SELL, "0.10000", "30000.00", "3.00", timestamp=4),
        D("30000.00"),
    )
    assert closed.position is None
    assert closed.base_balance == D("0")
    assert closed.quote_balance == D("8987.00")
    assert closed.realised_pnl == D("-1013.00")
    assert closed.unrealised_pnl == D("0")
    assert closed.current_equity == closed.quote_balance
    assert closed.current_equity - closed.initial_equity == closed.realised_pnl
    assert closed.total_fees == D("13.00")
    assert closed.turnover == D("13000.00")
    assert closed.trade_count == closed.applied_fill_count == closed.version == 4


def test_instrument_rounding_edges_are_global_context_independent() -> None:
    value = rules()
    cases = (
        ("quantity", D("0.000009"), D("0.00000")),
        ("quantity", D("1.23456789123456789"), D("1.23456")),
        (
            "quantity",
            D("10000000000000000000000000000000000000000.000019"),
            D("10000000000000000000000000000000000000000.00001"),
        ),
        ("price", D("1E+5") + D("0.019"), D("100000.01")),
    )
    for operation, source, expected in cases:
        with localcontext() as context:
            context.prec = 6
            actual = (
                value.round_quantity_down(source)
                if operation == "quantity"
                else value.round_price_down(source)
            )
        assert actual == expected
    assert value.round_quantity_down(D("-0")) == D("0")
    assert value.round_quote(D("1.239E+2")) == D("123.90")
    with pytest.raises(InvalidInstrumentRulesError):
        value.round_quote(D("Infinity"))
    with pytest.raises(InvalidInstrumentRulesError):
        value.round_quantity_down(1.0)  # type: ignore[arg-type]


def test_fill_corruption_signs_negative_zero_and_exact_arithmetic() -> None:
    buy = fill("buy", FillSide.BUY, "1", "100.00", "0.10")
    portfolio = Portfolio(config())
    for corrupted in (
        replace(buy, gross_notional=D("100.01")),
        replace(buy, net_quote_change=D("-100.09")),
        replace(buy, net_base_change=D("1.00001")),
    ):
        before = portfolio.snapshot
        with pytest.raises(FillArithmeticMismatchError):
            portfolio.apply_fill(corrupted, D("100"))
        assert portfolio.snapshot == before
    zero_fee = replace(buy, fee=D("-0"), net_quote_change=D("-100.00"))
    assert Portfolio(config()).apply_fill(zero_fee, D("100")).total_fees == D("0")
    with pytest.raises(InvalidFillError):
        replace(buy, net_quote_change=D("100.10"))
    sell = fill("sell", FillSide.SELL, "1", "100.00", "0.10")
    with pytest.raises(InvalidFillError):
        replace(sell, fee=D("101.00"), net_quote_change=D("-1.00"))
    with pytest.raises(InvalidFillError):
        replace(sell, net_quote_change=D("-99.90"))


def test_equal_fill_timestamp_allowed_and_old_exact_duplicate_is_noop() -> None:
    portfolio = Portfolio(config())
    first_fill = fill("a", FillSide.BUY, "1", "100.00", "0.10", timestamp=5)
    portfolio.apply_fill(first_fill, D("100"))
    second = portfolio.apply_fill(
        fill("b", FillSide.BUY, "1", "100.00", "0.10", timestamp=5), D("100")
    )
    portfolio.mark_to_market(D("110"), 10, "mark-cycle")
    before_duplicate = portfolio.snapshot
    duplicate = portfolio.apply_fill(first_fill, D("1"))
    assert duplicate == before_duplicate
    assert second.as_of_ms == 5
    with pytest.raises(DuplicateFillConflictError):
        portfolio.apply_fill(replace(first_fill, filled_at_ms=11), D("100"))
    assert portfolio.snapshot == before_duplicate


def test_dust_remainder_is_preserved_but_new_fills_still_obey_rules() -> None:
    strict = rules(min_quantity=D("0.05000"))
    portfolio = Portfolio(config(instrument_rules=strict))
    portfolio.apply_fill(fill("buy", FillSide.BUY, "0.10000", "1000.00", "0.10"), D("1000"))
    dust = portfolio.apply_fill(
        fill("sell", FillSide.SELL, "0.09000", "1000.00", "0.09", timestamp=2), D("1000")
    )
    assert dust.position is not None
    assert dust.position.quantity == dust.base_balance == D("0.01000")
    with pytest.raises(FillRuleViolationError, match="min_quantity"):
        portfolio.apply_fill(
            fill("dust-sell", FillSide.SELL, "0.01000", "1000.00", "0.01", timestamp=3),
            D("1000"),
        )


def test_snapshot_cross_field_invariants_reject_contradictions() -> None:
    portfolio = Portfolio(config())
    valid = portfolio.apply_fill(fill("buy", FillSide.BUY, "1", "100.00", "0.10"), D("100"))
    assert valid.position is not None
    contradictions = (
        {"base_balance": D("2")},
        {"unrealised_pnl": D("1")},
        {"current_equity": D("1")},
        {"drawdown": D("1")},
        {"applied_fill_count": 2},
        {"position": replace(valid.position, updated_at_ms=2)},
    )
    for changes in contradictions:
        with pytest.raises(PortfolioInvariantError):
            replace(valid, **changes)
    with pytest.raises(PortfolioInvariantError):
        replace(Portfolio(config()).snapshot, base_balance=D("1"))


def test_drawdown_new_peak_and_realised_loss_without_position() -> None:
    portfolio = Portfolio(config())
    portfolio.apply_fill(fill("buy", FillSide.BUY, "1", "100.00", "0.10"), D("100"))
    low = portfolio.mark_to_market(D("50"), 2)
    assert low.drawdown == low.max_drawdown == D("50.10")
    recovery = portfolio.mark_to_market(D("80"), 3)
    assert recovery.drawdown == D("20.10")
    assert recovery.max_drawdown == D("50.10")
    peak = portfolio.mark_to_market(D("200"), 4)
    assert peak.drawdown == D("0")
    assert peak.peak_equity == peak.current_equity
    closed = portfolio.apply_fill(
        fill("sell", FillSide.SELL, "1", "90.00", "0.09", timestamp=5), D("90")
    )
    assert closed.position is None
    assert closed.realised_pnl == D("-10.19")
    assert closed.drawdown == closed.peak_equity - closed.current_equity
    counters = (closed.realised_pnl, closed.total_fees, closed.turnover, closed.trade_count)
    marked = portfolio.mark_to_market(D("500"), 6)
    assert (marked.realised_pnl, marked.total_fees, marked.turnover, marked.trade_count) == counters


def test_model_cross_field_and_type_validation_boundaries() -> None:
    with pytest.raises(PortfolioInvariantError, match="config"):
        Portfolio("bad")  # type: ignore[arg-type]
    portfolio = Portfolio(config())
    with pytest.raises(PortfolioInvariantError, match="fill"):
        portfolio.apply_fill("bad", D("1"))  # type: ignore[arg-type]
    with pytest.raises(InvalidPortfolioTimestampError):
        portfolio.mark_to_market(D("1"), True)  # type: ignore[arg-type]
    with pytest.raises(PortfolioInvariantError, match="correlation_id"):
        portfolio.mark_to_market(D("1"), 1, " ")

    valid_position = Position(
        stream_key=KEY,
        quantity=D("1"),
        average_entry_price=D("100"),
        cost_basis=D("100"),
        realised_pnl=D("0"),
        unrealised_pnl=D("0"),
        last_mark_price=D("100"),
        opened_at_ms=1,
        updated_at_ms=1,
    )
    for changes in (
        {"stream_key": "BTCUSDT"},
        {"quantity": D("0")},
        {"opened_at_ms": True},
        {"opened_at_ms": 2},
    ):
        with pytest.raises(PortfolioInvariantError):
            replace(valid_position, **changes)

    cash = Portfolio(config()).snapshot
    for changes in (
        {"stream_key": "BTCUSDT"},
        {"version": True},
        {"position": "bad"},
        {"current_equity": D("9999"), "peak_equity": D("10000"), "drawdown": D("1")},
        {"current_equity": D("10001")},
        {"max_drawdown": D("-0"), "drawdown": D("1")},
        {"metadata": {"nested": {"mutable": True}}},
    ):
        with pytest.raises(PortfolioInvariantError):
            replace(cash, **changes)


def test_config_and_fill_additional_validation_boundaries() -> None:
    with pytest.raises(InvalidInstrumentRulesError, match="stream_key"):
        rules(stream_key="BTCUSDT")  # type: ignore[arg-type]
    with pytest.raises(PortfolioConfigurationError, match="stream_key"):
        config(stream_key="BTCUSDT")  # type: ignore[arg-type]
    with pytest.raises(PortfolioConfigurationError, match="differ"):
        config(base_asset="USDT")
    with pytest.raises(PortfolioConfigurationError, match="instrument_rules"):
        config(instrument_rules="bad")
    with pytest.raises(PortfolioConfigurationError, match="assets"):
        config(instrument_rules=rules(base_asset="XBT"))

    valid = fill("a", FillSide.BUY, "1", "100.00", "0.10")
    with pytest.raises(InvalidFillError, match="stream_key"):
        replace(valid, stream_key="BTCUSDT")
    with pytest.raises(InvalidFillError, match="side"):
        replace(valid, side="BUY")
    with pytest.raises(InvalidFillError, match="correlation_id"):
        replace(valid, correlation_id=" ")
    with pytest.raises(InvalidFillError, match="metadata"):
        replace(valid, metadata={"nested": {"mutable": True}})


def test_fee_quote_precision_and_error_context() -> None:
    portfolio = Portfolio(config())
    value = fill("context", FillSide.BUY, "1", "100.00", "0.10")
    corrupted = replace(value, fee=D("0.101"), net_quote_change=D("-100.101"))
    before = portfolio.snapshot
    with pytest.raises(FillArithmeticMismatchError, match="fee violates quote precision"):
        portfolio.apply_fill(corrupted, D("100"))
    assert portfolio.snapshot == before

    mismatch = replace(value, gross_notional=D("100.01"))
    with pytest.raises(FillArithmeticMismatchError) as captured:
        portfolio.apply_fill(mismatch, D("100"))
    message = str(captured.value)
    assert "fill_id=context" in message
    assert "stream=BTCUSDT:1m" in message
    assert "expected=100.00" in message
    assert "received=100.01" in message
