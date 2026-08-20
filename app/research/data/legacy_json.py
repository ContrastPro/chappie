"""Narrow, dependency-free streaming reader for legacy research JSON payloads."""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterator
from pathlib import Path


def iter_legacy_rows(
    path: Path, *, expected_format: str, chunk_size: int = 65_536
) -> Iterator[dict[str, object]]:
    """Yield objects from the exact top-level ``rows`` array without loading it.

    The legacy format has a small object prefix followed by ``"rows": [``.  A
    character-state scanner identifies complete object elements and delegates
    each object to the standard-library JSON decoder.  It is deliberately not
    a general JSON parser.
    """
    if chunk_size < 128:
        raise ValueError("chunk_size too small")
    opener = gzip.open if path.suffix == ".gz" else Path.open
    with opener(path, "rt", encoding="utf-8") as handle:  # type: ignore[arg-type]
        prefix = ""
        marker = '"rows"'
        while marker not in prefix:
            block = handle.read(chunk_size)
            if not block:
                raise ValueError("legacy payload has no rows collection")
            prefix += block
            if len(prefix) > 2 * chunk_size:
                prefix = prefix[-chunk_size:]
        before, remainder = prefix.split(marker, 1)
        if (
            f'"format": "{expected_format}"' not in before
            and f'"format":"{expected_format}"' not in before
        ):
            raise ValueError("unexpected legacy format")
        bracket = remainder.find("[")
        if bracket < 0:
            raise ValueError("rows collection is malformed")
        buffer = remainder[bracket + 1 :]
        decoder = json.JSONDecoder()
        while True:
            stripped = buffer.lstrip()
            if stripped.startswith(","):
                buffer = stripped[1:]
                continue
            if stripped.startswith("]"):
                suffix = stripped[1:] + handle.read()
                if not suffix.strip().endswith("}"):
                    raise ValueError("invalid content after legacy rows")
                return
            try:
                value, end = decoder.raw_decode(stripped)
            except json.JSONDecodeError:
                block = handle.read(chunk_size)
                if not block:
                    raise ValueError("truncated or malformed legacy rows") from None
                buffer = stripped + block
                continue
            if not isinstance(value, dict):
                raise TypeError("legacy row must be an object")
            yield value
            buffer = stripped[end:]
