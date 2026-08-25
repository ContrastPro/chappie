from decimal import Decimal

from app.market.models import MarketStreamKey
from app.paper.attribution import PaperAttributionQualityState, scorecard
from app.persistence.canonical import canonical_sha256
from app.persistence.models import CycleOutcome, PaperAttributionV1, PersistentFillRecord


def _digest(value: str) -> str:
    return canonical_sha256({"value": value}, algorithm_version="test/v1")


def _record(
    key: str,
    *,
    outcome: CycleOutcome = CycleOutcome.EXECUTED,
    command: str | None = None,
    fill_id: str | None = None,
    config: str = "a",
    at_ms: int = 1,
) -> PaperAttributionV1:
    digest = _digest(key)
    payload = {
        "stable_decision_key": digest,
        "decision_slot_key": _digest(f"slot-{key}"),
        "durable_command_key": command,
        "cycle_outcome": outcome,
        "portfolio_id": "paper",
        "portfolio_version_before": 0,
        "portfolio_version_after": 1,
        "symbol": "BTCUSDT",
        "interval": "1m",
        "decision_time_ms": at_ms,
        "strategy_name": "ema",
        "strategy_version": "1",
        "strategy_config_fingerprint": _digest(config),
        "signal_id": f"signal-{key}",
        "signal_direction": "buy",
        "signal_reason_code": "TEST",
        "pipeline_run_id": f"run-{key}",
        "intent_id": None,
        "risk_decision_id": None,
        "risk_outcome": None,
        "risk_rejection_reasons": (),
        "risk_modification_reasons": (),
        "requested_target_exposure": None,
        "approved_target_exposure": None,
        "execution_id": None,
        "execution_status": None,
        "fill_id": fill_id,
        "observed_paper_fee": None,
        "gross_notional": None,
        "before_state_digest": _digest(f"before-{key}"),
        "after_state_digest": _digest(f"after-{key}"),
    }
    return PaperAttributionV1(
        **payload,
        attribution_digest=canonical_sha256(
            {"identity": PaperAttributionV1.IDENTITY, **payload},
            algorithm_version="paper-attribution/v1",
        ),
    )


def _fill(
    command: str, fill_id: str, side: str, price: str, fee: str, at_ms: int
) -> PersistentFillRecord:
    return PersistentFillRecord(
        fill_id=fill_id,
        durable_command_key=command,
        order_id=f"order-{fill_id}",
        risk_decision_id="risk",
        intent_id="intent",
        stream_key=MarketStreamKey("BTCUSDT", "1m"),
        side=side,
        quantity=Decimal(1),
        price=Decimal(price),
        gross_notional=Decimal(price),
        fee=Decimal(fee),
        fee_asset="USDT",
        net_quote_change=Decimal(0),
        net_base_change=Decimal(1) if side == "BUY" else Decimal(-1),
        filled_at_ms=at_ms,
    )


def test_fifo_scorecard_reconciles_observed_fees_benchmark_and_excess() -> None:
    buy_command, sell_command = _digest("buy"), _digest("sell")
    records = (
        _record("buy", command=buy_command, fill_id="fill-buy", at_ms=1),
        _record("hold", outcome=CycleOutcome.HOLD, at_ms=2),
        _record("sell", command=sell_command, fill_id="fill-sell", at_ms=3),
    )
    cards = scorecard(
        records,
        {
            buy_command: _fill(buy_command, "fill-buy", "BUY", "100", "1", 1),
            sell_command: _fill(sell_command, "fill-sell", "SELL", "110", "1", 3),
        },
    )
    assert len(cards) == 1
    card = cards[0]
    assert (card.decisions, card.hold_count, card.fills, card.closed_lots) == (3, 1, 2, 1)
    assert card.gross_realized_result == Decimal(10)
    assert card.observed_paper_fees == Decimal(2)
    assert card.net_realized_result == Decimal(8)
    assert card.benchmark_result == Decimal(10)
    assert card.excess_result == Decimal(-2)
    assert card.quality_state is PaperAttributionQualityState.COMPLETE


def test_open_position_and_missing_lineage_remain_visible() -> None:
    command = _digest("buy")
    open_card = scorecard(
        (_record("buy", command=command, fill_id="fill", at_ms=1),),
        {
            command: _fill(command, "fill", "BUY", "100", "1", 1),
        },
    )[0]
    missing_card = scorecard((_record("missing", command=command, fill_id="fill", at_ms=1),))[0]
    assert open_card.quality_state is PaperAttributionQualityState.OPEN_POSITION
    assert missing_card.quality_state is PaperAttributionQualityState.MISSING_LINEAGE


def test_hold_only_configuration_does_not_make_open_position_mixed() -> None:
    command = _digest("buy")
    cards = scorecard(
        (
            _record("a", command=command, fill_id="fill", config="a"),
            _record("b", outcome=CycleOutcome.HOLD, config="b"),
        ),
        {command: _fill(command, "fill", "BUY", "100", "1", 1)},
    )
    assert len(cards) == 2
    filled = next(card for card in cards if card.fills)
    assert filled.quality_state is PaperAttributionQualityState.OPEN_POSITION
    assert sum(card.fills for card in cards) == 1


def test_open_configurations_on_same_stream_remain_explicitly_mixed() -> None:
    first, second = _digest("first"), _digest("second")
    cards = scorecard(
        (
            _record("a", command=first, fill_id="first-fill", config="a"),
            _record("b", command=second, fill_id="second-fill", config="b"),
        ),
        {
            first: _fill(first, "first-fill", "BUY", "100", "1", 1),
            second: _fill(second, "second-fill", "BUY", "101", "1", 2),
        },
    )
    assert {card.quality_state for card in cards} == {
        PaperAttributionQualityState.MIXED_CONFIGURATION_POSITION
    }


def test_closed_configuration_is_not_mixed_by_unrelated_configuration() -> None:
    buy, sell = _digest("buy"), _digest("sell")
    cards = scorecard(
        (
            _record("buy", command=buy, fill_id="buy-fill", config="a", at_ms=1),
            _record("sell", command=sell, fill_id="sell-fill", config="a", at_ms=2),
            _record("other", outcome=CycleOutcome.HOLD, config="b", at_ms=3),
        ),
        {
            buy: _fill(buy, "buy-fill", "BUY", "100", "1", 1),
            sell: _fill(sell, "sell-fill", "SELL", "110", "1", 2),
        },
    )
    closed = next(card for card in cards if card.closed_lots)
    assert closed.quality_state is PaperAttributionQualityState.COMPLETE
    assert closed.gross_realized_result == Decimal(10)
    assert closed.net_realized_result == Decimal(8)


def test_scorecard_is_deterministic() -> None:
    record = _record("hold", outcome=CycleOutcome.HOLD)
    assert scorecard((record,)) == scorecard((record,))
