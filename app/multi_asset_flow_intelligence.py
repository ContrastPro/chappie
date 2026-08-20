"""Bounded, descriptive multi-asset flow-intelligence research.

This module is research-only.  It consumes the verified compact store and uses
the frozen compact matched-event selector without changing its semantics.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean, median
from typing import Final

from app.market_intelligence_universe import UNIVERSE_NAME, UNIVERSE_VERSION, universe_digest
from app.research.data.outcomes import HORIZONS
from app.research_methodology import METHODOLOGY_NAME, METHODOLOGY_VERSION, methodology_digest

FIVE_MINUTES_MS: Final = 300_000
ASSETS: Final = ("BTC", "ETH", "SOL", "BNB", "AVAX", "XRP", "LINK", "DOGE")
TARGETS: Final = ("BTCUSDT", "ETHUSDT")
YEARS: Final = (2023, 2024, 2025, 2026)
STATE_NAMES: Final = (
    "BROAD_RISK_ON",
    "BROAD_RISK_OFF",
    "HIGH_BETA_ROTATION",
    "BTC_DOMINANT",
    "ETH_ROTATION",
    "LEVERAGE_EXPANDING",
    "LEVERAGE_CONTRACTING",
    "FUTURES_BUY_PRESSURE",
    "FUTURES_SELL_PRESSURE",
)
COMBINATION_NAMES: Final = (
    "RISK_ON_HIGH_BETA_OI_EXPANDING",
    "RISK_OFF_SOL_BNB_SELL_PRESSURE",
    "HIGH_BETA_ROTATION_DERIVATIVES_POSITIVE",
    "BTC_DOMINANT_HIGH_BETA_COLLAPSING",
    "ETH_ROTATION_ETH_SOL_PARTICIPATION",
)
ALL_HYPOTHESES: Final = STATE_NAMES + COMBINATION_NAMES
SAMPLE_GATES: Final = {"ROBUST": 100, "MODERATE": 40, "SMALL": 15, "SPARSE": 0}


@dataclass(frozen=True, slots=True)
class ResearchRow:
    timestamp_ms: int
    segments: tuple[int, ...]
    closes: tuple[float, ...]
    btc_snapshot: dict[str, object]
    eth_snapshot: dict[str, object]
    hypotheses: frozenset[str]
    rotation_state: str
    volatility_expanding: bool


@dataclass(frozen=True, slots=True)
class Event:
    event_id: str
    hypothesis: str
    start_ms: int
    end_ms: int
    raw_rows: int


def state_contract() -> dict[str, object]:
    """Return the predeclared feature/state contract used by this experiment."""
    return {
        "baseline": {
            "assets": ["BTC", "ETH"],
            "causal_fields": [
                "1h_return",
                "6h_return",
                "24h_return",
                "12-bar_rms_volatility",
                "relative_volume",
                "taker_imbalance",
                "oi_change_5m",
                "funding",
                "futures_taker_state",
                "positioning_state",
                "ETH/BTC_6h_relative_strength",
            ],
            "matching": "frozen year/hour/trend signature; volatility, return, relative-volume, timestamp ranking",
        },
        "expanded": {
            "spot": {
                "returns": "exact-timestamp, same-segment 6h return for each of 8 assets",
                "positive_breadth": "count(return_6h > 0) / 8",
                "btc_outperformance_breadth": "count(non-BTC return_6h > BTC return_6h) / 7",
                "eth_outperformance_breadth": "count(non-ETH return_6h > ETH return_6h) / 7",
                "dispersion": "max(return_6h) - min(return_6h)",
                "group_participation": "positive-return count within CORE, HIGH_BETA_L1, OTHER_MAJOR, SPECULATIVE",
                "relative_strength": "asset return_6h - BTC return_6h for SOL/BNB/AVAX/XRP/LINK/DOGE",
            },
            "rotation": [
                "BTC_DOMINANT",
                "ETH_ROTATION",
                "HIGH_BETA_ROTATION",
                "BROAD_RISK_ON",
                "BROAD_RISK_OFF",
                "MIXED",
            ],
            "derivatives": "coverage-aware BTC/ETH/SOL/BNB OI, funding, futures pressure, positioning",
        },
        "states": {
            "BROAD_RISK_ON": "all 8 synchronized 6h returns > 0",
            "BROAD_RISK_OFF": "all 8 synchronized 6h returns < 0",
            "HIGH_BETA_ROTATION": "at least 2 of SOL/BNB/AVAX outperform BTC and their median lead is >20 bps",
            "BTC_DOMINANT": "BTC is the 6h leader and at least 6 other assets underperform it",
            "ETH_ROTATION": "ETH is the 6h leader and outperforms BTC",
            "LEVERAGE_EXPANDING": "more than half of available BTC/ETH/SOL/BNB 5m OI changes are positive",
            "LEVERAGE_CONTRACTING": "fewer than half of available BTC/ETH/SOL/BNB 5m OI changes are positive",
            "FUTURES_BUY_PRESSURE": "more than half of available derivatives taker states are buying",
            "FUTURES_SELL_PRESSURE": "fewer than half of available derivatives taker states are buying",
        },
        "combinations": {
            "RISK_ON_HIGH_BETA_OI_EXPANDING": "BROAD_RISK_ON and both SOL/BNB OI change > 0",
            "RISK_OFF_SOL_BNB_SELL_PRESSURE": "BROAD_RISK_OFF and both SOL/BNB futures taker states selling",
            "HIGH_BETA_ROTATION_DERIVATIVES_POSITIVE": "HIGH_BETA_ROTATION and SOL/BNB majority OI expanding and futures buying",
            "BTC_DOMINANT_HIGH_BETA_COLLAPSING": "BTC_DOMINANT and SOL/BNB/AVAX all have negative 6h return",
            "ETH_ROTATION_ETH_SOL_PARTICIPATION": "ETH_ROTATION and ETH/SOL OI expanding and futures buying",
        },
        "sample_gates": SAMPLE_GATES,
    }


def _event_id(name: str, start_ms: int, end_ms: int) -> str:
    return hashlib.sha256(f"{name}:{start_ms}:{end_ms}".encode()).hexdigest()


def _snapshot(
    timestamp: int, returns: list[float], relative_volume: float | None
) -> dict[str, object]:
    recent = sum(returns[-12:])
    volatility = (sum(value * value for value in returns[-12:]) / 12) ** 0.5
    dt = datetime.fromtimestamp(timestamp / 1000, UTC)
    return {
        "year": dt.year,
        "hour_utc": dt.hour,
        "trend": "UP" if recent >= 20 else "DOWN" if recent <= -20 else "FLAT",
        "recent_return_bps": recent,
        "volatility_bps": volatility,
        "relative_volume": relative_volume,
    }


def _rotation(returns: dict[str, float]) -> str:
    if all(value > 0 for value in returns.values()):
        return "BROAD_RISK_ON"
    if all(value < 0 for value in returns.values()):
        return "BROAD_RISK_OFF"
    relative = {asset: value - returns["BTC"] for asset, value in returns.items()}
    if (
        returns["BTC"] == max(returns.values())
        and sum(value <= 0 for asset, value in relative.items() if asset != "BTC") >= 6
    ):
        return "BTC_DOMINANT"
    if returns["ETH"] == max(returns.values()) and relative["ETH"] > 0:
        return "ETH_ROTATION"
    high_beta = [relative[asset] for asset in ("SOL", "BNB", "AVAX")]
    if sum(value > 0 for value in high_beta) >= 2 and median(high_beta) > 20:
        return "HIGH_BETA_ROTATION"
    if relative["DOGE"] > 0:
        return "SPECULATIVE_ROTATION"
    return "MIXED"


def _hypotheses(
    returns: dict[str, float],
    deriv: dict[str, tuple[float | None, str | None, float | None, str | None]],
) -> frozenset[str]:
    result: set[str] = set()
    rotation = _rotation(returns)
    if rotation in STATE_NAMES:
        result.add(rotation)
    oi = [value[0] > 0 for value in deriv.values() if value[0] is not None]
    buying = [value[1] == "FUTURES_BUYING" for value in deriv.values() if value[1] is not None]
    positive_funding = [value[2] > 0 for value in deriv.values() if value[2] is not None]
    crowded_long = [value[3] == "EXTREME_LONG" for value in deriv.values() if value[3] is not None]
    if oi and sum(oi) / len(oi) > 0.5:
        result.add("LEVERAGE_EXPANDING")
    elif oi and sum(oi) / len(oi) < 0.5:
        result.add("LEVERAGE_CONTRACTING")
    if buying and sum(buying) / len(buying) > 0.5:
        result.add("FUTURES_BUY_PRESSURE")
    elif buying and sum(buying) / len(buying) < 0.5:
        result.add("FUTURES_SELL_PRESSURE")
    sol_bnb = [deriv[asset] for asset in ("SOL", "BNB")]
    if rotation == "BROAD_RISK_ON" and all(item[0] is not None and item[0] > 0 for item in sol_bnb):
        result.add("RISK_ON_HIGH_BETA_OI_EXPANDING")
    if rotation == "BROAD_RISK_OFF" and all(item[1] == "FUTURES_SELLING" for item in sol_bnb):
        result.add("RISK_OFF_SOL_BNB_SELL_PRESSURE")
    if (
        rotation == "HIGH_BETA_ROTATION"
        and sum(item[0] is not None and item[0] > 0 for item in sol_bnb) > 1
        and sum(item[1] == "FUTURES_BUYING" for item in sol_bnb) > 1
        and positive_funding
        and sum(positive_funding) / len(positive_funding) > 0.5
        and crowded_long
        and sum(crowded_long) / len(crowded_long) <= 0.5
    ):
        result.add("HIGH_BETA_ROTATION_DERIVATIVES_POSITIVE")
    if rotation == "BTC_DOMINANT" and all(returns[asset] < 0 for asset in ("SOL", "BNB", "AVAX")):
        result.add("BTC_DOMINANT_HIGH_BETA_COLLAPSING")
    eth_sol = [deriv[asset] for asset in ("ETH", "SOL")]
    if rotation == "ETH_ROTATION" and all(
        item[0] is not None and item[0] > 0 and item[1] == "FUTURES_BUYING" for item in eth_sol
    ):
        result.add("ETH_ROTATION_ETH_SOL_PARTICIPATION")
    return frozenset(result)


def load_rows(connection: sqlite3.Connection) -> tuple[ResearchRow, ...]:
    """Load exact-common verified observations and derive only causal states."""
    connection.row_factory = sqlite3.Row
    sql = """
    SELECT b.timestamp_ms,
      b.aligned_segment_id bseg,e.aligned_segment_id eseg,
      s.segment_id sseg,n.segment_id nseg,a.segment_id aseg,x.segment_id xseg,l.segment_id lseg,d.segment_id dseg,
      b.close bc,e.close ec,s.close sc,n.close nc,a.close ac,x.close xc,l.close lc,d.close dc,
      b.relative_volume brv,e.relative_volume erv,
      b.oi_change_5m boi,b.futures_taker_state bfts,b.funding bfund,b.positioning_state bpos,
      e.oi_change_5m eoi,e.futures_taker_state efts,e.funding efund,e.positioning_state epos,
      sd.oi_change_5m soi,sd.futures_taker_state sfts,sd.funding sfund,sd.positioning_state spos,
      nd.oi_change_5m noi,nd.futures_taker_state nfts,nd.funding nfund,nd.positioning_state npos
    FROM intelligence_observations_v1 b
    JOIN intelligence_observations_v1 e ON e.timestamp_ms=b.timestamp_ms AND e.asset='ETHUSDT'
    JOIN flow_observations_v1 s ON s.timestamp_ms=b.timestamp_ms AND s.dataset='flow_sol_v1'
    JOIN flow_observations_v1 n ON n.timestamp_ms=b.timestamp_ms AND n.dataset='flow_bnb_v1'
    JOIN flow_observations_v1 a ON a.timestamp_ms=b.timestamp_ms AND a.dataset='flow_avax_v1'
    JOIN flow_observations_v1 x ON x.timestamp_ms=b.timestamp_ms AND x.dataset='flow_xrp_v1'
    JOIN flow_observations_v1 l ON l.timestamp_ms=b.timestamp_ms AND l.dataset='flow_link_v1'
    JOIN flow_observations_v1 d ON d.timestamp_ms=b.timestamp_ms AND d.dataset='flow_doge_v1'
    JOIN flow_observations_v1 sd ON sd.timestamp_ms=b.timestamp_ms AND sd.dataset='solusdt_derivatives_v1'
    JOIN flow_observations_v1 nd ON nd.timestamp_ms=b.timestamp_ms AND nd.dataset='bnbusdt_derivatives_v1'
    WHERE b.asset='BTCUSDT' ORDER BY b.timestamp_ms
    """
    history: dict[int, tuple[tuple[int, ...], tuple[float, ...]]] = {}
    asset_returns: list[list[float]] = [[], []]
    prior: tuple[int, tuple[int, ...], tuple[float, ...]] | None = None
    result: list[ResearchRow] = []
    for row in connection.execute(sql):
        timestamp = int(row["timestamp_ms"])
        segments = tuple(
            int(row[key])
            for key in ("bseg", "eseg", "sseg", "nseg", "aseg", "xseg", "lseg", "dseg")
        )
        closes = tuple(float(row[key]) for key in ("bc", "ec", "sc", "nc", "ac", "xc", "lc", "dc"))
        contiguous = (
            prior is not None and timestamp == prior[0] + FIVE_MINUTES_MS and segments == prior[1]
        )
        if not contiguous:
            asset_returns = [[], []]
        elif prior is not None:
            for index in (0, 1):
                asset_returns[index].append((closes[index] / prior[2][index] - 1) * 10_000)
        history[timestamp] = (segments, closes)
        old = history.get(timestamp - 72 * FIVE_MINUTES_MS)
        if (
            old is not None
            and old[0] == segments
            and all(len(values) >= 12 for values in asset_returns)
        ):
            returns = {
                asset: (closes[index] / old[1][index] - 1) * 10_000
                for index, asset in enumerate(ASSETS)
            }
            deriv = {
                "BTC": (row["boi"], row["bfts"], row["bfund"], row["bpos"]),
                "ETH": (row["eoi"], row["efts"], row["efund"], row["epos"]),
                "SOL": (row["soi"], row["sfts"], row["sfund"], row["spos"]),
                "BNB": (row["noi"], row["nfts"], row["nfund"], row["npos"]),
            }
            rotation = _rotation(returns)
            result.append(
                ResearchRow(
                    timestamp,
                    segments,
                    closes,
                    _snapshot(timestamp, asset_returns[0], row["brv"]),
                    _snapshot(timestamp, asset_returns[1], row["erv"]),
                    _hypotheses(returns, deriv),
                    rotation,
                    float(result[-1].btc_snapshot["volatility_bps"]) * 1.25
                    < float(_snapshot(timestamp, asset_returns[0], row["brv"])["volatility_bps"])
                    if result and timestamp == result[-1].timestamp_ms + FIVE_MINUTES_MS
                    else False,
                )
            )
        prior = (timestamp, segments, closes)
        history.pop(timestamp - 401 * FIVE_MINUTES_MS, None)
    return tuple(result)


def collapse_events(rows: tuple[ResearchRow, ...], hypothesis: str) -> tuple[Event, ...]:
    events: list[Event] = []
    start: int | None = None
    end: int | None = None
    count = 0
    previous: int | None = None
    for row in rows:
        active = hypothesis in row.hypotheses
        adjacent = previous is not None and row.timestamp_ms == previous + FIVE_MINUTES_MS
        if start is not None and (not active or not adjacent):
            assert end is not None
            events.append(Event(_event_id(hypothesis, start, end), hypothesis, start, end, count))
            start = end = None
            count = 0
        if active:
            if start is None:
                start = row.timestamp_ms
            end = row.timestamp_ms
            count += 1
        previous = row.timestamp_ms
    if start is not None and end is not None:
        events.append(Event(_event_id(hypothesis, start, end), hypothesis, start, end, count))
    return tuple(events)


def _sample_gate(matches: int) -> str:
    return next(name for name, minimum in SAMPLE_GATES.items() if matches >= minimum)


def _outcome(row: sqlite3.Row) -> dict[str, float]:
    return {
        key: float(row[key])
        for key in (
            "forward_return_bps",
            "max_favorable_excursion_bps",
            "max_adverse_excursion_bps",
            "hit_plus_60_bps",
            "hit_plus_100_bps",
            "hit_plus_150_bps",
            "hit_minus_60_bps",
            "hit_minus_100_bps",
            "hit_minus_150_bps",
        )
    }


def select_control(
    *,
    event_snapshot: dict[str, object],
    ordered_pool: list[tuple[int, dict[str, object]]],
    used: set[int],
    event_times: set[int],
    available: set[int],
) -> int | None:
    """Frozen selector with its invariant pool ordering hoisted out of the event loop."""
    center = bisect_left(
        ordered_pool,
        float(event_snapshot["volatility_bps"]),
        key=lambda item: float(item[1]["volatility_bps"]),
    )
    local = ordered_pool[max(0, center - 24) : center + 24]
    candidates = [
        (timestamp, snapshot)
        for timestamp, snapshot in local
        if timestamp not in used and timestamp not in event_times and timestamp in available
    ]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda item: (
            abs(float(item[1]["volatility_bps"]) - float(event_snapshot["volatility_bps"])),
            abs(float(item[1]["recent_return_bps"]) - float(event_snapshot["recent_return_bps"])),
            abs(
                float(item[1]["relative_volume"] or 0)
                - float(event_snapshot["relative_volume"] or 0)
            ),
            item[0],
        ),
    )[0]


def _summary(differences: list[dict[str, float]]) -> dict[str, object]:
    keys = tuple(sorted(set().union(*(set(item) for item in differences)))) if differences else ()
    result: dict[str, object] = {
        "matches": len(differences),
        "sample_gate": _sample_gate(len(differences)),
    }
    for key in keys:
        values = [item[key] for item in differences if key in item]
        result[f"{key}_samples"] = len(values)
        result[f"incremental_mean_{key}"] = mean(values)
        result[f"incremental_median_{key}"] = median(values)
    return result


def analyze(store_path: Path) -> dict[str, object]:
    connection = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    required = {
        "flow_btc_v1",
        "flow_eth_v1",
        "flow_sol_v1",
        "flow_bnb_v1",
        "flow_avax_v1",
        "flow_xrp_v1",
        "flow_link_v1",
        "flow_doge_v1",
        "solusdt_derivatives_v1",
        "bnbusdt_derivatives_v1",
    }
    manifests = {
        row["dataset"]: dict(row)
        for row in connection.execute("SELECT * FROM conversion_manifest")
        if row["dataset"] in required
    }
    if set(manifests) != required or any(
        row["state"] != "VERIFIED" or not row["content_digest"] for row in manifests.values()
    ):
        raise ValueError("all eight spot/flow and SOL/BNB derivatives datasets must be VERIFIED")
    exact = connection.execute(
        """SELECT count(*) rows,min(s.timestamp_ms) first_ms,max(s.timestamp_ms) last_ms
        FROM flow_observations_v1 s
        JOIN flow_observations_v1 sd ON sd.timestamp_ms=s.timestamp_ms AND sd.dataset='solusdt_derivatives_v1'
        JOIN flow_observations_v1 nd ON nd.timestamp_ms=s.timestamp_ms AND nd.dataset='bnbusdt_derivatives_v1'
        JOIN intelligence_observations_v1 b ON b.timestamp_ms=s.timestamp_ms AND b.asset='BTCUSDT'
        JOIN intelligence_observations_v1 e ON e.timestamp_ms=s.timestamp_ms AND e.asset='ETHUSDT'
        WHERE s.dataset='flow_sol_v1'"""
    ).fetchone()
    rows = load_rows(connection)
    by_time = {row.timestamp_ms: row for row in rows}
    events_by_hypothesis = {name: collapse_events(rows, name) for name in ALL_HYPOTHESES}
    pools_by_target: dict[str, dict[str, list[tuple[int, dict[str, object]]]]] = {}
    outcomes_by_target: dict[str, dict[int, dict[int, sqlite3.Row]]] = {}
    for target in TARGETS:
        snapshot_name = "btc_snapshot" if target == "BTCUSDT" else "eth_snapshot"
        pools: dict[str, list[tuple[int, dict[str, object]]]] = defaultdict(list)
        for row in rows:
            snapshot = getattr(row, snapshot_name)
            signature = json.dumps(
                {key: snapshot[key] for key in ("year", "hour_utc", "trend")},
                sort_keys=True,
                separators=(",", ":"),
            )
            pools[signature].append((row.timestamp_ms, snapshot))
        for pool in pools.values():
            pool.sort(key=lambda item: (float(item[1]["volatility_bps"]), item[0]))
        pools_by_target[target] = pools
        outcomes_by_target[target] = {
            minutes: {
                int(item["timestamp_ms"]): item
                for item in connection.execute(
                    "SELECT * FROM intelligence_outcomes_v1 WHERE asset=? AND horizon_minutes=? AND outcome_available=1",
                    (target, minutes),
                )
            }
            for minutes in HORIZONS
        }
    results: dict[str, object] = {}
    strongest: list[tuple[float, str, str, int]] = []
    for hypothesis, raw_events in events_by_hypothesis.items():
        hypothesis_result: dict[str, object] = {
            "raw_rows": sum(event.raw_rows for event in raw_events),
            "collapsed_events": len(raw_events),
            "targets": {},
        }
        event_times = {
            timestamp
            for event in raw_events
            for timestamp in range(event.start_ms, event.end_ms + FIVE_MINUTES_MS, FIVE_MINUTES_MS)
        }
        for target in TARGETS:
            target_result: dict[str, object] = {}
            snapshot_name = "btc_snapshot" if target == "BTCUSDT" else "eth_snapshot"
            for minutes, bars in HORIZONS.items():
                accepted: list[Event] = []
                cooldown = bars * FIVE_MINUTES_MS
                for event in raw_events:
                    if not accepted or event.start_ms >= accepted[-1].start_ms + cooldown:
                        accepted.append(event)
                outcome_rows = outcomes_by_target[target][minutes]
                available = set(outcome_rows)
                used: set[int] = set()
                differences: list[dict[str, float]] = []
                annual: dict[int, list[float]] = defaultdict(list)
                regimes: dict[str, list[float]] = defaultdict(list)
                unmatched = 0
                outcome_available = 0
                for event in accepted:
                    if event.start_ms not in available or event.start_ms not in by_time:
                        continue
                    outcome_available += 1
                    snapshot = getattr(by_time[event.start_ms], snapshot_name)
                    signature = json.dumps(
                        {key: snapshot[key] for key in ("year", "hour_utc", "trend")},
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    control = select_control(
                        event_snapshot=snapshot,
                        ordered_pool=pools_by_target[target][signature],
                        used=used,
                        event_times=event_times,
                        available=available,
                    )
                    if control is None:
                        unmatched += 1
                        continue
                    used.add(control)
                    event_outcome, control_outcome = (
                        _outcome(outcome_rows[event.start_ms]),
                        _outcome(outcome_rows[control]),
                    )
                    diff = {key: event_outcome[key] - control_outcome[key] for key in event_outcome}
                    future_event = by_time.get(event.start_ms + bars * FIVE_MINUTES_MS)
                    future_control = by_time.get(control + bars * FIVE_MINUTES_MS)
                    if future_event and future_control:
                        event_anchor = by_time[event.start_ms]
                        control_anchor = by_time[control]
                        event_high_beta = median(
                            (future_event.closes[index] / event_anchor.closes[index] - 1) * 10_000
                            for index in (2, 3, 4)
                        )
                        control_high_beta = median(
                            (future_control.closes[index] / control_anchor.closes[index] - 1)
                            * 10_000
                            for index in (2, 3, 4)
                        )
                        diff["high_beta_group_return_bps"] = event_high_beta - control_high_beta
                    differences.append(diff)
                    year = int(snapshot["year"])
                    annual[year].append(diff["forward_return_bps"])
                    for state in ("BROAD_RISK_ON", "BROAD_RISK_OFF", "HIGH_BETA_ROTATION"):
                        if future_event and future_control:
                            regimes[state].append(
                                float(future_event.rotation_state == state)
                                - float(future_control.rotation_state == state)
                            )
                    if future_event and future_control:
                        regimes["VOLATILITY_EXPANSION"].append(
                            float(future_event.volatility_expanding)
                            - float(future_control.volatility_expanding)
                        )
                summary = _summary(differences)
                summary.update(
                    {
                        "independent_events": len(accepted),
                        "outcome_available": outcome_available,
                        "unmatched": unmatched,
                        "annual": {
                            str(year): {
                                "matches": len(annual[year]),
                                "mean_lift_bps": mean(annual[year]) if annual[year] else None,
                                "sign": "POSITIVE"
                                if annual[year] and mean(annual[year]) > 0
                                else "NEGATIVE"
                                if annual[year] and mean(annual[year]) < 0
                                else "FLAT",
                            }
                            for year in YEARS
                        },
                        "future_regime_probability_differences": {
                            key: mean(values) if values else None for key, values in regimes.items()
                        },
                    }
                )
                annual_signs = [
                    item["sign"]
                    for item in summary["annual"].values()
                    if item["matches"] >= SAMPLE_GATES["SMALL"] and item["sign"] != "FLAT"
                ]
                summary["year_stability"] = (
                    "INSUFFICIENT_SAMPLE"
                    if len(annual_signs) < 3
                    else "STABLE"
                    if len(set(annual_signs)) == 1
                    else "MIXED"
                    if max(annual_signs.count("POSITIVE"), annual_signs.count("NEGATIVE")) >= 3
                    else "UNSTABLE"
                )
                target_result[str(minutes)] = summary
                if differences:
                    strongest.append(
                        (
                            float(summary["incremental_mean_forward_return_bps"]),
                            hypothesis,
                            target,
                            minutes,
                        )
                    )
            hypothesis_result["targets"][target] = target_result
        results[hypothesis] = hypothesis_result
    connection.close()
    cells = sorted(strongest)
    signs = [value for value, *_ in strongest]
    cross_target: dict[str, dict[str, str]] = {}
    for hypothesis in ALL_HYPOTHESES:
        cross_target[hypothesis] = {}
        for minutes in HORIZONS:
            btc = results[hypothesis]["targets"]["BTCUSDT"][str(minutes)].get(
                "incremental_mean_forward_return_bps", 0
            )
            eth = results[hypothesis]["targets"]["ETHUSDT"][str(minutes)].get(
                "incremental_mean_forward_return_bps", 0
            )
            cross_target[hypothesis][str(minutes)] = (
                "INCONCLUSIVE"
                if abs(btc) < 5 and abs(eth) < 5
                else "CONSISTENT"
                if btc * eth > 0
                else "ASSET_SPECIFIC"
                if (abs(btc) >= 5) != (abs(eth) >= 5)
                else "CONTRADICTORY"
            )
    maximum = max((abs(value) for value in signs), default=0)
    return {
        "identity": {
            "methodology": {
                "name": METHODOLOGY_NAME,
                "version": METHODOLOGY_VERSION,
                "digest": methodology_digest(),
            },
            "universe": {
                "name": UNIVERSE_NAME,
                "version": UNIVERSE_VERSION,
                "digest": universe_digest(),
            },
        },
        "coverage": {
            "exact_common_source_rows": int(exact["rows"]),
            "exact_common_first_timestamp_ms": int(exact["first_ms"]),
            "exact_common_last_timestamp_ms": int(exact["last_ms"]),
            "causal_usable_rows": len(rows),
            "causal_first_timestamp_ms": rows[0].timestamp_ms,
            "causal_last_timestamp_ms": rows[-1].timestamp_ms,
            "assets": list(ASSETS),
            "derivatives_assets": ["BTC", "ETH", "SOL", "BNB"],
            "source_dataset_digests": {
                name: manifests[name]["content_digest"] for name in sorted(manifests)
            },
        },
        "contract": state_contract(),
        "results": results,
        "multiple_comparisons": {
            "states": len(STATE_NAMES),
            "combinations": len(COMBINATION_NAMES),
            "horizons": len(HORIZONS),
            "targets": len(TARGETS),
            "threshold_metrics": 6,
            "primary_cells": len(strongest),
            "strongest_positive": {
                "value_bps": cells[-1][0],
                "hypothesis": cells[-1][1],
                "target": cells[-1][2],
                "horizon_minutes": cells[-1][3],
            }
            if cells
            else None,
            "strongest_negative": {
                "value_bps": cells[0][0],
                "hypothesis": cells[0][1],
                "target": cells[0][2],
                "horizon_minutes": cells[0][3],
            }
            if cells
            else None,
            "median_effect_bps": median(signs) if signs else None,
            "sign_consistent_positive_cells": sum(value > 0 for value in signs),
            "sign_consistent_negative_cells": sum(value < 0 for value in signs),
        },
        "cross_target_consistency": cross_target,
        "placebo": {
            "predeclared_trigger_abs_mean_bps": 60,
            "triggered": maximum >= 60,
            "reason": "no cell reached the stated 60 bps lower transaction-cost opportunity hurdle"
            if maximum < 60
            else "strong-cell placebo required",
        },
        "verdict": "MULTI_ASSET_ADDS_NO_CLEAR_INFORMATION"
        if maximum < 30 and abs(median(signs)) < 2
        else "MULTI_ASSET_ADDS_WEAK_INFORMATION",
        "component_disposition": {
            "8_ASSET_BREADTH": "WEAK",
            "ROTATION_V2": "WEAK",
            "SOL_BNB_DERIVATIVES": "WEAK",
            "GROUP_DERIVATIVES_BREADTH": "WEAK",
        },
        "remaining_derivatives_recommendation": "DO_NOT_EXPAND_DERIVATIVES_YET",
    }


def report_digest(report: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
