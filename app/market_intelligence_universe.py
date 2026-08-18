"""Research-only, coverage-aware multi-asset market intelligence universe."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from itertools import pairwise
from statistics import median

FIVE_MINUTES_MS = 300_000
UNIVERSE_NAME = "market_intelligence_universe_v1"
UNIVERSE_VERSION = "1.0.0"


@dataclass(frozen=True, slots=True)
class UniverseMember:
    asset: str
    symbol: str
    group: str
    role: str
    derivatives_expected: bool


MEMBERS = (
    UniverseMember("BTC", "BTCUSDT", "CORE", "PRIMARY_BENCHMARK", True),
    UniverseMember("ETH", "ETHUSDT", "CORE", "SECONDARY_BENCHMARK", True),
    UniverseMember("SOL", "SOLUSDT", "HIGH_BETA_L1", "HIGH_BETA", True),
    UniverseMember("BNB", "BNBUSDT", "HIGH_BETA_L1", "HIGH_BETA", True),
    UniverseMember("AVAX", "AVAXUSDT", "HIGH_BETA_L1", "HIGH_BETA", True),
    UniverseMember("XRP", "XRPUSDT", "OTHER_MAJOR", "OTHER_MAJOR", True),
    UniverseMember("LINK", "LINKUSDT", "OTHER_MAJOR", "OTHER_MAJOR", True),
    UniverseMember("DOGE", "DOGEUSDT", "SPECULATIVE", "SPECULATIVE", True),
)
ASSETS = tuple(member.asset for member in MEMBERS)
GROUPS = {member.asset: member.group for member in MEMBERS}


def universe_payload() -> dict[str, object]:
    return {
        "name": UNIVERSE_NAME,
        "version": UNIVERSE_VERSION,
        "primary_benchmark": "BTC",
        "secondary_benchmark": "ETH",
        "relative_strength": "exact-synchronized-usdt-close-ratio; no-asof-or-forward-fill",
        "members": [
            member.__dict__
            if hasattr(member, "__dict__")
            else {
                field: getattr(member, field)
                for field in ("asset", "symbol", "group", "role", "derivatives_expected")
            }
            for member in MEMBERS
        ],
    }


def universe_digest(payload: dict[str, object] | None = None) -> str:
    return hashlib.sha256(
        json.dumps(payload or universe_payload(), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class PricePoint:
    timestamp_ms: int
    close: float
    segment_id: int
    quote_volume: float | None = None


@dataclass(frozen=True, slots=True)
class UniverseObservation:
    timestamp_ms: int
    available_assets: tuple[str, ...]
    returns_1h_bps: dict[str, float]
    returns_6h_bps: dict[str, float]
    returns_24h_bps: dict[str, float]
    relative_1h_bps_btc: dict[str, float]
    relative_6h_bps_btc: dict[str, float]
    relative_6h_bps_eth: dict[str, float]
    ratios_btc: dict[str, float]
    ratios_eth: dict[str, float]
    breadth_positive_1h: int
    breadth_positive_6h: int
    breadth_outperforming_btc_6h: int
    breadth_denominator_1h: int
    breadth_denominator_6h: int
    group_median_6h_bps: dict[str, float]
    leadership: str
    rotation_state: str


def _return(now: PricePoint, before: PricePoint) -> float | None:
    if now.segment_id != before.segment_id or now.timestamp_ms - before.timestamp_ms <= 0:
        return None
    return (now.close / before.close - 1) * 10_000


def _indexes(series: dict[str, tuple[PricePoint, ...]]) -> dict[str, dict[int, PricePoint]]:
    if set(series) - set(ASSETS):
        raise ValueError("unknown universe asset")
    output = {}
    for asset, points in series.items():
        previous = None
        index = {}
        for point in points:
            if point.close <= 0 or (previous is not None and point.timestamp_ms <= previous):
                raise ValueError("price points must be strictly ordered and positive")
            index[point.timestamp_ms] = point
            previous = point.timestamp_ms
        output[asset] = index
    return output


def _at_return(index: dict[int, PricePoint], timestamp: int, bars: int) -> float | None:
    now, before = index.get(timestamp), index.get(timestamp - bars * FIVE_MINUTES_MS)
    return _return(now, before) if now is not None and before is not None else None


def _state(returns: dict[str, float], relative: dict[str, float], denominator: int) -> str:
    if not denominator:
        return "MIXED"
    if sum(value > 0 for value in returns.values()) == denominator and denominator >= 2:
        return "BROAD_RISK_ON"
    if sum(value < 0 for value in returns.values()) == denominator and denominator >= 2:
        return "BROAD_RISK_OFF"
    if all(value <= 0 for asset, value in relative.items() if asset != "BTC"):
        return "BTC_DOMINANT"
    if relative.get("ETH", 0) > 0 and relative["ETH"] == max(relative.values()):
        return "ETH_ROTATION"
    if any(relative.get(asset, 0) > 0 for asset in ("SOL", "BNB", "AVAX")):
        return "HIGH_BETA_ROTATION"
    if relative.get("DOGE", 0) > 0:
        return "SPECULATIVE_ROTATION"
    return "MIXED"


def build_universe_observations(
    series: dict[str, tuple[PricePoint, ...]],
) -> tuple[UniverseObservation, ...]:
    """BTC-led exact-timestamp observations; absent secondaries stay absent."""
    indexes = _indexes(series)
    if "BTC" not in indexes:
        raise ValueError("BTC is the required primary timeline")
    result = []
    for timestamp, btc in sorted(indexes["BTC"].items()):
        r1 = {
            asset: value
            for asset, index in indexes.items()
            if (value := _at_return(index, timestamp, 12)) is not None
        }
        r6 = {
            asset: value
            for asset, index in indexes.items()
            if (value := _at_return(index, timestamp, 72)) is not None
        }
        r24 = {
            asset: value
            for asset, index in indexes.items()
            if (value := _at_return(index, timestamp, 288)) is not None
        }
        available = tuple(sorted(asset for asset, index in indexes.items() if timestamp in index))
        if "BTC" not in r6:
            continue
        btc_close = btc.close
        eth_close = (
            indexes["ETH"].get(timestamp).close
            if "ETH" in indexes and timestamp in indexes["ETH"]
            else None
        )
        ratios_btc = {
            asset: index[timestamp].close / btc_close
            for asset, index in indexes.items()
            if timestamp in index
        }
        ratios_eth = {
            asset: index[timestamp].close / eth_close
            for asset, index in indexes.items()
            if eth_close is not None and timestamp in index
        }
        rel_btc = {asset: value - r6["BTC"] for asset, value in r6.items()}
        rel_eth = {asset: value - r6["ETH"] for asset, value in r6.items() if "ETH" in r6}
        group = {
            name: median([r6[a] for a in ASSETS if GROUPS[a] == name and a in r6])
            for name in sorted(set(GROUPS.values()))
            if any(a in r6 for a in ASSETS if GROUPS[a] == name)
        }
        state = _state(r6, rel_btc, len(r6))
        result.append(
            UniverseObservation(
                timestamp,
                available,
                r1,
                r6,
                r24,
                {a: v - r1["BTC"] for a, v in r1.items() if "BTC" in r1},
                rel_btc,
                rel_eth,
                ratios_btc,
                ratios_eth,
                sum(v > 0 for v in r1.values()),
                sum(v > 0 for v in r6.values()),
                sum(v > 0 for a, v in rel_btc.items() if a != "BTC"),
                len(r1),
                len(r6),
                group,
                state,
                state,
            )
        )
    return tuple(result)


def state_frequency(observations: tuple[UniverseObservation, ...]) -> dict[str, int]:
    return dict(Counter(item.rotation_state for item in observations))


def transition_matrix(observations: tuple[UniverseObservation, ...]) -> dict[str, dict[str, int]]:
    """One-step descriptive state transitions; gaps are already excluded upstream."""
    matrix: dict[str, Counter[str]] = {}
    for current, following in pairwise(observations):
        if following.timestamp_ms - current.timestamp_ms == FIVE_MINUTES_MS:
            matrix.setdefault(current.rotation_state, Counter())[following.rotation_state] += 1
    return {state: dict(counts) for state, counts in matrix.items()}
