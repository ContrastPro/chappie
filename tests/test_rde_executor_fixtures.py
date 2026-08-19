from dataclasses import replace

from app.ai.rde_capabilities import ExistingEventResearchQueryV1
from app.ai.rde_query_executor import (
    execute_canonical_existing_cache_query,
    execute_existing_cache_query,
)
from app.ai.research_query_compiler import CanonicalRDEQueryV1, CanonicalResearchPredicateV1
from app.research_cache_identity import VerifiedResearchCacheIdentityV1
from tests.rde_executor_fixture_support import (
    bps_oracle,
    btc_fixture,
    eth_touch_fixture,
    invalid_cross_asset_fixture,
)


def _query(fixture):
    return ExistingEventResearchQueryV1(
        "query-btc",
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


def _canonical_event_query(fixture) -> CanonicalRDEQueryV1:
    return CanonicalRDEQueryV1(
        predicates=(
            CanonicalResearchPredicateV1(
                fixture.asset,
                "EVENT_TYPE",
                "EQUALS",
                fixture.family,
                None,
                "AS_OF_FACT_TIMESTAMP",
                "AVAILABLE_ONLY",
                "fixture-fact",
            ),
        ),
        target_asset=fixture.asset,
        target_metric=fixture.metric,
        target_horizon="6h",
        comparison_basis="MATCHED_CONTROL",
        expected_relationship="GREATER_THAN_MATCHED_CONTROL",
        source_intent_id="fixture-intent",
        source_fact_ids=("fixture-fact",),
        compiler_identity="fixture-compiler",
        compiler_digest="fixture-compiler-digest",
    )


def _unverified(identity: VerifiedResearchCacheIdentityV1) -> VerifiedResearchCacheIdentityV1:
    return replace(identity, outcome_verified=False)


def test_executor_consumes_only_persisted_fixture_pairs() -> None:
    fixture = btc_fixture()
    result = execute_existing_cache_query(_query(fixture), fixture.identity, fixture)
    assert result.status == "EXECUTED"
    assert result.evidence is not None
    assert result.evidence.matched_count == 3
    assert result.evidence.incremental_mean == 200 / 3
    assert (fixture.event_reads, fixture.match_reads, fixture.outcome_reads) == (1, 1, 1)


def test_btc_execution_matches_oracle_years_provenance_and_persisted_references() -> None:
    fixture = btc_fixture()
    result = execute_existing_cache_query(_query(fixture), fixture.identity, fixture)
    assert result.status == "EXECUTED"
    evidence = result.evidence
    assert evidence is not None
    oracle = bps_oracle(fixture)
    assert (
        evidence.matched_count,
        evidence.unmatched_count,
        evidence.outcome_unavailable_count,
    ) == (
        oracle["matched"],
        oracle["unmatched"],
        oracle["unavailable"],
    )
    assert (evidence.event_mean, evidence.event_median) == (
        oracle["event_mean"],
        oracle["event_median"],
    )
    assert (evidence.control_mean, evidence.control_median) == (
        oracle["control_mean"],
        oracle["control_median"],
    )
    assert (evidence.incremental_mean, evidence.incremental_median) == (
        oracle["incremental_mean"],
        oracle["incremental_median"],
    )
    assert [item.year for item in evidence.year_slices] == [2023, 2024, 2025]
    assert [item.event_id for item in evidence.event_references] == [
        "btc-event-1",
        "btc-event-2",
        "btc-event-3",
    ]
    assert [item.match_id for item in evidence.match_references] == [
        "btc-match-1",
        "btc-match-2",
        "btc-match-3",
    ]
    assert evidence.evidence_status == "DESCRIPTIVE_DIRECTIONAL_SUPPORT"
    assert (
        evidence.verified_cache_identity_digest == fixture.identity.research_cache_identity_digest
    )
    assert evidence.event_match_cache_identity == fixture.event_match_digest
    assert evidence.outcome_cache_identity == fixture.outcome_digest


def test_eth_touch_execution_uses_persisted_boolean_outcomes() -> None:
    fixture = eth_touch_fixture()
    result = execute_existing_cache_query(_query(fixture), fixture.identity, fixture)
    assert result.status == "EXECUTED"
    evidence = result.evidence
    assert evidence is not None
    assert (evidence.event_mean, evidence.control_mean, evidence.incremental_mean) == (
        2 / 3,
        1 / 3,
        1 / 3,
    )
    assert evidence.evidence_status == "DESCRIPTIVE_DIRECTIONAL_SUPPORT"


def test_invalid_capability_and_identity_fail_closed_before_storage() -> None:
    fixture = btc_fixture()
    query = _query(fixture)
    object.__setattr__(query, "event_family", "NOT_A_FROZEN_FAMILY")
    result = execute_existing_cache_query(query, fixture.identity, fixture)
    assert result.status == "QUERY_INVALID"
    assert fixture.reads == 0

    fixture = btc_fixture()
    result = execute_existing_cache_query(_query(fixture), _unverified(fixture.identity), fixture)
    assert result.status == "VERIFIED_CACHE_IDENTITY_INVALID"
    assert fixture.reads == 0


def test_query_and_dependency_identity_mismatches_have_zero_storage_access() -> None:
    variants = (
        lambda query, identity: replace(query, asset="ETHUSDT"),
        lambda query, identity: replace(query, existing_cache_identity="wrong"),
        lambda query, identity: replace(query, event_match_cache_identity="wrong"),
        lambda query, identity: replace(query, outcome_cache_identity="wrong"),
        lambda query, identity: replace(identity, aligned_content_digest="wrong"),
        lambda query, identity: replace(identity, methodology_digest="wrong"),
    )
    for mutate in variants:
        fixture = btc_fixture()
        query, identity = _query(fixture), fixture.identity
        changed = mutate(query, identity)
        if isinstance(changed, ExistingEventResearchQueryV1):
            query = changed
        else:
            identity = changed
        result = execute_existing_cache_query(query, identity, fixture)
        assert result.status != "EXECUTED"
        assert fixture.reads == 0


def test_cross_asset_control_fails_after_event_and_match_read_without_evidence() -> None:
    fixture = invalid_cross_asset_fixture()
    result = execute_existing_cache_query(_query(fixture), fixture.identity, fixture)
    assert result.status == "ASSET_MISMATCH"
    assert result.evidence is None
    assert (fixture.event_reads, fixture.match_reads, fixture.outcome_reads) == (1, 1, 0)


def test_canonical_query_resolves_then_executes_without_llm() -> None:
    fixture = btc_fixture()
    result = execute_canonical_existing_cache_query(
        _canonical_event_query(fixture), fixture.identity, fixture
    )
    assert result.status == "EXECUTED"
    assert result.evidence is not None
    assert result.evidence.query_digest == _canonical_event_query(fixture).query_digest


def test_generic_oi_capability_is_rejected_before_storage() -> None:
    fixture = btc_fixture()
    query = replace(
        _canonical_event_query(fixture),
        predicates=(
            CanonicalResearchPredicateV1(
                "BTCUSDT", "OI_STATE", "EQUALS", "UP", None, "AS_OF", "AVAILABLE", "oi"
            ),
        ),
    )
    result = execute_canonical_existing_cache_query(query, fixture.identity, fixture)
    assert result.status == "METHODOLOGY_EXTENSION_REQUIRED"
    assert fixture.reads == 0


def test_btc_fixture_is_deterministic_and_has_independent_bps_oracle() -> None:
    fixture = btc_fixture()
    assert fixture == btc_fixture()
    assert bps_oracle(fixture) == {
        "matched": 3,
        "unmatched": 1,
        "unavailable": 1,
        "event_mean": 250 / 3,
        "event_median": 100.0,
        "control_mean": 50 / 3,
        "control_median": 20.0,
        "incremental_mean": 200 / 3,
        "incremental_median": 80.0,
        "years": {2023: 1, 2024: 1, 2025: 1},
    }


def test_mutated_future_outcomes_preserve_frozen_event_and_match_identity() -> None:
    baseline, mutated = btc_fixture(), btc_fixture(mutated=True)
    assert [(x.event_id, x.match_id, x.control_asset, x.control_ms) for x in baseline.pairs] == [
        (x.event_id, x.match_id, x.control_asset, x.control_ms) for x in mutated.pairs
    ]
    assert baseline.event_match_digest == mutated.event_match_digest
    assert baseline.outcome_digest != mutated.outcome_digest
    assert (
        baseline.identity.research_cache_identity_digest
        != mutated.identity.research_cache_identity_digest
    )


def test_eth_touch_fixture_uses_persisted_boolean_representation() -> None:
    fixture = eth_touch_fixture()
    assert [pair.event_value for pair in fixture.pairs] == [1.0, 0.0, 1.0]
    assert [pair.control_value for pair in fixture.pairs] == [0.0, 0.0, 1.0]


def test_cross_asset_fixture_is_explicitly_invalid_for_future_executor() -> None:
    assert invalid_cross_asset_fixture().pairs[0].control_asset == "ETHUSDT"
