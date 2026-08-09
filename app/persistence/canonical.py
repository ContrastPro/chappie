import hashlib
import json
from collections.abc import Mapping, Sequence
from decimal import Decimal
from enum import Enum

from app.persistence.exceptions import PersistenceSerializationError

CANONICAL_SCHEMA_VERSION = "canonical-json/v1"
MAX_PLAIN_DECIMAL_CHARACTERS = 4_096


def serialize_decimal(value: Decimal) -> str:
    """Return a context-independent canonical base-10 representation."""
    if not isinstance(value, Decimal) or not value.is_finite():
        raise PersistenceSerializationError("value must be a finite Decimal")
    if value.is_zero():
        return "0"
    sign, digits, exponent = value.as_tuple()
    digit_values = list(digits)
    while len(digit_values) > 1 and digit_values[-1] == 0:
        digit_values.pop()
        exponent += 1
    raw = "".join(str(digit) for digit in digit_values)
    adjusted = exponent + len(raw) - 1
    expanded_size = len(raw) + max(exponent, 0) + max(-adjusted, 0) + 2
    if expanded_size > MAX_PLAIN_DECIMAL_CHARACTERS:
        coefficient = raw[0] if len(raw) == 1 else f"{raw[0]}.{raw[1:]}"
        result = f"{coefficient}E{adjusted:+d}"
        return f"-{result}" if sign else result
    if exponent >= 0:
        result = raw + "0" * exponent
    else:
        split = len(raw) + exponent
        result = raw[:split] + "." + raw[split:] if split > 0 else "0." + "0" * -split + raw
        result = result.rstrip("0").rstrip(".")
    return f"-{result}" if sign else result


def deserialize_decimal(value: str) -> Decimal:
    if not isinstance(value, str) or not value:
        raise PersistenceSerializationError("canonical Decimal must be a non-empty string")
    try:
        result = Decimal(value)
    except Exception as error:
        raise PersistenceSerializationError("invalid canonical Decimal") from error
    if not result.is_finite() or serialize_decimal(result) != value:
        raise PersistenceSerializationError("Decimal text is not canonical")
    return result


def _normalize(value: object) -> object:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, Decimal):
        return {"$decimal": serialize_decimal(value)}
    if isinstance(value, Enum):
        return _normalize(value.value)
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise PersistenceSerializationError("canonical mapping keys must be strings")
            normalized[key] = _normalize(item)
        return normalized
    if isinstance(value, (tuple, list)):
        return [_normalize(item) for item in value]
    raise PersistenceSerializationError(f"unsupported canonical type: {type(value).__name__}")


def canonical_json(value: object) -> str:
    try:
        return json.dumps(
            _normalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError) as error:
        raise PersistenceSerializationError("canonical JSON serialization failed") from error


def canonical_sha256(value: object, *, algorithm_version: str) -> str:
    if not isinstance(algorithm_version, str) or not algorithm_version.strip():
        raise PersistenceSerializationError("algorithm_version must be a non-empty string")
    envelope = {"algorithm_version": algorithm_version, "payload": value}
    return hashlib.sha256(canonical_json(envelope).encode("utf-8")).hexdigest()


def canonical_sequence(values: Sequence[object]) -> tuple[object, ...]:
    if isinstance(values, (str, bytes)):
        raise PersistenceSerializationError("values must be a sequence of records")
    return tuple(values)
