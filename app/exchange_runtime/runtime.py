from app.exchange_execution import ExchangeReconciler
from app.exchange_runtime.models import TestnetExecutionReadiness
from app.exchange_runtime.readiness import evaluate_testnet_readiness
from app.exchange_runtime.settlement import SQLiteExchangeSettlement
from app.persistence.exchange import SQLiteExchangePersistence


class TestnetRuntimeStartup:
    """Ordered, dependency-injected Testnet startup; performs no implicit network creation."""

    def __init__(
        self,
        persistence: SQLiteExchangePersistence,
        reconciler: ExchangeReconciler,
        settlement: SQLiteExchangeSettlement,
        *,
        execution_root_id: str,
    ) -> None:
        if not isinstance(persistence, SQLiteExchangePersistence):
            raise TypeError("persistence must be SQLiteExchangePersistence")
        if not isinstance(reconciler, ExchangeReconciler):
            raise TypeError("reconciler must be ExchangeReconciler")
        if not isinstance(settlement, SQLiteExchangeSettlement):
            raise TypeError("settlement must be SQLiteExchangeSettlement")
        if not isinstance(execution_root_id, str) or not execution_root_id.strip():
            raise TypeError("execution_root_id must be non-empty")
        self._persistence = persistence
        self._reconciler = reconciler
        self._settlement = settlement
        self._root_id = execution_root_id

    def start(self, *, enabled: bool) -> TestnetExecutionReadiness:
        """Recover, reconcile, settle, then evaluate authority in that exact order."""
        recovery = self._persistence.recover_local()
        reconciliation = self._reconciler.reconcile(self._root_id)
        self._settlement.initialize()
        settled = self._settlement.settle_pending()
        bootstrap = self._persistence.load_bootstrap(self._root_id)
        non_terminal = sum(
            not value.order.status.is_terminal
            for value in self._persistence.list_orders()
            if value.execution_root_id == self._root_id
        )
        return evaluate_testnet_readiness(
            enabled=enabled,
            bootstrap=bootstrap,
            recovery=recovery,
            reconciliation_status=reconciliation.outcome,
            reconciliation_generation=reconciliation.generation_after,
            settlement_status=settled.status,
            non_terminal_orders=non_terminal,
        )
