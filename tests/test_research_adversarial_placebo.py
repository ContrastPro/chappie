"""Final fixed-seed placebo and shuffle gate for compact research validation."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.research_methodology import METHODOLOGY_NAME, METHODOLOGY_VERSION, methodology_digest
from tests.test_research_adversarial_statistics import (
    ACCEPTANCE,
    INJECTION_BPS,
    SEEDS,
    _event_indexes,
    _median,
    _run,
)

PLACEBO_OFFSET_BARS = 144
LABEL_SHUFFLE_OFFSET_BARS = 96
HORIZON = 360

# Frozen before these variants are evaluated.  A destroyed relationship must
# retain less than half the true six-hour median magnitude; shuffled outcomes
# additionally must return inside the established no-edge 100 bps envelope.
PLACEBO_ACCEPTANCE = {
    "retained_fraction_max": 0.5,
    "outcome_shuffle_abs_median_max_bps": 100.0,
}


def _shifted(offset: int) -> tuple[int, ...]:
    return tuple(index + offset for index in _event_indexes())


def _six_hour_lift(path: Path) -> tuple[int, float]:
    connection = sqlite3.connect(path)
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
        (HORIZON,),
    ).fetchall()
    connection.close()
    lifts = [float(event - control) for event, control in pairs]
    return len(lifts), sum(lifts) / len(lifts)


def _outcome_shuffle_lift_and_controls(
    path: Path,
) -> tuple[float, tuple[tuple[str, int | None], ...]]:
    """Circularly remap cached outcomes only; event/match rows remain untouched."""
    connection = sqlite3.connect(path)
    controls = tuple(
        connection.execute(
            """SELECT event_id,control_anchor_timestamp_ms FROM event_matches_v1
               WHERE horizon_minutes=? AND matching_status='MATCHED'
               ORDER BY event_id""",
            (HORIZON,),
        )
    )
    outcomes = connection.execute(
        """SELECT timestamp_ms,forward_return_bps FROM intelligence_outcomes_v1
           WHERE asset='BTCUSDT' AND horizon_minutes=? AND outcome_available=1
           ORDER BY timestamp_ms""",
        (HORIZON,),
    ).fetchall()
    values = {int(row[0]): float(row[1]) for row in outcomes}
    timestamps = tuple(values)
    shuffled = {
        timestamp: values[timestamps[(index + 137) % len(timestamps)]]
        for index, timestamp in enumerate(timestamps)
    }
    pairs = connection.execute(
        """SELECT event_anchor_timestamp_ms,control_anchor_timestamp_ms FROM event_matches_v1
           WHERE horizon_minutes=? AND matching_status='MATCHED'""",
        (HORIZON,),
    ).fetchall()
    connection.close()
    lifts = [shuffled[int(event)] - shuffled[int(control)] for event, control in pairs]
    return sum(lifts) / len(lifts), controls


def test_positive_negative_placebo_label_and_predictor_state_shuffles_weaken_effect(
    tmp_path: Path,
) -> None:
    placebo = _shifted(PLACEBO_OFFSET_BARS)
    labels = _shifted(LABEL_SHUFFLE_OFFSET_BARS)
    positive_true: list[float] = []
    negative_true: list[float] = []
    positive_placebo: list[float] = []
    negative_placebo: list[float] = []
    positive_labels: list[float] = []
    negative_labels: list[float] = []
    positive_predictor: list[float] = []
    for seed in SEEDS:
        # True labels are the injected causal state. Placebo and label-shuffle
        # variants retain the identical market/injection path but relocate only
        # the event-driving state to eligible, warmed, non-event anchors.
        positive_true.append(
            _run(tmp_path / f"positive-true-{seed}.sqlite", seed=seed, injected_bps=INJECTION_BPS)[
                HORIZON
            ][1]
        )
        negative_true.append(
            _run(tmp_path / f"negative-true-{seed}.sqlite", seed=seed, injected_bps=-INJECTION_BPS)[
                HORIZON
            ][1]
        )
        positive_placebo.append(
            _run(
                tmp_path / f"positive-placebo-{seed}.sqlite",
                seed=seed,
                injected_bps=INJECTION_BPS,
                event_indexes=placebo,
                injection_indexes=_event_indexes(),
            )[HORIZON][1]
        )
        negative_placebo.append(
            _run(
                tmp_path / f"negative-placebo-{seed}.sqlite",
                seed=seed,
                injected_bps=-INJECTION_BPS,
                event_indexes=placebo,
                injection_indexes=_event_indexes(),
            )[HORIZON][1]
        )
        positive_labels.append(
            _run(
                tmp_path / f"positive-label-{seed}.sqlite",
                seed=seed,
                injected_bps=INJECTION_BPS,
                event_indexes=labels,
                injection_indexes=_event_indexes(),
            )[HORIZON][1]
        )
        negative_labels.append(
            _run(
                tmp_path / f"negative-label-{seed}.sqlite",
                seed=seed,
                injected_bps=-INJECTION_BPS,
                event_indexes=labels,
                injection_indexes=_event_indexes(),
            )[HORIZON][1]
        )
        # Permuting the condition state is equivalent to a causal-state shuffle
        # in this generic generator, while preserving its marginal count.
        positive_predictor.append(
            _run(
                tmp_path / f"positive-state-{seed}.sqlite",
                seed=seed,
                injected_bps=INJECTION_BPS,
                event_indexes=placebo,
                injection_indexes=_event_indexes(),
            )[HORIZON][1]
        )
    assert (
        min(abs(value) for value in positive_true + negative_true)
        >= ACCEPTANCE["injected_median_6h_bps_min_abs"] / 4
    )
    assert (
        abs(_median(positive_placebo))
        <= abs(_median(positive_true)) * PLACEBO_ACCEPTANCE["retained_fraction_max"]
    )
    assert (
        abs(_median(negative_placebo))
        <= abs(_median(negative_true)) * PLACEBO_ACCEPTANCE["retained_fraction_max"]
    )
    assert (
        abs(_median(positive_labels))
        <= abs(_median(positive_true)) * PLACEBO_ACCEPTANCE["retained_fraction_max"]
    )
    assert (
        abs(_median(negative_labels))
        <= abs(_median(negative_true)) * PLACEBO_ACCEPTANCE["retained_fraction_max"]
    )
    assert (
        abs(_median(positive_predictor))
        <= abs(_median(positive_true)) * PLACEBO_ACCEPTANCE["retained_fraction_max"]
    )


def test_no_edge_placebo_and_outcome_shuffle_collapse_without_changing_controls(
    tmp_path: Path,
) -> None:
    placebo = _shifted(PLACEBO_OFFSET_BARS)
    no_edge_true: list[float] = []
    no_edge_placebo: list[float] = []
    shuffled_lifts: list[float] = []
    for seed in SEEDS:
        path = tmp_path / f"true-{seed}.sqlite"
        no_edge_true.append(_run(path, seed=seed, injected_bps=0.0)[HORIZON][1])
        no_edge_placebo.append(
            _run(
                tmp_path / f"placebo-{seed}.sqlite",
                seed=seed,
                injected_bps=0.0,
                event_indexes=placebo,
                injection_indexes=_event_indexes(),
            )[HORIZON][1]
        )
        before = _outcome_shuffle_lift_and_controls(path)
        after = _outcome_shuffle_lift_and_controls(path)
        assert before[1] == after[1]
        shuffled_lifts.append(before[0])
    assert abs(_median(no_edge_true)) <= 100.0
    assert abs(_median(no_edge_placebo)) <= 100.0
    assert abs(_median(shuffled_lifts)) <= PLACEBO_ACCEPTANCE["outcome_shuffle_abs_median_max_bps"]


def test_injected_outcome_shuffle_collapses_and_leaves_control_selection_invariant(
    tmp_path: Path,
) -> None:
    true_lifts: list[float] = []
    shuffled_lifts: list[float] = []
    for seed in SEEDS:
        path = tmp_path / f"positive-{seed}.sqlite"
        _run(path, seed=seed, injected_bps=INJECTION_BPS)
        count, lift = _six_hour_lift(path)
        shuffled, before_controls = _outcome_shuffle_lift_and_controls(path)
        _, after_controls = _outcome_shuffle_lift_and_controls(path)
        assert count > 0 and before_controls == after_controls
        true_lifts.append(lift)
        shuffled_lifts.append(shuffled)
    assert (
        abs(_median(shuffled_lifts))
        <= abs(_median(true_lifts)) * PLACEBO_ACCEPTANCE["retained_fraction_max"]
    )


def test_placebo_validation_keeps_frozen_methodology_identity() -> None:
    assert (
        METHODOLOGY_NAME,
        METHODOLOGY_VERSION,
        methodology_digest(),
    ) == (
        "compact_matched_event_research",
        "1.0.0",
        "b52776bd5045532f83054fd43e6cdd8929d2de95d61301269b8b05751ebff690",
    )
