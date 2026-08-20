from app.research.data.cache_identity import (
    IDENTITY_SCHEMA,
    IdentityVerificationStatus,
    VerifiedResearchCacheIdentityV1,
    build_verified_research_cache_identity,
    verify_research_cache_identity,
)


def _identity(**changes: object) -> VerifiedResearchCacheIdentityV1:
    values: dict[str, object] = {
        "asset": "BTCUSDT",
        "aligned_content_digest": "a" * 64,
        "outcome_schema": "outcomes/v1",
        "outcome_config_digest": "b" * 64,
        "outcome_content_digest": "c" * 64,
        "outcome_verified": True,
        "event_match_schema": "bridge/v3",
        "event_match_config_digest": "d" * 64,
        "event_match_content_digest": "e" * 64,
        "event_match_verified": True,
    }
    values.update(changes)
    return VerifiedResearchCacheIdentityV1(**values)  # type: ignore[arg-type]


def test_atomic_event_match_identity_is_stable_and_path_free() -> None:
    first = _identity()
    assert IDENTITY_SCHEMA == "verified_research_cache_identity/1.0.0"
    assert first.research_cache_identity_digest == _identity().research_cache_identity_digest
    assert first.created_from_existing_verified_artifacts


def test_identity_verification_fails_closed_for_dependency_or_methodology_changes() -> None:
    identity = _identity()
    assert (
        verify_research_cache_identity(
            identity,
            asset="BTCUSDT",
            aligned_content_digest="a" * 64,
            outcome_content_digest="c" * 64,
            event_match_content_digest="e" * 64,
        )
        == IdentityVerificationStatus.VALID
    )
    assert (
        _identity(outcome_content_digest="x" * 64).research_cache_identity_digest
        != identity.research_cache_identity_digest
    )
    assert (
        verify_research_cache_identity(
            identity,
            asset="ETHUSDT",
            aligned_content_digest="a" * 64,
            outcome_content_digest="c" * 64,
            event_match_content_digest="e" * 64,
        )
        == IdentityVerificationStatus.INVALID
    )
    assert (
        verify_research_cache_identity(
            _identity(methodology_digest="z" * 64),
            asset="BTCUSDT",
            aligned_content_digest="a" * 64,
            outcome_content_digest="c" * 64,
            event_match_content_digest="e" * 64,
        )
        == IdentityVerificationStatus.INVALID
    )


def test_unverified_atomic_cache_is_rejected() -> None:
    identity = _identity(event_match_verified=False)
    assert (
        verify_research_cache_identity(
            identity,
            asset="BTCUSDT",
            aligned_content_digest="a" * 64,
            outcome_content_digest="c" * 64,
            event_match_content_digest="e" * 64,
        )
        == IdentityVerificationStatus.INVALID
    )


def test_manifest_adapter_requires_verified_cross_manifest_dependencies() -> None:
    aligned = {
        "asset": "BTCUSDT",
        "state": "VERIFIED",
        "alignment_schema": "a",
        "content_digest": "a" * 64,
    }
    outcome = {
        "asset": "BTCUSDT",
        "state": "VERIFIED",
        "outcome_schema": "o",
        "aligned_content_digest": "a" * 64,
        "outcome_config_digest": "b" * 64,
        "content_digest": "c" * 64,
    }
    event_match = {
        "asset": "BTCUSDT",
        "state": "VERIFIED",
        "bridge_schema": "e",
        "aligned_content_digest": "a" * 64,
        "outcome_content_digest": "c" * 64,
        "config_digest": "d" * 64,
        "content_digest": "e" * 64,
    }
    identity = build_verified_research_cache_identity(aligned, outcome, event_match)
    assert identity is not None
    event_match["outcome_content_digest"] = "x" * 64
    assert build_verified_research_cache_identity(aligned, outcome, event_match) is None
