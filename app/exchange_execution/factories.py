import base64
import hashlib
import json
from typing import Protocol, runtime_checkable

from app.exchange_execution.exceptions import ExchangeOrderIdentityError
from app.exchange_execution.models import (
    ExecutionEnvironment,
    require_account_fingerprint,
    require_hash,
)

CLIENT_ORDER_ID_VERSION = "exchange-client-order/v1"
CLIENT_ORDER_ID_PREFIX = "ctt1t-"


@runtime_checkable
class ClientOrderIdFactory(Protocol):
    def create_client_order_id(
        self,
        *,
        environment: ExecutionEnvironment,
        venue: str,
        account_fingerprint: str,
        durable_command_key: str,
    ) -> str: ...


class DeterministicClientOrderIdFactory:
    def create_client_order_id(
        self,
        *,
        environment: ExecutionEnvironment,
        venue: str,
        account_fingerprint: str,
        durable_command_key: str,
    ) -> str:
        if environment is not ExecutionEnvironment.TESTNET:
            raise ExchangeOrderIdentityError("client order IDs support TESTNET only")
        if venue != "binance-spot":
            raise ExchangeOrderIdentityError("venue must be binance-spot")
        require_account_fingerprint(account_fingerprint)
        require_hash(durable_command_key, "durable_command_key")
        payload = {
            "account_fingerprint": account_fingerprint,
            "durable_command_key": durable_command_key,
            "environment": environment.value,
            "venue": venue,
        }
        envelope = {"algorithm_version": CLIENT_ORDER_ID_VERSION, "payload": payload}
        canonical = json.dumps(envelope, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).digest()
        encoded = base64.b32encode(digest).decode("ascii").rstrip("=").lower()
        result = CLIENT_ORDER_ID_PREFIX + encoded[:26]
        if len(result) != 32:  # pragma: no cover - fixed SHA-256/Base32 construction invariant
            raise ExchangeOrderIdentityError("client order ID construction failed")
        return result


def create_client_order_id(
    factory: ClientOrderIdFactory,
    *,
    environment: ExecutionEnvironment,
    venue: str,
    account_fingerprint: str,
    durable_command_key: str,
) -> str:
    if not isinstance(factory, ClientOrderIdFactory):
        raise ExchangeOrderIdentityError("factory does not implement ClientOrderIdFactory")
    try:
        return factory.create_client_order_id(
            environment=environment,
            venue=venue,
            account_fingerprint=account_fingerprint,
            durable_command_key=durable_command_key,
        )
    except ExchangeOrderIdentityError:
        raise
    except Exception as error:
        raise ExchangeOrderIdentityError("client order ID factory failed") from error
