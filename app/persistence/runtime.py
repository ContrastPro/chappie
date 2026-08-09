import hashlib
import logging
from dataclasses import dataclass

from app.execution.models import Fill, PaperExecutionResult
from app.persistence.canonical import canonical_json
from app.persistence.keys import (
    DurableCommandKeyInput,
    StableDecisionKeyInput,
    decision_slot_key,
    durable_command_key,
    fingerprint_strategy_input,
    stable_decision_key,
)
from app.persistence.models import (
    CycleOutcome,
    DurableDecisionState,
    PersistentCommandRecord,
    PersistentDecisionRecord,
    PersistentFillRecord,
    PortfolioState,
    PortfolioTransitionRecord,
    PositionState,
)
from app.persistence.ports import TradingPersistence
from app.portfolio.models import PortfolioSnapshot, PortfolioTransitionPreview, Position
from app.trading.models import MarketSnapshot, RiskDecision
from app.trading.pipeline import DecisionPipelineResult

_LOGGER = logging.getLogger("DurablePaperTrading")


@dataclass(frozen=True, slots=True)
class DurablePaperRuntimeConfig:
    exchange: str
    strategy_config_fingerprint: str
    planner_name: str
    planner_version: str
    planner_config_fingerprint: str
    snapshot_policy_fingerprint: str
    risk_policy_fingerprint: str
    instrument_rules_fingerprint: str
    execution_config_fingerprint: str
    checkpoint_interval_transitions: int = 100


@dataclass(frozen=True, slots=True)
class DurableDecisionIdentity:
    stable_decision_key: str
    decision_slot_key: str


@dataclass(frozen=True, slots=True)
class DurableCycleCommit:
    identity: DurableDecisionIdentity
    durable_command_key: str | None
    last_journal_seq: int
    committed: bool = True


def snapshot_to_state(snapshot: PortfolioSnapshot) -> PortfolioState:
    position = snapshot.position
    position_state = None
    mark_price = None
    if position is not None:
        position_state = PositionState(
            quantity=position.quantity,
            cost_basis=position.cost_basis,
            average_entry_price=position.average_entry_price,
            realised_pnl=position.realised_pnl,
            unrealised_pnl=position.unrealised_pnl,
            last_mark_price=position.last_mark_price,
            opened_at_ms=position.opened_at_ms,
            updated_at_ms=position.updated_at_ms,
        )
        mark_price = position.last_mark_price
    return PortfolioState(
        portfolio_id=snapshot.portfolio_id,
        stream_key=snapshot.stream_key,
        base_asset=snapshot.base_asset,
        quote_asset=snapshot.quote_asset,
        quote_balance=snapshot.quote_balance,
        base_balance=snapshot.base_balance,
        position=position_state,
        realised_pnl=snapshot.realised_pnl,
        unrealised_pnl=snapshot.unrealised_pnl,
        total_fees=snapshot.total_fees,
        initial_equity=snapshot.initial_equity,
        turnover=snapshot.turnover,
        trade_count=snapshot.trade_count,
        applied_fill_count=snapshot.applied_fill_count,
        peak_equity=snapshot.peak_equity,
        current_equity=snapshot.current_equity,
        drawdown=snapshot.drawdown,
        max_drawdown=snapshot.max_drawdown,
        mark_price=mark_price,
        as_of_ms=snapshot.as_of_ms,
        version=snapshot.version,
        correlation_id=snapshot.correlation_id,
    )


def state_to_snapshot(state: PortfolioState) -> PortfolioSnapshot:
    position = None
    if state.position is not None:
        value = state.position
        position = Position(
            stream_key=state.stream_key,
            quantity=value.quantity,
            average_entry_price=value.average_entry_price,
            cost_basis=value.cost_basis,
            realised_pnl=value.realised_pnl,
            unrealised_pnl=value.unrealised_pnl,
            last_mark_price=value.last_mark_price,
            opened_at_ms=value.opened_at_ms,
            updated_at_ms=value.updated_at_ms,
        )
    return PortfolioSnapshot(
        portfolio_id=state.portfolio_id,
        version=state.version,
        stream_key=state.stream_key,
        as_of_ms=state.as_of_ms,
        base_asset=state.base_asset,
        quote_asset=state.quote_asset,
        base_balance=state.base_balance,
        quote_balance=state.quote_balance,
        position=position,
        realised_pnl=state.realised_pnl,
        unrealised_pnl=state.unrealised_pnl,
        total_fees=state.total_fees,
        initial_equity=state.initial_equity,
        current_equity=state.current_equity,
        peak_equity=state.peak_equity,
        drawdown=state.drawdown,
        max_drawdown=state.max_drawdown,
        turnover=state.turnover,
        trade_count=state.trade_count,
        applied_fill_count=state.applied_fill_count,
        correlation_id=state.correlation_id,
    )


class DurablePaperCycleStore:
    """Application service owning durable identity and the paper cycle transaction."""

    def __init__(self, persistence: TradingPersistence, config: DurablePaperRuntimeConfig) -> None:
        if not isinstance(persistence, TradingPersistence):
            raise TypeError("persistence must implement TradingPersistence")
        if not isinstance(config, DurablePaperRuntimeConfig):
            raise TypeError("config must be DurablePaperRuntimeConfig")
        self._persistence = persistence
        self._config = config

    def identity(
        self, decision: DecisionPipelineResult, market: MarketSnapshot
    ) -> DurableDecisionIdentity:
        latest = market.latest_closed_candle
        if latest is None:
            raise ValueError("durable decision requires latest closed candle")
        value = StableDecisionKeyInput(
            exchange=self._config.exchange,
            symbol=market.stream_key.symbol,
            interval=market.stream_key.interval,
            latest_closed_candle=latest,
            strategy_input_fingerprint=fingerprint_strategy_input(market.closed_candles),
            strategy_name=decision.strategy_name,
            strategy_version=decision.strategy_version,
            strategy_config_fingerprint=self._config.strategy_config_fingerprint,
            planner_name=self._config.planner_name,
            planner_version=self._config.planner_version,
            planner_config_fingerprint=self._config.planner_config_fingerprint,
            snapshot_policy_fingerprint=self._config.snapshot_policy_fingerprint,
        )
        return DurableDecisionIdentity(stable_decision_key(value), decision_slot_key(value))

    def find_terminal(self, identity: DurableDecisionIdentity) -> DurableDecisionState | None:
        return self._persistence.load_terminal_decision(identity.stable_decision_key)

    def commit_cycle(
        self,
        *,
        identity: DurableDecisionIdentity,
        decision: DecisionPipelineResult,
        outcome: CycleOutcome,
        transitions: tuple[PortfolioTransitionPreview, ...],
        risk: RiskDecision | None,
        execution: PaperExecutionResult | None,
    ) -> DurableCycleCommit:
        decision_payload = canonical_json(
            {
                "pipeline_outcome": decision.outcome.value,
                "reason_code": decision.reason_code,
                "strategy_name": decision.strategy_name,
                "strategy_version": decision.strategy_version,
                "signal_direction": decision.signal.direction.value,
                "intent_action": decision.order_intent.action.value
                if decision.order_intent
                else None,
                "evaluated_at_ms": decision.evaluated_at_ms,
            }
        )
        decision_record = PersistentDecisionRecord(
            identity.stable_decision_key,
            identity.decision_slot_key,
            decision_payload,
            hashlib.sha256(decision_payload.encode()).hexdigest(),
            decision.evaluated_at_ms,
        )
        command_key = None
        command_record = None
        if risk is not None and risk.approved_order is not None:
            order = risk.approved_order
            command_key = durable_command_key(
                DurableCommandKeyInput(
                    stable_decision_key=identity.stable_decision_key,
                    intent_action=decision.order_intent.action.value,  # type: ignore[union-attr]
                    requested_target_exposure=risk.requested_target_exposure,
                    approved_side=order.side.value,
                    approved_quantity=order.quantity,
                    approved_target_exposure=order.approved_target_exposure,
                    reference_price=order.reference_price,
                    worst_case_price=order.worst_case_price,
                    risk_outcome=risk.outcome.value,
                    modification_reasons=tuple(str(reason) for reason in risk.modification_reasons),
                    risk_policy_fingerprint=self._config.risk_policy_fingerprint,
                    instrument_rules_fingerprint=self._config.instrument_rules_fingerprint,
                    execution_config_fingerprint=self._config.execution_config_fingerprint,
                )
            )
            command_payload = canonical_json(
                {
                    "side": order.side.value,
                    "quantity": order.quantity,
                    "target_exposure": order.approved_target_exposure,
                    "risk_outcome": risk.outcome.value,
                }
            )
            command_record = PersistentCommandRecord(
                command_key,
                identity.stable_decision_key,
                command_payload,
                hashlib.sha256(command_payload.encode()).hexdigest(),
            )
        fill = execution.fill if execution is not None else None
        transaction = self._persistence.begin_cycle()
        last_journal_seq = 0
        try:
            transaction.claim_decision(decision_record)
            if command_record is not None:
                transaction.record_command(command_record)
            if fill is not None:
                transaction.record_fill(self._fill_record(fill, command_key))
            for index, preview in enumerate(transitions):
                after_state = snapshot_to_state(preview.after)
                transition_fill = preview.fill if isinstance(preview.fill, Fill) else None
                record = PortfolioTransitionRecord(
                    portfolio_id=after_state.portfolio_id,
                    transition_type="FILL" if transition_fill is not None else "VALUATION",
                    version_before=preview.before.version,
                    version_after=preview.after.version,
                    before_digest=snapshot_to_state(preview.before).digest,
                    after_state=after_state,
                    domain_timestamp_ms=preview.after.as_of_ms,
                    stable_decision_key=identity.stable_decision_key,
                    durable_command_key=command_key if transition_fill is not None else None,
                    fill_id=transition_fill.fill_id if transition_fill is not None else None,
                )
                last_journal_seq = transaction.record_portfolio_transition(record)
                if index == len(transitions) - 1:
                    transaction.set_portfolio_current(after_state, last_journal_seq)
            transaction.mark_terminal(identity.stable_decision_key, outcome)
        except BaseException:
            transaction.rollback()
            raise
        transaction.commit()
        return DurableCycleCommit(identity, command_key, last_journal_seq)

    def maybe_checkpoint(self, snapshot: PortfolioSnapshot, journal_seq: int) -> bool:
        if (
            journal_seq <= 0
            or snapshot.version == 0
            or snapshot.version % self._config.checkpoint_interval_transitions != 0
        ):
            return False
        try:
            return self._persistence.create_checkpoint(snapshot_to_state(snapshot), journal_seq)
        except Exception:
            _LOGGER.exception("Portfolio checkpoint failed version=%s", snapshot.version)
            return False

    @staticmethod
    def _fill_record(fill: Fill, command_key: str | None) -> PersistentFillRecord:
        if command_key is None:
            raise ValueError("Fill requires durable command key")
        return PersistentFillRecord(
            fill_id=fill.fill_id,
            durable_command_key=command_key,
            order_id=fill.order_id,
            risk_decision_id=fill.risk_decision_id,
            intent_id=fill.intent_id,
            stream_key=fill.stream_key,
            side=fill.side.value,
            quantity=fill.quantity,
            price=fill.price,
            gross_notional=fill.gross_notional,
            fee=fill.fee,
            fee_asset=fill.fee_asset,
            net_quote_change=fill.net_quote_change,
            net_base_change=fill.net_base_change,
            filled_at_ms=fill.filled_at_ms,
            dust_liquidation=fill.metadata.get("dust_liquidation") is True,
            correlation_id=fill.correlation_id,
        )
