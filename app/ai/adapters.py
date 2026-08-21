"""Adapters from existing research records into causal provider-neutral facts."""

from __future__ import annotations

from datetime import UTC, datetime

from app.ai.errors import AIValidationError
from app.ai.schemas import FactV1, digest
from app.research.intelligence.macro_events import HistoricalEventAIContextV1


def facts_from_historical_event_context(
    context: HistoricalEventAIContextV1,
) -> tuple[FactV1, ...]:
    cutoff = datetime.fromtimestamp(context.information_cutoff / 1000, tz=UTC)
    if context.market_snapshot.get("anchor_timestamp_ms") != context.information_cutoff:
        raise AIValidationError("historical context anchor must equal its information cutoff")
    facts: list[FactV1] = []
    for field, value in sorted(context.event_facts.items()):
        if field == "market_known_at_ms" and value is not None:
            timestamp = datetime.fromtimestamp(int(value) / 1000, tz=UTC)
        else:
            timestamp = cutoff
        facts.append(
            FactV1(
                f"event:{context.event_id}:{field}",
                "HISTORICAL_EVENT",
                timestamp,
                field,
                _scalar(value),
                None,
                context.context_digest,
            )
        )
    assets = context.market_snapshot.get("assets", {})
    if not isinstance(assets, dict):
        raise AIValidationError("historical market snapshot assets must be an object")
    for asset, fields in sorted(assets.items()):
        if not isinstance(fields, dict):
            raise AIValidationError("historical asset snapshot must be an object")
        for field, value in sorted(fields.items()):
            facts.append(
                FactV1(
                    f"market:{asset}:{field}:{context.information_cutoff}",
                    "RDE_CAUSAL_SNAPSHOT",
                    cutoff,
                    f"{asset}.{field}",
                    _scalar(value),
                    None,
                    context.context_digest,
                )
            )
    if len({fact.fact_id for fact in facts}) != len(facts):
        raise AIValidationError("historical adapter produced duplicate fact IDs")
    return tuple(facts)


def historical_context_metadata(context: HistoricalEventAIContextV1) -> dict[str, object]:
    """Return identifiers only; mutable future outcomes are structurally absent."""
    return {
        "historical_event_id": context.event_id,
        "historical_context_digest": context.context_digest,
        "adapter_digest": digest(
            {
                "event_id": context.event_id,
                "context_digest": context.context_digest,
                "information_cutoff": context.information_cutoff,
            }
        ),
    }


def _scalar(value: object) -> str | int | float | bool | None:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
