import hmac
from collections.abc import Iterable
from decimal import Decimal
from hashlib import sha256
from urllib.parse import urlencode

from app.infrastructure.binance.exceptions import BinanceConfigurationError

ParameterValue = str | int | Decimal | bool


def _encode_value(value: ParameterValue) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise BinanceConfigurationError("request Decimal must be finite")
        return format(value, "f")
    if isinstance(value, str):
        return value
    raise BinanceConfigurationError("unsupported request parameter type")


def encode_parameters(parameters: Iterable[tuple[str, ParameterValue]]) -> str:
    encoded: list[tuple[str, str]] = []
    names: set[str] = set()
    for name, value in parameters:
        if not isinstance(name, str) or not name:
            raise BinanceConfigurationError("request parameter name must be non-empty")
        if name in names:
            raise BinanceConfigurationError(f"duplicate request parameter: {name}")
        names.add(name)
        encoded.append((name, _encode_value(value)))
    return urlencode(encoded)


def sign_hmac_sha256(payload: str, secret: str) -> str:
    if not isinstance(payload, str):
        raise BinanceConfigurationError("signature payload must be a string")
    if not isinstance(secret, str) or not secret:
        raise BinanceConfigurationError("API secret must be non-empty")
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), sha256).hexdigest()
