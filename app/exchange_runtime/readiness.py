from app.exchange_execution.models import (
    ExecutionEnvironment,
    ExecutionSessionState,
    KillSwitchState,
)
from app.exchange_execution.ports import SubmissionCapability
from app.exchange_runtime.models import (
    ExchangeSettlementStatus,
    TestnetExecutionReadiness,
    TestnetReadinessReason,
    TestnetReadinessState,
)
from app.persistence.exchange_models import (
    ExchangeReconciliationStatus,
    ExecutionBootstrap,
    LocalExchangeRecoveryResult,
    LocalExchangeRecoveryStatus,
)


def evaluate_testnet_readiness(
    *,
    enabled: bool,
    bootstrap: ExecutionBootstrap,
    recovery: LocalExchangeRecoveryResult,
    reconciliation_status: ExchangeReconciliationStatus,
    reconciliation_generation: int,
    settlement_status: ExchangeSettlementStatus,
    non_terminal_orders: int,
) -> TestnetExecutionReadiness:
    """Mint process-local authority only when every durable safety gate is current."""
    reasons: list[TestnetReadinessReason] = []
    if not enabled:
        reasons.append(TestnetReadinessReason.MODE_DISABLED)
    if recovery.status is not LocalExchangeRecoveryStatus.CLEAN:
        reasons.append(TestnetReadinessReason.LOCAL_RECOVERY_UNSAFE)
    if reconciliation_status is not ExchangeReconciliationStatus.SAFE:
        reasons.append(TestnetReadinessReason.RECONCILIATION_UNSAFE)
    if settlement_status is ExchangeSettlementStatus.PENDING:
        reasons.append(TestnetReadinessReason.SETTLEMENT_PENDING)
    elif settlement_status is ExchangeSettlementStatus.BLOCKED_UNSUPPORTED_FEE:
        reasons.append(TestnetReadinessReason.SETTLEMENT_BLOCKED)
    if bootstrap.control.kill_switch.state is not KillSwitchState.CLEARED:
        reasons.append(TestnetReadinessReason.KILL_SWITCH_ACTIVE)
    if non_terminal_orders:
        reasons.append(TestnetReadinessReason.UNRESOLVED_ORDER)
    if bootstrap.session.state is not ExecutionSessionState.ACTIVE:
        reasons.append(TestnetReadinessReason.SESSION_INVALID)
    ordered = tuple(sorted(set(reasons), key=lambda value: value.value))
    state = (
        TestnetReadinessState.DISABLED
        if TestnetReadinessReason.MODE_DISABLED in ordered
        else TestnetReadinessState.BLOCKED
        if ordered
        else TestnetReadinessState.READY
    )
    capability = None
    if state is TestnetReadinessState.READY:
        capability = SubmissionCapability._issue(
            reconciliation_generation=reconciliation_generation,
            control_generation=bootstrap.control.kill_switch.generation,
            account_fingerprint=bootstrap.root.account_fingerprint,
            session_id=bootstrap.session.session_id,
        )
    return TestnetExecutionReadiness(
        state,
        ordered,
        reconciliation_generation,
        bootstrap.control.kill_switch.generation,
        bootstrap.session.session_id,
        capability,
        ExecutionEnvironment.TESTNET,
    )
