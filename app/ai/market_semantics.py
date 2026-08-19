"""Frozen deterministic market-data semantics for research reasoning only."""

from __future__ import annotations

from dataclasses import dataclass

from app.ai.schemas import digest

MARKET_SEMANTICS_IDENTITY = "market_semantics/1.0.0"

SEMANTIC_RULES = {
    "open_interest": {
        "UP": "aggregate open positions or exposure increased; direction is unknown",
        "DOWN": "aggregate open positions or exposure decreased; direction is unknown",
    },
    "funding": {
        "POSITIVE": "longs pay shorts; positive long-side payment pressure, not participant identity",
        "NEGATIVE": "shorts pay longs; negative short-side payment pressure, not future direction",
    },
    "taker_pressure": {
        "BUY": "aggressive buy-side execution dominates the supplied measure",
        "SELL": "aggressive sell-side execution dominates the supplied measure",
    },
    "breadth": "available positive members divided by available defined members; missing is not negative",
    "relative_strength": "outperformance versus BTC is relative and does not imply positive absolute return",
    "high_beta_l1": ("ETHUSDT", "SOLUSDT", "BNBUSDT", "AVAXUSDT"),
    "forbidden_inferences": (
        "open interest direction implies bullish sentiment",
        "open interest direction implies bearish sentiment",
        "positive funding implies institutional demand",
        "funding guarantees future direction",
        "missing data has an unprovided technical cause",
    ),
}


def market_semantics_digest() -> str:
    return digest({"identity": MARKET_SEMANTICS_IDENTITY, "rules": SEMANTIC_RULES})


def semantics_context() -> dict[str, object]:
    """System knowledge, explicitly separate from supplied market facts."""
    return {
        "identity": MARKET_SEMANTICS_IDENTITY,
        "digest": market_semantics_digest(),
        "rules": SEMANTIC_RULES,
    }


def semantic_violation(statement: str) -> str | None:
    """Small deterministic guardrail, not a market or trading model."""
    normalized = statement.casefold()
    checks = (
        ("institutional demand", "participant identity is unsupported"),
        ("whales", "participant identity is unsupported"),
        ("technical issue", "missing-data cause is unsupported"),
        ("technical failure", "missing-data cause is unsupported"),
        ("bullish sentiment", "OI direction is not sentiment"),
        ("bearish sentiment", "OI direction is not sentiment"),
        ("btc is high beta", "BTC is not HIGH_BETA_L1"),
        ("short squeeze", "funding does not establish a future squeeze"),
        ("fomc caused", "event temporal coincidence is not causal proof"),
        ("cpi caused", "event temporal coincidence is not causal proof"),
        ("employment caused", "event temporal coincidence is not causal proof"),
    )
    for phrase, reason in checks:
        if phrase in normalized:
            return reason
    mentions_oi = (
        "open interest" in normalized or normalized.startswith("oi") or " oi " in normalized
    )
    if mentions_oi and ("bullish" in normalized or "bearish" in normalized):
        return "OI direction is not sentiment or directional proof"
    if "funding" in normalized and ("guarantees" in normalized or "guarantee" in normalized):
        return "funding does not guarantee future direction"
    if "relative strength" in normalized and "positive absolute" in normalized:
        return "relative strength does not imply positive absolute return"
    return None


@dataclass(frozen=True, slots=True)
class MarketSemanticsV1:
    identity: str = MARKET_SEMANTICS_IDENTITY
    digest: str = market_semantics_digest()
