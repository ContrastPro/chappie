"""E1-C proof that outcome changes cannot alter persisted executor identities."""

from __future__ import annotations

from datetime import UTC, datetime
from statistics import mean, median

from app.ai.rde_capabilities import ExistingEventResearchQueryV1
from app.ai.rde_execution_contracts import ExistingCacheResearchEvidenceV1
from app.ai.rde_query_executor import execute_existing_cache_query
from tests.rde_executor_fixture_support import Fixture, Pair, btc_fixture, eth_touch_fixture


def _query(fixture: Fixture) -> ExistingEventResearchQueryV1:
    return ExistingEventResearchQueryV1(
        "btc-fixture-query",
        fixture.asset,
        fixture.family,
        "BASE",
        fixture.metric,
        "6h",
        "GREATER_THAN_MATCHED_CONTROL",
        "MATCHED_CONTROL",
        fixture.identity.research_cache_identity_digest,
        fixture.event_match_digest,
        fixture.outcome_digest,
    )


def _execute(fixture: Fixture) -> ExistingCacheResearchEvidenceV1:
    result = execute_existing_cache_query(_query(fixture), fixture.identity, fixture)
    assert result.status == "EXECUTED"
    assert isinstance(result.evidence, ExistingCacheResearchEvidenceV1)
    return result.evidence


def _independent_summary(fixture: Fixture) -> dict[str, object]:
    """Deliberately separate arithmetic from the executor implementation."""
    matched = [pair for pair in fixture.pairs if pair.match_id is not None]
    valid = [
        pair for pair in matched if pair.event_value is not None and pair.control_value is not None
    ]
    event_values = [float(pair.event_value) for pair in valid]
    control_values = [float(pair.control_value) for pair in valid]
    years: dict[int, list[Pair]] = {}
    for pair in valid:
        years.setdefault(datetime.fromtimestamp(pair.event_ms / 1000, UTC).year, []).append(pair)
    return {
        "matched": len(valid),
        "unmatched": len(fixture.pairs) - len(matched),
        "unavailable": len(matched) - len(valid),
        "event_values": tuple(event_values),
        "control_values": tuple(control_values),
        "event_mean": mean(event_values),
        "event_median": median(event_values),
        "control_mean": mean(control_values),
        "control_median": median(control_values),
        "incremental_mean": (sum(event_values) - sum(control_values)) / len(event_values),
        "incremental_median": median(event_values) - median(control_values),
        "years": tuple(
            (
                year,
                len(items),
                mean([float(item.event_value) for item in items]),
                median([float(item.event_value) for item in items]),
                len(items),
                mean([float(item.control_value) for item in items]),
                median([float(item.control_value) for item in items]),
                (
                    sum(float(item.event_value) for item in items)
                    - sum(float(item.control_value) for item in items)
                )
                / len(items),
                median([float(item.event_value) for item in items])
                - median([float(item.control_value) for item in items]),
            )
            for year, items in sorted(years.items())
        ),
    }


def _assert_independent_parity(fixture: Fixture, evidence: ExistingCacheResearchEvidenceV1) -> None:
    expected = _independent_summary(fixture)
    assert (
        evidence.matched_count,
        evidence.unmatched_count,
        evidence.outcome_unavailable_count,
    ) == (
        expected["matched"],
        expected["unmatched"],
        expected["unavailable"],
    )
    assert (evidence.event_values, evidence.control_values) == (
        expected["event_values"],
        expected["control_values"],
    )
    assert (evidence.event_mean, evidence.event_median) == (
        expected["event_mean"],
        expected["event_median"],
    )
    assert (evidence.control_mean, evidence.control_median) == (
        expected["control_mean"],
        expected["control_median"],
    )
    assert (evidence.incremental_mean, evidence.incremental_median) == (
        expected["incremental_mean"],
        expected["incremental_median"],
    )
    assert [
        (
            item.year,
            item.matched_count,
            item.event_mean,
            item.event_median,
            item.control_count,
            item.control_mean,
            item.control_median,
            item.incremental_mean,
            item.incremental_median,
        )
        for item in evidence.year_slices
    ] == list(expected["years"])


def test_future_outcome_mutation_preserves_executor_consumed_identities() -> None:
    baseline_fixture, mutated_fixture = btc_fixture(), btc_fixture(mutated=True)
    baseline, mutated = _execute(baseline_fixture), _execute(mutated_fixture)

    assert baseline.query_digest == mutated.query_digest
    assert (
        baseline.asset,
        baseline.event_family,
        baseline.variant,
        baseline.target_metric,
        baseline.target_horizon,
        baseline.expected_relationship,
        baseline.comparison_basis,
    ) == (
        mutated.asset,
        mutated.event_family,
        mutated.variant,
        mutated.target_metric,
        mutated.target_horizon,
        mutated.expected_relationship,
        mutated.comparison_basis,
    )
    assert baseline.event_references == mutated.event_references
    assert baseline.match_references == mutated.match_references
    assert (
        baseline.executor_identity,
        baseline.executor_digest,
        baseline.methodology_name,
        baseline.methodology_version,
        baseline.methodology_digest,
        baseline.aligned_cache_identity,
        baseline.event_match_cache_identity,
    ) == (
        mutated.executor_identity,
        mutated.executor_digest,
        mutated.methodology_name,
        mutated.methodology_version,
        mutated.methodology_digest,
        mutated.aligned_cache_identity,
        mutated.event_match_cache_identity,
    )
    assert baseline.outcome_cache_identity != mutated.outcome_cache_identity
    assert baseline.verified_cache_identity_digest != mutated.verified_cache_identity_digest
    assert baseline.event_values != mutated.event_values
    assert [
        (pair.event_id, pair.match_id, pair.control_asset)
        for pair in baseline_fixture.pairs
        if pair.event_id in {"btc-unmatched", "btc-unavailable"}
    ] == [
        (pair.event_id, pair.match_id, pair.control_asset)
        for pair in mutated_fixture.pairs
        if pair.event_id in {"btc-unmatched", "btc-unavailable"}
    ]
    assert baseline.evidence_status == "DESCRIPTIVE_DIRECTIONAL_SUPPORT"
    assert mutated.evidence_status == "DESCRIPTIVE_DIRECTIONAL_CONTRADICTION"
    assert (
        baseline_fixture.event_reads,
        baseline_fixture.match_reads,
        baseline_fixture.outcome_reads,
    ) == (
        1,
        1,
        1,
    )
    assert (
        mutated_fixture.event_reads,
        mutated_fixture.match_reads,
        mutated_fixture.outcome_reads,
    ) == (
        1,
        1,
        1,
    )


def test_baseline_and_mutated_btc_arithmetic_is_independently_recomputed() -> None:
    for fixture in (btc_fixture(), btc_fixture(mutated=True)):
        evidence = _execute(fixture)
        _assert_independent_parity(fixture, evidence)
        assert evidence.unmatched_count == 1
        assert evidence.outcome_unavailable_count == 1
        assert "btc-unmatched" not in [item.event_id for item in evidence.event_references]
        assert "btc-unavailable" not in [item.event_id for item in evidence.event_references]


def test_eth_touch_metric_and_event_year_slices_have_independent_parity() -> None:
    fixture = eth_touch_fixture()
    evidence = _execute(fixture)
    _assert_independent_parity(fixture, evidence)
    assert (evidence.event_mean, evidence.control_mean, evidence.incremental_mean) == (
        2 / 3,
        1 / 3,
        1 / 3,
    )
    assert [item.year for item in evidence.year_slices] == [2023, 2024, 2025]


def test_median_difference_is_difference_of_medians_not_median_of_pair_deltas() -> None:
    event_values = (0.0, 100.0, 101.0)
    control_values = (0.0, 1.0, 100.0)
    canonical = median(event_values) - median(control_values)
    pairwise = median(
        event - control for event, control in zip(event_values, control_values, strict=True)
    )
    assert canonical == 99.0
    assert pairwise == 1.0
    assert canonical != pairwise


def test_repeated_baseline_execution_is_semantically_deterministic() -> None:
    first, second = _execute(btc_fixture()), _execute(btc_fixture())
    assert first == second
