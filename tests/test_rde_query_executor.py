"""E1-B executor boundary tests: verified fixtures are read only after validation."""

from __future__ import annotations

from dataclasses import replace

from app.ai.rde_capabilities import ExistingEventResearchQueryV1
from app.ai.rde_query_executor import execute_existing_cache_query, existing_executor_digest
from tests.rde_executor_fixture_support import Pair, btc_fixture


def _query() -> ExistingEventResearchQueryV1:
    fixture = btc_fixture()
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


def test_invalid_existing_query_never_reads_persisted_fixture_rows() -> None:
    fixture = btc_fixture()
    query = _query()
    object.__setattr__(query, "target_metric", "NOT_A_METRIC")
    result = execute_existing_cache_query(query, fixture.identity, fixture)
    assert result.status == "QUERY_INVALID"
    assert fixture.reads == 0


def test_conflicting_persisted_match_rows_fail_closed_before_outcome_lookup() -> None:
    fixture = btc_fixture()
    duplicate = Pair(
        "btc-event-1",
        "btc-match-1",
        "BTCUSDT",
        1672531200000,
        "BTCUSDT",
        1672531200002,
        100.0,
        20.0,
    )
    corrupted = replace(fixture, pairs=(*fixture.pairs, duplicate))
    result = execute_existing_cache_query(_query(), corrupted.identity, corrupted)
    assert result.status == "MATCH_INTEGRITY_INVALID"
    assert result.evidence is None
    assert (corrupted.event_reads, corrupted.match_reads, corrupted.outcome_reads) == (1, 1, 0)


def test_existing_executor_identity_is_deterministic_and_semantics_scoped() -> None:
    assert existing_executor_digest() == existing_executor_digest()
    assert len(existing_executor_digest()) == 64
