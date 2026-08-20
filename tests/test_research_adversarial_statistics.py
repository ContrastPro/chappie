"""Fixed-seed no-edge and injected-edge checks for the frozen compact engine."""

from __future__ import annotations

import hashlib
import json
import random
import sqlite3
from pathlib import Path

from app.research.data.event_bridge import build_event_match_cache
from app.research.data.outcomes import build_outcome_cache
from app.research.data.store import ResearchStore
from app.research_methodology import METHODOLOGY_NAME, METHODOLOGY_VERSION, methodology_digest

SEEDS = (11, 29, 47, 71, 101)
ROWS = 7_200
EVENT_START = 400
EVENT_SPACING = 288  # One day: avoids overlapping 24-hour event cooldowns.
INJECTION_HORIZON_BARS = 72
INJECTION_BPS = 1_000.0
HORIZONS = (60, 180, 360, 720, 1440)

# Frozen before observing results.  The no-edge rule allows ordinary finite-sample
# variation but rejects a stable directional result; injected effects need a
# material aggregate directional lift at their six-hour horizon.
ACCEPTANCE = {
    "no_edge_abs_median_6h_bps_max": 100.0,
    "no_edge_large_lift_bps": 50.0,
    "no_edge_same_direction_seed_max": 4,
    "injected_median_6h_bps_min_abs": 60.0,
    "injected_correct_direction_seed_min": 4,
    "removal_abs_median_fraction_max": 0.5,
}


def _refresh_alignment_digest(connection: sqlite3.Connection) -> None:
    digest = hashlib.sha256()
    for row in connection.execute(
        "SELECT * FROM intelligence_observations_v1 ORDER BY asset,timestamp_ms"
    ):
        digest.update(
            json.dumps(tuple(row), separators=(",", ":"), ensure_ascii=True).encode() + b"\n"
        )
    connection.execute(
        "UPDATE aligned_manifest_v1 SET content_digest=? WHERE asset='BTCUSDT'",
        (digest.hexdigest(),),
    )


def _event_indexes() -> tuple[int, ...]:
    return tuple(range(EVENT_START, ROWS - 288, EVENT_SPACING))


def _build_market(
    path: Path,
    *,
    seed: int,
    injected_bps: float,
    event_indexes: tuple[int, ...] | None = None,
    injection_indexes: tuple[int, ...] | None = None,
) -> None:
    """A causal-label-independent random walk with optional gradual post-event drift."""
    rng = random.Random(seed)
    returns = [rng.choice((-12.0, -8.0, -4.0, 4.0, 8.0, 12.0)) for _ in range(ROWS - 1)]
    event_indexes = _event_indexes() if event_indexes is None else event_indexes
    injection_indexes = event_indexes if injection_indexes is None else injection_indexes
    for anchor in injection_indexes:
        for offset in range(1, INJECTION_HORIZON_BARS + 1):
            returns[anchor + offset - 1] += injected_bps / INJECTION_HORIZON_BARS
    closes = [100.0]
    for change in returns:
        closes.append(closes[-1] * (1 + change / 10_000))

    store = ResearchStore(path)
    store.initialize()
    connection = store.connection
    events = set(event_indexes)
    rows = []
    for index, close in enumerate(closes):
        active = index in events
        rows.append(
            (
                "BTCUSDT",
                1_735_689_600_000 + index * 300_000,
                0,
                0,
                0,
                1,
                1,
                close,
                1.0,
                0.5,
                None,
                1.0,
                1.0,
                None,
                None,
                None,
                "PRICE_DOWN_OI_UP" if active else None,
                None,
                None,
                None,
                -0.001 if active else None,
                "EXTREME_LONG" if active else None,
                None,
                None,
                None,
                100.0,
                100.0,
                0.0,
                0.0,
                0.0,
                0.0,
                "FLAT",
                "FLAT",
            )
        )
    connection.executemany(
        f"INSERT INTO intelligence_observations_v1 VALUES ({','.join('?' for _ in rows[0])})",
        rows,
    )
    connection.execute(
        """INSERT INTO aligned_manifest_v1(
              asset,state,alignment_schema,flow_dataset,basis_dataset,flow_content_digest,
              basis_content_digest,flow_source_sha256,basis_source_sha256,build_config_digest,
              content_digest
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "BTCUSDT",
            "VERIFIED",
            "test",
            "flow",
            "basis",
            "flow",
            "basis",
            "flow",
            "basis",
            "test",
            "",
        ),
    )
    _refresh_alignment_digest(connection)
    connection.commit()
    store.close()


def _run(
    path: Path,
    *,
    seed: int,
    injected_bps: float,
    event_indexes: tuple[int, ...] | None = None,
    injection_indexes: tuple[int, ...] | None = None,
) -> dict[int, tuple[int, float]]:
    _build_market(
        path,
        seed=seed,
        injected_bps=injected_bps,
        event_indexes=event_indexes,
        injection_indexes=injection_indexes,
    )
    build_outcome_cache(store_path=path, asset="BTCUSDT")
    build_event_match_cache(store_path=path, asset="BTCUSDT", event_type="CROWDED_STRESS")
    connection = sqlite3.connect(path)
    results: dict[int, tuple[int, float]] = {}
    for horizon in HORIZONS:
        pairs = connection.execute(
            """SELECT event_outcome.forward_return_bps, control_outcome.forward_return_bps
               FROM event_matches_v1 AS matched
               JOIN intelligence_outcomes_v1 AS event_outcome
                 ON event_outcome.asset=matched.asset
                AND event_outcome.timestamp_ms=matched.event_anchor_timestamp_ms
                AND event_outcome.horizon_minutes=matched.horizon_minutes
                JOIN intelligence_outcomes_v1 AS control_outcome
                 ON control_outcome.asset=matched.asset
                AND control_outcome.timestamp_ms=matched.control_anchor_timestamp_ms
                AND control_outcome.horizon_minutes=matched.horizon_minutes
               WHERE matched.asset='BTCUSDT' AND matched.horizon_minutes=?
                 AND matched.matching_status='MATCHED'""",
            (horizon,),
        ).fetchall()
        lifts = [float(event - control) for event, control in pairs]
        results[horizon] = (len(lifts), sum(lifts) / len(lifts) if lifts else 0.0)
    connection.close()
    return results


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def test_no_edge_has_no_persistent_directional_lift(tmp_path: Path) -> None:
    lifts = [
        _run(tmp_path / f"no-edge-{seed}.sqlite", seed=seed, injected_bps=0.0)[360]
        for seed in SEEDS
    ]
    values = [lift for _, lift in lifts]
    assert all(count >= 4 for count, _ in lifts)
    assert abs(_median(values)) <= ACCEPTANCE["no_edge_abs_median_6h_bps_max"]
    material = [value for value in values if abs(value) >= ACCEPTANCE["no_edge_large_lift_bps"]]
    assert (
        max(sum(value > 0 for value in material), sum(value < 0 for value in material))
        <= ACCEPTANCE["no_edge_same_direction_seed_max"]
    )


def test_positive_negative_injection_recovery_removal_and_horizon_response(tmp_path: Path) -> None:
    no_edge = [
        _run(tmp_path / f"base-{seed}.sqlite", seed=seed, injected_bps=0.0) for seed in SEEDS
    ]
    positive = [
        _run(tmp_path / f"positive-{seed}.sqlite", seed=seed, injected_bps=INJECTION_BPS)
        for seed in SEEDS
    ]
    negative = [
        _run(tmp_path / f"negative-{seed}.sqlite", seed=seed, injected_bps=-INJECTION_BPS)
        for seed in SEEDS
    ]
    baseline = [_result[360][1] for _result in no_edge]
    recovered_positive = [result[360][1] for result in positive]
    recovered_negative = [result[360][1] for result in negative]
    assert all(result[360][0] >= 4 for result in positive + negative)
    assert (
        sum(value > 0 for value in recovered_positive)
        >= ACCEPTANCE["injected_correct_direction_seed_min"]
    )
    assert (
        sum(value < 0 for value in recovered_negative)
        >= ACCEPTANCE["injected_correct_direction_seed_min"]
    )
    assert _median(recovered_positive) >= ACCEPTANCE["injected_median_6h_bps_min_abs"]
    assert _median(recovered_negative) <= -ACCEPTANCE["injected_median_6h_bps_min_abs"]
    assert (
        abs(_median(baseline))
        <= abs(_median(recovered_positive)) * ACCEPTANCE["removal_abs_median_fraction_max"]
    )
    assert (
        abs(_median(baseline))
        <= abs(_median(recovered_negative)) * ACCEPTANCE["removal_abs_median_fraction_max"]
    )
    # Gradual 6h drift is expected to be strongest by 6h, with persistence at
    # later horizons allowed and no requirement to optimize a horizon profile.
    assert _median([result[360][1] for result in positive]) > _median(
        [result[60][1] for result in positive]
    )
    assert _median([result[360][1] for result in negative]) < _median(
        [result[60][1] for result in negative]
    )


def test_statistical_validation_keeps_frozen_methodology_identity() -> None:
    assert (
        METHODOLOGY_NAME,
        METHODOLOGY_VERSION,
        methodology_digest(),
    ) == (
        "compact_matched_event_research",
        "1.0.0",
        "b52776bd5045532f83054fd43e6cdd8929d2de95d61301269b8b05751ebff690",
    )
