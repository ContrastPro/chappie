"""Synthetic-fixture causal selection only; no historical alignment or feature values."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from .contracts import SourceRecordV1, StalenessPolicyV1


@dataclass(frozen=True, slots=True)
class AlignmentDecisionV1:
    available: bool
    reason: str | None
    age_seconds: int | None


def align_fixture_record(
    record: SourceRecordV1 | None,
    *,
    btc_close: datetime,
    policy: StalenessPolicyV1,
    regular_session_open: bool = True,
) -> AlignmentDecisionV1:
    if btc_close.tzinfo is None or btc_close.utcoffset() != UTC.utcoffset(btc_close):
        raise ValueError("btc_close must be UTC")
    if record is None:
        return AlignmentDecisionV1(False, "SOURCE_RECORD_MISSING", None)
    if not record.value_available:
        return AlignmentDecisionV1(False, record.unavailable_reason, None)
    if policy.regular_session_only and not regular_session_open:
        return AlignmentDecisionV1(False, "REGULAR_SESSION_CLOSED", None)
    if record.temporal.usable_at > btc_close:
        return AlignmentDecisionV1(False, "USABLE_AT_IN_FUTURE", None)
    age = btc_close - record.temporal.event_time
    if age > policy.maximum_age:
        return AlignmentDecisionV1(False, "STALE_SOURCE_RECORD", int(age.total_seconds()))
    return AlignmentDecisionV1(True, None, int(age.total_seconds()))
