from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.research_json import iter_legacy_rows


def _write(path: Path, payload: str) -> None:
    path.write_text(payload, encoding="utf-8")


def test_streams_nested_objects_and_escaped_braces(tmp_path: Path) -> None:
    path = tmp_path / "legacy.json"
    _write(
        path,
        json.dumps(
            {
                "format": "fixture/v1",
                "rows": [
                    {"id": 1, "nested": {"text": '{quoted} \\"x\\"'}},
                    {"id": 2, "items": [1, {"a": True}]},
                ],
            }
        ),
    )
    assert list(iter_legacy_rows(path, expected_format="fixture/v1", chunk_size=128)) == [
        {"id": 1, "nested": {"text": '{quoted} \\"x\\"'}},
        {"id": 2, "items": [1, {"a": True}]},
    ]


@pytest.mark.parametrize(
    "payload", ['{"format":"fixture/v1","rows":[{"x":1}', '{"format":"other/v1","rows":[]}']
)
def test_streaming_reader_rejects_truncated_or_unexpected_formats(
    tmp_path: Path, payload: str
) -> None:
    path = tmp_path / "bad.json"
    _write(path, payload)
    with pytest.raises(ValueError):
        list(iter_legacy_rows(path, expected_format="fixture/v1"))
