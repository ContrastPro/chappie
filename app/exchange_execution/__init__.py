from app.exchange_execution.durable import DurableExchangeOrderLifecycle
from app.exchange_execution.factories import (
    ClientOrderIdFactory,
    DeterministicClientOrderIdFactory,
    create_client_order_id,
)
from app.exchange_execution.lifecycle import (
    aggregate_pending_exposure,
    aggregate_trades,
    assert_submission_gates,
    classify_settlement,
    status_after_transport,
    transition_order,
)
from app.exchange_execution.models import (
    ExchangeCommand,
    ExchangeOrder,
    ExchangeOrderEvent,
    ExchangeOrderEventSource,
    ExchangeOrderSide,
    ExchangeOrderStatus,
    ExchangeTrade,
    ExchangeTradeIdentity,
    ExecutionEnvironment,
    ExecutionSession,
    ExecutionSessionState,
    KillSwitch,
    KillSwitchState,
    PendingExposure,
    QueryResultKind,
    SettlementClassification,
    TradeAggregate,
    TransportFailureCategory,
    TransportOutcomeKind,
)
from app.exchange_execution.ports import (
    AccountBalance,
    ExchangeAccountReader,
    ExchangeInstrumentRules,
    ExchangeOrderGateway,
    ExchangeTradeHistory,
    MarketOrderSubmission,
    OrderQueryResult,
    SubmissionCapability,
    SubmissionObservation,
)
from app.exchange_execution.reconciliation import (
    ExchangeReconciler,
    ExchangeReconciliationReason,
    ExchangeReconciliationResult,
)

__all__ = [
    "AccountBalance",
    "ClientOrderIdFactory",
    "DeterministicClientOrderIdFactory",
    "DurableExchangeOrderLifecycle",
    "ExchangeAccountReader",
    "ExchangeCommand",
    "ExchangeInstrumentRules",
    "ExchangeOrder",
    "ExchangeOrderEvent",
    "ExchangeOrderEventSource",
    "ExchangeOrderGateway",
    "ExchangeOrderSide",
    "ExchangeOrderStatus",
    "ExchangeReconciler",
    "ExchangeReconciliationReason",
    "ExchangeReconciliationResult",
    "ExchangeTrade",
    "ExchangeTradeHistory",
    "ExchangeTradeIdentity",
    "ExecutionEnvironment",
    "ExecutionSession",
    "ExecutionSessionState",
    "KillSwitch",
    "KillSwitchState",
    "MarketOrderSubmission",
    "OrderQueryResult",
    "PendingExposure",
    "QueryResultKind",
    "SettlementClassification",
    "SubmissionCapability",
    "SubmissionObservation",
    "TradeAggregate",
    "TransportFailureCategory",
    "TransportOutcomeKind",
    "aggregate_pending_exposure",
    "aggregate_trades",
    "assert_submission_gates",
    "classify_settlement",
    "create_client_order_id",
    "status_after_transport",
    "transition_order",
]

# M4.10/Stage 4 experiments remain importable for historical audits, but they
# are deliberately not part of the active continuous MVP import surface.
_HISTORICAL_EXPORTS = {
    "DISPOSITION_AUTHORIZATION_ENV": ("stage4_sell_disposition", "DISPOSITION_AUTHORIZATION_ENV"),
    "RECOVERY_AUTHORIZATION_ENV": ("stage4_sell_recovery", "RECOVERY_AUTHORIZATION_ENV"),
    "Phase3BalanceScope": ("phase3", "Phase3BalanceScope"),
    "Phase3ClaimReceipt": ("phase3", "Phase3ClaimReceipt"),
    "Phase3ProcessClaim": ("phase3", "Phase3ProcessClaim"),
    "Phase3RiskEnvelope": ("phase3", "Phase3RiskEnvelope"),
    "ReviewedPhase3SmokeRequest": ("phase3", "ReviewedPhase3SmokeRequest"),
    "derive_phase3_economic_slot": ("phase3", "derive_phase3_economic_slot"),
    "ReviewedStage4ManagedSellRequest": ("stage4_sell", "ReviewedStage4ManagedSellRequest"),
    "Stage4ManagedSellAuthority": ("stage4_sell", "Stage4ManagedSellAuthority"),
    "Stage4ManagedSellAuthorityEvent": ("stage4_sell", "Stage4ManagedSellAuthorityEvent"),
    "Stage4ManagedSellClaimReceipt": ("stage4_sell", "Stage4ManagedSellClaimReceipt"),
    "Stage4ManagedSellProcessClaim": ("stage4_sell", "Stage4ManagedSellProcessClaim"),
    "Stage4ManagedSellRiskEnvelope": ("stage4_sell", "Stage4ManagedSellRiskEnvelope"),
    "derive_stage4_sell_economic_slot": ("stage4_sell", "derive_stage4_sell_economic_slot"),
    "derive_stage4_sell_successor_economic_slot": (
        "stage4_sell",
        "derive_stage4_sell_successor_economic_slot",
    ),
    "Stage4SellDisposition": ("stage4_sell_disposition", "Stage4SellDisposition"),
    "Stage4SellDispositionAuthorization": (
        "stage4_sell_disposition",
        "Stage4SellDispositionAuthorization",
    ),
    "Stage4SellDispositionError": ("stage4_sell_disposition", "Stage4SellDispositionError"),
    "dispose_stage4_sell_no_execution": (
        "stage4_sell_disposition",
        "dispose_stage4_sell_no_execution",
    ),
    "Stage4SellRecoveryAuthorization": ("stage4_sell_recovery", "Stage4SellRecoveryAuthorization"),
    "Stage4SellRecoveryDecisionStore": ("stage4_sell_recovery", "Stage4SellRecoveryDecisionStore"),
    "Stage4SellRecoveryEvent": ("stage4_sell_recovery", "Stage4SellRecoveryEvent"),
    "Stage4SellRecoveryEventType": ("stage4_sell_recovery", "Stage4SellRecoveryEventType"),
    "Stage4SellRecoveryExchangeState": ("stage4_sell_recovery", "Stage4SellRecoveryExchangeState"),
    "Stage4SellRecoveryReason": ("stage4_sell_recovery", "Stage4SellRecoveryReason"),
    "Stage4SellRecoveryReview": ("stage4_sell_recovery", "Stage4SellRecoveryReview"),
    "evaluate_stage4_sell_recovery": ("stage4_sell_recovery", "evaluate_stage4_sell_recovery"),
}


def __getattr__(name: str):
    """Lazily expose historical-only APIs for explicit audit tooling."""
    try:
        module_name, attribute = _HISTORICAL_EXPORTS[name]
    except KeyError as error:
        raise AttributeError(name) from error
    from importlib import import_module

    value = getattr(import_module(f"{__name__}.{module_name}"), attribute)
    globals()[name] = value
    return value
