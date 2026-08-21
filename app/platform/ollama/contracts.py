"""Neutral contracts shared by Chappie's Ollama platform adapters."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

MODEL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*(?::[a-z0-9][a-z0-9._-]*)?$")


class ExternalActionStatusV1(StrEnum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    MANUAL_FOLLOWUP = "MANUAL_FOLLOWUP"


@dataclass(frozen=True, slots=True)
class ExternalActionResultV1:
    action: str
    status: ExternalActionStatusV1
    platform: str
    source: str
    version: str | None = None
    duration_ms: int = 0
    requires_restart: bool = False
    requires_relogin: bool = False
    requires_manual_followup: bool = False
    error_code: str | None = None
