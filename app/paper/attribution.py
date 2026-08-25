"""Read-only deterministic Paper Attribution V1 projections.

These projections consume durable attribution and fill evidence only. They never
write portfolio state or replace the authoritative Paper accounting path.
"""

from collections import defaultdict, deque
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum

from app.persistence.models import CycleOutcome, PaperAttributionV1, PersistentFillRecord

ZERO = Decimal(0)


class PaperAttributionQualityState(StrEnum):
    COMPLETE = "COMPLETE"
    MISSING_BENCHMARK = "MISSING_BENCHMARK"
    MISSING_LINEAGE = "MISSING_LINEAGE"
    OPEN_POSITION = "OPEN_POSITION"
    MIXED_CONFIGURATION_POSITION = "MIXED_CONFIGURATION_POSITION"


@dataclass(frozen=True, slots=True)
class PaperAttributionScorecardV1:
    strategy_name: str
    strategy_version: str
    strategy_config_fingerprint: str
    symbol: str
    interval: str
    decisions: int
    hold_count: int
    risk_reject_count: int
    no_fill_count: int
    fills: int
    closed_lots: int
    gross_realized_result: Decimal
    observed_paper_fees: Decimal
    net_realized_result: Decimal
    wins: int
    losses: int
    expectancy: Decimal | None
    profit_factor: Decimal | None
    turnover: Decimal
    benchmark_result: Decimal | None
    excess_result: Decimal | None
    quality_state: PaperAttributionQualityState


@dataclass(slots=True)
class _Lot:
    fill: PersistentFillRecord
    quantity: Decimal
    remaining_fee: Decimal


def scorecard(
    records: tuple[PaperAttributionV1, ...],
    fills_by_command: dict[str, PersistentFillRecord] | None = None,
) -> tuple[PaperAttributionScorecardV1, ...]:
    """Group exact strategy/configuration lineage and analytically FIFO-match fills."""
    fills_by_command = fills_by_command or {}
    groups: dict[tuple[str, str, str, str, str], list[PaperAttributionV1]] = defaultdict(list)
    for record in records:
        groups[_group_key(record)].append(record)
    scored = [
        _score_group(
            sorted(groups[key], key=lambda item: (item.decision_time_ms, item.stable_decision_key)),
            fills_by_command,
        )
        for key in sorted(groups)
    ]
    open_groups_by_stream: dict[tuple[str, str], int] = defaultdict(int)
    for card, has_open_quantity in scored:
        if has_open_quantity:
            open_groups_by_stream[(card.symbol, card.interval)] += 1
    return tuple(
        replace(card, quality_state=PaperAttributionQualityState.MIXED_CONFIGURATION_POSITION)
        if has_open_quantity and open_groups_by_stream[(card.symbol, card.interval)] > 1
        else card
        for card, has_open_quantity in scored
    )


def _group_key(record: PaperAttributionV1) -> tuple[str, str, str, str, str]:
    return (
        record.strategy_name,
        record.strategy_version,
        record.strategy_config_fingerprint,
        record.symbol,
        record.interval,
    )


def _score_group(
    records: list[PaperAttributionV1],
    fills_by_command: dict[str, PersistentFillRecord],
) -> tuple[PaperAttributionScorecardV1, bool]:
    open_lots: deque[_Lot] = deque()
    gross = fees = benchmark = turnover = ZERO
    closed = wins = losses = 0
    missing_lineage = False
    realized: list[Decimal] = []
    for record in records:
        if record.fill_id is None:
            continue
        if record.durable_command_key is None or record.durable_command_key not in fills_by_command:
            missing_lineage = True
            continue
        fill = fills_by_command[record.durable_command_key]
        if fill.fill_id != record.fill_id or (fill.stream_key.symbol, fill.stream_key.interval) != (
            record.symbol,
            record.interval,
        ):
            missing_lineage = True
            continue
        fees += fill.fee
        turnover += fill.gross_notional
        remaining, remaining_fee = fill.quantity, fill.fee
        opposite = "SELL" if fill.side == "BUY" else "BUY"
        while remaining > ZERO and open_lots and open_lots[0].fill.side == opposite:
            opening = open_lots[0]
            quantity = min(remaining, opening.quantity)
            opening_fee = opening.remaining_fee * quantity / opening.quantity
            closing_fee = remaining_fee * quantity / remaining
            lot_gross = (
                (fill.price - opening.fill.price) * quantity
                if opening.fill.side == "BUY"
                else (opening.fill.price - fill.price) * quantity
            )
            lot_benchmark = (fill.price - opening.fill.price) * quantity
            lot_net = lot_gross - opening_fee - closing_fee
            gross += lot_gross
            benchmark += lot_benchmark
            realized.append(lot_net)
            closed += 1
            wins += int(lot_net > ZERO)
            losses += int(lot_net < ZERO)
            opening.quantity -= quantity
            opening.remaining_fee -= opening_fee
            remaining -= quantity
            remaining_fee -= closing_fee
            if opening.quantity == ZERO:
                open_lots.popleft()
        if remaining > ZERO:
            open_lots.append(_Lot(fill, remaining, remaining_fee))
    unrealized_open_fees = sum((lot.remaining_fee for lot in open_lots), ZERO)
    net = gross - (fees - unrealized_open_fees)
    quality = PaperAttributionQualityState.COMPLETE
    if missing_lineage:
        quality = PaperAttributionQualityState.MISSING_LINEAGE
    elif open_lots:
        quality = PaperAttributionQualityState.OPEN_POSITION
    elif closed == 0:
        quality = PaperAttributionQualityState.MISSING_BENCHMARK
    positive = sum((value for value in realized if value > ZERO), ZERO)
    negative = -sum((value for value in realized if value < ZERO), ZERO)
    return PaperAttributionScorecardV1(
        *_group_key(records[0]),
        decisions=len(records),
        hold_count=sum(record.cycle_outcome is CycleOutcome.HOLD for record in records),
        risk_reject_count=sum(
            record.cycle_outcome is CycleOutcome.RISK_REJECTED for record in records
        ),
        no_fill_count=sum(record.cycle_outcome is CycleOutcome.NOT_EXECUTED for record in records),
        fills=sum(record.fill_id is not None for record in records),
        closed_lots=closed,
        gross_realized_result=gross,
        observed_paper_fees=fees,
        net_realized_result=net,
        wins=wins,
        losses=losses,
        expectancy=sum(realized, ZERO) / len(realized) if realized else None,
        profit_factor=positive / negative if negative else None,
        turnover=turnover,
        benchmark_result=benchmark if closed and not missing_lineage else None,
        excess_result=net - benchmark if closed and not missing_lineage else None,
        quality_state=quality,
    ), bool(open_lots)
