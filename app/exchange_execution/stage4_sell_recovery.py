from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

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
