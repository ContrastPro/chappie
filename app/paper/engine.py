import logging
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import replace
from time import monotonic

from app.execution.models import PaperExecutionResult, PaperExecutionStatus
from app.execution.provider import ExecutionProvider
from app.paper.exceptions import (
    PaperTradingConfigurationError,
    PaperTradingDecisionError,
    PaperTradingError,
    PaperTradingExecutionError,
    PaperTradingInvariantError,
    PaperTradingPortfolioError,
    PaperTradingRiskError,
    PaperTradingStateError,
)
from app.paper.factories import PaperTradingCycleIdFactory, create_cycle_id
from app.paper.models import (
    PaperTradingCycleOutcome,
    PaperTradingCycleResult,
    PaperTradingEngineActivity,
    PaperTradingEngineConfig,
    PaperTradingEngineHealth,
    PaperTradingEngineStatus,
    PaperTradingFailureCategory,
    PaperTradingObservationStatus,
    PaperTradingRecord,
    PaperTradingStage,
)
from app.paper.observer import PaperTradingObserver
from app.portfolio.models import PortfolioSnapshot
from app.portfolio.portfolio import Portfolio
from app.risk.manager import RiskManager
from app.trading.models import MarketSnapshot, RiskDecision, RiskDecisionOutcome
from app.trading.pipeline import DecisionPipelineResult, PipelineOutcome

_LOGGER = logging.getLogger("PaperTrading")


class PaperTradingEngine:
    """Synchronous, single-writer paper trading application service."""

    def __init__(
        self,
        config: PaperTradingEngineConfig,
        portfolio: Portfolio,
        risk_manager: RiskManager,
        execution_provider: ExecutionProvider,
        cycle_id_factory: PaperTradingCycleIdFactory,
        observer: PaperTradingObserver,
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> None:
        if not isinstance(config, PaperTradingEngineConfig):
            raise PaperTradingConfigurationError("config must be PaperTradingEngineConfig")
        if not isinstance(portfolio, Portfolio):
            raise PaperTradingConfigurationError("portfolio must be Portfolio")
        if not isinstance(risk_manager, RiskManager):
            raise PaperTradingConfigurationError("risk_manager must be RiskManager")
        if not isinstance(execution_provider, ExecutionProvider):
            raise PaperTradingConfigurationError("execution_provider must implement protocol")
        if not isinstance(cycle_id_factory, PaperTradingCycleIdFactory):
            raise PaperTradingConfigurationError("cycle_id_factory must implement protocol")
        if not isinstance(observer, PaperTradingObserver):
            raise PaperTradingConfigurationError("observer must implement protocol")
        if not callable(monotonic_clock):
            raise PaperTradingConfigurationError("monotonic_clock must be callable")
        if (
            risk_manager.instrument_rules.stream_key
            != execution_provider.instrument_rules.stream_key
        ):
            raise PaperTradingConfigurationError("risk and execution streams do not match")
        if (
            risk_manager.instrument_rules.rules_version
            != execution_provider.instrument_rules.rules_version
        ):
            raise PaperTradingConfigurationError("instrument rules versions do not match")
        if portfolio.snapshot.stream_key != risk_manager.instrument_rules.stream_key:
            raise PaperTradingConfigurationError("portfolio stream does not match risk stream")
        self._config = config
        self._portfolio = portfolio
        self._risk_manager = risk_manager
        self._execution_provider = execution_provider
        self._cycle_id_factory = cycle_id_factory
        self._observer = observer
        self._clock = monotonic_clock
        self._dedup: OrderedDict[str, PaperTradingCycleResult] = OrderedDict()
        self._status = PaperTradingEngineStatus()
        self._last_result: PaperTradingCycleResult | None = None

    @property
    def status(self) -> PaperTradingEngineStatus:
        return replace(self._status)

    @property
    def last_result(self) -> PaperTradingCycleResult | None:
        return self._last_result

    def process(
        self,
        decision_result: DecisionPipelineResult,
        market_snapshot: MarketSnapshot,
    ) -> PaperTradingCycleResult:
        if self._status.health is PaperTradingEngineHealth.DEGRADED:
            raise PaperTradingStateError("paper trading engine is degraded")
        if self._status.activity is not PaperTradingEngineActivity.IDLE:
            raise PaperTradingStateError("paper trading engine is already processing")
        self._status = replace(self._status, activity=PaperTradingEngineActivity.PROCESSING)
        started = self._now()
        failed_before = self._status.failed_cycles
        try:
            self._validate_decision(decision_result, market_snapshot)
            key = self._dedup_key(decision_result)
            duplicate = self._dedup.get(key)
            if duplicate is not None:
                self._status = replace(
                    self._status,
                    ignored_duplicates=self._status.ignored_duplicates + 1,
                )
                return duplicate
            return self._process_unique(decision_result, market_snapshot, key, started)
        except PaperTradingStateError:
            raise
        except PaperTradingInvariantError as error:
            if self._status.failed_cycles == failed_before:
                self._fail(PaperTradingFailureCategory.DECISION_VALIDATION, error)
            raise
        except PaperTradingError as error:
            if self._status.failed_cycles == failed_before:
                self._fail(PaperTradingFailureCategory.INTERNAL_ERROR, error)
            raise
        except Exception as error:
            wrapped = PaperTradingDecisionError("unexpected paper cycle failure")
            self._fail(PaperTradingFailureCategory.INTERNAL_ERROR, wrapped)
            raise wrapped from error
        finally:
            self._status = replace(self._status, activity=PaperTradingEngineActivity.IDLE)

    def _process_unique(
        self,
        decision: DecisionPipelineResult,
        market: MarketSnapshot,
        key: str,
        started: float,
    ) -> PaperTradingCycleResult:
        self._status = replace(
            self._status,
            handled_decisions=self._status.handled_decisions + 1,
        )
        cycle_id = create_cycle_id(self._cycle_id_factory)
        self._status = replace(
            self._status,
            last_cycle_id=cycle_id,
            last_intent_id=decision.order_intent.intent_id if decision.order_intent else None,
        )
        latest = market.latest_closed_candle
        if latest is None:
            error = PaperTradingDecisionError("market snapshot requires a closed candle")
            self._fail(PaperTradingFailureCategory.DECISION_VALIDATION, error)
            raise error
        at_start = self._portfolio.snapshot
        mark_started = self._now()
        try:
            valued = self._portfolio.mark_to_market(
                latest.close, market.as_of_ms, decision.correlation_id
            )
        except Exception as error:
            wrapped = PaperTradingPortfolioError("portfolio valuation failed")
            self._fail(PaperTradingFailureCategory.PORTFOLIO_VALUATION, wrapped)
            raise wrapped from error
        portfolio_ms = self._elapsed(mark_started)
        try:
            self._validate_valued(at_start, valued, decision, market)
        except PaperTradingInvariantError as error:
            wrapped = PaperTradingPortfolioError("portfolio valuation result is invalid")
            self._fail(PaperTradingFailureCategory.PORTFOLIO_VALUATION, wrapped)
            raise wrapped from error
        base_stages = (
            PaperTradingStage.DECISION_RECEIVED,
            PaperTradingStage.DECISION_VALIDATED,
            PaperTradingStage.PORTFOLIO_VALUED,
        )
        risk: RiskDecision | None = None
        execution: PaperExecutionResult | None = None
        risk_ms = 0
        execution_ms = 0
        after = valued
        if decision.outcome is PipelineOutcome.HOLD:
            outcome = PaperTradingCycleOutcome.NO_INTENT
            stages = (*base_stages, PaperTradingStage.NO_INTENT)
            observe = self._config.observe_no_intent
        elif decision.outcome is PipelineOutcome.PLANNER_DECLINED:
            outcome = PaperTradingCycleOutcome.PLANNER_DECLINED
            stages = (*base_stages, PaperTradingStage.PLANNER_DECLINED)
            observe = self._config.observe_no_intent
        else:
            risk_started = self._now()
            try:
                risk = self._risk_manager.evaluate(decision.order_intent, valued, market)  # type: ignore[arg-type]
            except Exception as error:
                wrapped = PaperTradingRiskError("risk evaluation failed")
                self._fail(PaperTradingFailureCategory.RISK_EVALUATION, wrapped)
                raise wrapped from error
            risk_ms = self._elapsed(risk_started)
            try:
                self._validate_risk(risk, decision, valued, market)
            except PaperTradingInvariantError as error:
                wrapped = PaperTradingRiskError("risk result is invalid")
                self._fail(PaperTradingFailureCategory.RISK_EVALUATION, wrapped)
                raise wrapped from error
            if risk.outcome is RiskDecisionOutcome.REJECTED:
                outcome = PaperTradingCycleOutcome.RISK_REJECTED
                stages = (
                    *base_stages,
                    PaperTradingStage.RISK_EVALUATED,
                    PaperTradingStage.RISK_REJECTED,
                )
                observe = self._config.observe_risk_rejected
            else:
                execution_started = self._now()
                try:
                    execution = self._execution_provider.execute(risk)
                except Exception as error:
                    wrapped = PaperTradingExecutionError("paper execution failed")
                    self._fail(PaperTradingFailureCategory.EXECUTION, wrapped)
                    raise wrapped from error
                execution_ms = self._elapsed(execution_started)
                try:
                    self._validate_execution(execution, risk)
                except PaperTradingInvariantError as error:
                    wrapped = PaperTradingExecutionError("execution result is invalid")
                    self._fail(PaperTradingFailureCategory.EXECUTION, wrapped)
                    raise wrapped from error
                if execution.status is PaperExecutionStatus.NOT_EXECUTED:
                    outcome = PaperTradingCycleOutcome.NOT_EXECUTED
                    stages = (
                        *base_stages,
                        PaperTradingStage.RISK_EVALUATED,
                        PaperTradingStage.EXECUTION_COMPLETED,
                        PaperTradingStage.NO_FILL,
                    )
                else:
                    fill = execution.fill
                    apply_started = self._now()
                    try:
                        after = self._portfolio.apply_fill(fill, latest.close)  # type: ignore[arg-type]
                        self._validate_applied(valued, after, execution)
                    except Exception as error:
                        wrapped = PaperTradingPortfolioError(
                            "portfolio apply failed after FILLED execution"
                        )
                        self._status = replace(
                            self._status, health=PaperTradingEngineHealth.DEGRADED
                        )
                        self._fail(PaperTradingFailureCategory.PORTFOLIO_APPLY, wrapped)
                        raise wrapped from error
                    portfolio_ms += self._elapsed(apply_started)
                    outcome = PaperTradingCycleOutcome.EXECUTED
                    stages = (
                        *base_stages,
                        PaperTradingStage.RISK_EVALUATED,
                        PaperTradingStage.EXECUTION_COMPLETED,
                        PaperTradingStage.PORTFOLIO_UPDATED,
                    )
                observe = True
        provisional = self._result(
            cycle_id,
            decision,
            at_start,
            valued,
            after,
            risk,
            execution,
            outcome,
            stages,
            risk_ms,
            execution_ms,
            portfolio_ms,
            0,
            self._elapsed(started),
            PaperTradingObservationStatus.NOT_ATTEMPTED,
        )
        final = self._observe(provisional, observe, started)
        self._store(key, final)
        self._complete(final)
        return final

    def _observe(
        self, result: PaperTradingCycleResult, enabled: bool, started: float
    ) -> PaperTradingCycleResult:
        if not enabled:
            return replace(result, total_duration_ms=self._elapsed(started))
        observation_started = self._now()
        record = self._record(result, PaperTradingObservationStatus.OBSERVED, None)
        try:
            self._observer.on_cycle(record)
        except Exception as error:
            duration = self._elapsed(observation_started)
            message = self._safe_message(error)
            _LOGGER.exception(
                "Paper observer failed cycle_id=%s pipeline_run_id=%s",
                result.cycle_id,
                result.decision_pipeline_run_id,
            )
            self._status = replace(
                self._status,
                observation_failures=self._status.observation_failures + 1,
            )
            return replace(
                result,
                observation_status=PaperTradingObservationStatus.OBSERVATION_FAILED,
                failure_category=PaperTradingFailureCategory.OBSERVER,
                failure_message=message,
                observation_duration_ms=duration,
                total_duration_ms=self._elapsed(started),
            )
        except BaseException as error:
            self._status = replace(self._status, health=PaperTradingEngineHealth.DEGRADED)
            self._fail(PaperTradingFailureCategory.OBSERVER, error)
            raise
        duration = self._elapsed(observation_started)
        return replace(
            result,
            observation_status=PaperTradingObservationStatus.OBSERVED,
            observation_duration_ms=duration,
            total_duration_ms=self._elapsed(started),
        )

    def _result(
        self,
        cycle_id: str,
        decision: DecisionPipelineResult,
        at_start: PortfolioSnapshot,
        before: PortfolioSnapshot,
        after: PortfolioSnapshot,
        risk: RiskDecision | None,
        execution: PaperExecutionResult | None,
        outcome: PaperTradingCycleOutcome,
        stages: tuple[PaperTradingStage, ...],
        risk_ms: int,
        execution_ms: int,
        portfolio_ms: int,
        observation_ms: int,
        total_ms: int,
        observation_status: PaperTradingObservationStatus,
    ) -> PaperTradingCycleResult:
        fill = execution.fill if execution else None
        intent = decision.order_intent
        return PaperTradingCycleResult(
            cycle_id=cycle_id,
            decision_pipeline_run_id=decision.pipeline_run_id,
            market_snapshot_id=decision.snapshot_id,
            signal_id=decision.signal.signal_id,
            intent_id=intent.intent_id if intent else None,
            risk_decision_id=risk.decision_id if risk else None,
            execution_id=execution.execution_id if execution else None,
            fill_id=fill.fill_id if fill else None,
            portfolio_id=before.portfolio_id,
            portfolio_version_at_start=at_start.version,
            portfolio_version_before=before.version,
            portfolio_version_after=after.version,
            portfolio_before=before,
            portfolio_after=after,
            decision_result=decision,
            risk_decision=risk,
            execution_result=execution,
            outcome=outcome,
            reason_code=self._reason(decision, risk, execution),
            correlation_id=decision.correlation_id,
            completed_stages=stages,
            risk_duration_ms=risk_ms,
            execution_duration_ms=execution_ms,
            portfolio_duration_ms=portfolio_ms,
            observation_duration_ms=observation_ms,
            total_duration_ms=total_ms,
            observation_status=observation_status,
            metadata={
                "engine_name": self._config.engine_name,
                "engine_version": self._config.engine_version,
                "portfolio_version_at_start": at_start.version,
            },
        )

    def _record(
        self,
        result: PaperTradingCycleResult,
        observation: PaperTradingObservationStatus,
        failure: PaperTradingFailureCategory | None,
    ) -> PaperTradingRecord:
        decision = result.decision_result
        intent = decision.order_intent
        risk = result.risk_decision
        execution = result.execution_result
        fill = execution.fill if execution else None
        after = result.portfolio_after
        return PaperTradingRecord(
            cycle_id=result.cycle_id,
            decision_pipeline_run_id=result.decision_pipeline_run_id,
            market_snapshot_id=result.market_snapshot_id,
            signal_direction=decision.signal.direction,
            signal_reason_code=decision.signal.reason_code,
            intent_id=result.intent_id,
            intent_action=intent.action if intent else None,
            risk_decision_id=result.risk_decision_id,
            risk_outcome=risk.outcome if risk else None,
            risk_rejection_reasons=risk.rejection_reasons if risk else (),
            risk_modification_reasons=risk.modification_reasons if risk else (),
            execution_id=result.execution_id,
            execution_status=execution.status.value if execution else None,
            fill_id=result.fill_id,
            side=fill.side.value if fill else None,
            quantity=fill.quantity if fill else None,
            fill_price=fill.price if fill else None,
            gross_notional=fill.gross_notional if fill else None,
            fee=fill.fee if fill else None,
            quote_balance_after=after.quote_balance,
            base_balance_after=after.base_balance,
            position_quantity_after=after.position.quantity if after.position else None,
            realised_pnl=after.realised_pnl,
            unrealised_pnl=after.unrealised_pnl,
            equity=after.current_equity,
            drawdown=after.drawdown,
            cycle_outcome=result.outcome,
            reason_code=result.reason_code,
            observation_status=observation,
            failure_category=failure,
            correlation_id=result.correlation_id,
            metadata={"portfolio_version": after.version},
        )

    @staticmethod
    def _validate_decision(decision: object, market: object) -> None:
        if not isinstance(decision, DecisionPipelineResult):
            raise PaperTradingDecisionError("decision_result must be DecisionPipelineResult")
        if not isinstance(market, MarketSnapshot):
            raise PaperTradingDecisionError("market_snapshot must be MarketSnapshot")
        signal = decision.signal
        intent = decision.order_intent
        if decision.snapshot_id != market.snapshot_id or decision.stream_key != market.stream_key:
            raise PaperTradingDecisionError("decision and market snapshot do not match")
        if decision.evaluated_at_ms != market.as_of_ms:
            raise PaperTradingDecisionError("decision and market timestamps do not match")
        if decision.correlation_id != market.correlation_cycle_id:
            raise PaperTradingDecisionError("decision and market correlation do not match")
        if signal.snapshot_id != market.snapshot_id or signal.stream_key != market.stream_key:
            raise PaperTradingDecisionError("signal does not belong to market snapshot")
        if signal.effective_at_ms != market.as_of_ms:
            raise PaperTradingDecisionError("signal time does not match market snapshot")
        if intent is not None and (
            intent.source_signal_id != signal.signal_id
            or intent.stream_key != market.stream_key
            or intent.correlation_id != decision.correlation_id
        ):
            raise PaperTradingDecisionError("intent does not belong to decision")

    @staticmethod
    def _dedup_key(decision: DecisionPipelineResult) -> str:
        if decision.outcome is PipelineOutcome.INTENT_CREATED:
            if decision.order_intent is None:
                raise PaperTradingDecisionError("intent-created decision has no intent")
            return f"intent:{decision.order_intent.intent_id}"
        return f"pipeline:{decision.pipeline_run_id}"

    @staticmethod
    def _validate_valued(
        before: PortfolioSnapshot,
        valued: PortfolioSnapshot,
        decision: DecisionPipelineResult,
        market: MarketSnapshot,
    ) -> None:
        if valued.portfolio_id != before.portfolio_id or valued.stream_key != market.stream_key:
            raise PaperTradingInvariantError("valued portfolio identity changed")
        if valued.as_of_ms != market.as_of_ms or valued.correlation_id != decision.correlation_id:
            raise PaperTradingInvariantError("valued portfolio context does not match decision")
        if valued.version not in {before.version, before.version + 1}:
            raise PaperTradingInvariantError("invalid valuation version progression")

    @staticmethod
    def _validate_risk(
        risk: object,
        decision: DecisionPipelineResult,
        portfolio: PortfolioSnapshot,
        market: MarketSnapshot,
    ) -> None:
        intent = decision.order_intent
        if not isinstance(risk, RiskDecision) or intent is None:
            raise PaperTradingInvariantError("risk result is invalid")
        if (
            risk.intent_id != intent.intent_id
            or risk.market_snapshot_id != market.snapshot_id
            or risk.portfolio_id != portfolio.portfolio_id
            or risk.portfolio_version != portfolio.version
            or risk.stream_key != market.stream_key
            or risk.correlation_id != decision.correlation_id
            or risk.decided_at_ms != market.as_of_ms
        ):
            raise PaperTradingInvariantError("risk result does not match cycle context")

    @staticmethod
    def _validate_execution(execution: object, risk: RiskDecision) -> None:
        if not isinstance(execution, PaperExecutionResult):
            raise PaperTradingInvariantError("execution result is invalid")
        if (
            execution.risk_decision_id != risk.decision_id
            or execution.intent_id != risk.intent_id
            or execution.correlation_id != risk.correlation_id
        ):
            raise PaperTradingInvariantError("execution result does not match risk decision")
        fill = execution.fill
        if fill is not None and (
            fill.risk_decision_id != risk.decision_id
            or fill.intent_id != risk.intent_id
            or fill.stream_key != risk.stream_key
            or fill.correlation_id != risk.correlation_id
            or fill.order_id != execution.execution_id
            or fill.filled_at_ms != execution.completed_at_ms
        ):
            raise PaperTradingInvariantError("fill does not match execution context")

    @staticmethod
    def _validate_applied(
        valued: PortfolioSnapshot,
        after: PortfolioSnapshot,
        execution: PaperExecutionResult,
    ) -> None:
        fill = execution.fill
        if fill is None or after.portfolio_id != valued.portfolio_id:
            raise PaperTradingInvariantError("applied portfolio identity changed")
        if after.version != valued.version + 1 or after.applied_fill_count != (
            valued.applied_fill_count + 1
        ):
            raise PaperTradingInvariantError("fill was not applied exactly once")
        if after.as_of_ms != fill.filled_at_ms or after.correlation_id != fill.correlation_id:
            raise PaperTradingInvariantError("applied portfolio context does not match fill")

    @staticmethod
    def _reason(
        decision: DecisionPipelineResult,
        risk: RiskDecision | None,
        execution: PaperExecutionResult | None,
    ) -> str:
        if execution is not None:
            return execution.reason_code
        if risk is not None and risk.rejection_reasons:
            return risk.rejection_reasons[0]
        return decision.reason_code

    def _store(self, key: str, result: PaperTradingCycleResult) -> None:
        if len(self._dedup) >= self._config.dedup_capacity:
            self._dedup.popitem(last=False)
        self._dedup[key] = result
        self._last_result = result

    def _complete(self, result: PaperTradingCycleResult) -> None:
        field = {
            PaperTradingCycleOutcome.NO_INTENT: "no_intent_cycles",
            PaperTradingCycleOutcome.PLANNER_DECLINED: "planner_declined_cycles",
            PaperTradingCycleOutcome.RISK_REJECTED: "risk_rejected_cycles",
            PaperTradingCycleOutcome.NOT_EXECUTED: "not_executed_cycles",
            PaperTradingCycleOutcome.EXECUTED: "executed_cycles",
        }[result.outcome]
        self._status = replace(
            self._status,
            **{field: getattr(self._status, field) + 1},
            last_risk_outcome=result.risk_decision.outcome if result.risk_decision else None,
            last_execution_status=(
                result.execution_result.status.value if result.execution_result else None
            ),
            last_fill_id=result.fill_id,
            last_portfolio_version=result.portfolio_version_after,
            last_error_category=(
                PaperTradingFailureCategory.OBSERVER
                if result.observation_status is PaperTradingObservationStatus.OBSERVATION_FAILED
                else None
            ),
            last_error_message=result.failure_message,
        )

    def _fail(self, category: PaperTradingFailureCategory, error: Exception) -> None:
        self._status = replace(
            self._status,
            failed_cycles=self._status.failed_cycles + 1,
            last_error_category=category,
            last_error_message=self._safe_message(error),
        )

    def _now(self) -> float:
        try:
            value = self._clock()
        except Exception:  # noqa: BLE001 - metrics must not affect business state
            return 0.0
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0.0
        return float(value)

    def _elapsed(self, started: float) -> int:
        return max(0, int((self._now() - started) * 1000))

    @staticmethod
    def _safe_message(error: BaseException) -> str:
        message = str(error).strip() or type(error).__name__
        return message[:500]
