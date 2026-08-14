from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from app.exchange_execution.exceptions import ExchangeCapabilityError, ExchangeExecutionError
from app.exchange_execution.models import require_hash, require_text, require_timestamp

RECOVERY_AUTHORIZATION_ENV = "STAGE4_SELL_RECOVERY_AUTHORIZED"
RECOVERY_AUTHORIZATION_VALUE = "YES"
RECOVERY_POLICY_VERSION = "stage4-sell-recovery/v1"


class Stage4SellRecoveryError(ExchangeExecutionError):
    pass


class Stage4SellRecoveryEventType(StrEnum):
    REQUIRED = "SELL_RECOVERY_REQUIRED"
    CONTINUATION_ALLOWED = "RECOVERY_CONTINUATION_ALLOWED"
    DISPOSITION_REQUIRED = "DISPOSITION_REQUIRED"


class Stage4SellRecoveryReason(StrEnum):
    SNAPSHOT_RULE_CHANGED = "SNAPSHOT_RULE_CHANGED"
    SNAPSHOT_PRICE_CHANGED = "SNAPSHOT_PRICE_CHANGED"
    SNAPSHOT_BALANCE_CHANGED = "SNAPSHOT_BALANCE_CHANGED"
    SNAPSHOT_HISTORY_CHANGED = "SNAPSHOT_HISTORY_CHANGED"
    SNAPSHOT_OBSERVATION_FAILED = "SNAPSHOT_OBSERVATION_FAILED"
    PORTFOLIO_CHANGED = "PORTFOLIO_CHANGED"
    SESSION_CHANGED = "SESSION_CHANGED"
    RECONCILIATION_CHANGED = "RECONCILIATION_CHANGED"
    CONTROL_CHANGED = "CONTROL_CHANGED"
    FINAL_GATE_REJECTED = "FINAL_GATE_REJECTED"
    CLAIM_FAILURE = "CLAIM_FAILURE"
    MANUAL_DISPOSITION = "MANUAL_DISPOSITION"
    VERIFIED_NO_EXCHANGE_ORDER = "VERIFIED_NO_EXCHANGE_ORDER"
    EXCHANGE_STATE_UNKNOWN = "EXCHANGE_STATE_UNKNOWN"
    EXCHANGE_ORDER_PRESENT = "EXCHANGE_ORDER_PRESENT"
    RISK_INCOMPATIBLE = "RISK_INCOMPATIBLE"
    CONFIGURATION_CHANGED = "CONFIGURATION_CHANGED"
    OWNERSHIP_CHANGED = "OWNERSHIP_CHANGED"
    OPERATOR_DECLINED_CONTINUATION = "OPERATOR_DECLINED_CONTINUATION"


class Stage4SellRecoveryExchangeState(StrEnum):
    """Sanitized conclusion of the bounded exchange-absence review.

    This is deliberately a conclusion, not a transport response.  In particular,
    ``NOT_FOUND`` must be classified as ``UNKNOWN`` unless the caller has gathered
    the full absence proof required by ADR-067.
    """

    ABSENCE_PROVEN = "ABSENCE_PROVEN"
    UNKNOWN = "UNKNOWN"
    ORDER_PRESENT = "ORDER_PRESENT"


class Stage4SellRecoveryAuthorization:
    __slots__ = ("_consumed", "_nonce")
    _ISSUER = object()

    def __init__(self, *, _issuer: object | None = None) -> None:
        if _issuer is not self._ISSUER:
            raise ExchangeCapabilityError("SELL recovery authorization cannot be constructed")
        self._nonce = object()
        self._consumed = False

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> Stage4SellRecoveryAuthorization:
        if environ.get(RECOVERY_AUTHORIZATION_ENV) != RECOVERY_AUTHORIZATION_VALUE:
            raise Stage4SellRecoveryError("SELL recovery authorization is absent or invalid")
        return cls(_issuer=cls._ISSUER)

    def _consume(self) -> None:
        if self._consumed:
            raise ExchangeCapabilityError("SELL recovery authorization was already consumed")
        self._consumed = True


@runtime_checkable
class Stage4SellRecoveryDecisionStore(Protocol):
    """Narrow persistence boundary used by the recovery-decision service."""

    def decide_stage4_sell_recovery(
        self,
        durable_command_key: str,
        *,
        decision: Stage4SellRecoveryEventType,
        reasons: tuple[str, ...],
        snapshot_digest: str,
        current_rules_fingerprint: str,
        occurred_at_ms: int,
        authorization: Stage4SellRecoveryAuthorization,
    ) -> Stage4SellRecoveryEvent: ...


@dataclass(frozen=True, slots=True)
class Stage4SellRecoveryReview:
    """The non-secret, non-capability-bearing inputs to an ADR-067 decision.

    Durable identity, Portfolio/session lineage, reconciliation, settlement and
    control are reread atomically by persistence.  The remaining values are the
    bounded external/operator conclusions that persistence cannot safely infer.
    """

    snapshot_digest: str
    current_rules_fingerprint: str
    exchange_state: Stage4SellRecoveryExchangeState
    risk_compatible: bool
    configuration_compatible: bool
    managed_ownership_compatible: bool
    operator_approves_continuation: bool

    def __post_init__(self) -> None:
        require_hash(self.snapshot_digest, "snapshot_digest")
        require_hash(self.current_rules_fingerprint, "current_rules_fingerprint")
        if not isinstance(self.exchange_state, Stage4SellRecoveryExchangeState):
            raise Stage4SellRecoveryError("exchange_state is invalid")
        for name in (
            "risk_compatible",
            "configuration_compatible",
            "managed_ownership_compatible",
            "operator_approves_continuation",
        ):
            if not isinstance(getattr(self, name), bool):
                raise Stage4SellRecoveryError(f"{name} must be boolean")

    @property
    def reasons(self) -> tuple[str, ...]:
        reasons: set[str] = set()
        if self.exchange_state is Stage4SellRecoveryExchangeState.UNKNOWN:
            reasons.add(Stage4SellRecoveryReason.EXCHANGE_STATE_UNKNOWN.value)
        elif self.exchange_state is Stage4SellRecoveryExchangeState.ORDER_PRESENT:
            reasons.add(Stage4SellRecoveryReason.EXCHANGE_ORDER_PRESENT.value)
        else:
            reasons.add(Stage4SellRecoveryReason.VERIFIED_NO_EXCHANGE_ORDER.value)
        if not self.risk_compatible:
            reasons.add(Stage4SellRecoveryReason.RISK_INCOMPATIBLE.value)
        if not self.configuration_compatible:
            reasons.add(Stage4SellRecoveryReason.CONFIGURATION_CHANGED.value)
        if not self.managed_ownership_compatible:
            reasons.add(Stage4SellRecoveryReason.OWNERSHIP_CHANGED.value)
        if not self.operator_approves_continuation:
            reasons.add(Stage4SellRecoveryReason.OPERATOR_DECLINED_CONTINUATION.value)
        return tuple(sorted(reasons))

    @property
    def decision(self) -> Stage4SellRecoveryEventType:
        if (
            self.exchange_state is Stage4SellRecoveryExchangeState.ABSENCE_PROVEN
            and self.risk_compatible
            and self.configuration_compatible
            and self.managed_ownership_compatible
            and self.operator_approves_continuation
        ):
            return Stage4SellRecoveryEventType.CONTINUATION_ALLOWED
        return Stage4SellRecoveryEventType.DISPOSITION_REQUIRED


def evaluate_stage4_sell_recovery(
    persistence: Stage4SellRecoveryDecisionStore,
    durable_command_key: str,
    *,
    review: Stage4SellRecoveryReview,
    occurred_at_ms: int,
    authorization: Stage4SellRecoveryAuthorization,
) -> Stage4SellRecoveryEvent:
    """Persist exactly one ADR-067 recovery decision without granting submission.

    A continuation event means only that a future, separately implemented final
    gate may evaluate the unchanged durable action.  It neither issues a claim nor
    transitions the order or creates a submission attempt.
    """
    if not isinstance(persistence, Stage4SellRecoveryDecisionStore):
        raise Stage4SellRecoveryError("recovery persistence boundary is invalid")
    require_hash(durable_command_key, "durable_command_key")
    if not isinstance(review, Stage4SellRecoveryReview):
        raise Stage4SellRecoveryError("recovery review is invalid")
    require_timestamp(occurred_at_ms, "occurred_at_ms")
    if not isinstance(authorization, Stage4SellRecoveryAuthorization):
        raise Stage4SellRecoveryError("SELL recovery authorization is required")
    return persistence.decide_stage4_sell_recovery(
        durable_command_key,
        decision=review.decision,
        reasons=review.reasons,
        snapshot_digest=review.snapshot_digest,
        current_rules_fingerprint=review.current_rules_fingerprint,
        occurred_at_ms=occurred_at_ms,
        authorization=authorization,
    )


@dataclass(frozen=True, slots=True)
class Stage4SellRecoveryEvent:
    recovery_event_id: str
    authority_id: str
    durable_command_key: str
    client_order_id: str
    event_ordinal: int
    event_type: Stage4SellRecoveryEventType
    block_reason_code: str
    reason_set_digest: str
    snapshot_digest: str | None
    occurred_at_ms: int
    payload_digest: str
    replayed: bool = False

    def __post_init__(self) -> None:
        for name in ("recovery_event_id", "reason_set_digest", "payload_digest"):
            require_hash(getattr(self, name), name)
        require_text(self.authority_id, "authority_id")
        require_hash(self.durable_command_key, "durable_command_key")
        require_text(self.client_order_id, "client_order_id")
        require_text(self.block_reason_code, "block_reason_code")
        require_timestamp(self.occurred_at_ms, "occurred_at_ms")
        if self.snapshot_digest is not None:
            require_hash(self.snapshot_digest, "snapshot_digest")


def reason_set_digest(reasons: tuple[str, ...]) -> str:
    normalized = tuple(sorted(set(reasons)))
    allowed = {reason.value for reason in Stage4SellRecoveryReason}
    if not normalized or normalized != reasons or any(reason not in allowed for reason in reasons):
        raise Stage4SellRecoveryError("recovery reasons must be sorted and unique")
    payload = json.dumps(
        {"algorithm_version": RECOVERY_POLICY_VERSION, "payload": normalized},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()
