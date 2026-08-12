from collections.abc import Callable

from app.exchange_execution.exceptions import ExchangeSubmissionSafetyError
from app.exchange_execution.models import (
    ExchangeCommand,
    ExchangeOrderStatus,
    ExchangeTrade,
    TransportOutcomeKind,
)
from app.exchange_execution.ports import (
    ExchangeOrderGateway,
    MarketOrderSubmission,
    PreSubmitAuthorization,
)
from app.persistence.exchange_models import DurableExchangeOrder, LocalExchangeRecoveryResult
from app.persistence.ports import ExchangeLifecyclePersistence


class DurableExchangeOrderLifecycle:
    """Persist-before-side-effect orchestration with no retry or reconciliation policy."""

    def __init__(
        self,
        persistence: ExchangeLifecyclePersistence,
        gateway: ExchangeOrderGateway,
        *,
        timestamp_provider: Callable[[], int],
    ) -> None:
        if not isinstance(persistence, ExchangeLifecyclePersistence):
            raise TypeError("persistence must implement ExchangeLifecyclePersistence")
        if not isinstance(gateway, ExchangeOrderGateway):
            raise TypeError("gateway must implement ExchangeOrderGateway")
        if not callable(timestamp_provider):
            raise TypeError("timestamp_provider must be callable")
        self._persistence = persistence
        self._gateway = gateway
        self._timestamp_provider = timestamp_provider

    def prepare(
        self,
        *,
        execution_root_id: str,
        session_id: str,
        command: ExchangeCommand,
        client_order_id: str,
    ) -> DurableExchangeOrder:
        return self._persistence.prepare_order(
            execution_root_id=execution_root_id,
            session_id=session_id,
            command=command,
            client_order_id=client_order_id,
            occurred_at_ms=self._timestamp(),
        )

    def _submit_authorized(self, authorization: PreSubmitAuthorization) -> DurableExchangeOrder:
        """Internal side-effect primitive; only the final gate may call this method."""
        if not isinstance(authorization, PreSubmitAuthorization):
            raise ExchangeSubmissionSafetyError("final pre-submit authorization is required")
        durable_command_key = authorization.durable_command_key
        current = self._persistence.load_order(durable_command_key)
        if current.order.status is not ExchangeOrderStatus.PREPARED:
            raise ExchangeSubmissionSafetyError("authorized order is no longer PREPARED")
        if (
            current.execution_root_id != authorization.execution_root_id
            or current.session_id != authorization.session_id
            or current.order.account_fingerprint != authorization.account_fingerprint
            or current.order.client_order_id != authorization.client_order_id
        ):
            raise ExchangeSubmissionSafetyError("authorization/order lineage mismatch")
        authorization._consume()
        submitting = self._persistence.begin_submission(
            durable_command_key, occurred_at_ms=self._timestamp()
        )
        request = MarketOrderSubmission(
            submitting.order.durable_command_key,
            submitting.order.client_order_id,
            submitting.order.account_fingerprint,
            submitting.order.stream_key,
            submitting.order.side,
            submitting.order.original_base_quantity,
        )
        observation = self._gateway.submit_market_order(request)
        now = self._timestamp()
        if observation.transport_outcome is TransportOutcomeKind.RESPONSE_RECEIVED:
            if (
                observation.client_order_id != submitting.order.client_order_id
                or observation.exchange_status is not ExchangeOrderStatus.ACKNOWLEDGED
                or observation.exchange_order_id is None
            ):
                return self._persistence.record_status(
                    durable_command_key,
                    status=ExchangeOrderStatus.RECONCILIATION_REQUIRED,
                    occurred_at_ms=now,
                    reason_code="malformed_submission_acknowledgement",
                    count_failure=True,
                )
            return self._persistence.record_status(
                durable_command_key,
                status=ExchangeOrderStatus.ACKNOWLEDGED,
                occurred_at_ms=now,
                reason_code="exchange_acknowledged",
                exchange_order_id=observation.exchange_order_id,
                acknowledged_at_ms=observation.transact_time_ms,
            )
        if observation.transport_outcome is TransportOutcomeKind.DETERMINISTIC_REJECTION:
            if observation.exchange_order_id is not None or observation.exchange_status is not None:
                return self._persistence.record_status(
                    durable_command_key,
                    status=ExchangeOrderStatus.RECONCILIATION_REQUIRED,
                    occurred_at_ms=now,
                    reason_code="conflicting_deterministic_rejection",
                    count_failure=True,
                )
            return self._persistence.record_status(
                durable_command_key,
                status=ExchangeOrderStatus.REJECTED,
                occurred_at_ms=now,
                reason_code="deterministic_exchange_rejection",
                count_failure=True,
            )
        return self._persistence.record_status(
            durable_command_key,
            status=ExchangeOrderStatus.RECONCILIATION_REQUIRED,
            occurred_at_ms=now,
            reason_code="submission_outcome_ambiguous",
            count_failure=True,
        )

    def record_exchange_trades(
        self, durable_command_key: str, trades: tuple[ExchangeTrade, ...]
    ) -> tuple[ExchangeTrade, ...]:
        return self._persistence.persist_trades(durable_command_key, trades)

    def inspect_local_recovery(self) -> LocalExchangeRecoveryResult:
        return self._persistence.recover_local()

    def _timestamp(self) -> int:
        value = self._timestamp_provider()
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise TypeError("timestamp provider must return a non-negative integer")
        return value
