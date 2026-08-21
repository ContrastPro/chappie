"""E2 cache/service proofs use only deterministic existing-cache fixtures."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from app.ai.research.capabilities import ExistingEventResearchQueryV1
from app.ai.research.execution_cache import (
    CacheStatusV1,
    RDEResearchExecutionService,
    cache_path,
    execution_cache_identity,
    research_evidence_digest,
)
from app.ai.research.query_compiler import CanonicalRDEQueryV1, CanonicalResearchPredicateV1
from app.ai.research.query_executor import execute_existing_cache_query
from tests.rde_executor_fixture_support import Fixture, btc_fixture


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


def _canonical_oi() -> CanonicalRDEQueryV1:
    return CanonicalRDEQueryV1(
        predicates=(
            CanonicalResearchPredicateV1(
                "BTCUSDT", "OI_STATE", "EQUALS", "UP", None, "AS_OF", "AVAILABLE", "oi"
            ),
        ),
        target_asset="BTCUSDT",
        target_metric="FORWARD_RETURN_BPS",
        target_horizon="6h",
        comparison_basis="MATCHED_CONTROL",
        expected_relationship="GREATER_THAN_MATCHED_CONTROL",
        source_intent_id="oi-intent",
        source_fact_ids=("oi",),
        compiler_identity="fixture-compiler",
        compiler_digest="fixture-digest",
    )


def _spy():
    calls = {"count": 0}

    def execute(query, identity, storage):
        calls["count"] += 1
        return execute_existing_cache_query(query, identity, storage)

    return calls, execute


def test_execution_cache_miss_then_reuse_avoids_executor_and_fixture_reads(tmp_path: Path) -> None:
    calls, executor = _spy()
    service = RDEResearchExecutionService(tmp_path, executor=executor)
    first_fixture = btc_fixture()
    first = service.execute(_query(first_fixture), first_fixture.identity, first_fixture)
    assert (first.status, first.cache_status, calls["count"]) == (
        "EXECUTED",
        CacheStatusV1.MISS,
        1,
    )
    assert first_fixture.reads == 3
    assert cache_path(tmp_path, first.cache_identity).is_file()

    second_fixture = btc_fixture()
    second = service.execute(_query(second_fixture), second_fixture.identity, second_fixture)
    assert (second.status, second.cache_status, calls["count"]) == (
        "EXECUTED",
        CacheStatusV1.REUSED,
        1,
    )
    assert second_fixture.reads == 0
    assert second.evidence == first.evidence


def test_outcome_and_event_match_dependencies_produce_separate_cache_records(
    tmp_path: Path,
) -> None:
    calls, executor = _spy()
    service = RDEResearchExecutionService(tmp_path, executor=executor)
    baseline = btc_fixture()
    baseline_result = service.execute(_query(baseline), baseline.identity, baseline)
    mutated = btc_fixture(mutated=True)
    mutated_result = service.execute(_query(mutated), mutated.identity, mutated)
    assert baseline_result.cache_identity != mutated_result.cache_identity
    assert mutated_result.cache_status == CacheStatusV1.MISS
    assert calls["count"] == 2
    assert cache_path(tmp_path, baseline_result.cache_identity).is_file()
    assert cache_path(tmp_path, mutated_result.cache_identity).is_file()
    assert research_evidence_digest(baseline_result.evidence) != research_evidence_digest(
        mutated_result.evidence
    )

    event_changed = replace(
        baseline,
        event_match_digest="changed-event-match",
        identity=replace(baseline.identity, event_match_content_digest="changed-event-match"),
    )
    event_changed_result = service.execute(
        _query(event_changed), event_changed.identity, event_changed
    )
    assert event_changed_result.cache_status == CacheStatusV1.MISS
    assert event_changed_result.cache_identity != baseline_result.cache_identity
    assert calls["count"] == 3


def test_changed_executor_identity_and_invalid_methodology_do_not_reuse(tmp_path: Path) -> None:
    fixture = btc_fixture()
    first = RDEResearchExecutionService(tmp_path).execute(
        _query(fixture), fixture.identity, fixture
    )
    changed = RDEResearchExecutionService(tmp_path, executor_digest="changed-executor-digest")
    changed_fixture = btc_fixture()
    second = changed.execute(_query(changed_fixture), changed_fixture.identity, changed_fixture)
    assert second.cache_status == CacheStatusV1.MISS
    assert first.cache_identity != second.cache_identity

    invalid_fixture = btc_fixture()
    invalid = changed.execute(
        _query(invalid_fixture),
        replace(invalid_fixture.identity, methodology_digest="invalid"),
        invalid_fixture,
    )
    assert invalid.status == "VERIFIED_CACHE_IDENTITY_INVALID"
    assert invalid_fixture.reads == 0


def test_corrupt_cache_is_not_reused_and_is_atomically_recomputed(tmp_path: Path) -> None:
    calls, executor = _spy()
    service = RDEResearchExecutionService(tmp_path, executor=executor)
    fixture = btc_fixture()
    first = service.execute(_query(fixture), fixture.identity, fixture)
    path = cache_path(tmp_path, first.cache_identity)
    path.write_text("not-json", encoding="utf-8")
    recompute_fixture = btc_fixture()
    second = service.execute(
        _query(recompute_fixture), recompute_fixture.identity, recompute_fixture
    )
    assert second.cache_status == CacheStatusV1.INVALID_RECOMPUTED
    assert calls["count"] == 2
    assert not list(path.parent.glob("*.tmp"))


def test_dry_run_no_cache_and_generic_oi_never_execute_fixture_storage(tmp_path: Path) -> None:
    calls, executor = _spy()
    service = RDEResearchExecutionService(tmp_path, executor=executor)
    fixture = btc_fixture()
    dry = service.execute(_query(fixture), fixture.identity, fixture, dry_run=True)
    assert (dry.status, dry.cache_status, calls["count"], fixture.reads) == (
        "EXECUTABLE_EXISTING_CACHE",
        CacheStatusV1.DRY_RUN,
        0,
        0,
    )

    no_cache_fixture = btc_fixture()
    uncached = service.execute(
        _query(no_cache_fixture), no_cache_fixture.identity, no_cache_fixture, no_cache=True
    )
    assert uncached.cache_status == CacheStatusV1.DISABLED
    assert not cache_path(tmp_path, uncached.cache_identity).exists()

    oi_fixture = btc_fixture()
    oi = service.execute_canonical(_canonical_oi(), oi_fixture.identity, oi_fixture)
    assert oi.status == "METHODOLOGY_EXTENSION_REQUIRED"
    assert calls["count"] == 1
    assert oi_fixture.reads == 0


def test_cache_identity_is_path_free_and_artifacts_are_deterministic(tmp_path: Path) -> None:
    fixture = btc_fixture()
    query = _query(fixture)
    assert execution_cache_identity(query) == execution_cache_identity(query)
    assert cache_path(Path("one"), execution_cache_identity(query)).relative_to(
        "one"
    ) == cache_path(Path("two"), execution_cache_identity(query)).relative_to("two")
    service = RDEResearchExecutionService(tmp_path / "cache")
    result = service.execute(query, fixture.identity, fixture)
    artifact_dir = service.write_artifacts(tmp_path / "results", query, result)
    assert {path.name for path in artifact_dir.iterdir()} == {
        "query.json",
        "capability.json",
        "execution_manifest.json",
        "evidence.json",
        "report.md",
    }
    assert "does not establish profitability" in (artifact_dir / "report.md").read_text(
        encoding="utf-8"
    )
