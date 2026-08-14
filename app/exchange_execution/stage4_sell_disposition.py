from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.exchange_execution.exceptions import ExchangeCapabilityError, ExchangeExecutionError
from app.exchange_execution.models import require_hash, require_text, require_timestamp

DISPOSITION_AUTHORIZATION_ENV = "STAGE4_SELL_DISPOSITION_AUTHORIZED"
DISPOSITION_AUTHORIZATION_VALUE = "YES"
DISPOSITION_POLICY_VERSION = "stage4-sell-disposition/v1"


class Stage4SellDispositionError(ExchangeExecutionError):
    pass


class Stage4SellDispositionAuthorization:
    __slots__ = ("_consumed",)

    def __init__(self, token: object | None = None) -> None:
        if token is not _token:
            raise ExchangeCapabilityError("SELL disposition authorization cannot be constructed")
        self._consumed = False

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> Stage4SellDispositionAuthorization:
        if environ.get(DISPOSITION_AUTHORIZATION_ENV) != DISPOSITION_AUTHORIZATION_VALUE:
            raise Stage4SellDispositionError("SELL disposition authorization is absent or invalid")
        return cls(_token)

    def _consume(self) -> None:
        if self._consumed:
            raise ExchangeCapabilityError("SELL disposition authorization was already consumed")
        self._consumed = True


_token = object()


@dataclass(frozen=True, slots=True)
class Stage4SellDisposition:
    disposition_id: str
    authority_id: str
    durable_command_key: str
    client_order_id: str
    reason_code: str
    occurred_at_ms: int
    replayed: bool = False

    def __post_init__(self) -> None:
        require_hash(self.disposition_id, "disposition_id")
        require_text(self.authority_id, "authority_id")
        require_hash(self.durable_command_key, "durable_command_key")
        require_text(self.client_order_id, "client_order_id")
        require_text(self.reason_code, "reason_code")
        require_timestamp(self.occurred_at_ms, "occurred_at_ms")


@runtime_checkable
class Stage4SellDispositionStore(Protocol):
    def dispose_stage4_sell_no_execution(
        self,
        durable_command_key: str,
        *,
        reason_code: str,
        occurred_at_ms: int,
        authorization: Stage4SellDispositionAuthorization,
    ) -> Stage4SellDisposition: ...


def dispose_stage4_sell_no_execution(
    persistence: Stage4SellDispositionStore,
    durable_command_key: str,
    *,
    reason_code: str,
    occurred_at_ms: int,
    authorization: Stage4SellDispositionAuthorization,
) -> Stage4SellDisposition:
    if not isinstance(persistence, Stage4SellDispositionStore):
        raise Stage4SellDispositionError("disposition persistence boundary is invalid")
    require_hash(durable_command_key, "durable_command_key")
    require_text(reason_code, "reason_code")
    require_timestamp(occurred_at_ms, "occurred_at_ms")
    if not isinstance(authorization, Stage4SellDispositionAuthorization):
        raise Stage4SellDispositionError("SELL disposition authorization is required")
    return persistence.dispose_stage4_sell_no_execution(
        durable_command_key,
        reason_code=reason_code,
        occurred_at_ms=occurred_at_ms,
        authorization=authorization,
    )
